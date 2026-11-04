"""PDF の墨消し。PyMuPDF が要る（pip install pymupdf。AGPL のライブラリ）。

- 文字の層（テキスト）がある PDF だけを扱う。スキャンしただけの PDF は OCR してから使う
- 黒塗りの下の文字は本当に取り除く（上から黒い四角を重ねるだけの墨消しは、下の文字をコピーできてしまう）。
  黒塗りに重なる画像の部分も塗りつぶす（OCR 済みのスキャン PDF でも、画像の文字が消える）
- 注釈・リンク・しおり・添付ファイル・フォーム・文書情報（作成者など）・XMP を消す
- 置換の形が記号（Ｘ・Ａ社）のときは、黒塗りの上に白い字で記号を載せる
"""
from __future__ import annotations

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
    images: int = 0
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def add(self, idx: int) -> None:
        self.counts[idx] = self.counts.get(idx, 0) + 1


def _page_chars(page):
    fitz = _fitz()
    d = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
    chars: list[tuple[str, object]] = []
    for b in d["blocks"]:
        for line in b.get("lines", []):
            for sp in line["spans"]:
                for c in sp["chars"]:
                    chars.append((c["c"], fitz.Rect(c["bbox"])))
            chars.append(("\n", None))
    return "".join(c for c, _ in chars), chars


def _open(path):
    fitz = _fitz()
    doc = fitz.open(path)
    if doc.needs_pass:
        raise ValueError("パスワードのかかった PDF は扱えません。解除してから使ってください")
    return doc


def read_units(path) -> list[tuple[str, str]]:
    doc = _open(path)
    units = []
    for i, page in enumerate(doc, 1):
        text, _ = _page_chars(page)
        for line in text.split("\n"):
            if line.strip():
                units.append((line, f"{i}頁"))
    for k, v in (doc.metadata or {}).items():
        if v and k not in ("format", "encryption"):
            units.append((v, f"文書情報（{k}）"))
    return units


def _rects(chars, s: int, e: int):
    """一致した字の四角を、行ごとにまとめる。"""
    out, cur = [], None
    for ch, r in chars[s:e]:
        if r is None:
            if cur is not None:
                out.append(cur)
                cur = None
            continue
        if ch.isspace():
            continue
        cur = r if cur is None else cur | r
    if cur is not None:
        out.append(cur)
    return out


def _trim(matched: str, replacement: str) -> tuple[int, int, str]:
    """置換後と共通の頭・尾は塗らない（住所の「東京都新宿区」、「口座番号」、「Ａ弁護士」の「弁護士」）。

    戻り値: (一致の頭から飛ばす字数, 尾から飛ばす字数, 上に載せる記号)
    """
    rep = core(replacement)
    if not rep or KUROFILL in rep:
        rep_core = rep.replace(KUROFILL, "")
    else:
        rep_core = rep
    pos = [i for i, ch in enumerate(matched) if ch not in SPACE_SET]
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


def apply(path, out_path, matcher: Matcher) -> PdfStats:
    fitz = _fitz()
    stats = PdfStats()
    doc = _open(path)
    stats.pages = len(doc)
    for no, page in enumerate(doc, 1):
        text, chars = _page_chars(page)
        imgs = page.get_images(full=True)
        stats.images += len(imgs)
        if not text.strip():
            stats.pages_without_text.append(no)
        for h in matcher.find(text):
            head, tail, label = _trim(h.matched, h.replacement)
            # 墨消し（■）のときは字を載せない。記号のときは黒塗りの上に白字で載せる
            overlay = bool(label) and KUROFILL not in label and len(label) <= 8
            for r in _rects(chars, h.start + head, h.end - tail):
                if overlay:
                    page.add_redact_annot(r, text=label, fontname="japan", fontsize=max(4, r.height * 0.7),
                                          fill=(0, 0, 0), text_color=(1, 1, 1), align=fitz.TEXT_ALIGN_CENTER)
                else:
                    page.add_redact_annot(r, fill=(0, 0, 0))
            stats.add(h.entry)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        # 残った注釈・リンク・フォームを消す（コメントの作者や mailto: のリンク先が残るため）
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
        for link in page.get_links():
            page.delete_link(link)
        for widget in list(page.widgets() or []):
            page.delete_widget(widget)
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except Exception:
        pass
    doc.set_toc([])
    for name in doc.embfile_names():
        doc.embfile_del(name)
    # garbage=4: 使われなくなった古い文字の塊まで捨てる（追記保存だと消したはずの文字が残る）
    doc.save(out_path, garbage=4, deflate=True, clean=True)
    return stats


def all_strings(path) -> list[tuple[str, str]]:
    doc = _open(path)
    out = []
    for i, page in enumerate(doc, 1):
        text, _ = _page_chars(page)
        for line in text.split("\n"):
            if line.strip():
                out.append((line, f"{i}頁"))
        for annot in page.annots() or []:
            info = annot.info or {}
            for v in info.values():
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
    return out


def hygiene(path) -> dict:
    doc = _open(path)
    meta = doc.metadata or {}
    return {
        "注釈": sum(len(list(p.annots() or [])) for p in doc),
        "リンク": sum(len(p.get_links()) for p in doc),
        "フォーム": sum(len(list(p.widgets() or [])) for p in doc),
        "添付ファイル": len(doc.embfile_names()),
        "しおり": len(doc.get_toc()),
        "文書情報": sum(1 for k, v in meta.items() if v and k in ("author", "creator", "producer", "title",
                                                              "subject", "keywords")),
        "XMP": 1 if doc.get_xml_metadata() else 0,
    }


def page_count(path) -> int:
    return len(_open(path))
