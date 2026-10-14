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

# ---- pictures by likeness: one model that reads pictures and sentences into the same numbers -------------
_LOOKS = _HF + "Xenova/clip-vit-base-patch32/resolve/d15189d7028b43f1d3e65039190477f6af591c2a/"
LOOKS_BASE: Tuple[Pinned, ...] = (
    Pinned("looks-pictures.onnx", _LOOKS + "onnx/vision_model.onnx",
           351685709, "fd6e1402a588279d1723c7534d4bcba5bc0b14b47dfab0e46f8c47b8270d7d40",
           "a picture model (352 MB; 512 numbers a picture), the picture half of CLIP ViT-B/32",
           "MIT, https://huggingface.co/Xenova/clip-vit-base-patch32 (OpenAI's CLIP weights)"),
    Pinned("looks-words.onnx", _LOOKS + "onnx/text_model.onnx",
           254058553, "3f6571f5bad13a97c469c1622e1cfc4d9aef78b79fdbfcff804ca357bfada8cc",
           "its sentence half (254 MB), so a sentence can be compared with pictures"),
    Pinned("looks-tokenizer.json", _LOOKS + "tokenizer.json",
           2224119, "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd",
           "its tokenizer (2.2 MB)"),
)
LOOKS_BY_CHOICE: Dict[str, Tuple[Pinned, ...]] = {"base": LOOKS_BASE}


# ---- captions: a model that describes a picture in a sentence, for naming (on demand only) ----------------
_CAPTIONS = _HF + "onnx-community/Florence-2-base-ft/resolve/e88a44eaf3791a35eae0c5a47b3dbcd36e67eb6f/"
CAPTIONS_BASE: Tuple[Pinned, ...] = (
    Pinned("captions-vision.onnx", _CAPTIONS + "onnx/vision_encoder.onnx",
           366549825, "d67258cdfdebfa21285dad9e7bd4bd99725236d0aaef9e474a1b24a6ec471351",
           "a captioning model's picture half (367 MB; Florence-2 base)",
           "MIT, https://huggingface.co/microsoft/Florence-2-base-ft"),
    Pinned("captions-embed.onnx", _CAPTIONS + "onnx/embed_tokens.onnx",
           157560044, "90cae3deb6406938c676a35b5246db02b478c9cc8cf93508361be80c05babf95", "its word table (158 MB)"),
    Pinned("captions-encoder.onnx", _CAPTIONS + "onnx/encoder_model.onnx",
           173380723, "cb0bccc232c64290397f5e1235eb3e1fa6ccf8c5afed9216480ee4eed80737fc", "its encoder (173 MB)"),
    Pinned("captions-decoder.onnx", _CAPTIONS + "onnx/decoder_model.onnx",
           388077195, "16b40ea746ea09802a549be74c2eedc937c76025a1ea9baa040617ba0605306d", "its decoder, which writes the sentence (388 MB)"),
    Pinned("captions-tokenizer.json", _CAPTIONS + "tokenizer.json",
           2297961, "d69dcdb2323e124ac4f800cb9863ddccea0d7bb11e16125e8df3bd60f2f8aeac", "its tokenizer (2.3 MB)"),
)
CAPTIONS_BY_CHOICE: Dict[str, Tuple[Pinned, ...]] = {"base": CAPTIONS_BASE}


def captions_present(folder: Path, choice: str = "base") -> bool:
    return all((Path(folder) / item.name).exists() for item in CAPTIONS_BY_CHOICE[choice])


def looks_present(folder: Path, choice: str = "base") -> bool:
    return all((Path(folder) / item.name).exists() for item in LOOKS_BY_CHOICE[choice])


def present(folder: Path, choice: str) -> bool:
    return all((Path(folder) / item.name).exists() for item in BY_CHOICE[choice])
