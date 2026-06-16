"""RV-30 parity 도구 자체 단위테스트 (표준 unittest, 외부 의존 0).

도구가 "무엇을 통과시키고 무엇을 잡는지"를 고정한다. 이 테스트가 PASS해야
RV-30 도구를 신뢰할 수 있다(판정자가 사람이 아니라 이 도구이므로 도구 자체의
정확성이 곧 게이트 신뢰성).

실행: python3 -m unittest discover -s tools/equiv/tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# tools/equiv 를 import 경로에 추가
TOOLS_EQUIV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_EQUIV))

from parity.canonical import canonicalize, canonical_dumps  # noqa: E402
from parity.normalizer import normalize  # noqa: E402
from parity.rules import load_rules, from_dict, Policy  # noqa: E402
from parity.compare import compare_documents  # noqa: E402
from parity.fixtures import load_capture, load_case  # noqa: E402

FIX = TOOLS_EQUIV / "fixtures"
RULES_PATH = TOOLS_EQUIV / "rules" / "compare-rules.sample.json"


def _rules():
    return load_rules(RULES_PATH)


def _cap(p):
    return load_capture(FIX / p).comparable()


class TestCanonical(unittest.TestCase):
    def test_key_order_irrelevant(self):
        a = {"b": 1, "a": {"y": 2, "x": 3}}
        b = {"a": {"x": 3, "y": 2}, "b": 1}
        self.assertEqual(canonical_dumps(a), canonical_dumps(b))

    def test_list_order_preserved(self):
        self.assertEqual(canonicalize([3, 1, 2]), [3, 1, 2])


class TestNormalizer(unittest.TestCase):
    def test_uuid_reference_consistency(self):
        # 같은 uuid 두 번 → 같은 placeholder. 다른 uuid → 다른 placeholder.
        u1 = "11111111-2222-3333-4444-555555555555"
        u2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        doc = {"a": u1, "b": u1, "c": u2}
        norm, stats = normalize(doc)
        self.assertEqual(norm["a"], norm["b"])
        self.assertNotEqual(norm["a"], norm["c"])
        self.assertEqual(stats.uuid_count, 3)

    def test_timestamp_placeholder(self):
        doc = {"t": "2026-06-16T09:23:45.123456+00:00"}
        norm, stats = normalize(doc)
        self.assertTrue(norm["t"].startswith("<TS#"))
        self.assertEqual(stats.timestamp_count, 1)

    def test_cursor_structure_preserved(self):
        # cursor = <ts>|message:<uuid> → 구조 유지, 비결정부만 치환
        cur = "2026-06-16T09:23:45+00:00|message:11111111-2222-3333-4444-555555555555"
        norm, _ = normalize({"cursor": cur})
        self.assertIn("|message:", norm["cursor"])
        self.assertIn("<TS#", norm["cursor"])
        self.assertIn("<UUID#", norm["cursor"])

    def test_abs_path_masked(self):
        norm, stats = normalize({"root_path": "/Users/ppillip/Projects/Panthea"})
        self.assertEqual(norm["root_path"], "<ABS_PATH>")
        self.assertEqual(len(stats.abs_path_hits), 1)


class TestRulesPolicy(unittest.TestCase):
    def setUp(self):
        self.rules = _rules()

    def test_explicit_contract_rule_wins(self):
        self.assertEqual(
            self.rules.policy_for("http.body.data.version", "META-01"), Policy.STRICT
        )

    def test_heuristic_normalize_suffix(self):
        # 명시 규칙 없는 *_at, *_id 는 normalize 휴리스틱
        self.assertEqual(self.rules.policy_for("http.body.data.foo_at"), Policy.NORMALIZE)
        self.assertEqual(self.rules.policy_for("http.body.data.some_id"), Policy.NORMALIZE)

    def test_heuristic_exclude(self):
        self.assertEqual(self.rules.policy_for("http.headers.date"), Policy.EXCLUDE)

    def test_default_strict(self):
        self.assertEqual(self.rules.policy_for("http.body.data.status"), Policy.STRICT)

    def test_array_wildcard_match(self):
        self.assertEqual(
            self.rules.policy_for("http.body.data.projects[].project_id", "WG-PROJ-01"),
            Policy.STRICT,
        )
        self.assertEqual(
            self.rules.policy_for("http.body.data.projects[].root_path", "WG-PROJ-01"),
            Policy.NORMALIZE,
        )


class TestCompareMeta01(unittest.TestCase):
    def test_pass(self):
        r = compare_documents(
            _cap("META-01/healthz.python.golden.json"),
            _cap("META-01/healthz.rust.actual.json"),
            _rules(), contract_id="META-01",
        )
        self.assertTrue(r.passed, msg=json.dumps(r.summary(), ensure_ascii=False, indent=2))

    def test_fail_status_and_version(self):
        r = compare_documents(
            _cap("META-01/healthz.python.golden.json"),
            _cap("META-01/healthz.rust.fail.json"),
            _rules(), contract_id="META-01",
        )
        self.assertFalse(r.passed)
        paths = {d.path for d in r.diffs}
        self.assertIn("http.body.data.status", paths)
        self.assertIn("http.body.data.version", paths)
        # 둘 다 strict 정책으로 잡혀야 함
        for d in r.diffs:
            self.assertEqual(d.policy, "strict")


class TestCompareWgProj01(unittest.TestCase):
    def test_pass_with_nondeterminism(self):
        # workspace_id(uuid)·root_path·last_discovered_at 가 양쪽 다른 값이어도
        # 정규화로 PASS. strict 필드는 동일.
        case = load_case(FIX / "WG-PROJ-01" / "projects.case.json")
        r = compare_documents(
            _cap("WG-PROJ-01/projects.python.golden.json"),
            _cap("WG-PROJ-01/projects.rust.actual.json"),
            _rules(), contract_id="WG-PROJ-01",
            array_sort_paths=set(case.array_sort_paths),
        )
        self.assertTrue(r.passed, msg=json.dumps(r.summary(), ensure_ascii=False, indent=2))
        # root_path 노출은 경고로 남는다(parity FAIL은 아님)
        self.assertTrue(any("absolute path" in w for w in r.warnings))

    def test_strict_field_mismatch_fails(self):
        py = _cap("WG-PROJ-01/projects.python.golden.json")
        rust = _cap("WG-PROJ-01/projects.rust.actual.json")
        rust["http"]["body"]["data"]["projects"][0]["connection_state"] = "disconnected"
        r = compare_documents(py, rust, _rules(), contract_id="WG-PROJ-01")
        self.assertFalse(r.passed)
        self.assertIn(
            "http.body.data.projects[0].connection_state",
            {d.path for d in r.diffs},
        )


class TestSecurityAndStructure(unittest.TestCase):
    def test_forbidden_key_exposure(self):
        rules = _rules()
        py = {"http": {"body": {"data": {"role_id": 7}}}}
        rust = {"http": {"body": {"data": {"role_id": 7}}}}
        r = compare_documents(py, rust, rules, contract_id="META-01")
        self.assertFalse(r.passed)
        self.assertTrue(any("role_id" in v for v in r.security_violations))

    def test_exclude_policy_skips(self):
        rules = from_dict({"contracts": {"C": {"fields": {"a": "exclude"}}}})
        r = compare_documents({"a": 1}, {"a": 2}, rules, contract_id="C")
        self.assertTrue(r.passed)

    def test_missing_key_detected(self):
        r = compare_documents(
            {"http": {"x": 1}}, {"http": {}}, _rules(), contract_id="META-01"
        )
        self.assertFalse(r.passed)
        self.assertEqual(r.diffs[0].kind, "missing_in_rust")

    def test_array_length_diff(self):
        r = compare_documents(
            {"items": [1, 2]}, {"items": [1]}, _rules(), contract_id="META-01"
        )
        self.assertFalse(r.passed)
        self.assertTrue(any(d.kind == "array_length" for d in r.diffs))

    def test_array_sort_path(self):
        # 순서만 다른 배열: array_sort 지정 시 PASS, 미지정 시 FAIL
        py = {"events": [{"v": "a"}, {"v": "b"}]}
        rust = {"events": [{"v": "b"}, {"v": "a"}]}
        r_no = compare_documents(py, rust, _rules(), contract_id="META-01")
        self.assertFalse(r_no.passed)
        r_yes = compare_documents(
            py, rust, _rules(), contract_id="META-01", array_sort_paths={"events[]"}
        )
        self.assertTrue(r_yes.passed, msg=json.dumps(r_yes.summary(), ensure_ascii=False, indent=2))


class TestPlaceholderNumberMask(unittest.TestCase):
    """A-2: normalize 경로의 placeholder 번호 어긋남 흡수 (false FAIL 제거)."""

    def test_normalize_number_offset_passes(self):
        # py에 앞선 비결정 요소가 더 있어 placeholder 번호가 어긋난 경우 → 마스크로 PASS.
        # (a_pre는 exclude지만 normalize 번호 매김에는 포함되어 z의 번호를 +1 시킨다)
        rules = from_dict({"contracts": {"C": {"fields": {"a_pre": "exclude", "z": "normalize"}}}})
        py = {"a_pre": "11111111-1111-4111-8111-111111111111",
              "z": "22222222-2222-4222-8222-222222222222"}   # z=<UUID#2>
        rust = {"z": "33333333-3333-4333-8333-333333333333"}  # z=<UUID#1>
        r = compare_documents(py, rust, rules, contract_id="C")
        self.assertTrue(r.passed, msg=json.dumps(r.summary(), ensure_ascii=False, indent=2))

    def test_normalize_format_mismatch_still_fails(self):
        # 형식이 다르면(uuid vs timestamp) 여전히 FAIL
        rules = from_dict({"contracts": {"C": {"fields": {"a": "normalize"}}}})
        py = {"a": "11111111-1111-4111-8111-111111111111"}
        rust = {"a": "2026-06-16T09:00:00+00:00"}
        r = compare_documents(py, rust, rules, contract_id="C")
        self.assertFalse(r.passed)

    def test_strict_literal_mismatch_still_fails(self):
        # strict 경로의 리터럴 값차는 여전히 FAIL (마스크는 placeholder에만 적용)
        rules = from_dict({"contracts": {"C": {"fields": {"a": "strict"}}}})
        r = compare_documents({"a": "ok"}, {"a": "degraded"}, rules, contract_id="C")
        self.assertFalse(r.passed)


class TestMuxDiscoveryFilter(unittest.TestCase):
    """7차: mux discovery/liveness 명령(비결정 백그라운드)은 비교 제외, side-effect만."""

    def test_discovery_mux_asymmetry_ignored(self):
        rules = from_dict({"contracts": {"C": {}}})
        py = {"mux": [{"argv": ["read-screen", "--lines", "1"]}]}
        rust = {"mux": [{"argv": ["tree"]}]}  # 비대칭 discovery 호출 → 무시
        r = compare_documents(py, rust, rules, contract_id="C")
        self.assertTrue(r.passed, msg=json.dumps(r.summary(), ensure_ascii=False, indent=2))

    def test_one_side_only_discovery_ignored(self):
        rules = from_dict({"contracts": {"C": {}}})
        py = {"mux": []}
        rust = {"mux": [{"argv": ["tree", "--all"]}]}  # rs만 discovery → 무시
        r = compare_documents(py, rust, rules, contract_id="C")
        self.assertTrue(r.passed)

    def test_send_sideeffect_still_compared(self):
        rules = from_dict({"contracts": {"C": {}}})
        py = {"mux": [{"argv": ["send", "hello"]}]}
        rust = {"mux": [{"argv": ["send", "world"]}]}  # side-effect 차이 → FAIL 유지
        r = compare_documents(py, rust, rules, contract_id="C")
        self.assertFalse(r.passed)


class TestDbSnapshot(unittest.TestCase):
    def test_jsonb_key_order_normalized(self):
        # DB 덤프에서 jsonb가 문자열로 직렬화되어 키 순서가 달라도 통과
        py = {"db": {"webgui_message": [{"attachments_json": "{\"a\":1,\"b\":2}"}]}}
        rust = {"db": {"webgui_message": [{"attachments_json": "{\"b\":2,\"a\":1}"}]}}
        r = compare_documents(py, rust, _rules(), contract_id="WG-PROJ-01")
        self.assertTrue(r.passed, msg=json.dumps(r.summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
