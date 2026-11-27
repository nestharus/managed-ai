//! Error types for the Membus v2 backed Managed AI Information store.
use managed_ai_fs::FsError;
use managed_ai_protocol::{CanonicalError, RecordError};
use membus::MembusError;
use std::path::PathBuf;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("membus error: {0}")]
    Membus(#[from] MembusError),
    #[error("filesystem error: {0}")]
    Fs(#[from] FsError),
    #[error("record validation error: {0}")]
    Record(#[from] RecordError),
    #[error("canonical JSON error: {0}")]
    Canonical(#[from] CanonicalError),
    #[error("invalid store input: {0}")]
    InvalidInput(String),
    #[error("directory I/O error on {path}: {source}")]
    DirIo {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}
