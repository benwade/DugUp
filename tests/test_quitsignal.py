"""Quit all of DugUp (quitsignal.py): one program asks, every program on the same data folder closes."""

from dugup import quitsignal


def test_a_quit_asked_after_a_program_started_reaches_it_and_not_one_started_later(tmp_path):
    clock = [100.0]
    early = quitsignal.Listener(tmp_path, now=lambda: clock[0])
    assert not any(early.due() for _ in range(quitsignal.EVERY * 2)), "nothing asked yet"
    clock[0] = 200.0
    quitsignal.send(tmp_path, now=lambda: clock[0])
    assert any(early.due() for _ in range(quitsignal.EVERY)), "seen within one round of looks"
    clock[0] = 300.0
    late = quitsignal.Listener(tmp_path, now=lambda: clock[0])
    assert not any(late.due() for _ in range(quitsignal.EVERY * 2)), "an older quit does not close a new program"


def test_a_damaged_or_missing_signal_is_no_quit(tmp_path):
    assert not quitsignal.asked_since(tmp_path, 0)
    (tmp_path / quitsignal.FILE).write_text("not a time", encoding="utf-8")
    assert not quitsignal.asked_since(tmp_path, 0)
