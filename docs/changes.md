# Recent changes

Release notes and performance improvements for DugUp.

[Back to the README](../README.md) · [User guide](user-guide.md) · [Technical information](technical.md)

## 0.2.6 beta — October 2026

**Leave a folder out.** **Leave a folder out...** on the **Index** tab, or **Leave this folder out of the index...** on a search result's right-click menu, keeps a folder and everything in it out of the index. DugUp says how many files will leave the index and asks once; your files are not touched. **Index → Put a folder back** undoes it. See [Leave a folder out](user-guide.md#leave-a-folder-out).

**Closing the window keeps DugUp in the tray.** The tray icon or the search key brings DugUp back, without the Start menu. The first close explains where DugUp went and how to quit completely (**File → Quit DugUp**, or **Quit** on the tray icon). A setting turns this off.

**Every Index tab button is visible.** The buttons now take three rows, so **Stop**, **Refresh** and **Settings** no longer fall off the edge of the pane.

## 0.2.5 beta — October 2026

**DugUp is named DugUp everywhere in Windows.** The Start menu, Startup apps, the taskbar and Task Manager showed "Python", because DugUp ran as a Python program. DugUp now has its own program file, `DugUp.exe`, and every shortcut starts it. The start-at-login entry is now a shortcut called **DugUp tray**; installing this version replaces the old one for you.

**A new icon.** DugUp's windows, shortcuts, tray and program file share a new icon: a magnifying glass over a folder being dug up.

**Rename in one click.** **Rename as shown** renames at once and reports in the status line, with no question first and no window after. A window appears only when a picture could not be renamed. **Undo** works the same way.

## 0.2.4 beta — October 2026

Thanks to an early tester who showed us where DugUp was hard to use.

**Picture search is easy to find.** **Search by** sits under the search box with four choices: **Words**, **Meaning**, **Words + meaning** and **Pictures**. When a search by words finds nothing and your pictures are ready, DugUp offers **Search pictures instead**. See [Search for a file](user-guide.md#search-for-a-file).

**Plain words understands picture requests.** “Pictures of dogs” or “beach photos” now sets up a search for what the pictures show.

**Pictures are prepared for you.** **Update the index** now finishes by preparing new pictures for picture search, the same as the tray. The **Index** tab says how many pictures are waiting, and **Prepare picture search** does it on request, asking before it downloads the picture model.

**Settings are shorter.** Settings opens on the ten choices most people change, plus anything you have changed. **Show all settings (advanced)** shows the rest, and the search box always finds every setting.

**More files are read.** Saved e-mails (`.eml`) are read: subject, people, date, message and attachment names. Older Office files (`.doc`, `.xls`, `.ppt`, `.rtf`) are read when the free LibreOffice is installed; install it later and the next update reads them. See [File types](technical.md#file-types).

**The search form fits the window.** At the usual window size, some filters and **Clear filters** were off the right edge. The form now uses an extra line so everything is visible.

## 0.2.3 beta — October 2026

**Report a problem, even when DugUp is stuck.** A new report window gathers the versions, health check, recent log and any freeze notes, hides folder and file names behind numbered stand-ins, shows you everything, and lets you open a GitHub issue, copy, or save it. It runs as a program of its own and is one step away in the Start menu, the search box, the tray menu, DugUp Remote and the main window. A freeze recorder in each window notes where a window got stuck. See [Report a problem](user-guide.md#report-a-problem).

**Changing a file type now updates the index.** When you change the `[extensions]` table in `config.toml`, the next index update reads again just the files whose type changed. See [File types](technical.md#file-types).

## 0.2.2 beta — October 2026

**Claude can search your files for you, when you allow it.** Cloud search access is off unless you turn it on. When it is on, Claude can search the index read-only, in the folders you choose, and read short passages around what it finds. It can never change, move or delete anything.

- **One switch, where you look:** **Allow cloud search access** is in the search box, the tray menu, the **File** menu and **DugUp Remote**. Turning it on explains what leaves your computer and asks first; turning it off takes effect immediately.
- **Your choice of folders:** a folder window lists the folders Claude may search, remembering where you last browsed.
- **Connected in one step:** DugUp offers to add itself to the Claude desktop app's settings, keeping everything else.
- **Command-line control:** `dugup mcp enable`, `folders`, `connect`, `status` and `disable`.

What Claude is shown, file names, paths and matching passages, goes to Anthropic as part of the conversation. See [Let Claude search for you](user-guide.md#let-claude-search-for-you).

## 0.2.1 beta — October 2026

The installer now displays its welcome page, including notices about the unencrypted index and the software's warranty terms. Those notices also appear in the README and About box.

Source checkouts include `DugUp Remote.cmd` for opening the remote controls without a console.

## 0.2.0 beta — October 2026

This release introduces the Windows installer and makes it easier to choose where DugUp keeps its index.

- Program and data share one folder by default, with the option to keep the index elsewhere.
- Existing indexes can be moved to the new location or kept where they are.
- Optional downloads show their sizes during setup.
- DugUp warns when it detects a data folder inside a cloud-synced location.
- **DugUp Remote** is available from the Start menu.

The October work also includes the search improvements below.

## Faster meaning and picture searches — October 2026

The initial comparison stage now processes vectors in small blocks using faster numerical routines. This avoids creating a large temporary copy of the vectors.

On a test index of one million passages, the first comparison fell from **1.35 seconds to 0.12 seconds**; later comparisons fell from **0.25 seconds to 0.06 seconds**, with the same results. These are measurements of that stage on the test setup, not end-to-end search times or a prediction for every computer.

Picture search uses the same initial comparison stage and benefits from the change too.

## A responsive search window — October 2026

Search now runs on a dedicated worker, so the window can still accept typing, movement and stop requests while waiting for results.

Previously, a slow search could occupy the interface for several seconds. The worker now uses its own SQLite read connection, retains only the newest waiting request, and discards answers to searches you have replaced. Repeated **More** requests while loading do not add the same rows twice.

This change improves responsiveness rather than the underlying search time. See [Architecture](technical.md#architecture) for the implementation details.
