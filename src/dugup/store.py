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
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .protocols import OCR_DONE, OCR_FAILED, OCR_WAITING, OcrResult, OcrTask, PageInfo, PageVerdict
from .shapes import (WHERE_ANYWHERE, WHERE_NAME, WHERE_OCR, WHERE_TEXT, SearchFilters, sort_keys, Box, Evidence, FileKind, FileRecord, FileState, Passage, Root, SearchPage, SearchRow, SegmentIn,
                     SegmentKind)

__all__ = ["SqliteStore", "StoreError", "StoreVersionError", "SearchPage", "SearchRow", "SORT_COLUMNS",
           "MIGRATIONS", "TOKENIZER", "build_match", "encode_path", "decode_path", "fts5_available"]

TOKENIZER = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
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


MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [_migration_1, _migration_2, _migration_3, _migration_4,
                                                          _migration_5, _migration_6]
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

class SqliteStore:
    def __init__(self, path: os.PathLike | str, near_words: int = 15, compress_text: bool = True) -> None:
        self.path = Path(path)
        self.compress_text = compress_text     # for text stored from now on; text already stored is read either way
        self.near_words = near_words     # how close words must stand to rank above the same words far apart
        if not self.path.parent.is_dir():
            raise StoreError(f"the folder for the index does not exist: {self.path.parent}")
        self._depth = 0
        self._conn: Optional[sqlite3.Connection] = None
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
                    "state=?, error=?, policy=? WHERE id=?",
                    values + (row_id,),
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
        with self.transaction():
            name_row = self.conn.execute("SELECT name FROM files WHERE id=?", (row_id,)).fetchone()
            if name_row is None:
                raise StoreError(f"no file with id {row_id}")
            self._delete_segments(row_id, keep_name=True)
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
        return match, seg, seg_args, "".join(f" AND {c}" for c in clauses), args, terms

    def search_words(self, query: str, limit: int = 100, offset: int = 0,
                     sort="rank", descending: bool = False, filters: Optional[SearchFilters] = None) -> SearchPage:
        keys = sort_keys(sort, descending, SORT_COLUMNS)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        match = build_match(query)
        if match is None:
            return SearchPage([], 0, offset, limit)
        match, seg_clause, seg_args, file_clause, file_args, terms = self._narrow(match, filters, query)
        total = self.conn.execute(
            "SELECT COUNT(DISTINCT s.file_id) FROM fts CROSS JOIN segments s ON s.id = fts.rowid "
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
        sql = (
            "SELECT f.id, f.path, f.path_raw, f.name, f.folder, f.size, f.modified, f.kind, f.state, "
            f"f.missing_since, COUNT(*) AS matches, MIN(fts.rank) AS rank, {tier} AS tier "
            "FROM fts CROSS JOIN segments s ON s.id = fts.rowid CROSS JOIN files f ON f.id = s.file_id "
            f"WHERE fts MATCH ?{seg_clause}{file_clause} GROUP BY f.id "
            f"ORDER BY {order}, f.id ASC LIMIT ? OFFSET ?"
        )
        rows = [
            SearchRow(r["id"], decode_path(r["path"], r["path_raw"]), r["name"], r["folder"], r["size"],
                      r["modified"], r["kind"], r["state"], r["matches"], r["rank"],
                      missing_since=r["missing_since"])
            for r in self.conn.execute(sql, tier_args + [match] + seg_args + file_args + [limit, offset])
        ]
        self._attach_evidence(rows, match, seg_clause, seg_args, terms)
        return SearchPage(rows, total, offset, limit)

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
