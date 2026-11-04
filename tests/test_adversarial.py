"""漏れを探す試験（敵対的な試験）で見つかった型を固定する。名前・番号はすべて架空のもの。"""
import io
import re
import unicodedata
import zipfile

import pytest

from fixtures import package_strings
from sumikeshi.cli import main
from sumikeshi.collect import Collector
from sumikeshi.detect import scan_unit
from sumikeshi.matcher import Entry, Matcher, core

LIST = "採否,種別,原文,置換後\n○,人名,山田太郎,Ｘ\n○,人名,鈴木一郎,Ｙ\n"


def _list(tmp_path, body=LIST):
    p = tmp_path / "list.csv"
    p.write_text(body, encoding="utf-8-sig")
    return p


def _docx(path, body_xml, extra=None, rels="", ct=""):
    """最小の docx を手で組む（本文の XML と、追加の部品）。"""
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ' \
        'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/><Default Extension="jpeg" ContentType="image/jpeg"/>'
        '<Default Extension="bin" ContentType="application/octet-stream"/><Default Extension="xlsx" ContentType="application/octet-stream"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        + ct + "</Types>",
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/_rels/document.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + rels + "</Relationships>",
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{body_xml}</w:body></w:document>',
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)
    return path


def _p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _flat(path):
    raw = package_strings(path)
    return raw + re.sub(r"<[^>]+>", "", raw)


# ------------------------------------------------------------ Word
def test_altchunk_is_refused(tmp_path):
    src = _docx(tmp_path / "a.docx", _p("本文") + '<w:altChunk r:id="rIdA"/>',
                {"word/afchunk.htm": "<html><body>山田太郎</body></html>"},
                rels='<Relationship Id="rIdA" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="afchunk.htm"/>')
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) == 2
    assert not list(tmp_path.glob("*_匿名*.docx"))


def test_strict_ooxml_is_refused(tmp_path):
    src = _docx(tmp_path / "s0.docx", _p("原告山田太郎"))
    files = {n: zipfile.ZipFile(src).read(n) for n in zipfile.ZipFile(src).namelist()}
    files["word/document.xml"] = files["word/document.xml"].replace(
        b"http://schemas.openxmlformats.org/wordprocessingml/2006/main", b"http://purl.oclc.org/ooxml/wordprocessingml/main")
    src = tmp_path / "s.docx"
    with zipfile.ZipFile(src, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) == 2


def test_hidden_places_in_word(tmp_path):
    body = (
        '<w:tbl><w:tblPr><w:tblCaption w:val="山田太郎の一覧"/><w:tblDescription w:val="鈴木一郎"/></w:tblPr>'
        '<w:tr><w:tc><w:p><w:r><w:t>表</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:p><w:permStart w:id="1" w:ed="taro@example.com"/><w:r><w:t>本文</w:t></w:r><w:permEnd w:id="1"/></w:p>'
        '<w:p><w:r><w:ruby><w:rubyPr/><w:rt><w:r><w:t>山田太郎</w:t></w:r></w:rt><w:rubyBase><w:r><w:t>あ</w:t></w:r></w:rubyBase></w:ruby></w:r></w:p>'
        '<w:p><m:oMath><m:r><m:t>山田</m:t></m:r><m:r><m:t>太郎</m:t></m:r></m:oMath></w:p>'
        '<w:p><w:sdt><w:sdtPr><w:dropDownList><w:listItem w:displayText="鈴木一郎" w:value="鈴木一郎"/></w:dropDownList></w:sdtPr>'
        '<w:sdtContent><w:r><w:t>選択</w:t></w:r></w:sdtContent></w:sdt></w:p>'
        '<!-- 作成者 山田太郎 -->'
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "mailto:taro@example.com" </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        '<w:p><w:r><w:pict><v:shape id="s1"><v:textpath string="山田太郎"/></v:shape></w:pict></w:r></w:p>')
    src = _docx(tmp_path / "h.docx", body)
    lst = _list(tmp_path, LIST + "○,メール,taro@example.com,［メール］\n")
    assert main(["apply", str(src), "--list", str(lst)]) in (0, 1)
    out = tmp_path / "h_匿名.docx"
    flat = _flat(out)
    for s in ("山田太郎", "鈴木一郎", "taro@example.com"):
        assert s not in flat, s
    instr = re.findall(r"<w:instrText[^>]*>([^<]*)<", zipfile.ZipFile(out).read("word/document.xml").decode())
    assert any("PAGE" in i for i in instr) and not any("PAGE" in i and "HYPERLINK" in i for i in instr)


