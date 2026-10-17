"""Turning text into a sketch of its meaning (an embedding): a list of numbers such that texts about the same
thing come out close together. Used for searching by meaning, and measured first (meaning.py).

The model runs through the same runtime as OCR (onnxruntime), on the processor or, with a runtime that
can, on a graphics card. Two models are pinned (embedding_models.py): a small one and a larger one whose
sketches can be cut short (the first N numbers still work, less precisely), which is what makes the vector
size a choice. Everything here runs inside a worker (jobs.embed); the main program never loads the model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Shape:
    """How one model is to be used."""
    choice: str
    dims: int                 # numbers in a full sketch
    max_tokens: int           # longer text is cut
    document_prefix: str      # the model's own way of telling a stored text from a question
    query_prefix: str
    layer_norm: bool          # a layer norm before cutting short (the larger model wants it)
    cuttable: bool            # the first N numbers of a sketch still work (Matryoshka)
    model_file: str
    tokenizer_file: str


SHAPES = {
    "small": Shape("small", 384, 256, "", "", False, False, "small.onnx", "small-tokenizer.json"),
    "larger": Shape("larger", 768, 512, "search_document: ", "search_query: ", True, True, "larger.onnx", "larger-tokenizer.json"),
}
SIZES = {"compact": 256, "full": 768}     # the vector-size choice, in numbers kept (the larger model; measured: 128 lost too much)


def device_used(session, wanted: str) -> str:
    """The device a session really runs on, in words. A runtime that offers a card but finds none falls back
    to the processor by itself; saying "graphics card" then would be untrue."""
    try:
        first = session.get_providers()[0]
    except Exception:
        return wanted
    if first == "CUDAExecutionProvider":
        return "graphics card (CUDA)"
    if first == "DmlExecutionProvider":
        return "graphics card (DirectML)"
    return "processor"


class Embedder:
    """One model, loaded once. `embed` takes texts and gives one sketch each, as float32 rows."""

    def __init__(self, folder: str, shape: Shape, gpu: bool = False, threads: int = 0) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        self.shape = shape
        options = ort.SessionOptions()
        if threads > 0:
            options.intra_op_num_threads = threads
        providers = ["CPUExecutionProvider"]
        self.device = "processor"
        if gpu:
            have = set(ort.get_available_providers())
            for name in ("CUDAExecutionProvider", "DmlExecutionProvider"):
                if name in have:
                    providers = [name, "CPUExecutionProvider"]
                    self.device = "graphics card (" + ("CUDA" if name.startswith("CUDA") else "DirectML") + ")"
                    break
        self.session = ort.InferenceSession(os.path.join(folder, shape.model_file), options, providers=providers)
        self.device = device_used(self.session, self.device)
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(os.path.join(folder, shape.tokenizer_file))
        self.tokenizer.enable_truncation(shape.max_tokens)
        self.tokenizer.enable_padding()

    def embed(self, texts: Sequence[str], kind: str = "document", dims: Optional[int] = None, batch: int = 16):
        """Sketches for `texts` (a document each, or a question when kind is "query"), one row each, each cut
        to `dims` numbers when asked and the model allows, and scaled to length 1."""
        np = self.np
        prefix = self.shape.query_prefix if kind == "query" else self.shape.document_prefix
        keep = self.shape.dims if not dims or not self.shape.cuttable else max(1, min(dims, self.shape.dims))
        out = np.zeros((len(texts), keep), dtype=np.float32)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))     # like lengths together: less padding
        for at in range(0, len(order), batch):
            rows = order[at:at + batch]
            encoded = self.tokenizer.encode_batch([prefix + (texts[i] or " ") for i in rows])
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]                     # (rows, tokens, dims)
            weights = mask[:, :, None].astype(np.float32)
            pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
            if self.shape.layer_norm:
                mean = pooled.mean(axis=1, keepdims=True)
                var = pooled.var(axis=1, keepdims=True)
                pooled = (pooled - mean) / np.sqrt(var + 1e-5)
            pooled = pooled[:, :keep]
            pooled = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)
            for row, vector in zip(rows, pooled):
                out[row] = vector
        return out


def pack(vectors) -> Tuple[str, int, int]:
    """Sketches as text for the worker's answer (base64 of float32), with their shape."""
    import base64
    import numpy as np
    array = np.ascontiguousarray(vectors, dtype=np.float32)
    return base64.b64encode(array.tobytes()).decode("ascii"), int(array.shape[0]), int(array.shape[1])


