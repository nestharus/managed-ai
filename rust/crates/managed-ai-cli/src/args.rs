//! Strict CLI argument parser for `managed-ai-rs`.
use managed_ai_protocol::LexicalMode;
use std::collections::HashSet;
use std::net::SocketAddr;
use std::path::PathBuf;

pub const DEFAULT_TIMEOUT_SECS: u64 = 30;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EndpointTarget {
    NamedPipe(String),
    Tcp(SocketAddr),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectionOpts {
    pub endpoint: EndpointTarget,
    pub token_env: Option<String>,
    pub timeout_secs: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    Help,
    Health(ConnectionOpts),
    Status {
        conn: ConnectionOpts,
        namespace: String,
    },
    Get {
        conn: ConnectionOpts,
        namespace: String,
        id: String,
    },
    List {
        conn: ConnectionOpts,
        namespace: String,
        limit: usize,
    },
    Search {
        conn: ConnectionOpts,
        namespace: String,
        query: String,
        kind: Option<String>,
        mode: LexicalMode,
        limit: usize,
    },
    VerifyParity {
        conn: ConnectionOpts,
        namespace: String,
        workspace_root: PathBuf,
    },
    ExportFs {
        conn: ConnectionOpts,
        namespace: String,
        out_dir: PathBuf,
    },
    VerifyRecord {
        file: PathBuf,
    },
}

pub fn usage() -> &'static str {
    "managed-ai-rs - Native Rust Managed AI Information CLI (Membus v2)

USAGE:
  managed-ai-rs <COMMAND> [OPTIONS]

COMMON CONNECTION OPTIONS:
  (--pipe PIPE | --tcp ADDR) [--token-env VAR] [--timeout-secs N]

COMMANDS:
  health         <CONN>
  status         <CONN> --namespace NS
  get            <CONN> --namespace NS --id ID
  list           <CONN> --namespace NS [--limit N]
  search         <CONN> --namespace NS --query Q [--kind K] [--mode bm25|literal] [--limit N]
  verify-parity  <CONN> --namespace NS --workspace-root DIR
  export-fs      <CONN> --namespace NS --out-dir DIR
  verify-record  --file PATH
  help           Show this help message
"
}

pub fn parse_args<I: IntoIterator<Item = String>>(args: I) -> Result<Command, String> {
    let mut iter = args.into_iter();
    let _bin = iter.next();
    let Some(sub) = iter.next() else {
        return Ok(Command::Help);
    };
    if matches!(sub.as_str(), "-h" | "--help" | "help") {
        return Ok(Command::Help);
    }
    let rest: Vec<String> = iter.collect();
    if has_help_flag(&rest) {
        return Ok(Command::Help);
    }

    match sub.as_str() {
        "health" => Ok(Command::Health(parse_conn_only(&rest)?)),
        "status" => parse_status(&rest),
        "get" => parse_get(&rest),
        "list" => parse_list(&rest),
        "search" => parse_search(&rest),
        "verify-parity" => parse_verify_parity(&rest),
        "export-fs" => parse_export_fs(&rest),
        "verify-record" => parse_verify_record(&rest),
        other => Err(format!("unknown command: {other:?} (run --help)")),
    }
}

fn has_help_flag(args: &[String]) -> bool {
    let mut i = 0;
    while i < args.len() {
        let tok = args[i].as_str();
        if matches!(tok, "-h" | "--help") {
            return true;
        }
        if tok.starts_with("--") {
            i += 2;
        } else {
            i += 1;
        }
    }
    false
}

struct FlagMap {
    values: Vec<(String, String)>,
}

impl FlagMap {
    fn parse(args: &[String], allowed: &[&str]) -> Result<Self, String> {
        let mut seen = HashSet::new();
        let mut values = Vec::new();
        let mut i = 0;
        while i < args.len() {
            let flag = &args[i];
            if !flag.starts_with("--") {
                return Err(format!("unexpected positional argument: {flag:?}"));
            }
            if !allowed.contains(&flag.as_str()) {
                return Err(format!("unknown option: {flag}"));
            }
            if !seen.insert(flag.clone()) {
                return Err(format!("duplicate option: {flag}"));
            }
            let Some(val) = args.get(i + 1) else {
                return Err(format!("option {flag} requires a value"));
            };
            values.push((flag.clone(), val.clone()));
            i += 2;
        }
        Ok(Self { values })
    }

