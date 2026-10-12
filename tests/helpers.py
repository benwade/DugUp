"""Shared test constants. Source files stay ASCII; non-ASCII text is built with chr()."""

E_ACUTE = chr(0xE9)          # e with acute accent
CAFE = "caf" + E_ACUTE
CAFE_PLAIN = "cafe"
U_UMLAUT = chr(0xFC)
MUELLER = "M" + U_UMLAUT + "ller"
GREEK = "".join(chr(c) for c in (0x3A3, 0x3C9, 0x3BA, 0x3C1, 0x3AC, 0x3C4, 0x3B7, 0x3C2))  # Socrates-like word
CYRILLIC = "".join(chr(c) for c in (0x41C, 0x43E, 0x441, 0x43A, 0x432, 0x430))  # Moskva
CJK = chr(0x65E5) + chr(0x672C) + chr(0x8A9E)  # Nihongo
EMOJI = chr(0x1F600)
LEFT_QUOTE = chr(0x201C)
RIGHT_QUOTE = chr(0x201D)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)

# A folder name with spaces, an accent, a Japanese run and an en dash
AWKWARD_FOLDER = f"My Data {E_ACUTE} {CJK} {EN_DASH} test"
