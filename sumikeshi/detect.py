"""本文から伏せる候補を拾う（収集）。

拾うもの: 人名（役割語・敬称・肩書き・氏名欄・定義・記名欄・形態素）、代理人（弁護士名など）、法人、
住所（市区町村より後）、物件（地番・家屋番号）、電話・FAX、メール、郵便番号、口座、生年月日、
番号（個人番号・免許証など）、車両番号、事件番号、URL。

判例・条文・号証の中からは拾わない（〔ＮＥＸＸ事件〕を社名と取り違えない）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import lexicon as L
from . import sudachi as S
from .matcher import core

K, KA, HI, D, SP = L.KANJI, L.KATA, L.HIRA, L.DIGIT, L.SP


def spaced(word: str) -> str:
    """字間の空白を許すパターン（「原　　告」）。"""
    return f"{SP}*".join(re.escape(ch) for ch in word)


@dataclass
class Hit:
    kind: str
    start: int
    end: int
    text: str                     # 本文にあるままの形
    rule: str
    conf: str = "高"
    side: str = ""                # X / Y / ""
    replacement: str | None = None   # 決まった形で置き換えるもの（住所・電話など）
    entity: str | None = None     # 同じ記号に寄せる相手（姓・名・略称のとき、正式名）
    part: str = ""                # sei / mei / alias
    sei: str = ""                 # フルネームのとき、分かれば姓と名
    mei: str = ""


# ---------------------------------------------------------------- 全角→半角（位置を変えない）
_FOLD = {chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)}
_FOLD.update({"　": " ", "－": "-", "‐": "-", "−": "-", "―": "-", "‑": "-", "–": "-", "ー": "-"})


def fold(text: str) -> str:
    return "".join(_FOLD.get(ch, ch) for ch in text)


# ---------------------------------------------------------------- 保護（ここからは拾わない）
_COURT = (rf"(?:最高裁判所|最高裁|大審院|最大?[判決]|大[判決]|大連判|知財高[判決]|"
          rf"[{K}]{{1,4}}(?:高|地|家|簡)(?:裁)?(?:[{K}]{{1,4}}支)?[判決審])")
_ERA_DATE = (rf"(?:明治|大正|昭和|平成|令和|[明大昭平令MTSHR])\.?{SP}*[{D}元]{{1,2}}{SP}*[年・.．]"
             rf"{SP}*[{D}]{{1,2}}{SP}*[月・.．]{SP}*[{D}]{{1,2}}{SP}*日?")
_REPORTER = (r"(?:民集|刑集|集民|集刑|裁判集民事|裁判集刑事|判時|判タ|判例時報|判例タイムズ|金判|金法|"
             r"労判|労経速|家月|家判|訟月|高民集?|高刑集?|下民集?|下刑集?|行集|知財集|無体集|"
             r"ＬＥＸ／ＤＢ|LEX/DB|裁判所ウェブサイト|裁判所ＨＰ|裁判所HP|判例秘書|ウエストロー|Westlaw)")
_NUMK = f"[{D}一二三四五六七八九十百千]"
PROTECT = [
    ("判例", re.compile(rf"{_COURT}{SP}*[^\n。]{{0,30}}?{_ERA_DATE}"
                       rf"(?:[^\n。]{{0,24}}?{_REPORTER}[^\n。、）)]{{0,25}})?")),
    ("判例", re.compile(rf"[〔【]([^〔〕【】\n]{{1,40}}?事件)[〕】]")),
    ("条文", re.compile(rf"(?:[{K}]{{1,15}}法|同法|本法|[{K}]{{1,10}}(?:令|規則))第?{SP}*{_NUMK}+{SP}*条"
                       rf"(?:の{_NUMK}+)?(?:{SP}*第?{_NUMK}+{SP}*項)?(?:{SP}*第?{_NUMK}+{SP}*号)?")),
    ("号証", re.compile(rf"[甲乙丙丁](?:[Ａ-ＺA-Z])?第?{SP}*[{D}]+{SP}*(?:号証)?(?:の[{D}]+)?")),
]


def protected_spans(text: str) -> list[tuple[int, int, str]]:
    out = []
    for kind, pat in PROTECT:
        for m in pat.finditer(text):
            out.append((m.start(), m.end(), kind))
    return out


def _overlaps(s: int, e: int, spans) -> bool:
    return any(s < b and a < e for a, b, *_ in spans)


# ---------------------------------------------------------------- 決まった形の個人情報
_PHONE = re.compile(
    r"(?<![0-9])(?:\(0\d{1,4}\)\s?|0\d{1,4}(?:[-\s]{1,2}|\(|\)))\d{1,4}(?:[-\s]{1,2}|\)\s?)\d{3,4}(?![0-9])"
    r"|(?<![0-9])\+81[-\s]?\d{1,4}[-\s]\d{1,4}[-\s]\d{3,4}(?![0-9])"
    r"|(?<![0-9])0[5789]0\d{8}(?![0-9])|(?<![0-9])0\d{9}(?![0-9])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+")
_POSTAL = re.compile(r"(?:〒|郵便番号[:\s]*)\s?\d{3}\s?-?\s?\d{4}(?![0-9])")
_ACCOUNT = re.compile(
    r"(?P<label>(?:普通|当座|貯蓄|総合)(?:預金)?(?:口座)?|口座番号|口座|預金番号)[\s:]*"
    r"(?:No\.?|番号|口座番号)?[\s:]*(?P<num>\d{5,8})(?![0-9])")
_YUCHO = re.compile(r"記号[\s:]*\d{5}[\s\-]*番号[\s:]*\d{6,8}(?![0-9])")
_DATE = (r"(?:(?:明治|大正|昭和|平成|令和|[MTSHR]\.?)\s?(?:\d{1,2}|元)|\d{4})\s?[年./\-]\s?"
         r"\d{1,2}\s?[月./\-]\s?\d{1,2}\s?日?")
_DOB = [re.compile(rf"(?P<d>{_DATE})\s?(?P<suf>生まれ|生)(?![活産命徒])"),
        re.compile(rf"(?P<lab>生年月日[\s:]*)(?P<d>{_DATE})")]
_NUMLABEL = re.compile(
    r"(?P<label>個人番号|マイナンバー|運転免許証番号|免許証番号|免許証の番号|被保険者番号|保険者番号|"
    r"基礎年金番号|旅券番号|パスポート番号|在留カード番号|法人番号|会社法人等番号|登録番号|社員番号|"
    r"患者番号|カルテ番号|受付番号|証券番号|契約番号|顧客番号|会員番号)[\s:第は]*"
    r"(?P<num>[A-Z]{0,2}\d[\d\-\s]{3,18}\d)(?![0-9])")
_TWELVE = re.compile(r"(?<![0-9])\d{4}[-\s]?\d{4}[-\s]?\d{4}(?![0-9])")
_VEHICLE = re.compile(rf"(?P<region>[{K}]{{1,8}}){SP}*[{D}]{{1,3}}{SP}*[{HI}]{SP}*[{D}・･.]{{1,2}}{SP}*[{L.HYPHENS}]{SP}*[{D}]{{2}}(?![{D}])")
_VEHICLE_PREFIX = ("普通乗用自動車", "軽自動車", "自動車", "原告車", "被告車", "車両", "登録番号", "番号", "車")
_CASENO = re.compile(r"(?P<era>令和|平成|昭和)\s?(?P<y>\d{1,2}|元)\s?年\s?\(\s?(?P<type>[^\s()]{1,6})\s?\)\s?第?\s?(?P<n>\d{1,7})\s?号")
_CASENO_CITE_BEFORE = re.compile(r"(?:最高裁|最判|最決|最大|大判|大決|高判|高決|地判|地決|家審|簡判|知財高)[^\n]{0,12}$")
_CASENO_CITE_AFTER = re.compile(r"^[^\n。]{0,25}(?:判決|決定|判例|民集|刑集|判時|判タ)")


def _digits_to_maru(s: str) -> str:
    return re.sub(f"[{D}]", "○", s)


def _pii(text: str, ft: str) -> list[Hit]:
    hits: list[Hit] = []

    def add(kind, s, e, rule, rep, conf="高"):
        hits.append(Hit(kind, s, e, text[s:e], rule, conf, replacement=rep))

    for m in _EMAIL.finditer(ft):
        add("メール", m.start(), m.end(), "メールアドレスの形", "［メールアドレス］")
    for m in _URL.finditer(ft):
        e = m.end()
        while e > m.start() and ft[e - 1] in ".,;:)":
            e -= 1
        add("URL", m.start(), e, "URLの形", "［URL］")
    for m in _POSTAL.finditer(ft):
        add("郵便番号", m.start(), m.end(), "〒の後の7桁", _digits_to_maru(text[m.start():m.end()]))
    for m in _PHONE.finditer(ft):
        if _overlaps(m.start(), m.end(), [(h.start, h.end) for h in hits]):
            continue
        add("電話", m.start(), m.end(), "電話番号の形", "［電話番号］")
    for m in _ACCOUNT.finditer(ft):
        s, e = m.start("num"), m.end("num")
        add("口座", m.start(), e, "口座の種類の後の数字",
            text[m.start():s] + "○" * (e - s))
    for m in _YUCHO.finditer(ft):
        add("口座", m.start(), m.end(), "ゆうちょの記号番号", _digits_to_maru(text[m.start():m.end()]))
    for pat in _DOB:
        for m in pat.finditer(ft):
            lab = m.groupdict().get("lab") or ""
            suf = m.groupdict().get("suf") or ""
            add("生年月日", m.start(), m.end(), "生年月日の形", f"{lab}○○年○月○日{suf}")
    for m in _NUMLABEL.finditer(ft):
        add("番号", m.start(), m.end(), f"「{m.group('label')}」の後の数字",
            text[m.start():m.start('num')] + "［番号］")
    for m in _TWELVE.finditer(ft):
        if not _overlaps(m.start(), m.end(), [(h.start, h.end) for h in hits]):
            add("番号", m.start(), m.end(), "12桁の数字（個人番号の形）", "［番号］", "中")
    for m in _VEHICLE.finditer(text):
        s = m.start()
        region = m.group("region")
        for p in _VEHICLE_PREFIX:
            if region.startswith(p) and len(region) > len(p):
                s += len(p)
                break
        add("車両番号", s, m.end(), "自動車登録番号の形", "［車両番号］")
    for m in _CASENO.finditer(ft):
        if _CASENO_CITE_BEFORE.search(ft[max(0, m.start() - 20):m.start()]) or \
                _CASENO_CITE_AFTER.search(ft[m.end():m.end() + 30]):
            continue
        add("事件番号", m.start(), m.end(), "事件番号の形",
            f"{m.group('era')}○年（{m.group('type')}）第○○号")
    return hits


# ---------------------------------------------------------------- 住所
_PREF = rf"(?:東京都|北海道|京都府|大阪府|[{K}]{{2,3}}県)"
_MUNI = (rf"(?:[{K}]{{1,5}}?市{SP}*[{K}]{{1,4}}?区|[{K}{KA}]{{1,5}}?郡{SP}*[{K}{KA}]{{1,5}}?[町村]"
         rf"|[{K}{KA}ヶケ]{{1,6}}?[市区町村])")
_ANUM = f"[{D}一二三四五六七八九十〇百千]+"
_AUNIT = rf"(?:丁目|番地|番|号|の|[{L.HYPHENS}])"
_ABODY = (rf"[{K}{KA}{HI}ヶケ]{{0,10}}?{SP}*(?:{_ANUM}{SP}*{_AUNIT}{SP}*)+(?:{_ANUM})?"
          rf"(?:{SP}*(?:番地|番|号))?")
_BWORD = r"(?:ビル|マンション|ハイツ|コーポ|荘|レジデンス|タワー|棟|号館|アパート|ハウス|メゾン|パレス|コート|ヒルズ|ハイム|プラザ)"
_ROOM = rf"(?:{_ANUM}{SP}*(?:号室|号|階)|[{D}]{{1,2}}[FＦ])"
_ABLDG = rf"(?:{SP}*(?:[^\s、。，,「」（）()]{{0,25}}?{_BWORD}(?:{SP}*{_ROOM})?|{_ROOM}))?"
# 都道府県から書いた住所を先に拾い、残りで市区町村から書いた住所を拾う
_ADDR_P = re.compile(rf"(?P<head>{_PREF}{SP}*{_MUNI})(?P<body>{_ABODY}){_ABLDG}")
_ADDR_N = re.compile(rf"(?P<head>{_MUNI})(?P<body>{_ABODY}){_ABLDG}")
_PREF_RE = re.compile(_PREF)
_ADDR_LABEL = re.compile(
    rf"^{SP}*(?:〒{SP}*[{D}]{{3}}{SP}*[{L.HYPHENS}]?{SP}*[{D}]{{4}}{SP}*)?(?:住{SP}*所|住{SP}*居|居{SP}*所|所{SP}*在{SP}*地|本店所在地|本{SP}*店|主たる事務所|事務所|送達場所"
    rf"|勤務先|現住所|最後の住所|本{SP}*籍|住民票上の住所|就業場所|肩書地)?[{L.SPACE_CLASS}:：]*$")
_HEAD_STRIP = ("本店所在地", "所在地", "現住所", "住所", "住居", "居所", "本店", "本籍", "自宅", "肩書地",
               "被告", "原告", "申立人", "相手方", "債権者", "債務者", "所在", "地番")
_PROPERTY = re.compile(rf"(?P<label>地{SP}*番|家{SP}*屋{SP}*番{SP}*号)[{L.SPACE_CLASS}:：]+(?P<val>[^\n]+?){SP}*$")


def _trim_head(text: str, hs: int, he: int) -> int:
    """市区町村から書いた住所の頭に付いた前置き（「住所」「被告」など）を外した開始位置。"""
    c = core(text[hs:he])
    for w in _HEAD_STRIP:
        if c.startswith(w) and len(c) > len(w) + 1:
            i = hs
            while i < he and len(core(text[hs:i])) < len(w):
                i += 1
            return _trim_head(text, i, he)
    # 市区町村名は長くても6字ほど。それより長ければ前に別の語が付いている
    if len(c) > 7 and "市" not in c[:-1] and "郡" not in c:
        while len(core(text[hs:he])) > 6:
            hs += 1
    return hs


def _address(text: str, mode: str) -> list[Hit]:
    hits: list[Hit] = []
    taken: list[tuple[int, int]] = []
    matches = list(_ADDR_P.finditer(text))
    taken = [m.span() for m in matches]
    matches += [m for m in _ADDR_N.finditer(text) if not _overlaps(m.start(), m.end(), taken)]
    for m in matches:
        hs, he = m.start("head"), m.end("head")
        if m.re is _ADDR_N:
            hs = _trim_head(text, hs, he)
        head = text[hs:he]
        end = m.end()
        line_start = text.rfind("\n", 0, hs) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        # 「住所　東京都…」の行は、建物名まで行末を伏せる
        if _ADDR_LABEL.match(text[line_start:hs]) and line_end - end <= 30 and "。" not in text[end:line_end]:
            end = line_end
            while end > he and text[end - 1] in L.SPACE_SET:
                end -= 1
        if mode == "pref":
            pm = _PREF_RE.match(head)
            keep = head[:pm.end()] if pm else ""
        elif mode == "all":
            keep = ""
        else:
            keep = head
        hits.append(Hit("住所", hs, end, text[hs:end], "住所の形（市区町村より後）",
                        replacement=keep + "○○"))
    for m in _PROPERTY.finditer(text):
        hits.append(Hit("物件", m.start("val"), m.end("val"), m.group("val"),
                        f"「{core(m.group('label'))}」の欄", replacement="○○"))
    return hits


# ---------------------------------------------------------------- 法人
_FORMS = sorted(L.CORP_FORMS, key=len, reverse=True)
_FORM_RE = re.compile("|".join(map(re.escape, _FORMS)))
_CO_CH = f"{K}{KA}{HI}{L.ALNUM}&＆・･．.ー\\-"
_CO_END = f"[{K}{KA}{L.ALNUM}]"
_CO_STOPWORDS = ("代表取締役", "代表者", "代表", "御中", "本店", "支店", "営業所", "工場", "取締役",
                 "社長", "会長", "殿", "様", "宛", "及び", "並びに", "外", "は", "が", "の", "を", "に",
                 "へ", "と", "で", "も", "や", "から", "より")
_CO_PRE = re.compile(
    rf"(?P<form>{_FORM_RE.pattern}){SP}?(?P<name>[{_CO_CH}]{{0,24}}?{_CO_END})"
    rf"(?=$|[^{_CO_CH}]|{'|'.join(_CO_STOPWORDS)})")
_SHOP_RE = re.compile("|".join(map(re.escape, L.SHOP_SUFFIXES)))
_PARTICLES = set("のとはがをにへでもや")
_CO_CH_RE = re.compile(f"[{_CO_CH}]")
_EN_CORP = re.compile(
    r"(?<![A-Za-z])(?P<name>[A-Z][A-Za-z0-9&'.\-]*(?:[ ,]{1,2}(?:[A-Z][A-Za-z0-9&'.\-]*|&|and|of))"
    r"{0,5}),?\s+(?P<form>Co\.,?\s?Ltd\.?|Co\.|Inc\.?|Corp(?:oration)?\.?|LLC|L\.L\.C\.|Ltd\.?|Limited|GmbH"
    r"|AG|S\.A\.|PLC|Pte\.?\s?Ltd\.?|LLP|K\.K\.)")
_ROLE_WORDS = sorted({w for w, _, _ in L.ROLES} | L.CORP_PREFIX_STOP, key=len, reverse=True)
_CONJ = ["及び", "並びに", "又は", "若しくは", "または", "および"]
_NOT_NOUN = ("動詞", "助詞", "助動詞", "形容詞", "副詞", "連体詞", "接続詞", "感動詞", "形状詞")


def _walk_back(text: str, pos: int, limit: int = 20) -> int:
    """pos の直前から社名らしい字を遡り、社名の始まりを返す。役割語・助詞の手前で止まる。"""
    i = pos
    while i > 0 and pos - i < limit:
        ch = text[i - 1]
        if not _CO_CH_RE.match(ch):
            break
        before = core(text[max(0, i - 12):i])
        if i < pos and any(before.endswith(w) for w in _ROLE_WORDS + _CONJ):
            break
        if ch in _PARTICLES:
            prev = text[i - 2] if i >= 2 else ""
            nxt = text[i] if i < len(text) else ""
            # ひらがなの名の中の字（「ことぶき」の「と」）でなければ、助詞の手前で止まる
            if not (re.match(f"[{HI}]", prev) and re.match(f"[{HI}]", nxt)):
                break
        i -= 1
    # 頭に残った役割語・助詞を外す
    while i < pos:
        rest = core(text[i:pos])
        w = next((w for w in _ROLE_WORDS if rest.startswith(w) and len(rest) > len(w)), None)
        if w:
            i += len(w)
            continue
        if text[i] in _PARTICLES or text[i] in "・･．.ー-":
            i += 1
            continue
        break
    return i


def _side_before(text: str, pos: int) -> str:
    """直前の役割語から当事者の側を返す（「被告株式会社…」→ Y）。"""
    before = core(text[max(0, pos - 10):pos])
    for w in sorted(L.ROLE_INFO, key=len, reverse=True):
        if before.endswith(w):
            return L.ROLE_INFO[w][1]
    return ""


def _company_name_ok(name: str, strict: bool = False) -> bool:
    """法人格・屋号の前後の名が社名らしいか。strict=True は法人格の無い屋号（証拠が弱い）。"""
    c = core(name)
    if not c or c in L.STOP_WORDS or c in L.CORP_PREFIX_STOP or L.HARD_TERMS_RE.fullmatch(c):
        return False
    if c.endswith(("法", "令", "規則", "条例", "規程")):          # 「独立行政法人…センター法」は法令の名
        return False
    if re.search(f"[{HI}]", c):
        # ひらがなを含む名は、動詞・助詞が混じっていたら文の一部（「用いて開発」）
        if S.available():
            if any(t[1][0] in _NOT_NOUN for t in S.tokens(c)):
                return False
        else:
            # Sudachi が無いとき: ひらがなの塊が送り仮名・助詞らしければ文の一部とみなす（「みどり建設」は可）
            for run in re.findall(f"[{HI}]+", c):
                if len(run) < (3 if strict else 2) or run[-1] in "てでにをがはのともたるくいしかよ":
                    return False
    if re.search(f"[{KA}{HI}{L.ALNUM}]", c):
        return not (re.fullmatch(f"[{KA}ー・]+", c) and L.kata_generic(c))
    if not strict:
        return True
    # 漢字だけの屋号: 固有名詞を含むこと（「道路建設」「公共工事」は社名ではない）
    if S.available():
        toks = S.tokens(c)
        return any(t[1][1] == "固有名詞" for t in toks) and not any(t[1][0] in _NOT_NOUN for t in toks)
    return len(c) >= 2 and c[-1] in L.SURNAME_END


def _company(text: str) -> list[Hit]:
    hits: list[Hit] = []
    taken: list[tuple[int, int]] = []
    for m in _FORM_RE.finditer(text):
        fs, fe = m.span()
        if _overlaps(fs, fe, taken):
            continue
        st = _walk_back(text, fs)
        if st < fs and not re.match(f"[{HI}]", text[fs - 1]) and _company_name_ok(text[st:fs]):
            hits.append(Hit("法人", st, fe, text[st:fe], "社名＋法人格", side=_side_before(text, st)))
            taken.append((st, fe))
            continue
        mp = _CO_PRE.match(text, fs)
        if mp and _company_name_ok(mp.group("name")):
            hits.append(Hit("法人", fs, mp.end(), text[fs:mp.end()], "法人格＋社名", side=_side_before(text, fs)))
            taken.append((fs, mp.end()))
    for m in _SHOP_RE.finditer(text):
        ss, se = m.span()
        if _overlaps(ss, se, taken):
            continue
        nxt = text[se:se + 1]
        if nxt and re.match(f"[{K}]", nxt) and not _FORM_RE.match(text, se) \
                and not any(text.startswith(w, se) for w in _CO_STOPWORDS):
            continue
        st = _walk_back(text, ss, limit=12)
        if st < ss and _company_name_ok(text[st:ss], strict=True):
            hits.append(Hit("法人", st, se, text[st:se], f"屋号（〜{m.group(0)}）", conf="中",
                            side=_side_before(text, st)))
            taken.append((st, se))
    ft = fold(text)
    for m in _EN_CORP.finditer(ft):
        if not _overlaps(m.start(), m.end(), taken):
            hits.append(Hit("法人", m.start(), m.end(), text[m.start():m.end()], "英文の社名"))
    return hits


# ---------------------------------------------------------------- 人名
_KCH = rf"(?:(?!及び|並びに|又は|若しくは|外{SP}*[{D}一二三四五六七八九十])[{K}])"
_NAME_RUN = rf"{_KCH}(?:{SP}*{_KCH}){{1,11}}"
_ROLE_ALT = "|".join(spaced(w) for w in sorted({w for w, _, _ in L.ROLES}, key=len, reverse=True))
_TITLE_ALT = "|".join(map(re.escape, sorted(L.TITLE_SUFFIXES, key=len, reverse=True)))
_P_ROLE = re.compile(rf"(?P<role>{_ROLE_ALT})(?:{SP}|[:：])*(?P<run>{_NAME_RUN})")
# 「代表取締役は、鈴木一郎である」「原告の代理人は山田太郎弁護士であり」
_P_ROLE_IS = re.compile(rf"(?P<role>{_ROLE_ALT}){SP}*(?:は|が)[、，]?{SP}*(?P<run>{_NAME_RUN})"
                        rf"(?=である|であり|であった|です|でした|弁護士|氏|様|さん|{SP}*[、，。）)]|{SP}*$)")
_P_ROLE1 = re.compile(rf"(?<![{K}])(?P<role>[{''.join(L.ONE_CHAR_ROLES)}])(?P<run>{_NAME_RUN})")
_P_ROLE_KATA = re.compile(rf"(?P<role>{_ROLE_ALT})(?:{SP}|[:：])*(?P<kata>[{KA}]{{2,}}(?:[・＝=][{KA}]{{1,}})*)")
_P_ROLE_LATIN = re.compile(
    rf"(?P<role>{_ROLE_ALT})(?:{SP}|[:：])*(?P<lat>[A-ZＡ-Ｚ][a-zａ-ｚA-ZＡ-Ｚ'’\-]+"
    rf"(?:[ 　][A-ZＡ-Ｚ][a-zａ-ｚA-ZＡ-Ｚ'’\-]+){{1,3}})")
_P_HON = re.compile(rf"(?<![{K}])(?P<run>[{K}](?:{SP}?[{K}]){{0,11}})(?P<gap>{SP}*)(?P<hon>{L.HONORIFIC_RE}|{_TITLE_ALT})")
_P_MIXED_HON = re.compile(rf"(?<![{K}{HI}])(?P<name>[{K}]{{1,3}}[{HI}]{{1,4}}[{K}]{{0,2}})(?={SP}*(?:様|氏|さん|殿))")
_P_FIELD = re.compile(rf"(?:氏{SP}*名|お名前|名{SP}*前)(?:{SP}|[:：])*(?P<run>{_NAME_RUN})")
_P_DEF = re.compile(r"[（(]以下[、，]?[「『](?P<alias>[^」』\n]{1,20})[」』]")
_KN = rf"(?:(?![印㊞])[{K}])"
# 記名だけの行（「　　山　田　太　郎　印」「島 田　　美喜 子」）。1〜4字の塊が空白で2〜6個並ぶ
_P_SIGN = re.compile(rf"^{SP}*(?P<run>{_KN}{{1,4}}(?:{SP}+{_KN}{{1,4}}){{1,5}}){SP}*(?:[印㊞]|（印）|\(印\))?{SP}*$")
_P_LIST_NEXT = re.compile(rf"(?:{SP}*(?:及び|並びに|、|，|・){SP}*)(?P<run>{_NAME_RUN})")
_PREFIXES = sorted(L.NAME_PREFIXES, key=len, reverse=True)
_NONNAME_RE = re.compile("|".join(map(re.escape, sorted(
    {w for w in set(L.HARD_TERMS) | set(L.NAME_PREFIXES) if len(w) >= 2}, key=len, reverse=True))))


def _name_ok(name: str, strong: bool) -> bool:
    if not 2 <= len(name) <= 6 or name in L.STOP_WORDS:
        return False
    if not re.fullmatch(f"[{K}]+", name) or L.HARD_TERMS_RE.search(name):
        return False
    sp = S.person(name)
    if sp is True:
        return True
    if sp is False:
        return False
    last = name[-1]
    if len(name) == 2:
        return last in L.SURNAME_END or last in L.NAME_END
    if last in L.WORD_END and last not in L.NAME_END and last not in L.SURNAME_END:
        return False
    # Sudachi があって未知語のとき（「乙組組織」「方玄関」）は、名の末字らしい字で終わるものだけ
    if S.available() or not strong:
        return last in L.NAME_END
    return True


class _Run:
    """漢字の連なり（字間の空白込み）から、前置きの役割語を剥がし、名前の部分を切り出す。"""

    def __init__(self, text: str, rs: int, re_: int):
        self.chars = [(i, ch) for i, ch in enumerate(text[rs:re_], rs) if ch not in L.SPACE_SET]
        self.gaps = [False] + [self.chars[k][0] - self.chars[k - 1][0] > 1 for k in range(1, len(self.chars))]
        self.s = "".join(ch for _, ch in self.chars)

    def span(self, a: int, b: int) -> tuple[int, int]:
        """連なりの中の字の番号 [a, b) を本文の位置に直す。"""
        return self.chars[a][0], self.chars[b - 1][0] + 1

    def analyze(self, from_right: bool = False):
        """(開始位置, 終了位置, 名前, 姓, 名, 剥がした役割語の列) か None。

        from_right=True は敬称の前の連なり（名前は右端にある）。
        """
        s, chars, gaps = self.s, self.chars, self.gaps
        k, roles = 0, []
        changed = True
        while changed:
            changed = False
            for w in _PREFIXES:
                if s.startswith(w, k) and len(s) - (k + len(w)) >= 1:
                    roles.append(w)
                    k += len(w)
                    changed = True
                    break
        if from_right:
            # 「本件建物所有者山田太郎様」: 名前でない語の最後の出現より後ろだけを見る
            last = max((m.end() for m in _NONNAME_RE.finditer(s, k) if m.end() < len(s) - 1), default=k)
            if last > k:
                roles += [m.group(0) for m in _NONNAME_RE.finditer(s, k, last) if m.group(0) in L.ROLE_INFO]
                k = last
        toks: list[list[int]] = []
        for idx in range(k, len(chars)):
            if idx == k or gaps[idx]:
                toks.append([idx, idx + 1])
            else:
                toks[-1][1] = idx + 1
        # 署名欄の「同　　山田太郎」（上の行と同じ肩書き）の「同」は名前ではない
        if len(toks) > 1 and s[toks[0][0]:toks[0][1]] in ("同", "右", "外"):
            toks = toks[1:]
        if not toks:
            return None
        lens = [b - a for a, b in toks]
        sei = mei = ""
        if from_right:
            if len(toks) >= 2 and len(toks) <= 6 and all(n == 1 for n in lens):
                a, b = toks[0][0], toks[-1][1]          # 山　田　太　郎　様
            elif len(toks) >= 2 and lens[-2] <= 3 and lens[-1] <= 4:
                a, b = toks[-2][0], toks[-1][1]         # 山田　太郎　様
                sei, mei = s[toks[-2][0]:toks[-2][1]], s[toks[-1][0]:toks[-1][1]]
            else:
                a, b = toks[-1]
        elif len(toks) >= 2 and sum(1 for n in lens if n > 1) <= 1 and lens[0] <= 2:
            # 1字ずつ空けた記名（空き方が不揃いでも）: 6字までつなぐ
            n, total = 0, 0
            while n < len(toks) and total + lens[n] <= 6 and lens[n] <= 3 \
                    and s[toks[n][0]:toks[n][1]] not in L.STOP_WORDS:
                total += lens[n]
                n += 1
            a, b = toks[0][0], toks[max(n, 1) - 1][1]   # 原告　山　田　太　郎
        elif len(toks) == 2 and lens[0] + lens[1] <= 6 and s[toks[1][0]:toks[1][1]] not in L.STOP_WORDS:
            a, b = toks[0][0], toks[1][1]               # 原告　山田　太郎／裁判官　柵木澄　子
            if lens[0] <= 3:
                sei, mei = s[toks[0][0]:toks[0][1]], s[toks[1][0]:toks[1][1]]
        elif lens[0] >= 3:
            a, b = toks[0]                              # 原告　山田太郎　東京都…
        elif len(toks) >= 2 and lens[1] <= 4:
            a, b = toks[0][0], toks[1][1]               # 原告　山田　太郎　東京都…
            sei, mei = s[toks[0][0]:toks[0][1]], s[toks[1][0]:toks[1][1]]
        else:
            a, b = toks[0]
        name = s[a:b]
        mt = L.HARD_TERMS_RE.search(name)
        if mt:
            if mt.start() < 2:
                return None
            b = a + mt.start()                          # 山田太郎作成 → 山田太郎
            name = s[a:b]
            if sei and len(sei) >= len(name):
                sei = mei = ""
            elif sei:
                mei = name[len(sei):]
        for t in sorted(L.TITLE_SUFFIXES, key=len, reverse=True):
            if name.endswith(t) and len(name) - len(t) >= 2:
                b -= len(t)
                name = s[a:b]
                break
        if not name:
            return None
        if len(name) > 6 and S.available():
            # 「東京都新宿区山田太郎様」のように前に別の語が付いたまま: 右端の人名だけ取る
            spans = [sp for sp in S.person_spans(name) if sp[1] == len(name)]
            if not spans:
                return None
            a += spans[-1][0]
            name = s[a:b]
            sei = mei = ""
        st, en = self.span(a, b)
        return st, en, name, sei, mei, roles


def _role_of(roles: list[str]) -> tuple[str, str]:
    kind, side = "人名", ""
    for w in roles:
        if w in L.ROLE_INFO:
            kind, side = L.ROLE_INFO[w]
    return kind, side


def _person_hit(text, s, e, name, sei, mei, kind, side, rule, conf="高") -> Hit:
    return Hit(kind, s, e, text[s:e], rule, conf, side=side, sei=sei, mei=mei)


def _persons(text: str) -> list[Hit]:
    hits: list[Hit] = []

    def from_run(rs, re_, role, rule, strong=True, from_right=False):
        run = _Run(text, rs, re_)
        res = run.analyze(from_right=from_right)
        if not res:
            return None
        s, e, name, sei, mei, roles = res
        kind, side = _role_of(([role] if role else []) + roles)
        if not _name_ok(name, strong):
            return None
        h = _person_hit(text, s, e, name, sei, mei, kind, side, rule, "高" if strong else "中")
        hits.append(h)
        return h

    for m in _P_ROLE.finditer(text):
        role = core(m.group("role"))
        h = from_run(m.start("run"), m.end("run"), role, f"「{role}」の後")
        # 「原告山田太郎及び山田花子」の2人目以降
        pos = h.end if h else None
        while pos is not None:
            mn = _P_LIST_NEXT.match(text, pos)
            if not mn:
                break
            h2 = from_run(mn.start("run"), mn.end("run"), role, f"「{role}」の後（並記）", strong=False)
            pos = h2.end if h2 else None
    for m in _P_ROLE_IS.finditer(text):
        role = core(m.group("role"))
        from_run(m.start("run"), m.end("run"), role, f"「{role}は」の後")
    for m in _P_ROLE1.finditer(text):
        from_run(m.start("run"), m.end("run"), "", f"「{m.group('role')}」の後", strong=False)
    for m in _P_FIELD.finditer(text):
        from_run(m.start("run"), m.end("run"), "", "氏名の欄")
    for m in _P_HON.finditer(text):
        hon = m.group("hon")
        run = _Run(text, m.start("run"), m.end("run"))
        res = run.analyze(from_right=True)
        if not res:
            continue
        s, e, name, sei, mei, roles = res
        kind, side = _role_of(roles)
        if hon in ("弁護士", "弁理士"):
            kind = "代理人"
        if len(name) == 1:
            # 1字の姓＋敬称・肩書き（「森弁護士」「林様」）は肩書きごと原文にする
            if name not in L.ONE_CHAR_SURNAMES or name in L.STOP_WORDS:
                continue
            hits.append(Hit(kind, s, m.end(), text[s:m.end()], f"1字の姓＋「{hon}」", "中", side=side))
            continue
        if not _name_ok(name, strong=True):
            continue
        hits.append(_person_hit(text, s, e, name, sei, mei, kind, side, f"「{hon}」の前"))
    for m in _P_MIXED_HON.finditer(text):
        nm = m.group("name")
        if nm[-1] not in L.SAMA_COMPOUND and S.person(nm) is True:
            hits.append(Hit("人名", m.start(), m.end(), nm, "かな混じりの名＋敬称", "中"))
    for m in _P_ROLE_KATA.finditer(text):
        kt = m.group("kata")
        if len(kt.replace("・", "")) >= 2 and not L.kata_generic(kt) and not _FORM_RE.match(text, m.end("kata")):
            kind, side = _role_of([core(m.group("role"))])
            hits.append(Hit(kind, m.start("kata"), m.end("kata"), kt, f"「{core(m.group('role'))}」の後（カナ）", "中", side=side))
    for m in _P_ROLE_LATIN.finditer(text):
        kind, side = _role_of([core(m.group("role"))])
        hits.append(Hit(kind, m.start("lat"), m.end("lat"), m.group("lat"), "役割語の後（英字）", "中", side=side))
    ms = _P_SIGN.match(text)
    if ms:
        from_run(ms.start("run"), ms.end("run"), "", "記名の行", strong=bool(re.search(SP, ms.group("run"))))
    return hits


_P_TAIL_NAME = re.compile(rf"(?P<run>[{K}](?:{SP}?[{K}]){{0,11}})(?:{SP}*{L.HONORIFIC_RE})?{SP}*$")


def _definitions(text: str, others: list[Hit]) -> list[Hit]:
    """「（以下「原告」という。）」で当事者の側を、「（以下「みどり建設」という。）」で略称を拾う。"""
    hits: list[Hit] = []
    for m in _P_DEF.finditer(text):
        alias = core(m.group("alias"))
        st = m.start()
        if len(alias) < 2:
            continue
        # 直前の正式名: 社名なら遡って、人名なら敬称の前の漢字の連なりから
        cs = _walk_back(text, st, limit=30)
        tstart, tcore, kind = None, "", ""
        if cs < st and (_FORM_RE.search(text[cs:st]) or _SHOP_RE.search(text[cs:st])):
            tstart, tcore, kind = cs, core(text[cs:st]), "法人"
        else:
            mt = _P_TAIL_NAME.search(text, max(0, st - 40), st)
            if mt:
                res = _Run(text, mt.start("run"), mt.end("run")).analyze(from_right=True)
                if res and _name_ok(res[2], strong=True):
                    tstart, tcore, kind = res[0], res[2], "人名"
        if tstart is None:
            continue
        base = next((h for h in others if core(h.text) == tcore), None)
        role = next((w for w in sorted(L.ROLE_INFO, key=len, reverse=True) if alias.startswith(w)), None)
        if role:
            side = L.ROLE_INFO[role][1]
            if base is not None:
                base.side = base.side or side
            else:
                end = tstart
                while end < st and core(text[tstart:end]) != tcore:
                    end += 1
                hits.append(Hit(kind, tstart, end, text[tstart:end], f"定義（以下「{alias}」）", side=side))
        elif alias != tcore and alias in tcore and alias not in L.STOP_WORDS:
            hits.append(Hit(base.kind if base else kind, m.start("alias"), m.end("alias"), m.group("alias"),
                            f"略称（{tcore} の）", entity=tcore, part="alias"))
    return hits


_NUMERAL_KANJI = set("一二三四五六七八九十百千万")


def _sudachi_sweep(text: str, taken: list[tuple[int, int]]) -> list[Hit]:
    hits: list[Hit] = []
    if not S.available():
        return hits
    kanji = re.compile(f"[{K}]")
    for s, e, kind in S.person_spans(text):
        if kind == "full":
            # 「今村玲｜英子」のように名が途中で切れていたら、後ろの漢字まで延ばす（6字まで）
            while e < len(text) and e - s < 6 and kanji.match(text[e]) \
                    and not any(text.startswith(w, e) for w in L.TITLE_SUFFIXES + L.HONORIFICS):
                e += 1
        surf = text[s:e]
        if len(surf) < 2 or surf in L.STOP_WORDS or _overlaps(s, e, taken):
            continue
        if not re.fullmatch(f"[{K}]+", surf) or L.HARD_TERMS_RE.search(surf):
            continue
        before, after = text[s - 1:s], text[e:e + 1]
        if kind == "full":
            if _name_ok(surf, strong=False):
                hits.append(Hit("人名", s, e, surf, "形態素（姓名）", "中"))
            continue
        # 姓だけ・名だけ: 数字の字で始まる語（「一又は」）、前後が漢字の語の一部は捨てる
        if surf[0] in _NUMERAL_KANJI or (after and kanji.match(after)
                                         and not any(text.startswith(w, e) for w in L.TITLE_SUFFIXES)):
            continue
        if before and kanji.match(before) and not any(core(text[max(0, s - 8):s]).endswith(w) for w in _PREFIXES):
            continue
        if kind == "sei":
            hits.append(Hit("人名", s, e, surf, "形態素（姓だけ）", "低", part="sei"))
        else:
            hits.append(Hit("人名", s, e, surf, "形態素（名だけ）", "低", part="mei"))
    return hits


# ---------------------------------------------------------------- まとめ
def scan_unit(text: str, address_mode: str = "city") -> list[Hit]:
    """1段落（1行）分のテキストから候補を拾う。"""
    if not text or not text.strip():
        return []
    prot = protected_spans(text)
    ft = fold(text)
    fixed = [h for h in _pii(text, ft) if not _overlaps(h.start, h.end, prot)]
    addr = [h for h in _address(text, address_mode) if not _overlaps(h.start, h.end, prot)
            and not _overlaps(h.start, h.end, [(x.start, x.end) for x in fixed])]
    taken = [(h.start, h.end) for h in fixed + addr]
    comp = [h for h in _company(text) if not _overlaps(h.start, h.end, prot + taken)]
    taken += [(h.start, h.end) for h in comp]
    pers = [h for h in _persons(text) if not _overlaps(h.start, h.end, prot + taken)]
    defs = _definitions(text, comp + pers)
    taken += [(h.start, h.end) for h in pers + defs]
    # 住所の頭（残す市区町村）や裁判所の名から「中野」「新宿」を人名として拾わない
    court_like = [(m.start(), m.end()) for m in re.finditer(rf"[{K}]{{1,6}}(?:裁判所|法務局|検察庁|警察署|区役所|市役所)", text)]
    sweep = _sudachi_sweep(text, prot + taken + court_like)
    return fixed + addr + comp + pers + defs + sweep
