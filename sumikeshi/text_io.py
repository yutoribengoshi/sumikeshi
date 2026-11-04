"""テキスト（.txt / .md）の読み書き。文字コードを見分け、同じ形で書き戻す。

見分けるもの: UTF-8（BOM の有無）・UTF-16（BOM の有無）・ISO-2022-JP（JIS）・Shift_JIS（cp932）・EUC-JP。
どれでも読めないときは作らずに止める（壊れた文字に置き換えて「置換0件・合格」にしない）。
"""
from __future__ import annotations

import codecs
from pathlib import Path

from .matcher import Matcher


def _japanese_score(t: str) -> int:
    """日本語らしさ（かな・漢字の数から、半角カナと読めない字を引く）。EUC-JP を cp932 で読むと半角カナが並ぶ。"""
    good = sum(1 for ch in t if 0x3041 <= ord(ch) <= 0x30FF or 0x4E00 <= ord(ch) <= 0x9FFF)
    bad = sum(1 for ch in t if 0xFF61 <= ord(ch) <= 0xFF9F or ord(ch) == 0xFFFD)
    return good - 3 * bad


def read(path) -> tuple[str, str]:
    data = Path(path).read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16"), "utf-16"
    # BOM の無い UTF-16: 0x00 が偶数番目か奇数番目に偏る
    if len(data) >= 4 and data.count(0) > len(data) // 4:
        enc = "utf-16-le" if data[1::2].count(0) > data[0::2].count(0) else "utf-16-be"
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    if b"\x1b$" in data and max(data, default=0) < 0x80:
        try:
            return data.decode("iso2022_jp"), "iso2022_jp"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    cands = []
    for enc in ("cp932", "euc_jp"):
        try:
            cands.append((data.decode(enc), enc))
        except UnicodeDecodeError:
            continue
    if not cands:
        raise ValueError("文字コードを見分けられませんでした。UTF-8 で保存し直してから使ってください")
    return max(cands, key=lambda c: _japanese_score(c[0]))


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
