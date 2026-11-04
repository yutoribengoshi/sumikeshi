"""試験用の文書を作る。名前・住所・番号はすべて架空のもの（実在の事件・人と関係しない）。

Word の難所を1つの文書に詰める: 字間空白の記名、run の途中で切れた名前、変更履歴の挿入・削除、
コメント、脚注、テキストボックス、ヘッダー、表、mailto のリンク、フィールドコード、画像の代替テキスト、
文書情報（作成者・会社名）、テンプレートのパス、文書変数、作者一覧、サムネイル、カスタムXML。
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from pathlib import Path

# 伏せられていなければならない語（どの形でも出力に残ってはいけない）
SECRETS = ["山田太郎", "鈴木一郎", "佐々木健", "みどり建設", "taro.yamada@example.com", "1234-5678",
           "１２３４－５６７８", "西新宿", "高木健太", "鈴木に確認", "グリーンハイツ"]
# 残っていなければならない語（伏せすぎていないか）
KEEPS = ["最判平成２０年６月４日民集６２巻６号１３６７頁", "〔ＮＥＸＸ事件〕", "民法７０９条", "甲第３号証",
         "東京地方裁判所民事第３部", "準備書面", "東京都新宿区"]

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

PARAS = [
    "令和６年（ワ）第１２３４号　損害賠償請求事件",
    "原　　　告　　山　田　太　郎",
    "被　　　告　　株式会社みどり建設",
    "準備書面（１）",
    "東京地方裁判所民事第３部　御中",
    "〒１６０－００２３　東京都新宿区西新宿二丁目８番１号　グリーンハイツ３０１号室",
    "電話　０３－１２３４－５６７８",
    "第１　当事者",
    "２　被告株式会社みどり建設（以下「みどり建設」という。）の代表者代表取締役は、鈴木一郎である。",
    "３　訴外佐々木健は、被告の従業員である。佐々木は、原告と面識があった。",
    "４　最判平成２０年６月４日民集６２巻６号１３６７頁〔ＮＥＸＸ事件〕、民法７０９条、甲第３号証。",
]


def _png_file(tmpdir: Path) -> Path:
    p = tmpdir / "pixel.png"
    p.write_bytes(_PNG)
    return p


def make_tricky_docx(path: Path) -> Path:
    import docx
    from docx.shared import Pt

    d = docx.Document()
    d.core_properties.author = "山田太郎"
    d.core_properties.last_modified_by = "鈴木一郎"
    d.core_properties.title = "山田太郎 準備書面"
    d.sections[0].header.paragraphs[0].text = "原告　山田太郎"
    for t in PARAS:
        d.add_paragraph(t)
    # run の途中で切れた名前（書式も違う）: 原告|山|田太|郎|は、…
    p = d.add_paragraph()
    r0 = p.add_run("１　原告")
    r0.bold = True
    p.add_run("山").bold = True
    p.add_run("田太")
    r3 = p.add_run("郎")
    r3.italic = True
    p.add_run("は、昭和５０年１月２日生まれである。")
    # コメント（作者と本文に名前）
    cp = d.add_paragraph("コメント付きの段落です。")
    d.add_comment(cp.runs, text="鈴木に確認", author="山田太郎", initials="YT")
    # 表
    tbl = d.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "氏名"
    tbl.cell(0, 1).text = "山田　太郎"
    tbl.cell(1, 0).text = "勤務先"
    tbl.cell(1, 1).text = "株式会社みどり建設"
    # 画像（代替テキストに名前）
    tmp = path.parent
    d.add_picture(str(_png_file(tmp)), width=Pt(10))
    # 目印の段落（あとで XML を差し込む）
    for mark in ("__INS__", "__TEXTBOX__", "__LINK__", "__FIELD__", "__FOOTNOTE__"):
        d.add_paragraph(mark)
    buf = io.BytesIO()
    d.save(buf)
    _inject(buf.getvalue(), path)
    (tmp / "pixel.png").unlink()
    return path


def _inject(data: bytes, path: Path) -> None:
    zin = zipfile.ZipFile(io.BytesIO(data))
    files = {n: zin.read(n) for n in zin.namelist()}
    doc = files["word/document.xml"].decode("utf-8")

    def para(mark: str, inner: str) -> None:
        nonlocal doc
        doc = re.sub(rf"<w:p>(?:(?!<w:p>).)*?{mark}.*?</w:p>", f"<w:p>{inner}</w:p>", doc, count=1, flags=re.S)

    para("__INS__",
         '<w:r><w:t xml:space="preserve">変更履歴：</w:t></w:r>'
         '<w:ins w:id="901" w:author="山田太郎" w:date="2026-01-01T00:00:00Z"><w:r><w:t>鈴木一郎の陳述</w:t></w:r></w:ins>'
         '<w:del w:id="902" w:author="山田太郎" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>削除した旧依頼者高木健太</w:delText></w:r></w:del>')
    para("__TEXTBOX__",
         '<w:r><w:pict><v:shape id="tb1" style="width:200pt;height:40pt" type="#_x0000_t202">'
         '<v:textbox><w:txbxContent><w:p><w:r><w:t>被告　株式会社みどり建設　担当　佐々木健</w:t></w:r></w:p>'
         '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>')
    para("__LINK__",
         '<w:r><w:t xml:space="preserve">連絡先：</w:t></w:r><w:hyperlink r:id="rIdMail"><w:r><w:t>taro.yamada@example.com</w:t></w:r></w:hyperlink>')
    para("__FIELD__",
         '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:taro.yamada</w:instrText></w:r>'
         '<w:r><w:instrText xml:space="preserve">@example.com" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
         '<w:r><w:t>メール</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>')
    para("__FOOTNOTE__",
         '<w:r><w:t>脚注の参照</w:t></w:r><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteReference w:id="1"/></w:r>')
    # 画像の代替テキスト
    doc = re.sub(r'(<wp:docPr [^>]*?)descr="[^"]*"', r"\1", doc)
    doc = doc.replace("<wp:docPr ", '<wp:docPr descr="山田太郎の運転免許証" ', 1)
    files["word/document.xml"] = doc.encode("utf-8")

    rels = files["word/_rels/document.xml.rels"].decode("utf-8")
    rels = rels.replace("</Relationships>",
                        '<Relationship Id="rIdMail" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                        'Target="mailto:taro.yamada@example.com" TargetMode="External"/>'
                        '<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>'
                        '<Relationship Id="rIdPeople" Type="http://schemas.microsoft.com/office/2011/relationships/people" Target="people.xml"/>'
                        "</Relationships>")
    files["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    files["word/footnotes.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes {W}>'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
        '<w:footnote w:id="1"><w:p><w:r><w:t>佐々木健の陳述書（甲第３号証）による。</w:t></w:r></w:p></w:footnote>'
        "</w:footnotes>").encode("utf-8")
    files["word/people.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
        '<w15:person w15:author="山田太郎"><w15:presenceInfo w15:providerId="None" w15:userId="山田太郎"/></w15:person>'
        "</w15:people>").encode("utf-8")
    ct = files["[Content_Types].xml"].decode("utf-8")
    ct = ct.replace("</Types>",
                    '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>'
                    '<Override PartName="/word/people.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml"/>'
                    "</Types>")
    if 'Extension="png"' not in ct:
        ct = ct.replace("<Default ", '<Default Extension="png" ContentType="image/png"/><Default ', 1)
    files["[Content_Types].xml"] = ct.encode("utf-8")
    # テンプレートのパス・文書変数
    st = files["word/settings.xml"].decode("utf-8")
    st = re.sub(r"(<w:settings [^>]*>)", r'\1<w:attachedTemplate r:id="rIdTpl"/>', st, count=1)
    if "xmlns:r=" not in st:
        st = st.replace("<w:settings ", '<w:settings xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ', 1)
    st = st.replace("</w:settings>", '<w:docVars><w:docVar w:name="client" w:val="山田太郎"/></w:docVars></w:settings>')
    files["word/settings.xml"] = st.encode("utf-8")
    files["word/_rels/settings.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdTpl" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
        'Target="file:///C:/Users/yamada/AppData/Roaming/Microsoft/Templates/山田太郎事務所.dotm" TargetMode="External"/>'
        "</Relationships>").encode("utf-8")
    app = files["docProps/app.xml"].decode("utf-8")
    app = re.sub(r"<Company>.*?</Company>", "", app)
    app = app.replace("</Properties>", "<Company>みどり建設</Company></Properties>")
    files["docProps/app.xml"] = app.encode("utf-8")
    files["customXml/item1.xml"] = '<?xml version="1.0" encoding="UTF-8"?><client><name>山田太郎</name></client>'.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, b in files.items():
            zout.writestr(n, b)


def make_text(path: Path) -> Path:
    path.write_text("\n".join(PARAS + ["１　原告山田太郎は、昭和５０年１月２日生まれである。",
                                       "連絡先：taro.yamada@example.com"]) + "\n", encoding="utf-8")
    return path


def make_pdf(path: Path) -> Path:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for t in PARAS + ["１　原告山田太郎は、昭和５０年１月２日生まれである。", "連絡先：taro.yamada@example.com"]:
        page.insert_text((40, y), t, fontname="japan", fontsize=9)
        y += 16
    page.add_text_annot((300, 40), "鈴木に確認")
    doc.set_metadata({"author": "山田太郎", "title": "山田太郎 準備書面"})
    doc.save(path)
    return path


def package_strings(path: Path) -> str:
    """docx の全パートを文字としてつないだもの（XML のタグも含む）。"""
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
