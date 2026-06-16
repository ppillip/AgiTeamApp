"""골든 픽스처 포맷 로더·검증.

골든 픽스처 3종 (Python = oracle 캡처용):

1) 입력셋(case)      : *.case.json   — 무엇을 어떤 초기상태로 요청하는가
2) 골든출력(golden)  : *.golden.json — Python oracle이 돌려준 응답/WS 이벤트
3) DB덤프(dbsnap)    : *.dbsnap.json — 요청 직후 snapshot query 결과

스키마는 schemas/golden-fixture.md 참조. 본 모듈은 로드 + 최소 구조 검증만 한다
(값 검증·비교는 compare 엔진 담당).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class FixtureError(ValueError):
    pass


@dataclass
class Case:
    """입력셋: 하나의 테스트 케이스 요청 정의."""

    case_id: str
    contract_id: str
    title: str
    request: dict[str, Any]          # {kind: http|ws, method, path, query, headers, body, ws_ops}
    seed: dict[str, Any] = field(default_factory=dict)  # db_seed/fs/mux fixture 참조
    array_sort_paths: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Case":
        _require(d, ("case_id", "contract_id", "request"), "case")
        req = d["request"]
        _require(req, ("kind",), "case.request")
        if req["kind"] not in ("http", "ws"):
            raise FixtureError(f"case.request.kind는 http|ws여야 함: {req['kind']}")
        return Case(
            case_id=d["case_id"],
            contract_id=d["contract_id"],
            title=d.get("title", ""),
            request=req,
            seed=d.get("seed", {}),
            array_sort_paths=d.get("array_sort_paths", []),
        )


@dataclass
class Capture:
    """골든출력/실측 캡처(공통 포맷). Python과 Rust 모두 같은 포맷으로 저장한다."""

    case_id: str
    backend: str                      # "python" | "rust"
    http: dict[str, Any] | None = None   # {status, headers, body, body_b64?}
    ws_events: list[Any] | None = None   # WS envelope 리스트(수신순)
    db: dict[str, Any] | None = None     # {table: [row, ...]} snapshot
    mux: list[Any] | None = None         # fake mux capture(argv 등)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Capture":
        _require(d, ("case_id", "backend"), "capture")
        if d["backend"] not in ("python", "rust"):
            raise FixtureError(f"capture.backend는 python|rust여야 함: {d['backend']}")
        return Capture(
            case_id=d["case_id"],
            backend=d["backend"],
            http=d.get("http"),
            ws_events=d.get("ws_events"),
            db=d.get("db"),
            mux=d.get("mux"),
        )

    def comparable(self) -> dict[str, Any]:
        """compare 엔진에 넣을 비교 대상 문서로 변환.

        backend 식별자는 비교 대상에서 제외(당연히 다름).
        """
        doc: dict[str, Any] = {"case_id": self.case_id}
        if self.http is not None:
            doc["http"] = self.http
        if self.ws_events is not None:
            doc["ws_events"] = self.ws_events
        if self.db is not None:
            doc["db"] = self.db
        if self.mux is not None:
            doc["mux"] = self.mux
        return doc


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_case(path: str | Path) -> Case:
    return Case.from_dict(load_json(path))


def load_capture(path: str | Path) -> Capture:
    return Capture.from_dict(load_json(path))


def _require(d: dict[str, Any], keys: tuple[str, ...], ctx: str) -> None:
    missing = [k for k in keys if k not in d]
    if missing:
        raise FixtureError(f"{ctx}: 필수 키 누락 {missing}")
