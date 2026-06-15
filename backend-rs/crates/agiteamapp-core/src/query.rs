//! 조회(GET) usecase: projects / rooms / room messages. 레퍼런스: routers/{projects,rooms}.py.
//! 응답은 Python 직렬화(room_summary_dict/message_to_dict/ProjectSummary)와 동등 — adapters.js 정합.

use serde_json::{json, Value};

use crate::activity::ActivityRegistry;
use crate::discovery::DiscoveryRegistry;
use crate::event::event_to_dict;
use crate::message::message_to_dict;
use crate::repo::{ApiError, MessagePage, MessageRow, RoomFull, WebguiRepository};

const REAL_SOURCES: &[&str] = &["hook", "transcript", "bridge", "pm_bridge", "webgui"];

fn provenance(source: Option<&str>, runtime_state: &str) -> Value {
    let origin = source.unwrap_or("mock");
    let is_real = REAL_SOURCES.contains(&origin);
    let rs = if is_real { runtime_state } else { "mock" };
    json!({
        "origin": origin,
        "is_real_data": is_real,
        "is_mock": origin == "mock",
        "runtime_state": rs,
    })
}

fn last_message_dict(m: &MessageRow) -> Value {
    json!({
        "message_id": m.message_id,
        "text": m.normalized_text,
        "direction": m.direction,
        "status": m.status,
        "occurred_at": m.occurred_at,
    })
}

/// room_summary_dict (Python 정합). connection_state 미포팅(cmux discovery) → "disconnected".
pub fn room_summary_dict(
    r: &RoomFull,
    last: Option<&MessageRow>,
    collector_state: &str,
    connection_state: &str,
    runtime_activity: &str,
    last_active_at: Option<&str>,
) -> Value {
    let runtime_state = if connection_state == "connected" || connection_state == "live" {
        "live"
    } else {
        "disconnected"
    };
    let last_source = last.map(|m| m.source.as_str());
    json!({
        "room_id": r.room_id,
        "project_id": r.project_id,
        "role": r.role_id,
        "display_name": r.display_name,
        "agent_type": r.agent_type,
        "room_type": r.room_type,
        "surface_id": r.current_surface_id,
        "agent_session_id": r.current_agent_session_id,
        "team_session_id": r.team_session_id,
        "agent_id": r.agent_id,
        "ready_state": r.ready_state,
        "collector_state": collector_state,
        "runtime_state": runtime_state,
        "runtime_activity": runtime_activity,
        "last_active_at": last_active_at,
        "provenance": provenance(last_source, runtime_state),
        "last_message": last.map(last_message_dict),
        "last_message_at": r.last_message_at,
        "read_marker_at": r.read_marker_at,
        "unread_count": r.unread_count,
    })
}

/// runtime_activity/last_active_at REST degrade (DS-110 §9.1). pulse 있으면 active.
fn activity_fields(reg: &ActivityRegistry, project_id: &str, role: &str) -> (String, Option<String>) {
    match reg.get_pulse(project_id, role) {
        Some(last_active) => ("active".to_string(), Some(last_active)),
        None => ("unknown".to_string(), None),
    }
}

/// GET /api/webgui/rooms → {project_id, rooms: RoomSummary[]}.
pub async fn list_rooms<R: WebguiRepository>(
    repo: &R,
    reg: &ActivityRegistry,
    disc: &DiscoveryRegistry,
    project_id: &str,
    role_id: Option<&str>,
    with_last_message: bool,
) -> Result<Value, ApiError> {
    let rooms = repo.list_rooms(project_id).await?;
    let mut out = Vec::new();
    for r in &rooms {
        if let Some(rid) = role_id {
            if r.role_id != rid {
                continue;
            }
        }
        let last = if with_last_message {
            match &r.last_message_id {
                Some(mid) => repo.get_message(mid).await?,
                None => None,
            }
        } else {
            None
        };
        let (act, last_active) = activity_fields(reg, project_id, &r.role_id);
        let conn = disc.connection_state(project_id, &r.role_id);
        out.push(room_summary_dict(
            r,
            last.as_ref(),
            "unknown",
            &conn,
            &act,
            last_active.as_deref(),
        ));
    }
    Ok(json!({ "project_id": project_id, "rooms": out }))
}

