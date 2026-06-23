"""parity 도구 CLI.

서브커맨드:
- compare  : Python 골든 캡처 vs Rust 실측 캡처를 비교 → PASS/FAIL + diff 리포트
- normalize: 단일 문서를 정규화해 출력(정책 디버깅용)

실제 서버 호출(capture)은 RV-40 runner(아르고스) 영역이라 본 도구에는 두지 않는다.
본 도구는 "파일로 저장된 캡처를 같은 규칙으로 비교/판정"하는 엔진이다.

사용 예:
  python -m parity.cli compare \
      --rules rules/compare-rules.sample.json \
      --golden fixtures/META-01/healthz.python.golden.json \
      --actual fixtures/META-01/healthz.rust.actual.json \
      --case   fixtures/META-01/healthz.case.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compare import compare_documents
from .fixtures import load_capture, load_case
from .normalizer import normalize
from .rules import load_rules


def _cmd_compare(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    py = load_capture(args.golden)
    rust = load_capture(args.actual)

    contract_id = args.contract
    array_sort: set[str] = set()
    if args.case:
        case = load_case(args.case)
        contract_id = contract_id or case.contract_id
        array_sort = set(case.array_sort_paths)

    result = compare_documents(
        py.comparable(),
        rust.comparable(),
        rules=rules,
        contract_id=contract_id,
        array_sort_paths=array_sort,
    )
    summary = result.summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    status = "PASS" if result.passed else "FAIL"
    print(
        f"\n[{status}] contract={contract_id} "
        f"diffs={len(result.diffs)} strict={len(result.strict_diffs)} "
        f"security={len(result.security_violations)}",
        file=sys.stderr,
    )
    return 0 if result.passed else 1


def _cmd_normalize(args: argparse.Namespace) -> int:
    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
    norm, stats = normalize(doc)
    print(json.dumps(norm, ensure_ascii=False, indent=2))
    print(
        f"\nuuid={stats.uuid_count} timestamp={stats.timestamp_count} "
        f"abs_path={len(stats.abs_path_hits)} "
        f"bad_ts={len(stats.bad_timestamp_format)}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="parity", description="AgiTeamApp Python↔Rust 동등성 canonical-diff 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compare", help="골든 vs 실측 캡처 비교")
    c.add_argument("--rules", required=True, help="compare-rules JSON 경로")
    c.add_argument("--golden", required=True, help="Python oracle 캡처(JSON)")
    c.add_argument("--actual", required=True, help="Rust 실측 캡처(JSON)")
    c.add_argument("--case", help="입력셋(case) JSON — contract_id/array_sort 추출")
    c.add_argument("--contract", help="계약 ID(case 없을 때 명시)")
    c.set_defaults(func=_cmd_compare)

    n = sub.add_parser("normalize", help="단일 문서 정규화 출력(디버깅)")
    n.add_argument("--file", required=True, help="정규화할 JSON 경로")
    n.set_defaults(func=_cmd_normalize)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
