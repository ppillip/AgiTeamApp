//! agiteamapp-db — WebguiRepository 포트의 sqlx(PostgreSQL) 구현 (infra adapter).
//! core 포트만 의존. transport(http/ipc) 는 모른다.
//! 레퍼런스: Python `db/repositories.py`. timestamp 는 ::text 캐스트로 읽어 chrono 의존 회피.

use agiteamapp_core::{
    EventRow, MessagePage, MessageRow, NewEvent, NewMessage, ProjectAgg, RepoError, RoomFull,
    RoomRef, RoomRow, WebguiRepository,
};
use sqlx::postgres::{PgPool, PgPoolOptions, PgRow};
use sqlx::Row;
use uuid::Uuid;

pub struct PgRepository {
    pool: PgPool,
}

impl PgRepository {
    pub async fn connect(database_url: &str) -> Result<Self, RepoError> {
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(database_url)
            .await
            .map_err(|e| RepoError(format!("connect: {e}")))?;
        Ok(Self { pool })
    }
    pub fn from_pool(pool: PgPool) -> Self {
        Self { pool }
    }
}

/// try_get + RepoError 변환 단축.
macro_rules! tg {
    ($row:expr, $col:literal) => {
        $row.try_get($col).map_err(|e| RepoError(format!("read {}: {}", $col, e)))?
    };
}

/// postgres `::text` 시각("2026-06-16 01:02:00.5+00") → ISO-8601("...T...+00:00").
fn iso(s: Option<String>) -> Option<String> {
    s.map(iso_str)
}
fn iso_str(s: String) -> String {
    let mut t = s.replacen(' ', "T", 1);
    if t.ends_with("+00") {
        t.push_str(":00");
    }
    t
}

/// webgui_message SELECT/RETURNING 공통 컬럼 (timestamp 는 text 캐스트).
const MSG_COLS: &str = "message_id::text AS message_id, room_id::text AS room_id, \
     correlation_id::text AS correlation_id, role_id, surface_id, \
     agent_session_id::text AS agent_session_id, team_session_id, direction, source, \
     message_type, normalized_text, status, attachments_json, occurred_at::text AS occurred_at, \
     recorded_at::text AS recorded_at, updated_at::text AS updated_at";

/// webgui_room SELECT 공통 컬럼 (uuid/timestamp text 캐스트, unread_count→bigint).
const ROOM_COLS: &str = "room_id::text AS room_id, project_id, role_id, display_name, agent_type, \
     room_type, current_surface_id, current_agent_session_id::text AS current_agent_session_id, \
     team_session_id, agent_id, ready_state, last_message_id::text AS last_message_id, \
     last_message_at::text AS last_message_at, read_marker_at::text AS read_marker_at, \
     unread_count::bigint AS unread_count";

fn map_room(row: &PgRow) -> Result<RoomFull, RepoError> {
    Ok(RoomFull {
        room_id: tg!(row, "room_id"),
        project_id: tg!(row, "project_id"),
        role_id: tg!(row, "role_id"),
        display_name: tg!(row, "display_name"),
        agent_type: tg!(row, "agent_type"),
        room_type: tg!(row, "room_type"),
        current_surface_id: tg!(row, "current_surface_id"),
        current_agent_session_id: tg!(row, "current_agent_session_id"),
        team_session_id: tg!(row, "team_session_id"),
        agent_id: tg!(row, "agent_id"),
        ready_state: tg!(row, "ready_state"),
        last_message_id: tg!(row, "last_message_id"),
        last_message_at: iso(tg!(row, "last_message_at")),
        read_marker_at: iso(tg!(row, "read_marker_at")),
        unread_count: tg!(row, "unread_count"),
    })
}

fn map_event(row: &PgRow) -> Result<EventRow, RepoError> {
    Ok(EventRow {
        event_id: tg!(row, "event_id"),
        room_id: tg!(row, "room_id"),
        message_id: tg!(row, "message_id"),
        correlation_id: tg!(row, "correlation_id"),
        event_type: tg!(row, "event_type"),
        source: tg!(row, "source"),
        hook_provider: tg!(row, "hook_provider"),
        hook_event_name: tg!(row, "hook_event_name"),
        severity: tg!(row, "severity"),
        payload: tg!(row, "payload"),
        occurred_at: iso_str(tg!(row, "occurred_at")),
        recorded_at: iso_str(tg!(row, "recorded_at")),
    })
}

