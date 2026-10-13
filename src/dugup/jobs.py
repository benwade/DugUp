"""Jobs a worker can be asked to do, and the plain-data forms that cross the process boundary.

Everything that travels between the main program and a worker is JSON: a file record goes in as a
mapping, an Extraction or a Failure comes back as one. Both directions are written here, side by side,
so they cannot drift apart.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .protocols import Extraction, PageInfo, PageVerdict
from .shapes import Failure, FailureReason, FileKind, FileRecord, SegmentIn, SegmentKind

EXTRACT = "dugup.jobs:extract"
CONTENT_MARK = "dugup.jobs:content_mark"
EMBED = "dugup.jobs:embed"
OCR = "dugup.jobs:ocr"
PROBE = "dugup.jobs:probe_pictures"


def path_to_data(path: str) -> str:
    # a path is sent with escapes for bytes that are not valid text (Linux), and restored exactly
    return path.encode("utf-8", "surrogateescape").decode("latin-1")


def path_from_data(data: object) -> str:
    return str(data).encode("latin-1").decode("utf-8", "surrogateescape")


def record_to_data(record: FileRecord) -> Dict[str, Any]:
    return {"path": path_to_data(record.path),
            "size": record.size, "modified": record.modified, "kind": record.kind.value}


def record_from_data(data: Mapping[str, Any]) -> FileRecord:
    path = path_from_data(data["path"])
    return FileRecord(path=path, size=int(data["size"]), modified=float(data["modified"]), kind=FileKind(data["kind"]))


def result_to_data(result: "Extraction | Failure") -> Dict[str, Any]:
    if isinstance(result, Failure):
        return {"failure": result.reason.value, "detail": result.detail}
    return {
        "segments": [[s.page, s.order, s.kind.value, s.text] for s in result.segments],
        "pages": [[p.page, p.verdict.value, p.needs_ocr, p.unread_reason] for p in result.pages],
        "unread": list(result.unread),
    }


def result_from_data(data: Any) -> "Extraction | Failure":
    """What a worker sent back, checked. Anything that is not a well-formed answer is a stated failure."""
    try:
        if "failure" in data:
            return Failure(FailureReason(data["failure"]), str(data["detail"]))
        segments = tuple(SegmentIn(int(p), int(o), SegmentKind(k), str(t)) for p, o, k, t in data["segments"])
        pages = tuple(PageInfo(int(n), PageVerdict(v), bool(ocr), str(reason)) for n, v, ocr, reason in data["pages"])
        return Extraction(segments, pages, tuple(str(u) for u in data.get("unread", ())))
    except (KeyError, TypeError, ValueError) as exc:
        return Failure(FailureReason.CRASHED, f"the worker's answer could not be understood ({type(exc).__name__}: {exc})")


_extractors: Dict[str, Any] = {}


def extract(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one file with the ordinary extractors. Runs inside a worker."""
    from .extractors import default_extractors
    from .settings import from_mapping

    settings_data = payload.get("settings") or {}
    key = repr(sorted(settings_data.items()))
    if key not in _extractors:          # the readers are built once per worker and reused
        _extractors.clear()
        _extractors[key] = default_extractors(from_mapping(settings_data)[0])
    return result_to_data(_extractors[key].extract(record_from_data(payload["record"])))


