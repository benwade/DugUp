"""Commands that open a desktop surface: window, tray, remote, popup."""

from __future__ import annotations

import argparse

from .common import _service, _layout
from .output import safe_write


def cmd_window(args, env, out, environ) -> int:
    try:
        import tkinter
    except ImportError:
        safe_write("The window needs tkinter, which this Python does not have. On Windows it comes with Python "
                   "(tick 'tcl/tk' in the installer); on Linux install your system's python3-tk package.", out)
        return 1
    from ..window import App
    try:
        try:
            root = tkinter.Tk()
        except tkinter.TclError:
            root = tkinter.Tk()      # starting tkinter fails now and then on Windows while it reads its own files
    except tkinter.TclError as exc:
        first_line = (str(exc).splitlines() or ["no reason given"])[0]
        safe_write(f"A window cannot be opened here ({first_line}). The other commands work without one.", out)
        return 1
    try:
        from ..identity import dress
        dress(root)
        root.withdraw()                          # the notes first, on their own, before anything opens the index
        try:
            from ..startnotes import before_window
            before_window(root, args, environ)
        finally:
            root.deiconify()
        service = _service(args, environ, out, create=True)
    except BaseException:
        root.destroy()
        raise
    app = App(root, service)
    if getattr(args, "search", None):
        app.words.set(args.search)
        root.after(0, app.search)
    root.mainloop()
    return 0


def move_waits_for(args, environ) -> bool:
    """True when an older index waits to be moved beside the program and this tray would open that very index:
    started plainly, or by its login entry, which names the older folder with --data-dir."""
    import os
    from ..datadir import beside_program
    beside = beside_program(None, environ)
    if beside is None or beside.move_from is None:
        return False
    if not args.data_dir:
        return True
    return os.path.normcase(os.path.abspath(args.data_dir)) == os.path.normcase(os.path.abspath(beside.move_from))


def cmd_tray(args, env, out, environ) -> int:
    from .. import summons, tray as tray_module
    if args.startup == "refresh":                   # an upgrade: an entry there is made again, in today's form;
        # before the data folder is looked up, which would make it: installing makes no data folder
        path = tray_module.refresh_startup()
        safe_write(f"The start-at-login entry was made again ({path})." if path else
                   "Nothing to remake: no start-at-login entry, one already in today's form, or one for another copy of DugUp.", out)
        return 0
    layout = _layout(args, environ, create=True)
    if args.startup == "add":
        path = tray_module.install_startup(layout)
        safe_write(f"The tray will start when you log in (the entry is {path}). Start it now with: dugup tray", out)
        return 0
    if args.startup == "remove":
        path = tray_module.remove_startup()
        safe_write("The tray will no longer start when you log in." if path else "There was no start-at-login entry.", out)
        return 0
    if args.quit:
        answer = summons.summon(layout.root, "quit")
        safe_write("The tray was told to quit." if answer else "No tray is running.", out)
        return 0
    if summons.summon(layout.root, "ping") == "pong":
        summons.summon(layout.root, "popup")
        safe_write("DugUp is already in the tray; its search box was brought up.", out)
        return 0
    try:
        import tkinter
    except ImportError:
        safe_write("The tray needs tkinter, which this Python does not have.", out)
        return 1
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        safe_write(f"A window cannot be opened here ({(str(exc).splitlines() or ['no reason'])[0]}), so there is no tray.", out)
        return 1
    from ..identity import dress
    dress(root)                                  # the popup and every window the tray opens are its children
    root.withdraw()                              # the tray never shows its root; the question shows on its own
    try:
        if move_waits_for(args, environ):
            from ..startnotes import MOVE, before_window
            asked = argparse.Namespace(**{**vars(args), "data_dir": None})
            if before_window(root, asked, environ, is_elevated=lambda: False) == MOVE:
                args.data_dir = None             # the data is beside the program now; the login entry says so too
    except BaseException:
        root.destroy()
        raise
    try:
        service = _service(args, environ, out, create=True)
    except BaseException:
        root.destroy()
        raise
    tray = tray_module.Tray(root, service, service.settings, make_icon=(lambda t: None) if args.no_icon else None,
                            say=lambda text: safe_write(text, out))
    for line in tray.status_lines():
        safe_write(line, out)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        tray.quit()
        try:
            root.destroy()
        except Exception:
            pass
    return 0


def cmd_report(args, env, out, environ) -> int:
    """Report a problem: a window of its own (or the text, with --print), working while other windows are frozen."""
    from .. import report
    from ..datadir import DataDirError
    try:
        data_dir: "str | None" = str(_layout(args, environ, create=False).root)
    except DataDirError:
        data_dir = None
    if args.print:
        safe_write(report.gather(data_dir), out)
        return 0
    try:
        import tkinter
        root = tkinter.Tk()
    except Exception:
        safe_write("A window cannot be opened here; the report follows. Copy it into a new issue at "
                   + report.ISSUES, out)
        safe_write(report.gather(data_dir), out)
        return 0
    from ..identity import dress
    from ..reportwindow import ReportWindow
    dress(root)
    ReportWindow(root, data_dir)
    root.mainloop()
    return 0


def cmd_remote(args, env, out, environ) -> int:
    from .. import remote as remote_module
    layout = _layout(args, environ, create=True)
    try:
        import tkinter
    except ImportError:
        safe_write("The remote needs tkinter, which this Python does not have.", out)
        return 1
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        safe_write(f"A window cannot be opened here ({(str(exc).splitlines() or ['no reason'])[0]}).", out)
        return 1
    from ..identity import dress
    dress(root)
    window = remote_module.Remote(root, layout, say=lambda text: safe_write(text, out))
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        window.close()
    return 0


def cmd_popup(args, env, out, environ) -> int:
    from .. import summons
    layout = _layout(args, environ, create=False)
    if summons.summon(layout.root, "popup"):
        return 0
    safe_write("No tray is running, so there is no search box to bring up. Start it with: dugup tray", out)
    return 1
