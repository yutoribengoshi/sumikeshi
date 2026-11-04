"""照合の回帰試験。書式辞書の匿名化と仮名化ゲートで実際に起きた穴を固定する。"""
from sumikeshi.matcher import Entry, Matcher


def sub(text, *pairs):
    m = Matcher([Entry(a, b, k) for a, b, k in pairs])
    return m.sub(text)[0]


def test_spaced_signature():
    # 記名欄は1字ずつ空ける。素の str.replace は当たらない
    assert sub("弁護士　山　田　太　郎", ("山田太郎", "Ａ", "人名")) == "弁護士　Ａ"
    assert sub("原告　山田　太郎（以下", ("山田太郎", "Ｘ", "人名")) == "原告　Ｘ（以下"


def test_longest_first_keeps_given_name_from_leaking():
    # 姓を先に置換すると「Ｘ　太郎」と名だけ残る。長い原文から当てる
    out = sub("鈴木　一郎は", ("鈴木", "Ｘ", "人名"), ("鈴木一郎", "Ｘ", "人名"))
    assert out == "Ｘは"


def test_variant_kanji_and_width():
    assert sub("髙橋花子様", ("高橋花子", "Ａ", "人名")) == "Ａ様"
    assert sub("山﨑一郎は", ("山崎一郎", "Ａ", "人名")) == "Ａは"
    assert sub("ＡＢＣ商事", ("ABC商事", "Ｂ社", "法人")) == "Ｂ社"


def test_short_names_respect_word_boundaries():
    e = ("山田", "Ｘ", "人名")
    assert sub("山田町に住む", e) == "山田町に住む"      # 地名の一部は壊さない
    assert sub("原告山田は", e) == "原告Ｘは"            # 役割語の後は当てる
    assert sub("山田弁護士が", e) == "Ｘ弁護士が"        # 肩書きの前は当てる
    assert sub("山田は、", e) == "Ｘは、"


def test_one_char_surname_needs_title():
    # 1字の語は、前後がひらがなでも語の境とはみなさない（「この森に」を伏せない）
    e = ("森", "Ａ", "人名")
    assert sub("この森に入る", e) == "この森に入る"
    assert sub("森弁護士に", e) == "Ａ弁護士に"


def test_katakana_word_boundary():
    e = ("ジョン", "Ａ", "人名")
    assert sub("ジョンソン氏", e) == "ジョンソン氏"
    assert sub("ジョン・スミス", e) == "Ａ・スミス"


def test_digits_with_any_separator():
    e = ("03-1234-5678", "［電話番号］", "電話")
    assert sub("電話０３（１２３４）５６７８", e) == "電話［電話番号］"
    assert sub("TEL 03 1234 5678。", e) == "TEL ［電話番号］。"
    assert sub("１２０３－１２３４－５６７８９", e) == "１２０３－１２３４－５６７８９"


def test_excel_dropped_leading_zero():
    assert sub("口座 0123456", ("123456", "○○", "口座")) == "口座 ○○"


def test_kuro_keeps_spacing():
    assert sub("山　田　太　郎", ("山田太郎", "■■■■", "人名")) == "■　■　■　■"


def test_zero_width_space_does_not_hide_name():
    assert sub("山​田太郎", ("山田太郎", "Ａ", "人名")) == "Ａ"