def content_mark(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The fingerprint of one file's content. Runs inside a worker, so a drive that never answers costs the
    time limit and not the program."""
    from . import contentmark as printing
    try:
        return {"content": printing.of_file(path_from_data(payload["path"]))}
    except FileNotFoundError as exc:
        return {"failure": FailureReason.UNREACHABLE.value, "detail": f"the file was not there ({type(exc).__name__})"}
    except OSError as exc:
        return {"failure": FailureReason.OTHER.value, "detail": f"the file could not be read ({type(exc).__name__})"}


_embedders: Dict[str, Any] = {}


def embed(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Sketches of meaning for a list of texts. Runs inside a worker; the model is loaded once per worker.
    Payload: folder, choice, texts, kind ("document" | "query"), dims (0: the model's own), gpu, threads."""
    from . import embedding
    key = f"{payload.get('folder')}|{payload.get('choice')}|{bool(payload.get('gpu'))}|{int(payload.get('threads') or 0)}"
    try:
        if key not in _embedders:
            _embedders.clear()
            _embedders[key] = embedding.Embedder(str(payload["folder"]), embedding.SHAPES[str(payload["choice"])],
                                                 bool(payload.get("gpu")), int(payload.get("threads") or 0))
    except ImportError as exc:
        return {"failure": FailureReason.NOT_INSTALLED.value, "detail": f"the embedding runtime is not installed ({exc})"}
    except Exception as exc:
        return {"failure": FailureReason.CRASHED.value, "detail": f"the model could not be loaded ({type(exc).__name__}: {exc})"}
    embedder = _embedders[key]
    texts = [str(t) for t in payload.get("texts") or []]
    vectors = embedder.embed(texts, str(payload.get("kind") or "document"), int(payload.get("dims") or 0))
    packed, rows, dims = embedding.pack(vectors)
    return {"vectors": packed, "rows": rows, "dims": dims, "device": embedder.device}


# ---- OCR ------------------------------------------------------------------------------------------------
#
# Both jobs run inside a worker. They answer with plain data: either what was found, or
# {"failure": reason, "detail": words}. The engine and the picture library are imported here, on first
# use, so a machine without them still indexes text and says plainly what is missing.

_engines: Dict[str, Any] = {}
CAMERA_TAGS = (271, 272)   # EXIF Make and Model


def _failure(reason: FailureReason, detail: str) -> Dict[str, Any]:
    return {"failure": reason.value, "detail": detail}


def probe_pictures(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The size of each picture and whether it names a camera, without decoding it. None for one that cannot be opened."""
    try:
        from PIL import Image
    except ImportError:
        return _failure(FailureReason.NOT_INSTALLED, "the picture library is not installed (pip install pillow)")
    shapes: list = []
    for item in payload.get("paths") or ():
        try:
            with Image.open(path_from_data(item)) as picture:
                width, height = picture.size
                exif = picture.getexif()
                camera = any(str(exif.get(tag) or "").strip() for tag in CAMERA_TAGS)
            shapes.append([int(width), int(height), bool(camera)])
        except Exception:      # a damaged or odd file: its turn in the queue will say what is wrong with it
            shapes.append(None)
    return {"shapes": shapes}


def _page_picture(path: str, page: int, dpi: int):
    """A PDF page drawn as a picture."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    try:
        if not 1 <= page <= len(document):
            raise ValueError(f"the file has no page {page}")
        return document[page - 1].render(scale=max(1, dpi) / 72.0).to_pil()
    finally:
        document.close()


GPU_NAMES = {"DmlExecutionProvider": "graphics card (DirectML)", "CUDAExecutionProvider": "graphics card (CUDA)"}


def gpu_provider() -> str:
    """The way this machine's OCR runtime can use a graphics card, or "" when it cannot.
    The runtime decides: the plain package has no such way, the DirectML one (Windows) and the CUDA one do."""
    try:
        import onnxruntime
        have = set(onnxruntime.get_available_providers())
    except Exception:
        return ""
    for name in ("CUDAExecutionProvider", "DmlExecutionProvider"):
        if name in have:
            return name
    return ""


def ocr_device(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """What OCR would run on if a graphics card were asked for (asked in a worker, so the main program
    never loads the runtime)."""
    card = gpu_provider()
    return {"card": GPU_NAMES.get(card, ""), "provider": card}


SAMPLE_LINES = ("Quarterly report on harbour maintenance", "The bakery opens at seven every morning",
                "Invoice 20418 is due on the first of March", "Minutes of the meeting held on Tuesday",
                "Please return the signed form by Friday", "Chapter three: the long road to the coast")


def ocr_speed(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one made-up page a few times on the processor and, when the runtime has a way to the graphics
    card, on the card as well. Answers {"processor": seconds a page, "card": seconds a page or None,
    "card_name": words}. The first read of each is left out: it includes loading the engine."""
    try:
        import time

        import numpy
        from PIL import Image, ImageDraw, ImageFont
        from rapidocr import RapidOCR
    except ImportError as exc:
        return _failure(FailureReason.NOT_INSTALLED, f"the OCR engine is not installed ({exc.name or exc})")
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    try:
        font = ImageFont.load_default(size=30)
    except TypeError:                     # an older picture library: its one small built-in font
        font = ImageFont.load_default()
    for row in range(30):
        draw.text((90, 80 + row * 54), SAMPLE_LINES[row % len(SAMPLE_LINES)] + f" {row + 1}", fill="black", font=font)
    picture = numpy.ascontiguousarray(numpy.asarray(page)[:, :, ::-1])
    rounds = max(1, int(payload.get("rounds") or 3))
    threads = int(payload.get("threads") or 0)

    def time_one(card: str) -> float:
        params: Dict[str, Any] = {"Global.log_level": "error"}
        if card == "DmlExecutionProvider":
            params["EngineConfig.onnxruntime.use_dml"] = True
        elif card == "CUDAExecutionProvider":
            params["EngineConfig.onnxruntime.use_cuda"] = True
        elif threads > 0:
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads
        engine = RapidOCR(params=params)
        engine(picture)                   # the warm-up read
        started = time.perf_counter()
        for _ in range(rounds):
            engine(picture)
        return (time.perf_counter() - started) / rounds

    card = gpu_provider()
    answer: Dict[str, Any] = {"processor": time_one(""), "card": None, "card_name": GPU_NAMES.get(card, "")}
    if card:
        try:
            answer["card"] = time_one(card)
        except Exception as exc:          # the card route is there but does not work: said, and the processor stands
            answer["card_problem"] = f"{type(exc).__name__}: {exc}"[:200]
    return answer


def ocr(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one picture, or one page of a PDF (page 1 or more), with the OCR engine."""
    try:
        import numpy
        from PIL import Image, ImageOps
        from rapidocr import RapidOCR
    except ImportError as exc:
        return _failure(FailureReason.NOT_INSTALLED,
                        f"the OCR engine is not installed ({exc.name or exc}); install it with: pip install rapidocr onnxruntime")
    import os

    path = path_from_data(payload["path"])
    page = int(payload.get("page") or 0)
    if not os.path.isfile(path):
        return _failure(FailureReason.UNREACHABLE, "the file is not there right now")
    try:
        if page:
            picture = _page_picture(path, page, int(payload.get("dpi") or 150))
        else:
            with Image.open(path) as opened:
                picture = ImageOps.exif_transpose(opened)   # a photo taken sideways is read the right way up
                picture.load()
        picture = picture.convert("RGB")
    except MemoryError:
        raise
    except Exception as exc:
        return _failure(FailureReason.CORRUPT, f"the picture could not be opened ({type(exc).__name__})")
    width, height = picture.size
    left = top = 0
    region = payload.get("region")
    if region:
        x0, y0, x1, y1 = (int(v) for v in region)
        left, top = x0 * width // 1000, y0 * height // 1000
        picture = picture.crop((left, top, max(left + 1, x1 * width // 1000), max(top + 1, y1 * height // 1000)))
    shrink = 1.0
    longest = max(picture.size)
    limit = int(payload.get("max_pixels") or 0)
    if limit and longest > limit:
        shrink = limit / float(longest)
        picture = picture.resize((max(1, round(picture.width * shrink)), max(1, round(picture.height * shrink))))
    threads = int(payload.get("threads") or 0)       # 0: the engine decides (it takes every core it finds)
    card = gpu_provider() if payload.get("gpu") else ""     # asked for, and there: else the processor, and said so
    key = f"rapidocr:{threads}:{card}"
    if key not in _engines:
        params: Dict[str, Any] = {"Global.log_level": "error"}
        if card == "DmlExecutionProvider":
            params["EngineConfig.onnxruntime.use_dml"] = True
        elif card == "CUDAExecutionProvider":
            params["EngineConfig.onnxruntime.use_cuda"] = True
        if threads > 0:              # readers working side by side must each keep to a few cores
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads
            params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
        _engines[key] = RapidOCR(params=params)
    found = _engines[key](numpy.ascontiguousarray(numpy.asarray(picture)[:, :, ::-1]))
    items = []
    if found is not None and getattr(found, "boxes", None) is not None and found.txts is not None:
        scores = found.scores if found.scores is not None else [None] * len(found.txts)
        for points, text, score in zip(found.boxes.tolist(), found.txts, scores):
            points = [[x / shrink + left, y / shrink + top] for x, y in points]
            items.append([str(text), None if score is None else float(score), points])
    try:
        from importlib.metadata import version
        engine_version = version("rapidocr")
    except Exception:
        engine_version = "unknown"
    return {"width": width, "height": height, "items": items, "engine": "rapidocr", "version": engine_version,
            "device": GPU_NAMES.get(card, "processor")}
