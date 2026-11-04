"""「読み取り禁止」の印があるファイル・フォルダは、名前を見るだけで中身を開かない。

印つきのファイルは権限を0にして置く。道具が開こうとすれば OS が拒み、試験が落ちる。
"""
import os
from pathlib import Path

import pytest

from sumikeshi.cli import blocked_path, guard, main

MARK = "読み取り禁止"


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "memo.txt").write_text("原告山田太郎は\n", encoding="utf-8")
    locked = tmp_path / f"{MARK}_写真の説明.txt"
    locked.write_text("被告鈴木一郎は\n", encoding="utf-8")
    sub = tmp_path / MARK
    sub.mkdir()
    (sub / "inner.txt").write_text("被告鈴木一郎は\n", encoding="utf-8")
    os.chmod(locked, 0)
    os.chmod(sub / "inner.txt", 0)
    yield tmp_path
    for p in (locked, sub / "inner.txt"):
        if p.exists():
            os.chmod(p, 0o600)


def test_scan_folder_skips_marked_files(folder, capsys):
    assert main(["scan", str(folder), "-o", str(folder / "l.csv")]) == 0
    out = capsys.readouterr().out
    assert "1 件は、開かずに外しました" in out
    assert "鈴木" not in (folder / "l.csv").read_text(encoding="utf-8-sig")


def test_marked_folder_is_not_opened(folder):
    with pytest.raises(SystemExit):
        main(["scan", str(folder / MARK), "-o", str(folder / "l2.csv")])


def test_guard_stops_readable_marked_file(tmp_path):
    # 読める権限のままでも、関所が止める（関所が無ければ通ってしまう）
    p = tmp_path / f"{MARK}.txt"
    p.write_text("x", encoding="utf-8")
    assert blocked_path(p)
    with pytest.raises(SystemExit):
        guard(p)


def test_symlink_to_marked_target(tmp_path):
    target_dir = tmp_path / MARK
    target_dir.mkdir()
    (target_dir / "x.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target_dir / "x.txt")
    except OSError:
        pytest.skip("シンボリックリンクを作れない環境")
    assert blocked_path(link)


def test_apply_with_marked_list_is_refused(tmp_path):
    src = tmp_path / "memo.txt"
    src.write_text("原告山田太郎は\n", encoding="utf-8")
    lst = tmp_path / f"{MARK}_一覧.csv"
    lst.write_text("採否,原文,置換後\n○,山田太郎,Ｘ\n", encoding="utf-8-sig")
    os.chmod(lst, 0)
    try:
        with pytest.raises(SystemExit):
            main(["apply", str(src), "--list", str(lst)])
    finally:
        os.chmod(lst, 0o600)
