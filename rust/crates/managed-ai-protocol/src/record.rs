//! Record envelope validation, SHA-256 parity digest computation, and title extraction.
use crate::canonical::{encode_canonical, CanonicalError};
use managed_ai_fs::sha256_hex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use thiserror::Error;

pub const RECORD_FORMAT: &str = "g60-agent-system/0.1";
pub const MAX_RECORD_ID_LEN: usize = 200;

#[derive(Debug, Error)]
pub enum RecordError {
    #[error("invalid record id {id:?}: {reason}")]
    InvalidId { id: String, reason: &'static str },
    #[error("record root must be a JSON object")]
    NotObject,
    #[error("unsupported or missing record format: expected {expected}, got {actual:?}")]
    FormatMismatch { expected: &'static str, actual: Option<String> },
    #[error("record id mismatch: expected {expected:?}, got {actual:?}")]
    IdMismatch { expected: String, actual: Option<String> },
    #[error("record digest mismatch: stored {stored:?} != computed {computed}")]
    DigestMismatch { stored: Option<String>, computed: String },
    #[error(transparent)]
    Canonical(#[from] CanonicalError),
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecordRelation {
    pub relation: String,
    pub target: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct BasisRef {
    pub path: String,
    pub sha256: Option<String>,
    pub bytes: u64,
}

/// Validate a record identifier against traversal, NUL, leading/trailing dots,
/// Windows reserved device names, and length limits (matching Python `validate_identifier`).
pub fn validate_record_id(id: &str) -> Result<(), RecordError> {
    if id.is_empty() || id.len() > MAX_RECORD_ID_LEN {
        return Err(RecordError::InvalidId {
            id: id.to_owned(),
            reason: "length must be in 1..=200",
        });
    }
    if id.starts_with('.')
        || id.ends_with('.')
        || id.contains("..")
        || id.contains('/')
        || id.contains('\\')
        || id.contains('\0')
    {
        return Err(RecordError::InvalidId {
            id: id.to_owned(),
            reason: "path separator, dot-boundary, or control character refused",
        });
    }
    if !id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
    {
        return Err(RecordError::InvalidId {
            id: id.to_owned(),
            reason: "only ASCII alphanumeric, '-', '_', '.' allowed",
        });
    }
    let stem = id.split('.').next().unwrap_or(id).to_ascii_uppercase();
    if matches!(
        stem.as_str(),
        "CON"
            | "PRN"
            | "AUX"
            | "NUL"
            | "COM1"
            | "COM2"
            | "COM3"
            | "COM4"
            | "COM5"
            | "COM6"
            | "COM7"
            | "COM8"
            | "COM9"
            | "LPT1"
            | "LPT2"
            | "LPT3"
            | "LPT4"
            | "LPT5"
            | "LPT6"
            | "LPT7"
            | "LPT8"
            | "LPT9"
    ) {
        return Err(RecordError::InvalidId {
            id: id.to_owned(),
            reason: "reserved device name refused",
        });
    }
    Ok(())
}

/// Compute the canonical SHA-256 digest of a `g60-agent-system/0.1` record object
/// (exact parity with Python: `sha(encode({k: v for k, v in record.items() if k != "sha256"}))`).
pub fn compute_record_digest(record: &Value) -> Result<String, RecordError> {
    let Some(obj) = record.as_object() else {
        return Err(RecordError::NotObject);
    };
    let mut stripped = Map::with_capacity(obj.len());
    for (k, v) in obj {
        if k != "sha256" {
            stripped.insert(k.clone(), v.clone());
        }
    }
    let bytes = encode_canonical(&Value::Object(stripped))?;
    Ok(sha256_hex(&bytes))
}

/// Verify format, id (when provided), and exact SHA-256 digest of a record JSON object.
pub fn verify_record_envelope(
    record: &Value,
    expected_id: Option<&str>,
) -> Result<String, RecordError> {
    let Some(obj) = record.as_object() else {
        return Err(RecordError::NotObject);
    };
    let fmt = obj.get("format").and_then(Value::as_str);
    if fmt != Some(RECORD_FORMAT) {
        return Err(RecordError::FormatMismatch {
            expected: RECORD_FORMAT,
            actual: fmt.map(str::to_owned),
        });
    }
    let actual_id = obj.get("id").and_then(Value::as_str);
    if let Some(exp) = expected_id {
        if actual_id != Some(exp) {
            return Err(RecordError::IdMismatch {
                expected: exp.to_owned(),
                actual: actual_id.map(str::to_owned),
            });
        }
    }
    if let Some(rid) = actual_id {
        validate_record_id(rid)?;
    } else {
        return Err(RecordError::InvalidId {
            id: String::new(),
            reason: "missing id field",
        });
    }
    let computed = compute_record_digest(record)?;
    let stored = obj.get("sha256").and_then(Value::as_str);
    if stored != Some(computed.as_str()) {
        return Err(RecordError::DigestMismatch {
            stored: stored.map(str::to_owned),
            computed,
        });
    }
    Ok(computed)
}

/// Compute and attach `"sha256"` (and ensure `"format"`) on a mutable record JSON object.
pub fn seal_record_digest(record: &mut Value) -> Result<String, RecordError> {
    let Some(obj) = record.as_object_mut() else {
        return Err(RecordError::NotObject);
    };
    match obj.get("format") {
        Some(val) => {
            if val.as_str() != Some(RECORD_FORMAT) {
                return Err(RecordError::FormatMismatch {
                    expected: RECORD_FORMAT,
                    actual: val.as_str().map(str::to_owned),
                });
            }
        }
        None => {
            obj.insert("format".to_owned(), Value::String(RECORD_FORMAT.to_owned()));
        }
    }
    let digest = compute_record_digest(&Value::Object(obj.clone()))?;
    obj.insert("sha256".to_owned(), Value::String(digest.clone()));
    Ok(digest)
}

/// Extract a human-readable title for indexing/display (matching Python `extract_record_title`).
pub fn extract_record_title(id: &str, record: &Value) -> String {
    if let Some(content) = record.get("content").and_then(Value::as_object) {
        for key in ["title", "name", "heading", "summary", "question"] {
            if let Some(text) = content.get(key).and_then(Value::as_str) {
                let trimmed = text.trim();
                if !trimmed.is_empty() {
                    return trimmed.chars().take(160).collect();
                }
            }
        }
    }
    id.to_owned()
}
