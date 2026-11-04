"""置換表（原文→置換後）から正規表現を組み、文字列の中の一致を探して置き換える。

- 字間の空白を許す（記名欄の「山　田　太　郎」、当事者欄の「山田　太郎」）
- 異体字・全角半角を同じ字として当てる（髙／高、﨑／崎、ＡＢＣ／ABC）
- 長い原文から先に当てる（姓が先に置換されてフルネームの名だけ残る事故を防ぐ）
- 2字以下の語は、前後に漢字が続くと当てない（「山田」で「山田町」を壊さない）。
  ただし「原告山田」「山田弁護士」のように役割語・肩書きが隣にあるときは当てる
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import lexicon as L

# 数字の並びとして照合する種別（区切りの違いを吸収する）
DIGIT_KINDS = {"電話", "口座", "郵便番号", "番号"}
KUROFILL = "■"

_DIGIT_RE = re.compile(f"[{L.DIGIT}]")
_SEP = f"[{L.SPACE_CLASS}{L.HYPHENS}()（）]"
_HYPHEN_SET = set("-－‐−ー―‑–()（）")
_WORDISH = re.compile(f"[{L.KANJI}{L.KATA}]")
_KATA_CH = re.compile(f"[{L.KATA}]")
_LATIN_CH = re.compile(f"[{L.ALNUM}]")
_LATIN_ALPHA = re.compile("[A-Za-zＡ-Ｚａ-ｚ]")

# 2字以下の名前の前に来てよい語（「原告山田」「訴外山田」）と、後ろに来てよい語（「山田弁護士」）
_BEFORE_OK = sorted(set(L.NAME_PREFIXES) | set(L.ONE_CHAR_ROLES), key=len, reverse=True)
_AFTER_OK = sorted(
    set(L.TITLE_SUFFIXES) | set(L.HONORIFICS)
    | set("""作成 名義 所有 方 宅 家 邸 夫妻 夫婦 一家 側 ら 等 外 各 本人 自身 個人 名 姓 夫 妻 父 母
    兄 弟 姉 妹 長男 長女 次男 次女 二男 二女 子 代理人 証人 供述 証言 陳述 尋問 調書 名下 印
    及び 並びに 又は 若しくは""".split()),
    key=len, reverse=True,
)


@dataclass
class Entry:
    original: str
    replacement: str
    kind: str = ""
    cid: str = ""


@dataclass
class Hit:
    start: int
    end: int
    entry: int          # Matcher.entries の添字
    matched: str
    replacement: str


def core(text: str) -> str:
    """空白を除いた形（照合と並べ替えの基準）。"""
    return "".join(ch for ch in text if ch not in L.SPACE_SET)


def char_class(ch: str) -> str:
    group = L.VARIANTS.get(ch)
    if not group or len(group) == 1:
        return re.escape(ch)
    return "[" + "".join(re.escape(c) for c in sorted(group)) + "]"


def _digit_pattern(c: str, kind: str) -> str:
    parts: list[str] = []
    prev_digit = False
    for ch in c:
        if ch in _HYPHEN_SET:
            continue
        is_digit = bool(_DIGIT_RE.match(ch))
        if parts:
            parts.append(f"{_SEP}{{0,3}}" if (is_digit and prev_digit) else f"{L.SP}*")
        parts.append(char_class(ch))
        prev_digit = is_digit
    pat = "".join(parts)
    if c and _DIGIT_RE.match(c[0]):
        # Excel で開くと先頭の0が落ちることがある。電話・口座は0の有無を許す
        if kind in ("電話", "口座") and c[0] not in "0０":
            pat = f"[0０]?{pat}"
        pat = f"(?<![{L.DIGIT}]){pat}"
    if c and _DIGIT_RE.match(c[-1]):
        pat = f"{pat}(?![{L.DIGIT}])"
    return pat


def _caseless_class(ch: str) -> str:
    """英字の大文字・小文字（全角・半角とも）を同じ字として当てる。"""
    group = set(L.VARIANTS.get(ch, {ch}))
    for x in list(group):
        if x.isascii() and x.isalpha():
            group |= L.VARIANTS.get(x.swapcase(), {x.swapcase()})
        elif "Ａ" <= x <= "Ｚ" or "ａ" <= x <= "ｚ":
            other = chr(ord(x) + 0x20) if x <= "Ｚ" else chr(ord(x) - 0x20)
            group |= L.VARIANTS.get(other, {other})
    return "[" + "".join(re.escape(c) for c in sorted(group)) + "]" if len(group) > 1 else re.escape(ch)


def entry_pattern(e: Entry) -> str | None:
    c = core(e.original)
    if not c:
        return None
    digitish = re.fullmatch(f"[{L.DIGIT}{L.HYPHENS}()（）〒]+", c) is not None
    if e.kind in DIGIT_KINDS or (digitish and sum(1 for ch in c if _DIGIT_RE.match(ch)) >= 5):
        return _digit_pattern(c, e.kind)
    # 英字の名・社名は4字以上なら大文字小文字を問わない（JOHN SMITH／John Smith）。短い英字の語は区別する
    caseless = sum(1 for ch in c if _LATIN_ALPHA.match(ch)) >= 4
    return f"{L.SP}*".join(_caseless_class(ch) if caseless and _LATIN_ALPHA.match(ch) else char_class(ch) for ch in c)


def kuro(matched: str) -> str:
    """墨消し: 空白以外の字を■にする（字数と字間が残るので表や記名欄の配置が崩れない）。"""
    return "".join(ch if ch in L.SPACE_SET or ch == "\n" else KUROFILL for ch in matched)


def _ends_with_any(s: str, words: list[str]) -> bool:
    s = core(s[-12:])
    return any(s.endswith(w) for w in words)


def _starts_with_any(s: str, words: list[str]) -> bool:
    s = core(s[:12])
    return any(s.startswith(w) for w in words)


class Matcher:
    """置換表をまとめて1本の正規表現にし、左から最長一致で探す。"""

    def __init__(self, entries: list[Entry], guards: bool = True):
        self.guards = guards
        self.entries: list[Entry] = []
        pats: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for i, e in enumerate(entries):
            c = core(e.original)
            p = entry_pattern(e)
            if not p or c in seen:
                continue
            seen.add(c)
            self.entries.append(e)
            pats.append((len(c), -i, p))
        order = sorted(range(len(pats)), key=lambda k: (pats[k][0], pats[k][1]), reverse=True)
        self._index = [order[k] for k in range(len(order))]
        self.regex = re.compile("|".join(f"({pats[k][2]})" for k in order)) if order else None
        self._cores = [core(e.original) for e in self.entries]

    def __bool__(self) -> bool:
        return self.regex is not None

    def replacement(self, idx: int, matched: str) -> str:
        rep = self.entries[idx].replacement
        if rep and set(rep) == {KUROFILL}:
            return kuro(matched)
        return rep

    def _guard_ok(self, text: str, s: int, e: int, idx: int) -> bool:
        if not self.guards:
            return True
        c = self._cores[idx]
        before, after = text[s - 1:s], text[e:e + 1]
        # カナ・英数字の語は、同じ種類の字が続く語の一部なら当てない（ジョン／ジョンソン）
        if _KATA_CH.match(c[0]) and before and _KATA_CH.match(before):
            return False
        if _KATA_CH.match(c[-1]) and after and _KATA_CH.match(after):
            return False
        if _LATIN_CH.match(c[0]) and before and _LATIN_CH.match(before):
            return False
        if _LATIN_CH.match(c[-1]) and after and _LATIN_CH.match(after):
            return False
        if len(c) > 2 or not _WORDISH.search(c):
            return True
        # 2字以下の漢字の語: 隣が漢字・カナなら、役割語・肩書きでない限り別の語の一部とみなす
        rest = text[e:e + 14]
        stripped = rest.lstrip("".join(L.SPACE_SET))
        if len(c) == 1:
            # 1字の語は肩書き・敬称が続くときだけ（「森弁護士」は当て、「この森に」は当てない）
            if not _starts_with_any(stripped, _AFTER_OK):
                return False
        elif stripped and _WORDISH.match(stripped[0]) and not _starts_with_any(stripped, _AFTER_OK):
            return False
        if before and _WORDISH.match(before) and not _ends_with_any(text[max(0, s - 12):s], _BEFORE_OK):
            return False
        return True

    def find(self, text: str) -> list[Hit]:
        if not self.regex or not text:
            return []
        hits: list[Hit] = []
        for m in self.regex.finditer(text):
            idx = self._index[m.lastindex - 1]
            s, e = m.span()
            if not self._guard_ok(text, s, e, idx):
                continue
            hits.append(Hit(s, e, idx, m.group(0), self.replacement(idx, m.group(0))))
        return hits

    def sub(self, text: str) -> tuple[str, list[Hit]]:
        hits = self.find(text)
        if not hits:
            return text, hits
        out, pos = [], 0
        for h in hits:
            out.append(text[pos:h.start])
            out.append(h.replacement)
            pos = h.end
        out.append(text[pos:])
        return "".join(out), hits
