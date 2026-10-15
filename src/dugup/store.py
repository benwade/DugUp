"""The Store: one SQLite file (guide section 4, CONTRACTS.md part 6).

Built so far: schema, migrations, fingerprint, files, segments, pages, roots, word
search with Evidence, honest counts and database-side sorting, missing files and
purge, OCR text with a box per line. Vectors and fuzzy matching come in later steps. The
single-writer lock is lockfile.py (one indexing run at a time).
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import sqlite3
import unicodedata
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .protocols import OCR_DONE, OCR_FAILED, OCR_WAITING, OcrResult, OcrTask, PageInfo, PageVerdict
from .shapes import (WHERE_ANYWHERE, WHERE_BOTH, WHERE_MEANING, WHERE_NAME, WHERE_OCR, WHERE_TEXT, SearchFilters, sort_keys, Box, Evidence, FileKind, FileRecord, FileState, Passage, Root, SearchPage, SearchRow, SegmentIn,
                     SegmentKind)

__all__ = ["SqliteStore", "StoreError", "StoreVersionError", "SearchPage", "SearchRow", "SORT_COLUMNS",
           "MIGRATIONS", "TOKENIZER", "build_match", "encode_path", "decode_path", "fts5_available"]

EVERYWHERE_MIN_PIECES = 200   # below this many pieces, no word of a question is set aside as found everywhere
TOKENIZER = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
# For choosing which of several copies to show: a path that says backup, and how deep a path lies.
_BACKUP_SQL = "(f.path LIKE '%backup%' OR f.path LIKE '%- copy%' OR f.path LIKE '%copy of %' OR f.path LIKE '%(copy)%')"
_DEPTH_SQL = "(length(f.path) - length(replace(replace(f.path, '/', ''), char(92), '')))"
SORT_COLUMNS = {
    "name": "f.name COLLATE NOCASE",
    "folder": "f.folder COLLATE NOCASE",
    "size": "f.size",
    "modified": "f.modified",
    "kind": "f.kind",
    "matches": "matches",
    "rank": "rank",
}
EVIDENCE_PER_ROW = 3          # how many places a search row points at (best first)
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"   # wrapped around matched words by FTS5; never present in stored text


class StoreError(Exception):
    """The store could not do what was asked (the message says why)."""


class StoreVersionError(StoreError):
    """The file was written by a newer version of the program."""


# ---- schema and migrations -------------------------------------------------

def _migration_1(conn: sqlite3.Connection) -> None:
    statements = [
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE files (
               id INTEGER PRIMARY KEY,
               path TEXT NOT NULL UNIQUE,
               path_raw BLOB,
               name TEXT NOT NULL,
               folder TEXT NOT NULL,
               size INTEGER NOT NULL,
               modified REAL NOT NULL,
               kind TEXT NOT NULL,
               file_id TEXT,
               state TEXT NOT NULL,
               error TEXT NOT NULL DEFAULT '',
               policy TEXT NOT NULL DEFAULT ''
           )""",
        "CREATE INDEX files_folder ON files(folder)",
        # AUTOINCREMENT: a segment id is never handed out twice, so Evidence that points at a
        # segment from before a re-read can never show the new text by accident.
        """CREATE TABLE segments (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
               page INTEGER NOT NULL,
               ord INTEGER NOT NULL,
               kind TEXT NOT NULL,
               text TEXT NOT NULL,
               state TEXT NOT NULL DEFAULT 'done'
           )""",
        "CREATE INDEX segments_file ON segments(file_id)",
        """CREATE TABLE regions (
               id INTEGER PRIMARY KEY,
               segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
               engine TEXT NOT NULL DEFAULT '',
               label TEXT NOT NULL DEFAULT '',
               x0 INTEGER NOT NULL, y0 INTEGER NOT NULL, x1 INTEGER NOT NULL, y1 INTEGER NOT NULL,
               confidence REAL,
               text TEXT NOT NULL
           )""",
        """CREATE TABLE unread_bands (
               id INTEGER PRIMARY KEY,
               file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
               page INTEGER NOT NULL,
               y0 INTEGER NOT NULL, y1 INTEGER NOT NULL,
               reason TEXT NOT NULL
           )""",
        """CREATE TABLE annotations (
               id INTEGER PRIMARY KEY,
               segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
               kind TEXT NOT NULL, label TEXT NOT NULL, value TEXT NOT NULL,
               span_start INTEGER, span_end INTEGER,
               confidence REAL, verified INTEGER NOT NULL DEFAULT 0, annotator TEXT NOT NULL DEFAULT ''
           )""",
        f"CREATE VIRTUAL TABLE fts USING fts5(name, body, tokenize=\"{TOKENIZER}\")",
    ]
    for statement in statements:
        conn.execute(statement)


def _migration_2(conn: sqlite3.Connection) -> None:
    """Phase 1: per-page records, the user's roots, and a 'missing since' date kept apart from the state."""
    statements = [
        "ALTER TABLE files ADD COLUMN missing_since REAL",
        "CREATE INDEX files_state ON files(state)",
        """CREATE TABLE pages (
               file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
               page INTEGER NOT NULL,
               verdict TEXT NOT NULL,
               needs_ocr INTEGER NOT NULL,
               reason TEXT NOT NULL DEFAULT '',
               PRIMARY KEY (file_id, page)
           ) WITHOUT ROWID""",
        """CREATE TABLE roots (
               path TEXT PRIMARY KEY,
               path_raw BLOB,
               policy TEXT NOT NULL DEFAULT '',
               include TEXT NOT NULL DEFAULT '[]',
               exclude TEXT NOT NULL DEFAULT '[]',
               reachable INTEGER NOT NULL DEFAULT 1,
               last_scan REAL,
               warnings TEXT NOT NULL DEFAULT '[]'
           )""",
    ]
    for statement in statements:
        conn.execute(statement)


def _migration_3(conn: sqlite3.Connection) -> None:
    """OCR: what became of each page that needed it, and the shape of a picture (used to order the queue)."""
    statements = [
        "ALTER TABLE pages ADD COLUMN ocr TEXT NOT NULL DEFAULT ''",          # '' | done | failed
        "ALTER TABLE pages ADD COLUMN ocr_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE pages ADD COLUMN width INTEGER",
        "ALTER TABLE pages ADD COLUMN height INTEGER",
        "ALTER TABLE pages ADD COLUMN camera INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX regions_segment ON regions(segment_id)",
    ]
    for statement in statements:
        conn.execute(statement)


def _migration_4(conn: sqlite3.Connection) -> None:
    """Which OCR engine and version read each page, so a newer one can offer to read old pages again."""
    conn.execute("ALTER TABLE pages ADD COLUMN ocr_engine TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE pages SET ocr_engine = COALESCE((SELECT r.engine FROM segments s JOIN regions r ON r.segment_id = s.id "
        "WHERE s.file_id = pages.file_id AND s.page = pages.page AND s.kind = 'ocr' LIMIT 1), '') WHERE ocr = 'done'")
    # a page with no words on it left no line to copy the engine from: it was read by the engine in use so far
    usual = conn.execute("SELECT ocr_engine FROM pages WHERE ocr_engine != '' GROUP BY ocr_engine "
                         "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    if usual is not None:
        conn.execute("UPDATE pages SET ocr_engine = ? WHERE ocr = 'done' AND ocr_engine = ''", (usual[0],))


TEXT_PLAIN, TEXT_ZLIB = 0, 1      # how a segment's text is stored (the `z` column); another codec is another number


def pack_text(text: str, compress: bool = True) -> Tuple[object, int]:
    """A piece of text as it is stored: compressed when that is asked for and actually smaller."""
    if compress and text:
        raw = text.encode("utf-8")
        packed = zlib.compress(raw, 6)
        if len(packed) < len(raw):
            return packed, TEXT_ZLIB
    return text, TEXT_PLAIN


def unpack_text(value: object, z: object) -> str:
    """The text of a stored segment, whatever way it was stored."""
    if z == TEXT_ZLIB:
        return zlib.decompress(value).decode("utf-8")     # type: ignore[arg-type]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""                                     # type: ignore[return-value]


FTS_SOURCE = ("CREATE VIEW fts_src AS SELECT s.id AS id, dugup_clean(f.name) AS name, "
              "CASE WHEN s.kind = 'name' THEN '' ELSE dugup_text(s.text, s.z) END AS body "
              "FROM segments s JOIN files f ON f.id = s.file_id")


def _migration_5(conn: sqlite3.Connection) -> None:
    """A smaller index, with nothing changed for searching.

    Until now every piece of text was stored twice: once in `segments` and once more inside the full-text
    table. The full-text table now keeps only its word index and reads the text, when it needs it (to mark
    a match), from `segments` through a view. And the one copy that is left is stored compressed.
    """
    conn.execute("ALTER TABLE segments ADD COLUMN z INTEGER NOT NULL DEFAULT 0")
    conn.execute("DROP TABLE fts")
    conn.execute(FTS_SOURCE)
    conn.execute(f"CREATE VIRTUAL TABLE fts USING fts5(name, body, content='fts_src', content_rowid='id', tokenize=\"{TOKENIZER}\")")
    cursor = conn.execute("SELECT id, text FROM segments WHERE kind != 'name'")
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        packed = [(value, z, seg_id) for seg_id, (value, z) in ((r[0], pack_text(r[1] or "")) for r in rows) if z]
        conn.executemany("UPDATE segments SET text=?, z=? WHERE id=?", packed)
    conn.execute("INSERT INTO fts(fts) VALUES('rebuild')")


def _migration_6(conn: sqlite3.Connection) -> None:
    """A level for folders inside a root: [[folder below the root, level], ...] as JSON."""
    conn.execute("ALTER TABLE roots ADD COLUMN levels TEXT NOT NULL DEFAULT '[]'")


def _migration_7(conn: sqlite3.Connection) -> None:
    """A mark for each file's exact content (contentmark.py): files with the same mark are the same file.
    NULL until the file has been read with marks on."""
    conn.execute("ALTER TABLE files ADD COLUMN content TEXT")
    conn.execute("CREATE INDEX files_content ON files(content) WHERE content IS NOT NULL")


def _migration_8(conn: sqlite3.Connection) -> None:
    """Empty files are not copies of each other. The first marking gave them all the mark of "no bytes";
    each gets a mark of its own instead, which is what an empty file is given from now on."""
    conn.execute("UPDATE files SET content = '!' || id WHERE size <= 0 AND content IS NOT NULL")


def _migration_9(conn: sqlite3.Connection) -> None:
    """Sketches of meaning (sketches.py): one row a piece of text, for one model. `sketch` is the full
    sketch as signed bytes with `scale`; `fingerprint` its first numbers, scanned first. A piece that is
    deleted takes its sketch with it (the cascade). `files.sketched` says which model's sketches a file has,
    so what still waits is found without joining every piece."""
    conn.execute("""CREATE TABLE sketches (
                        segment_id INTEGER PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
                        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                        model TEXT NOT NULL,
                        dims INTEGER NOT NULL,
                        scale REAL NOT NULL,
                        sketch BLOB NOT NULL,
                        fingerprint BLOB NOT NULL
                    )""")
    conn.execute("CREATE INDEX sketches_file ON sketches(file_id)")
    conn.execute("ALTER TABLE files ADD COLUMN sketched TEXT NOT NULL DEFAULT ''")


def _migration_10(conn: sqlite3.Connection) -> None:
    """Pictures by likeness: one row a picture, for one model (looks.py): the sketch as signed bytes with its
    scale and its fingerprint, as for text; or `note` saying why the picture could not be read, so it is not
    tried again every run. `files.looked` names the model, as `sketched` does for text; a file that changes
    size or time wants a new look."""
    conn.execute("""CREATE TABLE looks (
                        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                        model TEXT NOT NULL,
                        scale REAL NOT NULL DEFAULT 0,
                        sketch BLOB,
                        fingerprint BLOB,
                        note TEXT NOT NULL DEFAULT ''
                    )""")
    conn.execute("ALTER TABLE files ADD COLUMN looked TEXT NOT NULL DEFAULT ''")


MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [_migration_1, _migration_2, _migration_3, _migration_4,
                                                          _migration_5, _migration_6, _migration_7, _migration_8,
                                                          _migration_9, _migration_10]
OCR_ORDER = 1_000_000   # the order number of a page's OCR text, after anything a text layer gave
SCHEMA_VERSION = len(MIGRATIONS)  # informational; the live value is len(MIGRATIONS)


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except sqlite3.Error:
        return False


# ---- paths -----------------------------------------------------------------

def encode_path(path: str) -> Tuple[str, Optional[bytes]]:
    """Text to index plus, when the name is not valid text, its exact bytes."""
    try:
        path.encode("utf-8")
        return path, None
    except UnicodeEncodeError:
        pass
    try:
        raw = path.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = path.encode("utf-8", "surrogatepass")
    return raw.decode("utf-8", "backslashreplace"), raw


def decode_path(text: str, raw: Optional[bytes]) -> str:
    if raw is None:
        return text
    try:
        return raw.decode("utf-8", "surrogateescape")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "surrogatepass")