def unpack(text: str, rows: int, dims: int):
    import base64
    import numpy as np
    return np.frombuffer(base64.b64decode(text), dtype=np.float32).reshape(rows, dims)


# ---- pictures ---------------------------------------------------------------------------------------------

LOOKS_DIMS = 512
LOOKS_SIDE = 224            # the model looks at a 224 by 224 picture
LOOKS_MEAN = (0.48145466, 0.4578275, 0.40821073)
LOOKS_STD = (0.26862954, 0.26130258, 0.27577711)
LOOKS_TOKENS = 77
LOOKS_FILES = {"pictures": "looks-pictures.onnx", "words": "looks-words.onnx", "tokenizer": "looks-tokenizer.json"}


class Looker:
    """One model in two halves: pictures and sentences into the same LOOKS_DIMS numbers, each of length 1,
    so a picture can be compared with a picture or with a sentence. The sentence half is loaded on first
    use only; a fill of pictures never needs it."""

    def __init__(self, folder: str, gpu: bool = False, threads: int = 0) -> None:
        import numpy as np
        import onnxruntime as ort
        self.np = np
        self.folder = folder
        self.options = ort.SessionOptions()
        if threads > 0:
            self.options.intra_op_num_threads = threads
        self.providers = ["CPUExecutionProvider"]
        self.device = "processor"
        if gpu:
            have = set(ort.get_available_providers())
            for name in ("CUDAExecutionProvider", "DmlExecutionProvider"):
                if name in have:
                    self.providers = [name, "CPUExecutionProvider"]
                    self.device = "graphics card (" + ("CUDA" if name.startswith("CUDA") else "DirectML") + ")"
                    break
        self.pictures = ort.InferenceSession(os.path.join(folder, LOOKS_FILES["pictures"]), self.options, providers=self.providers)
        self.device = device_used(self.pictures, self.device)
        self._words = None
        self._tokenizer = None

    @staticmethod
    def prepare(picture) -> "object":
        """A PIL picture as the model's input: RGB, the shorter side scaled to LOOKS_SIDE, the middle square cut
        out, scaled and normalised, channels first."""
        import numpy as np
        from PIL import Image, ImageOps
        picture = ImageOps.exif_transpose(picture).convert("RGB")
        w, h = picture.size
        scale = LOOKS_SIDE / max(1, min(w, h))
        picture = picture.resize((max(LOOKS_SIDE, round(w * scale)), max(LOOKS_SIDE, round(h * scale))), Image.BICUBIC)
        w, h = picture.size
        left, top = (w - LOOKS_SIDE) // 2, (h - LOOKS_SIDE) // 2
        picture = picture.crop((left, top, left + LOOKS_SIDE, top + LOOKS_SIDE))
        array = np.asarray(picture, dtype=np.float32) / 255.0
        array = (array - np.array(LOOKS_MEAN, dtype=np.float32)) / np.array(LOOKS_STD, dtype=np.float32)
        return array.transpose(2, 0, 1)

    def look(self, pictures: Sequence[object], batch: int = 16):
        """Sketches for prepared pictures (prepare), one row each, length 1."""
        np = self.np
        out = np.zeros((len(pictures), LOOKS_DIMS), dtype=np.float32)
        for at in range(0, len(pictures), batch):
            rows = np.stack(pictures[at:at + batch]).astype(np.float32)
            vectors = self.pictures.run(None, {"pixel_values": rows})[0]
            out[at:at + len(rows)] = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)
        return out

    def read(self, sentences: Sequence[str]):
        """Sketches for sentences, in the pictures' space, one row each, length 1."""
        import onnxruntime as ort
        from tokenizers import Tokenizer
        np = self.np
        if self._words is None:
            self._words = ort.InferenceSession(os.path.join(self.folder, LOOKS_FILES["words"]), self.options, providers=self.providers)
            self._tokenizer = Tokenizer.from_file(os.path.join(self.folder, LOOKS_FILES["tokenizer"]))
            self._tokenizer.enable_truncation(LOOKS_TOKENS)
        encoded = self._tokenizer.encode_batch([s or " " for s in sentences])
        end = encoded[0].ids[-1] if encoded and encoded[0].ids else 49407      # the end token pads too, as CLIP does
        width = max(len(e.ids) for e in encoded) if encoded else 1
        ids = np.full((len(encoded), width), end, dtype=np.int64)
        for row, e in enumerate(encoded):
            ids[row, :len(e.ids)] = e.ids
        vectors = self._words.run(None, {"input_ids": ids})[0]
        return (vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)).astype(np.float32)


