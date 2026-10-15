"""The index keeping itself current while the tray runs."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from dugup.shapes import Failure, FailureReason
from dugup.watcher import Watcher


class Service:
    """Records the runs it is asked for; each answers when the test lets it (never slower than the test)."""

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.answer = {}
        self.waiting = {"meaning": 3, "looks": 2}

    def _run(self, step, stop, **kw):
        self.calls.append((step, kw))
        self.gate.wait(5)
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped")
        return self.answer.get(step, SimpleNamespace(read=4, files=3, pictures=2))

    def run_indexing(self, stop, progress, readers=None, **kw):
        return self._run("index", stop, readers=readers)

    def run_ocr(self, stop, progress, fast=None, **kw):
        return self._run("ocr", stop, fast=fast)

    def run_meaning(self, stop, progress, **kw):
        return self._run("meaning", stop)

    def run_looks(self, stop, progress, readers=None, **kw):
        return self._run("looks", stop, readers=readers)

    def meaning_waiting(self):
        return self.waiting["meaning"]

    def looks_waiting(self):
        return self.waiting["looks"]


def settings(**kw):
    base = dict(watch_every_minutes=60.0, ocr_after_index=True, meaning_after_index=True, looks_after_index=True,
                watch_on_battery=False, watch_readers=0)
    base.update(kw)
    return SimpleNamespace(**base)


def finish(watcher):
    assert watcher.thread is not None
    watcher.thread.join(5)
    assert not watcher.running


def test_a_run_is_due_at_once_then_every_period_and_goes_through_the_steps_gently():
    now = [1000.0]
    service, said = Service(), []
    w = Watcher(service, settings(), clock=lambda: now[0], cores=16, say=said.append, on_battery=lambda: False)
    assert w.state() == "no update yet"
    assert w.tick(), "the first tick starts a run"
    finish(w)
    assert [c[0] for c in service.calls] == ["index", "ocr", "meaning", "looks"]
    assert service.calls[0][1] == {"readers": 4} and service.calls[1][1] == {"fast": False} and service.calls[3][1] == {"readers": 4}
    assert w.last_outcome == "index 4; ocr 4; meaning 3; looks 2" and said == ["Background update: index 4; ocr 4; meaning 3; looks 2"]
    now[0] += 30 * 60
    assert not w.tick() and w.state().startswith("last update 30 min ago")
    now[0] += 30 * 60
    assert w.tick()
    finish(w)
    assert len(service.calls) == 8


def test_steps_with_nothing_waiting_or_switched_off_are_skipped():
    service = Service()
    service.waiting = {"meaning": 0, "looks": 0}
    w = Watcher(service, settings(ocr_after_index=False), cores=4)
    w.tick()
    finish(w)
    assert [c[0] for c in service.calls] == ["index"] and w.last_outcome == "index 4"


def test_pause_now_stop_and_a_busy_lock():
    now = [0.0]
    service = Service()
    w = Watcher(service, settings(), clock=lambda: now[0], cores=8)
    w.pause()
    assert not w.tick() and w.state() == "updates paused"
    w.now()
    assert w.tick(), "'Update now' runs even while paused"
    finish(w)
    w.resume()
    # a run in progress can be stopped; the rest of the steps are not taken
    service.gate.clear()
    now[0] += 3600
    assert w.tick()
    for _ in range(50):
        if service.calls and service.calls[-1][0] == "index" and len(service.calls) == 5:
            break
        w.thread.join(0.02)
    assert w.running and w.state() == "updating (index)"
    w.stop()
    service.gate.set()
    finish(w)
    assert w.last_outcome == "stopped" and [c[0] for c in service.calls][4:] == ["index"]
    # another program holds the lock: wait for the next time, say so
    service.answer["index"] = Failure(FailureReason.BUSY, "another program is indexing")
    now[0] += 3600
    w.tick()
    finish(w)
    assert "another run was going" in w.last_outcome and [c[0] for c in service.calls][5:] == ["index"]


def test_off_when_the_period_is_zero():
    w = Watcher(Service(), settings(watch_every_minutes=0), cores=2)
    assert not w.tick() and "off" in w.state()
    w.now()
    assert w.tick(), "but Update now still works"
    finish(w)


def test_on_battery_the_update_waits_for_mains_unless_told_otherwise_and_update_now_still_works():
    power = {"battery": True}
    w = Watcher(Service(), settings(), cores=8, on_battery=lambda: power["battery"])
    assert not w.tick() and w.state() == "on battery: updates wait for mains power"
    w.now()
    assert w.tick()
    finish(w)
    power["battery"] = False
    w2 = Watcher(Service(), settings(), cores=8, on_battery=lambda: True)
    assert not w2.tick()
    w3 = Watcher(Service(), settings(watch_on_battery=True), cores=8, on_battery=lambda: True)
    assert w3.tick()
    finish(w3)
    w4 = Watcher(Service(), settings(), cores=8, on_battery=lambda: None)
    assert w4.tick(), "a machine that cannot say is treated as on mains"
    finish(w4)


def test_gentle_readers_never_take_every_core():
    assert Watcher(Service(), settings(), cores=16).gentle_readers() == 4
    assert Watcher(Service(), settings(), cores=2).gentle_readers() == 1
    assert Watcher(Service(), settings(), cores=1).gentle_readers() == 1
    assert Watcher(Service(), settings(), cores=6).gentle_readers() == 1
    assert Watcher(Service(), settings(watch_readers=3), cores=2).gentle_readers() == 3, "the setting is obeyed as given"

