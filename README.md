# DugUp

Find your files by what is in them: the words inside, the words in a photo or a scan, what a passage
means, what a picture looks like. Press one key anywhere, type a few letters, and the file opens.
Everything runs on your own machine; nothing you have leaves it.

DugUp was inspired by the macOS app [DigUp](https://github.com/ARahim3/DigUp) but is a separate design,
written from scratch in Python; it shares no code with it. It runs on Windows 11; Linux is supported and
tested on Ubuntu (the full test suite runs there on every push, under Python 3.10, 3.12 and 3.13; the window,
the tray and the search key have had real daily use only on Windows so far).

**Status: alpha.** The searches, the index and the file-changing paths (trash, rename, undo, near-duplicate
cleanup) have been reviewed independently, probed, and tested on Windows and Linux; the guarantees they keep
are stated below under *What it touches on your disk*. What makes it an alpha: one person has used the window for
real; a whole search still runs on the window's thread (the model work does not), so a very large index can
make it pause; the near-duplicate scan has no stated ceiling yet for collections past about 100,000 pictures;
and the index is not encrypted. Bug reports with the `dugup doctor` output are welcome.

## What it does

- **Words.** Indexes the folders you choose and finds files by the words in them and in their names:
  plain text and HTML, PDF text layers, docx/xlsx/pptx and odt/ods/odp. Stemmed, with phrases, closeness
  and exact-form search, and filters by folder, kind, date and size.
- **Words in pictures and scans.** Reads photos, screenshots and scanned pages (OCR), so a receipt can be
  found by the shop's name on it. Document pages first, photos later, tiny graphics last.
- **Meaning.** A question in your own words finds passages that say the same thing differently, and
  "Words and meaning" blends the two.
- **Pictures by likeness.** Pictures of something you name, pictures like another one, and sets of
  near-duplicates with a keeper chosen and the extras one press from the trash.
- **Names for pictures.** Photos called IMG_4471.jpg can be renamed from what they show, by a pattern you
  set, always previewed first, always undoable.
- **A key anywhere.** `dugup tray` keeps it running; Win+Shift+F brings up a small box over whatever you
  are doing.
- **Honest about what it knows.** Every file ends in a stated state; a file that cannot be read says why;
  a file gone since the last scan stays findable, flagged, for 30 days.

## Private by design

Local first, with the exceptions stated here and nowhere else. There is no account, no telemetry, no
crash reporting. Indexing, OCR, meaning, pictures and captions all run on this machine. The exceptions:

- The models for meaning search, pictures and captions are downloaded once, on first use, from pinned
  addresses (each file checked against its recorded SHA-256). That download tells the host nothing but
  the ordinary request; you can fetch the files yourself and drop them in the data folder instead, and
  then no request is ever made.
- The plain-words reader talks only to a model server on this machine unless you set
  `intent_allow_remote` on purpose. If you do, that server is sent what you typed and the list of your
  indexed folders' paths (so it can name one), never the files themselves.

What is kept, and where: the index is a plain SQLite file in your own data folder, holding the text that
was read from your files, so it is as private as that folder and no more; it is not encrypted, and anyone
who can read the folder can read it (your disk encryption, if any, covers it). Logs hold counts and reason
codes, never your text or your file names. The records of what was moved to the trash or renamed hold the
file names, since that is what undo needs. Delete the data folder and all of it is gone.

## What it touches on your disk

Indexing only reads. The index lives in your own data folder (`%LOCALAPPDATA%\DugUp` on Windows,
`~/.local/share/DugUp` on Linux), never in the folders you index, and never in a folder that looks
cloud-synced unless you say so. The two things that change files, clearing out copies and renaming
pictures, happen only when you ask, show you first, keep a record, and move files to the trash rather
than deleting them. On Windows that is the Recycle Bin through the shell, which warns, with its own
dialog, in the rare case where it could only delete outright (a drive with no bin); that is a warning you
can answer, not a guarantee, so read it. Logs hold counts and reasons, never your text or file names.

What the record promises, for every file it changes (trash, rename, undo, clearing out copies):

- **The record comes first.** Before a file is touched, a line saying what is about to happen is written to
  the record and confirmed by the disk. If that line cannot be written, the file is left alone and nothing
  after it happens either.
- **A change is never hidden.** If a file was changed but the line saying so cannot be written, DugUp
  reports the change, stops there, and changes nothing more; the line written beforehand is already on disk.
- **It must still be the same file.** A file is changed only if it is still the one you were shown: the same
  size, time and, where the disk keeps one, file ID as when it was indexed and previewed. That holds when you
  pick a different copy to keep, and for undo. A file edited or replaced since then is marked as changed and
  left alone.
- **Rules you set apply when it happens.** Protected folders are checked again at the moment of the change,
  not only at preview, and again for undo.
- **Renames never overwrite.** A new name that is already taken is refused, in one step, on Windows and on
  Linux's common filesystems. On a disk that offers neither of the safe ways, a last resort is used that has
  a brief window another program writing the same name at the same instant could slip into.
- **Old records still undo.** Records written by earlier versions can still be undone, with the same checks.

These checks catch the changes they were tested against; they are not a proof against every way a file's
contents could change, and a power cut mid-change has not been tested.

Files are parsed, and pictures read, in separate worker processes with a time limit, so a damaged or
hostile file can cost one worker and never the index. That is crash isolation, not a security boundary:
the workers run with your own file permissions, and their lack of network access is a shim inside Python,
not an operating-system restriction.

## First run

    dugup window

opens the search window and, the first time, a short setup that asks which folders to index and what to
do with the ones it is unsure about (downloads, caches, build output). Indexing runs while you watch, and
can be stopped and continued at any time. Searching works on what has been indexed so far.

Then, for the key:

    dugup tray --startup add
    dugup tray

## Set up

Python 3.10 or newer. From the folder you cloned into:

Windows (PowerShell):

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements-dev.txt
    pip install -e .
    pip install -r requirements-ocr.txt     (optional: to read pictures and scanned pages)
    pip install pystray                     (optional: the icon for "dugup tray")

Linux:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    pip install -e .
    pip install -r requirements-ocr.txt     (optional: to read pictures and scanned pages)

The OCR engine is RapidOCR. Its models come inside the package, so nothing is downloaded when it runs.
It reads printed words well. Drawn or handwritten lettering and blurred photos are often missed.

## Use

The window:

    dugup window        (or: dugup-window, which opens without a console)

Type words and press Enter. Narrow by folder, by where the words are (text, file name, text read by OCR),
by kind of file, by date and by size. Words are matched by their stem, so "education" also finds
"educate"; tick "Exact form" (or use `dugup search --exact`) for the very form you typed. Put a phrase in double
quotes to find it as typed. Without quotes, files where your words stand together rank above files where
they are far apart. Click a column header to sort; click up to three and the earlier
ones become the second and third sort keys. Double-click a result to open it; the buttons under the list
open it for editing, show it in its folder, or copy its path. The Index tab adds folders, brings the index
up to date and reads pictures and scans, with progress and a Stop button.

### Search in plain words

The window's "In plain words" box turns a sentence into the search form. Type "PDFs about nursing from
last month over 1 MB" and it fills in the words, the type, the dates and the size for you to check; nothing
is searched until you press Enter. It understands kinds of file, sizes, times (yesterday, last week, in
March, since 2026-01-15, in 2024), "in the file name", "on a sign" or "in a picture", "the exact word",
quoted phrases, the names of your indexed folders, and an order ("the most recent", "newest", "oldest",
"biggest" become the sort, not words to look for). Once the index holds sketches of meaning, the sentence
goes to "Words and meaning" rather than a plain word search, since a sentence is what that is for. English only.

This is done by rules built into the program: nothing to install, and the same answer every time.

Experimental: a small language model can read the sentence instead. `dugup models install` shows what
would be downloaded (a model server and a model file, about 300 MB, each checked against a known
checksum) and `dugup models install --yes` fetches them; that download is the only time DugUp uses the
internet. Set `intent_reader = "model"` in `config.toml` to use it. On the smallest model, tried without
any training for this job, the rules were clearly more reliable, so the rules are the default.
`intent_url` points at a model server you run yourself (LM Studio, for example) instead.

`dugup models install --model larger` fetches a mid-size model instead (2.2 GB, MIT licence). It needs about
3 GB of memory and, without a graphics card, probably several seconds a sentence (an estimate, not yet
measured). `dugup ask "pdfs about budgets from last month"` shows how a sentence fills in the form and how
long that took; add `--reader model` or `--reader rules` to compare the two.

The command line:

    dugup doctor
    dugup roots add "D:\Some Folder"
    dugup roots survey "D:\Some Folder"               (what is in it and what indexing would cost; opens no file)
    dugup roots suggest "D:\Some Folder"              (what may not be worth indexing; changes nothing)
    dugup roots level "D:\Some Folder\Books" names    (how far indexing goes there: names, words, ocr or meaning)
    dugup roots suggest "D:\Some Folder" --apply-safe (skip downloaded requirements and tool data)
    dugup roots exclude "D:\Some Folder" /Trash Temp  (skip anything else; --undo takes it back)
    dugup index
    dugup ocr                                         (read pictures and scanned pages; --minutes N to limit it)
    dugup search invoice 2024
    dugup status --attention
    dugup space                                       (what the index uses; --limit MB sets a spending limit)
    dugup space --compact                             (give back space left by removed and re-read text)
    dugup ask "word files about rent, over 1 MB"      (how a sentence is read; changes nothing)

The index keeps each piece of text once, compressed (`text_compression = "zlib"` in `config.toml`; `"none"`
turns it off for new text). Searching is unaffected: the word index is built from the text as written. An
index from an earlier version is converted the first time it is opened, which can take a minute on a
large one; run `dugup space --compact` afterwards to hand the freed space back to the disk.

On a first start (no folder indexed yet) the window opens a setup wizard in front, with the main window
minimized until setting up is over: it looks at the computer, suggests settings with the reason for each,
lists likely folders with why each is ticked or not, and then shows the picker for each folder chosen.
"Settings..." opens the same pages later with every setting. While a folder is looked through, a small
window in the middle of the screen says why, how far it has got, and offers Stop.

In the window, "Add folder..." looks through the folder (opening no file) and shows a picker: a box to take
each subfolder in or out, how far to go in each, what it holds, and what it would add to the index. It
starts from guesses (tool data and trash out, picture folders without OCR, folders named like a book
library by name only), each with its reason, and nothing changes until Apply. "Choose what to index..."
opens it again for a folder that is already indexed. Each row has an "Include" box and a box for each
thing to do there (Words, Scans, and two that are remembered for later: Meaning, Pictures alike); a folder
inside one that is not included shows as not included. Folders that look like copies of other folders are
pointed out (same file names and sizes; no file is opened): one named like a backup starts not included,
with the reason, and otherwise both stay in with a note. The guesses can be tuned in `config.toml`
(`book_folders`, `book_folder_level`, `picture_heavy_fraction`, `picture_heavy_min`).

"Update the index" in the window goes straight on to reading pictures and scans (OCR) in the folders whose
level asks for it, as one run with one Stop. `ocr_after_index = false` in `config.toml` keeps the two apart.
`dugup index` does the same; `dugup index --no-ocr` stops once the index is up to date, and `dugup ocr`
reads what waits at any time.

The same file kept in several places (a backup, another computer's copy) is known for one file: each file
read gets a mark of its exact content, and a search shows such a file once with "the same file is also at"
and the full path of every other copy. `dugup search ... --every-copy`, or "Every copy" in the window, lists
them all. `dugup status` says how many files are extra copies. `content_marks = false` in `config.toml`
turns this off. An index made before marks existed is marked by the next `dugup index` (each file is read
once more, nothing is parsed again). A file whose content was already read in another place is not read again: its text, and what OCR read from
its pictures, is taken from that file, and a page is read by OCR once for all its copies (`read_once = false`
turns this off). Empty files are never counted as copies of each other, and the same bytes under another
ending are read for themselves. Of several copies, a search shows the one on your own drive (`main_place`;
on Windows it is worked out as the drive your own folders are on). `dugup roots copies PATH` lists whole folders that look like copies of
each other, from names and sizes alone, before anything is indexed. `dugup duplicates G:\\` lists the same file kept in several places on one drive, from the
content marks, each set with the copy to keep marked (the one in the larger set of files, then the one higher
up) and every other copy's full path; `--save FILE` writes the whole report; folders in `cleanup_protect`
(or `--protect FOLDER`) are never proposed, for a live backup of this computer on a cloud drive. It lists;
you do the removing. `dugup status --unreadable` lists files
that are damaged or need a password, and `--save FILE` writes that list to a file. Indexing and searching
never change a file. The one part that moves files is the clearing out of copies (below), which sends them
to the trash and only when you ask it to.

Updating the index reads several files side by side, each in a worker of its own, so one slow file (a
large PDF, or one that must first come down from a cloud drive) does not hold up the rest. The number is
worked out from the processor cores and free memory, at most 8; `index_readers` in `config.toml` sets it
(1 reads one file at a time). The pause for "files in a row that crashed or ran out of time" counts per reader.

OCR reads one page at a time when little waits, so the computer stays pleasant to use, and goes fast by
itself (several readers) once at least `ocr_fast_pages` pages wait (200; 0 means never by itself). That holds
for the run that follows `dugup index` and for the plain `dugup ocr`; `dugup ocr --gentle` reads one page at a
time whatever waits. `dugup ocr --fast` runs
several readers side by side (about half the processor cores, limited by free memory; `--readers N` sets the
number), and `--folder "D:\Some Folder\Scans"` reads just one folder. `--again` with `--folder` reads that
folder afresh. In the window, "Read a folder now, fast..." does the same and takes over from a run that is
going on: that run stops cleanly, and what it had not finished stays queued. Without `--folder`,
`dugup ocr --fast` reads everything that waits; with many thousands of pages waiting it is the one to use
(the run that follows `dugup index` by itself is the gentle one).

OCR runs on the processor by default. A graphics card is an option: `dugup ocr --gpu`, or `ocr_gpu = true`
in `config.toml`. It needs an OCR runtime that can use the card; on Windows that is `onnxruntime-directml`
in place of `onnxruntime` (`pip uninstall onnxruntime`, then `pip install onnxruntime-directml`), which
works with NVIDIA, AMD and Intel cards. `dugup doctor` says whether a card can be used. Where it cannot,
the processor is used and the report says so.

What is done with a folder's files is a set of separate choices, not a ladder. File names are always
kept. Beyond that: `words` (the text of documents), `ocr` (words in pictures and scans), `meaning` and
`alike` (pictures by what they show; these two are remembered now and used once searching by meaning is
built). Join them with `+`, for example `dugup roots level "D:\Photos" words+alike` for a photo folder that
should not be put through OCR. `names` means file names only, and `inherit` makes a folder follow the one
above it again. A folder's choice applies to everything inside it until a folder further in has its own.
Lowering a folder to `names` removes the text kept for it at once. To leave a folder out completely, use
`dugup roots exclude`. In the picker these are tick columns, with "Apply to everything inside" and
"Follow the folder above" for the selected folder.

`python -m dugup ...` does the same. The index lives in one per-user data folder (`%LOCALAPPDATA%\DugUp`
on Windows, `~/.local/share/DugUp` on Linux); add `--data-dir FOLDER` to any command to keep it somewhere
else. Deleting that folder removes everything DugUp stored. Settings go in `config.toml` in the data
folder; see `src/dugup/settings.py` for the names and defaults. The ones most likely to matter:

    reader_seconds = 60        # time for one file, plus reader_seconds_per_mb (2) for each megabyte
    ocr_seconds = 120          # time for one page or picture
    retry_patience = 2.0       # the second try gets this many times the limit; 1 turns second tries off
    max_index_mb = 0           # spending limit for the index; 0 means none (dugup space --limit sets it)
    min_free_mb = 200          # a run stops when the drive has less than this free
    figure_fraction = 0.1      # a picture this share of a typed page gets the page read by OCR too; 0: off

## Settings

There are many, on purpose: the machine this runs on may be a 32-core workstation or a laptop with 8 GB,
and the settings are how it is made to fit. **Settings...** (in the File menu, and on the Index tab) shows every one of them, by the
part of the program it belongs to, the everyday ones first and then the ones that command the most of the
machine, each with a line of help and a tag (heavy, medium, light) saying what turning it up costs. A box at
the top finds a setting by any word in its name or its help ("readers", "battery", "card"); a setting that
differs from its default is marked, with Reset beside it. **Suggest for this machine** looks at the
computer (cores, memory, disk, the graphics card, with a short speed test) and fills in what fits, with the
reason under each; nothing is saved until Save. The same settings are reachable from the command line
(`dugup config list --all`, `get`, `set`, `reset`) and live in `config.toml` in the data folder.

## From the tray: a key that searches anywhere

    dugup tray

keeps DugUp running with the index open, and **Win+Shift+F** then brings up a small search box over
whatever you are doing: type a few letters, the files appear as you type, Enter opens the one at the top
(arrows pick another), Esc closes. Names are searched first, in the folder the main window was last set to
and then everywhere, and words in the files after that when names found too little. Ctrl+Enter hands what
you typed to the full window for the heavier searches.

The key is the setting `hotkey` (written the usual way: `Ctrl+Alt+D`, `Ctrl+Shift+Space`); the tray says at
start when another program already has it. On Windows the key is registered with the system and nothing
is installed for it. On Linux the desktop's own keyboard settings can bind a key to `dugup popup`, which
reaches the running tray; that command works on Windows too. With `pystray` installed there is an icon in
the tray (Search, Open the window, Update the index now, Quit); without it the key and `dugup popup` work
all the same, and `dugup tray --quit` ends it. `dugup tray --startup add` makes it start when you log in
(a small entry in the Startup folder, or an autostart file on Linux); `--startup remove` takes that back.
A second `dugup tray` does not start a second one: it brings up the search box of the one running.

`dugup remote` is a small window that drives the tray without a console: a read-only line saying what the
tray is doing, and buttons for Start tray, Search box, Full window, Update the index now, Quit tray, and one
that toggles between **Free the graphics card and memory** (the models are unloaded and background updates
pause, so the card goes to something else while the index stays warm) and **Load the models** (they come
back and updates resume). A tick starts the tray when you log in. Close the remote and the tray goes on.

While the tray runs, **the index keeps itself current**: once when the tray starts and then every
`watch_every_minutes` (an hour by default; 0 turns it off), the folders are scanned again, what changed is
read, and OCR, meaning and looks follow for what is new, gently (a quarter of the cores, OCR in its gentle
mode, never every core) so the machine stays yours; on a laptop running on its battery the update waits for
mains power unless `watch_on_battery` says otherwise. The tray's menu shows what it is doing and has "Update now", "Pause
updates" and "Stop this update". A run you start yourself in the window goes at full speed; if it finds
the tray's run in the way, it offers to stop that and takes over as soon as the lock is free. The first
background run after installing may fetch the meaning and picture models, as the window's Update would.

## Searching by meaning

Besides the words themselves, DugUp can keep a sketch of what each passage is about, so that a question in
your own words ("a receipt for a licence I paid online") finds passages that say the same thing in other
words. It is an optional part: `pip install dugup[meaning]` adds the runtime (onnxruntime, numpy,
tokenizers), and the model is fetched once, on first use, into the data folder (the larger one, 547 MB, by
default; `meaning_model = "small"` chooses the 90 MB one, about seven times faster and less exact).

- **Filling.** `dugup meaning fill` sketches every file with text that has no sketch yet, the most recently
  changed first, and keeps the sketches in the index as bytes (about 170 MB for 200,000 passages at full
  size; `meaning_size = "compact"` keeps a third of that and loses a few results from the top ten). Ctrl+C
  stops it and the next run continues where it left off. A folder whose level stops short of meaning is
  passed over. By default an index update (`dugup index`, or the window's Update) goes on to sketch what is
  new after OCR, once the model is there; `meaning_after_index = false` or `--no-meaning` leaves that out,
  and an update never fetches the model by itself. On a graphics card the larger model is about ten times
  faster than on the processor (`meaning_gpu`); where the card cannot be used, the processor is.
- **Searching.** `dugup search --meaning "a question in your own words"`, or "By meaning" in the window's
  where-choice. Each result is one file with its closest passage, closest first, paged with the same More
  button or `--offset`. A file kept in several places is shown once, as in a word search. The file filters
  (folder, kinds, dates, sizes) apply; the word ones (exact form, in the name) do not. While the sketches
  are still being made the answer says how far they reach ("cover 4,210 of 20,000 files so far").
  Changing the model or the size makes every file want a new sketch; the old ones go as the new are made.
- **Words and meaning together.** `dugup search --both "EZDriveMA receipt from 2018"`, or "Words and meaning"
  in the window. Each file's closeness in meaning (0 to 1) gets a bonus for matching the words: the best
  word match adds `meaning_word_weight` (0.5 by default), weaker ones less in proportion, so a name in the
  question lifts its file without letting a word match outrank a strong meaning on its own. A file found by
  only one side still appears, scored by that side. The weight is yours to set: 0 is meaning alone, above 1
  and the words rule; `meaning_word_pool` says how many word matches are brought in. The word side looks
  for any of the question's telling words, not all of them, and a word the index holds nearly everywhere
  ("I", "the", or whatever your own files say on every page: `meaning_word_everywhere`, 0.3 of the passages)
  is not looked for at all, so a plain question is not held to its filler. The command says which words
  were set aside.
- **In the window.** The model that reads your question is loaded in the background as the window opens,
  so the first search by meaning does not wait for it (`meaning_warm_up`: `auto` does this only when the
  index holds sketches; `yes` always; `never` keeps the 600 MB of memory for a wait on first use instead). A
  search pressed before it is done shows a small wait window with a Stop; the model goes on loading either
  way. The question is sketched off the window's own thread, so the window never freezes for it.
- **How it finds them.** Two stages: the first 96 numbers of every sketch are scanned at once (a few tens of
  milliseconds for a quarter of a million passages) to pick the 800 likeliest files, a few passages each,
  and those are scored by their full sketches. A file with many pages near the question takes a few
  places in that pool, not the whole of it. `dugup meaning status` says how much of the index is covered.
- **Measuring.** `dugup meaning measure --ask "a sentence"` tries both models on a sample of your own index
  and reports speed, disk and what the questions find, without storing anything.

## Pictures by likeness

DugUp can keep a sketch of what each picture looks like, so you can find pictures like another one, or
pictures of something you name. It is the same machinery as searching by meaning, with a picture model in
place of the text one: the picture half of CLIP (352 MB) and its sentence half (254 MB), pinned by hash and
fetched on first use into the data folder. It needs `pip install dugup[meaning]` and `pillow`.

- **Filling.** `dugup alike fill` sketches every picture that has none yet, the most recently changed first,
  several readers side by side (`looks_readers`, 0 works it out from the cores), on the card when the runtime
  can (`looks_gpu`). Opening and scaling the pictures is the slow part, not the model, which is why the readers
  matter more than the card. A picture that cannot be read is noted with the reason and not tried again unless
  it changes; `dugup alike status` lists them. Ctrl+C stops; the next run continues. Folders whose level leaves
  out "Pictures alike" are passed over. By default an index update goes on to new pictures after the sketches
  of meaning, once the model is there (`looks_after_index`, `--no-looks`). About 512 bytes a picture.
- **Searching.** `dugup search --looks "a dog on a beach"`, or "By looks" in the window: pictures that look
  like what the sentence says, closest first. `dugup alike like PICTURE`: pictures in the index that look like
  one (in the index or not; a picture outside it is read and sketched on the spot). Scores: about 0.30 is a
  strong match to a sentence and 0.20 none; between pictures, 0.9 and up is the same scene and 0.97 and up is
  nearly the same file (a resized copy, say). Screenshots and scanned pages all look like "a page of text" to
  this model; OCR serves those. The file filters apply.
- **In the window.** "By looks (pictures)" in the where-choice takes a sentence; "Pictures like this" beside
  Open shows the likes of the selected picture. The Pictures tab shows the results that are pictures as a grid
  of thumbnails with their scores, so likeness can be judged by eye; a click selects the picture in the list,
  a double click opens it. Thumbnails are made in the background and kept for the session only.
- **Measuring.** `dugup alike measure --ask "a sentence" --like PICTURE` tries the model on a sample of your
  own pictures and reports speed, disk and what is found, without storing anything.

## Near-duplicate pictures

Photos that are nearly the same but not byte for byte (a resized copy, a re-saved one, a second shot of the
same scene) are found by their sketches: `dugup alike duplicates [--threshold 0.97] [--protect F] [--prefer F]`
lists sets of pictures at that closeness and above with the one to keep in each, and `--remove --record FILE`
moves the extras to the trash after asking. In the window, the Pictures tab has **Find near-duplicates** with
the threshold beside it: the sets fill the grid with each keeper marked KEEP in green and the extras in red,
"Keep this one instead" changes a keeper by hand, and "Trash the extras" moves the rest with the same question,
record and protections as To trash.

The keeper of a set: a picture in a preferred folder (`cleanup_prefer`, `--prefer`) first; else the larger
file, which is usually the original rather than a resized or re-saved copy; else the older one; else the
shorter path. The rule that chose it is shown. Pictures in protected folders are shown aside and never
proposed, and a set needs two open copies to count. The threshold is a judgement, which is why the sets are
shown before anything moves: 0.97 and above is a copy or a re-save; two shots a second apart can score that
too, and whether they are duplicates is yours to say. Screenshots and scanned pages all look much alike to
the picture model, so a folder of those may cluster; look before trashing.

A sketch is evidence about the picture as it was indexed, so a picture that changed since (edited, or replaced
by another file at the same name) is shown as **changed** and never proposed, as keeper or as extra, until the
next index update sketches it again; choosing another keeper weighs the pictures as they were first seen and
cannot bring a changed one back in. At the moment of trashing, each extra and its keeper are checked once more
against what the preview saw (size, time, file id) and against the index; anything that differs is left,
with the reason.

## To trash, from the window

A file that was gone at the last scan stays in the results for 30 days, flagged (a drive may be merely
unplugged), and so do the files of a folder that cannot be reached right now. **Only files that are here
now** (a tick under the search form; `--here` on the command line) leaves both out. The tick is remembered.

Selecting works the usual way, in the list and in the Pictures grid alike: a click chooses one, Ctrl-click
adds or removes one, Shift-click takes a range, Ctrl+A takes every row; selected tiles carry a tick. The
details pane shows the first of them; Open, Edit and Show in folder act on that one only (thirty viewers by
accident is worse than one on purpose), while To trash, the Rename tab and "Keep this one instead" take them
all.

Beside Open and Show in folder, **To trash** moves the selected file(s) to the trash the way the cleanup does: the Windows shell with undo (a file on a cloud drive lands in
that drive's trash), `send2trash` elsewhere. It asks first, naming the files; files in protected folders are
left alone and said so; a record of what went is kept under the data folder (`removed\`); the index marks
them gone and the rows leave the results. Nothing is deleted outright. With "Pictures like this" this is the
by-hand way to clear near-duplicate photos: select the extras in the list and press it.

## Naming pictures by what they show

Many pictures have names that say nothing (`IMG_4471.jpg`). `dugup rename FOLDER` shows what each picture in a
folder would be called, from a sentence a captioning model writes about it and a pattern you set, and changes
nothing; `--apply --record FILE` renames as shown and writes every rename to the record; `dugup rename --undo
FILE` puts the names back, last first. The index follows each rename at once, so the new names are searchable
and nothing is read again.

- **Which pictures**: by default only those whose name is a camera's or a phone's (`IMG_4471`, `20220401_142335`,
  `DSC_0012`, `Screenshot 2025-03-01 ...`, numbers alone); a picture someone has named is left alone and
  said so. `--all` (or the tab's "Also pictures with real names") includes them.
- **The pattern** (`rename_pattern`, default `{caption} {taken}`): `{caption}` the sentence made safe for a
  name ("Green car parked in front of yellow building"; at most about ten words, cut at a whole phrase), `{taken}` the day the photo was taken from the
  camera's own record, else the day it was saved, `{time}`, `{camera}`, `{folder}`, `{original}` the name it
  has now, `{n}`, `{ext}` (the ending is kept unless named). Two pictures that would get the same name are
  numbered. `--pattern` tries another one for a run.
- **The captions** come from a pinned captioning model (Florence-2 base, four ONNX parts, about 1.1 GB),
  fetched once on first use, run in a worker on the card when it can (`caption_gpu`). It is the slowest
  model here (seconds a picture on a processor), which is why it runs on demand only, never in the
  background. `--longer` asks for a longer description; `--no-caption` uses the other fields alone.
  `caption_source = "server"` is the seam for a local model server of your own; it is not built yet.
- **In the window**: the Rename tab. It follows the selection like the other tabs: open it, or change the
  selection while it is open, and the selected picture is previewed by itself. Preview does every picture in
  the current results (a search by words, by looks, or the likes of a picture), with only the selected ones
  starting ticked, or a folder you choose; each picture appears with its
  thumbnail, the name it has, the name it would get in a box you can edit, the sentence the model wrote,
  and a tick to leave it alone. "Rename as shown" asks once, renames, and keeps the record under the data
  folder (`renames\`); "Undo last rename" puts the names back. The captions are written in the background
  with a Stop.
- **Protections**: pictures in protected folders (`cleanup_protect`) are left alone; a picture that changed
  since the preview, or whose new name is taken by then, is skipped and said so; `os.rename` never
  overwrites; the record is written a line at a time.

## Clearing out copies

A drive that several computers have backed up to, or that a sync dispute has left in a mess, ends up holding
the same files in many places. DugUp can find them, say which copy to keep, and move the rest to the trash.
This is the one thing in DugUp that changes a file, so it is kept apart from indexing and searching, and it
only ever moves an exact copy whose keeper is there and identical at that moment (the bytes are compared
afresh, not taken from the index). Where it moves to: on Windows the shell's trash, which for a cloud drive
is that drive's own trash; the shell is told to ask you first in the one case where it could only delete
outright (a drive with no recycle bin), so nothing is deleted without a word. Elsewhere, the `send2trash`
package when it is installed, otherwise a "DugUp removed" folder at the top of the drive with a subfolder
per run. The record file is written a line at a time as files move, so a run cut short still has its
record. Deleting for good is not something DugUp does today; if it ever does, it will be a separate choice
with the word in front of you. The order below has worked on one real drive; it is a way of going about
it, not the only one, and every step is a command you can run again.

1. **Index the drive**, so every file that is read has a mark of its content (`dugup index`). Folders listed
   by name only (book libraries, say) get no marks and take no part; raise their level if you want them in.
2. **See the shape of it** with `dugup roots copies G:\`: whole folders that look like copies of each other,
   from names and sizes. This is a guess, for orientation only, and it shows which folders are the usual
   suspects (a Desktop kept in four places).
3. **Decide what must never be touched.** A folder on the drive that is a live sync of another computer is
   one: removing a file there can remove it on that computer. Name such folders in `cleanup_protect`
   (separated by `;`). The report flags a protected folder that holds no indexed file, which usually means
   the name is wrong: check it against `dir`, not against what the cloud drive's settings call the computer.
4. **Decide where files belong.** When the same file is in four folders of the same size, only you know
   which folder is its home. Name those in `cleanup_prefer`; a copy there is the one kept. Otherwise the copy
   in the larger set of files is kept, then the one higher up.
5. **Look at the report**: `dugup duplicates G:\`, and `--save FILE` for the whole thing in a file. Each set
   shows the copy to keep and every other copy with its full path. Nothing is changed. Read through it;
   a keeper in the wrong place means a folder belongs in step 4.
6. **See how the trash behaves here** with `dugup duplicates G:\ --try-trash "G:\some throwaway file.txt"`:
   on a cloud drive the file should land in that drive's own trash, elsewhere in the Recycle Bin. Where no
   trash can be reached, DugUp moves files aside into a folder named "DugUp removed" at the top of the drive
   instead, and says so.
7. **Remove the extras**: `dugup duplicates G:\ --remove --record C:\Users\you\Desktop\moved.txt`. The
   list is shown and a `yes` asked for. Each extra is moved only after its keeper is found to hold the same
   bytes right then; a copy that has changed, or whose keeper is gone, is left alone and the record says why.
   Ctrl+C stops between files. The record lists every file moved and the copy kept for it.
8. **Run `dugup index` again** so the index catches up with what moved.

Copies on another drive are never duplicates to this report: a file on your own drive and its backup on a
cloud drive are a file and its backup. Run it per drive, and only on the drive you mean to clean.

## Built as a service, with clients

Everything the window does goes through one interface, `Service` (`protocols.py`): search, the index,
OCR, meaning and looks, naming, the trash. The command line, the window, the tray and the search box
are four clients of it, and the tests run against a fake implementation of the same interface. A program
of your own can do the same in a few lines:

    from dugup.datadir import DataLayout, resolve_data_dir
    from dugup.service import LocalService
    from dugup.settings import load

    layout = DataLayout(resolve_data_dir())
    settings, _ = load(layout.config)
    with LocalService(layout, settings) as dugup:
        for row in dugup.search("nursing license", limit=5).rows:
            print(row.path, row.rank)

The same footing will carry an MCP server, so an assistant on this machine can ask the index questions.

## Tests

    python -m pytest

`python tests/make_corpus.py SOME_EMPTY_FOLDER` writes the generated test tree, to try the program by hand.

## Layout

| Module | What it is |
|---|---|
| `shapes.py`, `protocols.py` | Plain data and the contracts between parts. Nothing here does any work. |
| `store.py` | The Store: one SQLite file. |
| `filesource.py`, `dirscan.py` | The FileSource: rules, change detection, the directory scan. |
| `extractors/` | One reader per family (text, PDF, Office, image) and the set that picks between them. |
| `indexer.py` | Scan, record changes, read what is waiting. Talks only to the three contracts above. |
| `service.py` | LocalService: what every client talks to. Policy and the single writer live here. |
| `cli/` | The command line, a client of the service: one module per group of commands, with the ones that change files on disk (`changes_cmds.py`) kept apart. |
| `window.py`, `desktop.py` | The search window (tkinter), another client of the service, and handing a file to the desktop. |
| `launcher.py` | The one place outside programs and workers are started; ends a program with everything it started. |
| `sandbox.py`, `worker.py`, `jobs.py` | The Sandbox: risky work in a worker process with limits, and what crosses the boundary. |
| `ocr.py`, `ocrqueue.py` | The OcrBackend (the engine, in a worker) and the queue that decides what is read first. |
| `plainwords.py` | A sentence to a filled-in search form, by rules (the default). |
| `intent.py`, `models.py` | Experimental: the same through a small language model, and fetching that model and its runtime. |
| `suggest.py` | Suggestions for what to skip (a proposal for the user, never applied by itself). |
| `settings.py`, `kinds.py`, `datadir.py`, `lockfile.py`, `doctor.py` | Settings, file kinds, the data folder, the run lock, the health report. |

## License

PolyForm Noncommercial 1.0.0, copyright Ben Wade: use it, copy it, change it and share it for any
noncommercial purpose, with the notice kept; selling it, or using it in something sold, needs a separate
agreement with the author. The full text is in `LICENSE`.
