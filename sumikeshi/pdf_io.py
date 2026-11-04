"""PDF の墨消し。PyMuPDF が要る（pip install pymupdf。AGPL のライブラリ）。

- 文字の層（テキスト）がある PDF だけを扱う。スキャンしただけの PDF は OCR してから使う
- 黒塗りの下の文字は本当に取り除く（上から黒い四角を重ねるだけの墨消しは、下の文字をコピーできてしまう）。
  黒塗りに重なる画像の部分も塗りつぶす（OCR 済みのスキャン PDF でも、画像の文字が消える）
- 行をまたいだ語・均等割付の語も当てるよう、段落（ブロック）の中の行はつないで読む
- ページの外・切り抜きの外・非表示のレイヤの文字も読んで伏せる
- 字の形に文字の対応が無いフォント（読むと文字化けする頁）は、伏せられないので不合格にする
- 字を取り除く四角は字の芯の高さだけにして、行間が詰まった文書で上下の行の字を消さない。
  そのうえで、見た目の黒塗りと画像の塗りつぶしは字の高さいっぱいに行う
- 注釈・リンク・しおり・添付ファイル・フォーム・文書情報・XMP・名前付き宛先・構造ツリー（代替テキスト）・
  ページラベル・XFA・ページごとの XMP を消し、レイヤの名前を伏せる
- 置換の形が記号（Ｘ・Ａ社）のときは、黒塗りの上に白い字で記号を載せる
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from .lexicon import SPACE_SET
from .matcher import KUROFILL, Matcher, core


def _fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        if getattr(sys, "frozen", False):
            raise RuntimeError("この EXE の版は PDF に対応していません（PDF は Python 版で扱えます）") from e
        raise RuntimeError("PDF を扱うには PyMuPDF が要ります: pip install pymupdf") from e
    return fitz


@dataclass
class PdfStats:
    pages: int = 0
    pages_without_text: list = field(default_factory=list)
    garbled_pages: list = field(default_factory=list)
    images: int = 0
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def add(self, idx: int) -> None:
        self.counts[idx] = self.counts.get(idx, 0) + 1


@dataclass
class _Char:
    ch: str
    rect: object          # 字の四角（フォントの高さいっぱい）
    core_rect: object     # 字の芯の四角（取り除く判定に使う）
    line: int


def _flags():
    fitz = _fitz()
    # ページの外の文字も読む（既定ではページの枠で切られる）
    return fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES & ~fitz.TEXT_MEDIABOX_CLIP


# ページ全体をつないだ文字の場所に付ける印（候補の件数には数えない）
JOINED_MARK = "（行をつないだ全文）"
# 行のつなぎ目に入れる字（ゼロ幅の空白）。照合では空白として跨ぎ、社名などの拾い方では区切りになる
JOIN = chr(0x200B)


def _page_chars(page):
    """ページの文字を、行もブロックもゼロ幅の空白でつないで読む順に並べる（行末で折り返した名前・
    均等割付の名前も当てる）。字ごとに行の番号を持ち、黒塗りの四角は行ごとに作る。"""
    fitz = _fitz()
    d = page.get_text("rawdict", flags=_flags())
    chars: list = []
    lines: list[str] = []
    line_no = 0
    for b in d["blocks"]:
        for line in b.get("lines", []):
            if chars:
                chars.append(_Char(JOIN, None, None, line_no))
            line_no += 1
            horizontal = abs(line.get("dir", (1, 0))[1]) < 0.01
            for sp in line["spans"]:
                size = sp.get("size", 0)
                for c in sp["chars"]:
                    r = fitz.Rect(c["bbox"])
                    cr = r
                    if horizontal and size:
                        oy = c["origin"][1]
                        # 字の芯（ベースラインの少し上から字の高さの半分ほど）。上下の行の字の四角に触れない
                        cr = fitz.Rect(r.x0, max(r.y0, oy - size * 0.62), r.x1, min(r.y1, oy - size * 0.22))
                        if cr.is_empty:
                            cr = r
                    chars.append(_Char(c["c"], r, cr, line_no))
            lines.append("".join(c["c"] for sp in line["spans"] for c in sp["chars"]))
    text = "".join(c.ch for c in chars)
    return text, chars, lines


def _rects(chars, s: int, e: int, attr: str):
    """一致した字の四角を、行ごとにまとめる。"""
    out, cur, cur_line = [], None, None
    for c in chars[s:e]:
        if c is None or c.rect is None or c.ch in SPACE_SET or c.ch.isspace():
            continue
        r = getattr(c, attr)
        if cur is not None and c.line != cur_line:
            out.append(cur)
            cur = None
        cur = r if cur is None else cur | r
        cur_line = c.line
    if cur is not None:
        out.append(cur)
    return out


def _expand_boxes(page):
    """切り抜き（CropBox）の外とページ（MediaBox）の外の文字も読めるよう、いったん枠を広げる。
    元の枠を返す（伏せた後に戻す）。ページの外の文字は texttrace で位置を確かめ、その分だけ広げる。"""
    fitz = _fitz()
    media, crop = fitz.Rect(page.mediabox), fitz.Rect(page.cropbox)
    changed = False
    try:
        if crop != media:
            page.set_cropbox(media)
            changed = True
        over = 0.0
        rect = page.rect
        for span in page.get_texttrace():
            x0, y0, x1, y1 = span["bbox"]
            over = max(over, rect.x0 - x0, rect.y0 - y0, x1 - rect.x1, y1 - rect.y1)
        if over > 0:
            m = over + 20
            page.set_mediabox(fitz.Rect(media.x0 - m, media.y0 - m, media.x1 + m, media.y1 + m))
            changed = True
    except Exception:
        return (media, crop) if changed else None
    return (media, crop) if changed else None


def _restore_boxes(page, saved) -> None:
    if not saved:
        return
    media, crop = saved
    try:
        page.set_mediabox(media)
        if crop != media:
            page.set_cropbox(crop)
    except Exception:
        pass


def _garbled(text: str) -> bool:
    """文字化けした文字の層か（フォントに文字の対応表が無いと、読めない字が並ぶ）。"""
    chars = [ch for ch in text if not ch.isspace()]
    if len(chars) < 20:
        return False

    def ok(ch: str) -> bool:
        cp = ord(ch)
        return (cp < 0x80 or 0x3000 <= cp <= 0x30FF or 0x4E00 <= cp <= 0x9FFF or 0xFF00 <= cp <= 0xFFEF
                or 0x2000 <= cp <= 0x206F or 0x2460 <= cp <= 0x24FF or 0x2500 <= cp <= 0x25FF or cp in (0xD7, 0xF7))
    return sum(ok(ch) for ch in chars) / len(chars) < 0.8


def _open(path):
    fitz = _fitz()
    doc = fitz.open(path)
    if doc.needs_pass:
        raise ValueError("パスワードのかかった PDF は扱えません。解除してから使ってください")
    return doc


def _all_layers_on(doc):
    """非表示のレイヤの文字も読んで伏せるため、表示の切り替えでいったん全部のレイヤを表示にする。
    表示に変えたレイヤの番号を返す（伏せた後に戻す）。"""
    turned = []
    try:
        for item in doc.layer_ui_configs():
            if item.get("type") == "checkbox" and not item.get("on"):
                doc.set_layer_ui_config(item["number"], 0)      # 0 = 表示にする
                turned.append(item["number"])
    except Exception:
        return []
    return turned


def _restore_layers(doc, turned) -> None:
    for number in turned or []:
        try:
            doc.set_layer_ui_config(number, 2)                  # 2 = 非表示に戻す
        except Exception:
            pass


def read_units(path) -> list[tuple[str, str]]:
    """行ごとの文字（行の形で拾う規則のため）と、ページ全体をつないだ文字（行をまたぐ語のため）。"""
    doc = _open(path)
    _all_layers_on(doc)
    units = []
    for i, page in enumerate(doc, 1):
        _expand_boxes(page)
        text, _, lines = _page_chars(page)
        units += [(ln, f"{i}頁") for ln in lines if ln.strip()]
        if text.strip():
            units.append((text, f"{i}頁{JOINED_MARK}"))
    for k, v in (doc.metadata or {}).items():
        if v and k not in ("format", "encryption"):
            units.append((v, f"文書情報（{k}）"))
    return units


def _trim(matched: str, replacement: str) -> tuple[int, int, str]:
    """置換後と共通の頭・尾は塗らない（住所の「東京都新宿区」、「口座番号」、「Ａ弁護士」の「弁護士」）。

    戻り値: (一致の頭から飛ばす字数, 尾から飛ばす字数, 上に載せる記号)
    """
    rep = core(replacement)
    rep_core = rep.replace(KUROFILL, "")
    pos = [i for i, ch in enumerate(matched) if ch not in SPACE_SET and ch != "\n"]
    mcore = "".join(matched[i] for i in pos)
    pre = 0
    while pre < min(len(mcore), len(rep_core)) and mcore[pre] == rep_core[pre]:
        pre += 1
    suf = 0
    while suf < min(len(mcore), len(rep_core)) - pre and mcore[-1 - suf] == rep_core[-1 - suf]:
        suf += 1
    if pre + suf >= len(mcore):
        return 0, 0, rep
    head = pos[pre] if pre else 0
    tail = len(matched) - (pos[-suf] if suf else len(matched))
    label = rep[pre:len(rep) - suf] if KUROFILL not in rep else ""
    return head, tail, label


def _scrub_catalog(doc) -> None:
    """文字以外の場所に残る文字列を消す（名前付き宛先・構造ツリーの代替テキスト・ページラベル・XFA・ページの XMP）。"""
    cat = doc.pdf_catalog()
    for key in ("Names", "Dests", "StructTreeRoot", "MarkInfo", "PageLabels", "AcroForm", "Metadata", "PieceInfo",
                "OpenAction", "URI", "SpiderInfo", "Threads"):
        try:
            doc.xref_set_key(cat, key, "null")
        except Exception:
            pass
    for page in doc:
        for key in ("Metadata", "PieceInfo", "StructParents", "Tabs"):
            try:
                doc.xref_set_key(page.xref, key, "null")
            except Exception:
                pass
    # レイヤの名前（「山田太郎の書き込み」など）を番号に置き換える。レイヤそのものは残す
    try:
        for n, xref in enumerate(sorted(doc.get_ocgs()), 1):
            doc.xref_set_key(xref, "Name", f"(Layer {n})")
    except Exception:
        pass


def apply(path, out_path, matcher: Matcher) -> PdfStats:
    fitz = _fitz()
    stats = PdfStats()
    doc = _open(path)
    stats.pages = len(doc)
    layers = _all_layers_on(doc)
    for no, page in enumerate(doc, 1):
        boxes_before = _expand_boxes(page)
        text, chars, _lines = _page_chars(page)
        stats.images += len(page.get_images(full=True))
        if not text.strip():
            stats.pages_without_text.append(no)
            _restore_boxes(page, boxes_before)
            continue
        if _garbled(text):
            stats.garbled_pages.append(no)
        boxes = []
        for h in matcher.find(text):
            head, tail, label = _trim(h.matched, h.replacement)
            s, e = h.start + head, h.end - tail
            for r in _rects(chars, s, e, "core_rect"):
                page.add_redact_annot(r, fill=False)
            boxes.append((_rects(chars, s, e, "rect"), label))
            stats.add(h.entry)
        # 1回目: 字の芯の四角で、当たった字だけを取り除く（上下の行の字は残る）
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        # 2回目: 字の高さいっぱいに黒く塗り、重なる画像の部分も塗りつぶす（文字はもう消してあるので触らない）
        for rects, label in boxes:
            overlay = bool(label) and KUROFILL not in label and len(label) <= 8
            for r in rects:
                if overlay:
                    page.add_redact_annot(r, text=label, fontname="japan", fontsize=max(4, r.height * 0.6),
                                          fill=(0, 0, 0), text_color=(1, 1, 1), align=fitz.TEXT_ALIGN_CENTER)
                else:
                    page.add_redact_annot(r, fill=(0, 0, 0))
        if boxes:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_NONE)
        _restore_boxes(page, boxes_before)
        # 残った注釈・リンク・フォームを消す（コメントの作者や mailto: のリンク先が残るため）
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
        for link in page.get_links():
            page.delete_link(link)
        for widget in list(page.widgets() or []):
            page.delete_widget(widget)
    _restore_layers(doc, layers)
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except Exception:
        pass
    doc.set_toc([])
    for name in doc.embfile_names():
        doc.embfile_del(name)
    _scrub_catalog(doc)
    # garbage=4: 使われなくなった古い文字の塊まで捨てる（追記保存だと消したはずの文字が残る）
    doc.save(out_path, garbage=4, deflate=True, clean=True)
    return stats


_PDF_STRING = re.compile(rb"\((?:\\.|[^\\)])*\)|<FEFF[0-9A-Fa-f]+>|<feff[0-9A-Fa-f]+>")


def _decode_pdf_string(tok: bytes) -> str:
    if tok.startswith(b"<"):
        try:
            return bytes.fromhex(tok[1:-1].decode()).decode("utf-16-be", "ignore")
        except ValueError:
            return ""
    raw = tok[1:-1]
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "ignore")
    return raw.decode("latin-1", "ignore")


def all_strings(path) -> list[tuple[str, str]]:
    doc = _open(path)
    _all_layers_on(doc)
    out = []
    for i, page in enumerate(doc, 1):
        _expand_boxes(page)
        text, _, lines = _page_chars(page)
        out += [(ln, f"{i}頁") for ln in lines if ln.strip()]
        if text.strip():
            out.append((text, f"{i}頁{JOINED_MARK}"))
        for annot in page.annots() or []:
            for v in (annot.info or {}).values():
                if v:
                    out.append((str(v), f"{i}頁の注釈"))
        for link in page.get_links():
            if link.get("uri"):
                out.append((link["uri"], f"{i}頁のリンク"))
    for k, v in (doc.metadata or {}).items():
        if v and k not in ("format", "encryption"):
            out.append((v, f"文書情報（{k}）"))
    xmp = doc.get_xml_metadata()
    if xmp:
        out.append((xmp, "XMP"))
    # すべてのオブジェクトの文字列（宛先の名前・代替テキスト・レイヤ名など、文字の層の外に置かれたもの）
    for xref in range(1, doc.xref_length()):
        try:
            src = doc.xref_object(xref, compressed=False).encode("latin-1", "ignore")
        except Exception:
            continue
        strings = [_decode_pdf_string(t) for t in _PDF_STRING.findall(src)]
        strings = [s for s in strings if s.strip()]
        if strings:
            out.append(("\n".join(strings), f"オブジェクト {xref} の文字列"))
        try:
            if doc.xref_is_stream(xref):
                data = doc.xref_stream(xref) or b""
                if data[:200].lstrip().startswith((b"<?xpacket", b"<x:xmpmeta", b"<?xml", b"<xdp")):
                    out.append((data.decode("utf-8", "ignore"), f"オブジェクト {xref} の XML"))
        except Exception:
            continue
    return out


def hygiene(path) -> dict:
    doc = _open(path)
    meta = doc.metadata or {}
    cat = doc.pdf_catalog()

    def has(key: str) -> int:
        try:
            return int(doc.xref_get_key(cat, key)[0] not in ("null", "undefined"))
        except Exception:
            return 0
    return {
        "注釈": sum(len(list(p.annots() or [])) for p in doc),
        "リンク": sum(len(p.get_links()) for p in doc),
        "フォーム": sum(len(list(p.widgets() or [])) for p in doc) + has("AcroForm"),
        "添付ファイル": len(doc.embfile_names()),
        "しおり": len(doc.get_toc()),
        "文書情報": sum(1 for k, v in meta.items() if v and k in ("author", "creator", "producer", "title",
                                                              "subject", "keywords")),
        "XMP": 1 if doc.get_xml_metadata() else 0,
        "名前付き宛先・構造ツリー・ページラベル": has("Names") + has("Dests") + has("StructTreeRoot") + has("PageLabels"),
    }


def page_count(path) -> int:
    return len(_open(path))