fn map_message(row: &PgRow) -> Result<MessageRow, RepoError> {
    Ok(MessageRow {
        message_id: tg!(row, "message_id"),
        room_id: tg!(row, "room_id"),
        correlation_id: tg!(row, "correlation_id"),
        role_id: tg!(row, "role_id"),
        surface_id: tg!(row, "surface_id"),
        agent_session_id: tg!(row, "agent_session_id"),
        team_session_id: tg!(row, "team_session_id"),
        direction: tg!(row, "direction"),
        source: tg!(row, "source"),
        message_type: tg!(row, "message_type"),
        normalized_text: tg!(row, "normalized_text"),
        status: tg!(row, "status"),
        attachments_json: row
            .try_get::<Option<serde_json::Value>, _>("attachments_json")
            .ok()
            .flatten()
            .unwrap_or_else(|| serde_json::json!([])),
        occurred_at: iso_str(tg!(row, "occurred_at")),
        recorded_at: iso_str(tg!(row, "recorded_at")),
        updated_at: iso_str(tg!(row, "updated_at")),
    })
}

impl WebguiRepository for PgRepository {
    async fn upsert_room(
        &self,
        project_id: &str,
        role_id: &str,
        display_name: &str,
        room_type: &str,
        team_session_id: Option<&str>,
        agent_id: Option<&str>,
    ) -> Result<RoomRef, RepoError> {
        let existing: Option<(Uuid,)> = sqlx::query_as(
            "SELECT room_id FROM webgui_room WHERE project_id = $1 AND role_id = $2 LIMIT 1",
        )
        .bind(project_id)
        .bind(role_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| RepoError(format!("select room: {e}")))?;

        if let Some((room_id,)) = existing {
            sqlx::query(
                "UPDATE webgui_room SET team_session_id = COALESCE($1, team_session_id), \
                 agent_id = COALESCE($2, agent_id), updated_at = now() WHERE room_id = $3",
            )
            .bind(team_session_id)
            .bind(agent_id)
            .bind(room_id)
            .execute(&self.pool)
            .await
            .map_err(|e| RepoError(format!("update room: {e}")))?;
            return Ok(RoomRef {
                room_id: room_id.to_string(),
                project_id: project_id.to_string(),
                role_id: role_id.to_string(),
            });
        }

        let new_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO webgui_room \
             (room_id, project_id, role_id, display_name, room_type, team_session_id, agent_id) \
             VALUES ($1, $2, $3, $4, $5, $6, $7)",
        )
        .bind(new_id)
        .bind(project_id)
        .bind(role_id)
        .bind(display_name)
        .bind(room_type)
        .bind(team_session_id)
        .bind(agent_id)
        .execute(&self.pool)
        .await
        .map_err(|e| RepoError(format!("insert room: {e}")))?;

