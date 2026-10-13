"""The two pinned embedding models (models.py has the fetching). Downloaded only when asked for, checked
by size and SHA-256, never bundled. Sizes are what is on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from .models import Pinned

_HF = "https://huggingface.co/"

SMALL: Tuple[Pinned, ...] = (
    Pinned("small.onnx",
           _HF + "sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/onnx/model.onnx",
           90405214, "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
           "a small sentence-embedding model (90 MB; English; 384 numbers a sketch)",
           "Apache 2.0, https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"),
    Pinned("small-tokenizer.json",
           _HF + "sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/tokenizer.json",
           466247, "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
           "its tokenizer (0.5 MB)"),
)

LARGER: Tuple[Pinned, ...] = (
    Pinned("larger.onnx",
           _HF + "nomic-ai/nomic-embed-text-v1.5/resolve/e9b6763023c676ca8431644204f50c2b100d9aab/onnx/model.onnx",
           547310275, "147d5aa88c2101237358e17796cf3a227cead1ec304ec34b465bb08e9d952965",
           "a larger text-embedding model (547 MB; English; up to 768 numbers a sketch, which can be cut short)",
           "Apache 2.0, https://huggingface.co/nomic-ai/nomic-embed-text-v1.5"),
    Pinned("larger-tokenizer.json",
           _HF + "nomic-ai/nomic-embed-text-v1.5/resolve/e9b6763023c676ca8431644204f50c2b100d9aab/tokenizer.json",
           711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
           "its tokenizer (0.7 MB)"),
)

BY_CHOICE: Dict[str, Tuple[Pinned, ...]] = {"small": SMALL, "larger": LARGER}


def present(folder: Path, choice: str) -> bool:
    return all((Path(folder) / item.name).exists() for item in BY_CHOICE[choice])
