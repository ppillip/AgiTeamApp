//! agiteamapp-mux — MuxPort 의 infra 구현 (infra adapter).
//! 두 변형을 enum 으로 묶어 dyn 없이 디스패치한다:
//!  - Dummy: 실 cmux 미접근. 안전 테스트/실 PM 미가동 환경용 (제출 흉내).
//!  - Cmux : team CLI(=cmux facade) subprocess 로 PM surface 해소·ping·submit.
//!
//! 레퍼런스: Python services/{pm_bridge,cmux_discovery}.py + bin/team.

use std::sync::Arc;

use agiteamapp_core::{ApiError, DiscoveryRegistry, MuxPort, PmTarget};
use tokio::process::Command;

/// 안전 더미: 실 cmux 미접근. resolve/ping 성공, submit 은 설정값 반환(실 전송 없음).
pub struct DummyMux {
    pub submit_ok: bool,
}
impl Default for DummyMux {
    fn default() -> Self {
        Self { submit_ok: true }
    }
}
impl MuxPort for DummyMux {
    async fn resolve_pm(&self, project_id: &str) -> Result<Option<PmTarget>, ApiError> {
        Ok(Some(PmTarget {
            project_id: project_id.to_string(),
            surface_id: "surface:dummy".to_string(),
            workspace_id: None,
            display_name: "PM".to_string(),
        }))
    }
    async fn ping(&self, _t: &PmTarget) -> bool {
        true
    }
    async fn submit(&self, _t: &PmTarget, _text: &str) -> Result<bool, ApiError> {
        // 실 cmux 전송 없음 — 안전 더미.
        eprintln!("[mux:dummy] submit (no real delivery)");
        Ok(self.submit_ok)
    }
}

/// 실 cmux: team CLI 경유. project_id → project_root(AGITEAM_HOME) 매핑 후 team read/send.
/// team 이 role "PM" → surface 해소를 내부 수행한다(bin/team resolve_role_surface_from_tree).
pub struct CmuxAdapter {
    /// cmux 바이너리. team facade(역할해소 workspace-scoped 함정) 대신 cmux 직접 호출.
    /// 레퍼런스: Python cmux_adapter.py (cmux 직접 + workspace+surface 직접 지정 → cross-workspace).
    pub cmux_bin: String,
    /// (보존) project_id → 프로젝트 루트 베이스. 현재 cmux 직접 호출엔 미사용.
    pub projects_base: String,
    /// PM surface 해소 일원화: discovery.resolve(project, PM) 사용.
    pub discovery: Option<Arc<DiscoveryRegistry>>,
}

impl CmuxAdapter {
    /// cmux subprocess (Python _cmux_env 정합: 기존 env 상속 + CMUX_* setdefault).
    fn cmux(&self) -> Command {
        let mut c = Command::new(&self.cmux_bin);
        for (k, v) in [
            ("CMUX_PORT", "9330"),
            ("CMUX_PORT_END", "9339"),
            ("CMUX_PORT_RANGE", "10"),
            ("CMUX_BUNDLE_ID", "com.cmuxterm.app"),
        ] {
            if std::env::var(k).is_err() {
                c.env(k, v);
            }
        }
        c
    }
    /// Python pm_bridge._refresh_discovery 대응: `cmux tree` 출력으로 discovery 재해소.
    /// cmux short ref 는 workspace-scoped 이므로 submit 직전 즉시 갱신해야 stale ref 회피.
    async fn refresh_discovery(&self) {
        let Some(disc) = &self.discovery else { return };
        if let Ok(out) = self.cmux().arg("tree").output().await {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout);
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs() as i64)
                    .unwrap_or(0);
                disc.refresh_from_tree(&text, now);
            }
        }
    }
    fn resolve_target(&self, project_id: &str) -> Option<PmTarget> {
        self.discovery.as_ref().and_then(|disc| {
            disc.resolve(project_id, "PM")
                .filter(|i| i.connection_state == "connected")
                .map(|i| PmTarget {
                    project_id: project_id.to_string(),
                    surface_id: i.surface_id,
                    workspace_id: Some(i.workspace_id),
                    display_name: i.display_name,
                })
        })
    }
}

