//! Byte-exact Python-parity canonical JSON encoding and strict duplicate-key-refusing parsing.
//!
//! Matches Python's:
//! `json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"`
//! down to the exact byte and SHA-256 digest.
use serde::de::{DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde::Deserializer;
use serde_json::{Map, Number, Value};
use std::collections::BTreeMap;
use std::fmt;
use thiserror::Error;

pub const DEFAULT_MAX_JSON_DEPTH: usize = 64;

#[derive(Debug, Error)]
pub enum CanonicalError {
    #[error("JSON input exceeds maximum size of {limit} bytes")]
    InputTooLarge { limit: usize },
    #[error("JSON nesting depth exceeds maximum of {limit}")]
    DepthExceeded { limit: usize },
    #[error("duplicate JSON object key: {key:?}")]
    DuplicateKey { key: String },
    #[error("non-finite floating point number refused by allow_nan=False")]
    NonFiniteFloat,
    #[error("invalid JSON: {0}")]
    Parse(String),
}

/// Encode `value` into byte-exact Python-parity canonical JSON (`sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False` + `\n`).
pub fn encode_canonical(value: &Value) -> Result<Vec<u8>, CanonicalError> {
    let mut out = String::with_capacity(256);
    write_value(value, 0, &mut out)?;
    out.push('\n');
    Ok(out.into_bytes())
}

fn write_indent(depth: usize, out: &mut String) {
    for _ in 0..depth {
        out.push_str("  ");
    }
}

fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000C}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let code = c as u32;
                let _ = fmt::Write::write_fmt(out, format_args!("\\u{code:04x}"));
            }
            // Python `ensure_ascii=False` preserves printable Unicode scalars verbatim as UTF-8.
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_value(value: &Value, depth: usize, out: &mut String) -> Result<(), CanonicalError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                let _ = fmt::Write::write_fmt(out, format_args!("{i}"));
            } else if let Some(u) = n.as_u64() {
                let _ = fmt::Write::write_fmt(out, format_args!("{u}"));
            } else if let Some(f) = n.as_f64() {
                if !f.is_finite() {
                    return Err(CanonicalError::NonFiniteFloat);
                }
                format_python_float(&n.to_string(), out);
            } else {
                return Err(CanonicalError::NonFiniteFloat);
            }
        }
        Value::String(s) => write_json_string(s, out),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
            } else {
                out.push_str("[\n");
                for (idx, item) in items.iter().enumerate() {
                    write_indent(depth + 1, out);
                    write_value(item, depth + 1, out)?;
                    if idx + 1 < items.len() {
                        out.push(',');
                    }
                    out.push('\n');
                }
                write_indent(depth, out);
                out.push(']');
            }
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
            } else {
                // Sort keys lexicographically by Unicode scalar (matching Python `sort_keys=True`).
                let sorted: BTreeMap<&str, &Value> =
                    map.iter().map(|(k, v)| (k.as_str(), v)).collect();
                out.push_str("{\n");
                let total = sorted.len();
                for (idx, (k, v)) in sorted.into_iter().enumerate() {
                    write_indent(depth + 1, out);
                    write_json_string(k, out);
                    out.push_str(": ");
                    write_value(v, depth + 1, out)?;
                    if idx + 1 < total {
                        out.push(',');
                    }
                    out.push('\n');
                }
                write_indent(depth, out);
                out.push('}');
            }
        }
    }
    Ok(())
}

/// Strictly parse UTF-8 JSON bytes, refusing duplicate object keys at any depth,
/// oversized inputs, excessive nesting depth, and trailing non-whitespace tokens.
pub fn strict_parse_json(
    bytes: &[u8],
    max_bytes: usize,
    max_depth: usize,
) -> Result<Value, CanonicalError> {
    if bytes.len() > max_bytes {
        return Err(CanonicalError::InputTooLarge { limit: max_bytes });
    }
    let text = std::str::from_utf8(bytes)
        .map_err(|e| CanonicalError::Parse(format!("invalid UTF-8: {e}")))?;
    let mut de = serde_json::Deserializer::from_str(text);
    let seed = StrictSeed {
        depth: 0,
        max_depth,
    };
    let val = seed
        .deserialize(&mut de)
        .map_err(|e| map_serde_err(e, max_depth))?;
    de.end().map_err(|e| map_serde_err(e, max_depth))?;
    Ok(val)
}

