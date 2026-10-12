import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dugup import store as store_module
from dugup.shapes import FileKind, FileRecord, FileState, SegmentIn, SegmentKind
from dugup.store import SqliteStore, StoreError, StoreVersionError
from helpers import CJK, CAFE, GREEK

EXPECTED_TABLES = {"meta", "files", "segments", "regions", "unread_bands", "annotations", "fts"}


def _tables(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
    finally:
        conn.close()


def test_new_store_has_every_table_and_the_current_version(store, store_path):
    assert EXPECTED_TABLES <= _tables(store_path)
    assert store.schema_version() == len(store_module.MIGRATIONS)


def test_connection_settings_are_the_safe_ones(store):
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_reopening_is_harmless_and_keeps_data(store_path):
    with SqliteStore(store_path) as first:
        first.upsert_file(FileRecord(path="/docs/a.txt", size=1, modified=1.0, kind=FileKind.TEXT))
    with SqliteStore(store_path) as second:
        assert second.file_count() == 1
        assert second.schema_version() == len(store_module.MIGRATIONS)


def test_the_folder_for_the_index_must_exist(tmp_path):
    with pytest.raises(StoreError, match="does not exist"):
        SqliteStore(tmp_path / "nope" / "index.sqlite")


def test_an_index_from_a_newer_program_is_refused_with_both_versions(store_path):
    SqliteStore(store_path).close()
    conn = sqlite3.connect(str(store_path))
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(StoreVersionError) as caught:
        SqliteStore(store_path)
    assert "99" in str(caught.value) and str(len(store_module.MIGRATIONS)) in str(caught.value)


def test_an_older_index_is_migrated_forward(store_path, monkeypatch):
    SqliteStore(store_path).close()

    def add_notes(conn):
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")

    before = len(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS + [add_notes])
    with SqliteStore(store_path) as upgraded:
        assert upgraded.schema_version() == before + 1
        assert "notes" in _tables(store_path)


def test_a_version_1_index_gains_the_phase_1_tables_and_keeps_its_data(store_path, monkeypatch):
    everything = list(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", everything[:1])
    with SqliteStore(store_path) as old:   # the version 1 tables, filled the way version 1 filled them
        assert old.schema_version() == 1
        old.conn.execute("INSERT INTO files(id, path, name, folder, size, modified, kind, state) "
                         "VALUES(1, '/docs/kept.txt', 'kept.txt', '/docs', 1, 1.0, 'text', 'done')")
        old.conn.execute("INSERT INTO segments(id, file_id, page, ord, kind, text) VALUES(1, 1, 0, 0, 'name', '')")
        old.conn.execute("INSERT INTO segments(id, file_id, page, ord, kind, text) VALUES(2, 1, 1, 0, 'text', 'keptword')")
        old.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(1, 'kept.txt', '')")
        old.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(2, 'kept.txt', 'keptword')")
    monkeypatch.setattr(store_module, "MIGRATIONS", everything)
    with SqliteStore(store_path) as new:
        assert new.schema_version() == len(everything)
        assert {"pages", "roots"} <= _tables(store_path)
        assert [r.path for r in new.search_words("keptword").rows] == ["/docs/kept.txt"]
        assert new.get_file("/docs/kept.txt").missing_since is None
        assert list(new.list_roots()) == []


def test_a_failing_migration_changes_nothing(store_path, monkeypatch):
    SqliteStore(store_path).close()

    def half_done(conn):
        conn.execute("CREATE TABLE half (id INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS + [half_done])
    with pytest.raises(RuntimeError, match="boom"):
        SqliteStore(store_path)
    assert "half" not in _tables(store_path)
    conn = sqlite3.connect(str(store_path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store_module.MIGRATIONS) - 1
    conn.close()


def test_sqlite_without_fts5_is_refused_with_a_reason(store_path, monkeypatch):
    monkeypatch.setattr(store_module, "fts5_available", lambda conn: False)
    with pytest.raises(StoreError, match="FTS5"):
        SqliteStore(store_path)


def test_closed_store_says_so(store_path):
    opened = SqliteStore(store_path)
    opened.close()
    opened.close()  # closing twice is harmless
    with pytest.raises(StoreError, match="closed"):
        opened.file_count()


# ---- fingerprint ---------------------------------------------------------------------

def test_fingerprint_starts_empty_and_round_trips_with_non_ascii(store):
    assert store.get_fingerprint() is None
    fingerprint = {"ocr.rapidocr": "3.10", "language": "en", "note": CAFE + GREEK + CJK, "chunk": 1800}
    store.set_fingerprint(fingerprint)
    assert store.get_fingerprint() == fingerprint


def test_fingerprint_survives_reopening(store_path):
    with SqliteStore(store_path) as first:
        first.set_fingerprint({"embedder": "m1"})
    with SqliteStore(store_path) as second:
        assert second.get_fingerprint() == {"embedder": "m1"}


def test_identical_fingerprint_reports_no_change(store):
    store.set_fingerprint({"a": 1, "b": "x"})
    assert store.changed_fingerprint_keys({"a": 1, "b": "x"}) == []


def test_changed_added_and_removed_keys_are_all_reported_sorted(store):
    store.set_fingerprint({"ocr.rapidocr": "3.10", "embedder": "m1", "gone": 1})
    changed = store.changed_fingerprint_keys({"ocr.rapidocr": "3.11", "embedder": "m1", "new": True})
    assert changed == ["gone", "new", "ocr.rapidocr"]


def test_a_none_to_missing_difference_is_not_hidden(store):
    store.set_fingerprint({"a": None})
    assert store.changed_fingerprint_keys({}) == ["a"]


def test_fingerprint_with_non_json_values_is_refused(store):
    with pytest.raises(ValueError):
        store.set_fingerprint({"bad": object()})


def test_no_stored_fingerprint_means_everything_is_new(store):
    assert store.changed_fingerprint_keys({"a": 1, "b": 2}) == ["a", "b"]


# ---- transactions and crashes ---------------------------------------------------------

def test_a_failed_transaction_rolls_everything_back(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert_file(FileRecord(path="/docs/a.txt", size=1, modified=1.0, kind=FileKind.TEXT))
            raise RuntimeError("stop")
    assert store.file_count() == 0


def test_nested_transactions_roll_back_only_the_inner_part(store):
    with store.transaction():
        store.upsert_file(FileRecord(path="/docs/outer.txt", size=1, modified=1.0, kind=FileKind.TEXT))
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.upsert_file(FileRecord(path="/docs/inner.txt", size=1, modified=1.0, kind=FileKind.TEXT))
                raise RuntimeError("inner failed")
    assert store.get_file("/docs/outer.txt") is not None
    assert store.get_file("/docs/inner.txt") is None


def test_work_not_committed_before_a_hard_crash_vanishes_and_the_file_stays_sound(store_path):
    SqliteStore(store_path).close()
    src = str(Path(__file__).resolve().parent.parent / "src")
    code = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {src!r})
        from dugup.store import SqliteStore
        from dugup.shapes import FileRecord, FileKind
        s = SqliteStore({str(store_path)!r})
        s.upsert_file(FileRecord(path="/docs/committed.txt", size=1, modified=1.0, kind=FileKind.TEXT))
        s.conn.execute("BEGIN IMMEDIATE")
        s.conn.execute("INSERT INTO files(path, name, folder, size, modified, kind, state) "
                       "VALUES('/docs/half.txt','half.txt','/docs',1,1.0,'text','pending')")
        os._exit(7)  # hard stop in the middle of a write: no commit, no clean close
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 7, done.stderr
    with SqliteStore(store_path) as reopened:
        assert reopened.get_file("/docs/committed.txt") is not None
        assert reopened.get_file("/docs/half.txt") is None
        assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
