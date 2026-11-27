//! Membus v2 backed Managed AI Information store client.
//!
//! Provides byte-exact Python-parity record storage (`records/<id>`), atomic lease-guarded
//! indexed transactions (`T_L`), Tantivy BM25 and literal substring search, sealed custody
//! isolation, atomic filesystem export, and filesystem-to-Membus parity verification.
pub mod client;
pub mod codec;
pub mod error;
pub mod parity;

pub use client::{
    NamespaceStatus, PutRecordResult, RecordSearchHit, RecordSummary, SearchRecordsResult,
    StoreClient, StoredRecordEnvelope,
};
pub use codec::{
    build_record_indexes, build_search_op, decode_and_verify_record, is_sealed_custody_payload,
    record_id_from_key, record_key, sealed_custody_bytes, IDX_BY_KIND, IDX_RECORD_REL,
    RECORDS_PREFIX, SEALED_CUSTODY_KIND,
};
pub use error::StoreError;
pub use parity::{ExportReport, ParityMismatch, ParityReport};

#[cfg(test)]
mod tests {
    use super::*;
    use managed_ai_protocol::{LexicalClause, LexicalMode, ManagedQuery};
    use membus::{
        Ack, ExpectedVersion, LeaseGuard, MembusError, MembusServer, RocksDbStorage, SearchConfig,
        SearchEngine, SearchVisibility, ServerConfig, ServerHandle, SignalBroker, SignalConfig,
        SignalPublisher, SignalRing, StorageConfig, StorageEngine,
    };
    use serde_json::json;
    use std::sync::Arc;

