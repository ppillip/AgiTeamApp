//! agiteamapp-mux — MuxPort 의 infra 구현 (infra adapter).
//! 두 변형을 enum 으로 묶어 dyn 없이 디스패치한다:
//!  - Dummy: 실 전송(submit) 미수행. 안전 테스트/실 PM 미가동 환경용 (제출 흉내).
//!  - Team : team CLI(facade) subprocess 로 PM surface 해소·ping·submit·tree.
//!
//! ★ transport 경계: backend 는 멀티플렉서(mux)를 **직접 호출하지 않는다**.
//!   오직 team CLI(facade)만 호출하고, 실제 transport 선택·호출은 team 이 책임진다.
//!   (backend-rs 안에 mux 바이너리 직접 호출 0건.)
//!
//! 레퍼런스: Python services/pm_bridge.py + bin/team(facade).

use std::sync::Arc;

use agiteamapp_core::{ApiError, DiscoveryRegistry, MuxPort, MuxSurface, MuxWorkspace, PmTarget};
use tokio::process::Command;

// ── tree 텍스트 파서 (Phase 0: core/discovery.rs 에서 이관) ───────────────────
// `team list tree` 출력은 team facade 가 포워딩한 텍스트 포맷이다. 그 포맷은
// 어댑터(이 crate)만 안다. core 는 중립 추상 구조(MuxWorkspace/MuxSurface)만 받는다.
// 역할 인식/terminal 필터는 core 책임이라 여기서는 **구조만** 추출한다(모든 surface 를
// is_terminal 플래그와 원문 제목으로). 함수명은 호환을 위해 보존한다(tests 의존).

