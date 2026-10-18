# Recent changes

Release notes and performance improvements for DugUp.

[Back to the README](../README.md) · [User guide](user-guide.md) · [Technical information](technical.md)

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
