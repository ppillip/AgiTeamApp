"""이미지 헤더 파서 단위테스트 (DV-90 / DS-40 §7.6.3)."""
from __future__ import annotations

import struct
import zlib

from app.services import image_meta


def _png_bytes(w: int, h: int) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return sig + chunk


def _gif_bytes(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 4


def _jpeg_bytes(w: int, h: int) -> bytes:
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    return soi + app0 + sof0 + b"\xff\xd9"


def _webp_vp8x(w: int, h: int) -> bytes:
    # RIFF....WEBP VP8X canvas w-1/h-1 (3 bytes LE each)
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
    body += struct.pack("<I", (w - 1))[:3] + struct.pack("<I", (h - 1))[:3]
    riff = b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body
    return riff


def test_detect_png():
    info = image_meta.detect_image(_png_bytes(1280, 720))
    assert info is not None
    assert info.fmt == "png" and info.mime == "image/png"
    assert (info.width, info.height) == (1280, 720)


def test_detect_gif():
    info = image_meta.detect_image(_gif_bytes(64, 32))
    assert info and info.fmt == "gif" and (info.width, info.height) == (64, 32)


def test_detect_jpeg():
    info = image_meta.detect_image(_jpeg_bytes(800, 600))
    assert info and info.fmt == "jpg" and info.mime == "image/jpeg"
    assert (info.width, info.height) == (800, 600)


def test_detect_webp_vp8x():
    info = image_meta.detect_image(_webp_vp8x(200, 100))
    assert info and info.fmt == "webp"
    assert (info.width, info.height) == (200, 100)


def test_non_image_rejected():
    assert image_meta.detect_image(b"not an image at all") is None
    assert image_meta.detect_image(b"") is None
    # PDF 매직바이트는 이미지 아님
    assert image_meta.detect_image(b"%PDF-1.7\n...") is None


def test_ext_consistency():
    assert image_meta.ext_consistent("png", "png") is True
    assert image_meta.ext_consistent("jpeg", "jpg") is True   # jpeg/jpg 동치
    assert image_meta.ext_consistent("jpg", "jpg") is True
    assert image_meta.ext_consistent("png", "jpg") is False   # 선언↔magic 모순
    assert image_meta.ext_consistent(None, "png") is True     # 확장자 없으면 magic 판정만
