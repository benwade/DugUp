"""What an assistant (Claude, or another on this machine) may ask the index: read-only search.

Served by `dugup mcp serve` (mcpserver.py), and kept apart from the MCP library so it can be tested on its own.

The rules, in one place:

- Off unless the person turns it on (`mcp_enabled`). The setting is read again on every request, so turning it
  off takes effect at once, even while an assistant is connected.
- Read-only. Nothing here changes a file or the index: no trash, no rename, no clean-up.
- Only the folders the person allows (`mcp_folders`, separated by ";"; empty means every indexed folder). A
  result, a passage, or an "also at" path outside them is never returned, and asking for such a folder is
  refused.
- Passages, not whole files. A result carries the passage that matched; more passages of one file can be
  asked for, a few at a time, each a few hundred characters.
- What is returned goes to the assistant's service, as a file attached to a conversation would. The README and
  `dugup mcp enable` say so.

Every call into the index runs on one thread of its own (the index connection stays with the thread that opened
it, and one connection keeps one copy of the search's working data in memory). Models loaded for an assistant's
search by meaning or by looks are let go after `mcp_idle_minutes` without one.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

from .datadir import is_within
from .shapes import (WHERE_ANYWHERE, WHERE_BOTH, WHERE_LOOKS, WHERE_MEANING, Failure, FileKind, SearchFilters,
                     SearchRow)

HOW = {"words": WHERE_ANYWHERE, "meaning": WHERE_MEANING, "both": WHERE_BOTH, "looks": WHERE_LOOKS}
MOST_RESULTS = 50
MOST_PASSAGES = 10
PASSAGE_CONTEXT = 200          # characters each side of the match
PASSAGE_MOST = 700             # never more than this many characters in one passage
CALL_SECONDS = 300.0           # a search waiting longer than this is reported as such (a model may be loading)

OFF = ("DugUp's assistant tools are switched off. The person can turn them on with: dugup mcp enable "
       "(what the tools return, such as file names and passages, is then sent to the assistant).")


class Refused(Exception):
    """A request that cannot be answered, with the reason in plain words."""


def folders_allowed(text: str) -> Optional[List[str]]:
    """The `mcp_folders` setting as a list, or None for every indexed folder."""
    found = [part.strip().strip('"') for part in (text or "").split(";")]
    found = [os.path.abspath(part) for part in found if part]
    return found or None


def _day(text: Optional[str], name: str) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text.strip()[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        raise Refused(f"{name} must be a date written YYYY-MM-DD, such as 2026-03-15") from None


def _when(stamp: Optional[float]) -> Optional[str]:
    if stamp is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(stamp).isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return None


class Tools:
    """The assistant's tools over one LocalService. `load_settings()` gives the current Settings (read from
    config.toml on each call, so a change takes effect at once); `clock` is for tests."""

    def __init__(self, service, load_settings: Callable[[], object], clock: Callable[[], float] = time.monotonic,
                 start_idle_watch: bool = True) -> None:
        self.service = service
        self.load_settings = load_settings
        self.clock = clock
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dugup-assistant")
        self._last_model_use: Optional[float] = None
        self._closing = threading.Event()
        self._idle = threading.Thread(target=self._watch_idle, name="dugup-assistant-idle", daemon=True)
        if start_idle_watch:
            self._idle.start()

    # ---- the rules ----------------------------------------------------------------------------------------

    def _settings(self):
        settings = self.load_settings()
        if not getattr(settings, "mcp_enabled", False):
            raise Refused(OFF)
        return settings

    def _allowed(self, settings) -> Optional[List[str]]:
        return folders_allowed(getattr(settings, "mcp_folders", ""))

    @staticmethod
    def _visible(path: str, allowed: Optional[Sequence[str]]) -> bool:
        return allowed is None or any(is_within(path, folder) for folder in allowed)

    def _run(self, work: Callable[[], object]):
        """Run `work` on the assistant's own thread and wait for it."""
        future = self._worker.submit(work)
        try:
            return future.result(timeout=CALL_SECONDS)
        except concurrent.futures.TimeoutError:
            raise Refused("the search is taking too long (a model may still be loading); try again in a minute") from None

    # ---- the tools ----------------------------------------------------------------------------------------

    def search(self, query: str, how: str = "words", folder: Optional[str] = None, kinds: Sequence[str] = (),
               modified_after: Optional[str] = None, modified_before: Optional[str] = None,
               limit: int = 10) -> Dict[str, object]:
        settings = self._settings()
        query = (query or "").strip()
        if not query:
            raise Refused("say what to look for")
        if how not in HOW:
            raise Refused(f"how must be one of: {', '.join(HOW)}")
        known = {k.value for k in FileKind}
        kinds = tuple(k.strip().lower() for k in (kinds or ()) if k and k.strip())
        for kind in kinds:
            if kind not in known:
                raise Refused(f"{kind!r} is not a kind of file DugUp knows ({', '.join(sorted(known))})")
        limit = max(1, min(int(limit or 10), MOST_RESULTS))
        allowed = self._allowed(settings)
        if folder:
            folder = os.path.abspath(folder.strip().strip('"'))
            if not self._visible(folder, allowed):
                raise Refused("that folder is not one the person has allowed assistants to search")
            places: List[Optional[str]] = [folder]
        else:
            places = list(allowed) if allowed is not None else [None]
        base = SearchFilters(kinds=kinds, where=HOW[how], modified_after=_day(modified_after, "modified_after"),
                             modified_before=_day(modified_before, "modified_before"))

        def work():
            rows: Dict[int, SearchRow] = {}
            total = 0
            for place in places:
                page = self.service.search(query, limit, 0, "rank", False, dataclasses.replace(base, folder=place))
                if isinstance(page, Failure):
                    return page
                total += page.total
                for row in page.rows:
                    rows.setdefault(row.file_id, row)
            if how in ("meaning", "looks", "both"):
                self._last_model_use = self.clock()
            words_order = how == "words"          # a word search's rank is "smaller is better"; the others closeness
            best = sorted(rows.values(), key=lambda r: (r.rank if words_order else -r.rank))[:limit]
            found = [self._row(r, allowed, with_passage=how != "looks", with_score=how != "words") for r in best if self._visible(r.path, allowed)]
            coverage = None
            if how in ("meaning", "both"):
                done, could = self.service.meaning_coverage()
                coverage = {"sketched_files": done, "files_with_text": could}
            elif how == "looks":
                done, could, _ = self.service.looks_coverage()
                coverage = {"sketched_pictures": done, "pictures": could}
            return found, total, coverage

        answer = self._run(work)
        if isinstance(answer, Failure):
            raise Refused(answer.detail)
        found, total, coverage = answer
        result: Dict[str, object] = {"query": query, "how": how, "results": found, "total_matches": total,
                                     "shown": len(found)}
        if coverage is not None:
            result["coverage"] = coverage
            done, could = list(coverage.values())
            if done < could:
                result["note"] = "Only what has been sketched so far is searched this way; the rest is still to come."
        if allowed is not None:
            result["searched_folders"] = [folder] if folder else list(allowed)
        return result

    def passages(self, path: str, words: str, limit: int = 5) -> Dict[str, object]:
        """More places in one file where the words are found, a passage each (never the whole file)."""
        settings = self._settings()
        path = os.path.abspath((path or "").strip().strip('"'))
        if not self._visible(path, self._allowed(settings)):
            raise Refused("that file is not in a folder the person has allowed assistants to search")
        words = (words or "").strip()
        if not words:
            raise Refused("say which words to find in the file")
        limit = max(1, min(int(limit or 5), MOST_PASSAGES))

        def work():
            record = self.service.store.get_file(path)
            if record is None or record.row_id is None:
                return None
            found = []
            for evidence in list(self.service.more_evidence(record.row_id, words))[:limit]:
                passage = self.service.get_evidence(evidence, PASSAGE_CONTEXT)
                if not isinstance(passage, Failure):
                    found.append({"page": passage.page or None, "text": passage.text[:PASSAGE_MOST]})
            return found

        found = self._run(work)
        if found is None:
            raise Refused("that file is not in the index")
        return {"path": path, "words": words, "passages": found}

    def status(self) -> Dict[str, object]:
        settings = self._settings()
        allowed = self._allowed(settings)

        def work():
            state = self.service.indexing_status()
            shown = []
            for root in state["roots"]:
                if self._visible(root["path"], allowed):
                    shown.append({"path": root["path"], "reachable": root["reachable"],
                                  "files": sum(root["counts"].values())})
                elif allowed is not None:       # an allowed folder inside an indexed one: shown, without the count
                    shown += [{"path": f, "reachable": root["reachable"]} for f in allowed if is_within(f, root["path"])]
            meaning_done, meaning_could = self.service.meaning_coverage()
            looks_done, looks_could, _ = self.service.looks_coverage()
            return {
                "folders": shown,
                "limited_to": allowed,
                "updating_now": bool(state["running"]),
                "search_by_meaning": {"sketched_files": meaning_done, "files_with_text": meaning_could,
                                      "ready": self.service.meaning_ready() is True},
                "pictures_by_looks": {"sketched_pictures": looks_done, "pictures": looks_could,
                                      "ready": self.service.looks_ready() is True},
            }

        return self._run(work)

    # ---- one result, as the assistant sees it ---------------------------------------------------------------

    def _row(self, row: SearchRow, allowed, with_passage: bool, with_score: bool = False) -> Dict[str, object]:
        item: Dict[str, object] = {"path": row.path, "name": row.name, "kind": row.kind, "size_bytes": row.size,
                                   "modified": _when(row.modified)}
        if with_score:
            item["closeness"] = round(float(row.rank), 3)     # 0 to 1 for meaning and looks; word results come best first
        if row.missing_since is not None:
            item["gone_since"] = _when(row.missing_since)
        if row.unavailable:
            item["unavailable"] = True
        also = [p for p in row.also_at if self._visible(p, allowed)]
        if also:
            item["also_at"] = also
        if with_passage and row.evidence:
            passage = self.service.get_evidence(row.evidence[0], PASSAGE_CONTEXT)
            if not isinstance(passage, Failure) and passage.text.strip():
                item["passage"] = passage.text[:PASSAGE_MOST]
                if passage.page:
                    item["page"] = passage.page
        return item

    # ---- letting models go ----------------------------------------------------------------------------------

    def release_if_idle(self) -> List[str]:
        """Let the models go when no search by meaning or looks has come for `mcp_idle_minutes`."""
        if self._last_model_use is None:
            return []
        try:
            minutes = float(getattr(self.load_settings(), "mcp_idle_minutes", 5.0))
        except Exception:
            minutes = 5.0
        if minutes <= 0 or self.clock() - self._last_model_use < minutes * 60:
            return []

        def work():
            if self._last_model_use is None or self.clock() - self._last_model_use < minutes * 60:
                return []
            self._last_model_use = None
            return self.service.free_models()

        return self._worker.submit(work).result()

    def _watch_idle(self) -> None:
        while not self._closing.wait(30):
            try:
                self.release_if_idle()
            except Exception:
                pass

    def close(self) -> None:
        self._closing.set()

        def last():
            self.service.free_models()
            self.service.release_thread_store()

        try:
            self._worker.submit(last).result(timeout=30)
        except Exception:
            pass
        self._worker.shutdown(wait=False)
