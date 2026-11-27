//! Canonical Query AST (`ManagedQuery`) unifying lexical, graph, and record-filter queries.
use serde::{Deserialize, Serialize};

pub const MAX_QUERY_LIMIT: usize = 200;
pub const MAX_GRAPH_HOPS: u8 = 8;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LexicalMode {
    Bm25,
    LiteralTrigram,
    ExactRef,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct LexicalClause {
    pub query: String,
    pub mode: LexicalMode,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TraversalDirection {
    Outbound,
    Inbound,
    Both,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct GraphClause {
    pub anchors: Vec<String>,
    pub direction: TraversalDirection,
    pub max_hops: u8,
    #[serde(default)]
    pub include_labels: Vec<String>,
    #[serde(default)]
    pub exclude_labels: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManagedQuery {
    pub namespace: String,
    #[serde(default)]
    pub kinds: Vec<String>,
    #[serde(default = "default_true")]
    pub active_only: bool,
    pub lexical: Option<LexicalClause>,
    pub graph: Option<GraphClause>,
    #[serde(default = "default_limit")]
    pub limit: usize,
    pub max_bytes: Option<usize>,
}

fn default_true() -> bool {
    true
}

fn default_limit() -> usize {
    25
}

impl ManagedQuery {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.namespace.trim().is_empty() {
            return Err("namespace must be non-empty");
        }
        if self.limit == 0 || self.limit > MAX_QUERY_LIMIT {
            return Err("limit must be in 1..=200");
        }
        if let Some(g) = &self.graph {
            if g.anchors.is_empty() {
                return Err("graph anchors must be non-empty");
            }
            if g.max_hops == 0 || g.max_hops > MAX_GRAPH_HOPS {
                return Err("graph max_hops must be in 1..=8");
            }
        }
        if let Some(lex) = &self.lexical {
            if lex.query.trim().is_empty() {
                return Err("lexical query must be non-empty");
            }
        }
        Ok(())
    }
}
