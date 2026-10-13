"""Files read side by side when the index is updated: the same result as one at a time, a slow file that does
not hold up the rest, and a stop that loses nothing."""

import threading

import pytest

from dugup.indexer import REFUSE, Indexer
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason, FileState
from dugup.store import SqliteStore

from fakes.fake_store import FakeStore
from test_indexer import DiskReader, World


class Side(World):
    """A World whose indexer has more readers. `make` builds each one."""

    def __init__(self, store, readers=4, make=None, settings=None):
        super().__init__(store, settings)
        make = make or (lambda disk: DiskReader(disk))
        self.reader = make(self.h.disk)
        self.more = [make(self.h.disk) for _ in range(readers - 1)]

    def indexer(self, settings=None):
        from test_indexer import Clock
        return Indexer(self.store, self.h.source, self.reader, settings or self.settings, self.seen.append, self.clock,
                       timer=Clock(0.0), extra_extractors=self.more)

    def calls(self):
        return sorted(path for reader in (self.reader, *self.more) for path in reader.calls)


@pytest.fixture(params=["fake", "sqlite"])
def store(request, tmp_path):
    if request.param == "fake":
        yield FakeStore()
    else:
        with SqliteStore(tmp_path / "index.sqlite") as made:
            yield made


def fill(world, count=40):
    for i in range(count):
        world.put(f"d{i % 4}/f{i}.txt", f"alpha {i}" if i % 2 else f"beta {i}")
    world.put("locked.txt", "!password")
    world.put("bug.txt", "!crash")
    world.put("nothing.txt", "!none")
    world.put("scan.txt", "!ocr")
    world.put("cut.txt", "!cut gamma")


def test_side_by_side_ends_exactly_where_one_at_a_time_does(store, tmp_path):
    alone = World(FakeStore())
    fill(alone)
    one = alone.run()
    side = Side(store)
    fill(side)
    report = side.run()
    assert (report.read, report.done, report.partial, report.failed, report.skipped) == \
        (one.read, one.done, one.partial, one.failed, one.skipped)
    strip = lambda world: {path[len(world.root):]: rest for path, rest in world.picture().items()}
    assert strip(side) == strip(alone)
    assert len(side.calls()) == 45 and len(set(side.calls())) == 45, "every file read once, by one reader or another"
    assert sum(1 for reader in (side.reader, *side.more) if reader.calls) > 1, "more than one reader did the work"
    assert "pending" not in dict(side.store.state_counts())


def test_a_slow_file_does_not_hold_up_the_rest(store):
    """The slow file is held until every other file has been read, so this can only pass side by side."""
    others_read = threading.Event()
    seen = []
    lock = threading.Lock()

    class Held(DiskReader):
        def extract(self, record, stop=None):
            if record.path.endswith("slow.txt"):
                if not others_read.wait(30):
                    return Failure(FailureReason.OTHER, "the other files were not read while this one was held")
                return super().extract(record, stop)
            result = super().extract(record, stop)
            with lock:
                seen.append(record.path)
                if len(seen) == 12:
                    others_read.set()
            return result

    side = Side(store, readers=3, make=Held)
    side.put("a/slow.txt", "delta slow")          # early in the queue
    for i in range(12):
        side.put(f"b/f{i}.txt", f"alpha {i}")
    report = side.run()
    assert report.done == 13 and report.failed == 0 and side.found("delta") == ["a/slow.txt"]


def test_a_stop_keeps_what_was_read_and_leaves_the_rest_waiting(store):
    stop = threading.Event()
    lock = threading.Lock()
    count = [0]

    class Stopping(DiskReader):
        def extract(self, record, stop_signal=None):
            result = super().extract(record, stop_signal)
            with lock:
                count[0] += 1
                if count[0] == 10:
                    stop.set()
            return result

    side = Side(store, readers=4, make=Stopping)
    for i in range(60):
        side.put(f"f{i:02d}.txt", f"alpha {i}")
    report = side.run(stop)
    states = dict(side.store.state_counts())
    assert report.stopped and states.get("done", 0) >= 10 and states.get("pending", 0) > 0
    assert states.get("done", 0) + states.get("pending", 0) == 60 and states.get("done", 0) == len(side.found("alpha"))
    stop.clear()
    again = side.run(stop)
    assert not again.stopped and dict(side.store.state_counts()) == {"done": 60}, "the next run finishes the job"


def test_a_file_out_of_time_gets_its_second_try_after_all_the_first_tries(store):
    order = []
    lock = threading.Lock()

    class Timed(DiskReader):
        def extract(self, record, stop=None):
            with lock:
                order.append(("first", record.path.rsplit("/", 1)[-1]))
            if record.path.endswith("big.txt"):
                return Failure(FailureReason.TIMED_OUT, "out of time")
            return super().extract(record, stop)

        def extract_patiently(self, record, stop=None, patience=2.0):
            with lock:
                order.append(("second", record.path.rsplit("/", 1)[-1]))
            return DiskReader.extract(self, record, stop)

    side = Side(store, readers=3, make=Timed)
    side.put("big.txt", "gamma big")
    for i in range(9):
        side.put(f"f{i}.txt", f"alpha {i}")
    report = side.run()
    assert report.done == 10 and report.second_tries == 1 and side.found("gamma") == ["big.txt"]
    assert order[-1] == ("second", "big.txt") and sum(1 for kind, _ in order if kind == "first") == 10


def test_trouble_with_the_readers_still_pauses_the_run(store):
    class Broken(DiskReader):
        def extract(self, record, stop=None):
            return Failure(FailureReason.CRASHED, "the worker died")

    side = Side(store, readers=3, make=Broken, settings=Settings(reader_failures_in_a_row=5))
    for i in range(40):
        side.put(f"f{i:02d}.txt", "alpha")
    report = side.run()
    assert "in a row" in report.paused and dict(side.store.state_counts()).get("pending", 0) > 0