def test_embedded_excel_exif_printer_and_vml(tmp_path):
    xlsx = io.BytesIO()
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>山田太郎</t></si></sst>')
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>鈴木一郎</dc:creator></cp:coreProperties>')
    exif = b"\xff\xd8" + b"\xff\xe1" + (2 + 16).to_bytes(2, "big") + "山田太郎".encode("utf-8") + b"\x00" * 4 + b"\xff\xda\x00\x02" + b"\x00" * 8
    src = _docx(tmp_path / "e.docx", _p("本文"), {
        "word/embeddings/Microsoft_Excel_Sheet1.xlsx": xlsx.getvalue(),
        "word/media/image1.jpeg": exif,
        "word/printerSettings/printerSettings1.bin": "山田太郎のプリンタ".encode("utf-16-le"),
        "word/vmlDrawing1.vml": "<xml><v:shape><v:textbox><div>鈴木一郎</div></v:textbox></v:shape></xml>",
    })
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) in (0, 1)
    out = tmp_path / "e_匿名.docx"
    z = zipfile.ZipFile(out)
    assert "word/printerSettings/printerSettings1.bin" not in z.namelist()
    assert "山田太郎".encode() not in z.read("word/media/image1.jpeg")
    inner = zipfile.ZipFile(io.BytesIO(z.read("word/embeddings/Microsoft_Excel_Sheet1.xlsx")))
    assert "山田太郎" not in inner.read("xl/sharedStrings.xml").decode()
    assert "鈴木一郎" not in inner.read("docProps/core.xml").decode()
    assert "鈴木一郎" not in z.read("word/vmlDrawing1.vml").decode()


# ------------------------------------------------------------ 表記の揺れ
@pytest.mark.parametrize("text", [
    "甲" + chr(0x200C) + "斐田光", "甲" + chr(0xE0100) + "斐田光", "甲" + chr(0xAD) + "斐田光",
    "甲斐" + chr(0x2F65) + "光",                    # 康熙部首の「⽥」
])
def test_invisible_chars_and_radicals(text):
    m = Matcher([Entry("甲斐田光", "Ａ", "人名")])
    assert m.sub(text)[0] == "Ａ"


def test_halfwidth_and_nfd_kana():
    m = Matcher([Entry("ヤマダタロウ", "Ａ", "人名")])
    assert m.sub("ﾔﾏﾀﾞﾀﾛｳ")[0] == "Ａ"
    assert m.sub(unicodedata.normalize("NFD", "ヤマダタロウ"))[0] == "Ａ"


def test_variant_kanji_added():
    m = Matcher([Entry("関根太郎", "Ａ", "人名"), Entry("吉田六郎", "Ｂ", "人名")])
    assert m.sub("關　根　太　郎")[0] == "Ａ"
    assert m.sub(chr(0x20BB7) + "田六郎")[0] == "Ｂ"


def test_person_context_words_around_short_names():
    m = Matcher([Entry("山田", "Ｘ", "人名")])
    assert m.sub("患者山田は")[0] == "患者Ｘは"
    assert m.sub("山田容疑者は")[0] == "Ｘ容疑者は"


# ------------------------------------------------------------ 検出
@pytest.mark.parametrize("text,kind", [
    ("原告は昭和50年1月2日に生まれ、", "生年月日"),
    ("原告は昭和五十年一月二日生まれである。", "生年月日"),
    ("口座番号は1234567である。", "口座"),
    ("住所 160-0023 東京都新宿区西新宿2-8-1", "郵便番号"),
    ("被告は東京都新宿区西新宿に居住している。", "住所"),
    ("車両番号 品川 500 さ 1234", "車両番号"),
    ("電話 03—1234—5678", "電話"),
    ("妻の花子は、パートで働いています。", "人名"),
])
def test_statement_style_identifiers(text, kind):
    assert kind in {h.kind for h in scan_unit(text)}


@pytest.mark.parametrize("text", [
    "作成日は03-04-2025である。", "金123456789012円",
    "東京地方裁判所平成３０年５月１０日判決（平成２８年（ワ）第１２３４５号）",
    "芦部信喜『憲法〔第８版〕』、佐藤幸治『日本国憲法論』",
])
def test_not_over_masked(text):
    assert scan_unit(text) == []


