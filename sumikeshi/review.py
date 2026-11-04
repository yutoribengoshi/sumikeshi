"""候補一覧（Excel / CSV）の書き出しと読み込み。

一覧には原文（伏せる前の名前）が載る。対応表そのものなので、外には出さない。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from .collect import Candidate

COLUMNS = ["採否", "種別", "原文", "置換後", "件数", "確度", "根拠", "文脈（最初の1件）", "ファイル", "備考", "ID"]
_NO = {"×", "✕", "✖", "x", "X", "ｘ", "Ｘ", "0", "０", "no", "No", "NO", "いいえ", "否", "不要", "残す",
       "除外", "外す", "-", "－", "ー", "FALSE", "False", "false"}
_GUIDE = [
    "この一覧の使い方",
    "",
    "1. 「採否」が ○ の行を伏せます。伏せない行は × にしてください（空欄は ○ として扱います）。",
    "2. 「置換後」は書き換えてかまいません。同じ人・同じ会社には同じ記号を振ってあります。",
    "   置換後を ■ だけにすると、その語は字数分の ■ で墨消しします（字間の空白は残ります）。",
    "3. 見落としがあれば、下に行を足してください（採否・原文・置換後だけで足ります）。",
    "   原文は空白を詰めて書けば、本文の「山　田　太　郎」のような字間の空白にも当たります。",
    "4. 保存したら、ターミナル（PowerShell）で次を実行します:",
    "   sumikeshi apply --list \"この一覧のファイル\"",
    "",
    "注意",
    "・この一覧と検査の報告書には伏せる前の名前が載っています。外に出さないでください。",
    "・機械の検査で合格しても、外に出す前に必ず全文を目で読んでください。",
    "・画像（印影・写真・スキャン）の中の文字は伏せられません。",
    "",
    "対象ファイル（apply でファイルを指定しなかったときに使います）",
]


@dataclass
class Row:
    accept: bool
    kind: str
    original: str
    replacement: str
    cid: str = ""


def _accept_value(v) -> bool:
    s = "" if v is None else str(v).strip()
    return s not in _NO


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _rows_of(c: Candidate) -> list:
    return ["○" if c.accept else "×", c.kind, c.original, c.replacement, c.count, c.conf, c.rule,
            c.context, "、".join(c.files), c.note, c.cid]


# ------------------------------------------------------------ 書き出し
def write(cands: list[Candidate], path: Path, sources: list[str], style: str) -> None:
    if path.suffix.lower() == ".csv":
        _write_csv(cands, path)
    else:
        _write_xlsx(cands, path, sources, style)


def _write_csv(cands, path: Path) -> None:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(COLUMNS)
    for c in cands:
        wr.writerow(_rows_of(c))
    # Excel が文字化けしないよう BOM 付きの UTF-8 で書く
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def _write_xlsx(cands, path: Path, sources: list[str], style: str) -> None:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "候補"
    ws.append(COLUMNS)
    for c in cands:
        ws.append(_rows_of(c))
    last = max(len(cands) + 1, 2)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
    # 数字の原文（電話・口座）が数値に化けないよう、文字列の書式にしておく
    for row in ws.iter_rows(min_row=2, max_row=last + 300, max_col=len(COLUMNS)):
        for cell in row:
            cell.number_format = "@"
            if cell.column in (8, 10):
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    widths = [6, 9, 30, 18, 6, 6, 26, 48, 18, 30, 7]
    for i, wdt in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = wdt
    dv = DataValidation(type="list", formula1='"○,×"', allow_blank=True)
    dv.add(f"A2:A{last + 300}")
    ws.add_data_validation(dv)
    ws.conditional_formatting.add(
        f"A2:K{last + 300}", FormulaRule(formula=['$A2="×"'], fill=PatternFill("solid", fgColor="E7E6E6"),
                                         font=Font(color="808080")))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{last}"

    g = wb.create_sheet("使い方")
    for line in _GUIDE:
        g.append([line])
    for s in sources:
        g.append([s])
    g.append([])
    g.append([f"置換の形: {style}"])
    g.column_dimensions["A"].width = 110
    g["A1"].font = Font(bold=True, size=13)
    # 作成者に使う人の名前を残さない
    wb.properties.creator = "sumikeshi"
    wb.properties.lastModifiedBy = "sumikeshi"
    wb.save(path)


# ------------------------------------------------------------ 読み込み
def read(path: Path) -> tuple[list[Row], list[str]]:
    """(行, 対象ファイル)。CSV のときは対象ファイルは空。"""
    if path.suffix.lower() == ".csv":
        return _read_csv(path), []
    return _read_xlsx(path)


def _from_table(header: list, records) -> list[Row]:
    pos = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
    for need in ("原文", "置換後"):
        if need not in pos:
            raise ValueError(f"一覧に「{need}」の列がありません")

    def get(rec, name):
        i = pos.get(name)
        return rec[i] if i is not None and i < len(rec) else None

    rows = []
    for rec in records:
        orig = _text(get(rec, "原文")).strip()
        if not orig:
            continue
        rows.append(Row(_accept_value(get(rec, "採否")), _text(get(rec, "種別")).strip() or "手入力",
                        orig, _text(get(rec, "置換後")), _text(get(rec, "ID")).strip()))
    return rows


def _read_csv(path: Path) -> list[Row]:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV の文字コードを読めませんでした（UTF-8 か Shift_JIS で保存してください）")
    recs = list(csv.reader(io.StringIO(text)))
    if not recs:
        return []
    return _from_table(recs[0], recs[1:])


def _read_xlsx(path: Path) -> tuple[list[Row], list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb["候補"] if "候補" in wb.sheetnames else wb.worksheets[0]
    recs = list(ws.iter_rows(values_only=True))
    rows = _from_table(list(recs[0]), recs[1:]) if recs else []
    sources: list[str] = []
    if "使い方" in wb.sheetnames:
        lines = [_text(r[0]) for r in wb["使い方"].iter_rows(values_only=True)]
        if _GUIDE[-1] in lines:
            for s in lines[lines.index(_GUIDE[-1]) + 1:]:
                if not s.strip():
                    break
                sources.append(s.strip())
    return rows, sources
