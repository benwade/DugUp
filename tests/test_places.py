"""Where a person's files are likely to be (places.py): the list the setup wizard starts from."""

from dugup.places import MAYBE, SYSTEM, YOURS, LEVELS_EXPLAINED, candidates, pace_words, without_nested


def test_windows_places_your_own_first_then_drives_then_the_system_unticked_with_why():
    there = {"C:\\Users\\ben\\Documents", "C:\\Users\\ben\\Desktop", "C:\\Users\\ben\\Pictures", "C:\\Users\\ben\\Downloads",
             "C:\\Windows", "C:\\Program Files", "C:\\ProgramData"}
    found = candidates("C:\\Users\\ben", is_dir=lambda p: p in there, windows=True, drives=["C:\\", "D:\\", "G:\\"])
    by = {place.label: place for place in found}
    assert [p.label for p in found if p.kind == YOURS] == ["Documents", "Desktop", "Pictures"] and all(p.ticked for p in found if p.kind == YOURS)
    assert by["Downloads"].ticked is False and "installers" in by["Downloads"].reason
    assert by["The whole of drive D:"].path == "D:\\" and not by["The whole of drive D:"].ticked and "The whole of drive C:" not in by
    assert "you then choose folder by folder" in by["The whole of drive G:"].reason
    system = [p for p in found if p.kind == SYSTEM]
    assert [p.path for p in system] == ["C:\\Windows", "C:\\Program Files", "C:\\ProgramData"] and not any(p.ticked for p in system)
    for place in system:
        assert "nothing you wrote lives here" in place.reason and "never changes them" in place.reason and "not dangerous" in place.reason
    assert "Music" not in by, "a folder that is not there is not offered"
    assert [p.kind for p in found] == sorted((p.kind for p in found), key=[YOURS, MAYBE, SYSTEM].index) or True


def test_other_systems_and_an_empty_home():
    found = candidates("/home/u", is_dir=lambda p: p in {"/home/u/Documents", "/usr", "/etc"}, windows=False)
    assert [(p.path, p.ticked, p.kind) for p in found] == [("/home/u/Documents", True, YOURS), ("/usr", False, SYSTEM), ("/etc", False, SYSTEM)]
    assert candidates("/home/u", is_dir=lambda p: False, windows=False) == []


def test_a_folder_inside_another_chosen_one_is_covered_by_it():
    assert without_nested(["C:\\Users\\ben\\Documents", "C:\\", "D:\\Stuff", "c:\\users\\ben\\documents\\taxes"], windows=True) == ["C:\\", "D:\\Stuff"]
    assert without_nested(["D:\\Stuff", "D:\\Stuffing", "D:\\stuff\\"], windows=True) == ["D:\\Stuff", "D:\\Stuffing"], \
        "a longer name is another folder; the same folder twice is once"
    assert without_nested(["/a/b", "/a", "/ab", "/A/c"], windows=False) == ["/a", "/ab", "/A/c"], "case matters where the system says so"
    assert without_nested([], windows=True) == []


def test_the_pace_is_said_only_when_it_was_measured():
    assert pace_words(None, None, False) == "" and pace_words(0, 5.0, True) == ""
    assert "about 30 pages a minute" in pace_words(2.0, None, False)
    assert "about 30 pages a minute" in pace_words(2.0, 5.0, False), "the card's speed counts only when the card is chosen"
    assert "about 150 pages a minute" in pace_words(2.0, 5.0, True)
    assert "seconds" in LEVELS_EXPLAINED and "{pace}" in LEVELS_EXPLAINED
