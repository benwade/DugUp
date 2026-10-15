"""Every setting described once, as data. The setup wizard, the Settings window, `dugup config` and the
checks on a value all read from here, so a new setting is one new entry and nothing else has to change.

An entry says what a setting is called in plain words, gives one line of guidance, says why a person is
asked (for the few everyday ones), which group it belongs to, what answers it takes, and, where the best
answer depends on the machine, a small rule that works it out. A rule returns the value and the reason in
words, so a default is never a mystery.

A test fails when a setting has no entry here, or an entry names no setting: the catalogue cannot fall
behind the program without someone noticing.

What this does not do is the thinking. A new setting still needs its guidance written and its default
considered; a careless rule is worse than a plain number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .settings import Settings
from .shapes import Failure, FailureReason


@dataclass(frozen=True)
class Machine:
    """What was found on this computer. Anything that could not be told is None (or "" for the card)."""

    cores: int = 1
    free_memory_bytes: Optional[int] = None
    free_disk_bytes: Optional[int] = None
    card: str = ""                           # the graphics card route the OCR runtime has, in words; "" when none
    card_speedup: Optional[float] = None     # how many times faster the card read a sample page than the processor
    page_seconds: Optional[float] = None     # how long the processor took to read the sample page


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    about: str          # two or three sentences: what this part of the program does and why the choices matter


@dataclass(frozen=True)
class Entry:
    name: str           # the setting's name in config.toml and in Settings
    group: str
    label: str          # what it is, in a few plain words
    help: str           # one line of guidance
    why: str = ""       # why a person is asked (the everyday settings)
    everyday: bool = False                              # shown in the setup wizard; the rest are "advanced"
    choices: Tuple[Tuple[object, str], ...] = ()        # (value, what it means), when only these are allowed
    low: Optional[float] = None
    high: Optional[float] = None
    unit: str = ""
    zero: str = ""      # what 0 means for this setting, in words ("no limit", "worked out by DugUp")
    suggest: Optional[Callable[[Machine], Tuple[object, str]]] = None   # a default that depends on the machine


GROUPS: Tuple[Group, ...] = (
    Group("folders", "Choosing what to index",
          "When you add a folder, DugUp looks through it and suggests what to do with each folder inside. "
          "These settings shape those first guesses. You can change any guess in the picker itself."),
    Group("pictures", "Reading pictures and scans",
          "Words in photos, screenshots and scanned pages are read by a separate reader (OCR). It is the slowest "
          "part of building an index, so where it runs and how hard it works make the most difference."),
    Group("meaning", "Searching by meaning",
          "Besides the words themselves, DugUp can keep a sketch of what each passage is about, so a question in "
          "your own words finds passages that say the same thing differently. The sketches are made in the "
          "background after indexing; searching by meaning works on what has been sketched so far."),
    Group("looks", "Pictures by likeness",
          "DugUp can keep a sketch of what each picture looks like, so you can find pictures like another one, "
          "or pictures of something you name. The sketches are made in the background after indexing, in folders "
          "whose level includes \"Pictures alike\"."),
    Group("naming", "Naming pictures",
          "Many pictures have names that say nothing (IMG_4471.jpg). DugUp can rename them from what they show: a "
          "captioning model writes a sentence about each, and a pattern you set turns it into a name. Always a "
          "preview first, a record of every change, and an undo."),
    Group("tray", "The tray and the search key",
          "DugUp can stay in the tray (dugup tray), with the index open, so that one key brings up a small search "
          "box anywhere: type a few letters of a name, Enter opens the file. While it runs, the index keeps itself "
          "current. The tray can start with the computer (dugup tray --startup add)."),
    Group("speed", "How hard to work",
          "Building the index for the first time is heavy work, and that is temporary. Afterwards DugUp only "
          "reads what is new or changed. These settings say how much of the computer it may take while it works."),
    Group("disk", "Disk space",
          "The index is one file on your own disk. These settings stop it from growing past what you allow "
          "and keep it tidy."),
    Group("search", "Searching", "How results are ranked and what is kept for files that have gone."),
    Group("words", "Searching in plain words",
          "You can type a sentence such as \"pdfs about rent from last month\" and have the search form filled "
          "in. Built-in rules do this at once. A language model is an optional alternative."),
    Group("files", "Reading files",
          "How text is taken out of documents. The defaults suit almost everyone; change them for unusual files."),
    Group("safety", "Staying safe with difficult files",
          "Each file is read in a separate, limited process, so one bad file cannot harm the index or hang the "
          "program. These are the limits."),
)


# ---- rules for defaults that depend on the machine --------------------------------------------------------------

CARD_WORTH_IT = 1.5      # the card is suggested only when it read the sample at least this many times faster


def _card(machine: Machine) -> Tuple[object, str]:
    if not machine.card:
        return False, "no graphics card that the reader can use was found"
    if machine.card_speedup is None:
        return False, "a graphics card is there but was not tried; the processor is the safe choice"
    if machine.card_speedup >= CARD_WORTH_IT:
        return True, f"the graphics card read a sample page {machine.card_speedup:.1f} times faster than the processor"
    return False, (f"the graphics card was not clearly faster in the test ({machine.card_speedup:.1f} times the "
                   "processor's speed), so the processor is used")


def _limit(machine: Machine) -> Tuple[object, str]:
    if machine.free_disk_bytes is None:
        return 0, "the free space on the disk could not be read, so no limit is set"
    megabytes = max(500, int(machine.free_disk_bytes / (1024 * 1024) / 10))
    return megabytes, f"a tenth of the {machine.free_disk_bytes / 1024 ** 3:,.0f} GB that is free now"


def _index_readers(machine: Machine) -> Tuple[object, str]:
    from .machine import index_readers_for
    count = index_readers_for(0, machine.cores, machine.free_memory_bytes)
    return 0, f"{count} on this computer, which has {machine.cores} processor cores"


def _readers(machine: Machine) -> Tuple[object, str]:
    from .machine import readers_for
    count = readers_for(0, Settings().ocr_reader_threads, Settings().ocr_reader_memory_mb, machine.cores,
                        machine.free_memory_bytes)
    return 0, f"{count} on this computer, which has {machine.cores} processor cores"


YES_NO = ((True, "yes"), (False, "no"))


def _entries() -> Tuple[Entry, ...]:
    E = Entry
    return (
        # -- folders
        E("book_folder_level", "folders", "Book libraries",
          "What a folder that looks like a book library starts at in the picker.",
          why="Books are large and often not what you want to search by content. Others want every page searchable.",
          everyday=True,
          choices=(("names", "file names only"), ("words", "the text of the books, without reading covers"),
                   ("ocr", "text and scanned pages"), ("meaning", "everything"), ("", "make no guess about books"))),
        E("book_folders", "folders", "Names of book folders",
          "Folder names, separated by semicolons, that are treated as book libraries. Case does not matter."),
        E("picture_heavy_fraction", "folders", "Picture folder: share",
          "A folder starts without OCR when at least this share of its readable files are pictures.", low=0.01, high=1.0),
        E("picture_heavy_min", "folders", "Picture folder: how many",
          "...and it holds at least this many pictures.", low=0, unit="pictures"),
        # -- pictures
        E("ocr_gpu", "pictures", "Read on the graphics card",
          "Use the graphics card for reading pictures and scans when the reader can use one.",
          why="A strong graphics card can be many times faster. A weak one can be slower than the processor, "
              "so this is decided by a short test on your own computer.",
          everyday=True, choices=YES_NO, suggest=_card),
        E("ocr_after_index", "pictures", "Read pictures as part of updating",
          "Updating the index goes straight on to reading pictures and scans, as one job.",
          why="Most people want one button. Turn it off to read pictures at a time of your choosing.",
          everyday=True, choices=YES_NO),
        # -- meaning
        E("meaning_model", "meaning", "Which model",
          "The model that makes the sketches. Changing it means sketching everything again.",
          why="The larger one (547 MB) finds the right passage noticeably more often; the small one (90 MB) is "
              "about seven times faster to run.",
          everyday=True, choices=(("larger", "larger (better)"), ("small", "small (faster)"))),
        E("meaning_size", "meaning", "Size of each sketch",
          "How much of each sketch is kept. Changing it means sketching everything again.",
          why="Full keeps every number: about 170 MB for 200,000 passages. Compact keeps a third of the disk "
              "and loses a few results from the top ten.",
          choices=(("full", "full"), ("compact", "compact"))),
        E("meaning_gpu", "meaning", "Sketch on the graphics card",
          "Use the graphics card for making sketches when the runtime can use one. Where it cannot, the processor is used.",
          why="On a graphics card the larger model is about ten times faster than on the processor.",
          everyday=True, choices=YES_NO),
        E("meaning_word_weight", "meaning", "Words and meaning: weight of the words",
          "In a \"Words and meaning\" search, the most that a word match can add to a file's closeness in meaning "
          "(closeness runs from 0 to 1). The best word match adds this much; weaker ones add less in proportion.",
          why="At 0.5 a name in the question lifts its file above files that are merely close in sense, without "
              "letting any word match outrank a strong meaning on its own. 0 is meaning alone; above 1 the words rule.",
          low=0.0, high=5.0),
        E("meaning_word_pool", "meaning", "Words and meaning: how many word matches",
          "How many of the best word matches are brought into a \"Words and meaning\" search.",
          low=1, high=100000, unit="files"),
        E("meaning_word_everywhere", "meaning", "Words and meaning: words found everywhere",
          "A word of the question that at least this share of the index's passages contain is not looked for "
          "(\"a\", \"the\", \"I\", and whatever your own files say on nearly every page). 0 looks for every word.",
          why="A plain question has filler words in it. Requiring them would let a file miss its bonus for want "
              "of the letter \"I\"; counting them would reward nothing in particular.",
          low=0.0, high=1.0),
        E("meaning_warm_up", "meaning", "Load the model when the window opens",
          "The model that reads your question is loaded in the background as the window opens, so the first search "
          "by meaning does not wait for it. It takes about 600 MB of memory while the window is open.",
          why="Loading takes a few seconds on a fast computer and longer on a slow disk; a search pressed before "
              "it is done shows a wait window. \"auto\" loads it only when the index holds sketches of meaning.",
          everyday=False, choices=(("auto", "when the index has sketches"), ("yes", "always"), ("never", "never"))),
        E("looks_gpu", "looks", "Look at pictures on the graphics card",
          "Use the graphics card for the picture model when the runtime can use one.",
          why="Opening and scaling the pictures costs more than the model, so the card helps less here than for text; "
              "it does no harm.", choices=YES_NO),
        E("looks_readers", "looks", "Readers side by side",
          "How many pictures are opened and sketched at once. 0 works it out from the cores, at most 8.",
          why="Opening and scaling pictures is the slow part, and several readers share it.", low=0, high=32, zero="work it out"),
        E("looks_after_index", "looks", "Look at pictures as part of updating",
          "Updating the index goes on to sketch new pictures, after the sketches of meaning.", choices=YES_NO),
        E("rename_pattern", "naming", "The name pattern",
          "What a renamed picture is called. Fields: {caption} the sentence about it, {taken} the day it was taken "
          "(or saved), {time}, {camera}, {folder}, {original} its name now, {n} a number, {ext}. The ending is kept "
          "unless {ext} is named.",
          why="\"{caption} {taken}\" gives names like \"Two dogs on snowy porch 2024-01-13.jpg\". Put {original} in to keep "
              "the old name in sight.", everyday=True),
        E("watch_every_minutes", "tray", "Update the index by itself, every (minutes)",
          "While the tray runs, the folders are scanned again this often, and once when the tray starts; what changed "
          "is read, then OCR, meaning and looks for what is new, gently (a quarter of the cores). 0 turns this off; "
          "the tray's menu has \"Update now\" and \"Pause updates\" either way.",
          why="Once an hour keeps the index current without the machine ever feeling busy. A run you start yourself "
              "in the window goes at full speed and may stop the tray's run to get its turn."),
        E("hotkey", "tray", "The search key",
          "Written the usual way: Win+Shift+F, Ctrl+Alt+D, Ctrl+Shift+Space. Empty means no key; the box can still be "
          "brought up with the tray icon or the command dugup popup. Takes effect when the tray is next started.",
          why="Win+Shift+F is free on a stock Windows; Ctrl+Alt+Space is often taken by dictation tools. If another program has the key, the tray says so when it starts."),
        E("caption_source", "naming", "Where captions come from",
          "\"model\": the pinned captioning model, fetched on first use. \"server\": a local model server of your own "
          "(not built yet; the seam is here for it).",
          choices=(("model", "the pinned model"), ("server", "a local model server (not built yet)"))),
        E("caption_gpu", "naming", "Write captions on the graphics card",
          "Use the graphics card for the captioning model when the runtime can use one.",
          why="It is the slowest model here: seconds a picture on a processor, much less on a card.", choices=YES_NO),
        E("meaning_after_index", "meaning", "Sketch as part of updating",
          "Updating the index goes on to sketch what is new, after reading pictures.",
          why="One button does it all. Turn it off to sketch at a time of your choosing with `dugup meaning fill`.",
          choices=YES_NO),
        E("ocr_dpi", "pictures", "Sharpness for scanned pages",
          "A scanned PDF page is drawn at this many dots per inch before it is read. Higher is slower and reads small print better.",
          low=50, high=600, unit="dpi"),
        E("ocr_max_pixels", "pictures", "Largest picture side",
          "A larger picture is scaled down to this before reading.", low=1, unit="pixels"),
        E("ocr_tiny_pixels", "pictures", "Tiny pictures",
          "Pictures smaller than this (icons, buttons) are read last. 0 turns this off.", low=0, unit="pixels", zero="off"),
        E("ocr_seconds", "pictures", "Time for one page",
          "How long one page or picture may take before it is set aside for a second, longer try.", low=1, unit="seconds"),
        E("ocr_memory_mb", "pictures", "Memory for the reader",
          "Memory the picture reader may use. 0 means no limit, which the reader needs on most machines.", low=0, unit="MB", zero="no limit"),
        E("ocr_failures_in_a_row", "pictures", "Pause after failures",
          "This many pages failing one after another pause the run: it looks like trouble with the reader, not the pictures.",
          low=1, unit="pages"),
        # -- speed
        E("content_marks", "speed", "Recognise the same file in several places",
          "Each file that is read gets a mark of its exact content. Files with the same mark are the same file, "
          "whatever they are called and wherever they lie.",
          why="A search then shows such a file once, with its other places listed. It costs reading each file one "
              "more time from start to end, which is quick next to reading its words.", choices=YES_NO),
        E("cleanup_protect", "search", "Folders never proposed for removal",
          "Folders, separated by \";\", whose files the duplicates report (dugup duplicates) never proposes removing. "
          "A live backup of this computer on a cloud drive belongs here: removing a file there can remove it here too.",
          why="The report only lists; you do the removing. But it should never point you at a folder where a removal "
              "reaches further than you can see."),
        E("cleanup_prefer", "search", "Folders that are the proper home of their files",
          "Folders, separated by \";\". When the duplicates report must choose which copy to keep, a copy in one of "
          "these wins; otherwise the copy in the larger set of files, then the one higher up.",
          why="Four folders may hold the same 23 files, and only you know which one is where they belong."),
        E("main_place", "search", "Where your own copy of a file is",
          "When the same file is in several places, a search shows the one below this folder or drive (for example C:\\) "
          "and lists the others under it. Empty lets DugUp work it out: on Windows, the drive your own folders are on.",
          why="A file on your own drive and its backup on a cloud drive are one file to you; the one on your own "
              "drive is the one you work on, and the quickest to open."),
        E("read_once", "speed", "Read the same file only once",
          "A file whose content was already read in another place takes its text, and what was read from its "
          "pictures, from there. It is not read again.",
          why="Backups and copies then cost almost nothing to index. It needs the setting above it to be on.", choices=YES_NO),
        E("index_readers", "speed", "Files read side by side",
          "How many files are read at the same time when the index is updated. 0 lets DugUp work it out; 1 reads one at a time.",
          why="Side by side, one slow file (a large PDF, or one that must first come down from a cloud drive) does not "
              "hold up the rest. More readers make the computer busier while the index is updated.",
          everyday=True, low=0, high=32, unit="readers", suggest=_index_readers, zero="worked out by DugUp"),
        E("ocr_fast_pages", "speed", "Go fast from this many pages",
          "When pictures and scans are read without saying fast or gentle (after an index update, or the plain command), "
          "the run goes fast once at least this many pages wait. 0 means never by itself.",
          why="A gentle run reads one page at a time and keeps the computer pleasant, but a drive's worth of pages would "
              "take it all night. Fast is several times quicker and is what a long wait deserves.",
          everyday=True, low=0, high=1_000_000, unit="pages", zero="never by itself"),
        E("ocr_fast_readers", "speed", "Readers in a fast run",
          "How many picture readers work side by side in a fast run. 0 lets DugUp work it out.",
          why="More readers finish sooner and make the computer busier while they run. The usual, gentle run uses one.",
          everyday=True, low=0, high=64, unit="readers", suggest=_readers, zero="worked out by DugUp"),
        E("ocr_gpu_readers", "speed", "Readers sharing the graphics card",
          "How many readers share the graphics card in a fast run. 0 lets DugUp work it out (at most 8).",
          low=0, high=64, unit="readers", zero="worked out by DugUp"),
        E("ocr_reader_threads", "speed", "Processor threads per reader",
          "How many processor threads each reader in a fast run may use.", low=1, high=64, unit="threads"),
        E("ocr_reader_memory_mb", "speed", "Memory counted per reader",
          "Memory allowed for each reader when working out how many fit. It does not limit a reader.", low=0, unit="MB"),
        E("batch_files", "speed", "Files per step", "How many files are taken from the queue at a time.", low=1, unit="files"),
        # -- disk
        E("max_index_mb", "disk", "Spending limit for the index",
          "Indexing and reading stop when the index is larger than this. 0 means no limit.",
          why="An index of a large disk can reach several GB. A limit means it can never surprise you.",
          everyday=True, low=0, unit="MB", suggest=_limit, zero="no limit"),
        E("min_free_mb", "disk", "Space always kept free",
          "A run stops when the disk that holds the index has less than this free. 0 turns the check off.", low=0, unit="MB", zero="not checked"),
        E("text_compression", "disk", "Compress stored text",
          "Stored text is compressed, which makes the index much smaller and does not affect searching.",
          choices=(("zlib", "compressed (smaller index)"), ("none", "not compressed"))),
        E("compact_at_fraction", "disk", "Tidy the index by itself",
          "After a run the index is rewritten to give space back when at least this share of it is unused. 0 means never by itself.",
          low=0.0, high=1.0, zero="never by itself"),
        E("compact_min_mb", "disk", "...when at least this much is unused", "The least unused space worth tidying for.",
          low=0, unit="MB"),
        # -- search
        E("near_words", "search", "Words close together",
          "Files where your words stand within this many words of each other are listed above files where they are far apart.",
          low=1, high=200, unit="words"),
        E("missing_retention_days", "search", "Keep files that have gone",
          "A file that can no longer be found stays searchable, marked as missing, for this many days.",
          why="A disconnected drive should not empty your index. Deleted files should not linger for ever.",
          everyday=True, low=0, unit="days"),
        # -- plain words
        E("intent_reader", "words", "Who reads a sentence",
          "Built-in rules are instant and need nothing. A language model is optional and must be downloaded first.",
          why="In our tests the rules were more reliable than the models tried. The model is there for those who want to experiment.",
          everyday=True, choices=(("rules", "the built-in rules"), ("model", "a language model"))),
        E("intent_url", "words", "A model server you run yourself",
          "The address of a model server that is already running, for example http://127.0.0.1:1234. Empty: not used."),
        E("intent_model", "words", "Model name on that server", "The model's name, when that server needs one."),
        E("intent_allow_remote", "words", "Allow a server on another machine",
          "A server on another machine is sent what you type. Off unless you turn it on.", choices=YES_NO),
        E("intent_server", "words", "Model server program", "A model server program for DugUp to start itself. Empty: the installed one."),
        E("intent_model_file", "words", "Model file", "The model file that server should load."),
        E("intent_gpu_layers", "words", "Model layers on the graphics card",
          "How much of the language model runs on the graphics card. 0 keeps it on the processor.", low=0, unit="layers", zero="none (the processor)"),
        E("intent_seconds", "words", "Time for one sentence", "How long the model may take to read one sentence.", low=0.1, unit="seconds"),
        E("intent_start_seconds", "words", "Time to load the model", "How long the model server may take to start.", low=0.1, unit="seconds"),
        # -- files
        E("language", "files", "Language of your documents", "Only English (en) is set up for reading pictures in this version."),
        E("chunk_chars", "files", "Size of a piece of text",
          "Long text is cut into pieces of about this many characters. A change means every file is read again.", low=50, unit="characters"),
        E("chunk_overlap", "files", "Overlap between pieces",
          "Neighbouring pieces share this many characters, so a phrase on a boundary is still found.", low=0, unit="characters"),
        E("max_text_bytes", "files", "Largest plain-text file read whole",
          "A plain-text file is read up to this size; the rest is reported as unread.", low=1, unit="bytes"),
        E("max_pdf_pages", "files", "Most pages read from one PDF", "Pages beyond this are reported as unread.", low=1, unit="pages"),
        E("max_member_bytes", "files", "Largest part of an Office file",
          "One part inside a Word, Excel or PowerPoint file is read up to this size.", low=1, unit="bytes"),
        E("full_page_image_fraction", "files", "What counts as a scanned page",
          "A picture covering at least this share of a PDF page marks the page as a scan.", low=0.01, high=1.0),
        E("garbled_fraction", "files", "What counts as unreadable text",
          "A PDF page whose text has at least this share of unusable characters is read by OCR instead.", low=0.01, high=1.0),
        E("figure_fraction", "files", "Pictures inside typed pages",
          "A typed page with a picture covering at least this share is also read by OCR, for the words in the picture. 0 turns it off.",
          low=0.0, high=1.0, zero="off"),
        # -- safety
        E("isolate_readers", "safety", "Read files in a separate process",
          "Leave on. Off is for finding faults: a bad file could then hang or crash the program.", choices=YES_NO),
        E("reader_seconds", "safety", "Time for one file", "How long one file may take, before the allowance for its size.",
          low=0.1, unit="seconds"),
        E("reader_seconds_per_mb", "safety", "Extra time per megabyte", "Added for each megabyte of the file.", low=0, unit="seconds"),
        E("reader_seconds_cap", "safety", "Longest time for any file", "Never more than this for one file.", low=0.1, unit="seconds"),
        E("reader_memory_mb", "safety", "Memory for reading a file", "Memory a file reader may use. 0 means no limit.", low=0, unit="MB", zero="no limit"),
        E("retry_patience", "safety", "Second tries",
          "A file or page that ran out of time gets one more try, last of all, with its time multiplied by this. 1 means no second try.",
          low=0, unit="times"),
        E("reader_failures_in_a_row", "safety", "Pause after failures",
          "This many crashes or timeouts one after another pause the run. 0 means never.", low=0, unit="files", zero="never"),
    )


ENTRIES: Dict[str, Entry] = {entry.name: entry for entry in _entries()}
NOT_HERE = frozenset({"extensions"})      # a table, not one value: edited in config.toml, described in the README
_KINDS = {f.name: str(f.type) for f in fields(Settings) if f.name not in NOT_HERE}
_DEFAULTS = Settings()


def kind(name: str) -> str:
    """"bool", "int", "float" or "str"."""
    return _KINDS[name]


def default(name: str) -> object:
    return getattr(_DEFAULTS, name)


def group(key: str) -> Group:
    return next(g for g in GROUPS if g.key == key)


def in_group(key: str, everyday_only: bool = False) -> List[Entry]:
    return [e for e in ENTRIES.values() if e.group == key and (e.everyday or not everyday_only)]


# ---- words to values and back -----------------------------------------------------------------------------------

_TRUE, _FALSE = {"yes", "y", "true", "on", "1"}, {"no", "n", "false", "off", "0"}


def parse(name: str, text: object) -> "object | Failure":
    """What a person typed (or a value) as this setting's value, or why it will not do."""
    entry = ENTRIES.get(name)
    if entry is None:
        return Failure(FailureReason.UNSUPPORTED, f"there is no setting called {name!r} (dugup config list shows them all)")
    wanted = kind(name)
    value: object = text
    if isinstance(text, str):
        typed = text.strip()
        try:
            if wanted == "bool":
                if typed.casefold() not in _TRUE | _FALSE:
                    return Failure(FailureReason.OTHER, f"{name} takes yes or no")
                value = typed.casefold() in _TRUE
            elif wanted == "int":
                value = int(typed.replace(",", "").replace("_", ""))
            elif wanted == "float":
                value = float(typed.replace(",", ""))
            else:
                value = text if entry.choices else typed
        except ValueError:
            return Failure(FailureReason.OTHER, f"{name} takes a {'whole ' if wanted == 'int' else ''}number")
    if wanted == "bool" and not isinstance(value, bool):
        return Failure(FailureReason.OTHER, f"{name} takes yes or no")
    if wanted in ("int", "float") and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return Failure(FailureReason.OTHER, f"{name} takes a number")
    if wanted == "int" and isinstance(value, float):
        if value != int(value):
            return Failure(FailureReason.OTHER, f"{name} takes a whole number")
        value = int(value)
    if wanted == "float":
        value = float(value)        # type: ignore[arg-type]
    if wanted == "str" and not isinstance(value, str):
        return Failure(FailureReason.OTHER, f"{name} takes text")
    if entry.choices:
        allowed = [choice for choice, _ in entry.choices]
        if isinstance(value, str) and value.strip().casefold() in [str(a).casefold() for a in allowed if isinstance(a, str)]:
            value = next(a for a in allowed if isinstance(a, str) and a.casefold() == value.strip().casefold())
        if value not in allowed:
            said = ", ".join(("yes" if a is True else "no" if a is False else repr(a) if a == "" else str(a)) for a in allowed)
            return Failure(FailureReason.OTHER, f"{name} takes one of: {said}")
    if wanted in ("int", "float"):
        unit = f" {entry.unit}" if entry.unit else ""
        if entry.low is not None and value < entry.low:        # type: ignore[operator]
            return Failure(FailureReason.OTHER, f"{name} must be at least {entry.low:g}{unit}")
        if entry.high is not None and value > entry.high:      # type: ignore[operator]
            return Failure(FailureReason.OTHER, f"{name} must be at most {entry.high:g}{unit}")
    return value


