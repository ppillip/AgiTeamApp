//! agiteamapp-core — 도메인/usecase + 저장 포트(trait). 전송(axum/IPC)·DB 구현 무관.
//!
//! 정책은 전부 여기. http adapter 는 usecase 호출만, db adapter 는 포트 구현만 한다.
//! 레퍼런스(동등성): Python `backend/app/services/{collector_service,runtime_activity_service,
//! hook_normalizer}.py` + `db/{repositories,serializers,models}.py`.

#![allow(async_fn_in_trait)]

pub mod activity;
pub mod artifact_changes;
pub mod artifacts;
pub mod attachments;
pub mod discovery;
pub mod event;
pub mod events;
pub mod hook;
pub mod masking;
pub mod message;
pub mod query;
pub mod repo;
pub mod send;
pub mod transcript;

// 편의 re-export
pub use activity::{collect_runtime_activity, ActivityRegistry, RuntimeActivityCollectRequest};
pub use artifact_changes::ArtifactChangeBuffer;
pub use artifacts::ArtifactService;
pub use attachments::{detect_image, epoch_to_iso, AttachmentService};
pub use discovery::{parse_tree, DiscoveryRegistry};
pub use event::{collect_event, event_to_dict, CollectEventRequest};
pub use events::{EventPublisher, NoopPublisher};
pub use hook::{collect_hook, HookCollectRequest};
pub use masking::{mask_payload, mask_text, sanitize_tool_leak};
pub use message::{collect_message, message_to_dict, CollectMessageRequest};
pub use query::{
    get_message, list_events_uc, list_projects, list_room_messages, list_rooms, mark_read,
    message_update_type, message_updates, room_summary_dict, runtime_status,
};
pub use send::{send_message, MuxPort, PmTarget, SendMessageRequest, PM_ROLE_ID};
pub use transcript::{
    parse_records, store_records, NoopTranscript, TranscriptPort, TranscriptRecord,
};
pub use repo::{
    compute_raw_hash, is_activity_role, normalize_event_type, normalize_provider, ApiError,
    EventRow, MessagePage, MessageRow, NewEvent, NewMessage, ProjectAgg, RepoError, RoomFull,
    RoomRef, RoomRow, TranscriptHint, WebguiRepository,
};

#[cfg(test)]
mod tests {
    use super::*;
    use crate::repo::*;
    use serde_json::json;
    use std::sync::Mutex;

    #[derive(Default)]
    struct FakeRepo {
        events: Mutex<Vec<String>>,
        messages: Mutex<Vec<MessageRow>>,
        room: Mutex<Option<RoomRow>>,
        open_outbound: Mutex<Option<String>>,
        msg_seq: Mutex<u64>,
        last_status: Mutex<Option<String>>,
    }

    impl FakeRepo {
        fn with_room(role_id: &str) -> Self {
            let r = FakeRepo::default();
            *r.room.lock().unwrap() = Some(RoomRow {
                room_id: "room-1".into(),
                project_id: "Panthea".into(),
                role_id: role_id.into(),
                team_session_id: Some("ts-1".into()),
            });
            r
        }
    }

