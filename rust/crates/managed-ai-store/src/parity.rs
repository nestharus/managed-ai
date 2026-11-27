//! Byte-exact filesystem <-> Membus v2 parity verification and atomic export.
use crate::codec::{is_sealed_custody_payload, record_id_from_key, RECORDS_PREFIX};
use crate::error::StoreError;
use managed_ai_fs::{
    ensure_ordinary_dir, is_ordinary_dir_metadata, is_ordinary_metadata, read_ordinary_bytes,
    sha256_hex, write_atomic_bytes, DEFAULT_MAX_FILE_BYTES,
};
use managed_ai_protocol::{
    strict_parse_json, validate_record_id, verify_record_envelope, DEFAULT_MAX_JSON_DEPTH,
};
use membus::{Client, ScanOptions};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

const SCAN_PAGE_LIMIT: usize = 256;
const SCAN_PAGE_MAX_BYTES: usize = 8 * 1024 * 1024;
const MAX_REPORTED_MISMATCHES: usize = 50;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ParityMismatch {
    pub record_id: String,
    pub reason: String,
    pub fs_sha256: Option<String>,
    pub membus_sha256: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ParityReport {
    pub namespace: String,
    pub fs_records: usize,
    pub membus_records: usize,
    pub matched_records: usize,
    pub sealed_records: usize,
    pub mismatch_count: usize,
    pub parity_passed: bool,
    pub mismatches: Vec<ParityMismatch>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExportReport {
    pub namespace: String,
    pub exported_records: usize,
    pub skipped_sealed: usize,
    pub snapshot_seq: u64,
}

pub async fn verify_fs_parity(
    client: &Client,
    namespace: &str,
    workspace_root: &Path,
) -> Result<ParityReport, StoreError> {
    let fs_digests = scan_local_record_files(workspace_root)?;
    let (membus_digests, sealed_ids) = scan_membus_record_digests(client, namespace).await?;

    let mut matched = 0usize;
    let mut mismatch_count = 0usize;
    let mut mismatches = Vec::new();

    for (rid, fs_sha) in &fs_digests {
        if sealed_ids.contains_key(rid) {
            continue;
        }
        match membus_digests.get(rid) {
            Some(mb_sha) if mb_sha == fs_sha => matched += 1,
            Some(mb_sha) => {
                mismatch_count += 1;
                if mismatches.len() < MAX_REPORTED_MISMATCHES {
                    mismatches.push(ParityMismatch {
                        record_id: rid.clone(),
                        reason: "sha256_mismatch".to_owned(),
                        fs_sha256: Some(fs_sha.clone()),
                        membus_sha256: Some(mb_sha.clone()),
                    });
                }
            }
            None => {
                mismatch_count += 1;
                if mismatches.len() < MAX_REPORTED_MISMATCHES {
                    mismatches.push(ParityMismatch {
                        record_id: rid.clone(),
                        reason: "missing_in_membus".to_owned(),
                        fs_sha256: Some(fs_sha.clone()),
                        membus_sha256: None,
                    });
                }
            }
        }
    }

    for (rid, mb_sha) in &membus_digests {
        if !fs_digests.contains_key(rid) {
            mismatch_count += 1;
            if mismatches.len() < MAX_REPORTED_MISMATCHES {
                mismatches.push(ParityMismatch {
                    record_id: rid.clone(),
                    reason: "extra_in_membus".to_owned(),
                    fs_sha256: None,
                    membus_sha256: Some(mb_sha.clone()),
                });
            }
        }
    }

    Ok(ParityReport {
        namespace: namespace.to_owned(),
        fs_records: fs_digests.len(),
        membus_records: membus_digests.len() + sealed_ids.len(),
        matched_records: matched,
        sealed_records: sealed_ids.len(),
        mismatch_count,
        parity_passed: mismatch_count == 0,
        mismatches,
    })
}

pub async fn export_fs(
    client: &Client,
    namespace: &str,
    out_dir: &Path,
) -> Result<ExportReport, StoreError> {
    let work_dir = out_dir.join("work");
    let records_dir = work_dir.join("records");
    fs::create_dir_all(&records_dir).map_err(|source| StoreError::DirIo {
        path: records_dir.clone(),
        source,
    })?;
    ensure_ordinary_dir(out_dir)?;
    ensure_ordinary_dir(&work_dir)?;
    ensure_ordinary_dir(&records_dir)?;

    let mut after_key: Option<Vec<u8>> = None;
    let mut exported = 0usize;
    let mut skipped_sealed = 0usize;
    let mut snapshot_seq;

    loop {
        let page = client
            .scan_prefix(
                namespace,
                RECORDS_PREFIX.as_bytes(),
                ScanOptions {
                    after_key: after_key.clone(),
                    limit: SCAN_PAGE_LIMIT,
                    max_bytes: SCAN_PAGE_MAX_BYTES,
                },
                None,
            )
            .await?;
        snapshot_seq = page.snapshot_seq;
        for rec in &page.records {
            let Some(rid) = record_id_from_key(&rec.key) else {
                continue;
            };
            let parsed =
                strict_parse_json(&rec.value, DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_JSON_DEPTH)?;
            if is_sealed_custody_payload(&parsed) {
                skipped_sealed += 1;
                continue;
            }
            verify_record_envelope(&parsed, Some(rid))?;
            let target = records_dir.join(format!("{rid}.json"));
            write_atomic_bytes(&target, &rec.value)?;
            exported += 1;
        }
        if !page.truncated {
            break;
        }
        after_key = advance_scan_cursor(after_key, page.next_cursor, page.records.is_empty())?;
        if after_key.is_none() {
            break;
        }
    }

    Ok(ExportReport {
        namespace: namespace.to_owned(),
        exported_records: exported,
        skipped_sealed,
        snapshot_seq,
    })
}

fn advance_scan_cursor(
    prev: Option<Vec<u8>>,
    next: Option<Vec<u8>>,
    page_empty: bool,
) -> Result<Option<Vec<u8>>, StoreError> {
    match (&prev, &next) {
        (Some(p), Some(n)) if n <= p => Err(StoreError::InvalidInput(
            "scan_prefix cursor did not advance monotonically".to_owned(),
        )),
        (_, Some(_)) if page_empty => Err(StoreError::InvalidInput(
            "scan_prefix reported truncated empty page".to_owned(),
        )),
        _ => Ok(next),
    }
}

fn check_optional_ordinary_dir(path: &Path) -> Result<bool, StoreError> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if !is_ordinary_dir_metadata(&meta) {
                return Err(StoreError::InvalidInput(format!(
                    "refused non-ordinary directory: {}",
                    path.display()
                )));
            }
            Ok(true)
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(source) => Err(StoreError::DirIo {
            path: path.to_path_buf(),
            source,
        }),
    }
}