fn first_quoted(s: &str) -> Option<String> {
    let start = s.find('"')?;
    let rest = &s[start + 1..];
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

/// `team list tree` 출력 → 중립 workspace 목록.
/// 역할/terminal 필터는 적용하지 않는다.
pub fn parse_team_tree(text: &str) -> Vec<MuxWorkspace> {
    let mut out: Vec<MuxWorkspace> = Vec::new();
    for line in text.lines() {
        if let Some(idx) = line.find("workspace ") {
            let after = &line[idx + "workspace ".len()..];
            let ws_id = after.split_whitespace().next().unwrap_or("").to_string();
            if let Some(title) = first_quoted(after) {
                out.push(MuxWorkspace {
                    workspace_id: ws_id,
                    title: title.trim().to_string(),
                    selected: line.contains("◀ active"),
                    surfaces: vec![],
                });
            }
            continue;
        }
        if let Some(idx) = line.find("surface ") {
            let Some(ws) = out.last_mut() else { continue };
            let after = &line[idx + "surface ".len()..];
            let surface_id = after.split_whitespace().next().unwrap_or("").to_string();
            // bracket [...] 내부의 surface 종류 토큰
            let bracket = after
                .find('[')
                .and_then(|b| after[b + 1..].find(']').map(|e| after[b + 1..b + 1 + e].to_string()))
                .unwrap_or_default();
            let Some(title) = first_quoted(after) else { continue };
            ws.surfaces.push(MuxSurface {
                surface_id,
                title,
                is_terminal: bracket.contains("terminal"),
            });
        }
    }
    out
}

/// 안전 더미: 실 전송(submit) 미수행. resolve/ping 성공, submit 은 설정값 반환(실 전송 없음).
/// 단, discovery(tree)는 동작 완전보존을 위해 team facade 로 폴링한다(아테나 Q1=ⓐ).
/// 즉 '읽기(discovery)는 실제, 쓰기(submit)는 더미'로 기존 동작과 동일.
/// (transport 직접 폴링 → team list tree 폴링으로 교체. transport 직접 호출 0건.)
pub struct DummyMux {
    pub submit_ok: bool,
    /// discovery 폴링용 team CLI(facade). 기존 동작(Dummy 모드도 실 tree 폴링) 보존.
    pub team_bin: String,
}
impl Default for DummyMux {
    fn default() -> Self {
        Self { submit_ok: true, team_bin: MuxConfig::default_team_bin() }
    }
}
impl MuxPort for DummyMux {
    async fn resolve_role(&self, project_id: &str, role: &str) -> Result<Option<PmTarget>, ApiError> {
        Ok(Some(PmTarget {
            project_id: project_id.to_string(),
            surface_id: "surface:dummy".to_string(),
            workspace_id: None,
            display_name: role.to_string(),
        }))
    }
    async fn ping(&self, _t: &PmTarget) -> bool {
        true
    }
    async fn submit(&self, _t: &PmTarget, _text: &str) -> Result<bool, ApiError> {
        // 실 전송 없음 — 안전 더미.
        eprintln!("[mux:dummy] submit (no real delivery)");
        Ok(self.submit_ok)
    }
    async fn tree(&self) -> Result<Vec<MuxWorkspace>, ApiError> {
        // 동작 완전보존(아테나 Q1=ⓐ): Dummy 모드도 기존처럼 실 tree 로 discovery 를 폴링한다.
        // 단 transport 직접 호출 금지 → team list tree(facade) 경유.
        // 비정상/실패는 Err → 갱신 skip(직전 상태 보존).
        match Command::new(&self.team_bin).arg("list").arg("tree").output().await {
            Ok(out) if out.status.success() => {
                Ok(parse_team_tree(&String::from_utf8_lossy(&out.stdout)))
            }
            Ok(out) => Err(ApiError::new(
                "mux_tree_failed",
                502,
                format!("team list tree exit={:?}", out.status.code()),
            )),
            Err(e) => Err(ApiError::new("mux_tree_failed", 502, format!("team list tree: {e}"))),
        }
    }
}

/// 실 transport: team CLI(facade) 경유. surface 는 discovery 가 해소한 surface:NN 을 직접 지정한다.
/// team 이 transport(mux) 선택·호출·멀티라인 제출(soft-newline)을 내부 처리한다.
/// 레퍼런스: bin/team cmd_list/cmd_read/cmd_send.
pub struct TeamAdapter {
    /// team CLI(facade) 바이너리 경로. transport 직접 호출 대신 이 facade 만 부른다.
    pub team_bin: String,
    /// (보존) project_id → 프로젝트 루트 베이스. 현재 team 호출엔 미사용(대칭 유지).
    pub projects_base: String,
    /// PM surface 해소 일원화: discovery.resolve(project, role) 사용.
    pub discovery: Option<Arc<DiscoveryRegistry>>,
}

impl TeamAdapter {
    /// team subprocess. 특별한 env 주입 없음 — transport env(소켓 등)는 team/supervisor 책임.
    /// (기존 transport env setdefault 제거: backend 는 transport env 를 모른다. 폴러도 team 을
    ///  그것 없이 호출해 동작하므로 회귀 없음.)
    fn team(&self) -> Command {
        Command::new(&self.team_bin)
    }
    /// Python pm_bridge._refresh_discovery 대응: 포트 tree() 로 discovery 재해소.
    /// short ref 는 workspace-scoped 이므로 submit 직전 즉시 갱신해야 stale ref 회피.
    async fn refresh_discovery(&self) {
        let Some(disc) = &self.discovery else { return };
        if let Ok(workspaces) = self.tree().await {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            disc.refresh_from_workspaces(&workspaces, now);
        }
    }
    fn resolve_target(&self, project_id: &str, role: &str) -> Option<PmTarget> {
        self.discovery.as_ref().and_then(|disc| {
            disc.resolve(project_id, role)
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

impl MuxPort for TeamAdapter {
    async fn resolve_role(&self, project_id: &str, role: &str) -> Result<Option<PmTarget>, ApiError> {
        if self.discovery.is_some() {
            // ① 송신 직전 refresh (workspace-scoped 재해소, Python pm_bridge:181).
            self.refresh_discovery().await;
            if let Some(t) = self.resolve_target(project_id, role) {
                return Ok(Some(t));
            }
            // ② refresh-before-fail: 미해소면 1회 더 refresh 후 재시도 (QI-WG-023/DV-42).
            self.refresh_discovery().await;
            return Ok(self.resolve_target(project_id, role));
        }
        Ok(None)
    }
    async fn ping(&self, t: &PmTarget) -> bool {
        // team read --target surface:NN [--workspace ws] --lines 1 으로 liveness 확정.
        // ★ 역할명 아님 — discovery 가 해소한 surface(+workspace)를 team 에 직접 지정.
        let mut cmd = self.team();
        cmd.arg("read").arg("--target").arg(&t.surface_id);
        if let Some(ws) = &t.workspace_id {
            cmd.arg("--workspace").arg(ws);
        }
        cmd.arg("--lines").arg("1");
        match cmd.output().await {
            Ok(o) => o.status.success(),
            Err(_) => false,
        }
    }
    async fn submit(&self, t: &PmTarget, text: &str) -> Result<bool, ApiError> {
        // team send 가 입력+제출을 원자적으로 처리한다(멀티라인 soft-newline 분해는 team 책임).
        // backend 는 한 줄(team send --target surface:NN --text <text>)만 호출한다.
        //  - 과거 transport 직접 호출 시의 shift+enter/Enter 수동 분해·settle 지연은 전부 제거.
        //    그 책임은 이제 team facade(bin/team cmd_send) 안에 있다.
        let mut c = self.team();
        c.arg("send").arg("--target").arg(&t.surface_id).arg("--text").arg(text);
        eprintln!(
            "[mux:team:send] surface={} ws={:?} lines={}",
            t.surface_id,
            t.workspace_id,
            text.split('\n').count()
        );
        match c.output().await {
            Ok(o) => {
                if !o.status.success() {
                    eprintln!(
                        "[mux:team:send] surface={} FAILED exit={:?} / {}",
                        t.surface_id,
                        o.status.code(),
                        String::from_utf8_lossy(&o.stderr).trim()
                    );
                }
                Ok(o.status.success())
            }
            Err(e) => Err(ApiError::new("send_failed", 502, format!("team send: {e}"))),
        }
    }

    async fn tree(&self) -> Result<Vec<MuxWorkspace>, ApiError> {
        // `team list tree`(전체 tree) → 텍스트 파싱 → 중립 추상 구조. discovery 갱신의 단일 소스.
        // 비정상 종료/spawn 실패는 Err 로 반환해 호출자가 갱신을 건너뛰게 한다
        // (현행 `if status.success()` 가드와 동등 — 일시 실패 시 직전 상태 보존).
        match self.team().arg("list").arg("tree").output().await {
            Ok(out) if out.status.success() => {
                Ok(parse_team_tree(&String::from_utf8_lossy(&out.stdout)))
            }
            Ok(out) => Err(ApiError::new(
                "mux_tree_failed",
                502,
                format!("team list tree exit={:?}", out.status.code()),
            )),
            Err(e) => Err(ApiError::new("mux_tree_failed", 502, format!("team list tree: {e}"))),
        }
    }
}

/// dyn 회피용 enum 디스패처. http AppState 가 보유.
pub enum MuxAdapter {
    Dummy(DummyMux),
    Team(TeamAdapter),
}
impl MuxPort for MuxAdapter {
    async fn resolve_role(&self, project_id: &str, role: &str) -> Result<Option<PmTarget>, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.resolve_role(project_id, role).await,
            MuxAdapter::Team(m) => m.resolve_role(project_id, role).await,
        }
    }
    async fn ping(&self, t: &PmTarget) -> bool {
        match self {
            MuxAdapter::Dummy(m) => m.ping(t).await,
            MuxAdapter::Team(m) => m.ping(t).await,
        }
    }
    async fn submit(&self, t: &PmTarget, text: &str) -> Result<bool, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.submit(t, text).await,
            MuxAdapter::Team(m) => m.submit(t, text).await,
        }
    }
    async fn tree(&self) -> Result<Vec<MuxWorkspace>, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.tree().await,
            MuxAdapter::Team(m) => m.tree().await,
        }
    }
}

