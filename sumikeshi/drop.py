"""ファイルを sumikeshi.exe に重ねて（ドラッグ＆ドロップして）使う入口。

  文書（.docx .txt .md .pdf）を重ねる → 候補一覧（Excel）を作って開く
  直した一覧（〜_候補.xlsx / .csv）を重ねる → 伏せた文書と検査の報告を作る

EXE をダブルクリックで開いた窓がすぐ閉じないよう、終わると Enter を押すまで画面を残す。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
from pathlib import Path

LIST_SUFFIXES = (".xlsx", ".csv")

GUIDE = """sumikeshi（すみけし）— 法律文書の名前・住所などを、この PC の中だけで伏せます。

使い方: ファイルを、この sumikeshi.exe のアイコンの上に重ねてください（ドラッグ＆ドロップ）。
  1. Word・テキストの文書（.docx .txt .md）を重ねる
     → 伏せる候補の一覧（〜_候補.xlsx）ができて、Excel で開きます
  2. 一覧の「採否」を ○（伏せる）か ×（伏せない）にし、「置換後」を直して保存する
     見落としがあれば、一覧の下に行を足す
  3. 直した一覧（〜_候補.xlsx）を重ねる
     → 伏せた文書（〜_匿名.docx）と検査の報告（〜_匿名_検査.txt）ができます

注意
  ・一覧と報告には、伏せる前の名前が載っています。外に出さないでください。
  ・外に出す前に、伏せた文書を必ず全文読んでください。
  ・画像（印影・写真・スキャン）の中の文字は伏せられません。"""


def _open(path: Path) -> None:
    """一覧を Excel で開く（開くだけで、閉じる操作はしない）。"""
    if os.environ.get("SUMIKESHI_NO_OPEN"):
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError:
        pass


def _run(paths: list[str]) -> int:
    from . import cli

    if not paths:
        print(GUIDE)
        return 0
    lists = [p for p in paths if Path(p).suffix.lower() in LIST_SUFFIXES]
    docs = [p for p in paths if p not in lists]
    if len(lists) > 1:
        raise SystemExit("一覧は1つずつ重ねてください")
    if lists:
        # 直した一覧を重ねた: 伏せる。前に作った出力は作り直す（一覧を直してやり直すのがふつうなので）
        args = argparse.Namespace(list=lists[0], files=docs, outdir=None, force=True, address="city")
        rc = cli.do_apply(args)
        print("\n伏せた文書は、元の文書と同じフォルダにあります。外に出す前に全文を読んでください。")
        return rc
    files = cli._expand(docs)
    lp = cli._list_path(files, None)
    if lp.exists():
        print(f"候補一覧がもうあります: {lp.name}")
        print("直し終えたら、その一覧をこの EXE に重ねてください（作り直すときは、一覧を消してからもう一度）。")
        _open(lp)
        return 0
    args = argparse.Namespace(files=docs, output=None, style="xy", address="city", keep=None, keep_file=None,
                              no_sudachi=False, force=False, drop=True)
    rc = cli.do_scan(args)
    _open(lp)
    return rc


def main(paths: list[str]) -> int:
    pause = getattr(sys, "frozen", False) and not os.environ.get("SUMIKESHI_NO_PAUSE")
    try:
        return _run(paths)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
            return 2
        return e.code or 0
    except Exception:
        traceback.print_exc()
        print("\n思わぬ誤りで止まりました。上の表示を写して、作った人に知らせてください。")
        return 2
    finally:
        if pause:
            try:
                input("\nEnter キーを押すと閉じます")
            except EOFError:
                pass