impl MuxPort for CmuxAdapter {
    async fn resolve_pm(&self, project_id: &str) -> Result<Option<PmTarget>, ApiError> {
        if self.discovery.is_some() {
            // ① 송신 직전 refresh (workspace-scoped 재해소, Python pm_bridge:181).
            self.refresh_discovery().await;
            if let Some(t) = self.resolve_target(project_id) {
                return Ok(Some(t));
            }
            // ② refresh-before-fail: 미해소면 1회 더 refresh 후 재시도 (QI-WG-023/DV-42).
            self.refresh_discovery().await;
            return Ok(self.resolve_target(project_id));
        }
        Ok(None)
    }
    async fn ping(&self, t: &PmTarget) -> bool {
        // Python pm_bridge:206 read-screen ping 으로 liveness 확정.
        // ★ 역할명 아님 — discovery 가 해소한 workspace+surface 를 cmux 에 직접 지정(cross-workspace).
        let mut cmd = self.cmux();
        cmd.arg("read-screen");
        if let Some(ws) = &t.workspace_id {
            cmd.arg("--workspace").arg(ws);
        }
        cmd.arg("--surface").arg(&t.surface_id).arg("--lines").arg("1");
        match cmd.output().await {
            Ok(o) => o.status.success(),
            Err(_) => false,
        }
    }
    async fn submit(&self, t: &PmTarget, text: &str) -> Result<bool, ApiError> {
        // Python cmux_adapter: `cmux send`(텍스트 입력) + `cmux send-key Enter`(제출) atomic.
        // workspace+surface 직접 지정(역할명 아님) → 어느 워크스페이스에서 떠도 PM 에 닿는다.
        let mut send = self.cmux();
        send.arg("send");
        if let Some(ws) = &t.workspace_id {
            send.arg("--workspace").arg(ws);
        }
        // `--` 구분자로 text 가 flag 로 오인되는 것 방지 (cmux send [flags] [--] <text>).
        send.arg("--surface").arg(&t.surface_id).arg("--").arg(text);
        let sent = match send.output().await {
            Ok(o) => o.status.success(),
            Err(e) => return Err(ApiError::new("send_failed", 502, format!("cmux send: {e}"))),
        };
        if !sent {
            return Ok(false);
        }
        // Enter 제출 (cmux send-key Enter)
        let mut key = self.cmux();
        key.arg("send-key");
        if let Some(ws) = &t.workspace_id {
            key.arg("--workspace").arg(ws);
        }
        key.arg("--surface").arg(&t.surface_id).arg("Enter");
        match key.output().await {
            Ok(o) => Ok(o.status.success()),
            Err(e) => Err(ApiError::new("send_failed", 502, format!("cmux send-key: {e}"))),
        }
    }
}

/// dyn 회피용 enum 디스패처. http AppState 가 보유.
pub enum MuxAdapter {
    Dummy(DummyMux),
    Cmux(CmuxAdapter),
}
impl MuxPort for MuxAdapter {
    async fn resolve_pm(&self, project_id: &str) -> Result<Option<PmTarget>, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.resolve_pm(project_id).await,
            MuxAdapter::Cmux(m) => m.resolve_pm(project_id).await,
        }
    }
    async fn ping(&self, t: &PmTarget) -> bool {
        match self {
            MuxAdapter::Dummy(m) => m.ping(t).await,
            MuxAdapter::Cmux(m) => m.ping(t).await,
        }
    }
    async fn submit(&self, t: &PmTarget, text: &str) -> Result<bool, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.submit(t, text).await,
            MuxAdapter::Cmux(m) => m.submit(t, text).await,
        }
    }
}
