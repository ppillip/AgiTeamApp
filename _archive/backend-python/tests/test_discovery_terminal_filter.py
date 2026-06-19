"""QI-WG-021/023: discovery 는 terminal surface 만 role 에 연결한다.

panel/split/output 등 non-terminal surface 가 role 괄호 제목을 가져도, cmux
read-screen/send 에서 "Surface is not a terminal" 오류를 내므로 discovery 에서 제외한다.
"""
from __future__ import annotations

from app.services.cmux_discovery import parse_tree

# terminal/non-terminal 이 섞인 tree. "Output(PM)"/"Editor(BE)" 는 역할 괄호가 있어도
# [terminal] 이 아니므로 제외돼야 한다.
MIXED_TREE = '''window window:1 [current] ◀ active
├── workspace workspace:6 "Panthea" [selected] ◀ active
│   ├── pane pane:29 [focused] ◀ active
│   │   └── surface surface:29 [terminal] "제우스(PM)" [selected] ◀ active tty=ttys000
│   │   └── surface surface:30 [terminal] "불칸(BE)" [selected] tty=ttys001
│   │   └── surface surface:90 [panel] "Output(PM)" [selected] tty=
│   │   └── surface surface:91 [split] "Editor(BE)" [selected] tty=ttys099
'''


def test_non_terminal_surfaces_excluded():
    projects = parse_tree(MIXED_TREE)
    by_name = {p.workspace_title: p for p in projects}
    surfaces = by_name["Panthea"].surfaces
    ids = {s.surface_id for s in surfaces}
    # terminal 만 통과
    assert ids == {"surface:29", "surface:30"}
    # non-terminal 제외 확인
    assert "surface:90" not in ids   # [panel]
    assert "surface:91" not in ids   # [split]


def test_terminal_surfaces_roles_and_tty_intact():
    """terminal 필터 추가 후에도 기존 role/tty 파싱이 정상이어야 한다(group shift 회귀가드)."""
    projects = parse_tree(MIXED_TREE)
    surfaces = {s.role_id: s for s in projects[0].surfaces}
    assert surfaces["PM"].surface_id == "surface:29"
    assert surfaces["PM"].tty == "ttys000"
    assert surfaces["DeveloperBE"].surface_id == "surface:30"
    assert surfaces["DeveloperBE"].tty == "ttys001"