    impl WebguiRepository for FakeRepo {
        async fn upsert_room(
            &self,
            project_id: &str,
            role_id: &str,
            _d: &str,
            _rt: &str,
            _ts: Option<&str>,
            _ag: Option<&str>,
        ) -> Result<RoomRef, RepoError> {
            Ok(RoomRef {
                room_id: "room-1".into(),
                project_id: project_id.into(),
                role_id: role_id.into(),
            })
        }
        async fn get_room(&self, _room_id: &str) -> Result<Option<RoomRow>, RepoError> {
            Ok(self.room.lock().unwrap().clone())
        }
        async fn insert_runtime_event(&self, ev: NewEvent) -> Result<EventRow, RepoError> {
            self.events.lock().unwrap().push(ev.event_type.clone());
            Ok(EventRow {
                event_id: "ev-1".into(),
                room_id: ev.room_id,
                message_id: ev.message_id,
                correlation_id: ev.correlation_id,
                event_type: ev.event_type,
                source: ev.source,
                hook_provider: ev.hook_provider,
                hook_event_name: ev.hook_event_name,
                severity: ev.severity,
                payload: ev.payload_json,
                occurred_at: ev.occurred_at_iso.unwrap_or_else(|| "now".into()),
                recorded_at: "rec".into(),
            })
        }
        async fn find_message_by_record(
            &self,
            _p: &str,
            _r: &str,
        ) -> Result<Option<MessageRow>, RepoError> {
            Ok(None)
        }
        async fn find_message_by_hash(
            &self,
            _room: &str,
            _s: &str,
            _h: &str,
        ) -> Result<Option<MessageRow>, RepoError> {
            Ok(None)
        }
        async fn find_open_outbound_correlation(
            &self,
            _room: &str,
        ) -> Result<Option<String>, RepoError> {
            Ok(self.open_outbound.lock().unwrap().clone())
        }
        async fn create_message(&self, m: NewMessage) -> Result<MessageRow, RepoError> {
            let mut seq = self.msg_seq.lock().unwrap();
            *seq += 1;
            let row = MessageRow {
                message_id: format!("msg-{seq}"),
                room_id: m.room_id,
                correlation_id: m.correlation_id,
                role_id: m.role_id,
                surface_id: m.surface_id,
                agent_session_id: m.agent_session_id,
                team_session_id: m.team_session_id,
                direction: m.direction,
                source: m.source,
                message_type: m.message_type,
                normalized_text: m.normalized_text,
                status: m.status,
                occurred_at: m.occurred_at_iso,
                recorded_at: "rec".into(),
                updated_at: "upd".into(),
            };
            self.messages.lock().unwrap().push(row.clone());
            Ok(row)
        }
        async fn create_message_on_conflict_skip(
            &self,
            m: NewMessage,
        ) -> Result<Option<MessageRow>, RepoError> {
            // 테스트 Fake: 충돌 분기 없이 항상 신규 저장으로 위임.
            Ok(Some(self.create_message(m).await?))
        }
        async fn touch_room_last_message(
            &self,
            _room: &str,
            _msg: &str,
            _at: &str,
            _inbound: bool,
        ) -> Result<(), RepoError> {
            Ok(())
        }
        async fn get_room_full(&self, _room_id: &str) -> Result<Option<RoomFull>, RepoError> {
            Ok(None)
        }
        async fn active_collector_state(&self, _room_id: &str) -> Result<Option<String>, RepoError> {
            Ok(None)
        }
        async fn find_outbound_text_dup(
            &self,
            _room_id: &str,
            _canonical_text: &str,
        ) -> Result<Option<MessageRow>, RepoError> {
            Ok(None)
        }
        async fn list_rooms(&self, _project_id: &str) -> Result<Vec<RoomFull>, RepoError> {
            Ok(vec![])
        }
        async fn get_message(&self, _message_id: &str) -> Result<Option<MessageRow>, RepoError> {
            Ok(None)
        }
        async fn list_room_messages(
            &self,
            _room_id: &str,
            _page: &MessagePage,
        ) -> Result<Vec<MessageRow>, RepoError> {
            Ok(vec![])
        }
        async fn list_projects(&self) -> Result<Vec<ProjectAgg>, RepoError> {
            Ok(vec![])
        }
        async fn list_events(
            &self,
            _room_id: &str,
            _limit: i64,
            _correlation_id: Option<&str>,
        ) -> Result<Vec<EventRow>, RepoError> {
            Ok(vec![])
        }
        async fn updates_since(
            &self,
            _room_id: &str,
            _after: Option<&str>,
            _limit: i64,
        ) -> Result<Vec<MessageRow>, RepoError> {
            Ok(vec![])
        }
        async fn mark_read(
            &self,
            _room_id: &str,
            _read_until: Option<&str>,
        ) -> Result<(Option<String>, i64, String), RepoError> {
            Ok((Some("2026-06-16T00:00:00Z".into()), 0, "2026-06-16T00:00:00Z".into()))
        }
        async fn server_now(&self) -> Result<String, RepoError> {
            Ok("2026-06-16T00:00:00Z".into())
        }
        async fn set_message_status(&self, _message_id: &str, status: &str) -> Result<(), RepoError> {
            *self.last_status.lock().unwrap() = Some(status.to_string());
            Ok(())
        }
    }

    use std::sync::atomic::{AtomicBool, Ordering};
    #[derive(Default)]
    struct FlagTranscript {
        called: AtomicBool,
    }
    impl crate::transcript::TranscriptPort for FlagTranscript {
        async fn collect(&self, _hint: &TranscriptHint) {
            self.called.store(true, Ordering::SeqCst);
        }
    }

