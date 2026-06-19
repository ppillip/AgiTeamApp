"""이미지 첨부 저장 서비스 (DV-90 / 요구사항 16-1, 방식 A).

설계: DS-40 §7.6 (WG-MSG-06), DS-60 §5.4 (PM Bridge 경로 주입).

저장 경계(§5.4.1):
- 저장 root = `<project_root>/.agiteam/webgui/uploads/images/` (project 격리)
- 파일명 = `paste|upload-YYYYMMDDTHHMMSSZ-<sha256_8>.<ext>` (서버 생성값만, 사용자 원본명 미사용)
- TTL 기본 24h. 송신 여부 무관 background cleanup 대상.
- 공개 응답은 attachment_id·메타·preview_url 만. **절대경로는 PM Bridge 내부 resolve 에서만**.

DB 비의존: 산출물 서비스와 동일하게 파일시스템이 단일 진실원이다. attachment 메타는
이미지 파일 옆 sidecar JSON(`<attachment_id>.json`)에 보관하고, attachment_id 로 해소한다.

보안(§7.6.3):
- 파일명은 서버 생성. attachment_id 는 `att_[0-9a-f]+` 형식만 허용(path 주입 차단).
- 저장 root resolve 결과가 project root 하위인지 재검증(traversal 방어).
- magic bytes 교차검증 + decoder 로 width/height 추출(image_meta).
- 응답/로그에 절대경로·decoder 원문오류 미노출.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import image_meta
from .image_meta import ImageInfo

_ATTACHMENT_ID_RE = re.compile(r"^att_[0-9a-f]{8,40}$")
_UPLOAD_SUBDIR = (".agiteam", "webgui", "uploads", "images")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class AttachmentError(Exception):
    """업로드/해소 실패. code 는 DS-40 §7.6.5 error.code 와 1:1."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class StoredAttachment:
    attachment_id: str
    client_attachment_id: str | None
    project_id: str
    kind: str
    filename: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    sha256: str
    created_at: datetime
    expires_at: datetime
    abs_path: str  # 내부 전용. 공개 dict 에는 절대 포함하지 않는다.

    def public_dict(self) -> dict:
        """공개 MessageAttachment (DS-40 §4.2.1). host 절대경로 미포함."""
        return {
            "attachment_id": self.attachment_id,
            "client_attachment_id": self.client_attachment_id,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "preview_url": f"/api/webgui/message-attachments/{self.attachment_id}/preview",
            "expires_at": _iso_z(self.expires_at),
        }


