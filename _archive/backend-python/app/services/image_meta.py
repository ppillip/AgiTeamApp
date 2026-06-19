"""이미지 signature + 크기 추출 (순수 파이썬, 외부 decoder 의존 없음).

DS-40 §7.6.3 / DS-60 §5.4.2: request content-type 만 신뢰하지 않고 magic bytes 로
재검증하고 width/height 를 추출한다. 지원 형식: PNG, JPEG, WebP, GIF.

Pillow 등 무거운 decoder 의존을 피하기 위해 헤더 파싱만 수행한다. 헤더로 형식·크기를
확정하지 못하면 None 을 반환하며, 호출부는 이를 invalid_image 로 처리한다.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# 허용 형식 (DS-120 확정): PNG/JPEG/WebP/GIF
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
# 정규 ext(저장 파일명용): jpeg→jpg 통일
_CANON_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp", "gif": "gif"}

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ALLOWED_EXT = set(_EXT_MIME.keys())


@dataclass
class ImageInfo:
    fmt: str          # 정규 확장자: png/jpg/webp/gif
    mime: str         # image/png 등
    width: int
    height: int


def mime_for_ext(ext: str | None) -> str | None:
    if not ext:
        return None
    return _EXT_MIME.get(ext.lower())


def _png(data: bytes) -> ImageInfo | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    if w <= 0 or h <= 0:
        return None
    return ImageInfo("png", "image/png", w, h)


def _gif(data: bytes) -> ImageInfo | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    w, h = struct.unpack("<HH", data[6:10])
    if w <= 0 or h <= 0:
        return None
    return ImageInfo("gif", "image/gif", w, h)


def _jpeg(data: bytes) -> ImageInfo | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0..SOF15 (C0~CF) 중 DHT(C4)/JPG(C8)/DAC(CC) 제외 = 실제 프레임 헤더
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            if w <= 0 or h <= 0:
                return None
            return ImageInfo("jpg", "image/jpeg", w, h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _webp(data: bytes) -> ImageInfo | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    fourcc = data[12:16]
    try:
        if fourcc == b"VP8 ":  # lossy
            # frame tag(3) + start code 0x9d 0x01 0x2a + width(2 LE,14bit) + height(2 LE,14bit)
            if data[23:26] != b"\x9d\x01\x2a":
                return None
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        elif fourcc == b"VP8L":  # lossless
            if data[20] != 0x2F:
                return None
            b = struct.unpack("<I", data[21:25])[0]
            w = (b & 0x3FFF) + 1
            h = ((b >> 14) & 0x3FFF) + 1
        elif fourcc == b"VP8X":  # extended
            w = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
            h = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
        else:
            return None
    except (struct.error, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    return ImageInfo("webp", "image/webp", w, h)


def detect_image(data: bytes) -> ImageInfo | None:
    """바이트에서 이미지 형식·크기 판정. 미지원/파싱불가 시 None."""
    for parser in (_png, _gif, _jpeg, _webp):
        info = parser(data)
        if info is not None:
            return info
    return None


def ext_consistent(declared_ext: str | None, detected_fmt: str) -> bool:
    """확장자 선언과 magic bytes 판정의 정합성 (jpeg/jpg 동치)."""
    if not declared_ext:
        return True  # paste blob 등 확장자 없으면 magic bytes 판정만 신뢰
    return _CANON_EXT.get(declared_ext.lower()) == detected_fmt