def _split(path: str) -> Tuple[str, str]:
    """(folder, name) splitting on either separator, whatever the platform."""
    cut = max(path.rfind("/"), path.rfind("\\"))
    if cut < 0:
        return "", path
    return path[:cut], path[cut + 1:]


# ---- results ---------------------------------------------------------------

def _clean(text: str) -> str:
    """Stored text never holds the two marker characters, so a match can always be located."""
    if MARK_OPEN in text or MARK_CLOSE in text:
        return text.replace(MARK_OPEN, " ").replace(MARK_CLOSE, " ")
    return text


SPAN_WINDOW = 120   # a span may stretch this far to take in more of the query's words


def _first_span(marked: str, window: int = SPAN_WINDOW) -> Optional[Tuple[int, int]]:
    """Where the matched words sit in the unmarked text, or None when nothing is marked.

    With one matched word this is that word. With several, it is the stretch (at most `window` characters)
    that takes in the most different matched words, ending at the last word needed for that; the earliest
    such stretch wins. So a query of two words points at the place where both stand together, when there is one.
    """
    return _best_span(_marked_runs(marked), window)


def _marked_runs(marked: str) -> List[Tuple[int, int, str]]:
    runs: List[Tuple[int, int, str]] = []   # (start, end, word) in the text without markers
    removed = 0
    at = 0
    while True:
        start = marked.find(MARK_OPEN, at)
        if start < 0:
            break
        end = marked.find(MARK_CLOSE, start)
        if end < 0:
            break
        runs.append((start - removed, end - removed - 1, marked[start + 1:end].casefold()))
        removed += 2
        at = end + 1
    return runs


def _best_span(runs: List[Tuple[int, int, str]], window: int = SPAN_WINDOW) -> Optional[Tuple[int, int]]:
    if not runs:
        return None
    best = (0, runs[0][0], runs[0][1])
    for i, (start, _, _) in enumerate(runs):
        seen = set()
        for _, end, word in runs[i:]:
            if end - start > window and seen:
                break
            if word not in seen:
                seen.add(word)
                if len(seen) > best[0]:
                    best = (len(seen), start, end)
    return best[1], best[2]


def _fold_word(word: str) -> str:
    """A word as the index compares it: case and accents do not count."""
    return "".join(c for c in unicodedata.normalize("NFD", word.casefold()) if unicodedata.category(c) != "Mn")


def _words(text: str) -> Iterator[Tuple[int, int, str]]:
    """(start, end, folded word) for every word of a text, cut where the index's tokenizer cuts
    (letters, numbers, marks and private-use characters make words; everything else separates them)."""
    start = -1
    for at, ch in enumerate(text):
        cat = unicodedata.category(ch)
        inside = cat[0] in "LNM" or cat == "Co"
        if inside and start < 0:
            start = at
        elif not inside and start >= 0:
            yield start, at, _fold_word(text[start:at])
            start = -1
    if start >= 0:
        yield start, len(text), _fold_word(text[start:])


def exact_terms(query: str) -> Tuple[str, ...]:
    """The words of a query, folded, for an exact-form search."""
    return tuple(dict.fromkeys(word for _, _, word in _words(query) if word))


def exact_phrases(query: str) -> Tuple[Tuple[str, ...], ...]:
    """Each term of a query as its folded words: one word for a plain term, several for a quoted phrase."""
    out = []
    for term in parse_query(query):
        words = tuple(w for _, _, w in _words(term) if w)
        if words and words not in out:
            out.append(words)
    return tuple(out)


@functools.lru_cache(maxsize=64)
def _term_set(terms_json: str) -> Tuple[Tuple[str, ...], ...]:
    return tuple(tuple(term) if isinstance(term, list) else (term,) for term in json.loads(terms_json))


def _holds(words: List[str], phrase: Tuple[str, ...]) -> bool:
    """Whether the words of a text hold this phrase: the same words, next to each other, in that order."""
    n = len(phrase)
    if n == 1:
        return phrase[0] in words
    first = phrase[0]
    return any(words[i] == first and tuple(words[i:i + n]) == phrase for i in range(len(words) - n + 1))


def _exact_ok(name: Optional[str], body: Optional[str], place: str, terms_json: str) -> int:
    """SQL function: 1 when every asked-for word (and every quoted phrase) stands in this row in exactly
    that form. `place` says where it must stand: in the name, in the body, or in either."""
    terms = _term_set(terms_json)
    in_name = [w for _, _, w in _words(name or "")] if place != "body" else []
    in_body = [w for _, _, w in _words(body or "")] if place != "name" else []
    return int(all(_holds(in_name, t) or _holds(in_body, t) for t in terms))


def _exact_span(text: str, terms: Sequence[str]) -> Optional[Tuple[int, int]]:
    wanted = set(terms)
    return _best_span([(start, end, word) for start, end, word in _words(text) if word in wanted])


def _ascii_lower(text: str) -> str:
    return "".join(ch.lower() if ch.isascii() else ch for ch in text)


def _under_clause(prefix: str, case_insensitive: Optional[bool], column: str = "path") -> Tuple[str, List[object]]:
    """SQL for "this path is the folder `prefix` or below it", on whole path parts, with either separator."""
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    text, _ = encode_path(prefix)
    base = text.rstrip("/\\")
    wrap = (lambda x: f"lower({x})") if case_insensitive else (lambda x: x)
    # SQLite's lower() folds ASCII letters only, so the text it is compared with is folded the same way.
    # Folding more here (a capital A-umlaut, say) would make a folder with such a name match nothing.
    fold = _ascii_lower if case_insensitive else (lambda t: t)
    clauses = [f"{wrap(column)} = ?"]
    args: List[object] = [fold(base)]
    for sep in ("/", "\\"):
        clauses.append(f"{wrap(f'substr({column}, 1, ?)')} = ?")
        args.extend([len(base) + 1, fold(base + sep)])
    return "(" + " OR ".join(clauses) + ")", args


def parse_query(query: str) -> List[str]:
    """What the user typed, as terms: each is one word, or a phrase that was typed between double quotes.
    A quote that is never closed is taken as a slip of the keyboard and all quotes are ignored."""
    if query.count('"') % 2:
        return [t for t in query.replace('"', " ").split() if t]
    terms: List[str] = []
    for index, piece in enumerate(query.split('"')):
        if index % 2:                       # between a pair of quotes: one phrase
            phrase = " ".join(piece.split())
            if phrase:
                terms.append(phrase)
        else:
            terms.extend(piece.split())
    return terms