    fn get(&self, flag: &str) -> Option<&str> {
        self.values
            .iter()
            .find(|(k, _)| k == flag)
            .map(|(_, v)| v.as_str())
    }

    fn require_nonempty(&self, flag: &str) -> Result<String, String> {
        let val = self
            .get(flag)
            .ok_or_else(|| format!("missing required option: {flag}"))?;
        if val.trim().is_empty() {
            return Err(format!("{flag} must be non-empty"));
        }
        Ok(val.to_owned())
    }
}

fn extract_conn(flags: &FlagMap) -> Result<ConnectionOpts, String> {
    let pipe = flags.get("--pipe");
    let tcp = flags.get("--tcp");
    let endpoint = match (pipe, tcp) {
        (Some(_), Some(_)) => return Err("--pipe and --tcp are mutually exclusive".to_owned()),
        (None, None) => return Err("one of --pipe or --tcp is required".to_owned()),
        (Some(p), None) => {
            if p.trim().is_empty() {
                return Err("--pipe must be non-empty".to_owned());
            }
            EndpointTarget::NamedPipe(p.to_owned())
        }
        (None, Some(addr)) => {
            let parsed: SocketAddr = addr
                .parse()
                .map_err(|e| format!("invalid --tcp socket address {addr:?}: {e}"))?;
            EndpointTarget::Tcp(parsed)
        }
    };
    let token_env = match flags.get("--token-env") {
        Some(name) if name.trim().is_empty() => {
            return Err("--token-env must be non-empty".to_owned());
        }
        Some(name) => Some(name.to_owned()),
        None => None,
    };
    let timeout_secs = match flags.get("--timeout-secs") {
        Some(raw) => {
            let n: u64 = raw
                .parse()
                .map_err(|_| format!("invalid --timeout-secs: {raw:?}"))?;
            if n == 0 || n > 600 {
                return Err("--timeout-secs must be in 1..=600".to_owned());
            }
            n
        }
        None => DEFAULT_TIMEOUT_SECS,
    };
    Ok(ConnectionOpts {
        endpoint,
        token_env,
        timeout_secs,
    })
}

fn parse_conn_only(args: &[String]) -> Result<ConnectionOpts, String> {
    let flags = FlagMap::parse(args, &["--pipe", "--tcp", "--token-env", "--timeout-secs"])?;
    extract_conn(&flags)
}

fn parse_status(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(
        args,
        &[
            "--pipe",
            "--tcp",
            "--token-env",
            "--timeout-secs",
            "--namespace",
        ],
    )?;
    Ok(Command::Status {
        conn: extract_conn(&flags)?,
        namespace: flags.require_nonempty("--namespace")?,
    })
}

fn parse_get(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(
        args,
        &[
            "--pipe",
            "--tcp",
            "--token-env",
            "--timeout-secs",
            "--namespace",
            "--id",
        ],
    )?;
    Ok(Command::Get {
        conn: extract_conn(&flags)?,
        namespace: flags.require_nonempty("--namespace")?,
        id: flags.require_nonempty("--id")?,
    })
}

fn parse_list(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(
        args,
        &[
            "--pipe",
            "--tcp",
            "--token-env",
            "--timeout-secs",
            "--namespace",
            "--limit",
        ],
    )?;
    let limit = parse_limit(flags.get("--limit"), 50, 2000)?;
    Ok(Command::List {
        conn: extract_conn(&flags)?,
        namespace: flags.require_nonempty("--namespace")?,
        limit,
    })
}

