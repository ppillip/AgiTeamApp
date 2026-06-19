"""마스킹 단위테스트 (DS-30 §4.4, DS-60 §13, TS-70)."""
from __future__ import annotations

from app.services.masking import MASK, mask_payload, mask_text


def test_mask_bearer_and_authorization():
    assert MASK in mask_text("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in mask_text("token=abc.def.ghi123456")


def test_mask_anthropic_key():
    out = mask_text("key sk-ant-1234567890abcdef")
    assert "sk-ant-1234567890abcdef" not in out


def test_mask_home_path():
    out = mask_text("read /Users/ppillip/.ssh/id_rsa")
    assert "/Users/ppillip" not in out
    assert "~" in out


def test_mask_payload_sensitive_key():
    p = {"tool": "exec", "api_key": "verysecretvalue1234", "nested": {"password": "p@ss"}}
    masked = mask_payload(p)
    assert masked["api_key"] == MASK
    assert masked["nested"]["password"] == MASK
    assert masked["tool"] == "exec"


def test_mask_payload_list():
    masked = mask_payload(["plain", "Bearer xyztoken12345"])
    assert masked[0] == "plain"
    assert MASK in masked[1]
