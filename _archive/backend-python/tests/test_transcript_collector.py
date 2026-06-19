"""transcript_collector DB 비의존 로직 테스트 (DV-25).

세션 레지스트리 등록, Claude/Codex 파일 해소, offset tail 을 검증한다.
DB 저장 경로(_store_record)는 PostgreSQL 통합테스트(QA/DevOps PG)에서 검증.
"""
from __future__ import annotations

import json

from app.services.transcript_collector import (
    TranscriptSession,
    TranscriptSessionRegistry,
    find_claude_files,
    find_codex_files,
)
from app.services.transcript_parser import PROVIDER_CLAUDE, PROVIDER_CODEX


def test_session_registry_register_and_dedupe():
    reg = TranscriptSessionRegistry()
    reg.register(PROVIDER_CLAUDE, "s1", "Panthea", "DeveloperBE")
    reg.register(PROVIDER_CLAUDE, "s1", "Panthea", "DeveloperBE")  # 같은 키 → 1개 유지
    reg.register(PROVIDER_CODEX, "c1", "Panthea", "PM")
    sessions = reg.sessions()
    assert len(sessions) == 2
    be = next(s for s in sessions if s.session_id == "s1")
    assert be.role == "DeveloperBE" and be.project_id == "Panthea"


def test_session_registry_ignores_empty():
    reg = TranscriptSessionRegistry()
    reg.register(PROVIDER_CLAUDE, "", "Panthea", "PM")
    reg.register("", "s1", "Panthea", "PM")
    assert reg.sessions() == []


def test_find_claude_files(tmp_path, monkeypatch):
    import app.services.transcript_collector as tc

    proj = tmp_path / "Projects" / "Panthea"
    proj.mkdir(parents=True)
    slug = str(proj.resolve()).replace("/", "-")
    claude_dir = tmp_path / "home" / ".claude" / "projects" / slug
    claude_dir.mkdir(parents=True)
    (claude_dir / "sessA.jsonl").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(tc, "claude_root", lambda: tmp_path / "home" / ".claude" / "projects")
    # session 지정
    found = find_claude_files(proj, "sessA")
    assert len(found) == 1 and found[0].name == "sessA.jsonl"
    # 미존재 session
    assert find_claude_files(proj, "nope") == []


def test_find_codex_files_by_cwd(tmp_path, monkeypatch):
    import app.services.transcript_collector as tc

    proj = tmp_path / "Projects" / "Panthea"
    proj.mkdir(parents=True)
    codex_dir = tmp_path / "home" / ".codex" / "sessions" / "2026" / "06" / "08"
    codex_dir.mkdir(parents=True)
    roll = codex_dir / "rollout-2026-06-08T00-00-00-abc.jsonl"
    roll.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "abc", "cwd": str(proj.resolve())}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tc, "codex_root", lambda: tmp_path / "home" / ".codex" / "sessions")
    found = find_codex_files(proj)  # session_id 없이 cwd 매칭
    assert len(found) == 1 and found[0] == roll


def test_read_new_offset_tail(tmp_path):
    from app.config import get_settings
    from app.services.cmux_discovery import DiscoveryRegistry
    from app.services.transcript_collector import TranscriptCollector

    get_settings.cache_clear()
    tcol = TranscriptCollector(get_settings(), DiscoveryRegistry(), sessionmaker=None)
    f = tmp_path / "s.jsonl"
    f.write_text("old\n", encoding="utf-8")
    sess = TranscriptSession(PROVIDER_CLAUDE, "s", "Panthea", "PM")
    # 최초 발견 → EOF 부터(히스토리 재적재 안 함)
    assert tcol._read_new(sess, f) == ""
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("new line\n")
    out = tcol._read_new(sess, f)
    assert "new line" in out and "old" not in out
    assert tcol._read_new(sess, f) == ""