fn format_python_float(raw: &str, out: &mut String) {
    let (sign, rest) = match raw.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", raw),
    };
    // Python switches to scientific notation below 1e-4, whereas serde_json/zmij uses fixed point for 1e-5.
    if let Some(after_zeros) = rest.strip_prefix("0.0000") {
        let mut chars = after_zeros.chars();
        if let Some(first) = chars.next().filter(|c| c.is_ascii_digit() && *c != '0') {
            out.push_str(sign);
            out.push(first);
            let tail: &str = chars.as_str();
            if !tail.is_empty() {
                out.push('.');
                out.push_str(tail);
            }
            out.push_str("e-05");
            return;
        }
    }
    if let Some((mantissa, exp)) = rest.split_once(['e', 'E']) {
        let (exp_sign, digits) = if let Some(d) = exp.strip_prefix('-') {
            ('-', d)
        } else if let Some(d) = exp.strip_prefix('+') {
            ('+', d)
        } else {
            ('+', exp)
        };
        out.push_str(sign);
        out.push_str(mantissa);
        out.push('e');
        out.push(exp_sign);
        if digits.len() == 1 {
            out.push('0');
        }
        out.push_str(digits);
        return;
    }
    out.push_str(raw);
}

fn map_serde_err(err: serde_json::Error, max_depth: usize) -> CanonicalError {
    let msg = err.to_string();
    let core = msg
        .rsplit_once(" at line ")
        .map_or(msg.as_str(), |(head, _)| head);
    if let Some(rest) = core.strip_prefix("DUP_KEY:") {
        return CanonicalError::DuplicateKey {
            key: rest.to_owned(),
        };
    }
    if let Some(rest) = core.strip_prefix("MAX_DEPTH:") {
        if let Ok(limit) = rest.parse::<usize>() {
            return CanonicalError::DepthExceeded { limit };
        }
    }
    if core.contains("recursion limit exceeded") {
        return CanonicalError::DepthExceeded {
            limit: max_depth.min(128),
        };
    }
    CanonicalError::Parse(msg)
}

#[derive(Clone, Copy)]
struct StrictSeed {
    depth: usize,
    max_depth: usize,
}

impl<'de> DeserializeSeed<'de> for StrictSeed {
    type Value = Value;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Value, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for StrictSeed {
    type Value = Value;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("any valid JSON value without duplicate object keys")
    }

    fn visit_bool<E>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }

    fn visit_i64<E>(self, v: i64) -> Result<Value, E> {
        Ok(Value::Number(Number::from(v)))
    }

    fn visit_u64<E>(self, v: u64) -> Result<Value, E> {
        Ok(Value::Number(Number::from(v)))
    }

    fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Value, E> {
        Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("non-finite float"))
    }

    fn visit_str<E>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_owned()))
    }

    fn visit_string<E>(self, v: String) -> Result<Value, E> {
        Ok(Value::String(v))
    }

    fn visit_unit<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        use serde::de::Error;
        if self.depth >= self.max_depth {
            return Err(A::Error::custom(format!("MAX_DEPTH:{}", self.max_depth)));
        }
        let child = StrictSeed {
            depth: self.depth + 1,
            max_depth: self.max_depth,
        };
        let mut out = Vec::new();
        while let Some(elem) = seq.next_element_seed(child)? {
            out.push(elem);
        }
        Ok(Value::Array(out))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        use serde::de::Error;
        if self.depth >= self.max_depth {
            return Err(A::Error::custom(format!("MAX_DEPTH:{}", self.max_depth)));
        }
        let child = StrictSeed {
            depth: self.depth + 1,
            max_depth: self.max_depth,
        };
        let mut out = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if out.contains_key(&key) {
                return Err(A::Error::custom(format!("DUP_KEY:{key}")));
            }
            let val = map.next_value_seed(child)?;
            out.insert(key, val);
        }
        Ok(Value::Object(out))
    }
}
