"""Word（.docx）の読み取りと置換。

- 変更履歴はすべて承諾した形にする（削除された文字は消し、挿入された文字は本文に残す）。
  python-docx の .text では見えない削除文字が、後の工程で「復活」する事故を防ぐ
- コメントと、コメント・変更履歴の作者一覧（people.xml）を消す
- 作成者・最終更新者・会社名・テンプレートのパス・文書変数・サムネイル画像を消す
- 本文・ヘッダー・フッター・脚注・文末脚注・テキストボックスの段落を、run をまたいで置換する。
  置換しない文字の書式はそのまま残る
- フィールドコード（HYPERLINK "mailto:…"）・画像の代替テキスト・リンク先・カスタムXMLも置換する
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from .matcher import Matcher

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


_REMOVE = [w(t) for t in (
    "del", "moveFrom", "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange",
    "tblGridChange", "numberingChange", "cellDel", "cellIns", "cellMerge", "moveFromRangeStart",
    "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd", "customXmlDelRangeStart", "customXmlDelRangeEnd",
    "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlMoveFromRangeStart",
    "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd")]
_UNWRAP = [w("ins"), w("moveTo")]
_REVISION_COUNT = [w(t) for t in ("ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange")]
# 段落の文字を集めるとき、中に入らない要素
_SKIP = {w(t) for t in ("pPr", "rPr", "sdtPr", "del", "moveFrom", "instrText", "delText", "delInstrText",
                        "fldData", "txbxContent", "p", "tblPr", "trPr", "tcPr", "sectPr", "rt", "rubyPr")}
# 人の書いた文字が入りうる属性（それ以外の属性は書式や参照なので触らない）
_TEXT_ATTRS = {"descr", "title", "alt", "instr", "tooltip", "anchor"}
_NAME_ATTR_ELEMS = {"docPr", "cNvPr", "bookmarkStart"}
_VAL_ATTR_ELEMS = {"alias", "tag", "statusText", "helpText", "default", "listEntry", "name", "docVar"}
_NOT_STORY = re.compile(r"^word/(styles|numbering|fontTable|settings|webSettings|stylesWithEffects)\.xml$|^word/theme/")


@dataclass
class DocxStats:
    revisions: int = 0
    comments: int = 0
    paragraphs: int = 0
    images: int = 0
    embeddings: int = 0
    macros: bool = False
    thumbnail: bool = False
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
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
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


# ------------------------------------------------------------ 段落
def _segments(p) -> list[tuple]:
    """段落の文字の並び。(w:t 要素, None) か (None, 区切り文字)。入れ子の段落は含めない。"""
    segs: list[tuple] = []

    def walk(el):
        for ch in el:
            tag = ch.tag
            if not isinstance(tag, str) or tag in _SKIP:
                continue
            if tag == w("t"):
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


def paragraph_text(p) -> str:
    return "".join((el.text or "") if el is not None else sep for el, sep in _segments(p))


def _apply_paragraph(p, matcher: Matcher, stats: DocxStats) -> None:
    segs = _segments(p)
    if not segs:
        return
    texts = [(el.text or "") if el is not None else sep for el, sep in segs]
    full = "".join(texts)
    hits = matcher.find(full)
    if not hits:
        return
    starts, pos = [], 0
    for t in texts:
        starts.append(pos)
        pos += len(t)
    rubies = [(rb, [t.text for t in rb.iter(w("t"))]) for rb in p.iter(w("rubyBase"))]
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
            el.set(XML_SPACE, "preserve")
            first = False
        stats.add(h.entry)
    # 振り仮名の親文字を置き換えたら、振り仮名（読み）も消す
    for rb, before in rubies:
        if [t.text for t in rb.iter(w("t"))] != before:
            ruby = rb.getparent()
            rt = ruby.find(w("rt")) if ruby is not None else None
            if rt is not None:
                for t in rt.iter(w("t")):
                    t.text = ""


def _apply_fields(p, matcher: Matcher, stats: DocxStats) -> None:
    """フィールドコード（段落内の w:instrText をつないだもの）を置換する。"""
    instrs = [el for el in p.iter(w("instrText"))]
    if not instrs:
        return
    full = "".join(el.text or "" for el in instrs)
    new, hits = matcher.sub(full)
    if hits:
        instrs[0].text = new
        instrs[0].set(XML_SPACE, "preserve")
        for el in instrs[1:]:
            el.text = ""
        for h in hits:
            stats.add(h.entry)


def _sub_text(s: str, matcher: Matcher, stats: DocxStats) -> str:
    new, hits = matcher.sub(s)
    for h in hits:
        stats.add(h.entry)
    return new


def _apply_other_text(root, matcher: Matcher, stats: DocxStats, all_attrs: bool = False) -> None:
    """段落以外の文字（図形の a:t、グラフの値、文書情報など）と、文字の入る属性を置換する。"""
    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue
        if tag not in (w("t"), w("instrText"), w("delText")) and el.text and el.text.strip():
            el.text = _sub_text(el.text, matcher, stats)
        loc = _local(tag)
        for name, val in list(el.attrib.items()):
            an = _local(name)
            if all_attrs or an in _TEXT_ATTRS or (an == "name" and loc in _NAME_ATTR_ELEMS) \
                    or (an == "val" and loc in _VAL_ATTR_ELEMS):
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
            raise ValueError("Word の文書（word/document.xml）が見つかりません。.docx ですか？")
        self.dropped: set[str] = set()

    def xml_parts(self):
        return [n for n in self.data if n.endswith((".xml", ".rels")) and n not in self.dropped]


def _is_story(name: str, root) -> bool:
    return name.startswith("word/") and not _NOT_STORY.match(name) and root.find(f".//{w('p')}") is not None


def _clean(pkg: _Package, name: str, root, stats: DocxStats | None) -> None:
    """名前を伏せる前の掃除（変更履歴の承諾・コメント・作者・文書情報）。"""
    if name.startswith("word/") and name.endswith(".xml"):
        _accept_revisions(root, stats)
        _strip_comment_anchors(root, stats)
    local = name.rsplit("/", 1)[-1]
    if re.fullmatch(r"comments(Extended|Ids|Extensible)?\.xml|people\.xml", local) and name.startswith("word/"):
        for child in list(root):
            root.remove(child)
    elif name == "word/settings.xml":
        for tag in ("attachedTemplate", "docVars"):
            for el in list(root.iter(w(tag))):
                el.getparent().remove(el)
    elif name == "docProps/core.xml":
        for el in root:
            if _local(el.tag) in ("creator", "lastModifiedBy"):
                el.text = ""
    elif name == "docProps/app.xml":
        for el in root:
            if _local(el.tag) in ("Company", "Manager", "Template", "HyperlinkBase"):
                el.text = ""
    elif name == "docProps/custom.xml":
        for child in list(root):
            root.remove(child)
    elif name.endswith(".rels"):
        for rel in list(root):
            typ, target = rel.get("Type", ""), rel.get("Target", "")
            if typ.endswith("/attachedTemplate") or re.search(r"thumbnail\.\w+$", target):
                root.remove(rel)
    elif name == "[Content_Types].xml":
        for el in list(root):
            if re.search(r"/docProps/thumbnail\.\w+$", el.get("PartName", "")):
                root.remove(el)


def _drop_thumbnail(pkg: _Package, stats: DocxStats | None) -> None:
    for n in list(pkg.data):
        if re.fullmatch(r"docProps/thumbnail\.\w+", n):
            pkg.dropped.add(n)
            if stats is not None:
                stats.thumbnail = True


def read_units(path) -> list[tuple[str, str]]:
    """候補を拾うための文字（段落ごと）。変更履歴は承諾した形で読む。[(文字, 場所)]"""
    pkg = _Package(path)
    units: list[tuple[str, str]] = []
    for name in pkg.xml_parts():
        try:
            root = _parse(pkg.data[name])
        except etree.XMLSyntaxError:
            continue
        _clean(pkg, name, root, None)
        if _is_story(name, root):
            for p in root.iter(w("p")):
                t = paragraph_text(p)
                if t.strip():
                    units.append((t, name))
                instr = "".join(el.text or "" for el in p.iter(w("instrText")))
                if instr.strip():
                    units.append((instr, name + "（フィールド）"))
        story = _is_story(name, root)
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            # 本文の w:t は段落として読んだので除く。図形の a:t など、ほかの文字はそのまま読む
            if el.text and el.text.strip() and el.tag not in (w("t"), w("instrText"), w("delText")) \
                    and (not story or _local(el.tag) == "t"):
                units.append((el.text, name))
            for an, val in el.attrib.items():
                if _local(an) in ("descr", "title", "alt", "tooltip") and val.strip():
                    units.append((val, name + "（属性）"))
            if name.endswith(".rels") and el.get("TargetMode") == "External":
                units.append((el.get("Target", ""), name))
    return units


def apply(path, out_path, matcher: Matcher) -> DocxStats:
    stats = DocxStats()
    pkg = _Package(path)
    _drop_thumbnail(pkg, stats)
    for n in pkg.data:
        if n.startswith("word/media/"):
            stats.images += 1
        elif n.startswith("word/embeddings/"):
            stats.embeddings += 1
        elif n.endswith("vbaProject.bin"):
            stats.macros = True
    new_data: dict[str, bytes] = {}
    for name in pkg.xml_parts():
        try:
            root = _parse(pkg.data[name])
        except etree.XMLSyntaxError:
            stats.warnings.append(f"{name} を XML として読めなかったので、そのまま残しました")
            continue
        _clean(pkg, name, root, stats)
        if _is_story(name, root):
            for p in root.iter(w("p")):
                _apply_paragraph(p, matcher, stats)
                _apply_fields(p, matcher, stats)
            if name == "word/document.xml":
                stats.paragraphs = sum(1 for _ in root.iter(w("p")))
        is_data = name.startswith("customXml/") or name.startswith("docProps/")
        if name.endswith(".rels"):
            for rel in root:
                if rel.get("TargetMode") == "External":
                    t = rel.get("Target", "")
                    new = _sub_text(t, matcher, stats)
                    if new != t:
                        rel.set("Target", new)
        else:
            _apply_other_text(root, matcher, stats, all_attrs=is_data)
        new_data[name] = _serialize(root)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in pkg.infos:
            if info.filename in pkg.dropped:
                continue
            zout.writestr(info, new_data.get(info.filename, pkg.data[info.filename]))
    return stats


# ------------------------------------------------------------ 検査用
def all_strings(path) -> list[tuple[str, str]]:
    """出力を検査するための文字をすべて（段落・要素の文字・すべての属性値・リンク先）。[(文字, 場所)]"""
    pkg = _Package(path)
    out: list[tuple[str, str]] = []
    for name in pkg.xml_parts():
        try:
            root = _parse(pkg.data[name])
        except etree.XMLSyntaxError:
            out.append((pkg.data[name].decode("utf-8", "replace"), name))
            continue
        story = _is_story(name, root)
        if story:
            for p in root.iter(w("p")):
                t = paragraph_text(p)
                if t.strip():
                    out.append((t, name))
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if el.text and el.text.strip() and not (story and el.tag == w("t")):
                out.append((el.text, name))
            for _an, val in el.attrib.items():
                if val.strip():
                    out.append((val, name + "（属性）"))
    return out


def hygiene(path) -> dict:
    """変更履歴・コメント・作者などが残っていないか数える。"""
    pkg = _Package(path)
    res = {"変更履歴": 0, "コメント": 0, "作者一覧": 0, "作成者・更新者": 0, "文書変数": 0,
           "テンプレートのパス": 0, "サムネイル": 0}
    for name in pkg.xml_parts():
        try:
            root = _parse(pkg.data[name])
        except etree.XMLSyntaxError:
            continue
        res["変更履歴"] += sum(1 for _ in root.iter(*_REVISION_COUNT))
        res["コメント"] += sum(1 for _ in root.iter(w("comment"), w("commentReference")))
        if name == "word/people.xml":
            res["作者一覧"] += len(root)
        if name == "docProps/core.xml":
            res["作成者・更新者"] += sum(1 for el in root if _local(el.tag) in ("creator", "lastModifiedBy") and (el.text or "").strip())
        if name == "word/settings.xml":
            res["文書変数"] += sum(1 for _ in root.iter(w("docVar")))
            res["テンプレートのパス"] += sum(1 for _ in root.iter(w("attachedTemplate")))
    res["サムネイル"] = sum(1 for n in pkg.data if re.fullmatch(r"docProps/thumbnail\.\w+", n))
    return res


def paragraph_count(path) -> int:
    pkg = _Package(path)
    root = _parse(pkg.data["word/document.xml"])
    return sum(1 for _ in root.iter(w("p")))
