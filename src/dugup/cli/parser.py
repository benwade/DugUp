"""The argument parser: every command and option, with its help line."""

from __future__ import annotations

import argparse
from .. import __version__
from ..filesource import SUGGESTED_EXCLUDES
from ..shapes import POLICIES, POLICY_OPEN
from ..store import SORT_COLUMNS


# ---- the parser ------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="the program's data folder (default: environment, then beside the program, then per-user folder)")
    common.add_argument("--never-write", action="append", default=[], metavar="FOLDER",
                        help="a folder the program must never write to (repeatable)")
    common.add_argument("--allow-synced", action="store_true", help="allow a data folder that looks cloud-synced")

    parser = argparse.ArgumentParser(prog="dugup", description="DugUp: local file search.")
    parser.add_argument("--version", action="version", version=f"dugup {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", parents=[common], help="check that this machine is ready and report what is missing")
    sub.add_parser("init", parents=[common], help="create the data folder and an empty index")

    roots = sub.add_parser("roots", help="choose the folders to index")
    roots_sub = roots.add_subparsers(dest="roots_command", required=True)
    add = roots_sub.add_parser("add", parents=[common], help="add a folder, or change its policy and rules")
    add.add_argument("path")
    add.add_argument("--policy", choices=POLICIES, default=POLICY_OPEN,
                     help="open: names and text (default). names_only: file names only, no text is kept")
    add.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                     help="skip files and folders matching this pattern, for example .git or *.tmp (repeatable)")
    add.add_argument("--include", action="append", default=[], metavar="PATTERN",
                     help="index only what matches this pattern (repeatable)")
    add.add_argument("--suggested-excludes", action="store_true",
                     help="also skip: " + ", ".join(SUGGESTED_EXCLUDES))
    suggest = roots_sub.add_parser("suggest", parents=[common],
                                   help="look through a folder and list what may not be worth indexing (changes nothing)")
    suggest.add_argument("path")
    level = roots_sub.add_parser("level", parents=[common],
                                 help="how far indexing goes in one folder (a subfolder's level wins over the folder above it)")
    level.add_argument("path", help="an indexed folder, or any folder inside one")
    level.add_argument("level",
                       help="names: file names only. Or any of these joined by +: words (text of documents), ocr (words in "
                            "pictures and scans), meaning, alike (pictures by what they show); for example words+ocr. "
                            "meaning alone is short for everything, the default. inherit: follow the folder above again")
    look = roots_sub.add_parser("survey", parents=[common],
                                help="what a folder holds and what indexing it would cost, folder by folder (opens no file, changes nothing)")
    look.add_argument("path")
    look.add_argument("--rows", type=int, default=30, help="how many folders to list, largest first (default 30)")
    alike = roots_sub.add_parser("copies", parents=[common],
                                 help="folders that look like copies of other folders, from names and sizes (opens no file, changes nothing)")
    alike.add_argument("path")
    alike.add_argument("--rows", type=int, default=40, help="how many sets to list, most extra files first (default 40)")
    suggest.add_argument("--apply-safe", action="store_true",
                         help="skip everything in the 'safe to skip' list (downloaded requirements, tool data)")
    exclude = roots_sub.add_parser("exclude", parents=[common], help="skip more files or folders in an indexed folder")
    exclude.add_argument("path")
    exclude.add_argument("patterns", nargs="+", metavar="PATTERN",
                         help="a folder or file name (skipped wherever it appears), a path below the folder, or a pattern such as *.tmp")
    exclude.add_argument("--undo", action="store_true", help="stop skipping these patterns instead")
    roots_sub.add_parser("list", parents=[common], help="show the folders and when each was last scanned")
    remove = roots_sub.add_parser("remove", parents=[common], help="stop indexing a folder and remove its files from the index")
    remove.add_argument("path")
    remove.add_argument("--keep-index", action="store_true", help="forget the folder but keep what was indexed")

    index = sub.add_parser("index", parents=[common], help="bring the index up to date (Ctrl+C stops cleanly; run again to continue)")
    index.add_argument("--root", action="append", default=None, metavar="FOLDER", help="only this folder (repeatable)")
    index.add_argument("--retry-failed", action="store_true", help="try the files that failed last time again")
    index.add_argument("--no-ocr", action="store_true",
                       help="stop after the index is up to date; do not go on to read pictures and scans (dugup ocr does that later)")
    index.add_argument("--no-meaning", action="store_true",
                       help="do not go on to sketch meaning after reading (dugup meaning fill does that later)")
    index.add_argument("--no-looks", action="store_true",
                       help="do not go on to look at new pictures after reading (dugup alike fill does that later)")
    choice = index.add_mutually_exclusive_group()
    choice.add_argument("--reread", action="store_true", help="after a settings change: read every file again")
    choice.add_argument("--keep", action="store_true", help="after a settings change: keep what is stored as it is")

    ocr = sub.add_parser("ocr", parents=[common],
                         help="read the words in pictures and scanned pages (Ctrl+C stops cleanly; run again to continue)")
    ocr.add_argument("--root", action="append", default=None, metavar="FOLDER", help="only this folder (repeatable)")
    ocr.add_argument("--minutes", type=float, default=None, metavar="N", help="stop after about N minutes")
    ocr.add_argument("--retry-failed", action="store_true", help="try the pages OCR failed on again")
    ocr.add_argument("--folder", default=None, metavar="FOLDER",
                     help="only what lies in this folder: an indexed folder or any folder inside one")
    ocr.add_argument("--fast", action="store_true",
                     help="several readers side by side; the computer is busier while it runs")
    ocr.add_argument("--readers", type=int, default=0, metavar="N", help="fast, with exactly N readers")
    ocr.add_argument("--gentle", action="store_true",
                     help="one page at a time, even when many wait (the usual run goes fast from ocr_fast_pages pages)")
    ocr.add_argument("--gpu", action="store_true", default=None,
                     help="read on the graphics card, when the OCR runtime can use one (see dugup doctor)")
    ocr.add_argument("--no-gpu", dest="gpu", action="store_false", help="read on the processor even if config.toml says ocr_gpu = true")
    ocr.add_argument("--again", action="store_true",
                     help="with --folder: read its pictures and scans afresh, even where that was done before")
    ocr.add_argument("--reread-old", action="store_true",
                     help="read again the pages that an older version of the OCR engine read")

    space = sub.add_parser("space", parents=[common], help="how much disk the index uses, and its spending limit")
    space.add_argument("--compact", action="store_true",
                       help="give back the disk space that removed and re-read text left in the index")
    space.add_argument("--limit", type=int, default=None, metavar="MB",
                       help="stop indexing and OCR when the index is larger than this many megabytes (0: no limit)")

    window = sub.add_parser("window", parents=[common], help="open the search window")
    window.add_argument("--search", metavar="WORDS", help="search for these words as soon as the window opens")
    sub.add_parser("remote", parents=[common], help="a small window that starts the tray, brings up the search box, frees the graphics card, quits")
    tray = sub.add_parser("tray", parents=[common], help="stay in the tray, with a key that brings up a search box anywhere")
    tray.add_argument("--startup", choices=("add", "remove"), help="make the tray start when you log in, or stop that")
    tray.add_argument("--quit", action="store_true", help="end the tray that is running")
    tray.add_argument("--no-icon", action="store_true", help="no icon in the tray (the key and `dugup popup` still work)")
    sub.add_parser("popup", parents=[common], help="bring up the running tray's search box")

    config = sub.add_parser("config", help="show and change settings (each with a line of guidance)")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    listing = config_sub.add_parser("list", parents=[common], help="the everyday settings and their values")
    listing.add_argument("--all", action="store_true", help="every setting, the advanced ones too")
    getting = config_sub.add_parser("get", parents=[common], help="one setting: its value, what it does, its choices")
    getting.add_argument("name")
    setting = config_sub.add_parser("set", parents=[common], help="change one setting (checked before it is saved)")
    setting.add_argument("name")
    setting.add_argument("value", nargs="+")
    resetting = config_sub.add_parser("reset", parents=[common], help="put one setting back to its default")
    resetting.add_argument("name")

    models_parser = sub.add_parser("models", help="the optional language model for plain-words search")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("status", parents=[common], help="what is installed, and what an install would download")
    install = models_sub.add_parser("install", parents=[common],
                                    help="download the model and its runtime (Ctrl+C stops; run again to continue)")
    install.add_argument("--yes", action="store_true", help="go ahead; without this the plan is shown and nothing is downloaded")
    install.add_argument("--model", choices=["small", "larger"], default="small",
                         help="small: quick, 0.3 GB. larger: reads sentences much better, 2.2 GB, about 3 GB of memory, "
                              "several seconds a sentence without a graphics card")
    models_sub.add_parser("remove", parents=[common], help="delete the downloaded model and runtime")

    ask = sub.add_parser("ask", parents=[common],
                         help="show how a sentence in plain words would fill in the search form, and how long that took")
    ask.add_argument("sentence", nargs="+")
    ask.add_argument("--reader", choices=["rules", "model"], default=None,
                     help="try the built-in rules or the installed model, whatever config.toml says")

    search = sub.add_parser("search", parents=[common], help="find files by the words in them or in their names")
    search.add_argument("words", nargs="+")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--sort", choices=sorted(SORT_COLUMNS), default="rank")
    search.add_argument("--desc", action="store_true", help="largest (or last) first")
    search.add_argument("--exact", action="store_true",
                        help="only the very word forms typed: education then does not find educate")
    search.add_argument("--every-copy", action="store_true",
                        help="list every file; the usual is to show a file kept in several places once, with its other places")
    search.add_argument("--here", action="store_true",
                        help="only files that are here now: not ones gone since the last scan, nor ones on a drive not reachable now")
    search.add_argument("--meaning", action="store_true",
                        help="find passages by what they are about rather than by the words: a question in your own words "
                             "(needs sketches: dugup meaning fill)")
    search.add_argument("--both", action="store_true",
                        help="the words and the meaning together: a name in the question counts, and so does the sense")
    search.add_argument("--looks", action="store_true",
                        help="pictures that look like what the sentence says (needs sketches of pictures: dugup alike fill)")
    search.add_argument("--context", type=int, default=60, help="characters shown on each side of a match")

    dup = sub.add_parser("duplicates", parents=[common],
                         help="the same file kept in several places on one drive, with the copy to keep marked (changes nothing)")
    dup.add_argument("drive", help="the drive or folder to look in, for example G:\\ (copies on other drives are not duplicates)")
    dup.add_argument("--protect", action="append", default=[], metavar="FOLDER",
                     help="a folder never proposed for removal (as well as the setting cleanup_protect); may be repeated")
    dup.add_argument("--prefer", action="append", default=[], metavar="FOLDER",
                     help="a folder that is the proper home of its files: a copy there is the one to keep (as well as "
                          "the setting cleanup_prefer); may be repeated")
    dup.add_argument("--rows", type=int, default=50, help="how many sets to show, most space first (default 50)")
    dup.add_argument("--save", metavar="FILE", help="write the whole report to a text file (keep or extra, path, size, files in its folder)")
    dup.add_argument("--remove", action="store_true",
                     help="move every 'extra' to the trash, each after its keeper is found to hold the same bytes now; "
                          "the list is shown and a 'yes' asked for first; needs --record")
    dup.add_argument("--record", metavar="FILE", help="with --remove: a file that will list what was moved and what was left, and why")
    dup.add_argument("--yes", action="store_true", help="with --remove: do not ask; answer yes")
    dup.add_argument("--try-trash", metavar="FILE", dest="try_trash",
                     help="move this one file to the trash and say where it went, to see how the trash behaves here")
    meaning = sub.add_parser("meaning", parents=[common], help="searching by meaning: sketches of what each passage is about")
    meaning_sub = meaning.add_subparsers(dest="meaning_command", required=True)
    gauge = meaning_sub.add_parser("measure", parents=[common],
                                   help="make sketches of a sample of the index with the pinned models and report speed, "
                                        "disk, and what a few questions find (fetches the models; stores nothing)")
    gauge.add_argument("--pieces", type=int, default=2000, help="how many pieces of text to sample (default 2000)")
    gauge.add_argument("--model", choices=("small", "larger", "both"), default="both")
    gauge.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="the graphics card only")
    gauge.add_argument("--no-gpu", dest="gpu", action="store_false", help="the processor only (default: both when the runtime can)")
    gauge.add_argument("--ask", action="append", default=[], metavar="SENTENCE",
                       help="a question to try on the sample, in plain words; may be repeated")
    filling = meaning_sub.add_parser("fill", parents=[common],
                                     help="make sketches of meaning for the files that have none yet, the most recently "
                                          "changed first (Ctrl+C stops; run again to continue)")
    filling.add_argument("--budget", type=float, default=None, metavar="SECONDS", help="stop after this long; run again to continue")
    filling.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="sketch on the graphics card")
    filling.add_argument("--no-gpu", dest="gpu", action="store_false", help="sketch on the processor (default: as the setting meaning_gpu says)")
    meaning_sub.add_parser("status", parents=[common], help="how much of the index has sketches of meaning")
    alike = sub.add_parser("alike", parents=[common], help="pictures by likeness: like one another, or like a sentence (measured first)")
    alike_sub = alike.add_subparsers(dest="alike_command", required=True)
    gauge2 = alike_sub.add_parser("measure", parents=[common],
                                  help="make sketches of a sample of the index's pictures with the pinned picture model and report "
                                       "speed, disk, and what a few sentences or an example picture find (fetches the model; stores nothing)")
    gauge2.add_argument("--pictures", type=int, default=300, help="how many pictures to sample (default 300)")
    gauge2.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="the graphics card only")
    gauge2.add_argument("--no-gpu", dest="gpu", action="store_false", help="the processor only (default: both when the runtime can)")
    gauge2.add_argument("--ask", action="append", default=[], metavar="SENTENCE",
                        help="a sentence to look for in the sample, such as \"a dog on a beach\"; may be repeated")
    gauge2.add_argument("--like", action="append", default=[], metavar="PICTURE",
                        help="a picture whose likes to find in the sample; may be repeated")
    filling2 = alike_sub.add_parser("fill", parents=[common],
                                    help="make sketches of the pictures that have none yet, the most recently changed first, "
                                         "several readers side by side (Ctrl+C stops; run again to continue)")
    filling2.add_argument("--budget", type=float, default=None, metavar="SECONDS", help="stop after this long; run again to continue")
    filling2.add_argument("--readers", type=int, default=None, help="readers side by side (default: the setting looks_readers)")
    filling2.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="on the graphics card")
    filling2.add_argument("--no-gpu", dest="gpu", action="store_false", help="on the processor (default: as the setting looks_gpu says)")
    alike_sub.add_parser("status", parents=[common], help="how many pictures have sketches, and which could not be read")
    twins = alike_sub.add_parser("duplicates", parents=[common],
                                 help="sets of pictures that look nearly the same, with the one to keep; --remove moves the rest to the trash")
    twins.add_argument("--threshold", type=float, default=0.97, help="closeness at and above which pictures count as the same (default 0.97)")
    twins.add_argument("--protect", action="append", default=[], metavar="FOLDER", help="never propose pictures below this folder")
    twins.add_argument("--prefer", action="append", default=[], metavar="FOLDER", help="the copy below this folder is the one kept")
    twins.add_argument("--rows", type=int, default=50, help="how many sets to show")
    twins.add_argument("--remove", action="store_true", help="move the extras to the trash (asks first; needs --record)")
    twins.add_argument("--record", metavar="FILE", help="with --remove: a file that lists what was moved")
    twins.add_argument("--yes", action="store_true", help="with --remove: do not ask")
    liking = alike_sub.add_parser("like", parents=[common], help="pictures in the index that look like a picture")
    liking.add_argument("picture", help="a picture file, in the index or not")
    liking.add_argument("--limit", type=int, default=20)
    liking.add_argument("--offset", type=int, default=0)
    rename = sub.add_parser("rename", parents=[common],
                            help="name the pictures in a folder by what they show (a preview; --apply renames; --undo puts names back)")
    rename.add_argument("folder", nargs="?", help="an indexed folder, or a folder inside one")
    rename.add_argument("--pattern", help="the name pattern (default: the setting rename_pattern, \"{caption} {taken}\")")
    rename.add_argument("--all", dest="every", action="store_true",
                        help="every picture, not only those whose name is a camera's (IMG_..., 20220401_142335, Screenshot ...)")
    rename.add_argument("--no-caption", action="store_true", help="do not write captions; {caption} is then empty and such files are left alone")
    rename.add_argument("--longer", action="store_true", help="ask the model for a longer description (slower; more words to choose from)")
    rename.add_argument("--apply", action="store_true", help="rename as the preview says (needs --record)")
    rename.add_argument("--record", metavar="FILE", help="with --apply: a file that lists every rename, for --undo")
    rename.add_argument("--yes", action="store_true", help="with --apply: do not ask")
    rename.add_argument("--undo", metavar="RECORD", help="put the names in this record back, last first")
    rename.add_argument("--gpu", dest="gpu", action="store_true", default=None)
    rename.add_argument("--no-gpu", dest="gpu", action="store_false")
    download = sub.add_parser("download", parents=[common],
                              help="fetch the optional models now, all at once (sizes and free space shown first)")
    download.add_argument("--list", action="store_true", help="only show what would be downloaded")
    download.add_argument("--yes", action="store_true", help="do not ask")
    download.add_argument("--plain-words", action="store_true",
                          help="include the experimental plain-words language model (off by default)")
    download.add_argument("--only", nargs="+", choices=("meaning", "looks", "captions", "words"), metavar="NAME",
                          help="just these: meaning, looks, captions, words")
    download.add_argument("--window", action="store_true", help="show the downloads in a small window")
    data = sub.add_parser("data", help="where DugUp keeps its index and settings, and moving them beside the program")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    data_sub.add_parser("where", parents=[common], help="the data folder in use, and why that one")
    moving = data_sub.add_parser("move", parents=[common],
                                 help="move an index from the per-user folder into the folder beside the program")
    moving.add_argument("--yes", action="store_true", help="do not ask")
    data_sub.add_parser("keep", parents=[common], help="keep using the per-user folder, and stop being asked")
    assistant = sub.add_parser("mcp", help="read-only search for an AI assistant such as Claude (off unless you turn it on)")
    assistant_sub = assistant.add_subparsers(dest="mcp_command", required=True)
    enable = assistant_sub.add_parser("enable", parents=[common], help="let assistants search (says what that sends first)")
    enable.add_argument("--folders", metavar="FOLDERS",
                        help='only these folders, separated by ";" (an empty string means every indexed folder)')
    enable.add_argument("--yes", action="store_true", help="do not ask")
    assistant_sub.add_parser("disable", parents=[common], help="stop assistants searching (at once)")
    assistant_sub.add_parser("status", parents=[common], help="on or off, which folders, whether the Claude app is connected")
    connect = assistant_sub.add_parser("connect", parents=[common], help="the entry for the Claude desktop app's settings")
    connect.add_argument("--write", action="store_true", help="add it to the app's settings file (a copy of the old one is kept)")
    connect.add_argument("--config", metavar="FILE", help="a different settings file to write to")
    assistant_sub.add_parser("serve", parents=[common], help="run the server (the assistant app starts this itself)")
    status = sub.add_parser("status", parents=[common], help="what is indexed, what is waiting, what needs attention")
    status.add_argument("--unreadable", action="store_true",
                        help="list every file that cannot be read (damaged, or needs a password)")
    status.add_argument("--save", metavar="FILE",
                        help="write that list to a text file (path, a tab, the reason) instead of the screen")
    status.add_argument("--attention", nargs="?", type=int, const=50, default=None, metavar="N",
                        help="list up to N files that failed or were read only in part (default 50)")
    return parser
