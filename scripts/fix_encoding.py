"""Fix mojibake in kapso-bridge/server.mjs — one-shot script, delete after use."""
import re

path = "kapso-bridge/server.mjs"

with open(path, "r", encoding="utf-8") as f:
    text = f.read()

# Windows-1252 mojibake: UTF-8 bytes read as Win-1252, then re-encoded as UTF-8
# Pattern: original UTF-8 byte E2 xx yy -> Win1252 chars -> re-encoded as UTF-8
replacements = {
    # Em-dash U+2014 (E2 80 94 -> â€")
    "\u00e2\u20ac\u201c": "\u2014",
    # Ellipsis U+2026 (E2 80 A6 -> â€¦)
    "\u00e2\u20ac\u00a6": "\u2026",
    # Bullet U+2022 (E2 80 A2 -> â€¢)
    "\u00e2\u20ac\u00a2": "\u2022",
    # Black right triangle U+25B6 (E2 96 B6 -> â–¶)
    "\u00e2\u2013\u00b6": "\u25b6",
    # Box drawing horizontal U+2500 (E2 94 80 -> â"€)
    "\u00e2\u201d\u20ac": "\u2500",
    # Right arrow U+2192 (E2 86 92 -> â†')
    "\u00e2\u2020\u2019": "\u2192",
    # Left arrow U+2190 (E2 86 90 -> â†\x90)
    "\u00e2\u2020\u0090": "\u2190",
    # Up arrow U+2191 (E2 86 91 -> â†')
    "\u00e2\u2020\u2018": "\u2191",
    # Counterclockwise arrows U+21BB (E2 86 BB -> â†»)
    "\u00e2\u2020\u00bb": "\u21bb",
    # Up arrow U+2191 (E2 86 91 -> â†')
    "\u00e2\u2020\u2018": "\u2191",
    # Gear U+2699 (E2 9A 99 -> âš™)
    "\u00e2\u0161\u2122": "\u2699",
    # Timer clock U+23F8 or similar (E2 8F B8 -> â\x8f¸)
    "\u00e2\u008f\u00b8": "\u23f8",
    # Spanish chars (if any remain)
    "\u00c3\u00a9": "\u00e9",  # é
    "\u00c3\u00b3": "\u00f3",  # ó
    "\u00c3\u00a1": "\u00e1",  # á
    "\u00c3\u00ad": "\u00ed",  # í
    "\u00c3\u009a": "\u00da",  # Ú
    "\u00c2\u00b7": "\u00b7",  # ·
    "\u00c2\u00bf": "\u00bf",  # ¿
    "\u00c3\u00b1": "\u00f1",  # ñ
}

count = 0
for old, new in replacements.items():
    n = text.count(old)
    if n > 0:
        text = text.replace(old, new)
        count += n
        print(f"  Replaced {repr(old)} -> {new} ({n} times)")

# Check for any remaining â + high-char patterns
remaining = re.findall(r'\u00e2.{0,2}', text)
if remaining:
    unique = set(m for m in remaining if any(ord(c) > 127 for c in m[1:]))
    for m in sorted(unique):
        print(f"  Still remaining: {repr(m)} ({text.count(m)} times)")

print(f"\nTotal replacements: {count}")

with open(path, "w", encoding="utf-8") as f:
    f.write(text)

print("File saved.")