        Ok(RoomRef {
            room_id: new_id.to_string(),
            project_id: project_id.to_string(),
            role_id: role_id.to_string(),
        })
    }

    async fn get_room(&self, room_id: &str) -> Result<Option<RoomRow>, RepoError> {
        let row = sqlx::query(
            "SELECT room_id::text AS room_id, project_id, role_id, team_session_id \
             FROM webgui_room WHERE room_id = $1::uuid",
        )
        .bind(room_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| RepoError(format!("get_room: {e}")))?;
        match row {
            None => Ok(None),
            Some(r) => Ok(Some(RoomRow {
                room_id: tg!(&r, "room_id"),
                project_id: tg!(&r, "project_id"),
                role_id: tg!(&r, "role_id"),
                team_session_id: tg!(&r, "team_session_id"),
            })),
        }
    }

    async fn insert_runtime_event(&self, ev: NewEvent) -> Result<EventRow, RepoError> {
        let event_id = Uuid::new_v4();
        // masked_payload_json = payload_json ($8 재사용). occurred_at None → now().
        let row = sqlx::query(
            "INSERT INTO webgui_runtime_event \
             (event_id, room_id, agent_session_id, message_id, correlation_id, event_type, \
              source, hook_provider, hook_event_name, severity, payload_json, masked_payload_json, occurred_at) \
             VALUES ($1, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6, $7, $8, $9, $10, $11, $11, COALESCE($12::timestamptz, now())) \
             RETURNING event_id::text AS event_id, room_id::text AS room_id, \
              message_id::text AS message_id, correlation_id::text AS correlation_id, \
              event_type, source, hook_provider, hook_event_name, severity, \
              masked_payload_json AS payload, occurred_at::text AS occurred_at, \
              recorded_at::text AS recorded_at",
        )
        .bind(event_id)
        .bind(&ev.room_id)
        .bind(&ev.agent_session_id)
        .bind(&ev.message_id)
        .bind(&ev.correlation_id)
        .bind(&ev.event_type)
        .bind(&ev.source)
        .bind(&ev.hook_provider)
        .bind(&ev.hook_event_name)
        .bind(&ev.severity)
        .bind(&ev.payload_json)
        .bind(&ev.occurred_at_iso)
        .fetch_one(&self.pool)
        .await
        .map_err(|e| RepoError(format!("insert runtime_event: {e}")))?;

        Ok(EventRow {
            event_id: tg!(&row, "event_id"),
            room_id: tg!(&row, "room_id"),
            message_id: tg!(&row, "message_id"),
            correlation_id: tg!(&row, "correlation_id"),
            event_type: tg!(&row, "event_type"),
            source: tg!(&row, "source"),
            hook_provider: tg!(&row, "hook_provider"),
            hook_event_name: tg!(&row, "hook_event_name"),
            severity: tg!(&row, "severity"),
            payload: tg!(&row, "payload"),
            occurred_at: iso_str(tg!(&row, "occurred_at")),
            recorded_at: iso_str(tg!(&row, "recorded_at")),
        })
    }

    async fn find_message_by_record(
        &self,
        provider: &str,
        record_id: &str,
    ) -> Result<Option<MessageRow>, RepoError> {
        let sql = format!(
            "SELECT {MSG_COLS} FROM webgui_message \
             WHERE provider = $1 AND transcript_record_id = $2 LIMIT 1"
        );
        let row = sqlx::query(&sql)
            .bind(provider)
            .bind(record_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(|e| RepoError(format!("find_message_by_record: {e}")))?;
        row.as_ref().map(map_message).transpose()
    }

    async fn find_message_by_hash(
        &self,
        room_id: &str,
        source: &str,
        raw_hash: &str,
    ) -> Result<Option<MessageRow>, RepoError> {
        let sql = format!(
            "SELECT {MSG_COLS} FROM webgui_message \
             WHERE room_id = $1::uuid AND source = $2 AND raw_hash = $3 LIMIT 1"
        );
        let row = sqlx::query(&sql)
            .bind(room_id)
            .bind(source)
            .bind(raw_hash)
            .fetch_optional(&self.pool)
            .await
            .map_err(|e| RepoError(format!("find_message_by_hash: {e}")))?;
        row.as_ref().map(map_message).transpose()
    }

    async fn find_open_outbound_correlation(
        &self,
        room_id: &str,
    ) -> Result<Option<String>, RepoError> {
        let row = sqlx::query(
            "SELECT correlation_id::text AS correlation_id FROM webgui_message \
             WHERE room_id = $1::uuid AND direction = 'outbound' AND status = 'sent' \
               AND correlation_id IS NOT NULL \
             ORDER BY occurred_at DESC LIMIT 1",
        )
        .bind(room_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| RepoError(format!("find_open_outbound: {e}")))?;
        match row {
            None => Ok(None),
            Some(r) => Ok(tg!(&r, "correlation_id")),
        }
    }

    async fn create_message(&self, m: NewMessage) -> Result<MessageRow, RepoError> {
        let message_id = Uuid::new_v4();
        let sql = format!(
            "INSERT INTO webgui_message \
             (message_id, room_id, agent_session_id, correlation_id, role_id, surface_id, \
              team_session_id, direction, source, message_type, provider, transcript_path, \
              transcript_offset, transcript_record_id, raw_text, normalized_text, raw_hash, \
              status, occurred_at) \
             VALUES ($1, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8, $9, $10, $11, $12, $13, \
              $14, $15, $16, $17, $18, $19::timestamptz) \
             RETURNING {MSG_COLS}"
        );
        let row = sqlx::query(&sql)
            .bind(message_id)
            .bind(&m.room_id)
            .bind(&m.agent_session_id)
            .bind(&m.correlation_id)
            .bind(&m.role_id)
            .bind(&m.surface_id)
            .bind(&m.team_session_id)
            .bind(&m.direction)
            .bind(&m.source)
            .bind(&m.message_type)
            .bind(&m.provider)
            .bind(&m.transcript_path)
            .bind(&m.transcript_offset)
            .bind(&m.transcript_record_id)
            .bind(&m.raw_text)
            .bind(&m.normalized_text)
            .bind(&m.raw_hash)
            .bind(&m.status)
            .bind(&m.occurred_at_iso)
            .fetch_one(&self.pool)
            .await
            .map_err(|e| RepoError(format!("create_message: {e}")))?;
        map_message(&row)
    }

    async fn touch_room_last_message(
        &self,
        room_id: &str,
        message_id: &str,
        occurred_at_iso: &str,
        inbound: bool,
    ) -> Result<(), RepoError> {
        sqlx::query(
            "UPDATE webgui_room SET last_message_id = $2::uuid, \
             last_message_at = $3::timestamptz, updated_at = now(), \
             unread_count = unread_count + CASE WHEN $4 THEN 1 ELSE 0 END \
             WHERE room_id = $1::uuid",
        )
        .bind(room_id)
        .bind(message_id)
        .bind(occurred_at_iso)
        .bind(inbound)
        .execute(&self.pool)
        .await
        .map_err(|e| RepoError(format!("touch_room: {e}")))?;
        Ok(())
    }

    async fn get_room_full(&self, room_id: &str) -> Result<Option<RoomFull>, RepoError> {
        let sql = format!("SELECT {ROOM_COLS} FROM webgui_room WHERE room_id = $1::uuid");
        let row = sqlx::query(&sql)
            .bind(room_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(|e| RepoError(format!("get_room_full: {e}")))?;
        row.as_ref().map(map_room).transpose()
    }

    async fn active_collector_state(&self, room_id: &str) -> Result<Option<String>, RepoError> {
        // active(ended_at IS NULL) agent_session 의 collector_state (Python active_session_for_room 정합).
        let row = sqlx::query(
            "SELECT collector_state FROM webgui_agent_session \
             WHERE room_id = $1::uuid AND ended_at IS NULL \
             ORDER BY started_at DESC LIMIT 1",
        )
        .bind(room_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| RepoError(format!("active_collector_state: {e}")))?;
        Ok(row.and_then(|r| r.try_get::<Option<String>, _>("collector_state").ok().flatten()))
    }

    async fn list_rooms(&self, project_id: &str) -> Result<Vec<RoomFull>, RepoError> {
        let sql = format!(
            "SELECT {ROOM_COLS} FROM webgui_room WHERE project_id = $1 \
             ORDER BY last_message_at DESC NULLS LAST, created_at ASC"
        );
        let rows = sqlx::query(&sql)
            .bind(project_id)
            .fetch_all(&self.pool)
            .await
            .map_err(|e| RepoError(format!("list_rooms: {e}")))?;
        rows.iter().map(map_room).collect()
    }

    async fn get_message(&self, message_id: &str) -> Result<Option<MessageRow>, RepoError> {
        let sql = format!("SELECT {MSG_COLS} FROM webgui_message WHERE message_id = $1::uuid");
        let row = sqlx::query(&sql)
            .bind(message_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(|e| RepoError(format!("get_message: {e}")))?;
        row.as_ref().map(map_message).transpose()
    }

    async fn list_room_messages(
        &self,
        room_id: &str,
        page: &MessagePage,
    ) -> Result<Vec<MessageRow>, RepoError> {
        let order = if page.direction == "asc" { "ASC" } else { "DESC" };
        // keyset 커서: desc=before(<), asc=after(>).
        let cmp = if page.direction == "asc" { ">" } else { "<" };
        let mut where_clause = String::from("room_id = $1::uuid");
        if page.correlation_id.is_some() {
            where_clause.push_str(" AND correlation_id = $2::uuid");
        }
        if page.cursor.is_some() {
            // $3,$4 (correlation 유무와 무관하게 bind 순서 고정: correlation 은 항상 $2 자리 예약)
            where_clause.push_str(&format!(
                " AND (occurred_at {cmp} $3::timestamptz OR \
                 (occurred_at = $3::timestamptz AND message_id {cmp} $4::uuid))"
            ));
        }
        let sql = format!(
            "SELECT {MSG_COLS} FROM webgui_message WHERE {where_clause} \
             ORDER BY occurred_at {order}, message_id {order} LIMIT $5"
        );
        // correlation_id 가 없을 때도 $2 자리를 NULL 로 채워 bind 순서를 고정한다.
        let (cur_ts, cur_id) = match &page.cursor {
            Some((ts, id)) => (Some(ts.clone()), Some(id.clone())),
            None => (None, None),
        };
        let rows = sqlx::query(&sql)
            .bind(room_id)
            .bind(page.correlation_id.as_deref())
            .bind(cur_ts)
            .bind(cur_id)
            .bind(page.limit + 1)
            .fetch_all(&self.pool)
            .await
            .map_err(|e| RepoError(format!("list_room_messages: {e}")))?;
        rows.iter().map(map_message).collect()
    }

    async fn list_projects(&self) -> Result<Vec<ProjectAgg>, RepoError> {
        let agg = sqlx::query(
            "SELECT project_id, count(*)::bigint AS room_count, \
             max(last_message_at)::text AS last_message_at \
             FROM webgui_room GROUP BY project_id \
             ORDER BY max(last_message_at) DESC NULLS LAST, project_id",
        )
        .fetch_all(&self.pool)
        .await
        .map_err(|e| RepoError(format!("list_projects agg: {e}")))?;

        let role_rows = sqlx::query(
            "SELECT project_id, role_id FROM webgui_room ORDER BY project_id",
        )
        .fetch_all(&self.pool)
        .await
        .map_err(|e| RepoError(format!("list_projects roles: {e}")))?;

        use std::collections::HashMap;
        let mut roles_by: HashMap<String, Vec<String>> = HashMap::new();
        for r in &role_rows {
            let pid: String = tg!(r, "project_id");
            let role: String = tg!(r, "role_id");
            roles_by.entry(pid).or_default().push(role);
        }

        agg.iter()
            .map(|r| {
                let project_id: String = tg!(r, "project_id");
                let roles = roles_by.get(&project_id).cloned().unwrap_or_default();
                Ok(ProjectAgg {
                    room_count: tg!(r, "room_count"),
                    last_message_at: tg!(r, "last_message_at"),
                    roles,
                    project_id,
                })
            })
            .collect()
    }

    async fn list_events(
        &self,
        room_id: &str,
        limit: i64,
        correlation_id: Option<&str>,
    ) -> Result<Vec<EventRow>, RepoError> {
        let mut where_clause = String::from("room_id = $1::uuid");
        if correlation_id.is_some() {
            where_clause.push_str(" AND correlation_id = $2::uuid");
        }
        let sql = format!(
            "SELECT event_id::text AS event_id, room_id::text AS room_id, \
             message_id::text AS message_id, correlation_id::text AS correlation_id, \
             event_type, source, hook_provider, hook_event_name, severity, \
             masked_payload_json AS payload, occurred_at::text AS occurred_at, \
             recorded_at::text AS recorded_at \
             FROM webgui_runtime_event WHERE {where_clause} \
             ORDER BY occurred_at DESC LIMIT $3"
        );
        let rows = sqlx::query(&sql)
            .bind(room_id)
            .bind(correlation_id)
            .bind(limit + 1)
            .fetch_all(&self.pool)
            .await
            .map_err(|e| RepoError(format!("list_events: {e}")))?;
        rows.iter().map(map_event).collect()
    }

    async fn updates_since(
        &self,
        room_id: &str,
        after: Option<&str>,
        limit: i64,
    ) -> Result<Vec<MessageRow>, RepoError> {
        let mut where_clause = String::from("room_id = $1::uuid");
        if after.is_some() {
            where_clause.push_str(" AND recorded_at > $2::timestamptz");
        }
        let sql = format!(
            "SELECT {MSG_COLS} FROM webgui_message WHERE {where_clause} \
             ORDER BY recorded_at ASC, message_id ASC LIMIT $3"
        );
        let rows = sqlx::query(&sql)
            .bind(room_id)
            .bind(after)
            .bind(limit)
            .fetch_all(&self.pool)
            .await
            .map_err(|e| RepoError(format!("updates_since: {e}")))?;
        rows.iter().map(map_message).collect()
    }

    async fn mark_read(
        &self,
        room_id: &str,
        read_until: Option<&str>,
    ) -> Result<(Option<String>, i64, String), RepoError> {
        let row = sqlx::query(
            "UPDATE webgui_room SET \
             read_marker_at = COALESCE($2::timestamptz, last_message_at, now()), \
             unread_count = 0, updated_at = now() \
             WHERE room_id = $1::uuid \
             RETURNING read_marker_at::text AS read_marker_at, \
              unread_count::bigint AS unread_count, updated_at::text AS updated_at",
        )
        .bind(room_id)
        .bind(read_until)
        .fetch_one(&self.pool)
        .await
        .map_err(|e| RepoError(format!("mark_read: {e}")))?;
        Ok((
            iso(tg!(&row, "read_marker_at")),
            tg!(&row, "unread_count"),
            iso_str(tg!(&row, "updated_at")),
        ))
    }

    async fn server_now(&self) -> Result<String, RepoError> {
        let row = sqlx::query("SELECT now()::text AS now")
            .fetch_one(&self.pool)
            .await
            .map_err(|e| RepoError(format!("server_now: {e}")))?;
        Ok(iso_str(tg!(&row, "now")))
    }

    async fn set_message_status(&self, message_id: &str, status: &str) -> Result<(), RepoError> {
        sqlx::query(
            "UPDATE webgui_message SET status = $2, updated_at = now() WHERE message_id = $1::uuid",
        )
        .bind(message_id)
        .bind(status)
        .execute(&self.pool)
        .await
        .map_err(|e| RepoError(format!("set_message_status: {e}")))?;
        Ok(())
    }
}
