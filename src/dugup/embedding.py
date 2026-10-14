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
