"""Settings: every number the program would otherwise hardcode, with its default and where it came from.

Read from `config.toml` in the data folder when that file exists; a missing file means the defaults.
Unknown keys and wrong types are reported, never silently ignored. Nothing here is a rule: each default
is a starting point (see DEVELOPMENT_GUIDE.md) and is meant to be changed when it stops fitting.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

from .kinds import DEFAULT_EXTENSIONS
from .shapes import FileKind

try:   # Python 3.11 and later
    import tomllib as _toml
except ModuleNotFoundError:   # pragma: no cover - Python 3.10
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Settings:
    # -- text
    chunk_chars: int = 1800            # DigUp's chunk size [source]
    chunk_overlap: int = 200           # DigUp's overlap [source]
    max_text_bytes: int = 20_000_000   # a plain-text file is read up to here; the rest is reported unread
    language: str = "en"               # guide 5d item 3
    # -- PDF
    max_pdf_pages: int = 5000          # DigUp's cap per file [source]
    full_page_image_fraction: float = 0.7   # an image covering this share of a page marks a scan [unverified]
    garbled_fraction: float = 0.2      # share of unusable characters that marks a garbled layer [unverified]
    # -- Office (native readers)
    max_member_bytes: int = 64_000_000  # one part inside a docx/xlsx/pptx/odt is read up to here
    # -- indexing
    batch_files: int = 200             # files fetched per step of the work queue
    missing_retention_days: float = 30.0   # guide 5d item 10
    # -- file kinds: extension (no dot, lower case) -> kind name
    extensions: Mapping[str, FileKind] = field(default_factory=lambda: dict(DEFAULT_EXTENSIONS))

    def __post_init__(self) -> None:
        problems = validate(self)
        if problems:
            raise ValueError("; ".join(problems))

    def fingerprint(self) -> Dict[str, object]:
        """The settings that change what ends up in the index. A change here means stored text is stale."""
        return {"chunk_chars": self.chunk_chars, "chunk_overlap": self.chunk_overlap, "language": self.language,
                "max_text_bytes": self.max_text_bytes, "max_pdf_pages": self.max_pdf_pages,
                "full_page_image_fraction": self.full_page_image_fraction, "garbled_fraction": self.garbled_fraction,
                "max_member_bytes": self.max_member_bytes}


def validate(s: Settings) -> List[str]:
    problems = []
    if s.chunk_chars < 50:
        problems.append("chunk_chars must be at least 50")
    if not 0 <= s.chunk_overlap < s.chunk_chars:
        problems.append("chunk_overlap must be 0 or more and smaller than chunk_chars")
    for name in ("max_text_bytes", "max_pdf_pages", "max_member_bytes", "batch_files"):
        if getattr(s, name) < 1:
            problems.append(f"{name} must be at least 1")
    for name in ("full_page_image_fraction", "garbled_fraction"):
        if not 0.0 < getattr(s, name) <= 1.0:
            problems.append(f"{name} must be above 0 and at most 1")
    if s.missing_retention_days < 0:
        problems.append("missing_retention_days must not be negative")
    return problems


_SIMPLE = {f.name: f.type for f in fields(Settings) if f.name != "extensions"}
_TYPES = {"int": int, "float": (int, float), "str": str}


def from_mapping(data: Mapping[str, object]) -> Tuple[Settings, List[str]]:
    """Settings from a parsed config file, plus a note for every key that was not understood."""
    notes: List[str] = []
    changes: Dict[str, object] = {}
    for key, value in data.items():
        if key == "extensions":
            table = dict(DEFAULT_EXTENSIONS)
            if not isinstance(value, Mapping):
                notes.append("extensions must be a table of extension = kind")
                continue
            for ext, kind in value.items():
                try:
                    table[str(ext).lower().lstrip(".")] = FileKind(kind)
                except ValueError:
                    notes.append(f"extensions.{ext}: {kind!r} is not a kind ({', '.join(k.value for k in FileKind)})")
            changes["extensions"] = table
        elif key in _SIMPLE:
            wanted = _TYPES[str(_SIMPLE[key])]
            if isinstance(value, bool) or not isinstance(value, wanted):
                notes.append(f"{key}: expected {_SIMPLE[key]}, got {type(value).__name__}; the default is used")
            else:
                changes[key] = value
        else:
            notes.append(f"{key}: not a setting this version knows; ignored")
    try:
        return replace(Settings(), **changes), notes
    except ValueError as exc:
        return Settings(), notes + [f"{exc}; all defaults are used"]


def load(config: Path) -> Tuple[Settings, List[str]]:
    """Read config.toml. A missing file is the defaults with no notes; a broken one is the defaults with a note."""
    try:
        raw = Path(config).read_bytes()
    except FileNotFoundError:
        return Settings(), []
    except OSError as exc:
        return Settings(), [f"could not read {config}: {exc.strerror or exc}; the defaults are used"]
    if _toml is None:
        return Settings(), ["config.toml needs Python 3.11 or the tomli package; the defaults are used"]
    try:
        data = _toml.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, _toml.TOMLDecodeError) as exc:
        return Settings(), [f"config.toml could not be read ({exc}); the defaults are used"]
    return from_mapping(data)
