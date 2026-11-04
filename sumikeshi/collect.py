"""拾った候補をまとめ、同じ人・同じ会社に同じ記号を振り、本文での件数を数える。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexicon as L
from . import sudachi as S
from .detect import Hit, scan_unit
from .matcher import KUROFILL, Entry, Matcher, core
from .romaji import to_hepburn

STYLES = ("xy", "abc", "maru", "kuro")
NAME_KINDS = ("人名", "代理人", "法人")
KIND_ORDER = ["人名", "代理人", "法人", "住所", "物件", "電話", "メール", "郵便番号", "口座", "生年月日",
              "番号", "車両番号", "事件番号", "URL", "手入力"]
_LETTERS = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
_FW_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")
_CJK_ONLY = re.compile(f"[{L.KANJI}{L.KATA}{L.HIRA}々・ー{L.SPACE_CLASS}]+")


@dataclass
class Candidate:
    kind: str
    original: str
    replacement: str = ""
    accept: bool = True
    count: int = 0
    conf: str = "高"
    rule: str = ""
    context: str = ""
    files: list[str] = field(default_factory=list)
    note: str = ""
    cid: str = ""
    entity: str | None = None
    part: str = ""            # "" / sei / mei / alias / short / title
    order: int = 0


@dataclass
class _Entity:
    key: str
    kind: str
    display: str
    order: int
    side: str = ""
    sei: str = ""
    mei: str = ""
    label: str = ""
    reading: str = ""


def display_form(text: str) -> str:
    """一覧に出す原文。漢字かなの語は空白を詰め、英字の社名などは空白を残す。"""
    t = text.strip(" 　\t")
    return core(t) if _CJK_ONLY.fullmatch(t) else re.sub(r"\s+", " ", t)


def _split_name(name: str, sei: str, mei: str) -> tuple[str, str]:
    if sei and mei:
        return sei, mei
    sp = S.split_name(name)
    if sp:
        return sp
    if len(name) >= 4:
        n = 3 if name[:3] in L.SUR3 else 2
        return name[:n], name[n:]
    return "", ""


_CORP_ABBR = (("（株）", "株式会社"), ("(株)", "株式会社"), ("㈱", "株式会社"), ("（有）", "有限会社"),
              ("(有)", "有限会社"), ("㈲", "有限会社"))
_LABEL_LIKE = re.compile(r"(?<![A-Za-zＡ-Ｚａ-ｚ])([Ａ-ＺA-Z][０-９0-9]{0,2})(社)?(?![A-Za-zＡ-Ｚａ-ｚ])")


def _corp_key(s: str) -> str:
    """「(株)みどり建設」「㈱みどり建設」を「株式会社みどり建設」と同じ会社として数えるためのキー。"""
    for a, b in _CORP_ABBR:
        s = s.replace(a, b)
    return L.nfkc_name(s)


def _kana_key(s: str) -> str:
    """読みの比べ方: 空白を除き、半角カナ・ひらがなをカタカナに。"""
    s = L.nfkc_name(s)
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def _to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _part_ok(part: str) -> bool:
    """フルネームから切り出した姓・名を、単独でも当ててよいか（「組織」「真実」のような一般の語は不可）。"""
    return (len(part) >= 2 and part not in L.STOP_WORDS and not L.HARD_TERMS_RE.search(part)
            and S.person(part) is not False)


def _letter(i: int, skip_xy: bool) -> str:
    letters = [c for c in _LETTERS if not (skip_xy and c in "ＸＹ")]
    n = len(letters)
    return letters[i % n] + ("" if i < n else str(i // n + 1).translate(_FW_DIGITS))


class Collector:
    """複数のファイル・段落から候補を集める。"""

    def __init__(self, style: str = "xy", address: str = "city", keep: list[str] | None = None):
        if style not in STYLES:
            raise ValueError(f"style は {STYLES} のどれか")
        self.style = style
        self.address = address
        self.keep = {L.nfkc_name(k) for k in (keep or []) if k.strip()}
        self.units: list[tuple[str, str]] = []      # (本文, 出所)
        self.hits: list[tuple[Hit, int]] = []       # (候補, 何番目の段落か)

    def add(self, text: str, source: str) -> None:
        idx = len(self.units)
        self.units.append((text, source))
        for h in scan_unit(text, self.address):
            self.hits.append((h, idx))

    # ------------------------------------------------------------ まとめ
    def finalize(self) -> list[Candidate]:
        ents: dict[str, _Entity] = {}
        rows: dict[str, Candidate] = {}           # 照合キー → 候補
        order = 0

        def ent_for(key: str, kind: str, display: str, side: str = "") -> _Entity:
            nonlocal order
            if key not in ents:
                order += 1
                ents[key] = _Entity(key, kind, display, order, side)
            e = ents[key]
            if side and not e.side:
                e.side = side
            if kind == "法人" and e.kind != "法人":
                e.kind = "法人"
            elif kind == "代理人" and e.kind == "人名":
                e.kind = "代理人"
            return e

        def add_row(kind, original, rule, conf, entity=None, part="", replacement=None) -> Candidate:
            nonlocal order
            key = L.nfkc_name(original)
            if not key:
                return None
            row = rows.get(key)
            if row is None:
                order += 1
                row = Candidate(kind, original, replacement or "", conf=conf, rule=rule,
                                entity=entity, part=part, order=order)
                rows[key] = row
            else:
                # 強い根拠を優先して残す
                if conf == "高" and row.conf != "高":
                    row.conf, row.rule = conf, rule
                if kind in NAME_KINDS and row.kind in NAME_KINDS:
                    if kind == "法人" or (kind == "代理人" and row.kind == "人名"):
                        row.kind = kind
                if entity and (not row.entity or row.part in ("sei", "mei") and not part):
                    row.entity, row.part = entity, part
                if replacement and not row.replacement:
                    row.replacement = replacement
            return row

        pending_parts: list[tuple[Hit, str]] = []
        for h, _idx in self.hits:
            disp = display_form(h.text)
            if h.part in ("reading", "romaji"):
                pending_parts.append((h, h.part))
                continue
            if h.kind in NAME_KINDS and h.part in ("", "alias") and not (h.part == "" and h.entity):
                if h.part == "alias":
                    tkey = (_corp_key if h.kind == "法人" else L.nfkc_name)(h.entity or "")
                    ent_for(tkey, h.kind, core(h.entity or ""))
                    add_row(h.kind, disp, h.rule, h.conf, entity=tkey, part="alias")
                    continue
                c = core(h.text)
                if len(c) >= 2 and h.rule.startswith("1字の姓"):
                    pending_parts.append((h, "title"))
                    continue
                key = _corp_key(c) if h.kind == "法人" else L.nfkc_name(c)
                e = ent_for(key, h.kind, disp, h.side)
                if h.kind != "法人" and re.fullmatch(f"[{L.KANJI}]+", c):
                    sei, mei = _split_name(c, h.sei, h.mei)
                    if sei and not e.sei:
                        e.sei, e.mei = sei, mei
                add_row(h.kind, disp, h.rule, h.conf, entity=key)
            elif h.kind in NAME_KINDS:
                pending_parts.append((h, h.part))
            else:
                add_row(h.kind, disp, h.rule, h.conf, replacement=h.replacement)

        # 法人の略称（法人格を外した形）
        for e in list(ents.values()):
            if e.kind != "法人":
                continue
            short = e.display
            for f in sorted(L.CORP_FORMS, key=len, reverse=True):
                short = short.replace(f, "")
            short = short.strip(" 　")
            if len(short) >= 2 and short != e.display and core(short) not in L.STOP_WORDS:
                sk = L.nfkc_name(short)
                if sk in ents and sk != e.key:
                    # 屋号として別に拾った「みどり建設」を正式名に寄せる
                    del ents[sk]
                add_row("法人", short, f"略称（{e.display} の）", "中", entity=e.key, part="short")
                rows[sk].entity, rows[sk].part = e.key, "short"
            # 「(株)みどり建設」「㈱みどり建設」の書き方でも当てる
            for a, b in _CORP_ABBR:
                if b in e.display:
                    add_row("法人", e.display.replace(b, a), f"略記（{e.display} の）", "中", entity=e.key, part="short")

        # 姓・名（フルネームから）
        sei_owner: dict[str, list[_Entity]] = {}
        for e in ents.values():
            if e.kind != "法人" and e.sei:
                sei_owner.setdefault(L.nfkc_name(e.sei), []).append(e)
        # 役割語の後で姓だけ拾った人（「運転手山田」）は、その姓のフルネームが1人だけならその人に寄せる
        for e in list(ents.values()):
            owners = sei_owner.get(e.key, [])
            if e.kind != "法人" and not e.mei and len(owners) == 1 and owners[0] is not e:
                for r in rows.values():
                    if r.entity == e.key:
                        r.entity, r.part = owners[0].key, r.part or "sei"
                del ents[e.key]
        for e in list(ents.values()):
            if e.kind == "法人" or not e.sei:
                continue
            owners = sei_owner.get(L.nfkc_name(e.sei), [])
            note = "・".join(o.display for o in owners) + " に共通の姓。文脈で確認" if len(owners) > 1 else ""
            if _part_ok(e.sei):
                r = add_row(e.kind, e.sei, f"姓（{e.display} の）", "中", entity=owners[0].key if owners else e.key, part="sei")
                if note:
                    r.note = note
            if _part_ok(e.mei):
                add_row(e.kind, e.mei, f"名（{e.display} の）", "中", entity=e.key, part="mei")

        # 読み（カナ・ひらがな・ローマ字）: 漢字の名前から作り、本文に出てくれば同じ記号で伏せる
        for e in list(ents.values()):
            name = core(e.display)
            if e.kind == "法人" or not re.fullmatch(f"[{L.KANJI}]+", name):
                continue
            rd = S.reading(name)
            if rd and len(rd) >= 3:
                e.reading = rd
                add_row(e.kind, rd, f"読み（{e.display} の）", "中", entity=e.key, part="reading")
                add_row(e.kind, _to_hira(rd), f"読み（{e.display} の）", "中", entity=e.key, part="reading")
            sr = S.reading(e.sei) if e.sei else None
            mr = S.reading(e.mei) if e.mei else None
            for r in (sr, mr):
                if r and len(r) >= 3:
                    add_row(e.kind, r, f"読み（{e.display} の）", "中", entity=e.key, part="reading")
            if sr and mr:
                rs, rm = to_hepburn(sr).capitalize(), to_hepburn(mr).capitalize()
                if rs and rm:
                    for disp in (f"{rm} {rs}", f"{rs} {rm}"):
                        add_row("ローマ字", disp, f"ローマ字（{e.display} の）", "中", entity=e.key, part="romaji")

        # 形態素で拾った姓だけ・名だけ、1字の姓＋肩書き、読み
        for h, part in pending_parts:
            c = core(h.text)
            key = L.nfkc_name(c)
            if part in ("reading", "romaji"):
                disp = display_form(h.text)
                if h.entity:
                    ek = L.nfkc_name(h.entity)
                    if ek not in ents:
                        ent_for(ek, "人名", core(h.entity))
                    add_row(h.kind, disp, h.rule, h.conf, entity=ek, part=part)
                else:
                    owner = next((e for e in ents.values() if e.reading and e.reading == _kana_key(c)), None)
                    if owner is None:
                        owner = ent_for(L.nfkc_name(c), "人名", disp)
                    add_row(h.kind, disp, h.rule, h.conf, entity=owner.key, part=part)
                continue
            if part == "title":
                head = c[0]
                owners = [e for e in ents.values() if e.kind != "法人" and (e.sei == head or (not e.sei and e.display.startswith(head)))]
                if len(owners) == 1:
                    add_row(h.kind, c, h.rule, h.conf, entity=owners[0].key, part="title")
                else:
                    e = ent_for(L.nfkc_name(head) + "#1", h.kind, head, h.side)
                    add_row(h.kind, c, h.rule, h.conf, entity=e.key, part="title")
                continue
            if key in rows:
                continue
            if part == "sei":
                owners = sei_owner.get(key, [])
                if len(owners) == 1:
                    add_row(h.kind, c, h.rule, h.conf, entity=owners[0].key, part="sei")
                else:
                    e = ent_for(key, h.kind, c, h.side)
                    r = add_row(h.kind, c, h.rule, h.conf, entity=e.key)
                    if r.conf == "低":
                        r.note = "形態素だけが根拠。人名でなければ × に"
            elif part == "mei":
                owner = next((e for e in ents.values() if L.nfkc_name(e.mei) == key), None)
                r = add_row(h.kind, c, h.rule, h.conf, entity=owner.key if owner else None, part="mei")
                if owner is None:
                    r.accept = False
                    r.note = "名だけ。一般の語のこともあるので文脈で確認（伏せるなら○に）"

        cands = list(rows.values())
        self._assign_labels(ents, cands)
        self._count(cands)
        # 派生した行（姓・名・略称）で本文に1件も無いものは一覧から落とす
        cands = [c for c in cands if c.count > 0 or (c.part not in ("sei", "mei", "short", "alias", "reading", "romaji")
                                                       and not c.rule.startswith("形態素"))]
        for c in cands:
            if c.count == 0:
                c.note = (c.note + " " if c.note else "") + "本文で当たらない（境界の判定で外れた）。文脈を確認"
            if L.nfkc_name(c.original) in self.keep or (c.entity and c.entity in self.keep):
                c.accept = False
                c.note = (c.note + " " if c.note else "") + "--keep で残す指定"

        def sort_key(c: Candidate):
            e = ents.get(c.entity) if c.entity else None
            if (c.kind in NAME_KINDS or c.part in ("reading", "romaji")) and e is not None:
                part_rank = {"": 0, "alias": 1, "short": 1, "title": 2, "sei": 3, "mei": 4, "reading": 5, "romaji": 6}.get(c.part, 7)
                return (0, e.order, part_rank, c.order)
            return (1, KIND_ORDER.index(c.kind) if c.kind in KIND_ORDER else 99, c.order, 0)

        cands.sort(key=sort_key)
        for i, c in enumerate(cands, 1):
            c.cid = f"C{i:03d}"
        return cands

    # ------------------------------------------------------------ 記号
    def _assign_labels(self, ents: dict[str, _Entity], cands: list[Candidate]) -> None:
        style = self.style
        ordered = sorted(ents.values(), key=lambda e: e.order)
        # 文書にもとからある伏せ字（「Ａ社」「Ｂ」「b3」）と同じ記号は使わない
        # （当事者が入れ替わって読める・検査で原文の残りと区別できなくなる）
        taken = {L.nfkc_name(c.original).upper() for c in cands}
        for text, _src in self.units:
            for m in _LABEL_LIKE.finditer(text):
                taken.add(L.nfkc_name(m.group(0)).upper())
                taken.add(L.nfkc_name(m.group(1)).upper())
        if style == "xy":
            xs = [e for e in ordered if e.side == "X"]
            ys = [e for e in ordered if e.side == "Y"]
            for grp, letter in ((xs, "Ｘ"), (ys, "Ｙ")):
                for i, e in enumerate(grp, 1):
                    label = letter if len(grp) == 1 else letter + str(i).translate(_FW_DIGITS)
                    if L.nfkc_name(label).upper() not in taken and L.nfkc_name(label + "社").upper() not in taken:
                        e.label = label
        i = 0
        for e in ordered:
            if not e.label:
                label = _letter(i, skip_xy=(style == "xy"))
                while L.nfkc_name(label).upper() in taken or L.nfkc_name(label + "社").upper() in taken:
                    i += 1
                    label = _letter(i, skip_xy=(style == "xy"))
                e.label = label
                i += 1
        for c in cands:
            e = ents.get(c.entity) if c.entity else None
            if style == "kuro":
                if c.kind == "住所" and c.replacement.endswith("○○"):
                    keep = c.replacement[:-2]
                    c.replacement = keep + KUROFILL * max(1, len(core(c.original)) - len(core(keep)))
                elif c.part == "title":
                    c.replacement = KUROFILL + core(c.original)[1:]
                else:
                    c.replacement = KUROFILL * len(core(c.original))
                continue
            if (c.kind in NAME_KINDS or c.part in ("reading", "romaji")) and e is not None:
                label = "○○" if style == "maru" else e.label
                if e.kind == "法人" and style != "maru":
                    label += "社"
                c.replacement = label + core(c.original)[1:] if c.part == "title" else label
            elif not c.replacement:
                c.replacement = "○○"

    # ------------------------------------------------------------ 件数と文脈
    def _count(self, cands: list[Candidate]) -> None:
        m = Matcher([Entry(c.original, c.replacement, c.kind) for c in cands])
        by_core = {core(e.original): e for e in m.entries}
        idx_of = {id(e): i for i, e in enumerate(m.entries)}
        cand_of = {}
        for c in cands:
            e = by_core.get(core(c.original))
            if e is not None:
                cand_of.setdefault(idx_of[id(e)], c)
        for text, source in self.units:
            for h in m.find(text):
                c = cand_of.get(h.entry)
                if c is None:
                    continue
                c.count += 1
                if source not in c.files:
                    c.files.append(source)
                if not c.context:
                    c.context = context_of(text, h.start, h.end)


def context_of(text: str, s: int, e: int, width: int = 14) -> str:
    before = text[max(0, s - width):s]
    after = text[e:e + width]
    snip = f"{'…' if s > width else ''}{before}【{text[s:e]}】{after}{'…' if e + width < len(text) else ''}"
    return snip.replace("\n", "⏎").replace("\t", " ")
