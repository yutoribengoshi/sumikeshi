"""コマンドライン。

  sumikeshi scan  準備書面.docx                → 候補一覧（Excel）を作る
  sumikeshi apply --list 準備書面_候補.xlsx     → 一覧の ○ の行を伏せた文書と検査の報告を作る
  sumikeshi auto  準備書面.docx                → scan と apply を続けて（一覧は直さずに）
  sumikeshi check 匿名化した文書 --list 一覧     → 検査だけ
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from . import __version__
from . import sudachi as S
from .collect import STYLES, Collector
from .matcher import Entry, Matcher
from .review import Row
from .review import read as read_list
from .review import write as write_list

SUPPORTED = (".docx", ".docm", ".txt", ".md", ".pdf")
SUFFIX_OUT = "_匿名"
# 名前かフォルダ名にこの語があるファイルは中身を開かない（画像などを名前の段階で隔離する印）
BLOCK_MARK = "読み取り禁止"
SUBCOMMANDS = ("scan", "apply", "auto", "check")


def _io(path: Path):
    ext = path.suffix.lower()
    if ext in (".docx", ".docm"):
        from . import docx_io
        return docx_io
    if ext == ".pdf":
        from . import pdf_io
        return pdf_io
    from . import text_io
    return text_io


def blocked_path(p: Path) -> bool:
    """名前・パス・別名（シンボリックリンク）の行き先のどれかに「読み取り禁止」があれば開かない。

    行き先を調べられないものも開かない側に倒す。
    """
    if BLOCK_MARK in unicodedata.normalize("NFC", str(p)):
        return True
    try:
        return BLOCK_MARK in unicodedata.normalize("NFC", str(Path(p).resolve()))
    except (OSError, RuntimeError):
        return True


def guard(p: Path) -> None:
    """開く直前の関所。拾う所で外しているはずだが、別の経路から来たときのために必ず通す。"""
    if blocked_path(p):
        raise SystemExit(f"「{BLOCK_MARK}」の印があるファイルは開きません。開くなら、ファイル名からその語を消してください")


def _expand(args: list[str]) -> list[Path]:
    """PowerShell は * を展開しないので、こちらで展開する。フォルダは中の対応ファイルを全部。

    「読み取り禁止」の印があるものは名前を見るだけで開かず、本数だけ知らせる。
    """
    out: list[Path] = []
    blocked = 0
    for a in args:
        paths = [Path(p) for p in glob.glob(a)] if any(ch in a for ch in "*?[") else [Path(a)]
        for p in paths:
            if blocked_path(p):
                blocked += 1
            elif p.is_dir():
                for q in sorted(p.iterdir()):
                    if q.suffix.lower() not in SUPPORTED:
                        continue
                    if blocked_path(q):
                        blocked += 1
                    else:
                        out.append(q)
            else:
                out.append(p)
    if blocked:
        print(f"「{BLOCK_MARK}」の印があるもの {blocked} 件は、開かずに外しました")
    res = []
    for p in out:
        if p.name.startswith("~$") or SUFFIX_OUT in p.stem:
            continue
        if not p.exists():
            raise SystemExit(f"ファイルが見つかりません: {p}")
        if p.suffix.lower() not in SUPPORTED:
            raise SystemExit(f"対応していない形式です（{', '.join(SUPPORTED)}）: {p}")
        res.append(p.resolve())
    if not res:
        raise SystemExit("対象のファイルがありません")
    return res


def _locked(path: Path) -> bool:
    """Word で開かれている（~$ の鍵ファイルがある）か。"""
    name = path.name
    cands = {f"~${name}", f"~${name[2:]}", f"~${name[1:]}"}
    return any((path.parent / c).exists() for c in cands)


def _keep_list(args) -> list[str]:
    keep = [k.strip() for k in (args.keep or "").replace("、", ",").split(",") if k.strip()]
    if args.keep_file:
        guard(Path(args.keep_file))
        keep += [ln.strip() for ln in Path(args.keep_file).read_text(encoding="utf-8-sig").splitlines()
                 if ln.strip() and not ln.startswith("#")]
    return keep


def _list_path(files: list[Path], given: str | None) -> Path:
    if given:
        return Path(given).resolve()
    if len(files) == 1:
        return files[0].with_name(files[0].stem + "_候補.xlsx")
    return files[0].with_name("匿名化候補.xlsx")


def _hint_sudachi() -> None:
    if not S.available() and not getattr(sys, "frozen", False):
        print("（参考）Sudachi が入っていないので、文中に姓・名だけで出てくる人は拾っていません。"
              "入れるには: pip install sudachipy sudachidict_core")


# ------------------------------------------------------------ scan
def do_scan(args) -> int:
    if args.no_sudachi:
        S.disable()
    files = _expand(args.files)
    out = _list_path(files, args.output)
    if out.exists() and not args.force:
        raise SystemExit(f"候補一覧がもうあります（直した内容を消さないよう止めました）: {out}\n"
                         "作り直すときは --force を付けてください")
    col = Collector(style=args.style, address=args.address, keep=_keep_list(args))
    for f in files:
        print(f"読み取り中: {f.name}")
        guard(f)
        try:
            units = _io(f).read_units(f)
        except Exception as e:  # 壊れたファイル・パスワード付きなど
            raise SystemExit(f"読めませんでした: {f}\n  {e}")
        col.add(f.stem, f"{f.name}（ファイル名）")
        for text, loc in units:
            col.add(text, f"{f.name} {loc}")
    cands = col.finalize()
    write_list(cands, out, [str(f) for f in files], args.style)
    _chmod_private(out)
    by_kind: dict[str, int] = {}
    for c in cands:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    print(f"\n候補 {len(cands)} 件: " + "、".join(f"{k} {v}" for k, v in by_kind.items()))
    print(f"候補一覧: {out}")
    _hint_sudachi()
    print("\n次の手順:")
    print("  1. 候補一覧を Excel で開き、採否（○/×）と置換後を直す。見落としは行を足す")
    if getattr(args, "drop", False):
        print("  2. 保存したら、その一覧をこの EXE に重ねる")
    else:
        print(f"  2. sumikeshi apply --list \"{out}\"")
    return 0


# ------------------------------------------------------------ apply
def _entries(rows: list[Row]) -> tuple[list[Entry], list[Entry]]:
    acc = [Entry(r.original, r.replacement, r.kind, r.cid) for r in rows if r.accept]
    rej = [Entry(r.original, r.replacement, r.kind, r.cid) for r in rows if not r.accept]
    return acc, rej


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _process(f: Path, acc: list[Entry], rej: list[Entry], outdir: Path | None, force: bool,
             list_path: Path | None, address: str) -> int:
    from . import verify

    guard(f)
    io = _io(f)
    matcher = Matcher(acc)
    stem = matcher.sub(f.stem)[0] if matcher else f.stem
    outdir = outdir or f.parent
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{stem}{SUFFIX_OUT}{f.suffix}"
    bad = outdir / f"{stem}{SUFFIX_OUT}_不合格{f.suffix}"
    report_path = outdir / f"{stem}{SUFFIX_OUT}_検査.txt"
    for p in (out, bad):
        if _locked(p):
            raise SystemExit(f"出力先が Word で開かれています。閉じてからもう一度: {p}")
        if p.exists() and not force:
            raise SystemExit(f"出力先がもうあります（上書きするなら --force）: {p}")
    tmp = outdir / f".{stem}{SUFFIX_OUT}.tmp{f.suffix}"
    notes: list[str] = []
    ext = f.suffix.lower()
    if ext in (".docx", ".docm"):
        st = io.apply(f, tmp, matcher)
        counts = st.counts
        hyg = io.hygiene(tmp)
        structure = ("段落数", io.paragraph_count(f), io.paragraph_count(tmp))
        if st.revisions:
            notes.append(f"変更履歴 {st.revisions} 件は、すべて承諾した形にしました（削除された文字は消しました）")
        if st.comments:
            notes.append(f"コメント {st.comments} 件を消しました")
        if st.images:
            notes.append(f"画像 {st.images} 枚の中の文字（印影・写真・スキャン）は確かめていません")
        if st.embeddings:
            notes.append(f"埋め込みのファイル {st.embeddings} 個（Excel の表など）の中は確かめていません")
        if st.macros:
            notes.append("マクロが入っています。中は確かめていません")
        notes += st.warnings
    elif ext == ".pdf":
        st = io.apply(f, tmp, matcher)
        counts = st.counts
        hyg = io.hygiene(tmp)
        structure = ("頁数", st.pages, io.page_count(tmp))
        if st.pages_without_text:
            pages = st.pages_without_text
            notes.append(f"文字の層が無い頁があります（{', '.join(map(str, pages[:20]))}頁）。"
                         "画像だけの頁は伏せられないので、OCR してからもう一度")
        if st.images:
            notes.append(f"画像 {st.images} 個の中の文字は、文字の層に無い限り確かめていません")
    else:
        counts, _enc = io.apply(f, tmp, matcher)
        hyg = None
        structure = ("行数", io.line_count(f), io.line_count(tmp))
    rep = verify.run(accepted=acc, rejected=rej, counts=counts, matcher=matcher,
                     in_units=io.read_units(f), out_units=io.read_units(tmp),
                     out_strings=io.all_strings(tmp) + [(stem, "ファイル名")],
                     structure=structure, hygiene=hyg, notes=notes, address=address)
    if ext == ".pdf" and st.pages_without_text:
        rep.bump(verify.WARN if len(st.pages_without_text) < st.pages else verify.FAIL)
    final = bad if rep.verdict == verify.FAIL else out
    if final.exists():
        final.unlink()
    tmp.replace(final)
    head = [f"入力: {f}", f"出力: {final}", f"候補一覧: {list_path or '（指定なし）'}",
            f"日時: {datetime.now():%Y-%m-%d %H:%M}"]
    report_path.write_text(rep.render(head), encoding="utf-8-sig")
    _chmod_private(report_path)
    total = sum(counts.values())
    print(f"{f.name} → {final.name}")
    print(f"  置換 {total} 件 / 判定: {rep.verdict}  （報告: {report_path.name}）")
    for title, lines in rep.sections:
        if title[0] in "△×":
            print(f"  {title}")
            for ln in lines[:3]:
                print(f"      {ln}")
    if rep.verdict == verify.FAIL:
        print("  ※ 不合格です。このファイルはまだ外に出さないでください。")
    return rep.exit_code


def do_apply(args) -> int:
    list_path = Path(args.list).resolve()
    guard(list_path)
    rows, sources = read_list(list_path)
    files = _expand(args.files) if args.files else _expand(sources) if sources else None
    if not files:
        raise SystemExit("対象のファイルを指定してください（CSV の一覧には対象ファイルが記録されていません）")
    acc, rej = _entries(rows)
    print(f"一覧: 伏せる {len(acc)} 件 / 残す {len(rej)} 件")
    outdir = Path(args.outdir).resolve() if args.outdir else None
    return max(_process(f, acc, rej, outdir, args.force, list_path, args.address) for f in files)


def do_auto(args) -> int:
    rc = do_scan(args)
    if rc:
        return rc
    args.list = str(_list_path(_expand(args.files), args.output))
    print("")
    return do_apply(args)


def do_check(args) -> int:
    from . import verify

    guard(Path(args.list))
    rows, _ = read_list(Path(args.list).resolve())
    acc, rej = _entries(rows)
    worst = 0
    for f in _expand_any(args.files):
        guard(f)
        io = _io(f)
        ext = f.suffix.lower()
        hyg = io.hygiene(f) if ext in (".docx", ".docm", ".pdf") else None
        rep = verify.run(accepted=acc, rejected=rej, counts=None, matcher=Matcher(acc),
                         in_units=io.read_units(f), out_units=io.read_units(f),
                         out_strings=io.all_strings(f) + [(f.stem, "ファイル名")],
                         structure=None, hygiene=hyg, notes=[], address=args.address)
        print(rep.render([f"検査したファイル: {f}"]))
        worst = max(worst, rep.exit_code)
    return worst


def _expand_any(args: list[str]) -> list[Path]:
    """check 用: 伏せた後のファイル（名前に _匿名 が付く）も対象にする。"""
    out, blocked = [], 0
    for a in args:
        for p in (glob.glob(a) if any(c in a for c in "*?[") else [a]):
            if blocked_path(Path(p)):
                blocked += 1
            else:
                out.append(Path(p).resolve())
    if blocked:
        print(f"「{BLOCK_MARK}」の印があるもの {blocked} 件は、開かずに外しました")
    for p in out:
        if not p.exists():
            raise SystemExit(f"ファイルが見つかりません: {p}")
    if not out:
        raise SystemExit("対象のファイルがありません")
    return out


# ------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sumikeshi",
        description="法律文書（Word・テキスト・PDF）の名前・住所・電話番号などを、手元の PC だけで置換・墨消しする。",
        epilog="流れ: scan で候補一覧を作る → Excel で ○/× と置換後を直す → apply で伏せて検査する")
    ap.add_argument("--version", action="version", version=f"sumikeshi {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, *, listing: bool):
        p.add_argument("--address", choices=("city", "pref", "all"), default="city",
                       help="住所をどこまで残すか（city=市区町村まで〔既定〕 / pref=都道府県まで / all=全部伏せる）")
        if listing:
            p.add_argument("--style", choices=STYLES, default="xy",
                           help="置換の形（xy=原告側Ｘ・被告側Ｙ・ほかＡＢＣ〔既定〕 / abc=全部ＡＢＣ / "
                                "maru=○○ / kuro=■で墨消し）")
            p.add_argument("--keep", help="伏せない名前（カンマ区切り）。例: --keep \"甲野法律事務所,乙野次郎\"")
            p.add_argument("--keep-file", help="伏せない名前の一覧（1行1語のテキスト）")
            p.add_argument("--no-sudachi", action="store_true", help="Sudachi を使わない（規則だけで拾う）")
            p.add_argument("-o", "--output", help="候補一覧の保存先（.xlsx か .csv）")
        p.add_argument("--force", action="store_true", help="同じ名前のファイルがあっても上書きする")

    p = sub.add_parser("scan", help="伏せる候補を洗い出して一覧（Excel）を作る")
    p.add_argument("files", nargs="+", help="対象のファイル（.docx .txt .md .pdf）。フォルダや *.docx も可")
    common(p, listing=True)
    p.set_defaults(func=do_scan)

    p = sub.add_parser("apply", help="一覧の ○ の行を伏せて、新しいファイルと検査の報告を作る")
    p.add_argument("files", nargs="*", help="対象のファイル（省略すると一覧に記録したファイル）")
    p.add_argument("--list", "-l", required=True, help="候補一覧（scan で作ったもの）")
    p.add_argument("--outdir", help="出力先のフォルダ（既定: 元のファイルと同じ場所）")
    common(p, listing=False)
    p.set_defaults(func=do_apply)

    p = sub.add_parser("auto", help="scan と apply を続けて行う（一覧は直さずに伏せる）")
    p.add_argument("files", nargs="+")
    p.add_argument("--outdir", help="出力先のフォルダ")
    common(p, listing=True)
    p.set_defaults(func=do_auto)

    p = sub.add_parser("check", help="伏せた後のファイルを検査だけする")
    p.add_argument("files", nargs="+")
    p.add_argument("--list", "-l", required=True, help="候補一覧")
    p.add_argument("--address", choices=("city", "pref", "all"), default="city", help=argparse.SUPPRESS)
    p.set_defaults(func=do_check)
    return ap


def _setup_streams() -> None:
    """出力の文字コード。日本語の Windows の画面ならそのまま。日本語を書けない行き先
    （英語版 Windows でファイルや GitHub のログに向けたとき）は UTF-8 にする。どちらでも落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            "日本語○×→".encode(stream.encoding or "ascii")
            stream.reconfigure(errors="replace")
        except (UnicodeEncodeError, LookupError):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _setup_streams()
    argv = sys.argv[1:] if argv is None else list(argv)
    # サブコマンドの無い呼び出し（EXE へのドラッグ＆ドロップ、ダブルクリック）はファイルの種類で振り分ける
    if (argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-")) \
            or (not argv and getattr(sys, "frozen", False)):
        from . import drop
        return drop.main(argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args)