    struct FakeMux {
        submitted: bool,
    }
    impl MuxPort for FakeMux {
        async fn resolve_pm(&self, _project_id: &str) -> Result<Option<PmTarget>, ApiError> {
            Ok(Some(PmTarget {
                surface_id: "surface:dummy".into(),
                workspace_id: Some("ws:1".into()),
                display_name: "PM".into(),
            }))
        }
        async fn ping(&self, _t: &PmTarget) -> bool {
            true
        }
        async fn submit(&self, _t: &PmTarget, _text: &str) -> Result<bool, ApiError> {
            Ok(self.submitted)
        }
    }

    // --- hook ---
    #[tokio::test]
    async fn hook_stop_triggers_transcript() {
        let repo = FakeRepo::default();
        let req = HookCollectRequest {
            project_id: "Panthea".into(),
            role: "DeveloperBE".into(),
            display_name: None,
            team_session_id: None,
            agent_id: None,
            hook_provider: Some("claude_code".into()),
            cli: None,
            hook_event_name: "Stop".into(),
            session_id: Some("s".into()),
            transcript_path: None,
            cwd: None,
        };
        let tr = FlagTranscript::default();
        let r = collect_hook(&repo, &NoopPublisher, &tr, req).await.unwrap();
        assert_eq!(r.event_type, "hook_stop");
        assert!(tr.called.load(Ordering::SeqCst));
    }

    // --- runtime-activity ---
    #[test]
    fn activity_rejects_non_active_and_bad_role() {
        let reg = ActivityRegistry::new();
        let base = |activity: &str, role: &str| RuntimeActivityCollectRequest {
            project_id: "Panthea".into(),
            team_session_id: None,
            role: role.into(),
            display_name: None,
            surface_id: None,
            activity: activity.into(),
            reason: None,
            snapshot_hash: "h1".into(),
            snapshot_bytes: None,
            poll_interval_ms: 1000,
            observed_at: "2026-06-16T00:00:00Z".into(),
        };
        assert_eq!(
            collect_runtime_activity(&reg, &NoopPublisher, base("idle", "PM")).unwrap_err().code,
            "invalid_activity"
        );
        assert_eq!(
            collect_runtime_activity(&reg, &NoopPublisher, base("active", "Monitor")).unwrap_err().code,
            "invalid_role"
        );
    }

    #[test]
    fn activity_dedupes_same_hash() {
        let reg = ActivityRegistry::new();
        let mk = || RuntimeActivityCollectRequest {
            project_id: "Panthea".into(),
            team_session_id: None,
            role: "QA".into(),
            display_name: None,
            surface_id: None,
            activity: "active".into(),
            reason: None,
            snapshot_hash: "hX".into(),
            snapshot_bytes: None,
            poll_interval_ms: 1000,
            observed_at: "2026-06-16T00:00:00Z".into(),
        };
        let first = collect_runtime_activity(&reg, &NoopPublisher, mk()).unwrap();
        assert_eq!(first["deduplicated"], json!(false));
        assert_ne!(first["event_id"], json!(null));
        let second = collect_runtime_activity(&reg, &NoopPublisher, mk()).unwrap();
        assert_eq!(second["deduplicated"], json!(true));
        assert_eq!(second["event_id"], json!(null));
    }

    // --- message ---
    #[tokio::test]
    async fn message_role_mismatch_409() {
        let repo = FakeRepo::with_room("DeveloperBE");
        let req = CollectMessageRequest {
            agent_session_id: None,
            role_id: "QA".into(), // 방은 DeveloperBE
            surface_id: None,
            source: "transcript".into(),
            message_type: "assistant_message".into(),
            provider: None,
            transcript_path: None,
            transcript_offset: None,
            transcript_record_id: None,
            raw_text: None,
            normalized_text: "hi".into(),
            raw_hash: None,
            correlation_id: None,
            occurred_at: "2026-06-16T00:00:00Z".into(),
        };
        let err = collect_message(&repo, &NoopPublisher, "room-1", req).await.unwrap_err();
        assert_eq!(err.code, "room_role_mismatch");
        assert_eq!(err.http, 409);
    }

