"""置換した後の機械の検査。

門番は決定論の検査だけで組む（書式辞書の匿名化と仮名化ゲートで残った教訓）:
  1 伏せるはずの原文が出力に残っていない（本文だけでなく、属性・リンク先・文書情報まで）
  2 置換した記号の直後に漢字が残っていない（姓だけ当たって名が残る形）
  3 個人情報の形（電話・メール・郵便番号・口座・生年月日・番号）が残っていない
  4 出力を洗い直して、一覧に無い人名・社名が出てこない
  5 判例・条文・号証の件数が入力と同じ（伏せすぎて法的な中身を壊していない）
  6 段落数（行数・頁数）が入力と同じ
  7 Word の変更履歴・コメント・作者が残っていない
合格は「機械が見つけられなかった」という意味で、全文を目で読むことの代わりにはならない。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from . import lexicon as L
from . import sudachi as S
from .collect import Collector, context_of
from .detect import _pii, fold, protected_spans
from .matcher import KUROFILL, Entry, Matcher, _AFTER_OK, core
from .parts import OPAQUE_MARK

PASS, WARN, FAIL = "合格", "要確認", "不合格"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}
_MAX_LIST = 15


@dataclass
class Report:
    verdict: str = PASS
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def bump(self, level: str) -> None:
        if _RANK[level] > _RANK[self.verdict]:
            self.verdict = level

    def section(self, title: str, lines: list[str], level: str = PASS) -> None:
        mark = {PASS: "○", WARN: "△", FAIL: "×"}[level]
        self.sections.append((f"{mark} {title}", lines))
        self.bump(level)

    @property
    def exit_code(self) -> int:
        return _RANK[self.verdict]

    def render(self, head: list[str]) -> str:
        out = ["匿名化の検査結果", "（この報告書には伏せる前の名前が載っています。外に出さないでください）", ""]
        out += head
        out += ["", f"■ 判定: {self.verdict}" + ("（機械の検査で見つかったものはありません。外に出す前に全文を目で読んでください）" if self.verdict == PASS else ""), ""]
        for title, lines in self.sections:
            out.append(title)
            out += [f"    {ln}" for ln in lines]
            out.append("")
        if self.notes:
            out.append("確かめていないもの")
            out += [f"    ・{n}" for n in self.notes]
        return "\n".join(out) + "\n"


def _cap(lines: list[str]) -> list[str]:
    if len(lines) <= _MAX_LIST:
        return lines
    return lines[:_MAX_LIST] + [f"…ほか {len(lines) - _MAX_LIST} 件"]


def run(*, accepted: list[Entry], rejected: list[Entry], counts: dict[int, int] | None, matcher: Matcher,
        in_units: list[tuple[str, str]], out_units: list[tuple[str, str]],
        out_strings: list[tuple[str, str]], structure: tuple[str, int, int] | None,
        hygiene: dict | None, notes: list[str], address: str = "city") -> Report:
    rep = Report()

    # 置換の件数（検査だけのときは出さない）
    lines = []
    total = 0
    for i, e in enumerate(matcher.entries if counts is not None else []):
        n = counts.get(i, 0)
        total += n
        if n:
            lines.append(f"{e.cid or '-'} {e.kind} 「{e.original}」→「{e.replacement}」 {n}件")
    absent = [e for i, e in enumerate(matcher.entries) if not counts.get(i)] if counts is not None else []
    if absent:
        lines.append(f"（この文書に出てこなかった候補 {len(absent)} 件: " + "、".join(e.original for e in absent[:10])
                     + ("…" if len(absent) > 10 else "") + "）")
    if counts is not None:
        if total == 0:
            # 置換が1件も無いのに「合格」と言わない（一覧の取り違え・読めない形の文書のことがある）
            rep.section("置換したものが1件もありません（一覧と文書の取り違え・文字として読めない文書でないか確認）",
                        _cap(lines) if lines else [], WARN)
        else:
            rep.section(f"置換した件数 合計 {total} 件", _cap(lines))

    # 1 原文の残り
    strict = Matcher(accepted, guards=True)
    loose = Matcher(accepted, guards=False)
    # 置き換えた後の記号そのものは残りに数えない。ただし記号が別の行の原文と同じとき
    # （山田太郎→鈴木一郎・鈴木一郎→山田太郎のような入れ替え）は数える
    originals = {L.nfkc_name(e.original) for e in accepted}
    labels = {L.nfkc_name(e.replacement) for e in accepted
              if e.replacement and L.nfkc_name(e.replacement) not in originals}
    fails, warns, infos = [], [], []
    for text, loc in out_strings:
        opaque = loc.endswith(OPAQUE_MARK)
        for h in strict.find(text):
            if L.nfkc_name(h.matched) in labels:
                continue
            # 中を読めない部品（圧縮されたバイト列）では、偶然に現れうる2字以下の語は数えない
            if opaque and len(core(strict.entries[h.entry].original)) < 3:
                continue
            fails.append(f"「{h.matched[:30]}」 {loc}: {context_of(text, h.start, h.end)[:80]}")
    # 語の一部として残ったものは、属性値（書式の番号など）でなく本文で見る
    for text, loc in out_units:
        found = {(h.start, h.end) for h in strict.find(text)}
        for h in loose.find(text):
            if L.nfkc_name(h.matched) in labels:
                continue
            if (h.start, h.end) in found:
                continue
            after = text[h.end:h.end + 3]
            line = f"「{h.matched}」 {loc}: {context_of(text, h.start, h.end)}"
            # 「山田　次郎」: 同じ姓の別の人が一覧に無い
            if re.match(f"{L.SP}+[{L.KANJI}]", after):
                warns.append(line)
            else:
                infos.append(line)
    if fails:
        rep.section("伏せるはずの原文が残っています", _cap(fails), FAIL)
    else:
        rep.section("伏せるはずの原文の残り: なし", [])
    if warns:
        rep.section("同じ姓・名の別の人かもしれない語が残っています（一覧に足すか確認）", _cap(warns), WARN)
    if infos:
        rep.section("参考: 2字以下の語が、別の語の一部として残っています（置換していません）", _cap(infos))

    # 2 記号の直後の漢字（名の取り残し）
    reps = {e.replacement for e in accepted if e.kind in ("人名", "代理人", "法人", "手入力")
            and e.replacement and set(e.replacement) != {KUROFILL}}
    frag = []
    allowed = sorted(set(_AFTER_OK) | set(L.STOP_WORDS) | set(L.HARD_TERMS) | {"間", "同士", "相互"},
                     key=len, reverse=True)
    in_text = "\n".join(t for t, _ in in_units)
    for r in sorted(reps, key=len, reverse=True):
        pat = re.compile(r"(?<![A-Za-zＡ-Ｚａ-ｚ])" + re.escape(r) + f"{L.SP}{{0,2}}(?P<k>[{L.KANJI}]{{1,3}})")
        for text, loc in out_units:
            for m in pat.finditer(text):
                k = m.group("k")
                if any(k.startswith(a) or a.startswith(k) for a in allowed):
                    continue
                if S.person(k) is False or S.person(k[:2]) is False:
                    continue
                # 入力にもとからある形（「Ｘ線」「Ｉ滝」）は、置換で生まれたものではない
                if m.group(0) in in_text:
                    continue
                frag.append(f"「{m.group(0)}」 {loc}: {context_of(text, m.start(), m.end())}")
    if frag:
        rep.section("置換した記号の直後に漢字が残っています（名の取り残しの疑い）", _cap(frag), WARN)

    # 3 個人情報の形
    rejected_keys = {L.nfkc_name(e.original) for e in rejected}
    accepted_keys = {L.nfkc_name(e.original) for e in accepted}
    pii = []
    for text, loc in out_units:
        for h in _pii(text, fold(text)):
            if L.nfkc_name(h.text) in rejected_keys or h.kind == "事件番号" and L.nfkc_name(h.text) in accepted_keys:
                continue
            pii.append(f"{h.kind}「{h.text}」 {loc}")
    if pii:
        rep.section("個人情報の形の文字列が残っています（× にしたものは除く）", _cap(pii), WARN)

    # 4 洗い直し
    col = Collector(address=address)
    for text, loc in out_units:
        col.add(text, loc)
    known = accepted_keys | rejected_keys | {L.nfkc_name(e.replacement) for e in accepted}
    new = [c for c in col.finalize() if L.nfkc_name(c.original) not in known and c.kind in ("人名", "代理人", "法人")]
    if new:
        rep.section("一覧に無い人名・社名らしい語があります（見落としの疑い）",
                    _cap([f"{c.kind}「{c.original}」 {c.count}件 [{c.rule}] {c.context}" for c in new]), WARN)

    # 5 判例・条文・号証の件数
    def prot_count(units):
        cnt = Counter()
        for text, _loc in units:
            for _s, _e, kind in protected_spans(text):
                cnt[kind] += 1
        return cnt
    pin, pout = prot_count(in_units), prot_count(out_units)
    if pin != pout:
        rep.section("判例・条文・号証の件数が入力と違います（伏せすぎの疑い）",
                    [f"{k}: 入力 {pin.get(k, 0)} / 出力 {pout.get(k, 0)}" for k in sorted(set(pin) | set(pout))], WARN)
    elif sum(pin.values()):
        rep.section("判例・条文・号証の件数は入力と同じ",
                    [f"{k}: {v}" for k, v in sorted(pin.items())])

    # 6 構造
    if structure:
        name, a, b = structure
        if a != b:
            rep.section(f"{name}が入力と違います", [f"入力 {a} / 出力 {b}"], WARN)
        else:
            rep.section(f"{name}は入力と同じ（{a}）", [])

    # 7 Word の衛生
    if hygiene is not None:
        bad = [f"{k} {v}" for k, v in hygiene.items() if v]
        if bad:
            rep.section("Word の文書情報に残りがあります", bad, FAIL)
        else:
            rep.section("変更履歴・コメント・作者・文書変数・サムネイル: なし", [])

    rep.notes += notes
    return rep