// ── 어댑터 팩토리 (Phase 0) ─────────────────────────────────────────────────
// main.rs 는 어댑터 선택 규칙을 모른다. env 해소 + 선택은 이 crate 가 전담한다.

/// 멀티플렉서 종류. 선택 규칙: AGITEAMAPP_MUX == "dummy" → Dummy, 그 외(미설정·임의값·레거시 포함) → Team(기본).
/// (backend 는 transport 를 모른다 — 실제 transport 선택·호출은 team facade 가 한다. 런처가
///  레거시 값으로 떠 있어도 backend 는 Team 으로 정상 동작한다.)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MuxKind {
    Dummy,
    Team,
}

/// 어댑터 구성값. env 에서 해소(MuxConfig::from_env)하거나 직접 구성한다.
#[derive(Debug, Clone)]
pub struct MuxConfig {
    pub kind: MuxKind,
    pub team_bin: String,
    pub projects_base: String,
    pub discovery_poll_ms: u64,
}

impl MuxConfig {
    /// 기본 프로젝트 베이스(현행 보존).
    pub const DEFAULT_PROJECTS_BASE: &'static str = "/Users/ppillip/Projects";
    /// 기본 프로젝트 id(team_bin 기본 경로 합성용. AGITEAMAPP_PROJECT_ID 로 재정의).
    pub const DEFAULT_PROJECT_ID: &'static str = "Panthea";
    /// 기본 discovery 폴링 주기(ms, 현행 보존).
    pub const DEFAULT_DISCOVERY_POLL_MS: u64 = 1000;