    #[tokio::test]
    async fn message_inbound_unmatched_when_no_open_outbound() {
        let repo = FakeRepo::with_room("DeveloperBE");
        let req = CollectMessageRequest {
            agent_session_id: None,
            role_id: "DeveloperBE".into(),
            surface_id: None,
            source: "transcript".into(),
            message_type: "assistant_message".into(),
            provider: Some("claude_code".into()),
            transcript_path: None,
            transcript_offset: None,
            transcript_record_id: None,
            raw_text: None,
            normalized_text: "hello".into(),
            raw_hash: None,
            correlation_id: None,
            occurred_at: "2026-06-16T00:00:00Z".into(),
        };
        let out = collect_message(&repo, &NoopPublisher, "room-1", req).await.unwrap();
        assert_eq!(out["deduplicated"], json!(false));
        assert_eq!(out["message"]["message_type"], json!("unmatched"));
        assert_eq!(out["message"]["status"], json!("unmatched"));
        assert_eq!(out["message"]["direction"], json!("inbound"));
    }

    #[tokio::test]
    async fn message_invalid_source_422() {
        let repo = FakeRepo::with_room("QA");
        let req = CollectMessageRequest {
            agent_session_id: None,
            role_id: "QA".into(),
            surface_id: None,
            source: "bogus".into(),
            message_type: "status".into(),
            provider: None,
            transcript_path: None,
            transcript_offset: None,
            transcript_record_id: None,
            raw_text: None,
            normalized_text: "x".into(),
            raw_hash: None,
            correlation_id: None,
            occurred_at: "2026-06-16T00:00:00Z".into(),
        };
        let err = collect_message(&repo, &NoopPublisher, "room-1", req).await.unwrap_err();
        assert_eq!(err.code, "invalid_source");
    }

    // --- event ---
    #[tokio::test]
    async fn event_room_not_found_404() {
        let repo = FakeRepo::default(); // room 없음
        let req = CollectEventRequest {
            agent_session_id: None,
            message_id: None,
            correlation_id: None,
            event_type: Some("hook_stop".into()),
            source: "hook".into(),
            hook_provider: None,
            hook_event_name: None,
            severity: "info".into(),
            payload: None,
            occurred_at: "2026-06-16T00:00:00Z".into(),
        };
        let err = collect_event(&repo, &NoopPublisher, &NoopTranscript, "room-x", req).await.unwrap_err();
        assert_eq!(err.code, "room_not_found");
        assert_eq!(err.http, 404);
    }

    // --- masking / sanitize / epoch_to_iso ---
    #[test]
    fn mask_and_sanitize() {
        assert_eq!(mask_text(Some("/Users/ppillip/x")).unwrap(), "~/x");
        assert!(mask_text(Some("token=abcdef123456")).unwrap().contains("***MASKED***"));
        let p = mask_payload(&json!({"api_key":"sk-abc","nested":{"password":"p"},"ok":"v"}));
        assert_eq!(p["api_key"], json!("***MASKED***"));
        assert_eq!(p["nested"]["password"], json!("***MASKED***"));
        assert_eq!(p["ok"], json!("v"));
        // tool-call 누출 마크업(invoke 블록)은 제거되고 산문만 남는다.
        let lt = "<";
        let leak = format!("안녕{lt}invoke name=\"x\">{lt}parameter name=\"a\">1{lt}/parameter>{lt}/invoke> 끝");
        let s = sanitize_tool_leak(Some(&leak)).unwrap();
        assert!(s.contains("안녕"), "got: {s}");
        assert!(s.contains("끝"));
        assert!(!s.contains("invoke"), "마크업 잔존: {s}");
    }