def apply(settings: Settings, changes: Mapping[str, object]) -> "Settings | Failure":
    """The settings with these changes, each parsed and then checked together (some depend on each other)."""
    parsed: Dict[str, object] = {}
    for name, text in changes.items():
        value = parse(name, text)
        if isinstance(value, Failure):
            return value
        parsed[name] = value
    try:
        return replace(settings, **parsed)
    except ValueError as exc:
        return Failure(FailureReason.OTHER, str(exc))


def show(name: str, value: object) -> str:
    """A value the way a person reads it."""
    entry = ENTRIES[name]
    for choice, words in entry.choices:
        if choice == value and type(choice) is type(value):
            return words
    if isinstance(value, bool):
        return "yes" if value else "no"
    if entry.zero and isinstance(value, (int, float)) and value == 0:
        return entry.zero
    if isinstance(value, float):
        text = f"{value:g}"
    elif isinstance(value, int):
        text = f"{value:,}"
    else:
        text = str(value) if value != "" else "(empty)"
    return f"{text} {entry.unit}".strip() if entry.unit and not isinstance(value, str) else text


def suggestions(machine: Machine) -> Dict[str, Tuple[object, str]]:
    """For each setting whose best value depends on the machine: (value, the reason in words)."""
    found: Dict[str, Tuple[object, str]] = {}
    for entry in ENTRIES.values():
        if entry.suggest is not None:
            value, reason = entry.suggest(machine)
            checked = parse(entry.name, value)
            if not isinstance(checked, Failure):       # a rule may never hand out a value the setting would refuse
                found[entry.name] = (checked, reason)
    return found


