"""Pictures with words in them, drawn with Pillow, for the tests that run the real OCR engine.

Generated data only. Pillow's built-in scalable font is used, so no font file is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence, Tuple

LineSpec = Tuple[str, Tuple[int, int, int, int]]   # (text, (x0, y0, x1, y1) on the 0..1000 scale)


def draw_lines(lines: Sequence[LineSpec], width: int = 800, height: int = 1000):
    """A white page with each text drawn in black so that it fills its box exactly."""
    from PIL import Image, ImageDraw, ImageFont

    page = Image.new("RGB", (width, height), "white")
    font = ImageFont.load_default(size=96)
    for text, (x0, y0, x1, y1) in lines:
        left, top, right, bottom = font.getbbox(text)
        word = Image.new("RGB", (right - left + 8, bottom - top + 8), "white")
        ImageDraw.Draw(word).text((4 - left, 4 - top), text, fill="black", font=font)
        word = word.crop((4, 4, 4 + right - left, 4 + bottom - top))
        box = (x0 * width // 1000, y0 * height // 1000, x1 * width // 1000, y1 * height // 1000)
        page.paste(word.resize((max(1, box[2] - box[0]), max(1, box[3] - box[1]))), box[:2])
    return page


def sign(words: Sequence[str], width: int = 1200, height: int = 800):
    """Big words one under the other, like a sign or a title page."""
    from PIL import Image, ImageDraw, ImageFont

    page = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(page)
    size = max(12, height // (2 * max(1, len(words)) + 2))
    font = ImageFont.load_default(size=size)
    widest = max(font.getlength(text) for text in words)
    if widest > 0.8 * width:              # every word stays on the page
        font = ImageFont.load_default(size=max(12, int(size * 0.8 * width / widest)))
    for index, text in enumerate(words):
        draw.text((width // 10, (2 * index + 1) * height // (2 * len(words) + 2)), text, fill="black", font=font)
    return page


def save(picture, path: Path, **options) -> Path:
    picture.save(path, **options)
    return Path(path)
