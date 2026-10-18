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
    figure_fraction: float = 0.1       # a picture covering this share of a typed page gets the page read by OCR
                                       # too (labels in a figure, a pasted screenshot); 0 turns it off [unverified]
    # -- Office (native readers)
    max_member_bytes: int = 64_000_000  # one part inside a docx/xlsx/pptx/odt is read up to here
    # -- isolation: readers run in a worker process, so a bad file can hurt the worker and not the index
    isolate_readers: bool = True
    reader_seconds: float = 60.0           # time allowed for one file, before the allowance for its size
    reader_seconds_per_mb: float = 2.0     # extra time for each megabyte of the file
    reader_seconds_cap: float = 900.0      # never more than this for one file
    reader_memory_mb: int = 4096           # memory a reader may use; 0 means no limit
    content_marks: bool = True             # give each file read a mark of its exact content, so the same file in
                                           # several places is known for one file (it is read once more for that)
    main_place: str = ""                   # of several copies of one file, a search shows the one below this folder
                                           # or drive; "" works it out (on Windows, the drive your own folders are on)
    read_once: bool = True                 # a file whose content was already read in another place takes its text
                                           # (and what OCR read) from there, and is not read again
    cleanup_protect: str = ""              # folders never proposed for removal, separated by ";" (a live backup of this
                                           # computer on a cloud drive, say); the duplicates report shows them aside
    cleanup_prefer: str = ""               # folders, separated by ";", that are the proper home of their files: a copy
                                           # there is the one to keep when the duplicates report must choose
    index_readers: int = 0                 # files read side by side when indexing; 0 works it out, 1 is one at a time
    retry_patience: float = 2.0            # a file or page that ran out of time gets one more try, last of all,
                                           # with its time limit multiplied by this; 1 or less means no second try
    reader_failures_in_a_row: int = 10     # this many crashes or timeouts one after another pause the run; 0: never
    min_free_mb: int = 200                 # a run stops when the drive that holds the index has less free; 0: no check
    compact_at_fraction: float = 0.25      # after a run, the index is compacted by itself when at least this share of
                                           # it is unused space; 0 means never by itself (dugup space --compact)
    compact_min_mb: int = 10               # ... and when that unused space is at least this much
    max_index_mb: int = 0                  # a spending limit: a run stops when the index is larger than this; 0: none
    # -- OCR: pictures and scanned pages are read in a worker of their own
    ocr_seconds: float = 120.0             # time allowed for one page or picture
    ocr_memory_mb: int = 0                 # memory the OCR worker may use; 0 means no limit [the engine reserves a lot]
    ocr_dpi: int = 150                     # a PDF page is drawn at this resolution to be read
    ocr_max_pixels: int = 4000             # a larger picture is scaled down to this longest side before reading
    ocr_tiny_pixels: int = 200             # a picture whose longest side is under this is queued last (icons, buttons)
    ocr_failures_in_a_row: int = 5         # this many pages failing one after another pauses the run
    # -- plain-words search (optional): a small language model fills in the search form from a sentence.
    #    Either name a server that is already running (LM Studio, for example) ...
    intent_reader: str = "rules"       # who reads the sentence: "rules" (built in, no download) or "model"
    intent_url: str = ""               # for example "http://127.0.0.1:1234"; empty: not used
    intent_model: str = ""             # the model's name on that server, when the server needs one
    intent_allow_remote: bool = False  # a server on another machine would be sent what you type; off by default
    #    ... or let the program start a model server itself (llama.cpp's llama-server) and end it on closing.
    intent_server: str = ""            # the server program, for example "C:\\Tools\\llama\\llama-server.exe"
    intent_model_file: str = ""        # the model file (GGUF) it should load
    intent_gpu_layers: int = 0         # 0 keeps the model on the processor; a small model does not need the GPU
    intent_seconds: float = 30.0       # time allowed for one sentence
    intent_start_seconds: float = 120.0   # time allowed for the server to load the model
    # -- storage
    text_compression: str = "zlib"     # how stored text is kept: "zlib" (smaller index) or "none"
    # -- searching
    near_words: int = 15               # words this close together rank above the same words far apart
    # -- indexing
    batch_files: int = 200             # files fetched per step of the work queue
    missing_retention_days: float = 30.0   # guide 5d item 10
    # -- file kinds: extension (no dot, lower case) -> kind name
    extensions: Mapping[str, FileKind] = field(default_factory=lambda: dict(DEFAULT_EXTENSIONS))

    ocr_fast_pages: int = 200              # a run not told fast or gentle goes fast when at least this many pages wait; 0: never
    ocr_fast_readers: int = 0              # readers side by side in fast mode; 0 works it out from cores and free memory
    ocr_reader_threads: int = 2            # processor threads each of them may use
    ocr_reader_memory_mb: int = 1500       # memory allowed for when counting how many fit [a guess; the engine is hungry]
    ocr_gpu: bool = False                  # read on a graphics card when the OCR runtime can use one; off: most machines have none worth using
    ocr_gpu_readers: int = 0               # readers sharing the card in fast mode; 0 works it out as for the processor, at most 8
                                           # [measured on one machine: 2, 4 and 8 readers gave 250, 451 and 695 pages a minute]
    ocr_after_index: bool = True           # the window's "Update the index" goes straight on to OCR where a folder asks for it
    # -- searching by meaning (meaning.py): sketches of every piece of text, made in the background
    meaning_model: str = "larger"          # "larger" (the better one; 547 MB) or "small" (90 MB, faster, less exact)
    meaning_size: str = "full"             # "full" keeps every number of a sketch; "compact" keeps 256 (a third of the disk, a few results lost)
    meaning_gpu: bool = True               # sketch on a graphics card when the runtime can use one; the processor is used when it cannot
    meaning_after_index: bool = True       # updating the index goes on to sketch what is new, after OCR
    meaning_word_weight: float = 0.5       # "Words and meaning": the most a word match may add to a file's closeness (the best word match adds this much)
    meaning_word_pool: int = 2000          # "Words and meaning": how many of the best word matches are combined with the meaning
    meaning_word_everywhere: float = 0.3   # "Words and meaning": a word in at least this share of the index's pieces is not looked for (0: look for every word)
    meaning_warm_up: str = "auto"          # the window loads the model that sketches questions as it opens: "yes", "never", or "auto" (when the index holds sketches)
    # -- pictures by likeness (looks.py)
    looks_gpu: bool = True                 # look at pictures on a graphics card when the runtime can; the processor otherwise
    looks_readers: int = 0                 # readers side by side (opening and scaling pictures is the slow part); 0 works it out from the cores, at most 8
    looks_after_index: bool = True         # updating the index goes on to look at new pictures, after the sketches of meaning
    # -- naming pictures (naming.py)
    rename_pattern: str = "{caption} {taken}"   # what a renamed picture is called; fields: {caption} {taken} {time} {camera} {folder} {original} {n} {ext}
    caption_gpu: bool = True               # write captions on a graphics card when the runtime can
    caption_source: str = "model"          # "model": the pinned captioning model; "server": a local model server (not built yet)
    watch_every_minutes: float = 60.0      # while the tray runs, the index updates itself this often (and once at start); 0: never
    watch_on_battery: bool = False         # run those background updates on battery power too; off: they wait for mains power
    watch_readers: int = 0                 # readers a background update may use; 0: a quarter of the cores, at least one, never all
    hotkey: str = "Win+Shift+F"            # the key that brings up the search box anywhere, while the tray runs; "" for none
    # -- assistants (assistant.py, `dugup mcp`): read-only search tools for Claude and other assistants here
    mcp_enabled: bool = False              # off by default: what the tools return (names, paths, passages) goes to the assistant's service
    mcp_folders: str = ""                  # only these folders, separated by ";", are visible to assistants; "" means every indexed folder
    mcp_idle_minutes: float = 5.0          # a model loaded for an assistant's search is let go after this long without one; 0: kept
    # The folder picker's first guesses (picker.py). A person can change every one of them in the picker.
    book_folders: str = "calibre; calibre library; books; ebooks; e-books; kindle; my kindle content"   # names, any case
    book_folder_level: str = "names"       # what a folder with such a name starts at; "" makes no guess about books
    picture_heavy_fraction: float = 0.8    # at least this share of a folder's readable files are pictures ...
    picture_heavy_min: int = 20            # ... and there are at least this many: it starts without OCR

    def __post_init__(self) -> None:
        problems = validate(self)
        if problems:
            raise ValueError("; ".join(problems))

    def reader_limit(self, size_bytes: int) -> float:
        """Seconds a reader may spend on a file of this size: a base, plus an allowance for size, up to the cap."""
        megabytes = max(0, size_bytes) / (1024.0 * 1024.0)
        return min(self.reader_seconds_cap, self.reader_seconds + self.reader_seconds_per_mb * megabytes)

    def fingerprint(self) -> Dict[str, object]:
        """The settings that change what ends up in the index. A change here means stored text is stale."""
        return {"chunk_chars": self.chunk_chars, "chunk_overlap": self.chunk_overlap, "language": self.language,
                "max_text_bytes": self.max_text_bytes, "max_pdf_pages": self.max_pdf_pages,
                "full_page_image_fraction": self.full_page_image_fraction, "garbled_fraction": self.garbled_fraction,
                "figure_fraction": self.figure_fraction,
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
    if not 0.0 <= s.figure_fraction <= 1.0:
        problems.append("figure_fraction must be between 0 and 1")
    if s.retry_patience < 0 or s.reader_failures_in_a_row < 0 or s.min_free_mb < 0 or s.max_index_mb < 0:
        problems.append("retry_patience, reader_failures_in_a_row, min_free_mb and max_index_mb must not be negative")
    for name in ("reader_seconds", "reader_seconds_cap"):
        if getattr(s, name) <= 0:
            problems.append(f"{name} must be above 0")
    if s.index_readers < 0:
        problems.append("index_readers must not be negative")
    if s.reader_seconds_per_mb < 0 or s.reader_memory_mb < 0:
        problems.append("reader_seconds_per_mb and reader_memory_mb must not be negative")
    if not 0.0 <= s.compact_at_fraction <= 1.0 or s.compact_min_mb < 0:
        problems.append("compact_at_fraction must be between 0 and 1, and compact_min_mb must not be negative")
    if s.intent_seconds <= 0 or s.intent_start_seconds <= 0 or s.intent_gpu_layers < 0:
        problems.append("intent_seconds and intent_start_seconds must be above 0, and intent_gpu_layers must not be negative")
    if s.intent_reader not in ("rules", "model"):
        problems.append('intent_reader must be "rules" or "model"')
    if s.text_compression not in ("zlib", "none"):
        problems.append('text_compression must be "zlib" or "none"')
    if s.near_words < 0:
        problems.append("near_words must not be negative")
    if s.ocr_seconds <= 0:
        problems.append("ocr_seconds must be above 0")
    for name in ("ocr_dpi", "ocr_max_pixels", "ocr_failures_in_a_row"):
        if getattr(s, name) < 1:
            problems.append(f"{name} must be at least 1")
    if s.ocr_memory_mb < 0 or s.ocr_tiny_pixels < 0:
        problems.append("ocr_memory_mb and ocr_tiny_pixels must not be negative")
    if s.book_folder_level not in ("", "names", "words", "ocr", "meaning"):
        problems.append('book_folder_level must be "", "names", "words", "ocr" or "meaning"')
    if not 0.0 < s.picture_heavy_fraction <= 1.0 or s.picture_heavy_min < 0:
        problems.append("picture_heavy_fraction must be above 0 and at most 1, and picture_heavy_min must not be negative")
    if s.ocr_gpu_readers < 0:
        problems.append("ocr_gpu_readers must not be negative")
    if s.meaning_model not in ("small", "larger"):
        problems.append('meaning_model must be "small" or "larger"')
    if s.meaning_size not in ("full", "compact"):
        problems.append('meaning_size must be "full" or "compact"')
    if s.meaning_word_weight < 0:
        problems.append("meaning_word_weight must not be negative")
    if s.meaning_word_pool < 1:
        problems.append("meaning_word_pool must be at least 1")
    if not 0 <= s.meaning_word_everywhere <= 1:
        problems.append("meaning_word_everywhere must be between 0 and 1")
    if s.meaning_warm_up not in ("auto", "yes", "never"):
        problems.append('meaning_warm_up must be "auto", "yes" or "never"')
    if s.looks_readers < 0:
        problems.append("looks_readers must not be negative")
    if s.caption_source not in ("model", "server"):
        problems.append('caption_source must be "model" or "server"')
    if s.watch_every_minutes < 0:
        problems.append("watch_every_minutes must be 0 (never) or more")
    if s.watch_readers < 0:
        problems.append("watch_readers must be 0 (worked out) or more")
    if s.hotkey:
        from .hotkey import parse
        _, _, problem = parse(s.hotkey)
        if problem:
            problems.append(f"hotkey: {problem}")
    if s.ocr_fast_readers < 0 or s.ocr_reader_threads < 1 or s.ocr_reader_memory_mb < 0:
        problems.append("ocr_fast_readers and ocr_reader_memory_mb must not be negative, and ocr_reader_threads must be at least 1")
    if s.mcp_idle_minutes < 0:
        problems.append("mcp_idle_minutes must be 0 (kept) or more")
    if s.missing_retention_days < 0:
        problems.append("missing_retention_days must not be negative")
    return problems


_SIMPLE = {f.name: f.type for f in fields(Settings) if f.name != "extensions"}
_TYPES = {"int": int, "float": (int, float), "str": str, "bool": bool}


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
            if (isinstance(value, bool) and wanted is not bool) or not isinstance(value, wanted):
                notes.append(f"{key}: expected {_SIMPLE[key]}, got {type(value).__name__}; the default is used")
            else:
                changes[key] = value
        else:
            notes.append(f"{key}: not a setting this version knows; ignored")
    try:
        return replace(Settings(), **changes), notes
    except ValueError:
        pass
    # One value will not do. The others stay as written: a typo in a timeout must not empty the folders that
    # are protected from removal. Each value is tried on its own first; what fails alone is set aside with a
    # note; then, in case two values fail only together (an overlap larger than the chunk), the rest are
    # tried as a whole and whittled down one at a time until they are accepted.
    kept: Dict[str, object] = {}
    for key, value in changes.items():
        try:
            replace(Settings(), **{key: value})
            kept[key] = value
        except ValueError as exc:
            notes.append(f"{key}: {exc}; its default is used")
    while kept:
        try:
            return replace(Settings(), **kept), notes
        except ValueError as exc:
            problem = str(exc)
            culprit = next((k for k in kept if k in problem), next(reversed(kept)))
            notes.append(f"{culprit}: {problem}; its default is used")
            del kept[culprit]
    return Settings(), notes


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