def test_readings_follow_the_name():
    col = Collector()
    for t in ["原告山田太郎（ヤマダ　タロウ）は、", "原告山田太郎（Taro Yamada）", "フリガナ　ヤマダ　タロウ", "TARO YAMADA"]:
        col.add(t, "t")
    rows = {c.original: c for c in col.finalize()}
    assert rows["ヤマダタロウ"].replacement == rows["山田太郎"].replacement == "Ｘ"
    assert rows["Taro Yamada"].replacement == "Ｘ"
    m = Matcher([Entry(c.original, c.replacement, c.kind) for c in rows.values()])
    assert "YAMADA" not in m.sub("TARO YAMADA")[0]


def test_labels_avoid_pseudonyms_already_in_text():
    col = Collector()
    for t in ["被告は、（Ａ社対Ｂ事件）を援用する。", "訴外鈴木商事は、取引先である。"]:
        col.add(t, "t")
    rows = {c.original: c for c in col.finalize()}
    assert rows["鈴木商事"].replacement not in ("Ａ社", "Ｂ社")


# ------------------------------------------------------------ テキストの文字コード
@pytest.mark.parametrize("enc", ["utf-16", "iso2022_jp", "euc_jp", "cp932"])
def test_text_encodings(tmp_path, enc):
    src = tmp_path / "t.txt"
    src.write_bytes("原告山田太郎は\n".encode(enc))
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) in (0, 1)
    out = (tmp_path / "t_匿名.txt").read_bytes()
    assert out.decode(enc) == "原告Ｘは\n"


# ------------------------------------------------------------ 検査
def test_swapped_names_are_not_excused(tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("山田太郎と鈴木一郎\n", encoding="utf-8")
    lst = _list(tmp_path, "採否,原文,置換後\n○,山田太郎,鈴木一郎\n○,鈴木一郎,山田太郎\n")
    assert main(["apply", str(src), "--list", str(lst)]) == 2


def test_zero_replacements_is_not_a_pass(tmp_path):
    src = tmp_path / "z.txt"
    src.write_text("本文だけ\n", encoding="utf-8")
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) == 1


# ------------------------------------------------------------ PDF
def _pdf(path, lines, *, size=12, pitch=None, crop=None):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for t in lines:
        page.insert_text((40, y), t, fontname="japan", fontsize=size)
        y += pitch or size * 1.6
    if crop:
        page.insert_text((40, 500), "原告山田太郎（切り取った部分）", fontname="japan", fontsize=size)
        page.set_cropbox(fitz.Rect(*crop))
    doc.save(path)
    return path


def _pdf_text(path):
    import fitz
    flags = fitz.TEXTFLAGS_TEXT & ~fitz.TEXT_MEDIABOX_CLIP
    doc = fitz.open(path)
    page = doc[0]
    page.set_cropbox(page.mediabox)
    return re.sub(r"\s", "", page.get_text("text", flags=flags))


def test_pdf_name_wrapped_across_lines(tmp_path):
    src = _pdf(tmp_path / "w.pdf", ["本件について原告山田", "太郎は請求する。"])
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) in (0, 1)
    assert "山田太郎" not in _pdf_text(tmp_path / "w_匿名.pdf")


def test_pdf_tight_leading_keeps_neighbour_lines(tmp_path):
    src = _pdf(tmp_path / "t.pdf", ["最判平成２０年６月４日民集６２巻６号１３６７頁", "原告山田太郎は、請求する。", "〔ＮＥＸＸ事件〕甲第３号証のとおり。"], pitch=12)
    assert main(["apply", str(src), "--list", str(_list(tmp_path))]) in (0, 1)
    text = _pdf_text(tmp_path / "t_匿名.pdf")
    assert "山田太郎" not in text
    assert "最判平成２０年６月４日民集６２巻６号１３６７頁" in text and "〔ＮＥＸＸ事件〕甲第３号証" in text


def test_pdf_text_outside_cropbox(tmp_path):
    src = _pdf(tmp_path / "c.pdf", ["公開部分"], crop=(0, 0, 595, 300))
    main(["apply", str(src), "--list", str(_list(tmp_path))])
    out = next(tmp_path.glob("c_匿名*.pdf"))
    assert "山田太郎" not in _pdf_text(out)
