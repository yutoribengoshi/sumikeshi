"""EXE にファイルを重ねたときの振り分け（サブコマンドなしの呼び出し）。"""
import pytest

from fixtures import make_text
from sumikeshi.cli import main


@pytest.fixture(autouse=True)
def no_open(monkeypatch):
    # 試験で Excel を開かない
    monkeypatch.setenv("SUMIKESHI_NO_OPEN", "1")


def test_drop_document_then_list(tmp_path, capsys):
    doc = make_text(tmp_path / "memo.txt")
    assert main([str(doc)]) == 0                       # 文書を重ねる → 一覧
    lst = tmp_path / "memo_候補.xlsx"
    assert lst.exists()
    assert "EXE に重ねる" in capsys.readouterr().out
    assert main([str(doc)]) == 0                       # もう一度重ねても一覧は作り直さない
    assert "候補一覧がもうあります" in capsys.readouterr().out
    assert main([str(lst)]) in (0, 1)                  # 一覧を重ねる → 伏せる
    out = (tmp_path / "memo_匿名.txt").read_text(encoding="utf-8")
    assert "山田太郎" not in out
    assert main([str(lst)]) in (0, 1)                  # 直して重ね直すと作り直す


def test_drop_errors_are_reported_not_raised(tmp_path, capsys):
    assert main([str(tmp_path / "ない.docx")]) == 2
    assert "見つかりません" in capsys.readouterr().out
