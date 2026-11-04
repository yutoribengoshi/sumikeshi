"""検出の回帰試験。"""
from sumikeshi.collect import Collector
from sumikeshi.detect import scan_unit
from sumikeshi.matcher import core


def found(text, kind=None):
    return {core(h.text) for h in scan_unit(text) if kind is None or h.kind == kind}


def test_role_and_honorific():
    assert "山田太郎" in found("原告山田太郎は、", "人名")
    assert "佐々木健" in found("訴外佐々木健は", "人名")          # 々 を取りこぼさない
    assert "鈴木一郎" in found("被告代表者代表取締役鈴木一郎", "人名")
    assert "中野" in found("担当者である中野室長は", "人名")


def test_one_char_prefix_is_not_stripped():
    # 「前」「元」を役割語として剥がすと「前田」「元木」の頭が消える
    assert "前田太郎" in found("原告前田太郎は", "人名")
    assert "元木花子" in found("被告元木花子は", "人名")


def test_hon_in_surname():
    # 法律語の「本」で「山本・橋本」を落とさない（書式辞書で実際に起きた）
    assert "山本一郎" in found("原告山本一郎は", "人名")
    assert "橋本" in found("橋本様", "人名")


def test_agent_name():
    hits = [h for h in scan_unit("原告訴訟代理人弁護士　佐　藤　　一　郎") if h.kind == "代理人"]
    assert [core(h.text) for h in hits] == ["佐藤一郎"]


def test_citation_is_protected():
    # 引用判例の事件名を社名と取り違えない
    text = "最判平成２０年６月４日民集６２巻６号１３６７頁〔ＮＥＸＸ事件〕、民法７０９条、甲第３号証"
    assert found(text) == set()


def test_not_a_company():
    assert "営む株式会社" not in found("建築業を営む株式会社であり", "法人")
    assert "株式会社みどり建設" in found("被告株式会社みどり建設は", "法人")
    assert "ＡＢＣ商事株式会社" in found("訴外ＡＢＣ商事株式会社の", "法人")


def test_address_keeps_city():
    hits = [h for h in scan_unit("〒１６０－００２３　東京都新宿区西新宿二丁目８番１号　グリーンハイツ３０１号室") if h.kind == "住所"]
    assert hits and hits[0].replacement == "東京都新宿区○○"
    assert core(hits[0].text).endswith("３０１号室")
    hits = [h for h in scan_unit("横浜市中区山下町１番地の現場") if h.kind == "住所"]
    assert hits and hits[0].replacement == "横浜市中区○○"
    assert not [h for h in scan_unit("東京都新宿区内において") if h.kind == "住所"]


def test_fixed_pii():
    kinds = {h.kind for h in scan_unit("電話０３－１２３４－５６７８　taro@example.com　〒１００－０００１　普通預金　口座番号０１２３４５６")}
    assert {"電話", "メール", "郵便番号", "口座"} <= kinds
    assert {h.kind for h in scan_unit("昭和５０年１月２日生")} == {"生年月日"}
    assert {h.kind for h in scan_unit("令和６年（ワ）第１２３４号　損害賠償請求事件")} == {"事件番号"}
    # 判例の事件番号は伏せない
    assert not [h for h in scan_unit("最高裁平成１９年（受）第１２３４号同２０年６月４日判決") if h.kind == "事件番号"]


def test_collector_labels_and_aliases():
    col = Collector(style="xy")
    for t in ["原告　山田太郎", "被告株式会社みどり建設（以下「みどり建設」という。）",
              "訴外佐々木健は、被告の従業員である。佐々木は、", "原告山田太郎は、"]:
        col.add(t, "t")
    rows = {c.original: c for c in col.finalize()}
    assert rows["山田太郎"].replacement == "Ｘ"
    assert rows["株式会社みどり建設"].replacement == "Ｙ社"
    assert rows["みどり建設"].replacement == "Ｙ社"          # 略称は同じ記号
    assert rows["佐々木"].replacement == rows["佐々木健"].replacement
    assert rows["山田太郎"].count == 2


def test_keep_list():
    col = Collector(keep=["佐藤一郎"])
    col.add("原告訴訟代理人弁護士　佐藤一郎", "t")
    rows = {c.original: c for c in col.finalize()}
    assert rows["佐藤一郎"].accept is False
