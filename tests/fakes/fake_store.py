"""An in-memory Store that keeps only the contract-level promises (no SQLite, no word stemming),
plus deliberately broken variants. If the shared checks pass on both this and SqliteStore, the checks
describe the contract, not one engine."""

from __future__ import annotations

import contextlib
import copy
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import os

from dugup.protocols import OCR_DONE, OCR_FAILED, OCR_WAITING, OcrTask, PageInfo
from dugup.shapes import (sort_keys, Box, Evidence, FileKind, FileRecord, FileState, Passage, Root, SegmentIn, SegmentKind)
from dugup.store import SORT_COLUMNS, SearchPage, SearchRow, StoreError, parse_query

FakeSearchRow = SearchRow   # the real row carries evidence now; the old name is kept for the broken variants


def tokens(text: str) -> List[str]:
    """Words the way the contract sees them: letters, numbers and marks; case and accents ignored."""
    plain = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    out, current = [], []
    for ch in plain.casefold():
        cat = unicodedata.category(ch)
        if cat[0] in "LNM" or cat == "Co":
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _contains(haystack: List[str], needle: List[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _one_of_each(places):
    """Every way to take one position from each list (small lists only: this is a fake)."""
    combos = [[]]
    for options in places:
        combos = [c + [o] for c in combos for o in options[:20]][:2000]
    return combos


def _split(path: str) -> Tuple[str, str]:
    cut = max(path.rfind("/"), path.rfind("\\"))
    return ("", path) if cut < 0 else (path[:cut], path[cut + 1:])


def _ascii_lower(text: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in text)


class FakeStore:
    def __init__(self) -> None:
        self.files: Dict[int, FileRecord] = {}
        self.segments: Dict[int, List[SegmentIn]] = {}
        self.fingerprint: Optional[str] = None
        self.next_id = 1
        self.pages: Dict[int, List[PageInfo]] = {}
        self.roots: Dict[str, Root] = {}
        self.segment_ids: Dict[int, List[int]] = {}   # row id -> the id of each stored segment, in order
        self.next_segment = 1
        self.ocr: Dict[Tuple[int, int], dict] = {}      # (row id, page) -> state, reason, width, height, camera
        self.lines: Dict[int, list] = {}                # segment id -> [(text, Box)] of an OCR segment

    # -- plumbing
    def close(self) -> None:
        pass

    def _state(self):
        return (self.files, self.segments, self.fingerprint, self.next_id, self.pages, self.roots,
                self.segment_ids, self.next_segment, self.ocr, self.lines)

    @contextlib.contextmanager
    def transaction(self):
        saved = copy.deepcopy(self._state())
        try:
            yield
        except BaseException:
            self._restore(saved)
            raise

    def _restore(self, saved) -> None:
        (self.files, self.segments, self.fingerprint, self.next_id, self.pages, self.roots,
         self.segment_ids, self.next_segment, self.ocr, self.lines) = copy.deepcopy(saved)

    # -- fingerprint
    def set_fingerprint(self, fingerprint) -> None:
        try:
            self.fingerprint = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
        except TypeError as exc:
            raise ValueError(f"fingerprint must be plain JSON values: {exc}") from exc

    def get_fingerprint(self):
        return None if self.fingerprint is None else json.loads(self.fingerprint)

    def changed_fingerprint_keys(self, current) -> List[str]:
        stored = self.get_fingerprint() or {}
        keys = set(stored) | set(current)
        return sorted(k for k in keys if stored.get(k) != current.get(k) or (k in stored) != (k in current))

    # -- files and segments
    def _find(self, path: str) -> Optional[int]:
        for row_id, rec in self.files.items():
            if rec.path == path:
                return row_id
        return None

    def upsert_file(self, record: FileRecord) -> int:
        row_id = self._find(record.path)
        if row_id is None:
            row_id = self.next_id
            self.next_id += 1
            self.segments[row_id] = []
            self.segment_ids[row_id] = []
            self.pages[row_id] = []
        old = self.files.get(row_id)
        if old is not None and (old.size, old.modified) != (record.size, record.modified) and hasattr(self, "looked"):
            self.looked.pop(row_id, None)          # a changed picture wants a new look
        self.files[row_id] = FileRecord(record.path, record.size, record.modified, record.kind, record.file_id,
                                        record.state, record.error, record.policy, row_id, record.missing_since)
        record.row_id = row_id
        return row_id

    def _fold(self, case_insensitive):
        if case_insensitive is None:
            case_insensitive = os.name == "nt"
        return (lambda t: t.lower()) if case_insensitive else (lambda t: t)

    def _below(self, prefix, case_insensitive):
        fold = self._fold(case_insensitive)
        base = prefix.rstrip("/\\")
        return [i for i in sorted(self.files) if self._under(self.files[i].path, base, fold)]

    def list_files(self, prefix=None, state=None, limit=None, after_row=0, missing=None, case_insensitive=None):
        if state is not None and not isinstance(state, FileState):
            raise ValueError("state must be a FileState")
        if limit is not None and limit < 0:
            raise ValueError("limit must not be negative")
        ids = sorted(self.files) if prefix is None else self._below(prefix, case_insensitive)
        out = []
        for row_id in ids:
            rec = self.files[row_id]
            if row_id <= after_row or (state is not None and rec.state is not state):
                continue
            if missing is not None and (rec.missing_since is not None) != missing:
                continue
            out.append(copy.copy(rec))
        return out if limit is None else out[:limit]

    # -- the same content in several places (kept beside the records: FileRecord does not carry it)
    def set_content(self, row_id: int, mark) -> None:
        if not hasattr(self, "contents"):
            self.contents = {}
        if mark:
            self.contents[row_id] = mark
        else:
            self.contents.pop(row_id, None)

    def content_of(self, row_id: int):
        return getattr(self, "contents", {}).get(row_id) if row_id in self.files else None

    def _marked(self):
        return {i: m for i, m in getattr(self, "contents", {}).items()
                if i in self.files and self.files[i].missing_since is None}

    def copies_of(self, row_ids):
        marked, out = self._marked(), {}
        every = getattr(self, "contents", {})
        for row_id in row_ids:
            mark = every.get(row_id) if row_id in self.files else None
            others = sorted((self.files[i].path for i, m in marked.items() if m == mark and i != row_id),
                            key=lambda p: (len(p), p)) if mark else []
            if others:
                out[row_id] = others
        return out

    def same_content(self, prefix=None, case_insensitive=None):
        marked = self._marked()
        ids = set(self.files) if prefix is None else set(self._below(prefix, case_insensitive))
        sets = {}
        for row_id in sorted(self.files):
            mark = marked.get(row_id)
            if row_id in ids and mark and not mark.startswith("!"):
                sets.setdefault(mark, []).append(copy.copy(self.files[row_id]))
        return [members for members in sets.values() if len(members) > 1]

    def files_in_folders(self, folders):
        counts = {}
        for rec in self.files.values():
            if rec.missing_since is None:
                folder, _ = _split(rec.path)
                counts[folder] = counts.get(folder, 0) + 1
        return {f: counts.get(f, 0) for f in folders}

    # -- sketches of meaning
    def _sk(self):
        if not hasattr(self, "sketches"):
            self.sketches, self.sketched = {}, {}       # segment id -> (file, model, dims, scale, sketch, fp); file -> model
        return self.sketches

    def unsketched(self, model, limit=200, prefix=None, case_insensitive=None, after=None):
        self._sk()
        read = (FileState.DONE, FileState.PARTIAL)
        ids = set(self.files) if prefix is None else set(self._below(prefix, case_insensitive))
        found = [r for i, r in self.files.items() if i in ids and r.missing_since is None and r.state in read
                 and self.sketched.get(i, "") != model and any(s.kind is not SegmentKind.NAME for s in self.segments.get(i, []))
                 and (after is None or (r.modified, -i) < (after[0], -after[1]))]
        found.sort(key=lambda r: (-r.modified, r.row_id))
        return [copy.copy(r) for r in found[:max(0, limit)]]

    def text_of(self, row_id):
        return [(seg_id, seg.page, seg.text) for seg, seg_id in zip(self.segments.get(row_id, []), self.segment_ids.get(row_id, []))
                if seg.kind is not SegmentKind.NAME]

    def save_sketches(self, row_id, model, dims, rows):
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        sk = self._sk()
        for seg_id in [k for k, v in sk.items() if v[0] == row_id]:
            del sk[seg_id]
        for seg, scale, sketch, fingerprint in rows:
            sk[seg] = (row_id, model, dims, scale, sketch, fingerprint)
        self.sketched[row_id] = model

    def forget_sketches(self, row_id):
        sk = self._sk()
        for seg_id in [k for k, v in sk.items() if v[0] == row_id]:
            del sk[seg_id]
        self.sketched.pop(row_id, None)

    def sketch_counts(self, model):
        self._sk()
        read = (FileState.DONE, FileState.PARTIAL)
        done = sum(1 for i, m in self.sketched.items() if m == model and i in self.files and self.files[i].missing_since is None)
        could = sum(1 for i, r in self.files.items() if r.missing_since is None and r.state in read
                    and any(s.kind is not SegmentKind.NAME for s in self.segments.get(i, [])))
        return done, could

    def fingerprints(self, model):
        import numpy as np
        from dugup.sketches import FINGERPRINT
        sk = self._sk()
        rows = sorted((seg, v[5]) for seg, v in sk.items() if v[1] == model and v[0] in self.files and self.files[v[0]].missing_since is None)
        ids = np.array([seg for seg, _ in rows], dtype=np.int64)
        files = np.array([sk[seg][0] for seg, _ in rows], dtype=np.int64)
        matrix = np.frombuffer(b"".join(fp for _, fp in rows), dtype=np.int8).reshape(len(rows), FINGERPRINT) if rows \
            else np.zeros((0, FINGERPRINT), dtype=np.int8)
        return ids, files, matrix

    def search_meaning(self, question, model, limit=100, offset=0, filters=None, candidates=None, sort="rank", descending=False):
        import numpy as np
        from dugup.sketches import closeness, dequantize
        sk = self._sk()
        best = {}
        for seg, (file_id, m, dims, scale, sketch, _) in sk.items():
            if m != model or file_id not in self.files:
                continue
            score = float(closeness(dequantize(sketch, dims, [scale]), np.asarray(question, dtype=np.float32)[:dims])[0])
            if file_id not in best or score > best[file_id][0]:
                best[file_id] = (score, seg)
        ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
        rows = []
        for file_id, (score, seg) in ordered[offset:offset + limit]:
            r = self.files[file_id]
            page = next((s.page for s, i in zip(self.segments.get(file_id, []), self.segment_ids.get(file_id, [])) if i == seg), 0)
            rows.append(SearchRow(file_id, r.path, os.path.basename(r.path), os.path.dirname(r.path), r.size, r.modified,
                                  r.kind.value if hasattr(r.kind, "value") else str(r.kind), r.state.value, 1, score,
                                  (Evidence(file_id, page, 0, 0, None, seg),), missing_since=r.missing_since))
        return SearchPage(rows, len(ordered), offset, limit)

    def closeness_of_files(self, question, model, file_ids):
        import numpy as np
        from dugup.sketches import closeness, dequantize
        sk = self._sk()
        found = {}
        for seg, (file_id, m, dims, scale, sketch, _) in sk.items():
            if m == model and file_id in set(file_ids):
                score = float(closeness(dequantize(sketch, dims, [scale]), np.asarray(question, dtype=np.float32)[:dims])[0])
                if file_id not in found or score > found[file_id][0]:
                    found[file_id] = (score, seg)
        return found

    def word_shares(self, words):
        from dugup.store import parse_query
        pieces = sum(1 for v in self.segments.values() for seg in v if seg.kind is not SegmentKind.NAME)
        found = {}
        for w in words:
            if " " in w.strip() or not pieces:
                found[w] = 0.0
                continue
            folded = w.casefold()
            holding = sum(1 for segs in self.segments.values() for seg in segs
                          if seg.kind is not SegmentKind.NAME and folded in seg.text.casefold().split())
            found[w] = holding / pieces
        return found

    def telling_words(self, query, everywhere=0.3):
        from dugup.store import parse_query
        from dugup.store import EVERYWHERE_MIN_PIECES
        terms = parse_query(query)
        if everywhere <= 0 or sum(1 for v in self.segments.values() for seg in v if seg.kind is not SegmentKind.NAME) < EVERYWHERE_MIN_PIECES:
            return terms
        shares = self.word_shares([t for t in terms if " " not in t])
        return [t for t in terms if " " in t or shares.get(t, 0.0) < everywhere]

    def search_both(self, query, question, model, limit=100, offset=0, filters=None, word_weight=0.5, word_pool=2000,
                    everywhere=0.3, sort="rank", descending=False):
        from dataclasses import replace as _replace
        from dugup.shapes import WHERE_ANYWHERE
        telling = self.telling_words(query, everywhere)
        words = self.search_words(" ".join(telling), max(1, word_pool), 0, "rank", False,
                                  _replace(filters, where=WHERE_ANYWHERE) if filters is not None else None) if telling \
            else SearchPage([], 0, 0, 0)
        meaning = self.search_meaning(question, model, 800, 0, filters)
        word_rows = {r.file_id: r for r in words.rows}
        meaning_rows = {r.file_id: r for r in meaning.rows}
        # the fake's word ranks are not bm25: the share falls off by place instead
        shares = {r.file_id: 1.0 - i / max(1, len(words.rows)) for i, r in enumerate(words.rows)}
        near = self.closeness_of_files(question, model, [i for i in word_rows if i not in meaning_rows])
        scored = []
        for file_id in set(word_rows) | set(meaning_rows):
            close = meaning_rows[file_id].rank if file_id in meaning_rows else max(0.0, near.get(file_id, (0.0, 0))[0])
            share = shares.get(file_id, 0.0)
            scored.append((close + word_weight * max(0.0, min(1.0, share)), file_id))
        scored.sort(key=lambda t: (-t[0], t[1]))
        rows = []
        for score, file_id in scored[offset:offset + limit]:
            base = word_rows.get(file_id) or meaning_rows[file_id]
            evidence = list(word_rows[file_id].evidence) if file_id in word_rows else []
            if file_id in meaning_rows:
                evidence.extend(e for e in meaning_rows[file_id].evidence if e.segment_id not in {w.segment_id for w in evidence})
            rows.append(_replace(base, rank=score, evidence=tuple(evidence)))
        return SearchPage(rows, len(scored), offset, limit)

    # -- pictures by likeness
    def _lk(self):
        if not hasattr(self, "looks"):
            self.looks, self.looked = {}, {}          # file -> (model, scale, sketch, fp, note); file -> model
        return self.looks

    def unlooked(self, model, limit=200, after=None):
        self._lk()
        found = [r for i, r in self.files.items() if r.missing_since is None and r.kind is FileKind.IMAGE
                 and self.looked.get(i, "") != model and (after is None or (r.modified, -i) < (after[0], -after[1]))]
        found.sort(key=lambda r: (-r.modified, r.row_id))
        return [copy.copy(r) for r in found[:max(0, limit)]]

    def save_looks(self, model, rows):
        lk = self._lk()
        for f, scale, sketch, fp, note in rows:
            lk[f] = (model, scale, sketch, fp, note or "")
            self.looked[f] = model

    def look_counts(self, model):
        self._lk()
        done = sum(1 for i, m in self.looked.items() if m == model and i in self.files and self.files[i].missing_since is None)
        could = sum(1 for r in self.files.values() if r.missing_since is None and r.kind is FileKind.IMAGE)
        unread = sum(1 for i, v in self.looks.items() if v[0] == model and v[2] is None and i in self.files and self.files[i].missing_since is None)
        return done, could, unread

    def unread_pictures(self, model, limit=1000):
        self._lk()
        return sorted((self.files[i].path, v[4]) for i, v in self.looks.items() if v[0] == model and v[2] is None and i in self.files)[:limit]

    def look_fingerprints(self, model):
        import numpy as np
        from dugup.sketches import FINGERPRINT
        lk = self._lk()
        rows = sorted((i, v[3]) for i, v in lk.items() if v[0] == model and v[3] is not None and i in self.files
                      and self.files[i].missing_since is None and self.looked.get(i) == model)
        ids = np.array([i for i, _ in rows], dtype=np.int64)
        matrix = np.frombuffer(b"".join(fp for _, fp in rows), dtype=np.int8).reshape(len(rows), FINGERPRINT) if rows \
            else np.zeros((0, FINGERPRINT), dtype=np.int8)
        return ids, matrix

    def looks_of(self, file_ids):
        lk = self._lk()
        return {i: (lk[i][1], lk[i][2]) for i in file_ids if i in lk and lk[i][2] is not None and self.looked.get(i) == lk[i][0]}

    def look_of(self, file_id):
        from dugup.embedding import LOOKS_DIMS
        from dugup.sketches import dequantize
        found = self.looks_of([file_id])
        return None if file_id not in found else dequantize(found[file_id][1], LOOKS_DIMS, [found[file_id][0]])[0]

    def search_looks(self, question, model, limit=100, offset=0, filters=None, candidates=None, sort="rank", descending=False, not_file=None):
        import numpy as np
        from dugup.embedding import LOOKS_DIMS
        from dugup.sketches import closeness, dequantize
        lk = self._lk()
        scored = []
        for i, (m, scale, sketch, fp, note) in lk.items():
            if m != model or sketch is None or i not in self.files or i == not_file or self.files[i].missing_since is not None:
                continue
            if self.looked.get(i) != model:         # a changed picture's old sketch is not evidence
                continue
            if not self._file_passes(self.files[i], filters):
                continue
            scored.append((float(closeness(dequantize(sketch, LOOKS_DIMS, [scale]), np.asarray(question, dtype=np.float32))[0]), i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        keys = sort_keys(sort, descending, SORT_COLUMNS)
        if keys != [("rank", False)]:
            for key, down in reversed(keys):
                scored.sort(key=lambda t: (-t[0] if key == "rank" else 1 if key == "matches" else
                                            (lambda v: v.casefold() if isinstance(v, str) else v)(getattr(self.files[t[1]], key if key != "name" else "path"))),
                            reverse=down)
        rows = []
        for score, i in scored[offset:offset + limit]:
            r = self.files[i]
            rows.append(SearchRow(i, r.path, os.path.basename(r.path), os.path.dirname(r.path), r.size, r.modified,
                                  r.kind.value if hasattr(r.kind, "value") else str(r.kind), r.state.value, 1, score, (),
                                  missing_since=r.missing_since))
        return SearchPage(rows, len(scored), offset, limit)

    def sketches_of(self, segment_ids):
        sk = self._sk()
        return {seg: (sk[seg][0], sk[seg][2], sk[seg][3], sk[seg][4]) for seg in segment_ids if seg in sk}

    def read_marks(self):
        read = (FileState.DONE, FileState.PARTIAL)
        return sorted({(m, self.files[i].kind.value) for i, m in self._marked().items() if self.files[i].state in read})

    def read_twin(self, mark, not_row, kind):
        read = (FileState.DONE, FileState.PARTIAL)
        found = sorted((self.files[i].state is not FileState.DONE, i) for i, m in self._marked().items()
                       if m == mark and i != not_row and self.files[i].state in read and self.files[i].kind is kind)
        return copy.copy(self.files[found[0][1]]) if found else None

    def _copy_segments(self, from_row, to_row, wanted):
        for seg, seg_id in zip(list(self.segments[from_row]), list(self.segment_ids[from_row])):
            if wanted(seg):
                self.segments[to_row].append(seg)
                self.segment_ids[to_row].append(self.next_segment)
                if seg_id in self.lines:
                    self.lines[self.next_segment] = list(self.lines[seg_id])
                self.next_segment += 1

    def copy_reading(self, from_row, to_row) -> None:
        for row in (from_row, to_row):
            if row not in self.files:
                raise StoreError(f"no file with id {row}")
        self.replace_segments(to_row, [])
        self._copy_segments(from_row, to_row, lambda seg: True)
        self.pages[to_row] = list(self.pages.get(from_row, []))
        self._forget_ocr(to_row)
        for (row, page), entry in list(self.ocr.items()):
            if row == from_row:
                self.ocr[(to_row, page)] = dict(entry)

    def copy_page(self, from_row, to_row, page) -> None:
        source, index = self._page_index(from_row, page), self._page_index(to_row, page)
        self._drop_ocr_text(to_row, page)
        self._copy_segments(from_row, to_row, lambda seg: seg.kind is SegmentKind.OCR and seg.page == page)
        was = self.pages[from_row][source]
        info = self.pages[to_row][index]
        self.pages[to_row][index] = PageInfo(info.page, info.verdict, was.needs_ocr, info.unread_reason)
        entry = dict(self._ocr_entry(from_row, page))
        mine = self._ocr_entry(to_row, page)
        mine.update(state=entry["state"], reason=entry["reason"], engine=entry["engine"])

    def unmarked(self, prefix=None, limit=None, after_row=0, case_insensitive=None):
        read = (FileState.DONE, FileState.PARTIAL, FileState.FAILED)
        every = getattr(self, "contents", {})
        out = [r for r in self.list_files(prefix, None, None, after_row, False, case_insensitive)
               if r.state in read and r.row_id not in every]
        return out if limit is None else out[:max(0, limit)]

    def copy_counts(self):
        marked = self._marked()
        sizes = {}
        for mark in marked.values():
            sizes[mark] = sizes.get(mark, 0) + 1
        return len(marked), sum(1 for n in sizes.values() if n > 1), sum(n - 1 for n in sizes.values() if n > 1)

    def move_file(self, old_path: str, new_path: str) -> int:
        row_id = self._find(old_path)
        if row_id is None:
            raise StoreError("cannot move a file the index does not know")
        if old_path != new_path and self._find(new_path) is not None:
            raise StoreError("cannot move a file onto a path the index already holds")
        self._moved(row_id, new_path)
        return row_id

    def _moved(self, row_id: int, new_path: str) -> None:
        self.files[row_id].path = new_path
        self.files[row_id].missing_since = None

    def set_missing(self, row_id: int, since) -> None:
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        self.files[row_id].missing_since = since

    def _drop(self, row_id: int) -> None:
        for seg_id in self.segment_ids.get(row_id, []):
            self.lines.pop(seg_id, None)
        for table in (self.files, self.segments, self.segment_ids, self.pages):
            table.pop(row_id, None)
        self._forget_ocr(row_id)

    def _forget_ocr(self, row_id: int) -> None:
        for key in [k for k in self.ocr if k[0] == row_id]:
            del self.ocr[key]

    def remove_files(self, row_ids) -> int:
        present = [i for i in row_ids if i in self.files]
        for row_id in present:
            self._drop(row_id)
        return len(present)

    def purge_missing(self, prefix: str, before: float, case_insensitive=None) -> int:
        doomed = [i for i in self._below(prefix, case_insensitive)
                  if self.files[i].missing_since is not None and self.files[i].missing_since < before]
        for row_id in doomed:
            self._drop(row_id)
        return len(doomed)

    def set_pages(self, row_id: int, pages) -> None:
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        self.pages[row_id] = sorted(pages, key=lambda p: p.page)
        self._forget_ocr(row_id)

    def get_pages(self, row_id: int):
        return list(self.pages.get(row_id, []))

    # -- OCR
    def _page_index(self, row_id: int, page: int) -> int:
        for index, info in enumerate(self.pages.get(row_id, [])):
            if info.page == page:
                return index
        raise StoreError(f"file {row_id} has no page {page}")

    def _ocr_entry(self, row_id: int, page: int) -> dict:
        self._page_index(row_id, page)
        return self.ocr.setdefault((row_id, page), {"state": "", "reason": "", "width": None, "height": None,
                                                    "camera": False, "engine": ""})

    def ocr_queue(self, prefix=None, case_insensitive=None):
        ids = sorted(self.files) if prefix is None else self._below(prefix, case_insensitive)
        out = []
        for row_id in ids:
            rec = self.files[row_id]
            if rec.missing_since is not None:
                continue
            for info in self.pages.get(row_id, []):
                entry = self.ocr.get((row_id, info.page), {})
                if info.needs_ocr and entry.get("state", "") in ("", "retry"):
                    out.append(OcrTask(row_id, rec.path, rec.kind, info.page, entry.get("width"), entry.get("height"),
                                       bool(entry.get("camera")), info.verdict.value, entry.get("state") == "retry"))
        return out

    def set_page_shape(self, row_id, page, width, height, camera) -> None:
        self._ocr_entry(row_id, page).update(width=int(width), height=int(height), camera=bool(camera))

    def _drop_ocr_text(self, row_id: int, page: int) -> None:
        keep = [(seg, seg_id) for seg, seg_id in zip(self.segments[row_id], self.segment_ids[row_id])
                if not (seg.kind is SegmentKind.OCR and seg.page == page)]
        for seg_id in set(self.segment_ids[row_id]) - {i for _, i in keep}:
            self.lines.pop(seg_id, None)
        self.segments[row_id] = [seg for seg, _ in keep]
        self.segment_ids[row_id] = [seg_id for _, seg_id in keep]

    def save_ocr(self, row_id, page, result) -> None:
        index = self._page_index(row_id, page)
        self._drop_ocr_text(row_id, page)
        lines = [(" ".join(l.text.split()), l.box) for l in result.lines]
        lines = [(t, b) for t, b in lines if t]
        if lines:
            self.segments[row_id].append(SegmentIn(page, 1_000_000, SegmentKind.OCR, "\n".join(t for t, _ in lines)))
            self.segment_ids[row_id].append(self.next_segment)
            self.lines[self.next_segment] = lines
            self.next_segment += 1
        self._mark_read(row_id, page, index, f"{result.engine} {result.version}".strip())

    def _mark_read(self, row_id, page, index, engine="") -> None:
        info = self.pages[row_id][index]
        self.pages[row_id][index] = PageInfo(info.page, info.verdict, False, info.unread_reason)
        self._ocr_entry(row_id, page).update(state="done", reason="", engine=engine)

    def defer_ocr(self, row_id, page, reason) -> None:
        self._ocr_entry(row_id, page).update(state="retry", reason=reason)

    def _kept_pages(self, row_id, pages):
        return {p.page for p in pages if p.needs_ocr and self.ocr.get((row_id, p.page), {}).get("state") == "done"}

    def replace_text(self, row_id, segments, pages) -> None:
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        keep = self._kept_pages(row_id, pages)
        old = {page: dict(entry) for (row, page), entry in self.ocr.items() if row == row_id}
        saved = [(seg, self.lines.get(seg_id, [])) for seg, seg_id in zip(self.segments[row_id], self.segment_ids[row_id])
                 if seg.kind is SegmentKind.OCR and seg.page in keep]
        self.replace_segments(row_id, segments)
        for seg, lines in saved:
            self.segments[row_id].append(seg)
            self.segment_ids[row_id].append(self.next_segment)
            self.lines[self.next_segment] = lines
            self.next_segment += 1
        self.set_pages(row_id, pages)
        for index, info in enumerate(self.pages[row_id]):
            before = old.get(info.page)
            if before is None:
                continue
            if info.page in keep:
                self._mark_read(row_id, info.page, index, before.get("engine", ""))
            if before.get("width") is not None:
                self._ocr_entry(row_id, info.page).update(width=before["width"], height=before["height"], camera=before["camera"])

    def ocr_engines(self):
        counts: Dict[str, int] = {}
        for (row, _), entry in self.ocr.items():
            if entry["state"] == "done" and self.files[row].missing_since is None:
                counts[entry["engine"]] = counts.get(entry["engine"], 0) + 1
        return counts

    def requeue_ocr_not_by(self, engine, prefix=None, case_insensitive=None):
        ids = set(self.files) if prefix is None else set(self._below(prefix, case_insensitive))
        found = set()
        for (row, page), entry in self.ocr.items():
            if row in ids and entry["state"] == "done" and entry["engine"] != engine:
                entry.update(state="")
                index = self._page_index(row, page)
                info = self.pages[row][index]
                self.pages[row][index] = PageInfo(info.page, info.verdict, True, info.unread_reason)
                found.add(row)
        return sorted(found)

    def fail_ocr(self, row_id, page, reason) -> None:
        self._ocr_entry(row_id, page).update(state="failed", reason=reason)

    def retry_ocr(self, prefix=None, case_insensitive=None):
        ids = set(self.files) if prefix is None else set(self._below(prefix, case_insensitive))
        found = sorted({row for (row, _), entry in self.ocr.items() if entry["state"] == "failed" and row in ids})
        for (row, _), entry in self.ocr.items():
            if row in found and entry["state"] == "failed":
                entry.update(state="", reason="")
        return found

    def ocr_status(self, row_id):
        out = []
        for info in self.pages.get(row_id, []):
            entry = self.ocr.get((row_id, info.page), {})
            state = entry.get("state", "")
            if info.needs_ocr or state:
                names = {"": OCR_WAITING, "retry": OCR_WAITING, "done": OCR_DONE, "failed": OCR_FAILED}
                out.append((info.page, names[state], entry.get("reason", "") if state == "failed" else ""))
        return out

    def _line_box(self, handle, start):
        at = 0
        for text, box in self.lines.get(handle, ()):
            at += len(text) + 1
            if start < at:
                return box
        return None

    def state_counts(self, prefix=None, case_insensitive=None):
        ids = sorted(self.files) if prefix is None else self._below(prefix, case_insensitive)
        counts: Dict[str, int] = {}
        for row_id in ids:
            key = self._count_key(self.files[row_id])
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _count_key(self, rec: FileRecord) -> str:
        return "missing" if rec.missing_since is not None else rec.state.value

    # -- roots
    def list_roots(self):
        return [self.roots[k] for k in sorted(self.roots)]

    def set_root(self, root: Root) -> None:
        old = self.roots.get(root.path)
        self.roots[root.path] = self._merged(old, root)

    def _merged(self, old, root: Root) -> Root:
        if old is None:
            return Root(root.path, root.policy, tuple(root.include), tuple(root.exclude), levels=tuple(root.levels))
        return Root(root.path, root.policy, tuple(root.include), tuple(root.exclude), old.reachable, old.last_scan,
                    old.warnings, tuple(root.levels))

    def note_scan(self, path: str, reachable: bool, when: float, warnings=()) -> None:
        if path not in self.roots:
            raise StoreError("that folder is not one of the roots")
        old = self.roots[path]
        self.roots[path] = Root(old.path, old.policy, old.include, old.exclude, bool(reachable), when, tuple(warnings),
                                old.levels)

    def remove_root(self, path: str) -> bool:
        return self.roots.pop(path, None) is not None

    def get_file(self, path: str) -> Optional[FileRecord]:
        row_id = self._find(path)
        return None if row_id is None else copy.copy(self.files[row_id])

    def get_file_by_id(self, row_id):
        return copy.copy(self.files[row_id]) if row_id in self.files else None

    def set_state(self, row_id: int, state, error: str = "") -> None:
        if not isinstance(state, FileState):
            raise ValueError("state must be a FileState")
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        self.files[row_id].state = state
        self.files[row_id].error = error

    def replace_segments(self, row_id: int, segments: Sequence[SegmentIn]) -> None:
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        if any(s.kind is SegmentKind.NAME for s in segments):
            raise ValueError("name segments are managed by the store")
        for seg_id in self.segment_ids.get(row_id, []):
            self.lines.pop(seg_id, None)
        self.segments[row_id] = list(segments)
        self.segment_ids[row_id] = list(range(self.next_segment, self.next_segment + len(segments)))
        self.next_segment += len(segments)
        if hasattr(self, "sketches"):
            self.forget_sketches(row_id)

    def file_count(self) -> int:
        return len(self.files)

    def segment_count(self, include_names: bool = False) -> int:
        total = sum(len(v) for v in self.segments.values())
        return total + (len(self.files) if include_names else 0)

    # -- search
    def _rows_of(self, row_id: int):
        """(name, body, page) for the name row and each text row of a file."""
        name = _split(self.files[row_id].path)[1]
        yield name, "", 0
        for seg in self.segments[row_id]:
            yield name, seg.text, seg.page

    def _segment_handles(self, row_id: int):
        """The handle of the name row (minus the row id) and of each text row, in the order of _rows_of."""
        ids = self.segment_ids.get(row_id, [])
        ids = ids if len(ids) == len(self.segments[row_id]) else [None] * len(self.segments[row_id])
        return [-row_id] + list(ids)

    @staticmethod
    def _span(text: str, terms) -> Tuple[int, int]:
        """Where the first query word sits in the text (plain words only; enough for a fake)."""
        folded = text.casefold()
        for term in terms:
            at = folded.find(term[0])
            if at >= 0:
                return at, at + len(term[0])
        return 0, 0

    def get_passage(self, evidence: Evidence, context: int = 80):
        row_id = evidence.file_id
        if evidence.segment_id is None or row_id not in self.files:
            return None
        if evidence.segment_id == -row_id:
            text, page, kind = _split(self.files[row_id].path)[1], 0, SegmentKind.NAME
        else:
            ids = self.segment_ids.get(row_id, [])
            if evidence.segment_id not in ids:
                return None
            seg = self.segments[row_id][ids.index(evidence.segment_id)]
            text, page, kind = seg.text, seg.page, seg.kind
        start = min(max(evidence.start, 0), len(text))
        end = min(max(evidence.end, start), len(text))
        left, right = max(0, start - max(context, 0)), min(len(text), end + max(context, 0))
        return Passage(text[left:right], start - left, end - left, page, kind)

    @staticmethod
    def _terms(query: str) -> List[List[str]]:
        return [t for t in (tokens(term) for term in parse_query(query)) if t]

    near_words = 15

    def _tier(self, row_id: int, query: str) -> int:
        """0 when a piece of the file holds the words as one phrase in the order typed, 1 when it holds them
        close together, 2 otherwise. One word alone has nothing to be close to."""
        terms = self._terms(query)
        words = [w for term in terms for w in term]
        if len(words) < 2:
            return 0
        best = 2
        for name, body, _ in self._rows_of(row_id):
            for text in (tokens(name), tokens(body)):
                if _contains(text, words):
                    return 0
                if all(_contains(text, term) for term in terms):
                    places = [[i for i in range(len(text)) if text[i:i + len(term)] == term] for term in terms]
                    spans = [max(p) - min(p) for p in _one_of_each(places)]
                    if spans and min(spans) <= self.near_words + len(words):
                        best = 1
        return best

    def _row_matches(self, name: str, body: str, terms: List[List[str]]) -> bool:
        name_t, body_t = tokens(name), tokens(body)
        return all(_contains(name_t, t) or _contains(body_t, t) for t in terms)

    def _file_passes(self, rec: FileRecord, filters) -> bool:
        if filters is None:
            return True
        if filters.folder is not None and not self._under(rec.path, filters.folder.rstrip("/\\"),
                                                          self._fold(filters.case_insensitive)):
            return False
        if filters.kinds and rec.kind.value not in filters.kinds:
            return False
        if filters.modified_after is not None and rec.modified < filters.modified_after:
            return False
        if filters.modified_before is not None and rec.modified >= filters.modified_before:
            return False
        if filters.min_size is not None and rec.size < filters.min_size:
            return False
        return not (filters.max_size is not None and rec.size > filters.max_size)

    def _where_passes(self, where: str, index: int, kind, name: str, body: str, terms) -> bool:
        """Whether a row counts for the place the words are asked for (the name row is index 0)."""
        if where == "anywhere":
            return self._row_matches(name, body, terms)
        if where == "name":
            return index == 0 and self._row_matches(name, "", terms)
        if index == 0 or (where == "ocr" and kind is not SegmentKind.OCR):
            return False
        return self._row_matches("", body, terms)

    def _hits(self, query: str, filters=None):
        terms = self._terms(query)
        if not terms:
            return {}
        where = "anywhere" if filters is None else filters.where
        found: Dict[int, List[tuple]] = {}
        for row_id in self.files:
            if not self._file_passes(self.files[row_id], filters):
                continue
            handles = self._segment_handles(row_id)
            kinds = [None] + [seg.kind for seg in self.segments[row_id]]
            for index, (name, body, page) in enumerate(self._rows_of(row_id)):
                if self._where_passes(where, index, kinds[index] if index < len(kinds) else None, name, body, terms):
                    handle = handles[index] if index < len(handles) else None
                    in_body = any(_contains(tokens(body), t) for t in terms)
                    # a piece of text that matched only through the file name counts as a match,
                    # but it is the name row that points at the words
                    span = self._span(body, terms) if in_body else (self._span(name, terms) if index == 0 else None)
                    found.setdefault(row_id, []).append((page, span, handle))
        return found

    def file_evidence(self, file_id, query, filters=None, limit=2000):
        from dugup.shapes import SearchFilters
        place = None if filters is None else SearchFilters(where=filters.where)    # only the place applies to one file
        hits = self._hits(query, place).get(file_id, [])
        ordered = self._in_reading_order([h for h in hits if h[1] is not None])
        return [Evidence(file_id, page, span[0], span[1], self._line_box(handle, span[0]), handle)
                for page, span, handle in ordered][:max(limit, 0)]

    def _in_reading_order(self, hits):
        return sorted(hits, key=lambda h: (h[2] is not None and h[2] < 0, h[0], h[2] or 0))

    def _sort_key(self, sort: str, row: SearchRow):
        return {
            "name": _ascii_lower(row.name), "folder": _ascii_lower(row.folder), "size": row.size,
            "modified": row.modified, "kind": row.kind, "matches": row.matches, "rank": row.rank,
        }[sort]

    def search_words(self, query: str, limit: int = 100, offset: int = 0, sort="rank",
                     descending: bool = False, filters=None) -> SearchPage:
        keys = self._keys(sort, descending)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        found = self._hits(query, filters)
        rows = []
        for row_id, hits in found.items():
            rec = self.files[row_id]
            folder, name = _split(rec.path)
            ordered = sorted((h for h in hits if h[1] is not None),
                             key=lambda h: (h[2] is not None and h[2] < 0, h[0]))   # text before the name
            evidence = tuple(Evidence(row_id, page, span[0], span[1], self._line_box(handle, span[0]), handle)
                             for page, span, handle in ordered)
            if not evidence:
                evidence = (Evidence(row_id, hits[0][0], 0, 0, None, hits[0][2]),)
            rows.append(FakeSearchRow(row_id, rec.path, name, folder, rec.size, rec.modified, rec.kind.value,
                                      rec.state.value, len(hits), self._rank(row_id, query, len(hits)), evidence,
                                      rec.missing_since))
        if not (filters is not None and getattr(filters, "every_copy", False)):
            rows = self._one_of_each(rows)
        if len(keys) == 1:
            return self._page(rows, keys[0][0], keys[0][1], limit, offset)
        return self._page_by(rows, keys, limit, offset)

    def _one_of_each(self, rows):
        """Of the matching files with the same content, the one to show, with the other places listed."""
        every = getattr(self, "contents", {})

        def standing(row):
            low = row.path.lower()
            named = any(word in low for word in ("backup", "- copy", "copy of ", "(copy)"))
            return (row.missing_since is not None, named, row.path.count("/") + row.path.count("\\"), len(row.path), row.file_id)

        best = {}
        for row in rows:
            key = every.get(row.file_id) or f"#{row.file_id}"
            if key not in best or standing(row) < standing(best[key]):
                best[key] = row
        kept = sorted(best.values(), key=lambda r: r.file_id)
        others = self.copies_of([r.file_id for r in kept])
        for row in kept:
            row.also_at = tuple(others.get(row.file_id, ()))
        return kept

    def _rank(self, row_id: int, query: str, hits: int) -> float:
        """Smaller is better: closeness first (a whole step apart), then the number of matching pieces."""
        return self._tier(row_id, query) * 1_000_000.0 - float(hits)

    def _keys(self, sort, descending):
        return sort_keys(sort, descending, SORT_COLUMNS)

    def _page_by(self, rows, keys, limit, offset) -> SearchPage:
        """Several keys: sort by the last first, each sort stable, with the row id as the final tie-break."""
        rows.sort(key=lambda r: r.file_id)
        for key, down in reversed(keys):
            rows.sort(key=lambda r: self._sort_key(key, r), reverse=down)   # stable, in either direction
        return SearchPage(rows[offset:offset + limit], len(rows), offset, limit)

    def _page(self, rows, sort, descending, limit, offset) -> SearchPage:
        rows.sort(key=lambda r: r.file_id)
        rows.sort(key=lambda r: self._sort_key(sort, r), reverse=descending)
        if descending:   # ties keep oldest row first even when the key is reversed
            rows = self._stable_ties(rows, sort)
        return SearchPage(rows[offset:offset + limit], len(rows), offset, limit)

    def _stable_ties(self, rows, sort):
        out, i = [], 0
        while i < len(rows):
            j = i
            while j < len(rows) and self._sort_key(sort, rows[j]) == self._sort_key(sort, rows[i]):
                j += 1
            out.extend(sorted(rows[i:j], key=lambda r: r.file_id))
            i = j
        return out

    def reclaimable_fraction(self) -> float:
        return 0.0

    def compact(self) -> None:
        pass

    # -- purge
    def _under(self, path: str, base: str, fold) -> bool:
        path, base = fold(path), fold(base)
        return path == base or path.startswith(base + "/") or path.startswith(base + "\\")

    def purge_folder(self, prefix: str, case_insensitive: Optional[bool] = None, thorough: bool = False) -> int:
        doomed = self._below(prefix, case_insensitive)
        for row_id in doomed:
            self._drop(row_id)
        return len(doomed)


# ---- deliberately broken variants ------------------------------------------------------------------------

class SortsAfterTruncating(FakeStore):
    def _page(self, rows, sort, descending, limit, offset):
        rows.sort(key=lambda r: r.file_id)
        cut = rows[offset:offset + limit]
        cut.sort(key=lambda r: self._sort_key(sort, r), reverse=descending)
        return SearchPage(cut, len(rows), offset, limit)


class PurgesWithoutBoundary(FakeStore):
    def _under(self, path, base, fold):
        return fold(path).startswith(fold(base))


class DishonestTotal(FakeStore):
    def _page(self, rows, sort, descending, limit, offset):
        page = super()._page(rows, sort, descending, limit, offset)
        return SearchPage(page.rows, len(page.rows), offset, limit)


class NamesNotSearchable(FakeStore):
    def _rows_of(self, row_id):
        for name, body, page in super()._rows_of(row_id):
            yield ("", body, page) if body else ("", "", page)


def _plain_words(text: str, fold) -> set:
    return {fold(w) for w in re.findall(r"\w+", text)}


class CaseSensitive(FakeStore):
    """Matches whole words exactly as written (upper and lower case differ)."""

    @staticmethod
    def _terms(query):
        return [[w] for w in re.findall(r"\w+", query)]

    def _row_matches(self, name, body, terms):
        words = _plain_words(name, str) | _plain_words(body, str)
        return all(t[0] in words for t in terms)


class AccentSensitive(FakeStore):
    """Ignores case but treats an accented letter as a different letter."""

    @staticmethod
    def _terms(query):
        return [[w.casefold()] for w in re.findall(r"\w+", query)]

    def _row_matches(self, name, body, terms):
        words = _plain_words(name, str.casefold) | _plain_words(body, str.casefold)
        return all(t[0] in words for t in terms)


class AnyWordMatches(FakeStore):
    def _row_matches(self, name, body, terms):
        name_t, body_t = tokens(name), tokens(body)
        return any(_contains(name_t, t) or _contains(body_t, t) for t in terms)


class OneRowPerSegment(FakeStore):
    def search_words(self, query, limit=100, offset=0, sort="rank", descending=False):
        page = super().search_words(query, 10 ** 9, 0, sort, descending)
        rows = []
        for r in page.rows:
            rows.extend([r] * r.matches)
        return SearchPage(rows[offset:offset + limit], len(rows), offset, limit)


class NoRollback(FakeStore):
    @contextlib.contextmanager
    def transaction(self):
        yield


class InnerFailureUndoesOuterWork(FakeStore):
    """Nested transactions share the outer snapshot: an inner failure rolls back everything."""

    depth = 0
    outer_saved = None

    @contextlib.contextmanager
    def transaction(self):
        if self.depth == 0:
            self.outer_saved = copy.deepcopy(self._state())
        self.depth += 1
        try:
            yield
        except BaseException:
            self.depth -= 1
            self._restore(self.outer_saved)
            raise
        else:
            self.depth -= 1


class UpsertDuplicates(FakeStore):
    def _find(self, path):
        return None


class SilentUnknownIds(FakeStore):
    def replace_segments(self, row_id, segments):
        if row_id in self.files:
            super().replace_segments(row_id, segments)

    def set_state(self, row_id, state, error=""):
        if row_id in self.files:
            super().set_state(row_id, state, error)


class AcceptsAnySortKey(FakeStore):
    def search_words(self, query, limit=100, offset=0, sort="rank", descending=False, filters=None):
        if not isinstance(sort, str):
            sort = [(k, d) for k, d in sort if k in SORT_COLUMNS] or "rank"
        return super().search_words(query, limit, offset, sort if not isinstance(sort, str) or sort in SORT_COLUMNS else "rank",
                                    descending, filters)


class FingerprintHidesMissingKeys(FakeStore):
    def changed_fingerprint_keys(self, current):
        stored = self.get_fingerprint() or {}
        return sorted(k for k in set(stored) | set(current) if stored.get(k) != current.get(k))


class ReplaceAppends(FakeStore):
    def replace_segments(self, row_id, segments):
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        self.segments[row_id] = self.segments[row_id] + list(segments)


class AllowsNameSegments(FakeStore):
    def replace_segments(self, row_id, segments):
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        self.segments[row_id] = list(segments)


class ReplaceWritesBeforeValidating(FakeStore):
    """Clears the old text, then notices a name segment: the refused write loses the old text."""

    def replace_segments(self, row_id, segments):
        if row_id in self.files:
            self.segments[row_id] = []
        super().replace_segments(row_id, segments)


class PurgeIgnoresCaseFlag(FakeStore):
    def purge_folder(self, prefix, case_insensitive=None, thorough=False):
        return super().purge_folder(prefix, True, thorough)


class MoveLosesText(FakeStore):
    def _moved(self, row_id, new_path):
        super()._moved(row_id, new_path)
        self.segments[row_id] = []
        self.segment_ids[row_id] = []


class MoveKeepsOldName(FakeStore):
    """Changes the path but goes on answering searches with the old name."""

    def _moved(self, row_id, new_path):
        self.stale = getattr(self, "stale", {})
        self.stale[row_id] = _split(self.files[row_id].path)[1]
        super()._moved(row_id, new_path)

    def _rows_of(self, row_id):
        name = getattr(self, "stale", {}).get(row_id)
        for real_name, body, page in super()._rows_of(row_id):
            yield (name or real_name), body, page


class RemoveLeavesText(FakeStore):
    """Forgets the file record but goes on finding its words."""

    def remove_files(self, row_ids):
        self.ghosts = getattr(self, "ghosts", {})
        present = [i for i in row_ids if i in self.files]
        for row_id in present:
            self.ghosts[row_id] = (self.files[row_id], self.segments[row_id])
        return len(present)


class MoveOverwrites(FakeStore):
    """Moves onto a path that is already taken, leaving two rows with one path."""

    def move_file(self, old_path, new_path):
        row_id = self._find(old_path)
        if row_id is None:
            raise StoreError("cannot move a file the index does not know")
        self._moved(row_id, new_path)
        return row_id


class MissingCountedUnderOldState(FakeStore):
    def _count_key(self, rec):
        return rec.state.value


class MissingLosesText(FakeStore):
    def set_missing(self, row_id, since):
        super().set_missing(row_id, since)
        if since is not None:
            self.segments[row_id] = []
            self.segment_ids[row_id] = []


class PurgeMissingIgnoresAge(FakeStore):
    def purge_missing(self, prefix, before, case_insensitive=None):
        return super().purge_missing(prefix, float("inf"), case_insensitive)


class SetRootForgetsStatus(FakeStore):
    def _merged(self, old, root):
        return Root(root.path, root.policy, tuple(root.include), tuple(root.exclude))


class PagesAppend(FakeStore):
    def set_pages(self, row_id, pages):
        if row_id not in self.files:
            raise StoreError(f"no file with id {row_id}")
        self.pages[row_id] = self.pages.get(row_id, []) + list(pages)


class FileEvidenceStopsAtThree(FakeStore):
    def file_evidence(self, file_id, query, filters=None, limit=2000):
        return super().file_evidence(file_id, query, filters, min(limit, 3))


class FileEvidenceOutOfOrder(FakeStore):
    def _in_reading_order(self, hits):
        return list(reversed(super()._in_reading_order(hits)))


class ExactMatchesRelatives(FakeStore):
    """Treats words as the same when their first six letters agree, even for an exact-form search."""

    def _row_matches(self, name, body, terms):
        def close(haystack, needle):
            return any(h[:6] == n[:6] for n in needle for h in haystack)
        name_t, body_t = tokens(name), tokens(body)
        return all(close(name_t, t) or close(body_t, t) for t in terms)


class IgnoresCloseness(FakeStore):
    def _tier(self, row_id, query):
        return 0


class PhraseIsJustItsWords(FakeStore):
    @staticmethod
    def _terms(query):
        return [t for t in (tokens(word) for word in query.replace('"', " ").split()) if t]


class IgnoresSecondSortKey(FakeStore):
    def _keys(self, sort, descending):
        return super()._keys(sort, descending)[:1]


class FiltersAfterTruncating(FakeStore):
    def search_words(self, query, limit=100, offset=0, sort="rank", descending=False, filters=None):
        page = super().search_words(query, limit, offset, sort, descending, None)
        rows = [r for r in page.rows if self._file_passes(self.files[r.file_id], filters)]
        return SearchPage(rows, page.total, offset, limit)


class WhereIsIgnored(FakeStore):
    def _where_passes(self, where, index, kind, name, body, terms):
        return super()._where_passes("anywhere", index, kind, name, body, terms)


class SizeLimitsAreExclusive(FakeStore):
    def _file_passes(self, rec, filters):
        if filters is not None and (rec.size == filters.min_size or rec.size == filters.max_size):
            return False
        return super()._file_passes(rec, filters)


class OcrTextNotSearchable(FakeStore):
    def save_ocr(self, row_id, page, result):
        self._mark_read(row_id, page, self._page_index(row_id, page))


class OcrSaveAppends(FakeStore):
    def _drop_ocr_text(self, row_id, page):
        pass


class OcrFailureStaysQueued(FakeStore):
    def fail_ocr(self, row_id, page, reason):
        self._page_index(row_id, page)


class OcrWithoutBoxes(FakeStore):
    def _line_box(self, handle, start):
        return None


class RereadForgetsOcr(FakeStore):
    def _kept_pages(self, row_id, pages):
        return set()


class RereadKeepsOcrOfPagesThatNoLongerNeedIt(FakeStore):
    def _kept_pages(self, row_id, pages):
        return {p.page for p in pages if self.ocr.get((row_id, p.page), {}).get("state") == "done"}

    def _mark_read(self, row_id, page, index, engine=""):
        self._ocr_entry(row_id, page).update(state="done", reason="", engine=engine)


class DeferredPagesLeaveTheQueue(FakeStore):
    def defer_ocr(self, row_id, page, reason):
        self._ocr_entry(row_id, page).update(state="failed", reason=reason)


class RequeueIgnoresTheEngine(FakeStore):
    def requeue_ocr_not_by(self, engine, prefix=None, case_insensitive=None):
        return super().requeue_ocr_not_by("no engine has this name", prefix, case_insensitive)


class OcrQueueIncludesMissing(FakeStore):
    def ocr_queue(self, prefix=None, case_insensitive=None):
        hidden = {i: r.missing_since for i, r in self.files.items()}
        for rec in self.files.values():
            rec.missing_since = None
        try:
            return super().ocr_queue(prefix, case_insensitive)
        finally:
            for i, since in hidden.items():
                self.files[i].missing_since = since


class RereadKeepsOcrState(FakeStore):
    def _forget_ocr(self, row_id):
        pass


class EvidenceAtTheStart(FakeStore):
    """Carries Evidence, but always points at the first characters instead of the matched words."""

    @staticmethod
    def _span(text, terms):
        return 0, min(3, len(text))


class PassageSurvivesReplace(FakeStore):
    """Hands out the same segment handles again after a file is re-read, so old Evidence shows new text."""

    def replace_segments(self, row_id, segments):
        super().replace_segments(row_id, segments)
        if row_id in self.files:
            self.segment_ids[row_id] = [row_id * 1000 + i for i in range(len(segments))]


class EvidenceOnEveryMatchingRow(FakeStore):
    """Points at every piece of text of a file whose name matched, although the words are not in that text."""

    def _hits(self, query, filters=None):
        found = super()._hits(query, filters)
        return {row: [(page, span or (0, 0), handle) for page, span, handle in hits] for row, hits in found.items()}


class NoEvidence(FakeStore):
    def search_words(self, query, limit=100, offset=0, sort="rank", descending=False):
        page = super().search_words(query, limit, offset, sort, descending)
        return SearchPage([SearchRow(r.file_id, r.path, r.name, r.folder, r.size, r.modified, r.kind, r.state, r.matches, r.rank)
                           for r in page.rows], page.total, page.offset, page.limit)