# ---- config.toml ------------------------------------------------------------------------------------------------

def to_toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".einf") else text + ".0"
    text = str(value)
    if "'" not in text and "\n" not in text and "\r" not in text:
        return f"'{text}'"            # a literal string: a Windows path needs no doubled backslashes
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def write(config: Path, changes: Mapping[str, Optional[object]]) -> None:
    """Set these keys in config.toml (None takes a key out, so its default applies again). Everything else
    in the file, comments and tables included, is left as it is. Written whole to a new file and swapped in,
    so a crash cannot leave half a file."""
    config = Path(config)
    try:
        lines = config.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    left = dict(changes)
    kept: List[str] = []
    in_table = False

    def pending() -> List[str]:
        out = [f"{key} = {to_toml(value)}" for key, value in left.items() if value is not None]
        left.clear()
        return out

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and not in_table:
            kept.extend(pending())          # top-level keys must stand before the first table
            in_table = True
        key = stripped.split("=")[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if not in_table and key in changes:
            if key in left:
                value = left.pop(key)
                if value is not None:
                    kept.append(f"{key} = {to_toml(value)}")
            continue                        # a second line for the same key would make the file invalid: dropped
        kept.append(line)
    if not in_table:
        kept.extend(pending())
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.with_name(config.name + ".new")
    temporary.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    os.replace(temporary, config)


def changed(settings: Settings) -> Dict[str, object]:
    """The settings that differ from their defaults."""
    return {name: getattr(settings, name) for name in ENTRIES if getattr(settings, name) != default(name)}
