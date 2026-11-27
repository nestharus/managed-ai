//! Wire encoding helpers aligning `managed-ai-store` with the Python `managed-ai-information`
//! Membus v2 namespace layout (`records/<id>`, `by_kind`, `record_rel`, and Tantivy search docs).
use crate::error::StoreError;
use managed_ai_fs::DEFAULT_MAX_FILE_BYTES;
use managed_ai_protocol::{
    encode_canonical, extract_record_title, strict_parse_json, validate_record_id,
    verify_record_envelope, DEFAULT_MAX_JSON_DEPTH,
};
use membus::{
    GraphEdgeEntry, IndexMutations, OrderedIndexEntry, SearchDocument, SearchLogOp,
    SearchVisibility,
};
use serde_json::{json, Value};

pub const RECORDS_PREFIX: &str = "records/";
pub const IDX_BY_KIND: &str = "by_kind";
pub const IDX_RECORD_REL: &str = "record_rel";
pub const SEALED_CUSTODY_KIND: &str = "managed-ai-sealed-custody/1";
const MAX_SEARCH_BODY_BYTES: usize = 64 * 1024;

pub fn record_key(id: &str) -> Result<Vec<u8>, StoreError> {
    validate_record_id(id)?;
    Ok(format!("{RECORDS_PREFIX}{id}").into_bytes())
}

pub fn record_id_from_key(key: &[u8]) -> Option<&str> {
    let text = std::str::from_utf8(key).ok()?;
    let id = text.strip_prefix(RECORDS_PREFIX)?;
    if validate_record_id(id).is_ok() {
        Some(id)
    } else {
        None
    }
}

pub fn is_sealed_custody_payload(val: &Value) -> bool {
    val.get("format").and_then(Value::as_str) == Some(SEALED_CUSTODY_KIND)
}

pub fn sealed_custody_bytes(id: &str, kind: &str) -> Result<Vec<u8>, StoreError> {
    let payload = json!({
        "format": SEALED_CUSTODY_KIND,
        "id": id,
        "kind": kind,
        "visibility": "Sealed",
    });
    Ok(encode_canonical(&payload)?)
}

pub fn decode_and_verify_record(
    expected_id: &str,
    raw: &[u8],
) -> Result<(Value, Option<String>, bool), StoreError> {
    let parsed = strict_parse_json(raw, DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_JSON_DEPTH)?;
    if is_sealed_custody_payload(&parsed) {
        let actual_id = parsed.get("id").and_then(Value::as_str);
        if actual_id != Some(expected_id) {
            return Err(StoreError::Record(
                managed_ai_protocol::RecordError::IdMismatch {
                    expected: expected_id.to_owned(),
                    actual: actual_id.map(str::to_owned),
                },
            ));
        }
        return Ok((parsed, None, true));
    }
    let sha256 = verify_record_envelope(&parsed, Some(expected_id))?;
    Ok((parsed, Some(sha256), false))
}

pub fn build_record_indexes(id: &str, record: &Value) -> IndexMutations {
    let kind = record
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("record");
    let title = extract_record_title(id, record);
    let active = record
        .get("active")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let sort_key = format!("{kind}\0{id}").into_bytes();
    let summary = json!({
        "id": id,
        "kind": kind,
        "title": title,
        "active": active,
    })
    .to_string()
    .into_bytes();

    let ordered = vec![OrderedIndexEntry {
        index_id: IDX_BY_KIND.to_owned(),
        sort_key,
        summary,
    }];

    let mut edges = Vec::new();
    if let Some(rels) = record.get("relations").and_then(Value::as_array) {
        for rel in rels {
            let Some(obj) = rel.as_object() else {
                continue;
            };
            let Some(label) = obj.get("relation").and_then(Value::as_str) else {
                continue;
            };
            let Some(target) = obj.get("target").and_then(Value::as_str) else {
                continue;
            };
            if label.is_empty() || validate_record_id(target).is_err() {
                continue;
            }
            edges.push(GraphEdgeEntry {
                index_id: IDX_RECORD_REL.to_owned(),
                src_id: id.as_bytes().to_vec(),
                rel_label: label.as_bytes().to_vec(),
                dst_id: target.as_bytes().to_vec(),
                meta: Vec::new(),
            });
        }
    }

    IndexMutations { ordered, edges }
}

pub fn build_search_op(
    namespace: &str,
    id: &str,
    record: &Value,
    visibility: SearchVisibility,
) -> SearchLogOp {
    let doc_id = format!("{RECORDS_PREFIX}{id}");
    if visibility == SearchVisibility::Sealed {
        return SearchLogOp::Delete {
            namespace: namespace.to_owned(),
            doc_id,
        };
    }
    let kind = record
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("record")
        .to_owned();
    let title = extract_record_title(id, record);
    let active = record
        .get("active")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let mut tags = vec![
        kind.clone(),
        if active { "active" } else { "inactive" }.to_owned(),
    ];
    if let Some(extra) = record
        .get("content")
        .and_then(|c| c.get("tags"))
        .and_then(Value::as_array)
    {
        for t in extra {
            if let Some(s) = t.as_str() {
                if !s.is_empty() && !tags.iter().any(|x| x == s) {
                    tags.push(s.to_owned());
                }
            }
        }
    }
    let body = if visibility == SearchVisibility::Public {
        extract_searchable_body(record)
    } else {
        String::new()
    };

    SearchLogOp::Index(SearchDocument {
        namespace: namespace.to_owned(),
        doc_id,
        kind,
        title,
        tags,
        body,
        visibility,
    })
}

fn extract_searchable_body(record: &Value) -> String {
    let Some(content) = record.get("content") else {
        return String::new();
    };
    let mut buf = String::new();
    collect_strings(content, &mut buf, MAX_SEARCH_BODY_BYTES);
    buf
}

fn collect_strings(val: &Value, out: &mut String, limit: usize) {
    if out.len() >= limit {
        return;
    }
    match val {
        Value::String(s) => {
            let trimmed = s.trim();
            if trimmed.is_empty() {
                return;
            }
            if !out.is_empty() {
                out.push('\n');
            }
            let remaining = limit.saturating_sub(out.len());
            if trimmed.len() <= remaining {
                out.push_str(trimmed);
            } else {
                let mut end = remaining;
                while end > 0 && !trimmed.is_char_boundary(end) {
                    end -= 1;
                }
                out.push_str(&trimmed[..end]);
            }
        }
        Value::Array(items) => {
            for item in items {
                collect_strings(item, out, limit);
            }
        }
        Value::Object(map) => {
            for v in map.values() {
                collect_strings(v, out, limit);
            }
        }
        _ => {}
    }
}
