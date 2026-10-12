"""Jobs a worker can be asked to do, and the plain-data forms that cross the process boundary.

Everything that travels between the main program and a worker is JSON: a file record goes in as a
mapping, an Extraction or a Failure comes back as one. Both directions are written here, side by side,
so they cannot drift apart.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .protocols import Extraction, PageInfo, PageVerdict
from .shapes import Failure, FailureReason, FileKind, FileRecord, SegmentIn, SegmentKind

EXTRACT = "dugup.jobs:extract"


def record_to_data(record: FileRecord) -> Dict[str, Any]:
    # the path is sent with escapes for bytes that are not valid text (Linux), and restored exactly
    return {"path": record.path.encode("utf-8", "surrogateescape").decode("latin-1"),
            "size": record.size, "modified": record.modified, "kind": record.kind.value}


def record_from_data(data: Mapping[str, Any]) -> FileRecord:
    path = str(data["path"]).encode("latin-1").decode("utf-8", "surrogateescape")
    return FileRecord(path=path, size=int(data["size"]), modified=float(data["modified"]), kind=FileKind(data["kind"]))


def result_to_data(result: "Extraction | Failure") -> Dict[str, Any]:
    if isinstance(result, Failure):
        return {"failure": result.reason.value, "detail": result.detail}
    return {
        "segments": [[s.page, s.order, s.kind.value, s.text] for s in result.segments],
        "pages": [[p.page, p.verdict.value, p.needs_ocr, p.unread_reason] for p in result.pages],
        "unread": list(result.unread),
    }


def result_from_data(data: Any) -> "Extraction | Failure":
    """What a worker sent back, checked. Anything that is not a well-formed answer is a stated failure."""
    try:
        if "failure" in data:
            return Failure(FailureReason(data["failure"]), str(data["detail"]))
        segments = tuple(SegmentIn(int(p), int(o), SegmentKind(k), str(t)) for p, o, k, t in data["segments"])
        pages = tuple(PageInfo(int(n), PageVerdict(v), bool(ocr), str(reason)) for n, v, ocr, reason in data["pages"])
        return Extraction(segments, pages, tuple(str(u) for u in data.get("unread", ())))
    except (KeyError, TypeError, ValueError) as exc:
        return Failure(FailureReason.CRASHED, f"the worker's answer could not be understood ({type(exc).__name__}: {exc})")


_extractors: Dict[str, Any] = {}


def extract(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one file with the ordinary extractors. Runs inside a worker."""
    from .extractors import default_extractors
    from .settings import from_mapping

    settings_data = payload.get("settings") or {}
    key = repr(sorted(settings_data.items()))
    if key not in _extractors:          # the readers are built once per worker and reused
        _extractors.clear()
        _extractors[key] = default_extractors(from_mapping(settings_data)[0])
    return result_to_data(_extractors[key].extract(record_from_data(payload["record"])))
