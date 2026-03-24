"""Fix remaining mojibake emojis in server.mjs"""
with open("kapso-bridge/server.mjs", "r", encoding="utf-8") as f:
    text = f.read()

# Eye emoji: U+00F0 U+0178 U+201D U+008D -> remove
eye = "\u00f0\u0178\u201d\u008d "
# Speech balloon: U+00F0 U+0178 U+2019 U+00AC -> remove
balloon = "\u00f0\u0178\u2019\u00ac "
# Gear with variation selector: U+2699 U+00EF U+00B8 U+008F -> just gear U+2699
gear_bad = "\u2699\u00ef\u00b8\u008f "
gear_good = ""

count = 0
for old, new in [(eye, ""), (balloon, ""), (gear_bad, gear_good)]:
    n = text.count(old)
    if n > 0:
        text = text.replace(old, new)
        count += n
        print(f"  Fixed {repr(old)} -> {repr(new)} ({n} times)")

with open("kapso-bridge/server.mjs", "w", encoding="utf-8") as f:
    f.write(text)
print(f"Done. {count} fixes applied.")
