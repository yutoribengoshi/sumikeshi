"""テキスト（.txt / .md）の読み書き。UTF-8（BOM の有無）・Shift_JIS（cp932）・EUC-JP を見分け、同じ形で書き戻す。"""
from __future__ import annotations

import codecs
from pathlib import Path

from .matcher import Matcher


def read(path) -> tuple[str, str]:
    data = Path(path).read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def read_units(path) -> list[tuple[str, str]]:
    text, _ = read(path)
    return [(line, f"{n}行目") for n, line in enumerate(text.splitlines(), 1) if line.strip()]


def apply(path, out_path, matcher: Matcher) -> tuple[dict, str]:
    text, enc = read(path)
    new, hits = matcher.sub(text)
    counts: dict[int, int] = {}
    for h in hits:
        counts[h.entry] = counts.get(h.entry, 0) + 1
    try:
        data = new.encode(enc)
    except UnicodeEncodeError:
        enc = "utf-8"
        data = new.encode(enc)
    Path(out_path).write_bytes(data)
    return counts, enc


def all_strings(path) -> list[tuple[str, str]]:
    return read_units(path)


def line_count(path) -> int:
    return len(read(path)[0].splitlines())