fn scan_local_record_files(workspace_root: &Path) -> Result<BTreeMap<String, String>, StoreError> {
    ensure_ordinary_dir(workspace_root)?;
    let work_dir = workspace_root.join("work");
    let mut out = BTreeMap::new();
    if !check_optional_ordinary_dir(&work_dir)? {
        return Ok(out);
    }
    let records_dir = work_dir.join("records");
    if !check_optional_ordinary_dir(&records_dir)? {
        return Ok(out);
    }
    let entries = fs::read_dir(&records_dir).map_err(|source| StoreError::DirIo {
        path: records_dir.clone(),
        source,
    })?;
    for entry in entries {
        let entry = entry.map_err(|source| StoreError::DirIo {
            path: records_dir.clone(),
            source,
        })?;
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if name.starts_with('.') {
            continue;
        }
        let lmeta = fs::symlink_metadata(&path).map_err(|source| StoreError::DirIo {
            path: path.clone(),
            source,
        })?;
        if !is_ordinary_metadata(&lmeta) {
            return Err(StoreError::InvalidInput(format!(
                "refused non-ordinary entry in work/records: {}",
                path.display()
            )));
        }
        let Some(rid) = name.strip_suffix(".json") else {
            continue;
        };
        validate_record_id(rid)?;
        let raw = read_ordinary_bytes(&path, DEFAULT_MAX_FILE_BYTES)?;
        let parsed = strict_parse_json(&raw, DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_JSON_DEPTH)?;
        if is_sealed_custody_payload(&parsed) {
            continue;
        }
        verify_record_envelope(&parsed, Some(rid))?;
        out.insert(rid.to_owned(), sha256_hex(&raw));
    }
    Ok(out)
}

async fn scan_membus_record_digests(
    client: &Client,
    namespace: &str,
) -> Result<(BTreeMap<String, String>, BTreeMap<String, ()>), StoreError> {
    let mut digests = BTreeMap::new();
    let mut sealed = BTreeMap::new();
    let mut after_key: Option<Vec<u8>> = None;

    loop {
        let page = client
            .scan_prefix(
                namespace,
                RECORDS_PREFIX.as_bytes(),
                ScanOptions {
                    after_key: after_key.clone(),
                    limit: SCAN_PAGE_LIMIT,
                    max_bytes: SCAN_PAGE_MAX_BYTES,
                },
                None,
            )
            .await?;
        for rec in &page.records {
            let Some(rid) = record_id_from_key(&rec.key) else {
                continue;
            };
            let parsed =
                strict_parse_json(&rec.value, DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_JSON_DEPTH)?;
            if is_sealed_custody_payload(&parsed) {
                sealed.insert(rid.to_owned(), ());
            } else {
                verify_record_envelope(&parsed, Some(rid))?;
                digests.insert(rid.to_owned(), sha256_hex(&rec.value));
            }
        }
        if !page.truncated {
            break;
        }
        after_key = advance_scan_cursor(after_key, page.next_cursor, page.records.is_empty())?;
        if after_key.is_none() {
            break;
        }
    }

    Ok((digests, sealed))
}
