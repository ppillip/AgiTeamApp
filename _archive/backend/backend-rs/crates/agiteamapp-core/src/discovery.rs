//! mux 디스커버리 레지스트리: mux tree(team facade 출력) 파싱 → (project, role) 연결상태.
//! 레퍼런스: 레거시 discovery 서비스. 파서 순수(테스트 가능). 시각은 epoch(http 주입).

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{json, Value};

fn role_token(token: &str) -> Option<&'static str> {
    Some(match token.trim().to_lowercase().as_str() {
        "pm" => "PM",
        "architect" | "arch" => "Architect",
        "be" | "developerbe" | "devbe" | "backend" => "DeveloperBE",
        "fe" | "developerfe" | "devfe" | "frontend" => "DeveloperFE",
        "qa" => "QA",
        "designer" | "design" => "Designer",
        "devops" | "ops" => "DevOps",
        _ => return None,
    })
}

const ROLE_ORDER: &[&str] = &["PM", "Architect", "DeveloperBE", "DeveloperFE", "Designer", "QA", "DevOps"];
fn role_order(role: &str) -> usize {
    ROLE_ORDER.iter().position(|r| *r == role).unwrap_or(99)
}

/// "제우스(PM)" → ("제우스","PM"). 인식 불가 None.
pub fn parse_title(title: &str) -> Option<(String, String)> {
    let t = title.trim();
    let open = t.rfind('(')?;
    let close = t.rfind(')')?;
    if close <= open {
        return None;
    }
    let role_tok = &t[open + 1..close];
    let role = role_token(role_tok)?;
    let display = t[..open].trim();
    Some((if display.is_empty() { role.to_string() } else { display.to_string() }, role.to_string()))
}

/// 멀티플렉서 중립 추상 구조 (DS-70 Phase 0). core 는 mux 의 **출력 포맷을 모른다**.
/// 어댑터(agiteamapp-mux)가 native tree(team facade 출력)를 파싱해 이 구조로 변환하고,
/// core 는 이 구조만 받아 도메인 규칙(역할 인식·terminal 필터·연결상태)을 적용한다.
#[derive(Debug, Clone)]
pub struct MuxSurface {
    pub surface_id: String,
    /// surface 표시 제목 원문(예: "제우스(PM)"). 역할 파싱은 core 의 parse_title 책임.
    pub title: String,
    /// 터미널 surface 여부. 비터미널(panel/split 등)은 core 가 제외한다.
    pub is_terminal: bool,
}

#[derive(Debug, Clone)]
pub struct MuxWorkspace {
    pub workspace_id: String,
    /// workspace 표시 제목(= project_id 의 현행 원천).
    pub title: String,
    pub selected: bool,
    pub surfaces: Vec<MuxSurface>,
}

#[derive(Debug, Clone)]
pub struct SurfaceInfo {
    pub project_id: String,
    pub role_id: String,
    pub surface_id: String,
    pub display_name: String,
    pub connection_state: String,
    pub last_seen_epoch: i64,
    pub workspace_id: String,
}

#[derive(Default)]
struct ProjMeta {
    workspace_id: String,
    workspace_title: String,
    selected: bool,
}

#[derive(Default)]
pub struct DiscoveryRegistry {
    inner: Mutex<Inner>,
}
#[derive(Default)]
struct Inner {
    map: HashMap<(String, String), SurfaceInfo>,
    proj_meta: HashMap<String, ProjMeta>,
    selected: Option<String>,
}

