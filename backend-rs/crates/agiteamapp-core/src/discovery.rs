//! cmux 디스커버리 레지스트리: `cmux tree` 파싱 → (project, role) 연결상태.
//! 레퍼런스: services/cmux_discovery.py. 파서 순수(테스트 가능). 시각은 epoch(http 주입).

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

#[derive(Debug, Clone)]
pub struct DiscoveredSurface {
    pub project_id: String,
    pub role_id: String,
    pub surface_id: String,
    pub display_name: String,
    pub workspace_id: String,
}

#[derive(Debug, Clone)]
pub struct DiscoveredProject {
    pub project_id: String,
    pub workspace_id: String,
    pub workspace_title: String,
    pub selected: bool,
    pub surfaces: Vec<DiscoveredSurface>,
}

fn first_quoted(s: &str) -> Option<(String, usize)> {
    let start = s.find('"')?;
    let rest = &s[start + 1..];
    let end = rest.find('"')?;
    Some((rest[..end].to_string(), start + 1 + end + 1))
}

/// `cmux tree` 출력 → 프로젝트 목록 (인식된 역할 surface 가 있는 workspace).
pub fn parse_tree(text: &str) -> Vec<DiscoveredProject> {
    let mut projects: HashMap<String, DiscoveredProject> = HashMap::new();
    let mut order: Vec<String> = Vec::new();
    let mut cur_ws = String::new();
    let mut cur_proj: Option<String> = None;
    let mut cur_selected = false;

    for line in text.lines() {
        if let Some(idx) = line.find("workspace ") {
            let after = &line[idx + "workspace ".len()..];
            let ws_id = after.split_whitespace().next().unwrap_or("").to_string();
            if let Some((title, _)) = first_quoted(after) {
                cur_ws = ws_id;
                cur_proj = Some(title.trim().to_string());
                cur_selected = line.contains("◀ active");
            }
            continue;
        }
        if let Some(idx) = line.find("surface ") {
            let Some(proj) = cur_proj.clone() else { continue };
            let after = &line[idx + "surface ".len()..];
            let surface_id = after.split_whitespace().next().unwrap_or("").to_string();
            // bracket [...]
            let bracket = after
                .find('[')
                .and_then(|b| after[b + 1..].find(']').map(|e| after[b + 1..b + 1 + e].to_string()))
                .unwrap_or_default();
            if !bracket.contains("terminal") {
                continue;
            }
            let Some((title, _)) = first_quoted(after) else { continue };
            let Some((display_name, role)) = parse_title(&title) else { continue };
            let entry = projects.entry(proj.clone()).or_insert_with(|| {
                order.push(proj.clone());
                DiscoveredProject {
                    project_id: proj.clone(),
                    workspace_id: cur_ws.clone(),
                    workspace_title: proj.clone(),
                    selected: cur_selected,
                    surfaces: vec![],
                }
            });
            entry.surfaces.push(DiscoveredSurface {
                project_id: proj.clone(),
                role_id: role,
                surface_id,
                display_name,
                workspace_id: cur_ws.clone(),
            });
        }
    }
    order.into_iter().filter_map(|k| projects.remove(&k)).collect()
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

    /// tree 텍스트로 갱신. 미present 역할은 disconnected 처리.
    pub fn refresh_from_tree(&self, tree_text: &str, now_epoch: i64) {
        let projects = parse_tree(tree_text);
        let mut inner = self.inner.lock().unwrap();
        inner.selected = None;
        let mut present: Vec<(String, String)> = Vec::new();
        for proj in &projects {
            inner.proj_meta.insert(
                proj.project_id.clone(),
                ProjMeta {
                    workspace_id: proj.workspace_id.clone(),
                    workspace_title: proj.workspace_title.clone(),
                    selected: proj.selected,
                },
            );
            if proj.selected {
                inner.selected = Some(proj.project_id.clone());
            }
            for s in &proj.surfaces {
                let key = (s.project_id.clone(), s.role_id.clone());
                present.push(key.clone());
                inner.map.insert(
                    key,
                    SurfaceInfo {
                        project_id: s.project_id.clone(),
                        role_id: s.role_id.clone(),
                        surface_id: s.surface_id.clone(),
                        display_name: s.display_name.clone(),
                        connection_state: "connected".into(),
                        last_seen_epoch: now_epoch,
                        workspace_id: s.workspace_id.clone(),
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

    pub fn selected_project_id(&self) -> Option<String> {
        self.inner.lock().unwrap().selected.clone()
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
