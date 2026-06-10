"""긴급 결함 2026-06-10: tool-call 누출 sanitize.

PM 출력이 tool_use 를 텍스트로 흘려 'invoke', 'parameter', 'course' 등 마크업이
대화 본문으로 저장·표시되는 것을 저장 직전에 차단한다.
"""
from __future__ import annotations

from app.services.sanitizer import has_tool_leak, sanitize_tool_leak

LT = "<"  # 태그 리터럴을 소스에 직접 두지 않기 위한 조립용


def test_none_and_empty_pass_through():
    assert sanitize_tool_leak(None) is None
    assert sanitize_tool_leak("") == ""


def test_clean_text_unchanged():
    s = "DS-40 작업을 검토해줘. of course 라는 표현도 보존된다."
    assert sanitize_tool_leak(s) == s
    assert has_tool_leak(s) is False


def test_full_invoke_block_removed():
    text = (
        "알겠습니다.\n"
        + LT + 'invoke name="Bash">\n'
        + LT + 'parameter name="command">ls -la' + LT + "/parameter>\n"
        + LT + "/invoke>\n"
        "처리하겠습니다."
    )
    out = sanitize_tool_leak(text)
    assert "invoke" not in out
    assert "parameter" not in out
    assert "ls -la" not in out          # 블록 내용까지 제거
    assert "알겠습니다." in out
    assert "처리하겠습니다." in out


def test_function_calls_wrapper_removed():
    text = (
        "course\n"
        + LT + "function_calls>\n"
        + LT + 'invoke name="Read">\n'
        + LT + 'parameter name="path">/etc/passwd' + LT + "/parameter>\n"
        + LT + "/invoke>\n"
        + LT + "/function_calls>"
    )
    out = sanitize_tool_leak(text)
    assert out == ""                    # 순수 누출 → 빈 본문
    assert "function_calls" not in out
    assert "passwd" not in out


def test_orphan_tags_stripped():
    """닫는 태그 없이 누출된 개별 태그 잔편은 태그만 제거하고 인접 본문은 보존."""
    text = "실제 발화 " + LT + 'invoke name="X"> 잔편 ' + LT + 'parameter name="y"> 끝'
    out = sanitize_tool_leak(text)
    assert "invoke" not in out
    assert "parameter" not in out
    assert "실제 발화" in out
    assert "끝" in out


def test_course_line_before_tag_removed():
    text = "course\n" + LT + 'invoke name="Bash">'
    out = sanitize_tool_leak(text)
    assert "course" not in out
    assert "invoke" not in out


def test_course_as_normal_word_preserved():
    """tool 마크업과 무관한 'course' 는 보존한다(오탐 방지)."""
    s = "Of course, 진행하겠습니다."
    assert sanitize_tool_leak(s) == s


def test_antml_namespace_variant():
    """antml: 네임스페이스가 붙은 누출도 제거한다."""
    text = LT + 'antml:invoke name="Bash">' + LT + 'antml:parameter name="c">x' + LT + "/antml:parameter>" + LT + "/antml:invoke>"
    out = sanitize_tool_leak(text)
    assert "invoke" not in out
    assert "parameter" not in out
    assert "antml" not in out


def test_has_tool_leak_detects():
    assert has_tool_leak("hello " + LT + 'invoke name="x">') is True
    assert has_tool_leak("just normal text") is False