class AttachmentService:
    def __init__(
        self,
        project_root: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        ttl_seconds: int = 24 * 3600,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._max_bytes = max_bytes
        self._ttl = timedelta(seconds=ttl_seconds)

    # --- 저장 디렉토리 (traversal 방어) ---

    def _store_dir(self) -> Path:
        d = self._project_root
        for seg in _UPLOAD_SUBDIR:
            d = d / seg
        d = d.resolve()
        # 저장 root 가 project root 하위인지 재검증(§7.6.3 traversal)
        try:
            d.relative_to(self._project_root)
        except ValueError:
            raise AttachmentError("attachment_storage_unavailable")
        return d

    def _ensure_store_dir(self) -> Path:
        d = self._store_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise AttachmentError("attachment_storage_unavailable")
        return d

    # --- 업로드 (WG-MSG-06) ---

    def save(
        self,
        *,
        project_id: str,
        data: bytes,
        declared_filename: str | None,
        client_attachment_id: str | None = None,
    ) -> StoredAttachment:
        if not data:
            raise AttachmentError("invalid_image")
        if len(data) > self._max_bytes:
            raise AttachmentError("attachment_too_large")

        declared_ext = None
        if declared_filename and "." in declared_filename:
            declared_ext = declared_filename.rsplit(".", 1)[1].lower()
            if declared_ext not in image_meta.ALLOWED_EXT:
                raise AttachmentError("unsupported_image_type")

        info: ImageInfo | None = image_meta.detect_image(data)
        if info is None:
            # magic bytes 로 이미지 판정 불가 → 형식 미지원/파싱 실패
            raise AttachmentError("invalid_image")
        # 확장자 선언이 magic bytes 와 모순이면 미지원 형식(content sniffing 방어)
        if not image_meta.ext_consistent(declared_ext, info.fmt):
            raise AttachmentError("unsupported_image_type")

        sha = hashlib.sha256(data).hexdigest()
        ts = _now()
        prefix = "upload" if declared_filename else "paste"
        filename = f"{prefix}-{_compact_ts(ts)}-{sha[:8]}.{info.fmt}"

        store = self._ensure_store_dir()
        abs_path = (store / filename).resolve()
        try:
            abs_path.relative_to(store)  # 파일명 주입 방어(서버 생성이라 사실상 항상 통과)
        except ValueError:
            raise AttachmentError("attachment_storage_unavailable")

        attachment_id = "att_" + uuid.uuid4().hex
        expires_at = ts + self._ttl
        meta = {
            "attachment_id": attachment_id,
            "client_attachment_id": client_attachment_id,
            "project_id": project_id,
            "kind": "image",
            "filename": filename,
            "mime_type": info.mime,
            "size_bytes": len(data),
            "width": info.width,
            "height": info.height,
            "sha256": sha,
            "created_at": _iso_z(ts),
            "expires_at": _iso_z(expires_at),
        }
        try:
            abs_path.write_bytes(data)
            (store / f"{attachment_id}.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            raise AttachmentError("attachment_storage_unavailable")

        return StoredAttachment(
            attachment_id=attachment_id,
            client_attachment_id=client_attachment_id,
            project_id=project_id,
            kind="image",
            filename=filename,
            mime_type=info.mime,
            size_bytes=len(data),
            width=info.width,
            height=info.height,
            sha256=sha,
            created_at=ts,
            expires_at=expires_at,
            abs_path=str(abs_path),
        )

    # --- 해소 (PM Bridge 내부 / preview) ---

    def resolve(self, project_id: str, attachment_id: str) -> StoredAttachment:
        """attachment_id → 저장 첨부. project 소유·만료 검증.

        실패 코드: attachment_not_found(없음/project 불일치), attachment_expired(TTL).
        """
        if not attachment_id or not _ATTACHMENT_ID_RE.match(attachment_id):
            raise AttachmentError("attachment_not_found")
        store = self._store_dir()
        sidecar = (store / f"{attachment_id}.json").resolve()
        try:
            sidecar.relative_to(store)  # path 주입 방어
        except ValueError:
            raise AttachmentError("attachment_not_found")
        if not sidecar.exists():
            raise AttachmentError("attachment_not_found")
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AttachmentError("attachment_not_found")
        # project 소유 검증(§7.5: 같은 project 의 업로드만 참조)
        if meta.get("project_id") != project_id:
            raise AttachmentError("attachment_not_found")

        expires_at = _parse_iso(meta.get("expires_at"))
        if expires_at is None or expires_at <= _now():
            raise AttachmentError("attachment_expired")

        abs_path = (store / meta["filename"]).resolve()
        try:
            abs_path.relative_to(store)
        except ValueError:
            raise AttachmentError("attachment_not_found")
        if not abs_path.exists():
            raise AttachmentError("attachment_expired")

        return StoredAttachment(
            attachment_id=meta["attachment_id"],
            client_attachment_id=meta.get("client_attachment_id"),
            project_id=meta["project_id"],
            kind=meta.get("kind", "image"),
            filename=meta["filename"],
            mime_type=meta["mime_type"],
            size_bytes=meta["size_bytes"],
            width=meta.get("width"),
            height=meta.get("height"),
            sha256=meta["sha256"],
            created_at=_parse_iso(meta.get("created_at")) or _now(),
            expires_at=expires_at,
            abs_path=str(abs_path),
        )

    # --- TTL cleanup ---

    def cleanup_expired(self) -> int:
        """만료 첨부(이미지+sidecar) 정리. 삭제 건수 반환. 실패해도 예외 던지지 않음."""
        removed = 0
        try:
            store = self._store_dir()
        except AttachmentError:
            return 0
        if not store.exists():
            return 0
        now = _now()
        for sidecar in store.glob("att_*.json"):
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                expires_at = _parse_iso(meta.get("expires_at"))
                if expires_at is not None and expires_at > now:
                    continue
                img = store / meta.get("filename", "")
                if img.name and img.exists():
                    img.unlink()
                sidecar.unlink()
                removed += 1
            except (OSError, ValueError):
                continue
        return removed


def candidate_project_ids(settings, registry=None) -> list[str]:
    """첨부 저장소를 가질 수 있는 후보 project_id 집합.

    원천: project_roots_json 키 ∪ Discovery registry 알려진 프로젝트 ∪ settings.project_id.
    (DV-90 cleanup·preview 전역해소 공용)
    """
    pids: set[str] = set()
    if settings.project_roots_json:
        try:
            pids.update(json.loads(settings.project_roots_json).keys())
        except (ValueError, TypeError):
            pass
    if registry is not None:
        try:
            for p in registry.projects():
                pid = p.get("project_id")
                if pid:
                    pids.add(pid)
        except Exception:  # noqa: BLE001
            pass
    if settings.project_id:
        pids.add(settings.project_id)
    return sorted(pids)


def resolve_attachment_globally(
    settings, registry, attachment_id: str
) -> tuple[str, "StoredAttachment"]:
    """attachment_id 만으로 sidecar 의 project_id 를 해소 (DS-40 §7.6.7 self-contained).

    등록된 project root 들의 `.agiteam/webgui/uploads/images/{attachment_id}.json` 을 찾아
    소유 project 를 먼저 해소한 뒤, 기존 경로보안(allowlist/traversal/root containment)을
    그대로 적용해 첨부를 반환한다. FE 가 project_id query 를 덧붙이지 않아도 동작한다.

    오류(§7.6.7): ID 형식·sidecar 없음·project 불일치·root escape → attachment_not_found,
    TTL/파일 누락 → attachment_expired.
    """
    if not attachment_id or not _ATTACHMENT_ID_RE.match(attachment_id):
        raise AttachmentError("attachment_not_found")
    for pid in candidate_project_ids(settings, registry):
        if not settings.project_exists(pid):
            continue
        svc = AttachmentService(
            settings.project_root(pid),
            max_bytes=settings.attachment_max_bytes,
            ttl_seconds=settings.attachment_ttl_seconds,
        )
        # 후보 project store 에 sidecar 가 있는지 먼저 확인(없으면 다음 후보)
        try:
            store = svc._store_dir()
        except AttachmentError:
            continue
        if not (store / f"{attachment_id}.json").exists():
            continue
        # 발견: 기존 resolve 로 소유·만료·root containment 검증.
        # 발견 후 만료(attachment_expired)는 즉시 전파(다음 후보 탐색 안 함).
        return pid, svc.resolve(pid, attachment_id)
    raise AttachmentError("attachment_not_found")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    txt = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(txt)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
