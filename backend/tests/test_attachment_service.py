"""이미지 첨부 저장 서비스 단위테스트 (DV-90 / DS-40 §7.6, DS-60 §5.4)."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from app.services.attachment_service import AttachmentError, AttachmentService


def _png(w: int = 10, h: int = 10) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


@pytest.fixture
def svc(tmp_path) -> AttachmentService:
    return AttachmentService(tmp_path, max_bytes=1024 * 1024, ttl_seconds=3600)


def test_save_public_dict_hides_abs_path(svc, tmp_path):
    stored = svc.save(project_id="P", data=_png(20, 30), declared_filename=None)
    pub = stored.public_dict()
    # 공개 dict 에 절대경로 미노출
    assert "abs_path" not in pub
    assert str(tmp_path) not in str(pub)
    assert pub["attachment_id"].startswith("att_")
    assert pub["mime_type"] == "image/png"
    assert (pub["width"], pub["height"]) == (20, 30)
    assert pub["preview_url"].endswith("/preview")
    # paste blob(파일명 없음) → paste- prefix
    assert pub["filename"].startswith("paste-")
    # 저장 위치 = <root>/.agiteam/webgui/uploads/images
    assert Path(stored.abs_path).parent == tmp_path / ".agiteam" / "webgui" / "uploads" / "images"


def test_save_upload_prefix_when_filename_present(svc):
    stored = svc.save(project_id="P", data=_png(), declared_filename="screenshot.png")
    assert stored.filename.startswith("upload-")


def test_reject_too_large(tmp_path):
    # _png 는 헤더만(~33B) 생성하므로 상한을 그보다 작게 잡아 용량초과를 유발
    svc = AttachmentService(tmp_path, max_bytes=10, ttl_seconds=3600)
    with pytest.raises(AttachmentError) as ei:
        svc.save(project_id="P", data=_png(), declared_filename=None)
    assert ei.value.code == "attachment_too_large"


def test_reject_non_image(svc):
    with pytest.raises(AttachmentError) as ei:
        svc.save(project_id="P", data=b"not an image", declared_filename=None)
    assert ei.value.code == "invalid_image"


def test_reject_ext_magic_mismatch(svc):
    # 확장자는 jpg 인데 실제 magic 은 png → unsupported_image_type
    with pytest.raises(AttachmentError) as ei:
        svc.save(project_id="P", data=_png(), declared_filename="evil.jpg")
    assert ei.value.code == "unsupported_image_type"


def test_reject_unsupported_ext(svc):
    with pytest.raises(AttachmentError) as ei:
        svc.save(project_id="P", data=_png(), declared_filename="x.bmp")
    assert ei.value.code == "unsupported_image_type"


def test_resolve_roundtrip(svc):
    stored = svc.save(project_id="P", data=_png(), declared_filename=None)
    got = svc.resolve("P", stored.attachment_id)
    assert got.attachment_id == stored.attachment_id
    assert Path(got.abs_path).exists()


def test_resolve_project_isolation(svc):
    stored = svc.save(project_id="P", data=_png(), declared_filename=None)
    # 타 프로젝트로 조회 시 not_found (소유 검증)
    with pytest.raises(AttachmentError) as ei:
        svc.resolve("OtherProj", stored.attachment_id)
    assert ei.value.code == "attachment_not_found"


def test_resolve_rejects_path_injection(svc):
    for bad in ["../etc/passwd", "att_/../x", "not_att_id", "att_xyz"]:
        with pytest.raises(AttachmentError) as ei:
            svc.resolve("P", bad)
        assert ei.value.code == "attachment_not_found"


def test_resolve_expired(tmp_path):
    svc = AttachmentService(tmp_path, max_bytes=1024 * 1024, ttl_seconds=-1)  # 즉시 만료
    stored = svc.save(project_id="P", data=_png(), declared_filename=None)
    with pytest.raises(AttachmentError) as ei:
        svc.resolve("P", stored.attachment_id)
    assert ei.value.code == "attachment_expired"


def test_cleanup_expired_removes_files(tmp_path):
    svc = AttachmentService(tmp_path, max_bytes=1024 * 1024, ttl_seconds=-1)
    stored = svc.save(project_id="P", data=_png(), declared_filename=None)
    assert Path(stored.abs_path).exists()
    removed = svc.cleanup_expired()
    assert removed == 1
    assert not Path(stored.abs_path).exists()


def test_cleanup_keeps_unexpired(svc):
    stored = svc.save(project_id="P", data=_png(), declared_filename=None)
    assert svc.cleanup_expired() == 0
    assert Path(stored.abs_path).exists()


# --- self-contained 전역 해소 (DS-40 §7.6.7) ---


class _FakeSettings:
    """전역 해소 테스트용 최소 settings stub."""

    def __init__(self, roots: dict[str, Path]) -> None:
        import json as _json

        self._roots = {k: Path(v) for k, v in roots.items()}
        # 실 운영처럼 등록 project 전체를 project_roots_json 으로 노출(candidate 열거 원천)
        self.project_roots_json = _json.dumps({k: str(v) for k, v in roots.items()})
        self.project_id = next(iter(roots), "P")
        self.attachment_max_bytes = 10 * 1024 * 1024
        self.attachment_ttl_seconds = 3600

    def project_root(self, pid: str) -> Path:
        return self._roots[pid]

    def project_exists(self, pid: str) -> bool:
        return pid in self._roots


def test_resolve_globally_finds_owner_project(tmp_path):
    from app.services.attachment_service import resolve_attachment_globally

    root_a = tmp_path / "ProjA"
    root_b = tmp_path / "ProjB"
    root_a.mkdir()
    root_b.mkdir()
    settings = _FakeSettings({"ProjA": root_a, "ProjB": root_b})
    # ProjB 에 업로드
    svc_b = AttachmentService(root_b, ttl_seconds=3600)
    stored = svc_b.save(project_id="ProjB", data=_png(), declared_filename=None)

    # project_id 없이 attachment_id 만으로 소유 project 해소
    pid, got = resolve_attachment_globally(settings, None, stored.attachment_id)
    assert pid == "ProjB"
    assert got.attachment_id == stored.attachment_id
    assert Path(got.abs_path).exists()


def test_resolve_globally_unknown_id_not_found(tmp_path):
    from app.services.attachment_service import resolve_attachment_globally

    root = tmp_path / "P"
    root.mkdir()
    settings = _FakeSettings({"P": root})
    for bad in ["att_" + "0" * 32, "att_zz", "bad-id", "../escape"]:
        with pytest.raises(AttachmentError) as ei:
            resolve_attachment_globally(settings, None, bad)
        assert ei.value.code == "attachment_not_found"


def test_resolve_globally_expired_propagates(tmp_path):
    from app.services.attachment_service import resolve_attachment_globally

    root = tmp_path / "P"
    root.mkdir()
    settings = _FakeSettings({"P": root})
    settings.attachment_ttl_seconds = -1
    svc = AttachmentService(root, ttl_seconds=-1)
    stored = svc.save(project_id="P", data=_png(), declared_filename=None)
    with pytest.raises(AttachmentError) as ei:
        resolve_attachment_globally(settings, None, stored.attachment_id)
    assert ei.value.code == "attachment_expired"
