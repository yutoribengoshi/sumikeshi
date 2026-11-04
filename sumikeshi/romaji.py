"""カタカナの読みをヘボン式のローマ字にする（名前のローマ字表記「Taro Yamada」を照合するため）。

旅券のヘボン式に寄せる: 長音は書かない（タロウ→taro、ユウコ→yuko）、ン は n。
照合の側（matcher）で、長音の u・h・o の有無と、b・m・p の前の n／m の揺れを許す。
"""
from __future__ import annotations

_BASE = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヰ": "i", "ヱ": "e", "ヲ": "o", "ン": "n",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "ヴ": "vu", "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
}
_YOON = {"ャ": "a", "ュ": "u", "ョ": "o"}
_SPECIAL = {"シ": "sh", "チ": "ch", "ジ": "j", "ヂ": "j"}
_FOREIGN = {
    "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo", "ティ": "ti", "ディ": "di", "シェ": "she",
    "ジェ": "je", "チェ": "che", "ウィ": "wi", "ウェ": "we", "ウォ": "wo", "ヴァ": "va", "ヴィ": "vi",
    "ヴェ": "ve", "ヴォ": "vo", "トゥ": "tu", "ドゥ": "du",
}


def _hira_to_kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def to_hepburn(reading: str) -> str:
    """カタカナ（ひらがなでもよい）→ ヘボン式の小文字。変換できない字があれば空文字。"""
    s = _hira_to_kata(reading)
    out: list[str] = []
    i = 0
    geminate = False
    while i < len(s):
        two = s[i:i + 2]
        ch = s[i]
        if ch == "ッ":
            geminate = True
            i += 1
            continue
        if ch == "ー":
            i += 1
            continue
        if two in _FOREIGN:
            syl, i = _FOREIGN[two], i + 2
        elif len(two) == 2 and two[1] in _YOON and ch in _BASE:
            stem = _SPECIAL.get(ch, _BASE[ch][:-1] + "y")
            syl, i = stem + _YOON[two[1]], i + 2
        elif ch in _BASE:
            syl, i = _BASE[ch], i + 1
        else:
            return ""
        if geminate:
            syl = ("t" + syl) if syl.startswith("ch") else (syl[0] + syl)
            geminate = False
        out.append(syl)
    word = "".join(out)
    # 長音を書かない旅券の形にそろえる（ou・oo → o、uu → u）
    for a, b in (("ou", "o"), ("oo", "o"), ("uu", "u")):
        word = word.replace(a, b)
    return word