/// GET /api/webgui/rooms/{room_id}/messages → {room, messages, page}.
#[allow(clippy::too_many_arguments)]
pub async fn list_room_messages<R: WebguiRepository>(
    repo: &R,
    reg: &ActivityRegistry,
    room_id: &str,
    limit: i64,
    direction: &str,
    cursor: Option<String>,
) -> Result<Value, ApiError> {
    if direction != "asc" && direction != "desc" {
        return Err(ApiError::new("invalid_pagination", 422, "direction must be asc|desc"));
    }
    let room = repo
        .get_room_full(room_id)
        .await?
        .ok_or_else(|| ApiError::new("room_not_found", 404, "Room not found."))?;

    let parsed_cursor = match cursor {
        None => None,
        Some(c) => Some(parse_cursor(&c)?),
    };
    let page = MessagePage {
        limit,
        direction: direction.to_string(),
        cursor: parsed_cursor,
        correlation_id: None,
    };
    let mut rows = repo.list_room_messages(&room.room_id, &page).await?;
    let has_more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);

    let last = match &room.last_message_id {
        Some(mid) => repo.get_message(mid).await?,
        None => None,
    };
    let (act, last_active) = activity_fields(reg, &room.project_id, &room.role_id);
    let next_cursor = if has_more {
        rows.last()
            .map(|m| format!("{}|message:{}", m.occurred_at, m.message_id))
    } else {
        None
    };
    let messages: Vec<Value> = rows
        .iter()
        .map(|m| message_to_dict(m, &room.project_id))
        .collect();

    Ok(json!({
        "room": room_summary_dict(&room, last.as_ref(), "unknown", "disconnected", &act, last_active.as_deref()),
        "messages": messages,
        "page": { "limit": limit, "next_cursor": next_cursor, "has_more": has_more },
    }))
}

/// cursor = "{occurred_at}|message:{message_id}" → (ts, id).
fn parse_cursor(cursor: &str) -> Result<(String, String), ApiError> {
    match cursor.split_once("|message:") {
        Some((ts, id)) if !ts.is_empty() && !id.is_empty() => {
            Ok((ts.to_string(), id.to_string()))
        }
        _ => Err(ApiError::new("invalid_pagination", 422, "invalid cursor format")),
    }
}

fn message_update_type(direction: &str, status: &str) -> &'static str {
    if direction == "inbound" {
        "message_received"
    } else if status == "sent" {
        "message_sent"
    } else if status == "failed" {
        "message_failed"
    } else {
        "message_streaming"
    }
}

/// GET /api/webgui/messages/{message_id} → {message, related_updates}.
pub async fn get_message<R: WebguiRepository>(
    repo: &R,
    message_id: &str,
) -> Result<Value, ApiError> {
    let msg = repo
        .get_message(message_id)
        .await?
        .ok_or_else(|| ApiError::new("message_not_found", 404, "Message not found."))?;
    let mut related = Vec::new();
    if let Some(corr) = &msg.correlation_id {
        let events = repo.list_events(&msg.room_id, 200, Some(corr)).await?;
        for e in &events {
            related.push(json!({
                "update_id": format!("event:{}", e.event_id),
                "room_id": e.room_id,
                "correlation_id": e.correlation_id,
                "update_type": if e.event_type == "hook_stop" { "correlation_closed" } else { "runtime_error" },
                "message": Value::Null,
                "event": { "event_type": e.event_type, "severity": e.severity },
                "occurred_at": e.occurred_at,
            }));
        }
    }
    let pid = repo
        .get_room(&msg.room_id)
        .await?
        .map(|r| r.project_id)
        .unwrap_or_default();
    Ok(json!({ "message": message_to_dict(&msg, &pid), "related_updates": related }))
}

/// GET /api/webgui/message-updates → {updates, next_cursor}. project_id 격리 강제.
pub async fn message_updates<R: WebguiRepository>(
    repo: &R,
    room_id: &str,
    project_id: &str,
    after: Option<String>,
    limit: i64,
) -> Result<Value, ApiError> {
    // after 복합커서 "{recorded_at}|message:{id}" → 시각부분만.
    let after_ts = after.map(|a| match a.split_once("|message:") {
        Some((ts, _)) => ts.to_string(),
        None => a,
    });
    let room = repo
        .get_room(room_id)
        .await?
        .filter(|r| r.project_id == project_id)
        .ok_or_else(|| ApiError::new("room_not_found", 404, "Room not found."))?;
    let msgs = repo
        .updates_since(&room.room_id, after_ts.as_deref(), limit)
        .await?;
    let updates: Vec<Value> = msgs
        .iter()
        .map(|m| {
            json!({
                "update_id": format!("message:{}", m.message_id),
                "room_id": m.room_id,
                "correlation_id": m.correlation_id,
                "update_type": message_update_type(&m.direction, &m.status),
                "message": message_to_dict(m, &room.project_id),
                "event": Value::Null,
                "occurred_at": m.occurred_at,
            })
        })
        .collect();
    let next_cursor = msgs
        .last()
        .map(|m| format!("{}|message:{}", m.recorded_at, m.message_id));
    Ok(json!({ "updates": updates, "next_cursor": next_cursor }))
}

