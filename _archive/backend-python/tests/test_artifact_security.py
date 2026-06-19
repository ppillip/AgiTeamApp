"""산출물 보안 단위테스트 (DS-20 §13.7 / DS-40 §16.4, §17.7, TS-70).

allowlist + traversal 차단이 핵심 요구사항 (제우스 지시).
"""
from __future__ import annotations

import pytest

from app.errors import WebguiError


@pytest.mark.parametrize(
    "path,code",
    [
        ("../etc/passwd", "path_forbidden"),
        ("../../../../etc/passwd", "path_forbidden"),
        ("02.설계/../../secret", "path_forbidden"),
        ("/etc/passwd", "path_forbidden"),
        ("/Users/someone/.ssh/id_rsa", "path_forbidden"),
        ("C:\\Windows\\system32", "path_forbidden"),
        ("\\\\server\\share", "path_forbidden"),
    ],
)
def test_traversal_and_absolute_blocked(svc, path, code):
    with pytest.raises(WebguiError) as ei:
        svc.resolve(path)
    assert ei.value.code == code


def test_encoded_traversal_via_resolve_is_blocked(svc):
    # 서버는 디코딩된 '..' 를 받게 됨 (Starlette decode). 동일 차단.
    with pytest.raises(WebguiError) as ei:
        svc.resolve("02.설계/../../..")
    assert ei.value.code == "path_forbidden"


def test_null_byte_and_control_char(svc):
    with pytest.raises(WebguiError) as ei:
        svc.resolve("02.설계/\x00evil.md")
    assert ei.value.code == "invalid_path"


def test_hidden_and_secret_blocked(svc):
    for p in (".env", "secret.key"):
        with pytest.raises(WebguiError) as ei:
            svc.resolve(p)
        assert ei.value.code == "artifact_hidden"


def test_symlink_escape_blocked(svc, art_root, tmp_path):
    outside = tmp_path / "outside_secret.md"
    outside.write_text("leak", encoding="utf-8")
    link = art_root / "evil_link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not permitted in this environment")
    with pytest.raises(WebguiError) as ei:
        svc.resolve("evil_link.md")
    assert ei.value.code in ("symlink_forbidden", "path_forbidden")


def test_system_root_not_reachable(svc):
    # system/AgiTeamBuilder 는 allowlist(documents/products/AgiTeamApp) 밖 -> traversal 로만 도달 가능 -> 차단
    with pytest.raises(WebguiError) as ei:
        svc.resolve("../../../system/AgiTeamBuilder/agiteam.sh")
    assert ei.value.code == "path_forbidden"


def test_valid_relative_path_ok(svc):
    rp = svc.resolve("02.설계/DS-50_화면설계서/DS-50_화면설계서.md")
    assert rp.rel_path == "02.설계/DS-50_화면설계서/DS-50_화면설계서.md"
    assert rp.abs_path.exists()


def test_empty_path_is_root(svc):
    rp = svc.resolve("")
    assert rp.rel_path == ""
