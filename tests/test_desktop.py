"""Opening files with the desktop: the choice made on each platform, with the system calls replaced."""

import helpers
from dugup.desktop import GONE, Desktop


class Calls:
    def __init__(self, fail_edit=False, fail_open=False):
        self.started = []
        self.ran = []
        self.fail_edit = fail_edit
        self.fail_open = fail_open

    def startfile(self, path, action="open"):
        if (action == "edit" and self.fail_edit) or (action == "open" and self.fail_open):
            raise OSError(1155, "No application is associated with the specified file for this operation")
        self.started.append((path, action))

    def run(self, command):
        self.ran.append(command)


def desk(platform, calls, present=("xdg-open", "open"), existing=None):
    return Desktop(platform, calls.startfile if platform.startswith("win") else None, calls.run,
                   which=lambda tool: tool if tool in present else None,
                   exists=(lambda p: True) if existing is None else (lambda p: p in existing))


WIN = f"C:\\Users\\{helpers.MUELLER}\\My Files, 2026\\{helpers.CJK} report.pdf"
NIX = f"/home/me/My Files/{helpers.CAFE} {helpers.CJK}.pdf"


def test_windows_opens_with_the_shell_and_edits_when_the_type_has_an_edit_action():
    calls = Calls()
    d = desk("win32", calls)
    assert d.open_file(WIN) == "" and d.edit_file(WIN) == ""
    assert calls.started == [(WIN, "open"), (WIN, "edit")] and calls.ran == []


def test_windows_edit_falls_back_to_open_and_a_refusal_is_said_plainly():
    calls = Calls(fail_edit=True)
    assert desk("win32", calls).edit_file(WIN) == "" and calls.started == [(WIN, "open")]
    refused = desk("win32", Calls(fail_open=True)).open_file(WIN)
    assert refused.startswith("Windows could not open the file") and "No application" in refused


def test_windows_shows_the_file_selected_and_keeps_awkward_paths_whole():
    calls = Calls()
    assert desk("win32", calls).show_in_folder(WIN) == ""
    assert calls.ran == [f'explorer /select,"{WIN}"'], "one string: explorer reads the path up to the closing quote"


def test_linux_uses_xdg_open_for_the_file_and_for_its_folder():
    calls = Calls()
    d = desk("linux", calls)
    assert d.open_file(NIX) == "" and d.edit_file(NIX) == "" and d.show_in_folder(NIX) == ""
    assert calls.ran == [["xdg-open", NIX], ["xdg-open", NIX], ["xdg-open", "/home/me/My Files"]]


def test_mac_reveals_the_file():
    calls = Calls()
    d = desk("darwin", calls)
    d.open_file(NIX)
    d.show_in_folder(NIX)
    assert calls.ran == [["open", NIX], ["open", "-R", NIX]]


def test_a_missing_tool_is_named():
    said = desk("linux", Calls(), present=()).open_file(NIX)
    assert "xdg-open" in said and "not found" in said


def test_a_tool_that_cannot_start_is_a_sentence_not_an_exception():
    def broken(command):
        raise OSError(13, "Permission denied")

    d = Desktop("linux", None, broken, which=lambda tool: tool, exists=lambda p: True)
    assert "Permission denied" in d.open_file(NIX)
    w = Desktop("win32", lambda *a: None, broken, which=lambda tool: tool, exists=lambda p: True)
    assert "Permission denied" in w.show_in_folder(WIN)


def test_a_file_that_is_gone_is_said_and_its_folder_is_still_shown():
    calls = Calls()
    d = desk("win32", calls, existing={"C:\\docs"})
    assert d.open_file("C:\\docs\\gone.pdf") == GONE and d.edit_file("C:\\docs\\gone.pdf") == GONE
    assert calls.started == []
    assert d.show_in_folder("C:\\docs\\gone.pdf") == "" and calls.ran == ['explorer "C:\\docs"']
    nix = desk("linux", Calls(), existing={"/docs"})
    assert nix.show_in_folder("/docs/gone.pdf") == ""
    assert "Neither" in desk("linux", Calls(), existing=set()).show_in_folder("/docs/gone.pdf")
