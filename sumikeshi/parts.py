"""Office の文書に入っている XML 以外の部品の扱い。

- 画像（JPEG・PNG）の付帯情報（EXIF・XMP・IPTC・コメント・PNG の文字チャンク）を取り除く。
  撮影者・作成者・撮影場所が残るため。絵そのものは変えない
- 埋め込みの文書（グラフの元データの Excel など。zip の形の部品）は、中の XML を同じ規則で伏せる
- 中を伏せられない部品（EMF・WMF・OLE など）に原文がそのまま残っていないかを、
  UTF-8・UTF-16・Shift_JIS のバイト列で探す（見つかれば検査で不合格にする）
"""
from __future__ import annotations

import io
import re
import zipfile

ZIP_MAGIC = b"PK\x03\x04"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_XML_HEAD = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<")


def looks_xml(name: str, data: bytes) -> bool:
    if name.lower().endswith((".xml", ".rels", ".vml", ".svg")):
        return True
    return bool(_XML_HEAD.match(data[:64])) and b"\x00" not in data[:512]


def is_zip(data: bytes) -> bool:
    return data[:4] == ZIP_MAGIC


def strip_jpeg(data: bytes) -> bytes:
    """JPEG から APP1（EXIF・XMP）・APP3〜APP13（IPTC など）・APP15・COM を除く。壊れていれば元のまま。"""
    if data[:2] != b"\xff\xd8":
        return data
    out = bytearray(data[:2])
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return data
        marker = data[i + 1]
        if marker == 0xDA:                       # 画像の本体が始まる
            out += data[i:]
            return bytes(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2 or i + 2 + seglen > len(data):
            return data
        drop = marker in (0xE1, 0xEF, 0xFE) or 0xE3 <= marker <= 0xED
        if not drop:
            out += data[i:i + 2 + seglen]
        i += 2 + seglen
    return data


def strip_png(data: bytes) -> bytes:
    """PNG から文字のチャンク（tEXt・zTXt・iTXt）と EXIF（eXIf）を除く。壊れていれば元のまま。"""
    if not data.startswith(_PNG_SIG):
        return data
    out = bytearray(_PNG_SIG)
    i = len(_PNG_SIG)
    while i + 12 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            return data
        if ctype not in (b"tEXt", b"zTXt", b"iTXt", b"eXIf"):
            out += data[i:end]
        i = end
        if ctype == b"IEND":
            return bytes(out)
    return data


# 中を読めない部品から拾った文字の場所に付ける印（検査では3字以上の原文だけを探す。
# 圧縮されたバイト列を文字として読むと、2字の語は偶然に現れうるため）
OPAQUE_MARK = "（中を読めない部品）"


def is_image(data: bytes) -> bool:
    """付帯情報を取り除いた後の画像（JPEG・PNG）。中は絵のデータだけなので文字としては探さない。"""
    return data[:2] == b"\xff\xd8" or data.startswith(_PNG_SIG)


def strip_image(name: str, data: bytes) -> bytes:
    if data[:2] == b"\xff\xd8":
        return strip_jpeg(data)
    if data.startswith(_PNG_SIG):
        return strip_png(data)
    return data


def map_zip(data: bytes, xml_fn, depth: int = 0) -> bytes:
    """zip の形の部品（埋め込みの Excel・Word・PowerPoint）の中の XML を xml_fn(name, bytes) で書き換える。"""
    if depth > 3:
        return data
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return data
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            b = zin.read(info.filename)
            if looks_xml(info.filename, b):
                b = xml_fn(info.filename, b)
            elif is_zip(b):
                b = map_zip(b, xml_fn, depth + 1)
            else:
                b = strip_image(info.filename, b)
            zout.writestr(info, b)
    return buf.getvalue()


def zip_texts(data: bytes, prefix: str, depth: int = 0) -> list[tuple[str, str]]:
    """埋め込みの zip の中の XML を、タグを除いた文字にして返す（検査用）。[(文字, 場所)]"""
    out: list[tuple[str, str]] = []
    if depth > 3:
        return out
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return out
    for info in zin.infolist():
        b = zin.read(info.filename)
        loc = f"{prefix}!{info.filename}"
        if looks_xml(info.filename, b):
            out.append((xml_text(b), loc))
        elif is_zip(b):
            out += zip_texts(b, loc, depth + 1)
        elif not is_image(b):
            out.append((binary_text(b), loc + OPAQUE_MARK))
    return out


def xml_text(b: bytes) -> str:
    """XML のタグを除いた文字（run に分かれた名前もつながる）。"""
    s = b.decode("utf-8", "replace")
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


def binary_text(b: bytes) -> str:
    """中を読めない部品から、文字として読める部分を拾う（UTF-8・UTF-16LE・UTF-16BE・Shift_JIS）。"""
    texts = []
    for enc in ("utf-8", "utf-16-le", "utf-16-be", "cp932"):
        try:
            texts.append(b.decode(enc, "ignore"))
        except LookupError:
            continue
    return "\n".join(texts)