    async fn start_test_server() -> (tempfile::TempDir, ServerHandle, StoreClient) {
        let dir = tempfile::tempdir().expect("tempdir");
        let signal_cfg = SignalConfig::default();
        let ring = Arc::new(SignalRing::new(signal_cfg));
        let storage = Arc::new(
            RocksDbStorage::open(
                dir.path(),
                StorageConfig::default(),
                Arc::clone(&ring) as Arc<dyn SignalPublisher>,
            )
            .expect("open storage"),
        );
        let broker = Arc::new(SignalBroker::new(
            Arc::clone(&storage) as Arc<dyn StorageEngine>,
            ring,
            signal_cfg,
        ));
        let search = Arc::new(
            SearchEngine::open(
                dir.path(),
                Arc::clone(&storage) as Arc<dyn StorageEngine>,
                SearchConfig::default(),
            )
            .expect("open search"),
        );
        let server = MembusServer::new(
            Arc::clone(&storage) as Arc<dyn StorageEngine>,
            broker,
            ServerConfig::default(),
        )
        .with_search(search);
        let bound = server
            .bind_tcp("127.0.0.1:0".parse().expect("addr"))
            .await
            .expect("bind tcp");
        let client = StoreClient::connect_endpoint(bound.endpoint())
            .await
            .expect("connect");
        (dir, bound, client)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn put_get_search_sealed_isolation_and_fs_parity_roundtrip() {
        let (_dir, bound, store) = start_test_server().await;
        let ns = "test-workspace";

        let mut public_rec = json!({
            "id": "rec-public-1",
            "kind": "cw-artifact",
            "active": true,
            "content": {
                "title": "Membus Atomic Transaction Design",
                "summary": "Atomic lease-guarded commit verification across workspaces",
                "tags": ["membus", "rust"]
            },
            "relations": [{"relation": "supports", "target": "rec-public-2"}],
            "basis": [],
            "revision": 1
        });
        let put_res = store
            .put_record(
                ns,
                &mut public_rec,
                ExpectedVersion::Absent,
                None,
                SearchVisibility::Public,
                Ack::Flush,
            )
            .await
            .expect("put public record");
        assert!(put_res.sha256.is_some());

        // Sealed record stores custody envelope only and MUST NEVER appear in search results.
        let mut sealed_rec = json!({
            "id": "rec-secret-1",
            "kind": "note",
            "active": true,
            "content": {
                "title": "Confidential Secret Payload",
                "body": "xyzzysupersecretphrase"
            },
            "relations": [],
            "basis": [],
            "revision": 1
        });
        let sealed_put = store
            .put_record(
                ns,
                &mut sealed_rec,
                ExpectedVersion::Absent,
                None,
                SearchVisibility::Sealed,
                Ack::Flush,
            )
            .await
            .expect("put sealed record");
        assert!(sealed_put.sha256.is_none());

        // Point read verifies SHA-256 on public record and detects sealed custody on sealed record.
        let got_pub = store
            .get_record(ns, "rec-public-1")
            .await
            .expect("get public")
            .expect("public exists");
        assert!(!got_pub.sealed);
        assert_eq!(got_pub.sha256, put_res.sha256);

        let got_sealed = store
            .get_record(ns, "rec-secret-1")
            .await
            .expect("get sealed")
            .expect("sealed exists");
        assert!(got_sealed.sealed);
        assert_eq!(got_sealed.record["visibility"], json!("Sealed"));
        assert!(!got_sealed.record.to_string().contains("xyzzysupersecretphrase"));

        // Inactive record must be excluded when active_only=true and included when active_only=false.
        let mut inactive_rec = json!({
            "id": "rec-inactive-1",
            "kind": "cw-artifact",
            "active": false,
            "content": {
                "title": "Archived Atomic Transaction Spec",
                "summary": "Atomic lease-guarded historical draft"
            },
            "relations": [],
            "basis": [],
            "revision": 1
        });
        store
            .put_record(
                ns,
                &mut inactive_rec,
                ExpectedVersion::Absent,
                None,
                SearchVisibility::Public,
                Ack::Flush,
            )
            .await
            .expect("put inactive record");

        // Tantivy BM25 search finds active public record only when active_only=true,
        // includes inactive when active_only=false, and NEVER returns sealed record.
        let search_pub = store
            .search(&ManagedQuery {
                namespace: ns.to_owned(),
                kinds: vec!["cw-artifact".to_owned()],
                active_only: true,
                lexical: Some(LexicalClause {
                    query: "Atomic Transaction".to_owned(),
                    mode: LexicalMode::Bm25,
                }),
                graph: None,
                limit: 10,
                max_bytes: None,
            })
            .await
            .expect("search public");
        assert_eq!(search_pub.hits.len(), 1);
        assert_eq!(search_pub.hits[0].id, "rec-public-1");

        let search_all = store
            .search(&ManagedQuery {
                namespace: ns.to_owned(),
                kinds: vec!["cw-artifact".to_owned()],
                active_only: false,
                lexical: Some(LexicalClause {
                    query: "Atomic Transaction".to_owned(),
                    mode: LexicalMode::Bm25,
                }),
                graph: None,
                limit: 10,
                max_bytes: None,
            })
            .await
            .expect("search including inactive");
        assert_eq!(search_all.hits.len(), 2);

        // Graph clause fails closed on lexical search rather than silently dropping the constraint.
        let err_graph = store
            .search(&ManagedQuery {
                namespace: ns.to_owned(),
                kinds: vec![],
                active_only: true,
                lexical: Some(LexicalClause {
                    query: "Atomic".to_owned(),
                    mode: LexicalMode::Bm25,
                }),
                graph: Some(managed_ai_protocol::GraphClause {
                    anchors: vec!["rec-public-1".to_owned()],
                    direction: managed_ai_protocol::TraversalDirection::Outbound,
                    max_hops: 1,
                    include_labels: vec![],
                    exclude_labels: vec![],
                }),
                limit: 10,
                max_bytes: None,
            })
            .await
            .expect_err("graph clause must fail closed");
        assert!(matches!(err_graph, StoreError::InvalidInput(_)));

        let search_secret = store
            .search(&ManagedQuery {
                namespace: ns.to_owned(),
                kinds: vec![],
                active_only: false,
                lexical: Some(LexicalClause {
                    query: "xyzzysupersecretphrase".to_owned(),
                    mode: LexicalMode::Bm25,
                }),
                graph: None,
                limit: 10,
                max_bytes: None,
            })
            .await
            .expect("search secret");
        assert!(search_secret.hits.is_empty());

        // Export to filesystem and verify byte-exact parity (0 mismatches, sealed counted accurately).
        let export_dir = tempfile::tempdir().expect("export dir");
        let exp = store
            .export_fs(ns, export_dir.path())
            .await
            .expect("export fs");
        assert_eq!(exp.exported_records, 2);
        assert_eq!(exp.skipped_sealed, 1);

        let parity_ok = store
            .verify_fs_parity(ns, export_dir.path())
            .await
            .expect("parity ok");
        assert!(parity_ok.parity_passed, "{parity_ok:?}");
        assert_eq!(parity_ok.matched_records, 2);
        assert_eq!(parity_ok.sealed_records, 1);

        // Nonexistent workspace_root must fail closed rather than falsely passing.
        let missing_root = export_dir.path().join("does-not-exist");
        assert!(store.verify_fs_parity(ns, &missing_root).await.is_err());

        // Altering a local record (re-sealing with different content) causes parity mismatch.
        let local_path = export_dir
            .path()
            .join("work")
            .join("records")
            .join("rec-public-1.json");
        let mut altered = got_pub.record.clone();
        altered["content"]["summary"] = json!("modified locally");
        managed_ai_protocol::seal_record_digest(&mut altered).unwrap();
        managed_ai_fs::write_atomic_bytes(
            &local_path,
            &managed_ai_protocol::encode_canonical(&altered).unwrap(),
        )
        .unwrap();

        let parity_bad = store
            .verify_fs_parity(ns, export_dir.path())
            .await
            .expect("parity bad");
        assert!(!parity_bad.parity_passed);
        assert_eq!(parity_bad.mismatch_count, 1);
        assert_eq!(parity_bad.mismatches[0].reason, "sha256_mismatch");

        bound.shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn lease_guarded_put_record_commits_valid_and_rejects_forged_or_released() {
        let (_dir, bound, store) = start_test_server().await;
        let ns = "fenced-ws";

        let grant = store
            .acquire_lease(ns, "writer/primary", "worker-alpha", 60_000)
            .await
            .expect("acquire lease");
        let guard = LeaseGuard::from_grant(&grant);

        let mut rec = json!({
            "id": "rec-fenced-1",
            "kind": "decision",
            "active": true,
            "content": {"title": "Fenced Commit"},
            "relations": [],
            "basis": [],
            "revision": 1
        });
        let ok = store
            .put_record(
                ns,
                &mut rec,
                ExpectedVersion::Absent,
                Some(guard.clone()),
                SearchVisibility::Public,
                Ack::Flush,
            )
            .await
            .expect("valid lease commit");
        assert_eq!(ok.version, 1);

        // Forged token must abort atomically with LeaseHeld and leave version 1 intact.
        let mut forged = guard.clone();
        forged.token = "0".repeat(64);
        let mut rec2 = rec.clone();
        rec2["revision"] = json!(2);
        let err = store
            .put_record(
                ns,
                &mut rec2,
                ExpectedVersion::Version(1),
                Some(forged),
                SearchVisibility::Public,
                Ack::Flush,
            )
            .await
            .expect_err("forged token must fail");
        assert!(matches!(err, StoreError::Membus(MembusError::LeaseHeld { .. })));

        // Release lease and confirm post-release commit fails with NotFound.
        store.release_lease(&grant).await.expect("release lease");
        let err2 = store
            .put_record(
                ns,
                &mut rec2,
                ExpectedVersion::Version(1),
                Some(guard),
                SearchVisibility::Public,
                Ack::Flush,
            )
            .await
            .expect_err("released lease must fail");
        assert!(matches!(err2, StoreError::Membus(MembusError::NotFound { .. })));

        let stored = store
            .get_record(ns, "rec-fenced-1")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(stored.version, 1);

        bound.shutdown().await;
    }
}
