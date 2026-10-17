"""Every optional download in one place: what each is for, how big, what is already here, and fetching them all.

DugUp works without any of them. Each one is fetched on first use otherwise; this is for fetching them all
at once (the installer offers it), so a person sets up once and is done. The list is honest about size and
about free space before anything starts, and every file is still pinned and checked as models.fetch does.

"Everything" means the features DugUp uses by default. The plain-words language model is experimental and
off by default (the built-in rules are better), so it is offered on its own and never part of "everything".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import embedding_models, models
from .models import Pinned, megabytes
from .shapes import Failure, FailureReason


@dataclass(frozen=True)
class Download:
    key: str                      # "meaning", "looks", "captions", "words"
    title: str
    enables: str                  # what it makes possible, in a line
    folder: Path
    items: Tuple[Pinned, ...]
    everyday: bool = True         # part of "everything"
    plan: Optional[models.Plan] = None    # the plain-words model and its server (installed, not only fetched)

    @property
    def size(self) -> int:
        return sum(item.size for item in self.items)

    def missing(self) -> int:
        """Bytes still to come: what is not here yet, less any part already downloaded."""
        if self.plan is not None and models.read_record(self.folder) is not None:
            return 0                       # installed and set up
        left = 0
        for item in self.items:
            final, part = self.folder / item.name, self.folder / (item.name + ".part")
            if final.is_file() and final.stat().st_size == item.size:
                continue
            have = part.stat().st_size if part.is_file() else 0
            left += item.size - min(have, item.size)
        if self.plan is not None and models.read_record(self.folder) is None and left == 0:
            left = 1                       # downloaded but not set up yet: still to do
        return left

    @property
    def present(self) -> bool:
        return self.missing() == 0


def available(layout, settings, plain_words: bool = True) -> List[Download]:
    """Every optional download for this data folder and these settings (the meaning model follows the setting)."""
    choice = settings.meaning_model
    found = [
        Download("meaning", "Search by meaning", "find passages that say the same thing in other words",
                 layout.models / "meaning", embedding_models.BY_CHOICE[choice]),
        Download("looks", "Pictures by likeness", "find pictures like another one, or of something you name",
                 layout.models / "looks", embedding_models.LOOKS_BY_CHOICE["base"]),
        Download("captions", "Naming pictures", "rename pictures from what they show",
                 layout.models / "captions", embedding_models.CAPTIONS_BY_CHOICE["base"]),
    ]
    if plain_words:
        plan = models.plan_for(layout.models / "intent")
        if not isinstance(plan, Failure):
            found.append(Download("words", "Plain-words model (experimental)",
                                  "a language model reads sentences instead of the built-in rules (off by default)",
                                  plan.folder, plan.items, everyday=False, plan=plan))
    return found


def chosen_in_config(config: Path, name: str) -> bool:
    """True when config.toml sets `name` itself (the person's own choice), not left to the default."""
    from . import settings as settings_module
    reader = getattr(settings_module, "_toml", None)
    if reader is None:
        return False
    try:
        with open(config, "rb") as handle:
            return name in reader.load(handle)
    except (OSError, ValueError):
        return False


def settle_meaning_model(config: Path, settings, look: Callable[[], object]) -> Tuple[object, str]:
    """Before downloading, choose the meaning model for this machine, so the right one is fetched. A choice
    already in config.toml is the person's and is kept. Returns (settings, a sentence saying what and why, or "")."""
    from dataclasses import replace
    from . import catalogue
    if chosen_in_config(config, "meaning_model"):
        return settings, ""
    value, why = catalogue.suggestions(look()).get("meaning_model", (settings.meaning_model, ""))
    catalogue.write(config, {"meaning_model": value})
    size = size_text(sum(item.size for item in embedding_models.BY_CHOICE[value]))
    return replace(settings, meaning_model=value), (f"Search by meaning will use the {value} model ({size}): {why}. "
                                                    "This can be changed in Settings.")


def everything(downloads: Sequence[Download]) -> List[Download]:
    return [d for d in downloads if d.everyday]


@dataclass
class Room:
    needed: int
    free: Optional[int]
    keep_free: int

    @property
    def enough(self) -> bool:
        return self.free is None or self.free - self.needed >= self.keep_free


def room_for(chosen: Sequence[Download], free: Optional[int], keep_free: int) -> Room:
    needed = sum(d.missing() for d in chosen)
    for d in chosen:
        if d.plan is not None and not d.present:
            needed += 3 * d.plan.runtime.size          # the server is unpacked beside its download
    return Room(needed, free, keep_free)


def size_text(count: int) -> str:
    """A size the way Windows shows it (1 MB = 1,048,576 bytes), in GB from 1 GB up."""
    if count >= 1024 ** 3:
        return f"{count / 1024 ** 3:,.1f} GB"
    return megabytes(count)


def describe(chosen: Sequence[Download], room: Room) -> str:
    """What would be downloaded, how big, and whether it fits, in words a person reads before saying yes."""
    lines = []
    for d in chosen:
        left = d.missing()
        state = " (already here)" if not left else (f" ({size_text(left)} left)" if left < d.size else "")
        lines.append(f"  {d.title}: {size_text(d.size)}{state}. {d.enables[0].upper()}{d.enables[1:]}.")
    lines.append(f"Still to download: {size_text(room.needed)}" if room.needed else "Nothing to download: all here.")
    if room.free is not None:
        lines.append(f"Free on that drive: {size_text(room.free)}; DugUp keeps at least {size_text(room.keep_free)} free.")
        if not room.enough:
            lines.append("That is not enough room. Free some space, or choose fewer.")
    lines.append("Each file comes from one fixed address and is checked against its known checksum before use.")
    return "\n".join(lines)


@dataclass
class Fetched:
    done: List[str]                       # keys fetched (or already here)
    failed: List[Tuple[str, str]]         # (key, why)
    stopped: bool = False


def fetch(chosen: Sequence[Download], tell: Optional[Callable[[int, int, str], None]] = None,
          stop=None, free: Optional[Callable[[], Optional[int]]] = None, keep_free: int = 0,
          fetcher: Callable = models.fetch, installer: Callable = models.install) -> "Fetched | Failure":
    """Fetch every chosen download. `tell(done_bytes, total_bytes, title)` follows the whole. A download that
    fails is reported and the rest go on; a stop keeps what came so far (it is continued next time). Refuses
    up front, changing nothing, when there is not room."""
    room = room_for(chosen, free() if free is not None else None, keep_free)
    if not room.enough:
        return Failure(FailureReason.OTHER, f"there is not enough room: {megabytes(room.needed)} is needed, "
                                            f"{megabytes(room.free or 0)} is free and {megabytes(keep_free)} is kept free")
    total = sum(d.missing() for d in chosen)
    base = 0
    result = Fetched([], [])
    tell = tell or (lambda done, whole, title: None)
    for d in chosen:
        if stop is not None and stop.is_set():
            result.stopped = True
            break
        before = d.missing()
        if d.plan is not None:
            seen: dict = {}                               # each file's own progress, so the whole only moves forward

            def installing(p, seen=seen, d=d, before=before):
                if p.phase == "download":
                    seen[p.root] = p.done
                tell(base + min(sum(seen.values()), before), total, d.title)

            got = installer(d.folder, installing, stop, plan=d.plan, free_bytes=free, keep_free=keep_free)
            outcome = got if isinstance(got, Failure) else None
        else:
            outcome = None
            counted = 0                                   # bytes of this download already told
            for item in d.items:
                final, part = d.folder / item.name, d.folder / (item.name + ".part")
                if final.is_file() and final.stat().st_size == item.size:
                    had, left = item.size, 0
                else:
                    had = min(part.stat().st_size, item.size) if part.is_file() else 0
                    left = item.size - had

                def progress(p, counted=counted, had=had, size=item.size):
                    tell(base + counted + max(0, min(p.done, size) - had), total, d.title)

                got = fetcher(item, d.folder, progress, stop)
                if isinstance(got, Failure):
                    outcome = got
                    break
                counted += left
        if isinstance(outcome, Failure):
            if outcome.reason == FailureReason.STOPPED:
                result.stopped = True
                break
            result.failed.append((d.key, outcome.detail))
        else:
            result.done.append(d.key)
        base += before
        tell(min(base, total), total, d.title)
    return result

