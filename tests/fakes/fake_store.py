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
from dugup.shapes import (Box, Evidence, FileKind, FileRecord, FileState, Passage, Root, SegmentIn, SegmentKind)
from dugup.store import SORT_COLUMNS, SearchPage, SearchRow, StoreError

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
            return Root(root.path, root.policy, tuple(root.include), tuple(root.exclude))
        return Root(root.path, root.policy, tuple(root.include), tuple(root.exclude), old.reachable, old.last_scan,
                    old.warnings)

    def note_scan(self, path: str, reachable: bool, when: float, warnings=()) -> None:
        if path not in self.roots:
            raise StoreError("that folder is not one of the roots")
        old = self.roots[path]
        self.roots[path] = Root(old.path, old.policy, old.include, old.exclude, bool(reachable), when, tuple(warnings))

    def remove_root(self, path: str) -> bool:
        return self.roots.pop(path, None) is not None

    def get_file(self, path: str) -> Optional[FileRecord]:
        row_id = self._find(path)
        return None if row_id is None else copy.copy(self.files[row_id])

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
        return [t for t in (tokens(word) for word in query.split()) if t]

    def _row_matches(self, name: str, body: str, terms: List[List[str]]) -> bool:
        name_t, body_t = tokens(name), tokens(body)
        return all(_contains(name_t, t) or _contains(body_t, t) for t in terms)

    def _hits(self, query: str):
        terms = self._terms(query)
        if not terms:
            return {}
        found: Dict[int, List[tuple]] = {}
        for row_id in self.files:
            handles = self._segment_handles(row_id)
            for index, (name, body, page) in enumerate(self._rows_of(row_id)):
                if self._row_matches(name, body, terms):
                    handle = handles[index] if index < len(handles) else None
                    in_body = any(_contains(tokens(body), t) for t in terms)
                    # a piece of text that matched only through the file name counts as a match,
                    # but it is the name row that points at the words
                    span = self._span(body, terms) if in_body else (self._span(name, terms) if index == 0 else None)
                    found.setdefault(row_id, []).append((page, span, handle))
        return found

    def _sort_key(self, sort: str, row: SearchRow):
        return {
            "name": _ascii_lower(row.name), "folder": _ascii_lower(row.folder), "size": row.size,
            "modified": row.modified, "kind": row.kind, "matches": row.matches, "rank": row.rank,
        }[sort]

    def search_words(self, query: str, limit: int = 100, offset: int = 0, sort: str = "rank",
                     descending: bool = False) -> SearchPage:
        if sort not in SORT_COLUMNS:
            raise ValueError(f"cannot sort by {sort!r}; choose one of {sorted(SORT_COLUMNS)}")
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        found = self._hits(query)
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
                                      rec.state.value, len(hits), -float(len(hits)), evidence, rec.missing_since))
        return self._page(rows, sort, descending, limit, offset)

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
    def search_words(self, query, limit=100, offset=0, sort="rank", descending=False):
        return super().search_words(query, limit, offset, sort if sort in SORT_COLUMNS else "rank", descending)


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

    def _hits(self, query):
        found = super()._hits(query)
        return {row: [(page, span or (0, 0), handle) for page, span, handle in hits] for row, hits in found.items()}


class NoEvidence(FakeStore):
    def search_words(self, query, limit=100, offset=0, sort="rank", descending=False):
        page = super().search_words(query, limit, offset, sort, descending)
        return SearchPage([SearchRow(r.file_id, r.path, r.name, r.folder, r.size, r.modified, r.kind, r.state, r.matches, r.rank)
                           for r in page.rows], page.total, page.offset, page.limit)
