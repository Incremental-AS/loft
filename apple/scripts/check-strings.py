#!/usr/bin/env python3
"""Every key in a Localizable.strings must still be a string in the Swift sources, or the
translation is dead (the English text changed and nobody noticed). Runs anywhere, no Xcode.

    apple/scripts/check-strings.py            (checks the Mac and iPhone nb files)

A key's %@ / %lld matches any Swift interpolation \\(…); \\u{…} escapes in the sources count as
the character. Also refuses a Norwegian value that has a lower-case letter right after ": "
(the Norwegian rule: capital letter after a colon), unless what follows is a value (%@), a
host name or a command.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETS = [
    (ROOT / "App/nb.lproj/Localizable.strings", [ROOT / "App"]),
    (ROOT / "iOS/App/nb.lproj/Localizable.strings", [ROOT / "iOS/App", ROOT / "iOS/Shared"]),
]
PAIR = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)";\s*$')
FORMAT = re.compile(r"%(?:@|lld|d)")
# After ": " a lower-case word is fine only for a value, a host/address or a command.
LOWER_OK = re.compile(r"^(%@|[a-z0-9.-]+\.[a-z]{2,}|nas-gateway|loft-gateway|https)")


def strings(path: Path) -> list[tuple[str, str]]:
    out = []
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = PAIR.match(line)
        if not m:
            sys.exit(f"FAILED: {path}:{n}: not a \"key\" = \"value\"; line")
        out.append((m.group(1), m.group(2)))
    return out


def source(dirs: list[Path]) -> str:
    s = "\n".join(p.read_text(encoding="utf-8") for d in dirs for p in sorted(d.rglob("*.swift")))
    return re.sub(r"\\u\{([0-9A-Fa-f]+)\}", lambda m: chr(int(m.group(1), 16)), s)


def main() -> int:
    bad = 0
    for path, dirs in SETS:
        src = source(dirs)
        pairs = strings(path)
        keys = [k for k, _ in pairs]
        for k in sorted({k for k in keys if keys.count(k) > 1}):
            print(f"{path.relative_to(ROOT)}: duplicate key {k!r}")
            bad += 1
        for k, v in pairs:
            parts = [re.escape(p) for p in FORMAT.split(k)]
            rx = r"\\\([^)]*(?:\([^)]*\))?[^)]*\)".join(parts)
            if not re.search('"' + rx + '"', src):
                print(f"{path.relative_to(ROOT)}: key not in the sources: {k!r}")
                bad += 1
            if sorted(FORMAT.findall(k)) != sorted(FORMAT.findall(v)):
                print(f"{path.relative_to(ROOT)}: placeholders differ: {k!r}")
                bad += 1
            for m in re.finditer(r": (\S+)", v):
                w = m.group(1)
                if w[0].islower() and not LOWER_OK.match(w):
                    print(f"{path.relative_to(ROOT)}: lower case after a colon: {v!r}")
                    bad += 1
        print(f"{path.relative_to(ROOT)}: {len(pairs)} strings")
    if bad:
        print(f"FAILED: {bad} problem(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