def _quoted(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_match(query: str) -> Optional[str]:
    """Turn what the user typed into a safe FTS5 query: every word, and every phrase, quoted."""
    terms = parse_query(query)
    if not terms:
        return None
    return " ".join(_quoted(t) for t in terms)


def closeness_matches(query: str, near_words: int) -> Optional[Tuple[str, str]]:
    """For a query of two or more words: (a match for the words as one phrase in the order typed, a match
    for the words standing within `near_words` of each other in any order). None when there is nothing
    to be close to, or closeness is turned off."""
    terms = parse_query(query)
    words = [w for _, _, w in _words(" ".join(terms))]
    if len(words) < 2 or near_words <= 0:
        return None
    phrase = _quoted(" ".join(terms))
    near = "NEAR(" + " ".join(_quoted(t) for t in terms) + f", {int(near_words)})"
    return phrase, (near if len(terms) > 1 else phrase)


# ---- the store -------------------------------------------------------------

def _order_in_python(items, value_of, keys):
    """Sort (score, ..., file id) items by (key, descending) pairs as a word search would be sorted, with the
    file id as the last tie-break. `value_of(item, key)` gives the key's value for an item."""
    out = sorted(items, key=lambda it: it[-1])
    for key, down in reversed(list(keys)):
        out.sort(key=lambda it: value_of(it, key), reverse=down)
    return out


def sketches_module():
    from . import sketches
    return sketches


class SqliteStore:
    def __init__(self, path: os.PathLike | str, near_words: int = 15, compress_text: bool = True,
                 prefer_under: str = "") -> None:
        self.path = Path(path)
        # Of several copies of one file, the one below this folder (or on this drive) is the one a search shows.
        self.prefer_under = prefer_under
        self.compress_text = compress_text     # for text stored from now on; text already stored is read either way
        self.near_words = near_words     # how close words must stand to rank above the same words far apart
        if not self.path.parent.is_dir():
            raise StoreError(f"the folder for the index does not exist: {self.path.parent}")
        self._depth = 0
        self._conn: Optional[sqlite3.Connection] = None
        self._prints: Optional[Tuple[str, int, int, object, object, object]] = None   # (model, data version, own writes, ids, files, matrix)
        self._sketch_writes = 0
        self._look_prints: Optional[Tuple[str, int, int, object, object]] = None
        self._looks_writes = 0
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if not fts5_available(conn):
                raise StoreError("this SQLite build lacks FTS5, which the index needs")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA secure_delete=ON")
            conn.create_function("dugup_exact", 4, _exact_ok, deterministic=True)
            conn.create_function("dugup_text", 2, unpack_text, deterministic=True)
            conn.create_function("dugup_clean", 1, lambda name: _clean(name or ""), deterministic=True)
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    # -- plumbing

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("the store is closed")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        wanted = len(MIGRATIONS)
        if current > wanted:
            raise StoreVersionError(
                f"this index is version {current}, but this program only understands up to version {wanted}; "
                "update the program"
            )
        for number in range(current + 1, wanted + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[number - 1](conn)
                conn.execute(f"PRAGMA user_version={number}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing group of changes. Nested uses join the outer one."""
        conn = self.conn
        if self._depth == 0:
            conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                conn.execute("ROLLBACK")
                raise
            self._depth = 0
            conn.execute("COMMIT")
        else:
            name = f"sp{self._depth}"
            conn.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
                raise
            self._depth -= 1
            conn.execute(f"RELEASE {name}")

    # -- fingerprint

    def set_fingerprint(self, fingerprint: Dict[str, object]) -> None:
        try:
            text = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
        except TypeError as exc:
            raise ValueError(f"fingerprint must be plain JSON values: {exc}") from exc
        with self.transaction():
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('fingerprint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (text,),
            )

    def get_fingerprint(self) -> Optional[Dict[str, object]]:
        row = self.conn.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        return None if row is None else json.loads(row[0])

    def changed_fingerprint_keys(self, current: Dict[str, object]) -> List[str]:
        """Keys that differ between the stored fingerprint and `current` (sorted)."""
        stored = self.get_fingerprint() or {}
        keys = set(stored) | set(current)
        return sorted(k for k in keys if stored.get(k) != current.get(k) or (k in stored) != (k in current))

    # -- files and segments

    def upsert_file(self, record: FileRecord) -> int:
        text, raw = encode_path(record.path)
        folder, name = _split(text)
        with self.transaction():
            existing = self.conn.execute("SELECT id FROM files WHERE path=?", (text,)).fetchone()
            values = (raw, name, folder, record.size, record.modified, record.kind.value,
                      record.file_id, record.state.value, record.error, record.policy)
            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO files(path_raw, name, folder, size, modified, kind, file_id, state, error, policy, path) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    values + (text,),
                )
                row_id = int(cur.lastrowid)
            else:
                row_id = int(existing[0])
                self.conn.execute(
                    "UPDATE files SET path_raw=?, name=?, folder=?, size=?, modified=?, kind=?, file_id=?, "
                    "state=?, error=?, policy=?, looked=CASE WHEN size=? AND modified=? THEN looked ELSE '' END WHERE id=?",
                    values + (record.size, record.modified, row_id),
                )
            self.conn.execute("UPDATE files SET missing_since=? WHERE id=?", (record.missing_since, row_id))
            self._refresh_name_segment(row_id, name)
        record.row_id = row_id
        return row_id

    def _refresh_name_segment(self, row_id: int, name: str) -> None:
        old = self.conn.execute(
            "SELECT id FROM segments WHERE file_id=? AND kind=?", (row_id, SegmentKind.NAME.value)
        ).fetchone()
        name = _clean(name)
        if old is not None:
            return      # the same path has the same name: the word index already holds it
        cur = self.conn.execute(
            "INSERT INTO segments(file_id, page, ord, kind, text) VALUES(?,?,?,?,?)",
            (row_id, 0, 0, SegmentKind.NAME.value, ""),
        )
        self.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", (cur.lastrowid, name, ""))

    @staticmethod
    def _record(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            path=decode_path(row["path"], row["path_raw"]),
            size=row["size"],
            modified=row["modified"],
            kind=FileKind(row["kind"]),
            file_id=row["file_id"],
            state=FileState(row["state"]),
            error=row["error"],
            policy=row["policy"],
            row_id=row["id"],
            missing_since=row["missing_since"],
        )

    def get_file(self, path: str) -> Optional[FileRecord]:
        text, _ = encode_path(path)
        row = self.conn.execute("SELECT * FROM files WHERE path=?", (text,)).fetchone()
        return None if row is None else self._record(row)

    def get_file_by_id(self, row_id: int) -> Optional[FileRecord]:
        row = self.conn.execute("SELECT * FROM files WHERE id=?", (int(row_id),)).fetchone()
        return None if row is None else self._record(row)

    def list_files(self, prefix: Optional[str] = None, state: Optional[FileState] = None,
                   limit: Optional[int] = None, after_row: int = 0, missing: Optional[bool] = None,
                   case_insensitive: Optional[bool] = None) -> List[FileRecord]:
        """Files in row order. `after_row` continues a walk; `missing` None means both kinds."""
        clauses, args = ["id > ?"], [after_row]
        if prefix is not None:
            clause, more = _under_clause(prefix, case_insensitive)
            clauses.append(clause)
            args.extend(more)
        if state is not None:
            if not isinstance(state, FileState):
                raise ValueError("state must be a FileState")
            clauses.append("state = ?")
            args.append(state.value)
        if missing is not None:
            clauses.append("missing_since IS NOT NULL" if missing else "missing_since IS NULL")
        sql = f"SELECT * FROM files WHERE {' AND '.join(clauses)} ORDER BY id"
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must not be negative")
            sql += " LIMIT ?"
            args.append(limit)
        return [self._record(r) for r in self.conn.execute(sql, args)]

    # -- the same content in several places

    def set_content(self, row_id: int, mark: Optional[str]) -> None:
        self.conn.execute("UPDATE files SET content=? WHERE id=?", (mark or None, row_id))

    def content_of(self, row_id: int) -> Optional[str]:
        row = self.conn.execute("SELECT content FROM files WHERE id=?", (row_id,)).fetchone()
        return None if row is None else row[0]

    def copies_of(self, row_ids: Sequence[int]) -> Dict[int, List[str]]:
        """For each of these files, the paths of the OTHER files with the same content that were there at
        the last scan, shortest path first. A file with no mark, or no copy, is left out."""
        found: Dict[int, List[str]] = {}
        ids = [int(i) for i in row_ids]
        for at in range(0, len(ids), 400):
            chunk = ids[at:at + 400]
            marks = ",".join("?" * len(chunk))
            sql = ("SELECT a.id AS one, b.path, b.path_raw FROM files a JOIN files b ON b.content = a.content AND b.id <> a.id "
                   f"WHERE a.id IN ({marks}) AND a.content IS NOT NULL AND b.missing_since IS NULL "
                   "ORDER BY a.id, length(b.path), b.path")
            for r in self.conn.execute(sql, chunk):
                found.setdefault(r["one"], []).append(decode_path(r["path"], r["path_raw"]))
        return found

    def same_content(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None) -> List[List[FileRecord]]:
        """Files below `prefix` (or anywhere) that share their exact content with another file there, in sets
        of two or more, each set in row order. Files not there at the last scan are left out."""
        clauses, args = ["content IS NOT NULL", "substr(content, 1, 1) <> '!'", "missing_since IS NULL"], []
        if prefix is not None:
            clause, more = _under_clause(prefix, case_insensitive)
            clauses.append(clause)
            args.extend(more)
        where = " AND ".join(clauses)
        sql = (f"SELECT * FROM files WHERE {where} AND content IN (SELECT content FROM files WHERE {where} "
               f"GROUP BY content HAVING COUNT(*) > 1) ORDER BY content, id")
        sets: Dict[str, List[FileRecord]] = {}
        for r in self.conn.execute(sql, args + args):
            sets.setdefault(r["content"], []).append(self._record(r))
        return [members for members in sets.values() if len(members) > 1]

    def files_in_folders(self, folders: Sequence[str]) -> Dict[str, int]:
        """How many files (there at the last scan) lie directly in each of these folders, by the folder's spelling."""
        found: Dict[str, int] = {}
        wanted = [encode_path(f)[0] for f in folders]
        for at in range(0, len(wanted), 400):
            chunk = wanted[at:at + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT folder, COUNT(*) FROM files WHERE folder IN ({marks}) AND missing_since IS NULL "
                                       "GROUP BY folder", chunk):
                found[r[0]] = int(r[1])
        return {f: found.get(encode_path(f)[0], 0) for f in folders}

    def read_marks(self) -> List[Tuple[str, str]]:
        """(content mark, kind of file) for the files that have been read (done, or partial) and were there at
        the last scan. The kind belongs to it: the same bytes under another ending go to another reader."""
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT DISTINCT content, kind FROM files WHERE content IS NOT NULL AND missing_since IS NULL AND state IN (?,?)",
            (FileState.DONE.value, FileState.PARTIAL.value))]

    def read_twin(self, mark: str, not_row: int, kind: FileKind) -> Optional[FileRecord]:
        """A file of this kind with this content that has been read, other than `not_row`; a fully read one first."""
        row = self.conn.execute(
            "SELECT * FROM files WHERE content=? AND kind=? AND id<>? AND missing_since IS NULL AND state IN (?,?) "
            "ORDER BY (state<>?), id LIMIT 1",
            (mark, kind.value, not_row, FileState.DONE.value, FileState.PARTIAL.value, FileState.DONE.value)).fetchone()
        return None if row is None else self._record(row)

    def _copy_segments(self, from_row: int, to_row: int, where: str, args: Sequence[object]) -> None:
        name = _clean(self.conn.execute("SELECT name FROM files WHERE id=?", (to_row,)).fetchone()[0])
        self.conn.execute("UPDATE files SET sketched='' WHERE id=?", (to_row,))
        rows = self.conn.execute(f"SELECT id, page, ord, kind, text, z FROM segments WHERE file_id=? AND {where} ORDER BY id",
                                 [from_row] + list(args)).fetchall()
        for r in rows:
            cur = self.conn.execute("INSERT INTO segments(file_id, page, ord, kind, text, z) VALUES(?,?,?,?,?,?)",
                                    (to_row, r["page"], r["ord"], r["kind"], r["text"], r["z"]))
            self.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)",
                              (cur.lastrowid, name, unpack_text(r["text"], r["z"])))
            self.conn.execute(
                "INSERT INTO regions(segment_id, engine, label, x0, y0, x1, y1, confidence, text) "
                "SELECT ?, engine, label, x0, y0, x1, y1, confidence, text FROM regions WHERE segment_id=? ORDER BY id",
                (cur.lastrowid, r["id"]))

    def copy_reading(self, from_row: int, to_row: int) -> None:
        """Give one file everything that was read from another with the same content: its text, its page
        records, and what OCR read or still has to read. The file's own name stays its own."""
        with self.transaction():
            for row in (from_row, to_row):
                if self.conn.execute("SELECT 1 FROM files WHERE id=?", (row,)).fetchone() is None:
                    raise StoreError(f"no file with id {row}")
            self._delete_segments(to_row, keep_name=True)
            self._copy_segments(from_row, to_row, "kind != ?", (SegmentKind.NAME.value,))
            self.conn.execute("DELETE FROM pages WHERE file_id=?", (to_row,))
            self.conn.execute(
                "INSERT INTO pages(file_id, page, verdict, needs_ocr, reason, ocr, ocr_reason, width, height, camera, ocr_engine) "
                "SELECT ?, page, verdict, needs_ocr, reason, ocr, ocr_reason, width, height, camera, ocr_engine "
                "FROM pages WHERE file_id=?", (to_row, from_row))

    def copy_page(self, from_row: int, to_row: int, page: int) -> None:
        """Give one file what OCR made of a page of another with the same content (read, or failed and why)."""
        with self.transaction():
            self._page_row(from_row, page)
            self._page_row(to_row, page)
            self._delete_ocr_segments(to_row, page)
            self._copy_segments(from_row, to_row, "page=? AND kind=?", (page, SegmentKind.OCR.value))
            self.conn.execute(
                "UPDATE pages SET (needs_ocr, ocr, ocr_reason, ocr_engine) = (SELECT needs_ocr, ocr, ocr_reason, ocr_engine "
                "FROM pages WHERE file_id=? AND page=?) WHERE file_id=? AND page=?", (from_row, page, to_row, page))

    def unmarked(self, prefix: Optional[str] = None, limit: Optional[int] = None, after_row: int = 0,
                 case_insensitive: Optional[bool] = None) -> List[FileRecord]:
        """Files that were read (done, partial or failed) and have no content mark yet, in row order."""
        clauses, args = ["id > ?", "content IS NULL", "missing_since IS NULL", "state IN (?,?,?)"], \
            [after_row, FileState.DONE.value, FileState.PARTIAL.value, FileState.FAILED.value]
        if prefix is not None:
            clause, more = _under_clause(prefix, case_insensitive)
            clauses.append(clause)
            args.extend(more)
        sql = f"SELECT * FROM files WHERE {' AND '.join(clauses)} ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(max(0, limit))
        return [self._record(r) for r in self.conn.execute(sql, args)]

    def copy_counts(self) -> Tuple[int, int, int]:
        """(files with a content mark, sets of two or more files with the same content, files that are an
        extra copy of another), counting files that were there at the last scan."""
        marked = self.conn.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL AND missing_since IS NULL").fetchone()[0]
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM files WHERE content IS NOT NULL "
            "AND missing_since IS NULL GROUP BY content HAVING COUNT(*) > 1)").fetchone()
        return int(marked), int(row[0]), int(row[1])

    def move_file(self, old_path: str, new_path: str) -> int:
        """The same file at a new path: its text, pages and state are kept. Returns the row id."""
        old_text, _ = encode_path(old_path)
        new_text, new_raw = encode_path(new_path)
        folder, name = _split(new_text)
        with self.transaction():
            row = self.conn.execute("SELECT id FROM files WHERE path=?", (old_text,)).fetchone()
            if row is None:
                raise StoreError("cannot move a file the index does not know")
            if old_text != new_text and self.conn.execute("SELECT 1 FROM files WHERE path=?", (new_text,)).fetchone():
                raise StoreError("cannot move a file onto a path the index already holds")
            row_id = int(row[0])
            # The name sits beside every piece of text in the word index. The index reads what to take out
            # from the stored rows, so the old entries go BEFORE the name changes, and the new ones come after.
            ids = [r[0] for r in self.conn.execute("SELECT id FROM segments WHERE file_id=?", (row_id,))]
            for seg_id in ids:
                self.conn.execute("DELETE FROM fts WHERE rowid=?", (seg_id,))
            self.conn.execute("UPDATE files SET path=?, path_raw=?, name=?, folder=?, missing_since=NULL WHERE id=?",
                              (new_text, new_raw, name, folder, row_id))
            self.conn.execute("INSERT INTO fts(rowid, name, body) SELECT s.id, s.name, s.body FROM fts_src s "
                              "JOIN segments g ON g.id = s.id WHERE g.file_id=?", (row_id,))
        return row_id

    def remove_files(self, row_ids: Sequence[int]) -> int:
        """Take these files out of the index with their text and page records. Unknown ids are ignored."""
        removed = 0
        with self.transaction():
            for row_id in row_ids:
                if self.conn.execute("SELECT 1 FROM files WHERE id=?", (row_id,)).fetchone() is None:
                    continue
                self._delete_segments(row_id, keep_name=False)
                self.conn.execute("DELETE FROM files WHERE id=?", (row_id,))
                removed += 1
        return removed

    def set_missing(self, row_id: int, since: Optional[float]) -> None:
        with self.transaction():
            cur = self.conn.execute("UPDATE files SET missing_since=? WHERE id=?", (since, row_id))
            if cur.rowcount == 0:
                raise StoreError(f"no file with id {row_id}")

    def purge_missing(self, prefix: str, before: float, case_insensitive: Optional[bool] = None) -> int:
        """Remove files below `prefix` that have been missing since before `before`."""
        clause, args = _under_clause(prefix, case_insensitive)
        with self.transaction():
            ids = [r[0] for r in self.conn.execute(
                f"SELECT id FROM files WHERE {clause} AND missing_since IS NOT NULL AND missing_since < ?",
                args + [before])]
            for row_id in ids:
                self._delete_segments(row_id, keep_name=False)
                self.conn.execute("DELETE FROM files WHERE id=?", (row_id,))
        return len(ids)

    def set_state(self, row_id: int, state: FileState, error: str = "") -> None:
        if not isinstance(state, FileState):
            raise ValueError("state must be a FileState")
        with self.transaction():
            cur = self.conn.execute("UPDATE files SET state=?, error=? WHERE id=?", (state.value, error, row_id))
            if cur.rowcount == 0:
                raise StoreError(f"no file with id {row_id}")

    def replace_segments(self, row_id: int, segments: Sequence[SegmentIn]) -> None:
        """Make the stored text of a file exactly `segments` (safe to repeat)."""
        self._sketch_writes += 1
        with self.transaction():
            name_row = self.conn.execute("SELECT name FROM files WHERE id=?", (row_id,)).fetchone()
            if name_row is None:
                raise StoreError(f"no file with id {row_id}")
            self._delete_segments(row_id, keep_name=True)
            self.conn.execute("UPDATE files SET sketched='' WHERE id=?", (row_id,))     # its sketches went with its pieces
            for seg in segments:
                if seg.kind is SegmentKind.NAME:
                    raise ValueError("name segments are managed by the store")
                text = _clean(seg.text)
                cur = self.conn.execute(
                    "INSERT INTO segments(file_id, page, ord, kind, text, z) VALUES(?,?,?,?,?,?)",
                    (row_id, seg.page, seg.order, seg.kind.value) + pack_text(text, self.compress_text),
                )
                self.conn.execute(
                    "INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", (cur.lastrowid, _clean(name_row[0]), text)
                )

    def set_pages(self, row_id: int, pages: Sequence[PageInfo]) -> None:
        """Make the stored page records of a file exactly `pages` (safe to repeat)."""
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM files WHERE id=?", (row_id,)).fetchone() is None:
                raise StoreError(f"no file with id {row_id}")
            self.conn.execute("DELETE FROM pages WHERE file_id=?", (row_id,))
            self.conn.executemany(
                "INSERT INTO pages(file_id, page, verdict, needs_ocr, reason) VALUES(?,?,?,?,?)",
                [(row_id, p.page, p.verdict.value, int(p.needs_ocr), p.unread_reason) for p in pages])

    def get_pages(self, row_id: int) -> List[PageInfo]:
        rows = self.conn.execute(
            "SELECT page, verdict, needs_ocr, reason FROM pages WHERE file_id=? ORDER BY page", (row_id,))
        return [PageInfo(r[0], PageVerdict(r[1]), bool(r[2]), r[3]) for r in rows]

    # -- OCR

    def ocr_queue(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None) -> List[OcrTask]:
        clauses, args = ["p.needs_ocr = 1", "p.ocr IN ('', 'retry')", "f.missing_since IS NULL"], []
        if prefix is not None:
            clause, args = _under_clause(prefix, case_insensitive, "f.path")
            clauses.append(clause)
        sql = ("SELECT f.id, f.path, f.path_raw, f.kind, p.page, p.width, p.height, p.camera, p.verdict, p.ocr "
               f"FROM pages p JOIN files f ON f.id = p.file_id WHERE {' AND '.join(clauses)} ORDER BY f.id, p.page")
        return [OcrTask(r["id"], decode_path(r["path"], r["path_raw"]), FileKind(r["kind"]), r["page"],
                        r["width"], r["height"], bool(r["camera"]), r["verdict"], r["ocr"] == "retry")
                for r in self.conn.execute(sql, args)]

    def _page_row(self, row_id: int, page: int) -> None:
        if self.conn.execute("SELECT 1 FROM pages WHERE file_id=? AND page=?", (row_id, page)).fetchone() is None:
            raise StoreError(f"file {row_id} has no page {page}")

    def set_page_shape(self, row_id: int, page: int, width: int, height: int, camera: bool) -> None:
        with self.transaction():
            self._page_row(row_id, page)
            self.conn.execute("UPDATE pages SET width=?, height=?, camera=? WHERE file_id=? AND page=?",
                              (int(width), int(height), int(bool(camera)), row_id, page))

    def _delete_ocr_segments(self, row_id: int, page: int) -> None:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM segments WHERE file_id=? AND page=? AND kind=?",
                                               (row_id, page, SegmentKind.OCR.value))]
        for seg_id in ids:
            self.conn.execute("DELETE FROM fts WHERE rowid=?", (seg_id,))
            self.conn.execute("DELETE FROM segments WHERE id=?", (seg_id,))

    def save_ocr(self, row_id: int, page: int, result: OcrResult) -> None:
        lines = [(_clean(" ".join(line.text.split())), line) for line in result.lines]
        lines = [(text, line) for text, line in lines if text]
        with self.transaction():
            self._page_row(row_id, page)
            self._delete_ocr_segments(row_id, page)
            if lines:
                name = self.conn.execute("SELECT name FROM files WHERE id=?", (row_id,)).fetchone()[0]
                text = "\n".join(t for t, _ in lines)
                cur = self.conn.execute("INSERT INTO segments(file_id, page, ord, kind, text, z) VALUES(?,?,?,?,?,?)",
                                        (row_id, page, OCR_ORDER, SegmentKind.OCR.value) + pack_text(text, self.compress_text))
                self.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", (cur.lastrowid, _clean(name), text))
                self.conn.executemany(
                    "INSERT INTO regions(segment_id, engine, label, x0, y0, x1, y1, confidence, text) "
                    "VALUES(?,?,'line',?,?,?,?,?,?)",
                    [(cur.lastrowid, f"{result.engine} {result.version}".strip(), l.box.x0, l.box.y0, l.box.x1, l.box.y1,
                      l.confidence, t) for t, l in lines])
            self.conn.execute("UPDATE pages SET needs_ocr=0, ocr='done', ocr_reason='', ocr_engine=? "
                              "WHERE file_id=? AND page=?", (f"{result.engine} {result.version}".strip(), row_id, page))
            if lines:
                self.conn.execute("UPDATE files SET sketched='' WHERE id=?", (row_id,))   # new text: sketches to make

    def fail_ocr(self, row_id: int, page: int, reason: str) -> None:
        with self.transaction():
            self._page_row(row_id, page)
            self.conn.execute("UPDATE pages SET ocr='failed', ocr_reason=? WHERE file_id=? AND page=?",
                              (reason, row_id, page))

    def defer_ocr(self, row_id: int, page: int, reason: str) -> None:
        with self.transaction():
            self._page_row(row_id, page)
            self.conn.execute("UPDATE pages SET ocr='retry', ocr_reason=? WHERE file_id=? AND page=?",
                              (reason, row_id, page))

    def replace_text(self, row_id: int, segments: Sequence[SegmentIn], pages: Sequence[PageInfo]) -> None:
        with self.transaction():
            old = {r["page"]: r for r in self.conn.execute(
                "SELECT page, ocr, ocr_engine, width, height, camera FROM pages WHERE file_id=?", (row_id,))}
            keep = {p.page for p in pages if p.needs_ocr and p.page in old and old[p.page]["ocr"] == "done"}
            saved = [(r[0], r[1], unpack_text(r[2], r[3])) for r in self.conn.execute(
                "SELECT id, page, text, z FROM segments WHERE file_id=? AND kind=?", (row_id, SegmentKind.OCR.value))
                if r[1] in keep]
            lines = {seg_id: self.conn.execute(
                "SELECT engine, label, x0, y0, x1, y1, confidence, text FROM regions WHERE segment_id=? ORDER BY id",
                (seg_id,)).fetchall() for seg_id, _, _ in saved}
            self.replace_segments(row_id, segments)
            name = _clean(self.conn.execute("SELECT name FROM files WHERE id=?", (row_id,)).fetchone()[0])
            for seg_id, page, text in saved:      # the same text under a new handle: old Evidence stops matching
                cur = self.conn.execute("INSERT INTO segments(file_id, page, ord, kind, text, z) VALUES(?,?,?,?,?,?)",
                                        (row_id, page, OCR_ORDER, SegmentKind.OCR.value) + pack_text(text, self.compress_text))
                self.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", (cur.lastrowid, name, text))
                self.conn.executemany(
                    "INSERT INTO regions(segment_id, engine, label, x0, y0, x1, y1, confidence, text) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(cur.lastrowid,) + tuple(line) for line in lines[seg_id]])
            self.set_pages(row_id, pages)
            for page in sorted(old):
                row = old[page]
                if page in keep:
                    self.conn.execute("UPDATE pages SET needs_ocr=0, ocr='done', ocr_engine=? WHERE file_id=? AND page=?",
                                      (row["ocr_engine"], row_id, page))
                if row["width"] is not None:
                    self.conn.execute("UPDATE pages SET width=?, height=?, camera=? WHERE file_id=? AND page=?",
                                      (row["width"], row["height"], row["camera"], row_id, page))

    def ocr_engines(self) -> Dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT ocr_engine, COUNT(*) FROM pages p JOIN files f ON f.id = p.file_id "
            "WHERE p.ocr='done' AND f.missing_since IS NULL GROUP BY ocr_engine")}

    def requeue_ocr_not_by(self, engine: str, prefix: Optional[str] = None,
                           case_insensitive: Optional[bool] = None) -> List[int]:
        where, args = "", []
        if prefix is not None:
            clause, args = _under_clause(prefix, case_insensitive)
            where = f"AND file_id IN (SELECT id FROM files WHERE {clause})"
        with self.transaction():
            ids = [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT file_id FROM pages WHERE ocr='done' AND ocr_engine != ? {where} ORDER BY file_id",
                [engine] + args)]
            self.conn.execute(f"UPDATE pages SET ocr='', needs_ocr=1 WHERE ocr='done' AND ocr_engine != ? {where}",
                              [engine] + args)
        return ids

    def retry_ocr(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None) -> List[int]:
        where, args = "", []
        if prefix is not None:
            clause, args = _under_clause(prefix, case_insensitive)
            where = f"AND file_id IN (SELECT id FROM files WHERE {clause})"
        with self.transaction():
            ids = [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT file_id FROM pages WHERE ocr='failed' {where} ORDER BY file_id", args)]
            self.conn.execute(f"UPDATE pages SET ocr='', ocr_reason='' WHERE ocr='failed' {where}", args)
        return ids

    def ocr_status(self, row_id: int) -> List[Tuple[int, str, str]]:
        rows = self.conn.execute("SELECT page, needs_ocr, ocr, ocr_reason FROM pages WHERE file_id=? "
                                 "AND (needs_ocr=1 OR ocr != '') ORDER BY page", (row_id,))
        names = {"": OCR_WAITING, "retry": OCR_WAITING, "done": OCR_DONE, "failed": OCR_FAILED}
        return [(r[0], names.get(r[2], OCR_WAITING), r[3] if r[2] == "failed" else "") for r in rows]

    def _line_box(self, segment_id: int, start: int) -> Optional[Box]:
        """The box of the OCR line that holds character `start` of a segment's text."""
        at = 0
        for r in self.conn.execute("SELECT x0, y0, x1, y1, text FROM regions WHERE segment_id=? ORDER BY id", (segment_id,)):
            at += len(r["text"]) + 1    # the lines are joined by one line break
            if start < at:
                return Box(r["x0"], r["y0"], r["x1"], r["y1"])
        return None

    def _delete_segments(self, row_id: int, keep_name: bool) -> None:
        clause = "AND kind != 'name'" if keep_name else ""
        ids = [r[0] for r in self.conn.execute(f"SELECT id FROM segments WHERE file_id=? {clause}", (row_id,))]
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            self.conn.execute(f"DELETE FROM fts WHERE rowid IN ({marks})", chunk)
            self.conn.execute(f"DELETE FROM segments WHERE id IN ({marks})", chunk)

    def file_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # -- sketches of meaning (sketches.py); the store does not know what a sketch means

    def unsketched(self, model: str, limit: int = 200, prefix: Optional[str] = None,
                   case_insensitive: Optional[bool] = None, after: Optional[Tuple[float, int]] = None) -> List[FileRecord]:
        """Files with text that have no sketches by this model yet, the most recently changed first. Files
        listed by name only (no text) are not among them. `after` (modified, id) of the last one seen
        continues the list past it, so a caller that leaves some files alone still gets to the rest."""
        clauses, args = ["f.sketched <> ?", "f.missing_since IS NULL", "f.state IN (?,?)",
                         "EXISTS (SELECT 1 FROM segments s WHERE s.file_id = f.id AND s.kind != 'name')"], \
            [model, FileState.DONE.value, FileState.PARTIAL.value]
        if after is not None:
            clauses.append("(f.modified < ? OR (f.modified = ? AND f.id > ?))")
            args.extend([after[0], after[0], after[1]])
        if prefix is not None:
            clause, more = _under_clause(prefix, case_insensitive, "f.path")
            clauses.append(clause)
            args.extend(more)
        sql = f"SELECT f.* FROM files f WHERE {' AND '.join(clauses)} ORDER BY f.modified DESC, f.id LIMIT ?"
        return [self._record(r) for r in self.conn.execute(sql, args + [max(0, limit)])]

    def text_of(self, row_id: int) -> List[Tuple[int, int, str]]:
        """(segment id, page, text) of a file's pieces of text, in order."""
        rows = self.conn.execute("SELECT id, page, text, z FROM segments WHERE file_id=? AND kind != 'name' ORDER BY page, ord, id",
                                 (row_id,))
        return [(r["id"], r["page"], unpack_text(r["text"], r["z"])) for r in rows]

    def save_sketches(self, row_id: int, model: str, dims: int, rows: Sequence[Tuple[int, float, bytes, bytes]]) -> None:
        """Keep the sketches of a file's pieces: (segment id, scale, sketch bytes, fingerprint bytes). Older
        sketches of the file by any model go, and the file is marked as sketched by this one."""
        self._sketch_writes += 1
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM files WHERE id=?", (row_id,)).fetchone() is None:
                raise StoreError(f"no file with id {row_id}")
            self.conn.execute("DELETE FROM sketches WHERE file_id=?", (row_id,))
            self.conn.executemany(
                "INSERT INTO sketches(segment_id, file_id, model, dims, scale, sketch, fingerprint) VALUES(?,?,?,?,?,?,?)",
                [(seg, row_id, model, dims, scale, sketch, fingerprint) for seg, scale, sketch, fingerprint in rows])
            self.conn.execute("UPDATE files SET sketched=? WHERE id=?", (model, row_id))

    def forget_sketches(self, row_id: int) -> None:
        """A file's text changed (or its level no longer asks for meaning): its sketches are stale."""
        self._sketch_writes += 1
        with self.transaction():
            self.conn.execute("DELETE FROM sketches WHERE file_id=?", (row_id,))
            self.conn.execute("UPDATE files SET sketched='' WHERE id=?", (row_id,))

    def sketch_counts(self, model: str) -> Tuple[int, int]:
        """(files sketched by this model, files with text that could be), among files there at the last scan."""
        done = self.conn.execute("SELECT COUNT(*) FROM files WHERE sketched=? AND missing_since IS NULL", (model,)).fetchone()[0]
        could = self.conn.execute(
            "SELECT COUNT(*) FROM files f WHERE f.missing_since IS NULL AND f.state IN (?,?) AND "
            "EXISTS (SELECT 1 FROM segments s WHERE s.file_id = f.id AND s.kind != 'name')",
            (FileState.DONE.value, FileState.PARTIAL.value)).fetchone()[0]
        return int(done), int(could)

    def fingerprints(self, model: str):
        """Every fingerprint by this model, for files there at the last scan: (segment ids, file ids, int8 matrix)."""
        import numpy as np
        from .sketches import FINGERPRINT
        rows = self.conn.execute(
            "SELECT k.segment_id, k.file_id, k.fingerprint FROM sketches k JOIN files f ON f.id = k.file_id "
            "WHERE k.model=? AND f.missing_since IS NULL ORDER BY k.segment_id", (model,)).fetchall()
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        files = np.fromiter((r[1] for r in rows), dtype=np.int64, count=len(rows))
        matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.int8).reshape(len(rows), FINGERPRINT) if rows \
            else np.zeros((0, FINGERPRINT), dtype=np.int8)
        return ids, files, matrix

    def sketches_of(self, segment_ids: Sequence[int]) -> Dict[int, Tuple[int, int, float, bytes]]:
        """segment id -> (file id, dims, scale, sketch bytes) for these pieces."""
        found: Dict[int, Tuple[int, int, float, bytes]] = {}
        ids = [int(i) for i in segment_ids]
        for at in range(0, len(ids), 400):
            chunk = ids[at:at + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT segment_id, file_id, dims, scale, sketch FROM sketches WHERE segment_id IN ({marks})", chunk):
                found[r[0]] = (r[1], r[2], r[3], r[4])
        return found

    def _fingerprints_cached(self, model: str):
        """The fingerprints, kept between searches while nothing has written to the index (this connection's own
        sketch writes and other connections' commits both count)."""
        version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        if self._prints is None or self._prints[:3] != (model, version, self._sketch_writes):
            ids, files, matrix = self.fingerprints(model)
            self._prints = (model, version, self._sketch_writes, ids, files, matrix)
        return self._prints[3], self._prints[4], self._prints[5]

    def search_meaning(self, question, model: str, limit: int = 100, offset: int = 0,
                       filters: Optional[SearchFilters] = None, candidates: Optional[int] = None,
                       sort="rank", descending: bool = False) -> SearchPage:
        """Files whose text is closest in meaning to `question` (a sketch of it, as float32 numbers, from the
        same model). Two stages: the fingerprints of every piece are scanned for the `candidates` likeliest
        FILES (sketches.CANDIDATES; a few pieces each), then those are scored by their full sketches. One row per file (one per
        content, unless filters.every_copy), its closest passage as Evidence, closest first. `rank` is
        the closeness (1 is the same meaning). Only the file filters apply (folder, kinds, dates, sizes),
        and they narrow the scan itself, before the candidates are chosen."""
        import numpy as np
        from . import sketches
        keys = sort_keys(sort, descending, SORT_COLUMNS)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        ids, files, prints = self._fingerprints_cached(model)
        if len(ids) == 0:
            return SearchPage([], 0, offset, limit)
        _, _, _, file_clause, file_args, _ = self._narrow("", filters)
        keep = candidates if candidates is not None else sketches.CANDIDATES
        if file_clause:
            # the filters narrow the scan itself, so files outside them take no place among the candidates
            allowed = np.fromiter((r[0] for r in self.conn.execute(f"SELECT f.id FROM files f WHERE 1=1{file_clause}", file_args)),
                                  dtype=np.int64)
            inside = np.flatnonzero(np.isin(files, allowed))
            if len(inside) == 0:
                return SearchPage([], 0, offset, limit)
            picked = sketches.scan_files(prints[inside], files[inside], np.asarray(question, dtype=np.float32), keep)
            chosen = ids[inside[picked]]
        else:
            chosen = ids[sketches.scan_files(prints, files, np.asarray(question, dtype=np.float32), keep)]
        found = self.sketches_of(chosen)
        if not found:
            return SearchPage([], 0, offset, limit)
        # the full sketches, scored; then the closest piece of each file
        seg_ids = list(found)
        dims = found[seg_ids[0]][1]
        data = b"".join(found[i][3] for i in seg_ids)
        full = sketches.dequantize(data, dims, [found[i][2] for i in seg_ids])
        scores = sketches.closeness(full, np.asarray(question, dtype=np.float32)[:dims])
        best: Dict[int, Tuple[float, int]] = {}
        for seg, score in zip(seg_ids, scores):
            file_id = found[seg][0]
            if file_id not in best or score > best[file_id][0]:
                best[file_id] = (float(score), seg)
        # the files themselves, filtered; copies grouped as a word search groups them
        grouped = not (filters is not None and filters.every_copy)
        rows_by_id: Dict[int, sqlite3.Row] = {}
        file_ids = list(best)
        for at in range(0, len(file_ids), 400):
            chunk = file_ids[at:at + 400]
            marks = ",".join("?" * len(chunk))
            sql = (f"SELECT f.*, ROW_NUMBER() OVER (PARTITION BY COALESCE(f.content, '#' || f.id) ORDER BY "
                   f"(f.missing_since IS NOT NULL), {self._elsewhere_sql()}, {_BACKUP_SQL}, {_DEPTH_SQL}, length(f.path), f.id) AS place "
                   f"FROM files f WHERE f.id IN ({marks}){file_clause}")
            for r in self.conn.execute(sql, chunk + list(file_args)):
                rows_by_id[r["id"]] = r
        members: Dict[object, List[int]] = {}
        for file_id in best:
            r = rows_by_id.get(file_id)
            if r is not None:
                members.setdefault((r["content"] or f"#{file_id}") if grouped else f"#{file_id}", []).append(file_id)
        groups: List[Tuple[float, int, int]] = []       # (score, segment, shown file): the best-placed copy is shown
        for files in members.values():
            shown = min(files, key=lambda i: rows_by_id[i]["place"])
            groups.append((max(best[i][0] for i in files), best[shown][1], shown))
        ordered = sorted(groups, key=lambda g: (-g[0], g[2]))
        if keys != [("rank", False)]:
            def value(item, key):
                r = rows_by_id[item[2]]
                if key == "rank":
                    return -item[0]          # a word search's rank is "smaller is better"; so is this when sorted by it
                if key == "matches":
                    return 1
                v = r[key]
                return v.casefold() if isinstance(v, str) else v
            ordered = _order_in_python(ordered, value, keys)
        page = ordered[offset:offset + limit]
        rows: List[SearchRow] = []
        for score, seg, file_id in page:
            r = rows_by_id[file_id]
            evidence = self.conn.execute("SELECT page FROM segments WHERE id=?", (seg,)).fetchone()
            rows.append(SearchRow(r["id"], decode_path(r["path"], r["path_raw"]), r["name"], r["folder"], r["size"],
                                  r["modified"], r["kind"], r["state"], 1, score,
                                  (Evidence(file_id, evidence["page"] if evidence else 0, 0, 0, None, seg),),
                                  missing_since=r["missing_since"]))
        if grouped and rows:
            others = self.copies_of([row.file_id for row in rows])
            for row in rows:
                row.also_at = tuple(others.get(row.file_id, ()))
        return SearchPage(rows, len(ordered), offset, limit)

    def word_shares(self, words: Sequence[str]) -> Dict[str, float]:
        """For each word, the share of the index's pieces of text (names aside) that contain it, 0 to 1, as
        the index itself counts them (stemmed and folded, so "paid" and "pay" are one word). A phrase, or a
        word the index has never seen, is 0."""
        found: Dict[str, float] = {}
        pieces = self.conn.execute("SELECT COUNT(*) FROM segments WHERE kind != 'name'").fetchone()[0]
        if not pieces:
            return {w: 0.0 for w in words}
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.stems USING fts5(w, tokenize=\"" + TOKENIZER + "\")")
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.stems_vocab USING fts5vocab('temp', 'stems', 'row')")
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_vocab USING fts5vocab('main', 'fts', 'col')")
        for word in words:
            if " " in word.strip() or not word.strip():
                found[word] = 0.0
                continue
            self.conn.execute("DELETE FROM temp.stems")
            self.conn.execute("INSERT INTO temp.stems(w) VALUES (?)", (word,))
            stems = [r[0] for r in self.conn.execute("SELECT term FROM temp.stems_vocab")]
            if len(stems) != 1:            # the tokenizer made no word of it, or several: not one word
                found[word] = 0.0
                continue
            row = self.conn.execute("SELECT doc FROM temp.fts_vocab WHERE term=? AND col='body'", (stems[0],)).fetchone()
            found[word] = min(1.0, (row[0] if row else 0) / pieces)
        return found

    def closeness_of_files(self, question, model: str, file_ids: Sequence[int]) -> Dict[int, Tuple[float, int]]:
        """For each of these files, how close its closest piece is to the question: file id -> (closeness,
        segment id). A file with no sketch by this model is left out."""
        import numpy as np
        from . import sketches
        found: Dict[int, Tuple[float, int]] = {}
        ids = [int(i) for i in file_ids]
        asked = np.asarray(question, dtype=np.float32)
        for at in range(0, len(ids), 400):
            chunk = ids[at:at + 400]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(f"SELECT segment_id, file_id, dims, scale, sketch FROM sketches "
                                     f"WHERE model=? AND file_id IN ({marks})", [model] + chunk).fetchall()
            if not rows:
                continue
            dims = rows[0][2]
            full = sketches.dequantize(b"".join(r[4] for r in rows), dims, [r[3] for r in rows])
            scores = sketches.closeness(full, asked[:dims])
            for r, score in zip(rows, scores):
                held = found.get(r[1])
                if held is None or score > held[0]:
                    found[r[1]] = (float(score), r[0])
        return found

    def telling_words(self, query: str, everywhere: float = 0.3) -> List[str]:
        """The terms of a question that are worth looking for: phrases typed in quotes always, and words that
        fewer than `everywhere` of the index's pieces contain. "I", "a" and "the" go; so does any word the
        index holds nearly everywhere. 0 or less keeps every word."""
        terms = parse_query(query)
        if everywhere <= 0 or self.conn.execute("SELECT COUNT(*) FROM segments WHERE kind != 'name'").fetchone()[0] < EVERYWHERE_MIN_PIECES:
            return terms          # a small index has no "everywhere" worth the name: every word counts
        shares = self.word_shares([t for t in terms if " " not in t])
        return [t for t in terms if " " in t or shares.get(t, 0.0) < everywhere]

    def search_both(self, query: str, question, model: str, limit: int = 100, offset: int = 0,
                    filters: Optional[SearchFilters] = None, word_weight: float = 0.5, word_pool: int = 2000,
                    everywhere: float = 0.3, sort="rank", descending: bool = False) -> SearchPage:
        """The words and the meaning together. The question's telling words (telling_words: not the ones
        found nearly everywhere) are looked for, any of them, and the best `word_pool` such files and the
        likeliest files by meaning are put on one footing: each file's closeness in meaning (0 to 1; its own
        sketches are scored even when the meaning stage did not pick it) plus `word_weight` times its share
        of the best word score (the best word match counts 1, a file with no word match 0). One row per
        file, closest first; `rank` is that sum; the word evidence first, then the closest passage by meaning.
        A question with no telling words is meaning alone. `sort` other than rank orders the files found as
        a word search would (newest first, say), the score still deciding which are found."""
        keys = sort_keys(sort, descending, SORT_COLUMNS)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        word_filters = replace(filters, where=WHERE_ANYWHERE) if filters is not None else None
        telling = self.telling_words(query, everywhere)
        if telling:
            words = self.search_words(" ".join(_quoted(t) if " " in t else t for t in telling), max(1, word_pool), 0,
                                      "rank", False, word_filters, match=" OR ".join(_quoted(t) for t in telling))
        else:
            words = SearchPage([], 0, 0, 0)
        by_meaning = self.search_meaning(question, model, sketches_module().CANDIDATES, 0, filters)
        meaning_rows = {r.file_id: r for r in by_meaning.rows}
        word_rows = {r.file_id: r for r in words.rows}
        best_word = min((r.rank for r in words.rows), default=0.0)        # bm25 ranks are negative: lower is better
        closeness = self.closeness_of_files(question, model, [i for i in word_rows if i not in meaning_rows])
        scored: List[Tuple[float, int]] = []
        for file_id in set(word_rows) | set(meaning_rows):
            if file_id in meaning_rows:
                near = float(meaning_rows[file_id].rank)
            else:
                near = max(0.0, closeness.get(file_id, (0.0, 0))[0])
            share = (word_rows[file_id].rank / best_word) if file_id in word_rows and best_word < 0 else 0.0
            scored.append((near + word_weight * max(0.0, min(1.0, share)), file_id))
        scored.sort(key=lambda t: (-t[0], t[1]))
        if keys != [("rank", False)]:
            def value(item, key):
                base = word_rows.get(item[1]) or meaning_rows[item[1]]
                if key == "rank":
                    return -item[0]
                if key == "matches":
                    return base.matches if item[1] in word_rows else 0
                v = getattr(base, key)
                return v.casefold() if isinstance(v, str) else v
            scored = _order_in_python(scored, value, keys)
        rows: List[SearchRow] = []
        for score, file_id in scored[offset:offset + limit]:
            base = word_rows.get(file_id) or meaning_rows[file_id]
            evidence = list(word_rows[file_id].evidence) if file_id in word_rows else []
            if file_id in meaning_rows:
                extra = meaning_rows[file_id].evidence
            else:
                seg = closeness.get(file_id, (0.0, 0))[1]
                page = self.conn.execute("SELECT page FROM segments WHERE id=?", (seg,)).fetchone() if seg else None
                extra = (Evidence(file_id, page["page"] if page else 0, 0, 0, None, seg),) if seg else ()
            evidence.extend(e for e in extra if e.segment_id not in {w.segment_id for w in evidence})
            rows.append(replace(base, matches=word_rows[file_id].matches if file_id in word_rows else 0, rank=score,
                                evidence=tuple(evidence)))
        return SearchPage(rows, len(scored), offset, limit)

    def sample_text(self, count: int) -> List[Tuple[int, str, int, str]]:
        """`count` pieces of text drawn at random: (segment id, file path, page, text). For measuring."""
        rows = self.conn.execute(
            "SELECT s.id, f.path, f.path_raw, s.page, s.text, s.z FROM segments s JOIN files f ON f.id = s.file_id "
            "WHERE s.kind != 'name' AND f.missing_since IS NULL ORDER BY random() LIMIT ?", (max(0, count),))
        return [(r["id"], decode_path(r["path"], r["path_raw"]), r["page"], unpack_text(r["text"], r["z"])) for r in rows]

    # -- pictures by likeness (looks.py): one sketch a picture; the store compares nothing itself

    def unlooked(self, model: str, limit: int = 200, after: Optional[Tuple[float, int]] = None) -> List[FileRecord]:
        """Picture files with no look by this model yet, the most recently changed first; `after` (modified,
        id) continues past the last one seen."""
        clauses, args = ["f.looked <> ?", "f.missing_since IS NULL", "f.kind = ?"], [model, FileKind.IMAGE.value]
        if after is not None:
            clauses.append("(f.modified < ? OR (f.modified = ? AND f.id > ?))")
            args.extend([after[0], after[0], after[1]])
        sql = f"SELECT f.* FROM files f WHERE {' AND '.join(clauses)} ORDER BY f.modified DESC, f.id LIMIT ?"
        return [self._record(r) for r in self.conn.execute(sql, args + [max(0, limit)])]

    def save_looks(self, model: str, rows: Sequence[Tuple[int, float, Optional[bytes], Optional[bytes], str]]) -> None:
        """Keep (file id, scale, sketch bytes, fingerprint bytes, note) for pictures; a note with no sketch
        says why the picture could not be read. The files are marked as looked at by this model."""
        self._looks_writes += 1
        with self.transaction():
            self.conn.executemany(
                "INSERT OR REPLACE INTO looks(file_id, model, scale, sketch, fingerprint, note) VALUES (?,?,?,?,?,?)",
                [(int(f), model, float(scale), sketch, fingerprint, note or "") for f, scale, sketch, fingerprint, note in rows])
            self.conn.executemany("UPDATE files SET looked=? WHERE id=?", [(model, int(f)) for f, *_ in rows])

    def look_counts(self, model: str) -> Tuple[int, int, int]:
        """(pictures looked at by this model, pictures there could be, of the looked-at ones those that could
        not be read), among files there at the last scan."""
        done = self.conn.execute("SELECT COUNT(*) FROM files WHERE looked=? AND missing_since IS NULL", (model,)).fetchone()[0]
        could = self.picture_count()
        unread = self.conn.execute("SELECT COUNT(*) FROM looks k JOIN files f ON f.id = k.file_id "
                                   "WHERE k.model=? AND k.sketch IS NULL AND f.missing_since IS NULL", (model,)).fetchone()[0]
        return int(done), int(could), int(unread)

    def unread_pictures(self, model: str, limit: int = 1000) -> List[Tuple[str, str]]:
        """(path, why) for pictures this model could not read."""
        rows = self.conn.execute("SELECT f.path, f.path_raw, k.note FROM looks k JOIN files f ON f.id = k.file_id "
                                 "WHERE k.model=? AND k.sketch IS NULL AND f.missing_since IS NULL ORDER BY f.path LIMIT ?",
                                 (model, max(0, limit)))
        return [(decode_path(r["path"], r["path_raw"]), r["note"]) for r in rows]

    def look_fingerprints(self, model: str):
        """Every picture fingerprint by this model, for files there at the last scan: (file ids, int8 matrix)."""
        import numpy as np
        from .sketches import FINGERPRINT
        rows = self.conn.execute(
            "SELECT k.file_id, k.fingerprint FROM looks k JOIN files f ON f.id = k.file_id "
            "WHERE k.model=? AND k.fingerprint IS NOT NULL AND f.missing_since IS NULL AND f.looked = k.model "
            "ORDER BY k.file_id", (model,)).fetchall()       # f.looked: a changed picture's old sketch is not evidence
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8).reshape(len(rows), FINGERPRINT) if rows \
            else np.zeros((0, FINGERPRINT), dtype=np.int8)
        return ids, matrix

    def looks_of(self, file_ids: Sequence[int]) -> Dict[int, Tuple[float, bytes]]:
        """file id -> (scale, sketch bytes) for these pictures (those that have a sketch)."""
        found: Dict[int, Tuple[float, bytes]] = {}
        ids = [int(i) for i in file_ids]
        for at in range(0, len(ids), 400):
            chunk = ids[at:at + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT k.file_id, k.scale, k.sketch FROM looks k JOIN files f ON f.id = k.file_id "
                                       f"WHERE k.sketch IS NOT NULL AND f.looked = k.model AND k.file_id IN ({marks})", chunk):
                found[r[0]] = (r[1], r[2])
        return found

    def _look_fingerprints_cached(self, model: str):
        version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
        if self._look_prints is None or self._look_prints[:3] != (model, version, self._looks_writes):
            ids, matrix = self.look_fingerprints(model)
            self._look_prints = (model, version, self._looks_writes, ids, matrix)
        return self._look_prints[3], self._look_prints[4]

    def search_looks(self, question, model: str, limit: int = 100, offset: int = 0,
                     filters: Optional[SearchFilters] = None, candidates: Optional[int] = None,
                     sort="rank", descending: bool = False, not_file: Optional[int] = None) -> SearchPage:
        """Pictures closest to `question` (a sketch from the picture model: of a sentence, or of a picture),
        closest first, one row a picture, `rank` the closeness. Two stages as for text. Only the file
        filters apply, and they narrow the scan. `not_file` leaves one picture out (the example itself)."""
        import numpy as np
        from . import sketches
        keys = sort_keys(sort, descending, SORT_COLUMNS)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        ids, prints = self._look_fingerprints_cached(model)
        if len(ids) == 0:
            return SearchPage([], 0, offset, limit)
        _, _, _, file_clause, file_args, _ = self._narrow("", filters)
        keep = candidates if candidates is not None else sketches.CANDIDATES
        asked = np.asarray(question, dtype=np.float32)
        inside = np.arange(len(ids))
        if file_clause:
            allowed = np.fromiter((r[0] for r in self.conn.execute(f"SELECT f.id FROM files f WHERE 1=1{file_clause}", file_args)),
                                  dtype=np.int64)
            inside = np.flatnonzero(np.isin(ids, allowed))
        if not_file is not None:
            inside = inside[ids[inside] != not_file]
        if len(inside) == 0:
            return SearchPage([], 0, offset, limit)
        picked = sketches.scan(prints[inside], asked, keep)
        chosen = [int(i) for i in ids[inside[picked]]]
        full = self.looks_of(chosen)
        order = [i for i in chosen if i in full]
        if not order:
            return SearchPage([], 0, offset, limit)
        vectors = sketches.dequantize(b"".join(full[i][1] for i in order), len(asked), [full[i][0] for i in order])
        scores = sketches.closeness(vectors, asked)
        scored = sorted(zip((float(x) for x in scores), order), key=lambda t: (-t[0], t[1]))
        rows_by_id: Dict[int, sqlite3.Row] = {}
        for at in range(0, len(order), 400):
            chunk = order[at:at + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT f.* FROM files f WHERE f.id IN ({marks})", chunk):
                rows_by_id[r["id"]] = r
        if keys != [("rank", False)]:
            def value(item, key):
                r = rows_by_id[item[1]]
                if key == "rank":
                    return -item[0]
                if key == "matches":
                    return 1
                v = r[key]
                return v.casefold() if isinstance(v, str) else v
            scored = _order_in_python(scored, value, keys)
        rows = [SearchRow(r["id"], decode_path(r["path"], r["path_raw"]), r["name"], r["folder"], r["size"], r["modified"],
                          r["kind"], r["state"], 1, score, (), missing_since=r["missing_since"])
                for score, file_id in scored[offset:offset + limit] for r in [rows_by_id[file_id]]]
        return SearchPage(rows, len(scored), offset, limit)

    def look_of(self, file_id: int):
        """The sketch of one picture as float32 numbers, or None when it has none."""
        from . import sketches
        found = self.looks_of([file_id])
        if file_id not in found:
            return None
        from .embedding import LOOKS_DIMS
        return sketches.dequantize(found[file_id][1], LOOKS_DIMS, [found[file_id][0]])[0]

    def sample_pictures(self, count: int) -> List[Tuple[int, str]]:
        """`count` picture files drawn at random, there at the last scan: (file id, path). For measuring."""
        rows = self.conn.execute("SELECT id, path, path_raw FROM files WHERE kind=? AND missing_since IS NULL ORDER BY random() LIMIT ?",
                                 (FileKind.IMAGE.value, max(0, count)))
        return [(r["id"], decode_path(r["path"], r["path_raw"])) for r in rows]

    def picture_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files WHERE kind=? AND missing_since IS NULL", (FileKind.IMAGE.value,)).fetchone()[0]

    def segment_count(self, include_names: bool = False) -> int:
        clause = "" if include_names else "WHERE kind != 'name'"
        return self.conn.execute(f"SELECT COUNT(*) FROM segments {clause}").fetchone()[0]

    def state_counts(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None) -> Dict[str, int]:
        """Files per state. A missing file is counted under "missing", not under the state it had."""
        where, args = "", []
        if prefix is not None:
            clause, args = _under_clause(prefix, case_insensitive)
            where = f"WHERE {clause}"
        sql = ("SELECT CASE WHEN missing_since IS NOT NULL THEN 'missing' ELSE state END AS s, COUNT(*) "
               f"FROM files {where} GROUP BY s")
        return {r[0]: r[1] for r in self.conn.execute(sql, args)}

    # -- search
    #
    # Every query below reads "FROM fts CROSS JOIN segments". In SQLite, CROSS JOIN fixes the order: the
    # full-text table is walked by its MATCH and each segment is then looked up by id. Left to itself the
    # planner may start from `segments` instead (it did on SQLite 3.37 once a file filter was added), and
    # then highlight() fails with "unable to use function in the requested context". For the same reason
    # scores come from the `rank` column, never from bm25() (which also fails inside GROUP BY on 3.37).

    @staticmethod
    def _narrow(match: str, filters: Optional[SearchFilters], query: str = ""
                ) -> Tuple[str, str, List[object], str, List[object], Tuple[str, ...]]:
        """(the match text, a clause on the segment and its values, a clause on the file and its values,
        the words that must stand in exactly that form, or () when any form will do)."""
        if filters is None:
            return match, "", [], "", [], ()
        seg = ""
        seg_args: List[object] = []
        terms: Tuple[str, ...] = ()
        if filters.where == WHERE_NAME:
            match, seg = f"name : ({match})", " AND s.kind = 'name'"
        elif filters.where == WHERE_TEXT:
            match = f"body : ({match})"
        elif filters.where == WHERE_OCR:
            match, seg = f"body : ({match})", " AND s.kind = 'ocr'"
        if filters.exact:
            terms = exact_terms(query)
            place = {WHERE_NAME: "name", WHERE_TEXT: "body", WHERE_OCR: "body"}.get(filters.where, "either")
            seg += " AND dugup_exact(fts.name, fts.body, ?, ?) = 1"
            seg_args = [place, json.dumps([list(p) for p in exact_phrases(query)])]
        clauses: List[str] = []
        args: List[object] = []
        if filters.folder is not None:
            clause, more = _under_clause(filters.folder, filters.case_insensitive, "f.path")
            clauses.append(clause)
            args.extend(more)
        if filters.kinds:
            clauses.append(f"f.kind IN ({','.join('?' * len(filters.kinds))})")
            args.extend(filters.kinds)
        for column, sign, value in (("f.modified", ">=", filters.modified_after), ("f.modified", "<", filters.modified_before),
                                    ("f.size", ">=", filters.min_size), ("f.size", "<=", filters.max_size)):
            if value is not None:
                clauses.append(f"{column} {sign} ?")
                args.append(value)
        if filters.present_only:
            clauses.append("f.missing_since IS NULL")
            for folder in filters.away:
                clause, more = _under_clause(folder, filters.case_insensitive, "f.path")
                clauses.append(f"NOT ({clause})")
                args.extend(more)
        return match, seg, seg_args, "".join(f" AND {c}" for c in clauses), args, terms

    def search_words(self, query: str, limit: int = 100, offset: int = 0,
                     sort="rank", descending: bool = False, filters: Optional[SearchFilters] = None,
                     match: Optional[str] = None) -> SearchPage:
        """`match`: an FTS5 match to use instead of the one built from `query` (search_both's any-of-these)."""
        keys = sort_keys(sort, descending, SORT_COLUMNS)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        match = match if match is not None else build_match(query)
        if match is None:
            return SearchPage([], 0, offset, limit)
        match, seg_clause, seg_args, file_clause, file_args, terms = self._narrow(match, filters, query)
        grouped = not (filters is not None and filters.every_copy)
        counted = f"COALESCE(f.content, '#' || f.id)" if grouped else "s.file_id"
        total = self.conn.execute(
            f"SELECT COUNT(DISTINCT {counted}) FROM fts CROSS JOIN segments s ON s.id = fts.rowid "
            f"CROSS JOIN files f ON f.id = s.file_id WHERE fts MATCH ?{seg_clause}{file_clause}",
            [match] + seg_args + file_args,
        ).fetchone()[0]
        # Best match: files where the words stand as a phrase come first, then files where they stand close
        # together, then the rest; the index's own score decides inside each of the three.
        close = closeness_matches(query, self.near_words) if any(key == "rank" for key, _ in keys) else None
        tier, tier_args = "0", []
        if close is not None:
            tier = ("MIN(CASE WHEN s.id IN (SELECT rowid FROM fts WHERE fts MATCH ?) THEN 0 "
                    "WHEN s.id IN (SELECT rowid FROM fts WHERE fts MATCH ?) THEN 1 ELSE 2 END)")
            tier_args = list(close)

        def by(key: str, down: bool) -> str:
            way = "DESC" if down else "ASC"
            return f"tier {way}, rank {way}" if key == "rank" else f"{SORT_COLUMNS[key]} {way}"

        order = ", ".join(by(key, down) for key, down in keys)
        # The score is read from FTS5's `rank` column (bm25 by default). Unlike the bm25() function it is an
        # ordinary column value, so it can be grouped and sorted on every SQLite version.
        per_file = (
            "SELECT f.id, f.path, f.path_raw, f.name, f.folder, f.size, f.modified, f.kind, f.state, "
            f"f.missing_since, f.content, COUNT(*) AS matches, MIN(fts.rank) AS rank, {tier} AS tier "
            "FROM fts CROSS JOIN segments s ON s.id = fts.rowid CROSS JOIN files f ON f.id = s.file_id "
            f"WHERE fts MATCH ?{seg_clause}{file_clause} GROUP BY f.id"
        )
        if grouped:
            # Of the matching files with the same content, one is shown: one that is still there, not in a folder
            # named like a backup, highest up in the folders, with the shortest path. (The inner query is named
            # f so that the sort columns, written for the files table, read the same out here.)
            sql = (f"SELECT * FROM (SELECT f.*, ROW_NUMBER() OVER (PARTITION BY COALESCE(f.content, '#' || f.id) ORDER BY "
                   f"(f.missing_since IS NOT NULL), {self._elsewhere_sql()}, {_BACKUP_SQL}, {_DEPTH_SQL}, length(f.path), f.id) AS place "
                   f"FROM ({per_file}) AS f) AS f WHERE place = 1 ORDER BY {order}, f.id ASC LIMIT ? OFFSET ?")
        else:
            sql = f"{per_file} ORDER BY {order}, f.id ASC LIMIT ? OFFSET ?"
        rows = [
            SearchRow(r["id"], decode_path(r["path"], r["path_raw"]), r["name"], r["folder"], r["size"],
                      r["modified"], r["kind"], r["state"], r["matches"], r["rank"],
                      missing_since=r["missing_since"])
            for r in self.conn.execute(sql, tier_args + [match] + seg_args + file_args + [limit, offset])
        ]
        self._attach_evidence(rows, match, seg_clause, seg_args, terms)
        if grouped and rows:
            others = self.copies_of([row.file_id for row in rows])
            for row in rows:
                row.also_at = tuple(others.get(row.file_id, ()))
        return SearchPage(rows, total, offset, limit)

    def _elsewhere_sql(self) -> str:
        """0 for a path below the preferred folder, 1 for any other; 0 for all when none is preferred."""
        prefix, _ = encode_path(self.prefer_under) if self.prefer_under else ("", None)
        if not prefix:
            return "0"
        quoted = prefix.lower().replace("'", "''")
        return f"(lower(substr(f.path, 1, {len(prefix)})) <> '{quoted}')"

    @staticmethod
    def _span(r: sqlite3.Row, is_name: bool, terms: Sequence[str]) -> Optional[Tuple[int, int]]:
        """Where the words stand in a matched row. For an exact-form search the marks the index makes are
        not used: they also mark other forms of a word."""
        if terms:
            return _exact_span(r["raw_name"] if is_name else r["raw_body"], terms)
        return _first_span(r["in_name"] if is_name else r["in_body"])

    def _attach_evidence(self, rows: List[SearchRow], match: str, seg_clause: str = "",
                         seg_args: Sequence[object] = (), terms: Sequence[str] = ()) -> None:
        """Give each row the places its words were found: text first, then the file name, best first."""
        found: Dict[int, List[Tuple[int, float, Evidence]]] = {}
        ids = [r.file_id for r in rows]
        for at in range(0, len(ids), 500):
            chunk = ids[at:at + 500]
            marks = ",".join("?" * len(chunk))
            sql = ("SELECT s.id, s.file_id, s.page, s.kind, fts.rank AS score, fts.name AS raw_name, fts.body AS raw_body, "
                   "highlight(fts, 0, char(2), char(3)) AS in_name, highlight(fts, 1, char(2), char(3)) AS in_body "
                   f"FROM fts CROSS JOIN segments s ON s.id = fts.rowid WHERE fts MATCH ?{seg_clause} AND s.file_id IN ({marks})")
            for r in self.conn.execute(sql, [match] + list(seg_args) + chunk):
                is_name = r["kind"] == SegmentKind.NAME.value
                span = self._span(r, is_name, terms)
                if span is None:
                    continue   # a piece of text that matched only through the file name: the name row covers it
                box = self._line_box(r["id"], span[0]) if r["kind"] == SegmentKind.OCR.value else None
                evidence = Evidence(r["file_id"], r["page"], span[0], span[1], box, r["id"])
                found.setdefault(r["file_id"], []).append((1 if is_name else 0, r["score"], evidence))
        for row in rows:
            hits = sorted(found.get(row.file_id, ()), key=lambda h: (h[0], h[1], h[2].page, h[2].segment_id))
            if not hits:   # the words are spread over the name and the text: point at the best text
                hits = self._fallback_evidence(row.file_id, match)
            row.evidence = tuple(h[2] for h in hits[:EVIDENCE_PER_ROW])

    def file_evidence(self, file_id: int, query: str, filters: Optional[SearchFilters] = None,
                      limit: int = 2000) -> List[Evidence]:
        match = build_match(query)
        if match is None or limit <= 0:
            return []
        match, seg_clause, seg_args, _, _, terms = self._narrow(match, filters, query)
        sql = ("SELECT s.id, s.page, s.kind, fts.name AS raw_name, fts.body AS raw_body, "
               "highlight(fts, 0, char(2), char(3)) AS in_name, "
               "highlight(fts, 1, char(2), char(3)) AS in_body "
               f"FROM fts CROSS JOIN segments s ON s.id = fts.rowid WHERE fts MATCH ?{seg_clause} AND s.file_id = ? "
               "ORDER BY (s.kind = 'name'), s.page, s.ord, s.id")
        found: List[Evidence] = []
        for r in self.conn.execute(sql, [match] + seg_args + [file_id]):
            is_name = r["kind"] == SegmentKind.NAME.value
            span = self._span(r, is_name, terms)
            if span is None:
                continue      # a piece of text that matched only through the file name
            box = self._line_box(r["id"], span[0]) if r["kind"] == SegmentKind.OCR.value else None
            found.append(Evidence(file_id, r["page"], span[0], span[1], box, r["id"]))
            if len(found) >= limit:
                break
        return found

    def _fallback_evidence(self, file_id: int, match: str) -> List[Tuple[int, float, Evidence]]:
        sql = ("SELECT s.id, s.page, s.kind, s.text FROM fts CROSS JOIN segments s ON s.id = fts.rowid "
               "WHERE fts MATCH ? AND s.file_id = ? ORDER BY fts.rank LIMIT 1")
        r = self.conn.execute(sql, (match, file_id)).fetchone()
        if r is None:
            return []
        return [(0, 0.0, Evidence(file_id, r["page"], 0, 0, None, r["id"]))]

    def get_passage(self, evidence: Evidence, context: int = 80) -> Optional[Passage]:
        """The text around a piece of Evidence (`context` characters each side), or None if it is gone."""
        if evidence.segment_id is None:
            return None
        r = self.conn.execute(
            "SELECT s.kind, s.page, s.text, s.z, f.name FROM segments s JOIN files f ON f.id = s.file_id "
            "WHERE s.id=? AND s.file_id=?", (evidence.segment_id, evidence.file_id)).fetchone()
        if r is None:
            return None
        kind = SegmentKind(r["kind"])
        text = _clean(r["name"]) if kind is SegmentKind.NAME else unpack_text(r["text"], r["z"])
        start = min(max(evidence.start, 0), len(text))
        end = min(max(evidence.end, start), len(text))
        left = max(0, start - max(context, 0))
        right = min(len(text), end + max(context, 0))
        if start == end == 0:      # nothing in particular to mark (a passage found by meaning): the room goes to the text
            right = min(len(text), 2 * max(context, 0))
        return Passage(text[left:right], start - left, end - left, r["page"], kind)

    # -- roots

    @staticmethod
    def _root(row: sqlite3.Row) -> Root:
        return Root(decode_path(row["path"], row["path_raw"]), row["policy"], tuple(json.loads(row["include"])),
                    tuple(json.loads(row["exclude"])), bool(row["reachable"]), row["last_scan"],
                    tuple(json.loads(row["warnings"])), tuple((str(f), str(l)) for f, l in json.loads(row["levels"])))

    def list_roots(self) -> List[Root]:
        return [self._root(r) for r in self.conn.execute("SELECT * FROM roots ORDER BY path")]

    def set_root(self, root: Root) -> None:
        """Add a root or change its policy and rules. Its scan status (reachable, last scan) is kept."""
        text, raw = encode_path(root.path)
        dump = lambda items: json.dumps(list(items), ensure_ascii=False)
        with self.transaction():
            self.conn.execute(
                "INSERT INTO roots(path, path_raw, policy, include, exclude, levels) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET policy=excluded.policy, include=excluded.include, "
                "exclude=excluded.exclude, levels=excluded.levels",
                (text, raw, root.policy, dump(root.include), dump(root.exclude), dump(list(pair) for pair in root.levels)))

    def note_scan(self, path: str, reachable: bool, when: float, warnings: Sequence[str] = ()) -> None:
        text, _ = encode_path(path)
        with self.transaction():
            cur = self.conn.execute("UPDATE roots SET reachable=?, last_scan=?, warnings=? WHERE path=?",
                                    (int(reachable), when, json.dumps(list(warnings)), text))
            if cur.rowcount == 0:
                raise StoreError("that folder is not one of the roots")

    def remove_root(self, path: str) -> bool:
        text, _ = encode_path(path)
        with self.transaction():
            return self.conn.execute("DELETE FROM roots WHERE path=?", (text,)).rowcount > 0

    def reclaimable_fraction(self) -> float:
        pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
        free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
        return free / pages if pages > 0 else 0.0

    def check_word_index(self) -> bool:
        """Whether the word index agrees with the stored text (a self-test; slow on a large index)."""
        try:
            try:
                self.conn.execute("INSERT INTO fts(fts, rank) VALUES('integrity-check', 1)")
            except sqlite3.OperationalError:      # an older SQLite: the plain form does the same comparison
                self.conn.execute("INSERT INTO fts(fts) VALUES('integrity-check')")
            return True
        except sqlite3.DatabaseError:
            return False

    def compact(self) -> None:
        """Rebuild the file without the free space that deleting and rewriting leave inside it. SQLite writes
        a full copy first, so this needs about as much free disk as the index is large."""
        if self._depth:
            raise StoreError("compacting cannot run inside a transaction")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
        self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- purge

    def purge_folder(self, prefix: str, case_insensitive: Optional[bool] = None, thorough: bool = False) -> int:
        """Remove every file at or below `prefix`, with its text, regions and annotations.

        Whole path parts only: purging /data/a never touches /data/ab.
        `thorough` also merges the full-text index and rebuilds the file so the
        removed text is not left behind in free space (slower; best effort).
        """
        clause, args = _under_clause(prefix, case_insensitive)
        with self.transaction():
            ids = [r[0] for r in self.conn.execute(f"SELECT id FROM files WHERE {clause}", args)]
            for row_id in ids:
                self._delete_segments(row_id, keep_name=False)
                self.conn.execute("DELETE FROM files WHERE id=?", (row_id,))
        if ids:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if thorough:
                self.conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
                self.conn.execute("VACUUM")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(ids)
