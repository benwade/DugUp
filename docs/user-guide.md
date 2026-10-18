# Using DugUp

Choose the folders you want to search, then find files by their names, the words they contain, their meaning, or what a picture shows.

[Back to the README](../README.md) · [Renaming and removing files](file-changes.md) · [Technical information](technical.md)

## Start with your folders

The first time you open DugUp, setup suggests settings for your computer and folders you might want to index. Review those suggestions, choose your folders, and start indexing. You can search the files already indexed while the rest are still being read.

To add another folder later, open the **Index** tab and choose **Add folder...**. DugUp looks at the folder structure, file names and sizes before opening the files themselves. It then shows you what it found and lets you choose what to include. Nothing is applied until you choose **Apply**.

The suggestions are a starting point. DugUp may leave out caches and tool folders, suggest names-only indexing for a book library, or skip OCR in a folder full of photos. It also points out folders that look like copies of others. Review the reasons and change any choices that do not suit you.

### Choose what to index

Each included folder keeps its file names. You can also enable these separately:

| Choice | What it adds |
|---|---|
| Words | Text inside supported documents. |
| Scans | Text read from pictures and scanned pages using OCR. |
| Meaning | Searchable representations of what passages mean. Requires its optional model. |
| Pictures alike | Searchable representations of what pictures show. Requires its optional models. |

A folder normally follows the choices of its parent. You can give a subfolder different choices, use **Apply to everything inside**, or restore **Follow the folder above**. A folder inside an excluded folder is excluded too.

Use **Choose what to index...** to revisit a folder's choices. Names-only indexing removes the stored text for that folder; excluding a folder leaves it out entirely. Neither choice deletes your original files.

## Search for a file

Type words in the search box and press **Enter**. Double-click a result to open it. The buttons below the list let you edit the selected file, show it in its folder, or copy its path.

Word search matches related forms: “education” can also find “educate.” Choose **Exact form** when you want the word exactly as typed. Put a phrase in double quotes to search for those words together. Without quotes, results where your words appear close together rank above results where they are far apart.

Use the filters to narrow your search by folder, file type, date, size, or where the words appear: in the file name, document text, or text read by OCR.

Click a column heading to sort. You can build a sort using up to three columns; earlier choices become secondary sort keys. Use **More** to load another page of results. If you start a different search while one is running, DugUp discards the older answer when it arrives.

### Describe your search in plain words

The **In plain words** box fills in the search form from a request such as:

> PDFs about nursing from last month over 1 MB

Check the words, file type, dates and size it chooses, then press **Enter**. It understands English requests for file types, sizes, dates, indexed folder names, quoted phrases, exact words, and sorting such as “newest” or “biggest.” You can also say “in the file name” or “on a sign.”

