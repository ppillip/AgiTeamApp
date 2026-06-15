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
    async fn resolve_pm(&self, _project_id: &str) -> Result<Option<PmTarget>, ApiError> {
        Ok(Some(PmTarget {
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
    pub team_bin: String,
    /// project_id → 프로젝트 루트(AGITEAM_HOME) 매핑 베이스. <base>/<project_id>.
    pub projects_base: String,
    /// PM surface 해소 일원화: discovery.resolve(project, PM) 사용.
    pub discovery: Option<Arc<DiscoveryRegistry>>,
}

impl CmuxAdapter {
    fn home(&self, project_id: &str) -> String {
        format!("{}/{}", self.projects_base.trim_end_matches('/'), project_id)
    }
    async fn team(&self, project_id: &str, args: &[&str]) -> std::io::Result<std::process::Output> {
        Command::new(&self.team_bin)
            .args(args)
            .env("AGITEAM_MUX", "cmux")
            .env("AGITEAM_HOME", self.home(project_id))
            .output()
            .await
    }
}

impl MuxPort for CmuxAdapter {
    async fn resolve_pm(&self, project_id: &str) -> Result<Option<PmTarget>, ApiError> {
        // discovery.resolve 로 PM surface 일원화. connected 일 때만 target 반환.
        if let Some(disc) = &self.discovery {
            return Ok(disc.resolve(project_id, "PM").filter(|i| i.connection_state == "connected").map(|i| {
                PmTarget {
                    surface_id: i.surface_id,
                    workspace_id: Some(i.workspace_id),
                    display_name: i.display_name,
                }
            }));
        }
        // discovery 미주입 시 team 의 내부 role 해소에 위임(논리값 "PM").
        Ok(Some(PmTarget {
            surface_id: "PM".to_string(),
            workspace_id: None,
            display_name: "PM".to_string(),
        }))
    }
    async fn ping(&self, _t: &PmTarget) -> bool {
        match self.team("", &["read", "--to", "PM", "--lines", "1"]).await {
            Ok(o) => o.status.success(),
            Err(_) => false,
        }
    }
    async fn submit(&self, _t: &PmTarget, text: &str) -> Result<bool, ApiError> {
        match self.team("", &["send", "--to", "PM", "--text", text]).await {
            Ok(o) => Ok(o.status.success()),
            Err(e) => Err(ApiError::new("send_failed", 502, format!("team send: {e}"))),
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
