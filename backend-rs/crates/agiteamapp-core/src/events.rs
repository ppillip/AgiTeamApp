//! WS 이벤트 발행 포트 (transport 무관). http adapter 가 broadcast hub 로 구현한다.
//! 레퍼런스: Python services/events.py WebSocketHub.publish.

use serde_json::Value;

/// DB commit 이후 update 를 구독자(WebSocket)에게 push 하는 포트.
/// topic = room_id(메시지/이벤트) 또는 "{project_id}:{role}"(runtime_activity 합성키).
pub trait EventPublisher: Send + Sync {
    fn publish(&self, topic: &str, payload: Value, project_id: &str);
}

/// 테스트/비-WS 경로용 no-op.
pub struct NoopPublisher;
impl EventPublisher for NoopPublisher {
    fn publish(&self, _topic: &str, _payload: Value, _project_id: &str) {}
}