/// GET /api/webgui/rooms/{room_id}/events → {events, page}.
pub async fn list_events_uc<R: WebguiRepository>(
    repo: &R,
    room_id: &str,
    limit: i64,
    correlation_id: Option<&str>,
) -> Result<Value, ApiError> {
    repo.get_room(room_id)
        .await?
        .ok_or_else(|| ApiError::new("room_not_found", 404, "Room not found."))?;
    let mut rows = repo.list_events(room_id, limit, correlation_id).await?;
    let has_more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    let events: Vec<Value> = rows.iter().map(event_to_dict).collect();
    Ok(json!({
        "events": events,
        "page": { "limit": limit, "next_cursor": Value::Null, "has_more": has_more },
    }))
}

fn runtime_context_dict(r: &RoomFull, collector_state: &str, connection_state: &str) -> Value {
    json!({
        "room_id": r.room_id,
        "role": r.role_id,
        "display_name": r.display_name,
        "agent_type": r.agent_type,
        "surface_id": r.current_surface_id,
        "agent_session_id": r.current_agent_session_id,
        "ready_state": r.ready_state,
        "collector_state": collector_state,
        "connection_state": connection_state,
    })
}

/// GET /api/webgui/runtime/status → {project_id, server_time, rooms}.
pub async fn runtime_status<R: WebguiRepository>(
    repo: &R,
    disc: &DiscoveryRegistry,
    project_id: &str,
    room_id: Option<&str>,
    role_id: Option<&str>,
) -> Result<Value, ApiError> {
    let rooms = repo.list_rooms(project_id).await?;
    let server_time = repo.server_now().await?;
    let out: Vec<Value> = rooms
        .iter()
        .filter(|r| room_id.is_none_or(|rid| r.room_id == rid))
        .filter(|r| role_id.is_none_or(|rid| r.role_id == rid))
        .map(|r| {
            let conn = disc.connection_state(project_id, &r.role_id);
            runtime_context_dict(r, "unknown", &conn)
        })
        .collect();
    Ok(json!({ "project_id": project_id, "server_time": server_time, "rooms": out }))
}

/// POST /api/webgui/rooms/{room_id}/read → {room_id, read_marker_at, unread_count, updated_at}.
pub async fn mark_read<R: WebguiRepository>(
    repo: &R,
    room_id: &str,
    read_until: Option<&str>,
    last_read_message_id: Option<&str>,
) -> Result<Value, ApiError> {
    let room = repo
        .get_room(room_id)
        .await?
        .ok_or_else(|| ApiError::new("room_not_found", 404, "Room not found."))?;
    if let Some(mid) = last_read_message_id {
        let m = repo.get_message(mid).await?;
        match m {
            Some(m) if m.room_id == room.room_id => {}
            _ => {
                return Err(ApiError::new(
                    "message_room_mismatch",
                    409,
                    "Message does not belong to room.",
                ))
            }
        }
    }
    let (read_marker_at, unread, updated_at) = repo.mark_read(&room.room_id, read_until).await?;
    Ok(json!({
        "room_id": room.room_id,
        "read_marker_at": read_marker_at,
        "unread_count": unread,
        "updated_at": updated_at,
    }))
}

/// GET /api/webgui/projects → {selected_project_id, projects: ProjectSummary[]}.
/// cmux discovery 미포팅 → DB(방 보유 프로젝트) 기준 (QI-WG-021 경로).
pub async fn list_projects<R: WebguiRepository>(
    repo: &R,
    disc: &DiscoveryRegistry,
    selected_project_id: Option<&str>,
) -> Result<Value, ApiError> {
    // 디스커버리(cmux tree) 우선 — connection_state 실값 보유.
    let disc_projects = disc.projects();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut projects: Vec<Value> = Vec::new();
    for p in &disc_projects {
        if let Some(pid) = p["project_id"].as_str() {
            seen.insert(pid.to_string());
        }
        let mut v = p.clone();
        v["root_path"] = Value::Null;
        v["last_discovered_at"] = Value::Null;
        projects.push(v);
    }
    // DB 방보유 프로젝트 union (디스커버리에 없는 것만, QI-WG-021).
    for p in repo.list_projects().await? {
        if seen.contains(&p.project_id) {
            continue;
        }
        projects.push(json!({
            "project_id": p.project_id,
            "workspace_id": Value::Null,
            "workspace_title": p.project_id,
            "root_path": Value::Null,
            "connection_state": "disconnected",
            "pm_connection_state": "absent",
            "room_count": p.room_count,
            "selected": false,
            "last_discovered_at": Value::Null,
            "roles": p.roles,
        }));
    }
    let selected = disc.selected_project_id().or_else(|| selected_project_id.map(|s| s.to_string()));
    Ok(json!({ "selected_project_id": selected, "projects": projects }))
}