    /// team CLI(facade) 기본 경로: <projects_base>/<project_id>/bin/team.
    /// (supervisor 의 team_bin = <project_dir>/bin/team 컨벤션과 동일.)
    pub fn default_team_bin() -> String {
        let base = std::env::var("AGITEAMAPP_PROJECTS_BASE")
            .unwrap_or_else(|_| Self::DEFAULT_PROJECTS_BASE.to_string());
        let project = std::env::var("AGITEAMAPP_PROJECT_ID")
            .unwrap_or_else(|_| Self::DEFAULT_PROJECT_ID.to_string());
        format!("{}/{}/bin/team", base.trim_end_matches('/'), project)
    }

    /// env 에서 구성. 의미:
    /// - AGITEAMAPP_MUX == "dummy" → Dummy, 그 외(미설정·임의값·레거시 포함) → Team(기본)
    /// - AGITEAMAPP_TEAM_BIN(우선) 또는 <projects_base>/<project_id>/bin/team
    /// - AGITEAMAPP_PROJECTS_BASE / AGITEAMAPP_DISCOVERY_POLL_MS
    pub fn from_env() -> Self {
        let kind = match std::env::var("AGITEAMAPP_MUX").as_deref() {
            Ok("dummy") => MuxKind::Dummy,
            _ => MuxKind::Team,
        };
        let projects_base = std::env::var("AGITEAMAPP_PROJECTS_BASE")
            .unwrap_or_else(|_| Self::DEFAULT_PROJECTS_BASE.to_string());
        let team_bin = std::env::var("AGITEAMAPP_TEAM_BIN")
            .unwrap_or_else(|_| Self::default_team_bin());
        let discovery_poll_ms = std::env::var("AGITEAMAPP_DISCOVERY_POLL_MS")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(Self::DEFAULT_DISCOVERY_POLL_MS);
        Self { kind, team_bin, projects_base, discovery_poll_ms }
    }
}

/// 구성값 + discovery 레지스트리로 어댑터를 만든다. main.rs 는 이 함수만 호출한다.
pub fn build_mux_adapter(
    config: &MuxConfig,
    discovery: Option<Arc<DiscoveryRegistry>>,
) -> MuxAdapter {
    match config.kind {
        MuxKind::Team => MuxAdapter::Team(TeamAdapter {
            team_bin: config.team_bin.clone(),
            projects_base: config.projects_base.clone(),
            discovery,
        }),
        MuxKind::Dummy => MuxAdapter::Dummy(DummyMux {
            submit_ok: true,
            // 동작 완전보존: Dummy 모드도 team facade 로 discovery 폴링(config 의 team_bin 사용).
            team_bin: config.team_bin.clone(),
        }),
    }
}