impl DiscoveryRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// 어댑터가 변환한 추상 workspace 목록으로 갱신. 미present 역할은 disconnected 처리.
    ///
    /// 도메인 규칙(현행 parse_tree 거동 보존):
    /// - terminal surface 만 채택(비터미널 제외)
    /// - 제목에서 역할 인식되는 surface 만 채택(parse_title)
    /// - project_id = workspace.title (현행 원천)
    /// - proj_meta/selected 는 인식된 역할 surface 가 1개 이상인 workspace 에만 등록(lazy)
    pub fn refresh_from_workspaces(&self, workspaces: &[MuxWorkspace], now_epoch: i64) {
        let mut inner = self.inner.lock().unwrap();
        inner.selected = None;
        let mut present: Vec<(String, String)> = Vec::new();
        for ws in workspaces {
            let project_id = ws.title.trim().to_string();
            let mut registered = false;
            for s in &ws.surfaces {
                if !s.is_terminal {
                    continue;
                }
                let Some((display_name, role)) = parse_title(&s.title) else { continue };
                if !registered {
                    // 첫 유효 surface 에서만 proj_meta/selected 등록(현행 lazy 생성과 동일).
                    inner.proj_meta.insert(
                        project_id.clone(),
                        ProjMeta {
                            workspace_id: ws.workspace_id.clone(),
                            workspace_title: project_id.clone(),
                            selected: ws.selected,
                        },
                    );
                    if ws.selected {
                        inner.selected = Some(project_id.clone());
                    }
                    registered = true;
                }
                let key = (project_id.clone(), role.clone());
                present.push(key.clone());
                inner.map.insert(
                    key,
                    SurfaceInfo {
                        project_id: project_id.clone(),
                        role_id: role,
                        surface_id: s.surface_id.clone(),
                        display_name,
                        connection_state: "connected".into(),
                        last_seen_epoch: now_epoch,
                        workspace_id: ws.workspace_id.clone(),
                    },
                );
            }
        }
        // 이번에 안 보인 역할 = disconnected
        let keys: Vec<_> = inner.map.keys().cloned().collect();
        for k in keys {
            if !present.contains(&k) {
                if let Some(info) = inner.map.get_mut(&k) {
                    info.connection_state = "disconnected".into();
                }
            }
        }
    }

    pub fn connection_state(&self, project_id: &str, role_id: &str) -> String {
        let inner = self.inner.lock().unwrap();
        inner
            .map
            .get(&(project_id.to_string(), role_id.to_string()))
            .map(|i| i.connection_state.clone())
            .unwrap_or_else(|| "disconnected".into())
    }

    pub fn resolve(&self, project_id: &str, role_id: &str) -> Option<SurfaceInfo> {
        let inner = self.inner.lock().unwrap();
        inner.map.get(&(project_id.to_string(), role_id.to_string())).cloned()
    }

    /// 현재 connected 인 모든 surface 의 (project_id, role_id). transcript 폴링 커버리지
    /// 교차검증용 — 발견된 surface 중 아직 훅으로 transcript 가 등록되지 않은 것을 식별한다.
    pub fn connected_surfaces(&self) -> Vec<(String, String)> {
        let inner = self.inner.lock().unwrap();
        inner
            .map
            .values()
            .filter(|i| i.connection_state == "connected")
            .map(|i| (i.project_id.clone(), i.role_id.clone()))
            .collect()
    }

    pub fn selected_project_id(&self) -> Option<String> {
        self.inner.lock().unwrap().selected.clone()
    }

    /// connected surface 들의 (project_id, role_id, display_name).
    /// room.display_name 을 discovery 해소 별칭으로 동기화(역할명 표시 결함 정정)하는 데 쓴다.
    pub fn connected_display_names(&self) -> Vec<(String, String, String)> {
        let inner = self.inner.lock().unwrap();
        inner
            .map
            .values()
            .filter(|i| i.connection_state == "connected")
            .map(|i| (i.project_id.clone(), i.role_id.clone(), i.display_name.clone()))
            .collect()
    }

    /// projects() — DS-40 ProjectSummary 원천(역할 목록 포함).
    pub fn projects(&self) -> Vec<Value> {
        let inner = self.inner.lock().unwrap();
        let mut grouped: HashMap<String, Vec<&SurfaceInfo>> = HashMap::new();
        for ((proj, _), info) in inner.map.iter() {
            grouped.entry(proj.clone()).or_default().push(info);
        }
        let mut out = Vec::new();
        for (proj, mut roles) in grouped {
            roles.sort_by_key(|r| role_order(&r.role_id));
            let meta = inner.proj_meta.get(&proj);
            let connected = roles.iter().any(|r| r.connection_state == "connected");
            let pm = roles.iter().find(|r| r.role_id == "PM");
            out.push(json!({
                "project_id": proj,
                "workspace_id": meta.map(|m| m.workspace_id.clone()),
                "workspace_title": meta.map(|m| m.workspace_title.clone()).unwrap_or_else(|| proj.clone()),
                "selected": meta.map(|m| m.selected).unwrap_or(false),
                "connection_state": if connected { "connected" } else { "disconnected" },
                "pm_connection_state": pm.map(|p| p.connection_state.as_str()).unwrap_or("absent"),
                "room_count": roles.len(),
                // FE(adapters.js)가 roles 를 객체배열로 소비(display_name·connection_state·monogram).
                // Python oracle 형태와 동일하게 노출(RV-55 string[] 판정은 오류였음, 9차 정정).
                "roles": roles.iter().map(|r| json!({
                    "role": r.role_id,
                    "display_name": r.display_name,
                    "surface_id": r.surface_id,
                    "connection_state": r.connection_state,
                    "last_seen_at": crate::attachments::epoch_to_iso(r.last_seen_epoch),
                    "team_session_id": Value::Null,
                    "agent_id": Value::Null,
                    "agent_type": Value::Null,
                })).collect::<Vec<_>>(),
            }));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ws(title: &str, surfaces: Vec<MuxSurface>) -> MuxWorkspace {
        MuxWorkspace { workspace_id: "ws:1".into(), title: title.into(), selected: true, surfaces }
    }
    fn surf(id: &str, title: &str) -> MuxSurface {
        MuxSurface { surface_id: id.into(), title: title.into(), is_terminal: true }
    }

    #[test]
    fn connected_display_names_exposes_parsed_alias() {
        // mumu 팬 제목 '박피엠(PM)' → display_name='박피엠', role='PM'.
        let reg = DiscoveryRegistry::new();
        reg.refresh_from_workspaces(
            &[ws("mumu", vec![surf("surface:77", "박피엠(PM)"), surf("surface:74", "박개발(DeveloperBE)")])],
            100,
        );
        let mut got = reg.connected_display_names();
        got.sort();
        assert_eq!(
            got,
            vec![
                ("mumu".to_string(), "DeveloperBE".to_string(), "박개발".to_string()),
                ("mumu".to_string(), "PM".to_string(), "박피엠".to_string()),
            ]
        );
        // 별칭이 role 과 달라 정정 대상임을 확인(표시 결함 정정의 입력).
        for (_p, role, display) in got {
            assert_ne!(role, display);
            assert!(!display.is_empty());
        }
    }

    #[test]
    fn connected_display_names_excludes_disconnected() {
        let reg = DiscoveryRegistry::new();
        reg.refresh_from_workspaces(&[ws("mumu", vec![surf("surface:77", "박피엠(PM)")])], 100);
        // 다음 폴에서 사라짐 → disconnected.
        reg.refresh_from_workspaces(&[ws("mumu", vec![])], 200);
        assert!(reg.connected_display_names().is_empty());
    }
}
