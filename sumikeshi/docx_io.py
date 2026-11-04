"""Word（.docx）の読み取りと置換。

- 変更履歴はすべて承諾した形にする（削除された文字は消し、挿入された文字は本文に残す）。
  python-docx の .text では見えない削除文字が、後の工程で「復活」する事故を防ぐ
- コメント・作者一覧（people.xml）・XML のコメントを消す
- 作成者・最終更新者・会社名・テンプレートのパス・文書変数・差し込み印刷のデータ元・文書の識別子（docId）・
  rsid・編集の制限の編集者・サムネイル画像・プリンタの設定を消し、作成・更新の日時をいまに置き換える
- 本文・ヘッダー・フッター・脚注・文末脚注・テキストボックス・振り仮名・数式・図形とグラフの文字の段落を、
  run をまたいで置換する。置換しない文字の書式はそのまま残る
- フィールドコード（フィールドごと）・属性（代替テキスト・リンク先・ドロップダウンの項目など）・
  カスタムXML・埋め込みの Excel の中も置換する。画像の付帯情報（EXIF・XMP）は取り除く
- 中を伏せられない形（Strict 形式・altChunk の埋め込み文書）は、作らずに止める
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from lxml import etree

from . import parts as P
from .matcher import Matcher

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"
STRICT_W = "http://purl.oclc.org/ooxml/wordprocessingml/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


_REMOVE = [w(t) for t in (
    "del", "moveFrom", "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "tblPrExChange", "trPrChange",
    "tcPrChange", "tblGridChange", "numberingChange", "cellDel", "cellIns", "cellMerge", "moveFromRangeStart",
    "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd", "customXmlDelRangeStart", "customXmlDelRangeEnd",
    "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlMoveFromRangeStart",
    "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd", "permStart", "permEnd")]
_UNWRAP = [w("ins"), w("moveTo")]
_REVISION_COUNT = [w(t) for t in ("ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange", "tblPrExChange")]
# 段落の文字を集めるとき、中に入らない要素
_SKIP = {w(t) for t in ("pPr", "rPr", "sdtPr", "del", "moveFrom", "instrText", "delText", "delInstrText",
                        "fldData", "txbxContent", "p", "tblPr", "trPr", "tcPr", "sectPr", "rt", "rubyPr")}
# 触らない属性（参照・番号・書式の識別子）。それ以外の属性は、伏せる語が入っていれば置き換える
_NO_TOUCH_ATTRS = {"id", "Id", "styleId", "embed", "link", "pict", "Type", "TargetMode", "PartName", "ContentType",
                   "Extension", "uri", "Ignorable", "ProcessContent", "MustUnderstand", "space"}
_STYLE_REF = {"pStyle", "rStyle", "tblStyle", "numStyleLink", "styleLink", "basedOn", "next", "link", "numId",
              "abstractNumId", "ilvl", "lvl"}
_NOT_STORY = re.compile(r"^word/(styles|numbering|fontTable|settings|webSettings|stylesWithEffects)\.xml$|^word/theme/")


class UnsupportedDocx(ValueError):
    """伏せきれない形の文書（作らずに止める）。"""


@dataclass
class DocxStats:
    revisions: int = 0
    comments: int = 0
    paragraphs: int = 0
    images: int = 0
    embeddings: int = 0
    macros: bool = False
    thumbnail: bool = False
    opaque: list = field(default_factory=list)   # 中を伏せられない部品の名前
    counts: dict = field(default_factory=dict)   # Matcher.entries の添字 → 件数
    warnings: list = field(default_factory=list)

    def add(self, idx: int) -> None:
        self.counts[idx] = self.counts.get(idx, 0) + 1


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse(data: bytes):
    return etree.fromstring(data, _PARSER)


def _serialize(root) -> bytes:
    sa = root.getroottree().docinfo.standalone
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=sa)


def _remove_keep_tail(node) -> None:
    """要素を消す（後ろに続く文字 tail は前の要素か親に付け替えて残す）。"""
    parent = node.getparent()
    if parent is None:
        return
    if node.tail:
        prev = node.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
    parent.remove(node)


# ------------------------------------------------------------ 変更履歴・コメント
def _accept_revisions(root, stats: DocxStats | None) -> None:
    if stats is not None:
        stats.revisions += sum(1 for _ in root.iter(*_REVISION_COUNT))
    # 削除が記録された表の行は、行ごと消す（承諾した形）
    for tr in list(root.iter(w("tr"))):
        trpr = tr.find(w("trPr"))
        if trpr is not None and trpr.find(w("del")) is not None and tr.getparent() is not None:
            tr.getparent().remove(tr)
    for el in list(root.iter(*_REMOVE)):
        if el.getparent() is not None:
            _remove_keep_tail(el)
    for el in list(root.iter(*_UNWRAP)):
        parent = el.getparent()
        if parent is None:
            continue
        idx = parent.index(el)
        for child in list(el):
            parent.insert(idx, child)
            idx += 1
        parent.remove(el)


def _strip_comment_anchors(root, stats: DocxStats | None) -> None:
    for el in list(root.iter(w("commentReference"))):
        run = el.getparent()
        if stats is not None:
            stats.comments += 1
        if run is not None and run.tag == w("r") and all(c.tag in (w("rPr"), w("commentReference")) for c in run):
            if run.getparent() is not None:
                run.getparent().remove(run)
        elif run is not None:
            run.remove(el)
    for el in list(root.iter(w("commentRangeStart"), w("commentRangeEnd"))):
        if el.getparent() is not None:
            el.getparent().remove(el)


def _strip_xml_comments(root) -> None:
    for c in list(root.iter(etree.Comment)):
        _remove_keep_tail(c)


def _strip_rsids(root) -> None:
    for el in root.iter():
        if isinstance(el.tag, str):
            for name in [n for n in el.attrib if _local(n).startswith("rsid")]:
                del el.attrib[name]


# ------------------------------------------------------------ 段落
def _segments(p) -> list[tuple]:
    """段落の文字の並び。(文字の要素, None) か (None, 区切り文字)。入れ子の段落は含めない。"""
    segs: list[tuple] = []
    w_t, m_t = w("t"), f"{{{MATH}}}t"

    def walk(el):
        for ch in el:
            tag = ch.tag
            if not isinstance(tag, str) or tag in _SKIP:
                continue
            if tag in (w_t, m_t):
                segs.append((ch, None))
            elif tag == w("tab"):
                segs.append((None, "\t"))
            elif tag in (w("br"), w("cr")):
                segs.append((None, "\n"))
            elif tag == w("noBreakHyphen"):
                segs.append((None, "-"))
            elif tag == w("sym"):
                segs.append((None, "￼"))
            else:
                walk(ch)

    walk(p)
    return segs


def _segments_a(p) -> list[tuple]:
    """図形・グラフ・SmartArt の段落（a:p）の文字の並び。"""
    segs: list[tuple] = []
    for el in p.iter(f"{{{A}}}t", f"{{{A}}}br"):
        segs.append((el, None) if _local(el.tag) == "t" else (None, "\n"))
    return segs


def paragraph_text(p) -> str:
    return "".join((el.text or "") if el is not None else sep for el, sep in _segments(p))


def _replace_in_segments(segs, matcher: Matcher, stats: DocxStats) -> bool:
    if not segs:
        return False
    texts = [(el.text or "") if el is not None else sep for el, sep in segs]
    hits = matcher.find("".join(texts))
    if not hits:
        return False
    starts, pos = [], 0
    for t in texts:
        starts.append(pos)
        pos += len(t)
    # 右から置き換えると、左の一致の位置がずれない
    for h in reversed(hits):
        first = True
        for k, (el, _sep) in enumerate(segs):
            a0, b0 = starts[k], starts[k] + len(texts[k])
            if el is None or b0 <= h.start or a0 >= h.end:
                continue
            cur = el.text or ""
            a, b = max(h.start, a0) - a0, min(h.end, b0) - a0
            el.text = cur[:a] + (h.replacement if first else "") + cur[b:]
            if el.tag == w("t"):
                el.set(XML_SPACE, "preserve")
            first = False
        stats.add(h.entry)
    return True


def _apply_paragraph(p, matcher: Matcher, stats: DocxStats) -> None:
    rubies = [(rb, [t.text for t in rb.iter(w("t"))]) for rb in p.iter(w("rubyBase"))]
    if not _replace_in_segments(_segments(p), matcher, stats):
        return
    # 振り仮名の親文字を置き換えたら、振り仮名（読み）も消す
    for rb, before in rubies:
        if [t.text for t in rb.iter(w("t"))] != before:
            ruby = rb.getparent()
            rt = ruby.find(w("rt")) if ruby is not None else None
            if rt is not None:
                for t in rt.iter(w("t")):
                    t.text = ""


def _field_groups(p) -> list[list]:
    """段落のフィールドごとに、指示文（w:instrText）の要素をまとめる。入れ子のフィールドも分ける。"""
    groups: list[list] = []
    stack: list[list] = []
    for el in p.iter(w("fldChar"), w("instrText")):
        if el.tag == w("fldChar"):
            kind = el.get(w("fldCharType"))
            if kind == "begin":
                stack.append([])
            elif kind in ("separate", "end") and stack:
                g = stack.pop()
                if g:
                    groups.append(g)
        elif stack:
            stack[-1].append(el)
        else:
            groups.append([el])
    groups += [g for g in stack if g]
    return groups


def _apply_fields(p, matcher: Matcher, stats: DocxStats) -> None:
    for group in _field_groups(p):
        full = "".join(el.text or "" for el in group)
        new, hits = matcher.sub(full)
        if hits:
            group[0].text = new
            group[0].set(XML_SPACE, "preserve")
            for el in group[1:]:
                el.text = ""
            for h in hits:
                stats.add(h.entry)


def _sub_text(s: str, matcher: Matcher, stats: DocxStats) -> str:
    new, hits = matcher.sub(s)
    for h in hits:
        stats.add(h.entry)
    return new


def _touch_attr(el, name: str) -> bool:
    an = _local(name)
    if an in _NO_TOUCH_ATTRS or an.startswith("rsid"):
        return False
    return not (an == "val" and _local(el.tag) in _STYLE_REF)


def _apply_other_text(root, matcher: Matcher, stats: DocxStats) -> None:
    """段落以外の文字（要素の文字・後ろに続く文字・処理命令）と、属性の値を置換する。"""
    for el in root.iter():
        tag = el.tag
        if tag is etree.ProcessingInstruction:
            if el.text:
                el.text = _sub_text(el.text, matcher, stats)
            continue
        if not isinstance(tag, str):
            continue
        if tag not in (w("t"), w("instrText"), w("delText"), f"{{{MATH}}}t", f"{{{A}}}t") and el.text and el.text.strip():
            el.text = _sub_text(el.text, matcher, stats)
        if el.tail and el.tail.strip():
            el.tail = _sub_text(el.tail, matcher, stats)
        for name, val in list(el.attrib.items()):
            if val.strip() and _touch_attr(el, name):
                new = _sub_text(val, matcher, stats)
                if new != val:
                    el.set(name, new)


# ------------------------------------------------------------ パッケージ
class _Package:
    def __init__(self, path):
        with zipfile.ZipFile(path) as z:
            self.infos = z.infolist()
            self.data = {i.filename: z.read(i.filename) for i in self.infos}
        if "word/document.xml" not in self.data:
            raise UnsupportedDocx("Word の文書（word/document.xml）が見つかりません。.docx ですか？")
        head = self.data["word/document.xml"][:2000]
        if STRICT_W.encode() in head:
            raise UnsupportedDocx("Strict Open XML 形式の Word 文書です。Word で開き、通常の「Word 文書（.docx）」"
                                  "として保存し直してから使ってください")
        rels = self.data.get("word/_rels/document.xml.rels", b"")
        if b"/aFChunk" in rels or b"altChunk" in self.data["word/document.xml"]:
            raise UnsupportedDocx("別の文書（HTML・RTF・Word）を丸ごと埋め込んだ部分（altChunk）があり、伏せきれません。"
                                  "Word で開いて保存し直すと本文に取り込まれるので、そのあとで使ってください")
        self.dropped: set[str] = set()

    def xml_parts(self):
        return [n for n, b in self.data.items() if n not in self.dropped and P.looks_xml(n, b)]

    def other_parts(self):
        return [n for n, b in self.data.items() if n not in self.dropped and not P.looks_xml(n, b)]


def _is_story(name: str, root) -> bool:
    return name.startswith("word/") and not _NOT_STORY.match(name) and root.find(f".//{w('p')}") is not None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(name: str, root, stats: DocxStats | None) -> None:
    """名前を伏せる前の掃除（変更履歴・コメント・作者・文書情報・識別子）。"""
    _strip_xml_comments(root)
    if name.startswith("word/") and name.endswith(".xml"):
        _accept_revisions(root, stats)
        _strip_comment_anchors(root, stats)
        _strip_rsids(root)
    local = name.rsplit("/", 1)[-1]
    if re.fullmatch(r"comments(Extended|Ids|Extensible)?\.xml|people\.xml", local) and name.startswith("word/"):
        for child in list(root):
            root.remove(child)
    elif name == "word/settings.xml":
        for el in list(root.iter()):
            if isinstance(el.tag, str) and _local(el.tag) in ("attachedTemplate", "docVars", "mailMerge", "rsids", "docId"):
                if el.getparent() is not None:
                    el.getparent().remove(el)
    elif name == "docProps/core.xml":
        for el in root:
            ln = _local(el.tag)
            if ln in ("creator", "lastModifiedBy"):
                el.text = ""
            elif ln in ("created", "modified"):
                el.text = _now()
            elif ln == "revision":
                el.text = "1"
    elif name == "docProps/app.xml":
        for el in root:
            if _local(el.tag) in ("Company", "Manager", "Template", "HyperlinkBase"):
                el.text = ""
            elif _local(el.tag) == "TotalTime":
                el.text = "0"
    elif name == "docProps/custom.xml":
        for child in list(root):
            root.remove(child)
    elif name.endswith(".rels"):
        for rel in list(root):
            typ, target = rel.get("Type", ""), rel.get("Target", "")
            if typ.endswith(("/attachedTemplate", "/mailMergeSource", "/mailMergeHeaderSource", "/printerSettings")) \
                    or re.search(r"thumbnail\.\w+$|printerSettings\d*\.bin$", target):
                root.remove(rel)
    elif name == "[Content_Types].xml":
        for el in list(root):
            if re.search(r"/docProps/thumbnail\.\w+$|printerSettings\d*\.bin$", el.get("PartName", "")):
                root.remove(el)


def _drop_parts(pkg: _Package, stats: DocxStats | None) -> None:
    for n in list(pkg.data):
        if re.fullmatch(r"docProps/thumbnail\.\w+", n):
            pkg.dropped.add(n)
            if stats is not None:
                stats.thumbnail = True
        elif re.search(r"printerSettings\d*\.bin$", n):
            pkg.dropped.add(n)


def read_units(path) -> list[tuple[str, str]]:
    """候補を拾うための文字（段落ごと）。変更履歴は承諾した形で読む。[(文字, 場所)]"""
    pkg = _Package(path)
    units: list[tuple[str, str]] = []
    for name in pkg.xml_parts():
        try:
            root = _parse(pkg.data[name])
        except etree.XMLSyntaxError:
            continue
        _clean(name, root, None)
        story = _is_story(name, root)
        if story:
            for p in root.iter(w("p")):
                t = paragraph_text(p)
                if t.strip():
                    units.append((t, name))
                for g in _field_groups(p):
                    instr = "".join(el.text or "" for el in g)
                    if instr.strip():
                        units.append((instr, name + "（フィールド）"))
            for rt in root.iter(w("rt")):
                t = paragraph_text(rt)
                if t.strip():
                    units.append((t, name + "（振り仮名）"))
        for ap in root.iter(f"{{{A}}}p"):
            t = "".join((el.text or "") if el is not None else sep for el, sep in _segments_a(ap))
            if t.strip():
                units.append((t, name + "（図形・グラフ）"))
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if el.text and el.text.strip() and el.tag not in (w("t"), w("instrText"), w("delText"), f"{{{A}}}t") \
                    and (not story or _local(el.tag) not in ("t",)):
                units.append((el.text, name))
            if el.tail and el.tail.strip():
                units.append((el.tail, name))
            for an, val in el.attrib.items():
                if val.strip() and _touch_attr(el, an) and re.search(r"[^\x00-\x7f]|@", val):
                    units.append((val, name + "（属性）"))
            if name.endswith(".rels") and el.get("TargetMode") == "External":
                units.append((el.get("Target", ""), name))
    for name in pkg.other_parts():
        if P.is_zip(pkg.data[name]):
            units += [(t, loc) for t, loc in P.zip_texts(pkg.data[name], name) if t.strip()]
    return units


_TAG_SPLIT = re.compile(r"(<[^>]*>)")
_ATTR_VAL = re.compile(r"""(=\s*)(["'])(.*?)(\2)""", re.S)


def _regex_scrub(data: bytes, matcher: Matcher, stats: DocxStats) -> bytes:
    """XML として読めない部品（名前空間の宣言の無い古い VML など）は、タグの間の文字と属性の値だけを置換する。"""
    text = data.decode("utf-8", "surrogateescape")
    out = []
    for seg in _TAG_SPLIT.split(text):
        if seg.startswith("<"):
            seg = _ATTR_VAL.sub(lambda m: m.group(1) + m.group(2) + _sub_text(m.group(3), matcher, stats) + m.group(4), seg)
        elif seg.strip():
            seg = _sub_text(seg, matcher, stats)
        out.append(seg)
    return "".join(out).encode("utf-8", "surrogateescape")


def _process_xml(name: str, data: bytes, matcher: Matcher, stats: DocxStats) -> bytes:
    try:
        root = _parse(data)
    except etree.XMLSyntaxError:
        stats.warnings.append(f"{name} は XML として読めなかったので、タグの間の文字と属性だけを置換しました")
        return _regex_scrub(data, matcher, stats)
    _clean(name, root, stats)
    if _is_story(name, root):
        for p in root.iter(w("p")):
            _apply_paragraph(p, matcher, stats)
            _apply_fields(p, matcher, stats)
        for rt in root.iter(w("rt")):
            _replace_in_segments(_segments(rt), matcher, stats)
        if name == "word/document.xml":
            stats.paragraphs = sum(1 for _ in root.iter(w("p")))
    for ap in root.iter(f"{{{A}}}p"):
        _replace_in_segments(_segments_a(ap), matcher, stats)
    if name.endswith(".rels"):
        for rel in root:
            if rel.get("TargetMode") == "External":
                t = rel.get("Target", "")
                new = _sub_text(t, matcher, stats)
                if new != t:
                    rel.set("Target", new)
    else:
        _apply_other_text(root, matcher, stats)
    return _serialize(root)


def apply(path, out_path, matcher: Matcher) -> DocxStats:
    stats = DocxStats()
    pkg = _Package(path)
    _drop_parts(pkg, stats)
    new_data: dict[str, bytes] = {}
    for name in pkg.xml_parts():
        new_data[name] = _process_xml(name, pkg.data[name], matcher, stats)
    for name in pkg.other_parts():
        data = pkg.data[name]
        if name.endswith("vbaProject.bin"):
            stats.macros = True
            stats.opaque.append(name)
        elif P.is_zip(data):
            stats.embeddings += 1
            # 埋め込みの文書の中も同じ規則で伏せる（中の docProps の作成者も消える）
            new_data[name] = P.map_zip(data, lambda n, b: _process_xml(n, b, matcher, stats))
        elif name.startswith("word/media/"):
            stats.images += 1
            new_data[name] = P.strip_image(name, data)
        else:
            stats.opaque.append(name)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in pkg.infos:
            if info.filename in pkg.dropped:
                continue
            zout.writestr(info, new_data.get(info.filename, pkg.data[info.filename]))
    return stats


# ------------------------------------------------------------ 検査用
def all_strings(path) -> list[tuple[str, str]]:
    """出力を検査するための文字をすべて（段落・要素の文字・後ろの文字・処理命令・すべての属性値・
    リンク先・埋め込みの文書の中・中を読めない部品の読める部分）。[(文字, 場所)]"""
    pkg = _Package(path)
    out: list[tuple[str, str]] = []
    for name in pkg.xml_parts():
        data = pkg.data[name]
        out.append((P.xml_text(data), name + "（タグを除いた全文）"))
        try:
            root = _parse(data)
        except etree.XMLSyntaxError:
            out.append((data.decode("utf-8", "replace"), name))
            continue
        story = _is_story(name, root)
        if story:
            for p in root.iter(w("p")):
                t = paragraph_text(p)
                if t.strip():
                    out.append((t, name))
        for el in root.iter():
            if el.tag is etree.ProcessingInstruction or el.tag is etree.Comment:
                out.append((el.text or "", name + "（コメント・処理命令）"))
                continue
            if not isinstance(el.tag, str):
                continue
            if el.text and el.text.strip() and not (story and el.tag == w("t")):
                out.append((el.text, name))
            if el.tail and el.tail.strip():
                out.append((el.tail, name))
            for _an, val in el.attrib.items():
                if val.strip():
                    out.append((val, name + "（属性）"))
    for name in pkg.other_parts():
        data = pkg.data[name]
        if P.is_zip(data):
            out += P.zip_texts(data, name)
        elif not P.is_image(data):
            out.append((P.binary_text(data), name + P.OPAQUE_MARK))
    return out


def hygiene(path) -> dict:
    """変更履歴・コメント・作者などが残っていないか数える。"""
    pkg = _Package(path)
    res = {"変更履歴": 0, "コメント": 0, "作者一覧": 0, "作成者・更新者": 0, "文書変数": 0,
           "テンプレートのパス": 0, "差し込み印刷のデータ元": 0, "文書の識別子": 0, "編集の制限の編集者": 0,
           "サムネイル": 0}
    for name in pkg.xml_parts():
        try:
            root = _parse(pkg.data[name])
        except etree.XMLSyntaxError:
            continue
        res["変更履歴"] += sum(1 for _ in root.iter(*_REVISION_COUNT))
        res["コメント"] += sum(1 for _ in root.iter(w("comment"), w("commentReference")))
        res["コメント"] += sum(1 for _ in root.iter(etree.Comment))
        res["編集の制限の編集者"] += sum(1 for _ in root.iter(w("permStart")))
        if name == "word/people.xml":
            res["作者一覧"] += len(root)
        if name == "docProps/core.xml":
            res["作成者・更新者"] += sum(1 for el in root if _local(el.tag) in ("creator", "lastModifiedBy") and (el.text or "").strip())
        if name == "word/settings.xml":
            res["文書変数"] += sum(1 for _ in root.iter(w("docVar")))
            res["テンプレートのパス"] += sum(1 for _ in root.iter(w("attachedTemplate")))
            res["差し込み印刷のデータ元"] += sum(1 for _ in root.iter(w("mailMerge")))
            res["文書の識別子"] += sum(1 for el in root.iter() if isinstance(el.tag, str) and _local(el.tag) == "docId")
    res["サムネイル"] = sum(1 for n in pkg.data if re.fullmatch(r"docProps/thumbnail\.\w+", n))
    return res


def paragraph_count(path) -> int:
    pkg = _Package(path)
    root = _parse(pkg.data["word/document.xml"])
    return sum(1 for _ in root.iter(w("p")))