fn parse_search(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(
        args,
        &[
            "--pipe",
            "--tcp",
            "--token-env",
            "--timeout-secs",
            "--namespace",
            "--query",
            "--kind",
            "--mode",
            "--limit",
        ],
    )?;
    let mode = match flags.get("--mode").unwrap_or("bm25") {
        "bm25" => LexicalMode::Bm25,
        "literal" => LexicalMode::LiteralTrigram,
        other => return Err(format!("invalid --mode {other:?} (expected bm25 or literal)")),
    };
    let kind = match flags.get("--kind") {
        Some(k) if k.trim().is_empty() => return Err("--kind must be non-empty".to_owned()),
        Some(k) => Some(k.to_owned()),
        None => None,
    };
    let limit = parse_limit(flags.get("--limit"), 25, 200)?;
    Ok(Command::Search {
        conn: extract_conn(&flags)?,
        namespace: flags.require_nonempty("--namespace")?,
        query: flags.require_nonempty("--query")?,
        kind,
        mode,
        limit,
    })
}

fn parse_verify_parity(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(
        args,
        &[
            "--pipe",
            "--tcp",
            "--token-env",
            "--timeout-secs",
            "--namespace",
            "--workspace-root",
        ],
    )?;
    Ok(Command::VerifyParity {
        conn: extract_conn(&flags)?,
        namespace: flags.require_nonempty("--namespace")?,
        workspace_root: PathBuf::from(flags.require_nonempty("--workspace-root")?),
    })
}

fn parse_export_fs(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(
        args,
        &[
            "--pipe",
            "--tcp",
            "--token-env",
            "--timeout-secs",
            "--namespace",
            "--out-dir",
        ],
    )?;
    Ok(Command::ExportFs {
        conn: extract_conn(&flags)?,
        namespace: flags.require_nonempty("--namespace")?,
        out_dir: PathBuf::from(flags.require_nonempty("--out-dir")?),
    })
}

fn parse_verify_record(args: &[String]) -> Result<Command, String> {
    let flags = FlagMap::parse(args, &["--file"])?;
    Ok(Command::VerifyRecord {
        file: PathBuf::from(flags.require_nonempty("--file")?),
    })
}

fn parse_limit(raw: Option<&str>, default: usize, max: usize) -> Result<usize, String> {
    match raw {
        None => Ok(default),
        Some(s) => {
            let n: usize = s.parse().map_err(|_| format!("invalid --limit: {s:?}"))?;
            if n == 0 || n > max {
                return Err(format!("--limit must be in 1..={max}"));
            }
            Ok(n)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vec_s(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn help_and_mutual_exclusion_rules() {
        assert_eq!(parse_args(vec_s(&["bin", "--help"])).unwrap(), Command::Help);
        assert!(parse_args(vec_s(&["bin", "health", "--pipe", "p", "--tcp", "127.0.0.1:1"])).is_err());
        assert!(parse_args(vec_s(&["bin", "health", "--pipe", "p", "--pipe", "q"])).is_err());
        assert!(parse_args(vec_s(&["bin", "health", "--bogus", "1"])).is_err());
        assert!(parse_args(vec_s(&[
            "bin", "search", "--pipe", "p", "--namespace", "ns", "--query", "q", "--kind", ""
        ]))
        .is_err());
        assert!(parse_args(vec_s(&[
            "bin", "status", "--pipe", "p", "--namespace", "   "
        ]))
        .is_err());
    }

    #[test]
    fn help_flag_not_triggered_inside_option_values() {
        let cmd = parse_args(vec_s(&[
            "bin",
            "search",
            "--tcp",
            "127.0.0.1:18422",
            "--namespace",
            "ns1",
            "--query",
            "--help",
        ]))
        .unwrap();
        assert!(matches!(cmd, Command::Search { query, .. } if query == "--help"));
    }

    #[test]
    fn parses_search_and_verify_record() {
        let cmd = parse_args(vec_s(&[
            "bin",
            "search",
            "--tcp",
            "127.0.0.1:18422",
            "--namespace",
            "ns1",
            "--query",
            "membus",
            "--mode",
            "literal",
            "--limit",
            "10",
        ]))
        .unwrap();
        assert!(matches!(
            cmd,
            Command::Search {
                mode: LexicalMode::LiteralTrigram,
                limit: 10,
                ..
            }
        ));
    }
}
