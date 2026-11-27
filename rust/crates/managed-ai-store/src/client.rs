//! High-level async `StoreClient` wrapping `membus::Client` with canonical record sealing,
//! atomic `T_L` lease-guarded indexed transactions, Tantivy search, and filesystem parity.
use crate::codec::{
    build_record_indexes, build_search_op, decode_and_verify_record, is_sealed_custody_payload,
    record_id_from_key, record_key, sealed_custody_bytes, RECORDS_PREFIX,
};
use crate::error::StoreError;
use crate::parity::{export_fs, verify_fs_parity, ExportReport, ParityReport};
use managed_ai_fs::{sha256_hex, DEFAULT_MAX_FILE_BYTES};
use managed_ai_protocol::{
    encode_canonical, extract_record_title, seal_record_digest, strict_parse_json,
    validate_record_id, verify_record_envelope, LexicalMode, ManagedQuery, DEFAULT_MAX_JSON_DEPTH,
};
use membus::{
    Ack, Client, Endpoint, ExpectedVersion, HealthStatus, IndexedMutation, LeaseGrant, LeaseGuard,
    MutationOp, ParityDigestMode, ScanOptions, SearchMode, SearchQuery, SearchVisibility,
    VersionGuard, WriteMutation,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;
use std::net::SocketAddr;
use std::path::Path;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StoredRecordEnvelope {
    pub id: String,
    pub version: u64,
    pub sha256: Option<String>,
    pub raw_sha256: String,
    pub sealed: bool,
    pub record: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PutRecordResult {
    pub id: String,
    pub sha256: Option<String>,
    pub seq: u64,
    pub epoch: u64,
    pub version: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RecordSummary {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub active: bool,
    pub sealed: bool,
    pub sha256: Option<String>,
    pub version: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RecordSearchHit {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub tags: Vec<String>,
    pub score: f32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SearchRecordsResult {
    pub namespace: String,
    pub engine: String,
    pub committed_seq: u64,
    pub total_hits_estimate: usize,
    pub hits: Vec<RecordSearchHit>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NamespaceStatus {
    pub namespace: String,
    pub health: HealthStatus,
    pub record_count: u64,
    pub xor_digest_hex: String,
    pub catalog_schema_version: Option<u32>,
}

#[derive(Clone)]
pub struct StoreClient {
    client: Client,
}

impl StoreClient {
    pub fn from_client(client: Client) -> Self {
        Self { client }
    }

    pub fn inner(&self) -> &Client {
        &self.client
    }

    pub async fn connect_endpoint(endpoint: &Endpoint) -> Result<Self, StoreError> {
        let client = Client::connect(endpoint).await?;
        Ok(Self { client })
    }

    pub async fn connect_tcp(addr: SocketAddr) -> Result<Self, StoreError> {
        let client = Client::connect_tcp(addr).await?;
        Ok(Self { client })
    }

    pub async fn connect_named_pipe(pipe_name: &str) -> Result<Self, StoreError> {
        let client = Client::connect_named_pipe(pipe_name).await?;
        Ok(Self { client })
    }

    pub async fn authenticate(&self, token: &str) -> Result<String, StoreError> {
        Ok(self.client.authenticate(token).await?)
    }

    pub async fn health_v2(&self) -> Result<HealthStatus, StoreError> {
        Ok(self.client.health_v2().await?)
    }

    pub async fn namespace_status(&self, namespace: &str) -> Result<NamespaceStatus, StoreError> {
        let health = self.client.health_v2().await?;
        let digest = self
            .client
            .namespace_digest(namespace, RECORDS_PREFIX.as_bytes(), ParityDigestMode::Full)
            .await?;
        Ok(NamespaceStatus {
            namespace: namespace.to_owned(),
            health,
            record_count: digest.entry_count,
            xor_digest_hex: digest.xor_digest_hex,
            catalog_schema_version: digest.catalog_schema_version,
        })
    }

    pub async fn get_record(
        &self,
        namespace: &str,
        id: &str,
    ) -> Result<Option<StoredRecordEnvelope>, StoreError> {
        let key = record_key(id)?;
        let Some(stored) = self.client.get(namespace, &key).await? else {
            return Ok(None);
        };
        let raw_sha256 = sha256_hex(&stored.value);
        let (record, sha256, sealed) = decode_and_verify_record(id, &stored.value)?;
        Ok(Some(StoredRecordEnvelope {
            id: id.to_owned(),
            version: stored.meta.version,
            sha256,
            raw_sha256,
            sealed,
            record,
        }))
    }

    pub async fn put_record(
        &self,
        namespace: &str,
        record: &mut Value,
        expected: ExpectedVersion,
        lease_guard: Option<LeaseGuard>,
        visibility: SearchVisibility,
        ack: Ack,
    ) -> Result<PutRecordResult, StoreError> {
        let id = record
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| StoreError::InvalidInput("record missing string 'id'".to_owned()))?
            .to_owned();
        validate_record_id(&id)?;
        let key = record_key(&id)?;

        let (payload, sha256, indexes, search_op) = if visibility == SearchVisibility::Sealed {
            let kind = record
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("record");
            (
                sealed_custody_bytes(&id, kind)?,
                None,
                None,
                Some(build_search_op(namespace, &id, record, visibility)),
            )
        } else {
            let digest = seal_record_digest(record)?;
            let bytes = encode_canonical(record)?;
            let indexes = build_record_indexes(&id, record);
            let search_op = build_search_op(namespace, &id, record, visibility);
            (bytes, Some(digest), Some(indexes), Some(search_op))
        };

        let guards = match expected {
            ExpectedVersion::Any => Vec::new(),
            other => vec![VersionGuard {
                namespace: namespace.to_owned(),
                key: key.clone(),
                expected: other,
            }],
        };
        let mutations = vec![IndexedMutation {
            mutation: WriteMutation {
                namespace: namespace.to_owned(),
                key,
                op: MutationOp::Put { value: payload },
            },
            indexes,
            search_op,
        }];

        let commit = match lease_guard {
            Some(lg) => {
                self.client
                    .commit_indexed_tx_with_leases(guards, vec![lg], mutations, ack)
                    .await?
            }
            None => self.client.commit_indexed_tx(guards, mutations, ack).await?,
        };
        let version = commit.versions.first().copied().unwrap_or(0);
        Ok(PutRecordResult {
            id,
            sha256,
            seq: commit.seq,
            epoch: commit.epoch,
            version,
        })
    }

    pub async fn list_records(
        &self,
        namespace: &str,
        limit: usize,
    ) -> Result<Vec<RecordSummary>, StoreError> {
        let capped = limit.clamp(1, 2000);
        let mut out = Vec::new();
        let mut after_key: Option<Vec<u8>> = None;

        while out.len() < capped {
            let page = self
                .client
                .scan_prefix(
                    namespace,
                    RECORDS_PREFIX.as_bytes(),
                    ScanOptions {
                        after_key: after_key.clone(),
                        limit: (capped - out.len()).min(256),
                        max_bytes: 8 * 1024 * 1024,
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
                let sealed = is_sealed_custody_payload(&parsed);
                let sha256 = if sealed {
                    None
                } else {
                    Some(verify_record_envelope(&parsed, Some(rid))?)
                };
                let kind = parsed
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or("record")
                    .to_owned();
                let title = if sealed {
                    rid.to_owned()
                } else {
                    extract_record_title(rid, &parsed)
                };
                let active = parsed
                    .get("active")
                    .and_then(Value::as_bool)
                    .unwrap_or(true);
                out.push(RecordSummary {
                    id: rid.to_owned(),
                    kind,
                    title,
                    active,
                    sealed,
                    sha256,
                    version: rec.meta.version,
                });
                if out.len() >= capped {
                    break;
                }
            }
            if !page.truncated {
                break;
            }
            if let (Some(prev), Some(next)) = (&after_key, &page.next_cursor) {
                if next <= prev {
                    return Err(StoreError::InvalidInput(
                        "scan_prefix cursor did not advance monotonically".to_owned(),
                    ));
                }
            }
            if page.records.is_empty() && page.next_cursor.is_some() {
                return Err(StoreError::InvalidInput(
                    "scan_prefix reported truncated empty page".to_owned(),
                ));
            }
            after_key = page.next_cursor;
            if after_key.is_none() {
                break;
            }
        }
        Ok(out)
    }

    pub async fn search(&self, query: &ManagedQuery) -> Result<SearchRecordsResult, StoreError> {
        query
            .validate()
            .map_err(|msg| StoreError::InvalidInput(msg.to_owned()))?;
        if query.graph.is_some() {
            return Err(StoreError::InvalidInput(
                "graph clause is not supported by lexical search".to_owned(),
            ));
        }
        if query.kinds.iter().any(|k| k.trim().is_empty()) {
            return Err(StoreError::InvalidInput(
                "kind filters must be non-empty".to_owned(),
            ));
        }
        let Some(lex) = &query.lexical else {
            return Err(StoreError::InvalidInput(
                "search requires a lexical clause".to_owned(),
            ));
        };
        let mode = match lex.mode {
            LexicalMode::Bm25 => SearchMode::Bm25,
            LexicalMode::LiteralTrigram | LexicalMode::ExactRef => SearchMode::LiteralSubstring,
        };
        let single_kind = if query.kinds.len() == 1 {
            Some(query.kinds[0].clone())
        } else {
            None
        };
        let fetch_limit = if query.kinds.len() > 1 || query.active_only {
            (query.limit * 4).clamp(50, 200)
        } else {
            (query.limit * 2).min(200)
        };
        let page = self
            .client
            .search(SearchQuery {
                query: lex.query.clone(),
                mode,
                namespace: Some(query.namespace.clone()),
                kind: single_kind,
                limit: fetch_limit,
            })
            .await?;

        let mut seen = HashSet::new();
        let mut hits = Vec::new();
        for h in page.hits {
            let Some(rid) = h.doc_id.strip_prefix(RECORDS_PREFIX) else {
                continue;
            };
            if validate_record_id(rid).is_err() {
                continue;
            }
            if !query.kinds.is_empty() && !query.kinds.iter().any(|k| k == &h.kind) {
                continue;
            }
            if query.active_only && h.tags.iter().any(|t| t == "inactive") {
                continue;
            }
            if !seen.insert(rid.to_owned()) {
                continue;
            }
            hits.push(RecordSearchHit {
                id: rid.to_owned(),
                kind: h.kind,
                title: h.title,
                tags: h.tags,
                score: h.score,
            });
            if hits.len() >= query.limit {
                break;
            }
        }

        let engine = match mode {
            SearchMode::Bm25 => "membus-bm25",
            SearchMode::LiteralSubstring => "membus-literal",
        };
        Ok(SearchRecordsResult {
            namespace: query.namespace.clone(),
            engine: engine.to_owned(),
            committed_seq: page.committed_seq,
            total_hits_estimate: page.total_hits_estimate,
            hits,
        })
    }

    pub async fn verify_fs_parity(
        &self,
        namespace: &str,
        workspace_root: &Path,
    ) -> Result<ParityReport, StoreError> {
        verify_fs_parity(&self.client, namespace, workspace_root).await
    }

    pub async fn export_fs(
        &self,
        namespace: &str,
        out_dir: &Path,
    ) -> Result<ExportReport, StoreError> {
        export_fs(&self.client, namespace, out_dir).await
    }

    pub async fn acquire_lease(
        &self,
        namespace: &str,
        resource: &str,
        holder: &str,
        ttl_ms: u64,
    ) -> Result<LeaseGrant, StoreError> {
        Ok(self
            .client
            .acquire_lease(namespace, resource, holder, ttl_ms)
            .await?)
    }

    pub async fn release_lease(&self, grant: &LeaseGrant) -> Result<(), StoreError> {
        Ok(self
            .client
            .release_lease(
                &grant.namespace,
                &grant.resource,
                &grant.holder,
                grant.generation,
                &grant.token,
            )
            .await?)
    }
}
