//! Native Rust CLI (`managed-ai-rs`) for Managed AI Information on Membus v2.
mod args;

use args::{parse_args, usage, Command, ConnectionOpts, EndpointTarget};
use managed_ai_fs::{read_ordinary_bytes, sha256_hex, DEFAULT_MAX_FILE_BYTES};
use managed_ai_protocol::{
    encode_canonical, strict_parse_json, verify_record_envelope, LexicalClause, ManagedQuery,
    DEFAULT_MAX_JSON_DEPTH,
};
use managed_ai_store::StoreClient;
use serde_json::json;
use std::path::Path;
use std::process::ExitCode;
use std::time::Duration;

#[tokio::main]
async fn main() -> ExitCode {
    let cmd = match parse_args(std::env::args()) {
        Ok(c) => c,
        Err(msg) => {
            eprintln!("error: {msg}");
            return ExitCode::from(2);
        }
    };

    if matches!(cmd, Command::Help) {
        print!("{}", usage());
        return ExitCode::SUCCESS;
    }

    match dispatch(cmd).await {
        Ok(exit_code) => exit_code,
        Err(msg) => {
            eprintln!("error: {msg}");
            ExitCode::FAILURE
        }
    }
}

async fn connect(conn: &ConnectionOpts) -> Result<StoreClient, String> {
    let timeout = Duration::from_secs(conn.timeout_secs);
    let client = tokio::time::timeout(timeout, async {
        match &conn.endpoint {
            EndpointTarget::NamedPipe(pipe) => StoreClient::connect_named_pipe(pipe).await,
            EndpointTarget::Tcp(addr) => StoreClient::connect_tcp(*addr).await,
        }
    })
    .await
    .map_err(|_| format!("connect timed out after {}s", conn.timeout_secs))?
    .map_err(|e| e.to_string())?;

    if let Some(env_var) = &conn.token_env {
        let token = std::env::var(env_var)
            .map_err(|_| format!("token env variable {env_var:?} is not set"))?;
        if token.trim().is_empty() {
            return Err(format!("token env variable {env_var:?} is empty"));
        }
        tokio::time::timeout(timeout, client.authenticate(&token))
            .await
            .map_err(|_| format!("authenticate timed out after {}s", conn.timeout_secs))?
            .map_err(|e| e.to_string())?;
    }
    Ok(client)
}

async fn run_timed<F, T, E>(timeout_secs: u64, label: &str, fut: F) -> Result<T, String>
where
    F: std::future::Future<Output = Result<T, E>>,
    E: std::fmt::Display,
{
    tokio::time::timeout(Duration::from_secs(timeout_secs), fut)
        .await
        .map_err(|_| format!("{label} timed out after {timeout_secs}s"))?
        .map_err(|e| e.to_string())
}

fn print_json<T: serde::Serialize>(val: &T) -> Result<(), String> {
    let text = serde_json::to_string_pretty(val).map_err(|e| e.to_string())?;
    println!("{text}");
    Ok(())
}

async fn dispatch(cmd: Command) -> Result<ExitCode, String> {
    match cmd {
        Command::Help => Ok(ExitCode::SUCCESS),
        Command::Health(conn) => {
            let client = connect(&conn).await?;
            let status = run_timed(conn.timeout_secs, "health", client.health_v2()).await?;
            print_json(&status)?;
            Ok(ExitCode::SUCCESS)
        }
        Command::Status { conn, namespace } => {
            let client = connect(&conn).await?;
            let status = run_timed(
                conn.timeout_secs,
                "status",
                client.namespace_status(&namespace),
            )
            .await?;
            print_json(&status)?;
            Ok(ExitCode::SUCCESS)
        }
        Command::Get {
            conn,
            namespace,
            id,
        } => {
            let client = connect(&conn).await?;
            let rec = run_timed(conn.timeout_secs, "get", client.get_record(&namespace, &id))
                .await?;
            match rec {
                Some(env) => {
                    print_json(&env)?;
                    Ok(ExitCode::SUCCESS)
                }
                None => {
                    print_json(&json!({"found": false, "namespace": namespace, "id": id}))?;
                    Ok(ExitCode::from(3))
                }
            }
        }
        Command::List {
            conn,
            namespace,
            limit,
        } => {
            let client = connect(&conn).await?;
            let items = run_timed(
                conn.timeout_secs,
                "list",
                client.list_records(&namespace, limit),
            )
            .await?;
            print_json(&json!({"namespace": namespace, "count": items.len(), "records": items}))?;
            Ok(ExitCode::SUCCESS)
        }
        Command::Search {
            conn,
            namespace,
            query,
            kind,
            mode,
            limit,
        } => {
            let client = connect(&conn).await?;
            let mq = ManagedQuery {
                namespace,
                kinds: kind.into_iter().collect(),
                active_only: true,
                lexical: Some(LexicalClause { query, mode }),
                graph: None,
                limit,
                max_bytes: None,
            };
            let res = run_timed(conn.timeout_secs, "search", client.search(&mq)).await?;
            print_json(&res)?;
            Ok(ExitCode::SUCCESS)
        }
        Command::VerifyParity {
            conn,
            namespace,
            workspace_root,
        } => {
            let client = connect(&conn).await?;
            let report = run_timed(
                conn.timeout_secs,
                "verify-parity",
                client.verify_fs_parity(&namespace, &workspace_root),
            )
            .await?;
            let ok = report.parity_passed;
            print_json(&report)?;
            Ok(if ok {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(4)
            })
        }
        Command::ExportFs {
            conn,
            namespace,
            out_dir,
        } => {
            let client = connect(&conn).await?;
            let report = run_timed(
                conn.timeout_secs,
                "export-fs",
                client.export_fs(&namespace, &out_dir),
            )
            .await?;
            print_json(&report)?;
            Ok(ExitCode::SUCCESS)
        }
        Command::VerifyRecord { file } => run_verify_record(&file),
    }
}

fn run_verify_record(path: &Path) -> Result<ExitCode, String> {
    let raw = read_ordinary_bytes(path, DEFAULT_MAX_FILE_BYTES).map_err(|e| e.to_string())?;
    let parsed = strict_parse_json(&raw, DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_JSON_DEPTH)
        .map_err(|e| e.to_string())?;
    let digest = verify_record_envelope(&parsed, None).map_err(|e| e.to_string())?;
    let reencoded = encode_canonical(&parsed).map_err(|e| e.to_string())?;
    let canonical_byte_exact = reencoded == raw;
    let id = parsed
        .get("id")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    print_json(&json!({
        "verified": true,
        "id": id,
        "sha256": digest,
        "raw_sha256": sha256_hex(&raw),
        "bytes": raw.len(),
        "canonical_byte_exact": canonical_byte_exact,
    }))?;
    Ok(ExitCode::SUCCESS)
}
