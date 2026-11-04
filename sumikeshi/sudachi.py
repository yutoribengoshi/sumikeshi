"""Sudachi（形態素解析）があれば人名の判定に使う。入っていなくても動く（規則だけで拾う）。

pip install sudachipy sudachidict_core
"""
from __future__ import annotations

from functools import lru_cache

from . import lexicon as L

_TOK = None  # None=まだ試していない / False=使えない


def _tokenizer():
    global _TOK
    if _TOK is None:
        try:
            from sudachipy import Dictionary, SplitMode
            d = Dictionary()
            # sudachipy 0.7 から create() は非推奨で tokenizer() に替わった。古い版は create() だけ
            make = getattr(d, "tokenizer", None) or d.create
            _TOK = (make(), SplitMode.C)
        except Exception:
            _TOK = False
    return _TOK


def available() -> bool:
    return bool(_tokenizer())


def disable() -> None:
    global _TOK
    _TOK = False
    person.cache_clear()


def _rep(ch: str) -> str:
    # 異体字を代表字へ（1字→1字なので位置はずれない）。「髙橋」が未知語になるのを防ぐ
    g = L.VARIANTS.get(ch)
    return min(g) if g else ch


def tokens(text: str) -> list[tuple[str, tuple, int, int, bool]]:
    """(表層, 品詞, 開始, 終了, 未知語か) の列。"""
    tk = _tokenizer()
    if not tk or not text:
        return []
    t, mode = tk
    norm = "".join(_rep(ch) for ch in text)
    out = []
    # Sudachi には入力長の上限があるので、改行で区切って渡す
    off = 0
    for line in norm.splitlines(keepends=True):
        for i in range(0, len(line), 3000):
            chunk = line[i:i + 3000]
            try:
                ms = t.tokenize(chunk, mode)
            except Exception:
                continue
            base = off + i
            for m in ms:
                s, e = base + m.begin(), base + m.end()
                out.append((text[s:e], tuple(m.part_of_speech()), s, e, m.is_oov()))
        off += len(line)
    return out


@lru_cache(maxsize=50000)
def person(word: str) -> bool | None:
    """True=人名の形態素を含む / False=既知の一般語・地名だけ / None=判定できない（未知語・Sudachi なし）。"""
    if not available():
        return None
    res: bool | None = False
    place = False
    for surf, pos, _s, _e, oov in tokens(word):
        if pos[1] == "固有名詞" and pos[2] == "人名":
            return True
        if pos[1] == "固有名詞" and pos[2] == "地名":
            place = True
            continue
        if oov or (len(surf) == 1 and pos[0] == "名詞"):
            res = None
    if res is None and place:
        return False
    return res


def split_name(word: str) -> tuple[str, str] | None:
    """「山田太郎」→（山田, 太郎）。姓・名の形態素に分かれなければ None。"""
    toks = tokens(word)
    if len(toks) == 2 and toks[0][1][2:4] == ("人名", "姓") and toks[1][1][2:4] == ("人名", "名"):
        return toks[0][0], toks[1][0]
    return None


def person_spans(text: str) -> list[tuple[int, int, str]]:
    """文中の人名の形態素。[(開始, 終了, 'full'|'sei'|'mei')]（姓＋名が続けば1つにまとめる）。"""
    toks = tokens(text)
    out = []
    i = 0
    while i < len(toks):
        _surf, pos, s, e, _oov = toks[i]
        if pos[1] == "固有名詞" and pos[2] == "人名":
            if (pos[3] == "姓" and i + 1 < len(toks) and toks[i + 1][1][2:4] == ("人名", "名")
                    and toks[i + 1][2] == e):
                out.append((s, toks[i + 1][3], "full"))
                i += 2
                continue
            out.append((s, e, "sei" if pos[3] == "姓" else "mei"))
        i += 1
    return out