    #[test]
    fn epoch_iso_format() {
        assert_eq!(epoch_to_iso(1_781_766_000), "2026-06-18T07:00:00Z");
        assert_eq!(epoch_to_iso(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn parse_tree_extracts_roles() {
        let tree = concat!(
            "└── workspace ws:2 \"Panthea\" [selected] ◀ active\n",
            "    ├── surface surface:29 [terminal] \"제우스(PM)\" tty=ttys000\n",
            "    ├── surface surface:30 [terminal] \"불칸(BE)\" tty=ttys001\n",
            "    └── surface surface:31 [panel] \"메모(QA)\"\n"
        );
        let projects = parse_tree(tree);
        assert_eq!(projects.len(), 1);
        let p = &projects[0];
        assert_eq!(p.project_id, "Panthea");
        assert!(p.selected);
        // panel(non-terminal) 제외 → PM, DeveloperBE 만
        let roles: Vec<&str> = p.surfaces.iter().map(|s| s.role_id.as_str()).collect();
        assert!(roles.contains(&"PM"));
        assert!(roles.contains(&"DeveloperBE"));
        assert!(!roles.contains(&"QA"));
        let pm = p.surfaces.iter().find(|s| s.role_id == "PM").unwrap();
        assert_eq!(pm.display_name, "제우스");
        assert_eq!(pm.surface_id, "surface:29");
    }

    // --- transcript parser ---
    #[test]
    fn parse_claude_extracts_user_and_assistant() {
        let jsonl = concat!(
            r#"{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-06-16T00:00:00Z","message":{"content":"안녕"}}"#, "\n",
            r#"{"type":"assistant","uuid":"a1","message":{"content":[{"type":"text","text":"답변"},{"type":"tool_use","name":"x"}]}}"#, "\n",
            r#"{"type":"user","uuid":"u2","message":{"content":[{"type":"tool_result","content":"무시"}]}}"#, "\n",
            "broken line {{{"
        );
        let recs = parse_records("claude_code", jsonl);
        assert_eq!(recs.len(), 2); // tool_result-only user 와 깨진 라인 제외
        assert_eq!(recs[0].kind, "user_message");
        assert_eq!(recs[0].text, "안녕");
        assert_eq!(recs[0].record_id.as_deref(), Some("u1"));
        assert_eq!(recs[1].kind, "assistant_message");
        assert_eq!(recs[1].text, "답변"); // tool_use 블록 제외
    }

    // --- send_message (PM 브리지, FakeMux 안전 타깃) ---
    #[tokio::test]
    async fn send_message_pending_to_sent() {
        let repo = FakeRepo::default();
        let mux = FakeMux { submitted: true };
        let req = SendMessageRequest {
            text: "PM에게 보내는 메시지".into(),
            project_id: Some("Panthea".into()),
            client_message_id: Some("cm-1".into()),
        };
        let out = send_message(&repo, &mux, &NoopPublisher, "Panthea", "corr-1", req)
            .await
            .unwrap();
        assert_eq!(out["ack"]["status"], json!("sent"));
        assert_eq!(out["ack"]["send_submitted"], json!(true));
        assert_eq!(out["message"]["status"], json!("sent"));
        assert_eq!(*repo.last_status.lock().unwrap(), Some("sent".to_string()));
        // cmux_send_result event 기록 확인
        assert!(repo.events.lock().unwrap().contains(&"cmux_send_result".to_string()));
    }

    #[tokio::test]
    async fn send_message_empty_422() {
        let repo = FakeRepo::default();
        let mux = FakeMux { submitted: true };
        let req = SendMessageRequest { text: "   ".into(), project_id: None, client_message_id: None };
        let err = send_message(&repo, &mux, &NoopPublisher, "Panthea", "corr-2", req)
            .await
            .unwrap_err();
        assert_eq!(err.code, "empty_message");
        assert_eq!(err.http, 422);
    }

    #[tokio::test]
    async fn send_message_submit_fail_502() {
        let repo = FakeRepo::default();
        let mux = FakeMux { submitted: false };
        let req = SendMessageRequest { text: "x".into(), project_id: None, client_message_id: None };
        let err = send_message(&repo, &mux, &NoopPublisher, "Panthea", "corr-3", req)
            .await
            .unwrap_err();
        assert_eq!(err.code, "send_failed");
        assert_eq!(err.http, 502);
        assert_eq!(*repo.last_status.lock().unwrap(), Some("failed".to_string()));
    }

    #[tokio::test]
    async fn event_hook_normalizes_and_triggers() {
        let repo = FakeRepo::with_room("PM");
        let req = CollectEventRequest {
            agent_session_id: None,
            message_id: None,
            correlation_id: None,
            event_type: None, // hook_provider 로 정규화
            source: "hook".into(),
            hook_provider: Some("claude_code".into()),
            hook_event_name: Some("Stop".into()),
            severity: "info".into(),
            payload: Some(json!({"k":"v"})),
            occurred_at: "2026-06-16T00:00:00Z".into(),
        };
        let tr = FlagTranscript::default();
        let out = collect_event(&repo, &NoopPublisher, &tr, "room-1", req).await.unwrap();
        assert_eq!(out["event"]["event_type"], json!("hook_stop"));
        assert!(tr.called.load(Ordering::SeqCst));
    }
}
