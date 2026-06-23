//! WG-ART-04 산출물 변경 이벤트 버퍼 (polling fallback). 레퍼런스: artifact_watcher.py.
//! 단조 seq 커서(ms-floor 복합키 대신 단순화). 응답 data = WS artifact_changed 와 동일.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::Value;

const MAX_PER_PROJECT: usize = 1000;

#[derive(Default)]
pub struct ArtifactChangeBuffer {
    inner: Mutex<HashMap<String, Vec<(u64, Value)>>>,
    seq: AtomicU64,
}

impl ArtifactChangeBuffer {
    pub fn new() -> Self {
        Self::default()
    }

    /// 변경 이벤트 적재. 부여된 seq 반환.
    pub fn push(&self, project_id: &str, data: Value) -> u64 {
        let s = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        let mut map = self.inner.lock().unwrap();
        let v = map.entry(project_id.to_string()).or_default();
        v.push((s, data));
        if v.len() > MAX_PER_PROJECT {
            let drop = v.len() - MAX_PER_PROJECT;
            v.drain(0..drop);
        }
        s
    }

    /// after(seq) 이후 이벤트 → (updates, next_cursor). after 없으면 최신 limit.
    pub fn changes_after(
        &self,
        project_id: &str,
        after: Option<&str>,
        limit: usize,
    ) -> (Vec<Value>, Option<String>) {
        let map = self.inner.lock().unwrap();
        let Some(v) = map.get(project_id) else {
            return (vec![], None);
        };
        let after_seq: u64 = after.and_then(|s| s.parse().ok()).unwrap_or(0);
        let filtered: Vec<&(u64, Value)> = if after.is_none() {
            v.iter().rev().take(limit).collect::<Vec<_>>().into_iter().rev().collect()
        } else {
            v.iter().filter(|(s, _)| *s > after_seq).take(limit).collect()
        };
        let next = filtered.last().map(|(s, _)| s.to_string());
        (filtered.into_iter().map(|(_, d)| d.clone()).collect(), next)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn changes_after_cursor_filters_seen() {
        let b = ArtifactChangeBuffer::new();
        b.push("P", json!({"n":1}));
        b.push("P", json!({"n":2}));
        let (all, cur) = b.changes_after("P", None, 100);
        assert_eq!(all.len(), 2);
        assert_eq!(cur.as_deref(), Some("2"));
        // 같은 커서 이후 = 없음
        let (none, _) = b.changes_after("P", Some("2"), 100);
        assert!(none.is_empty());
        // 새 이벤트 추가 후 after=2 → 새것만
        b.push("P", json!({"n":3}));
        let (fresh, cur2) = b.changes_after("P", Some("2"), 100);
        assert_eq!(fresh.len(), 1);
        assert_eq!(fresh[0]["n"], json!(3));
        assert_eq!(cur2.as_deref(), Some("3"));
        // 타 프로젝트 격리
        assert!(b.changes_after("Q", None, 100).0.is_empty());
    }
}
