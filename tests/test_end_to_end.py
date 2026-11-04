"""コマンドを通しで動かす試験（Word・テキスト・PDF）。"""
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from fixtures import KEEPS, SECRETS, make_pdf, make_text, make_tricky_docx, package_strings
from sumikeshi.cli import main
from sumikeshi.review import Row, read, write


def _no_secrets(text: str) -> list[str]:
    flat = re.sub(r"<[^>]+>", "", text)
    nospace = re.sub(r"[ 　\t\n]", "", flat)
    return [s for s in SECRETS if s in text or s in flat or s in nospace]


def test_docx(tmp_path):
    src = make_tricky_docx(tmp_path / "山田太郎様_準備書面.docx")
    rc = main(["auto", str(src)])
    assert rc == 0
    out = tmp_path / "Ｘ様_準備書面_匿名.docx"           # ファイル名の名前も伏せる
    assert out.exists()
    raw = package_strings(out)
    assert _no_secrets(raw) == []
    flat = re.sub(r"<[^>]+>", "", raw)
    for k in KEEPS:
        assert k in flat, k
    names = zipfile.ZipFile(out).namelist()
    assert not any("thumbnail" in n for n in names)
    assert "<w:ins " not in raw and "<w:del " not in raw and "<w:comment " not in raw
    assert "attachedTemplate" not in raw and "docVar" not in raw
    # 置換しなかった部分の書式は残る（「原告」は太字のまま）
    import docx
    d = docx.Document(str(out))
    p = next(p for p in d.paragraphs if p.text.startswith("１　原告"))
    assert p.text.startswith("１　原告Ｘは")
    assert p.runs[0].bold is True
    assert (tmp_path / "Ｘ様_準備書面_匿名_検査.txt").exists()


def test_docx_opens_in_libreoffice(tmp_path):
    soffice = shutil.which("soffice")
    if not soffice:
        pytest.skip("LibreOffice がない")
    src = make_tricky_docx(tmp_path / "doc.docx")
    assert main(["auto", str(src)]) == 0
    out = tmp_path / "doc_匿名.docx"
    r = subprocess.run([soffice, f"-env:UserInstallation=file://{tmp_path}/lo", "--headless",
                        "--convert-to", "txt:Text", "--outdir", str(tmp_path / "lo_out"), str(out)],
                       capture_output=True, text=True, timeout=180)
    txt = (tmp_path / "lo_out" / "doc_匿名.txt").read_text(encoding="utf-8", errors="replace")
    assert "原告" in txt and "Ｘ" in txt, r.stderr
    assert _no_secrets(txt) == []


def test_text(tmp_path):
    src = make_text(tmp_path / "memo.txt")
    assert main(["auto", str(src)]) == 0
    out = (tmp_path / "memo_匿名.txt").read_text(encoding="utf-8")
    assert _no_secrets(out) == []
    assert "原告Ｘは" in out


def test_text_cp932_roundtrip(tmp_path):
    src = tmp_path / "sjis.txt"
    src.write_bytes("原告山田太郎は、被告鈴木一郎に対し、\n".encode("cp932"))
    assert main(["auto", str(src)]) == 0
    out = (tmp_path / "sjis_匿名.txt").read_bytes().decode("cp932")
    assert out == "原告Ｘは、被告Ｙに対し、\n"


def test_pdf(tmp_path):
    pytest.importorskip("fitz")
    import fitz
    src = make_pdf(tmp_path / "evidence.pdf")
    assert main(["auto", "--style", "kuro", str(src)]) == 0
    d = fitz.open(tmp_path / "evidence_匿名.pdf")
    text = "".join(p.get_text() for p in d)
    assert _no_secrets(text) == []
    flat = re.sub(r"[ 　\t\n]", "", text)
    for k in KEEPS:
        assert k in flat, k
    assert not d.metadata.get("author")
    assert sum(len(list(p.annots() or [])) for p in d) == 0


def test_review_roundtrip_and_manual_rows(tmp_path):
    src = make_text(tmp_path / "memo.txt")
    assert main(["scan", str(src)]) == 0
    lst = tmp_path / "memo_候補.xlsx"
    rows, sources = read(lst)
    assert sources == [str(src)]
    assert any(r.original == "山田太郎" and r.accept for r in rows)
    # 人が直したことにする: 山田太郎は残し、見落とし「準備書面」を足す
    from openpyxl import load_workbook
    wb = load_workbook(lst)
    ws = wb["候補"]
    for row in ws.iter_rows(min_row=2):
        if row[2].value == "山田太郎":
            row[0].value = "×"
    ws.append([None, None, "準備書面", "【書面】"])
    wb.save(lst)
    assert main(["apply", "--list", str(lst)]) in (0, 1)
    out = (tmp_path / "memo_匿名.txt").read_text(encoding="utf-8")
    assert "山田太郎" in out and "【書面】" in out and "準備書面" not in out


def test_scan_refuses_to_overwrite_edited_list(tmp_path):
    src = make_text(tmp_path / "memo.txt")
    assert main(["scan", str(src)]) == 0
    with pytest.raises(SystemExit):
        main(["scan", str(src)])


def test_csv_list_written_in_cp932_by_excel(tmp_path):
    src = make_text(tmp_path / "memo.txt")
    lst = tmp_path / "list.csv"
    lst.write_bytes("採否,種別,原文,置換後\n○,人名,山田太郎,Ｘ\n×,人名,鈴木一郎,Ｂ\n".encode("cp932"))
    assert main(["apply", str(src), "--list", str(lst)]) in (0, 1)
    out = (tmp_path / "memo_匿名.txt").read_text(encoding="utf-8")
    assert "原告Ｘは" in out


def test_failed_check_marks_output(tmp_path):
    # 一覧に無い場所（ファイル名だけ伏せて本文を残す）は、検査で不合格になり出力名に印が付く
    src = tmp_path / "山田太郎.txt"
    src.write_text("原告山田太郎は\n", encoding="utf-8")
    lst = tmp_path / "l.csv"
    write([], lst, [], "xy")
    lst.write_text("採否,原文,置換後\n○,山田太郎,山田太郎\n", encoding="utf-8-sig")
    rc = main(["apply", str(src), "--list", str(lst)])
    assert rc == 2
    assert (tmp_path / "山田太郎_匿名_不合格.txt").exists()