# ---- captions ---------------------------------------------------------------------------------------------

CAPTION_SIDE = 768
CAPTION_MEAN = (0.485, 0.456, 0.406)
CAPTION_STD = (0.229, 0.224, 0.225)
CAPTION_FILES = {"vision": "captions-vision.onnx", "embed": "captions-embed.onnx", "encoder": "captions-encoder.onnx",
                 "decoder": "captions-decoder.onnx", "tokenizer": "captions-tokenizer.json"}
CAPTION_PROMPT = "What does the image describe?"        # the model's own short-caption task
CAPTION_LONGER = "Describe with a paragraph what is shown in the image."
_START, _BOS, _END = 2, 0, 2                            # the decoder starts on </s>, writes <s>, ends on </s>


class Captioner:
    """A model that writes a sentence about a picture (four ONNX parts and a tokenizer). Greedy decoding, one
    token at a time, the whole sentence re-run each step (no cache: simpler, and a sentence is short).
    Slow next to the other models (seconds a picture on a processor), so it is used on demand only."""

    def __init__(self, folder: str, gpu: bool = False, threads: int = 0) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        options = ort.SessionOptions()
        if threads > 0:
            options.intra_op_num_threads = threads
        providers = ["CPUExecutionProvider"]
        self.device = "processor"
        if gpu:
            have = set(ort.get_available_providers())
            for name in ("CUDAExecutionProvider", "DmlExecutionProvider"):
                if name in have:
                    providers = [name, "CPUExecutionProvider"]
                    self.device = "graphics card (" + ("CUDA" if name.startswith("CUDA") else "DirectML") + ")"
                    break
        load = lambda part: ort.InferenceSession(os.path.join(folder, CAPTION_FILES[part]), options, providers=providers)
        self.vision, self.embed, self.encoder, self.decoder = load("vision"), load("embed"), load("encoder"), load("decoder")
        self.device = device_used(self.vision, self.device)
        self.tokenizer = Tokenizer.from_file(os.path.join(folder, CAPTION_FILES["tokenizer"]))

    @staticmethod
    def prepare(picture):
        import numpy as np
        from PIL import Image, ImageOps
        picture = ImageOps.exif_transpose(picture).convert("RGB").resize((CAPTION_SIDE, CAPTION_SIDE), Image.BICUBIC)
        array = (np.asarray(picture, dtype=np.float32) / 255.0 - np.array(CAPTION_MEAN, dtype=np.float32)) / np.array(CAPTION_STD, dtype=np.float32)
        return array.transpose(2, 0, 1)

    def caption(self, prepared, longer: bool = False, max_tokens: int = 24) -> str:
        """One sentence about one prepared picture (prepare)."""
        np = self.np
        features = self.vision.run(None, {"pixel_values": prepared[None].astype(np.float32)})[0]
        prompt = np.array([self.tokenizer.encode(CAPTION_LONGER if longer else CAPTION_PROMPT).ids], dtype=np.int64)
        words = self.embed.run(None, {"input_ids": prompt})[0]
        inputs = np.concatenate([features, words], axis=1)
        mask = np.ones(inputs.shape[:2], dtype=np.int64)
        hidden = self.encoder.run(None, {"inputs_embeds": inputs, "attention_mask": mask})[0]
        written = [_START]
        for step in range(max_tokens if not longer else max_tokens * 3):
            so_far = self.embed.run(None, {"input_ids": np.array([written], dtype=np.int64)})[0]
            logits = self.decoder.run(None, {"encoder_attention_mask": mask, "encoder_hidden_states": hidden,
                                             "inputs_embeds": so_far})[0][0, -1]
            token = _BOS if step == 0 else int(np.argmax(logits))
            written.append(token)
            if token == _END and step > 0:
                break
        return self.tokenizer.decode(written, skip_special_tokens=True).strip()
