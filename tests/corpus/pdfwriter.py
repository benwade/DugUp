"""A very small PDF writer for test files. It writes exactly the page kinds the Extractor must tell apart:

  born_digital  visible text, no page image
  scanner       a full-page image with invisible text over it (what scanners and OCR tools produce)
  hidden        invisible text with no image under it (an OCR layer whose picture is drawn some other way)
  garbled       visible text whose character map is broken (it extracts as private-use characters)
  none          no text; a full-page image when `ink` is true, an empty page otherwise
  unreadable    a page entry that is not a page at all
  figure        visible text with a picture over about a quarter of the page (a chart, a pasted screenshot)
  logo          visible text with a picture over about one percent of the page

Text is written through a ToUnicode map made for the document, so any Unicode text (accents, CJK, emoji)
comes back out of the text layer exactly, whatever the glyphs look like. Optional password protection uses
the PDF standard security handler (RC4, 40 bit), written out here so no extra library is needed.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

PAGE_W, PAGE_H = 612, 792
_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


@dataclass(frozen=True)
class PdfPage:
    layer: str = "born_digital"
    text: str = ""
    ink: bool = True


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 255])
    return bytes(out)


class _Font:
    """One simple font whose byte codes map to whatever characters the document needs."""

    def __init__(self, garble: bool) -> None:
        self.garble = garble
        self.codes: Dict[str, int] = {}

    def encode(self, text: str) -> bytes:
        out = bytearray()
        for ch in text:
            if ch not in self.codes:
                if len(self.codes) >= 220:
                    raise ValueError("too many different characters for one test font")
                self.codes[ch] = 0x21 + len(self.codes)
            out.append(self.codes[ch])
        return bytes(out)

    def to_unicode(self) -> bytes:
        lines = []
        for index, (ch, code) in enumerate(sorted(self.codes.items(), key=lambda kv: kv[1])):
            target = chr(0xE000 + index) if self.garble else ch
            units = target.encode("utf-16-be").hex().upper()
            lines.append(f"<{code:02X}> <{units}>")
        body = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
                "/CMapName /TestMap def", "/CMapType 2 def", "1 begincodespacerange", "<00> <FF>", "endcodespacerange"]
        for at in range(0, len(lines), 100):
            block = lines[at:at + 100]
            body += [f"{len(block)} beginbfchar"] + block + ["endbfchar"]
        body += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
        return "\n".join(body).encode("ascii")


def _hex(data: bytes) -> bytes:
    return b"<" + data.hex().upper().encode("ascii") + b">"


def _text_ops(font_name: str, font: _Font, text: str, invisible: bool) -> bytes:
    ops = [b"BT", f"/{font_name} 12 Tf".encode(), b"14 TL", b"72 720 Td"]
    if invisible:
        ops.append(b"3 Tr")
    for line in text.split("\n"):
        words = line.split(" ")
        for index, word in enumerate(words):
            if word:
                ops.append(_hex(font.encode(word)) + b" Tj")
            if index < len(words) - 1:
                ops.append(_hex(font.encode(" ")) + b" Tj")
        ops.append(b"T*")
    ops.append(b"ET")
    return b"\n".join(ops)


def build_pdf(pages: Sequence[PdfPage], password: Optional[str] = None) -> bytes:
    good, bad = _Font(False), _Font(True)
    good.encode(" ")
    bad.encode(" ")
    contents: List[Optional[bytes]] = []
    for page in pages:
        if page.layer == "born_digital":
            contents.append(_text_ops("F1", good, page.text, False))
        elif page.layer == "garbled":
            contents.append(_text_ops("F2", bad, page.text, False))
        elif page.layer == "hidden":
            contents.append(_text_ops("F1", good, page.text, True))
        elif page.layer == "scanner":
            image = f"q {PAGE_W} 0 0 {PAGE_H} 0 0 cm /Im1 Do Q\n".encode()
            contents.append(image + _text_ops("F1", good, page.text, True))
        elif page.layer == "none":
            contents.append(f"q {PAGE_W} 0 0 {PAGE_H} 0 0 cm /Im1 Do Q".encode() if page.ink else b"")
        elif page.layer in ("figure", "logo"):
            w, h = (PAGE_W // 2, PAGE_H // 2) if page.layer == "figure" else (PAGE_W // 10, PAGE_H // 10)
            image = f"q {w} 0 0 {h} 40 40 cm /Im1 Do Q\n".encode()
            contents.append(image + _text_ops("F1", good, page.text, False))
        elif page.layer == "unreadable":
            contents.append(None)
        else:
            raise ValueError(f"unknown layer {page.layer!r}")

    objects: Dict[int, Tuple[bytes, Optional[bytes]]] = {}   # number -> (dictionary text, stream or None)
    n = len(pages)
    first_page = 8
    kids = " ".join(f"{first_page + 2 * i} 0 R" for i in range(n))
    objects[1] = (b"<< /Type /Catalog /Pages 2 0 R >>", None)
    objects[2] = (f"<< /Type /Pages /Count {n} /Kids [{kids}] >>".encode(), None)
    objects[3] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 5 0 R >>", None)
    objects[4] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>", None)
    objects[5] = (b"<< >>", good.to_unicode())
    objects[6] = (b"<< >>", bad.to_unicode())
    pixels = bytes((x * 37 + y * 11) % 200 + 20 for y in range(16) for x in range(16))
    objects[7] = (b"<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceGray "
                  b"/BitsPerComponent 8 /Filter /FlateDecode >>", zlib.compress(pixels))
    for i, content in enumerate(contents):
        number = first_page + 2 * i
        if content is None:
            objects[number] = (b"<< /Type /NotAPage /Broken true >>", None)
            objects[number + 1] = (b"<< >>", b"")
            continue
        objects[number] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                           f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> /XObject << /Im1 7 0 R >> >> "
                           f"/Contents {number + 1} 0 R >>".encode(), None)
        objects[number + 1] = (b"<< >>", content)

    doc_id = hashlib.md5(repr([(p.layer, p.text, p.ink) for p in pages]).encode("utf-8")).digest()
    key = None
    trailer_extra = b""
    if password is not None:
        permissions = -4
        user = (password.encode("latin-1") + _PAD)[:32]
        owner_key = hashlib.md5(user).digest()[:5]
        o_entry = _rc4(owner_key, user)
        key = hashlib.md5(user + o_entry + permissions.to_bytes(4, "little", signed=True) + doc_id).digest()[:5]
        u_entry = _rc4(key, _PAD)
        encrypt_number = max(objects) + 1
        objects[encrypt_number] = (b"<< /Filter /Standard /V 1 /R 2 /O " + _hex(o_entry) + b" /U " + _hex(u_entry)
                                   + f" /P {permissions} >>".encode(), None)
        trailer_extra = f" /Encrypt {encrypt_number} 0 R".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: Dict[int, int] = {}
    for number in sorted(objects):
        dictionary, stream = objects[number]
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode()
        if stream is None:
            out += dictionary + b"\nendobj\n"
            continue
        if key is not None:
            object_key = hashlib.md5(key + number.to_bytes(3, "little") + b"\x00\x00").digest()[:10]
            stream = _rc4(object_key, stream)
        head = dictionary.rstrip()[:-2] + f" /Length {len(stream)} >>".encode()
        out += head + b"\nstream\n" + stream + b"\nendstream\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {size} /Root 1 0 R /ID [".encode() + _hex(doc_id) + _hex(doc_id) + b"]"
            + trailer_extra + f" >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def corrupt_pdf() -> bytes:
    """Bytes that begin like a PDF and are nothing of the kind."""
    return b"%PDF-1.4\n" + bytes(range(256)) * 4 + b"\nthis is not a PDF body\n"
