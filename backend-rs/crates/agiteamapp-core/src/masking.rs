//! Secret 마스킹 + tool-call 누출 sanitize. 레퍼런스: services/{masking,sanitizer}.py.
//! 저장 전 1차 마스킹(키/토큰/홈경로) + normalized_text tool-call 마크업 제거.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

pub const MASK: &str = "***MASKED***";

fn home_patterns() -> &'static [Regex] {
    static P: OnceLock<Vec<Regex>> = OnceLock::new();
    P.get_or_init(|| {
        vec![
            Regex::new(r"/Users/[^/\s]+").unwrap(),
            Regex::new(r"/home/[^/\s]+").unwrap(),
            Regex::new(r"[A-Za-z]:\\Users\\[^\\\s]+").unwrap(),
        ]
    })
}

fn secret_patterns() -> &'static [Regex] {
    static P: OnceLock<Vec<Regex>> = OnceLock::new();
    P.get_or_init(|| {
        vec![
            Regex::new(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+").unwrap(),
            Regex::new(r"(?i)\bAuthorization\s*[:=]\s*\S+").unwrap(),
            Regex::new(r"\bsk-[A-Za-z0-9]{8,}").unwrap(),
            Regex::new(r"\bsk-ant-[A-Za-z0-9_\-]{8,}").unwrap(),
            Regex::new(r"\bgh[pousr]_[A-Za-z0-9]{20,}").unwrap(),
            Regex::new(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S+").unwrap(),
            Regex::new(r"\b[A-Za-z0-9]{32,}\b").unwrap(),
        ]
    })
}

/// 홈경로 → ~, secret 패턴 → MASK.
pub fn mask_text(text: Option<&str>) -> Option<String> {
    let t = text?;
    if t.is_empty() {
        return Some(String::new());
    }
    let mut out = t.to_string();
    for p in home_patterns() {
        out = p.replace_all(&out, "~").into_owned();
    }
    for p in secret_patterns() {
        out = p.replace_all(&out, MASK).into_owned();
    }
    Some(out)
}

fn sensitive_key() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)(authorization|api[_-]?key|secret|token|password|passwd|cookie)").unwrap()
    })
}

/// dict/list/str 재귀 마스킹. secret 키 이름이면 값 통째로 MASK.
pub fn mask_payload(payload: &Value) -> Value {
    match payload {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, v) in map {
                if sensitive_key().is_match(k) {
                    out.insert(k.clone(), Value::String(MASK.to_string()));
                } else {
                    out.insert(k.clone(), mask_payload(v));
                }
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(mask_payload).collect()),
        Value::String(s) => Value::String(mask_text(Some(s)).unwrap_or_default()),
        other => other.clone(),
    }
}

fn sanitizer_res() -> &'static (Regex, Regex, Regex, Regex, Regex, Regex) {
    static R: OnceLock<(Regex, Regex, Regex, Regex, Regex, Regex)> = OnceLock::new();
    R.get_or_init(|| {
        (
            Regex::new(r"(?is)<(?:antml:)?function_calls\b.*?</(?:antml:)?function_calls>").unwrap(),
            Regex::new(r"(?is)<(?:antml:)?invoke\b.*?</(?:antml:)?invoke>").unwrap(),
            // look-ahead 미지원 → 다음 줄 들여쓰기+'<' 를 캡처해 보존($1 로 되돌림).
            Regex::new(r"(?im)^[ \t]*course[ \t]*\r?\n([ \t]*<)").unwrap(),
            Regex::new(r"(?i)</?(?:antml:)?(?:invoke|parameter|function_calls|function_results|function)\b[^>]*/?>").unwrap(),
            Regex::new(r"[ \t]+(\r?\n)").unwrap(),
            Regex::new(r"\n{3,}").unwrap(),
        )
    })
}

/// tool-call 누출 마크업 제거 (normalized_text 전용; raw/dedup 미적용).
pub fn sanitize_tool_leak(text: Option<&str>) -> Option<String> {
    let t = text?;
    if t.is_empty() {
        return Some(String::new());
    }
    let (func_block, invoke_block, course, tool_tag, trail_ws, multi_nl) = sanitizer_res();
    let s = course.replace_all(t, "$1");
    let s = func_block.replace_all(&s, "");
    let s = invoke_block.replace_all(&s, "");
    let s = tool_tag.replace_all(&s, "");
    let s = trail_ws.replace_all(&s, "$1");
    let s = multi_nl.replace_all(&s, "\n\n");
    Some(s.trim().to_string())
}