This uses built-in rules by default, with no additional download. If the index has meaning information, the request uses **Words and meaning**. An experimental model-based reader is available in [Technical information](technical.md#experimental-plain-words-reader).

### Search by meaning

Choose **By meaning** and describe what you remember, such as “a receipt for a licence I paid online.” DugUp looks for passages expressing a similar idea, even if they use different words.

Choose **Words and meaning** when exact names or terms also matter. For example, “EZDriveMA receipt from 2018” can benefit from both kinds of match.

These modes require the optional meaning model and meaning indexing for the folders being searched. Results include each file's closest passage. Folder, type, date and size filters still apply; word-specific options such as **Exact form** do not apply to meaning-only search.

If indexing is incomplete, the results report how many files have meaning information so far. See [models and search](technical.md#models-and-search) for setup and command-line options.

### Search for pictures

Choose **By looks (pictures)** and describe a scene, such as “a dog on a beach.” To start from an image instead, select it and choose **Pictures like this**.

The **Pictures** tab shows matching images as thumbnails. Click one to select it or double-click to open it. These searches require the optional picture models and picture indexing.

For screenshots and scanned pages, search by their text instead. Picture similarity tends to treat many pages of text as visually alike, even when they contain different information.

## Understand your results

If the same file appears in several indexed places, DugUp normally shows one result and lists the other locations under **the same file is also at**. Choose **Every copy** to list each location separately. DugUp normally prefers the copy on your own drive when choosing which one to display.

A missing file remains listed and flagged for 30 days. Files on an unavailable drive or in an unreachable folder may also remain visible. Choose **Only files that are here now** to hide unavailable results; DugUp remembers that choice.

Some files cannot be read, for example because they are damaged or password-protected. The status information gives the reason rather than treating them as successfully indexed.

To select several results, use **Ctrl-click**, **Shift-click**, or **Ctrl+A**. The same shortcuts work in the picture grid. **Open**, **Edit** and **Show in folder** act on the first selected file; trashing and renaming can act on multiple selections. See [Renaming and removing files](file-changes.md) before applying those changes.

## Keep the index current

Choose **Update the index** on the **Index** tab after adding or changing files. By default, the run continues with OCR where enabled, then meaning and picture indexing where the corresponding models are installed. The progress display includes a **Stop** button. Work still waiting can be continued later.

OCR reads clear printed text best. Handwriting, decorative lettering and blurred photos are often missed. For a folder that needs attention immediately, **Read a folder now, fast...** starts OCR for that folder. If another run is active, DugUp stops it cleanly and leaves unfinished work queued.

The tray can handle routine updates in the background. See the next section for its schedule and controls.

## Tray and search shortcut

Keep DugUp in the tray to search from anywhere and update the index in the background.

On Windows, press **Win+Shift+F** to open the small search box:

- Type a few letters to see results as you type.
- Use the arrow keys to choose a result and **Enter** to open it.
- Press **Ctrl+Enter** to continue the search in the full window.
- Press **Esc** to close the box.

The box looks for file names first, starting with the folder last selected in the main window, then searching elsewhere. If that finds too little, it searches text inside files too.

Change the shortcut using the `hotkey` setting, for example to `Ctrl+Alt+D`. DugUp reports if another program already uses the chosen shortcut. On Linux, use your desktop's keyboard settings to bind a shortcut to `dugup popup`.

### Control the tray with DugUp Remote

Open **DugUp Remote** from the Windows Start menu to start or quit the tray, open either search window, or update the index. Closing the remote leaves the tray running. The remote also holds the [cloud search](#let-claude-search-for-you) switch and folder list.

**Free the graphics card and memory** unloads models and pauses background updates while leaving the index open. **Load the models** loads them again and resumes updates. The remote also has a start-at-login checkbox.

### Background updates

With background updates enabled, the tray scans when it starts and then once an hour by default. It uses fewer processor cores than a manual run. On a laptop, it waits for mains power unless you enable updates on battery.

The tray menu shows progress and offers **Update now**, **Pause updates** and **Stop this update**. A manual run in the main window offers to stop a background run before taking over.

Set `watch_every_minutes` to change the interval, or to `0` to disable scheduled updates. `watch_on_battery` controls battery use, and `watch_readers` controls background concurrency. If meaning or picture models are missing, a background run with eligible work may download them. Choose the setup downloads if you want that done in advance.

## Let Claude search for you

Ask Claude to find files for you, while you decide what it can see. Cloud search access is **off** until you turn it on.

### Turn it on

Tick **Allow cloud search access** wherever is handiest:

- the **Win+Shift+F** search box, on its bottom line;
- the tray icon's menu;
- the main window's **File** menu;
- **DugUp Remote**.

DugUp then explains what will leave your computer and asks you to choose **Allow** or **Cancel**. If the Claude desktop app is not yet connected, DugUp offers to connect it; restart the Claude app afterwards to finish.

### Choose what Claude can see

Choose **Folders...** in the search box, **Choose folders...** in DugUp Remote, or **Folders Claude may search...** in the **File** menu. Pick **Every indexed folder**, or **Only these folders** and add the ones you want. **Leave out folders you would not attach to a conversation**, such as medical or money records. Results, passages and other copies of a file outside your chosen folders are never shown to Claude.

### Search with Claude

In a Claude conversation linked to your computer, ask for what you need: “find my notes about the rental car” or “pictures of a dog in the snow.” Claude can:

- search by words, by meaning, by both, or pictures by what they show, with folder, type and date filters;
- read more passages from one file around some words, a few hundred characters each;
- check what is indexed and how far meaning and picture indexing have got.

Claude cannot change, move, rename or delete anything, or change DugUp's settings.

### What leaves your computer

Claude runs in the cloud. When it searches, what it is shown, file names, full paths and the passages that matched, goes to Anthropic as part of the conversation, as a file you attach would. Your files themselves are not sent. DugUp's promise that your files stay on your computer does not cover what Claude is shown, so choose your folders with that in mind.

Untick the switch to turn cloud search off. It takes effect immediately, even in a conversation already under way.

## Settings

Adjust DugUp to your computer, or review its suggested settings before saving.

Open **Settings...** from the File menu or Index tab. Settings are grouped by feature, with everyday choices first. Search by a word such as “battery,” “card,” or “readers” to find a setting by its name or help text.

Each setting explains what it does. Cost labels indicate whether increasing it uses light, medium or heavy resources. Changed values are marked, with **Reset** beside them.

**Suggest for this machine** checks processor cores, memory, disk and graphics performance, then fills in proposed values with a reason for each. Review them and choose **Save** to apply them.

## Command-line reference

Use these commands from a terminal where `dugup` is available. Replace the example paths with your own. A source installation normally needs its virtual environment activated first.

### Open and inspect

```powershell
dugup window
dugup doctor
dugup status --attention
dugup status --unreadable
```

`dugup-window` opens the main window without a console. Add `--save FILE` to the unreadable-file report to save it.

### Add and review folders

```powershell
dugup roots add "D:\Some Folder"
dugup roots survey "D:\Some Folder"
dugup roots suggest "D:\Some Folder"
```

`survey` estimates the work from directory information without opening file contents. `suggest` proposes exclusions without applying them. Add `--apply-safe` to apply exclusions for tool data and downloaded dependencies.

```powershell
dugup roots level "D:\Some Folder\Books" names
dugup roots level "D:\Photos" words+alike
dugup roots exclude "D:\Some Folder" /Trash Temp
```

Indexing choices are `words`, `ocr`, `meaning` and `alike`; combine them with `+`. Use `names` for file names only, or `inherit` to follow the parent folder. Add `--undo` to an exclusion command to remove those exclusion patterns.

### Update and search

```powershell
dugup index
dugup ocr
dugup search invoice 2024
dugup search --meaning "a receipt for a licence I paid online"
dugup search --both "EZDriveMA receipt from 2018"
dugup search --looks "a dog on a beach"
dugup ask "word files about rent, over 1 MB"
```

Add `--exact`, `--every-copy` or `--here` to word searches as needed. `ask` shows how a plain-words request is interpreted; it does not change files.

`dugup index --no-ocr` skips OCR for that run. `--no-meaning` and `--no-looks` skip their corresponding stages. OCR can run separately:

```powershell
dugup ocr --gentle
dugup ocr --fast --folder "D:\Some Folder\Scans"
```

`--gentle` uses one reader. `--fast` uses several, subject to available memory; `--readers N` sets the number. Use `--minutes N` to limit OCR time, or `--again` with `--folder` to reread that folder.

### Tray controls

```powershell
dugup tray
dugup popup
dugup remote
dugup tray --startup add
dugup tray --startup remove
dugup tray --quit
```

Starting the tray a second time opens the existing tray's search box. Source installations need the optional tray dependencies for the tray icon; the popup command still works without that icon.

### Cloud search

```powershell
dugup mcp enable
dugup mcp enable --folders "C:\Users\me\Documents;D:\Notes"
dugup mcp folders
dugup mcp connect --write
dugup mcp status
dugup mcp disable
```

`enable` explains what leaves your computer and asks before turning cloud search on. `folders` opens the folder window; `--folders` sets the list directly, separated by semicolons. `connect --write` adds DugUp to the Claude desktop app's settings, keeping everything else and a `.bak` copy. `disable` takes effect immediately.

### Storage and configuration

```powershell
dugup space
dugup space --limit 2000
dugup space --compact
dugup config list --all
```

The space limit is in MB; `0` means no limit. Compaction returns unused index space to the disk. Configuration also supports `get`, `set` and `reset`.

Settings live in `config.toml` in [the data folder](technical.md#data-storage). `python -m dugup` can be used in place of `dugup`. Use `--data-dir FOLDER` to choose a different data folder for a command.
