//! agiteamapp-mux — MuxPort 의 infra 구현 (infra adapter).
//! 두 변형을 enum 으로 묶어 dyn 없이 디스패치한다:
//!  - Dummy: 실 cmux 미접근. 안전 테스트/실 PM 미가동 환경용 (제출 흉내).
//!  - Cmux : team CLI(=cmux facade) subprocess 로 PM surface 해소·ping·submit.
//!
//! 레퍼런스: Python services/{pm_bridge,cmux_discovery}.py + bin/team.

use std::sync::Arc;

use agiteamapp_core::{ApiError, DiscoveryRegistry, MuxPort, MuxSurface, MuxWorkspace, PmTarget};
use tokio::process::Command;

// ── cmux tree 텍스트 파서 (Phase 0: core/discovery.rs 에서 이관) ─────────────
// cmux 의 `tree` 출력 텍스트 포맷은 어댑터(이 crate)만 안다. core 는 중립 추상
// 구조(MuxWorkspace/MuxSurface)만 받는다. 역할 인식/terminal 필터는 core 책임이라
// 여기서는 **구조만** 추출한다(모든 surface 를 is_terminal 플래그와 원문 제목으로).

fn first_quoted(s: &str) -> Option<String> {
    let start = s.find('"')?;
    let rest = &s[start + 1..];
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

/// `cmux tree` 출력 → 중립 workspace 목록. 역할/terminal 필터는 적용하지 않는다.
pub fn parse_cmux_tree(text: &str) -> Vec<MuxWorkspace> {
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
/// 단, discovery(tree)는 동작 완전보존을 위해 실 cmux 를 폴링한다(아테나 Q1=ⓐ).
/// 즉 '읽기(discovery)는 실제, 쓰기(submit)는 더미'로 기존 동작과 동일.
pub struct DummyMux {
    pub submit_ok: bool,
    /// discovery 폴링용 cmux 바이너리. 기존 동작(Dummy 모드도 실 cmux tree 폴링) 보존.
    pub cmux_bin: String,
}
impl Default for DummyMux {
    fn default() -> Self {
        Self { submit_ok: true, cmux_bin: MuxConfig::DEFAULT_CMUX_BIN.to_string() }
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
        // 실 cmux 전송 없음 — 안전 더미.
        eprintln!("[mux:dummy] submit (no real delivery)");
        Ok(self.submit_ok)
    }
    async fn tree(&self) -> Result<Vec<MuxWorkspace>, ApiError> {
        // 동작 완전보존(아테나 Q1=ⓐ): Dummy 모드도 기존처럼 실 cmux tree 로 discovery 를 폴링한다.
        // 원본 discovery 루프의 직접 호출과 동등(cmux_bin 직접 실행, 비정상/실패는 Err → 갱신 skip).
        // 누수 제거(Dummy=빈목록)는 별도 후속 피치로 분리.
        match Command::new(&self.cmux_bin).arg("tree").output().await {
            Ok(out) if out.status.success() => {
                Ok(parse_cmux_tree(&String::from_utf8_lossy(&out.stdout)))
            }
            Ok(out) => Err(ApiError::new(
                "mux_tree_failed",
                502,
                format!("cmux tree exit={:?}", out.status.code()),
            )),
            Err(e) => Err(ApiError::new("mux_tree_failed", 502, format!("cmux tree: {e}"))),
        }
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
    /// Python pm_bridge._refresh_discovery 대응: 포트 tree() 로 discovery 재해소.
    /// cmux short ref 는 workspace-scoped 이므로 submit 직전 즉시 갱신해야 stale ref 회피.
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

impl MuxPort for CmuxAdapter {
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
        // Python cmux_adapter.submit / bin/team cmd_send 정합:
        //  - 단일라인 = send(text) + send-key Enter(제출)
        //  - 멀티라인 = 각 줄 send, 줄 사이 send-key shift+enter(soft newline, 제출 안 함),
        //               맨 끝에 send-key Enter(진짜 제출). cmux send 가 실제 개행을 그대로
        //               흘려보내 에이전트 TUI 가 줄마다 제출로 오인하는 것을 막는다.
        // workspace+surface 직접 지정(역할명 아님) → 어느 워크스페이스에서 떠도 PM 에 닿는다.
        //
        // ★ 제출 누락 버그 수정: send → send-key 를 간격 없이 백투백 실행하면 에이전트 TUI
        //   (Claude Code·Codex)가 주입 텍스트를 입력위젯에 렌더하기 전에 Enter 가 도착해
        //   '빈 입력 제출(무효) + 텍스트만 남음'이 된다. team(셸 서브프로세스)·Python 은
        //   자연 스폰 간격이 있어 안 터지지만 tokio 백투백은 너무 빠르다. 텍스트 주입 후
        //   settle 지연을 두고 Enter 를 보낸다(기본 120ms, AGITEAMAPP_SUBMIT_SETTLE_MS).
        let settle_ms = std::env::var("AGITEAMAPP_SUBMIT_SETTLE_MS")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(200); // 기본 200ms (TUI 렌더 settle, 유저 지시 상향)
        let settle = || tokio::time::sleep(std::time::Duration::from_millis(settle_ms));
        eprintln!(
            "[mux:submit] surface={} ws={:?} lines={} settle={}ms",
            t.surface_id,
            t.workspace_id,
            text.split('\n').count(),
            settle_ms
        );

        // \r\n / \r 정규화 후 줄 분해.
        let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
        let lines: Vec<&str> = normalized.split('\n').collect();

        if lines.len() <= 1 {
            if !self.send_text(t, text).await? {
                return Ok(false);
            }
            settle().await; // TUI 렌더 settle 후 제출
            let ok = self.send_key(t, "Enter").await?;
            self.verify_submitted(t).await;
            return Ok(ok);
        }

        // 멀티라인: 줄 사이 shift+enter, 끝에 Enter
        for (i, line) in lines.iter().enumerate() {
            if i > 0 && !self.send_key(t, "shift+enter").await? {
                return Ok(false);
            }
            // 빈 줄은 shift+enter 로 이미 줄바꿈됨 → cmux send "" 회피
            if !line.is_empty() && !self.send_text(t, line).await? {
                return Ok(false);
            }
        }
        settle().await;
        let ok = self.send_key(t, "Enter").await?;
        self.verify_submitted(t).await;
        Ok(ok)
    }

    async fn tree(&self) -> Result<Vec<MuxWorkspace>, ApiError> {
        // `cmux tree` → 텍스트 파싱 → 중립 추상 구조. discovery 갱신의 단일 소스.
        // 비정상 종료/spawn 실패는 Err 로 반환해 호출자가 갱신을 건너뛰게 한다
        // (현행 `if status.success()` 가드와 동등 — 일시 실패 시 직전 상태 보존).
        match self.cmux().arg("tree").output().await {
            Ok(out) if out.status.success() => {
                Ok(parse_cmux_tree(&String::from_utf8_lossy(&out.stdout)))
            }
            Ok(out) => Err(ApiError::new(
                "mux_tree_failed",
                502,
                format!("cmux tree exit={:?}", out.status.code()),
            )),
            Err(e) => Err(ApiError::new("mux_tree_failed", 502, format!("cmux tree: {e}"))),
        }
    }
}

impl CmuxAdapter {
    /// `cmux send [--workspace ws] --surface X -- <text>` (텍스트 입력, 제출 안 함).
    async fn send_text(&self, t: &PmTarget, text: &str) -> Result<bool, ApiError> {
        let mut c = self.cmux();
        c.arg("send");
        if let Some(ws) = &t.workspace_id {
            c.arg("--workspace").arg(ws);
        }
        // `--` 로 text 가 flag 로 오인되는 것 방지 (cmux send [flags] [--] <text>).
        c.arg("--surface").arg(&t.surface_id).arg("--").arg(text);
        match c.output().await {
            Ok(o) => {
                // ② 진단: 텍스트 주입 명령 결과(운영 로그 가시화).
                eprintln!("[mux:send] surface={} rc={}", t.surface_id, o.status.success());
                Ok(o.status.success())
            }
            Err(e) => Err(ApiError::new("send_failed", 502, format!("cmux send: {e}"))),
        }
    }

    /// `cmux send-key [--workspace ws] --surface X <key>` (Enter / shift+enter 등).
    async fn send_key(&self, t: &PmTarget, key: &str) -> Result<bool, ApiError> {
        let mut c = self.cmux();
        c.arg("send-key");
        if let Some(ws) = &t.workspace_id {
            c.arg("--workspace").arg(ws);
        }
        c.arg("--surface").arg(&t.surface_id).arg(key);
        match c.output().await {
            Ok(o) => {
                // ② 진단: 제출(Enter)·soft newline 키 전송 결과.
                eprintln!("[mux:send-key] surface={} key={} rc={}", t.surface_id, key, o.status.success());
                Ok(o.status.success())
            }
            Err(e) => Err(ApiError::new("send_failed", 502, format!("cmux send-key: {e}"))),
        }
    }

    /// ④ 제출 검증: 제출 직후 입력란을 읽어 정체 텍스트 유무를 로그로 남긴다(진단용).
    /// AGITEAMAPP_SUBMIT_VERIFY=1 일 때만 동작(평상시 비용 0).
    async fn verify_submitted(&self, t: &PmTarget) {
        if std::env::var("AGITEAMAPP_SUBMIT_VERIFY").ok().as_deref() != Some("1") {
            return;
        }
        let mut c = self.cmux();
        c.arg("read-screen");
        if let Some(ws) = &t.workspace_id {
            c.arg("--workspace").arg(ws);
        }
        c.arg("--surface").arg(&t.surface_id).arg("--lines").arg("3");
        if let Ok(o) = c.output().await {
            let screen = String::from_utf8_lossy(&o.stdout);
            // 입력 프롬프트 줄(❯) 뒤에 내용이 남았는지 대략 판별.
            let stuck = screen.lines().any(|l| {
                let t = l.trim_start();
                t.starts_with('❯') && t.trim_start_matches('❯').trim().len() > 0
            });
            eprintln!("[mux:verify] surface={} input_stuck={}", t.surface_id, stuck);
        }
    }
}

/// dyn 회피용 enum 디스패처. http AppState 가 보유.
pub enum MuxAdapter {
    Dummy(DummyMux),
    Cmux(CmuxAdapter),
}
impl MuxPort for MuxAdapter {
    async fn resolve_role(&self, project_id: &str, role: &str) -> Result<Option<PmTarget>, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.resolve_role(project_id, role).await,
            MuxAdapter::Cmux(m) => m.resolve_role(project_id, role).await,
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
    async fn tree(&self) -> Result<Vec<MuxWorkspace>, ApiError> {
        match self {
            MuxAdapter::Dummy(m) => m.tree().await,
            MuxAdapter::Cmux(m) => m.tree().await,
        }
    }
}

// ── 어댑터 팩토리 (Phase 0) ─────────────────────────────────────────────────
// main.rs 는 어댑터 선택 규칙을 모른다. env 해소 + 선택은 이 crate 가 전담한다.

/// 멀티플렉서 종류. 선택 규칙은 현행 보존: AGITEAMAPP_MUX=cmux → Cmux, 그 외 → Dummy(기본).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MuxKind {
    Dummy,
    Cmux,
}

/// 어댑터 구성값. env 에서 해소(MuxConfig::from_env)하거나 직접 구성한다.
#[derive(Debug, Clone)]
pub struct MuxConfig {
    pub kind: MuxKind,
    pub cmux_bin: String,
    pub projects_base: String,
    pub discovery_poll_ms: u64,
}

impl MuxConfig {
    /// 기본 cmux 바이너리 절대경로(현행 보존). PATH 의존 금지.
    pub const DEFAULT_CMUX_BIN: &'static str = "/Applications/cmux.app/Contents/Resources/bin/cmux";
    /// 기본 프로젝트 베이스(현행 보존).
    pub const DEFAULT_PROJECTS_BASE: &'static str = "/Users/ppillip/Projects";
    /// 기본 discovery 폴링 주기(ms, 현행 보존).
    pub const DEFAULT_DISCOVERY_POLL_MS: u64 = 1000;

    /// env 에서 구성. 의미는 현행과 동일:
    /// - AGITEAMAPP_MUX == "cmux" → Cmux, 그 외(미설정 포함) → Dummy
    /// - AGITEAMAPP_CMUX_BIN / AGITEAMAPP_PROJECTS_BASE / AGITEAMAPP_DISCOVERY_POLL_MS
    pub fn from_env() -> Self {
        let kind = match std::env::var("AGITEAMAPP_MUX").as_deref() {
            Ok("cmux") => MuxKind::Cmux,
            _ => MuxKind::Dummy,
        };
        let cmux_bin = std::env::var("AGITEAMAPP_CMUX_BIN")
            .unwrap_or_else(|_| Self::DEFAULT_CMUX_BIN.to_string());
        let projects_base = std::env::var("AGITEAMAPP_PROJECTS_BASE")
            .unwrap_or_else(|_| Self::DEFAULT_PROJECTS_BASE.to_string());
        let discovery_poll_ms = std::env::var("AGITEAMAPP_DISCOVERY_POLL_MS")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(Self::DEFAULT_DISCOVERY_POLL_MS);
        Self { kind, cmux_bin, projects_base, discovery_poll_ms }
    }
}

/// 구성값 + discovery 레지스트리로 어댑터를 만든다. main.rs 는 이 함수만 호출한다.
pub fn build_mux_adapter(
    config: &MuxConfig,
    discovery: Option<Arc<DiscoveryRegistry>>,
) -> MuxAdapter {
    match config.kind {
        MuxKind::Cmux => MuxAdapter::Cmux(CmuxAdapter {
            cmux_bin: config.cmux_bin.clone(),
            projects_base: config.projects_base.clone(),
            discovery,
        }),
        MuxKind::Dummy => MuxAdapter::Dummy(DummyMux {
            submit_ok: true,
            // 동작 완전보존: Dummy 모드도 실 cmux 로 discovery 폴링(config 의 cmux_bin 사용).
            cmux_bin: config.cmux_bin.clone(),
        }),
    }
}
