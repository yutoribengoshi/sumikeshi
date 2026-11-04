"""EXE ができた後の通しの確かめ（Windows の実行環境で使う）。

  python packaging/smoke.py make  <フォルダ>   試験の文書を作る
  python packaging/smoke.py check <フォルダ>   伏せた文書に名前が残っていないか確かめる
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from fixtures import SECRETS, make_text, make_tricky_docx, package_strings  # noqa: E402


def main() -> int:
    cmd, folder = sys.argv[1], Path(sys.argv[2])
    if cmd == "make":
        folder.mkdir(parents=True, exist_ok=True)
        make_tricky_docx(folder / "doc.docx")
        make_text(folder / "memo.txt")
        return 0
    bad = []
    expect = [folder / "doc_匿名.docx", folder / "doc_匿名_検査.txt", folder / "memo_候補.xlsx",
              folder / "memo_匿名.txt"]
    for p in expect:
        if not p.exists():
            bad.append(f"できていない: {p.name}")
    for p in (folder / "doc_匿名.docx", folder / "memo_匿名.txt"):
        if p.exists():
            raw = package_strings(p) if p.suffix == ".docx" else p.read_text(encoding="utf-8")
            flat = re.sub(r"<[^>]+>", "", raw)
            for s in SECRETS:
                if s in raw or s in flat or s in re.sub(r"[ 　\t\n]", "", flat):
                    bad.append(f"{p.name} に残っている: {s}")
    print("\n".join(bad) if bad else "通しの確かめ: 合格")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
