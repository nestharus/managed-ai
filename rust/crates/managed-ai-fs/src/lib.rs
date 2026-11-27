//! Cross-platform ordinary-file I/O, symlink/reparse refusal, bounded reads,
//! atomic writes, streaming SHA-256 hashing, and windowless command construction.
use sha2::{Digest, Sha256};
use std::ffi::OsStr;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use thiserror::Error;

/// Default maximum record/document size (16 MiB).
pub const DEFAULT_MAX_FILE_BYTES: usize = 16 * 1024 * 1024;

#[cfg(windows)]
const WIN_FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
#[cfg(windows)]
const WIN_FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
#[cfg(windows)]
const WIN_CREATE_NO_WINDOW: u32 = 0x0800_0000;

static TEMP_SEQ: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Error)]
pub enum FsError {
    #[error("ordinary file required (refused symlink, reparse point, or non-regular entry): {path}")]
    NotOrdinaryFile { path: PathBuf },
    #[error("file exceeds maximum allowed size of {limit} bytes: {path}")]
    FileTooLarge { path: PathBuf, limit: usize },
    #[error("invalid target path (missing parent directory): {path}")]
    InvalidPath { path: PathBuf },
    #[error("I/O error on {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

/// Compute lowercase hex SHA-256 digest of a byte slice.
pub fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    encode_hex_lower(&digest)
}

/// Stream-hash up to `max_bytes` from a reader, failing if more than `max_bytes` are present.
pub fn sha256_reader<R: Read>(mut reader: R, max_bytes: usize, label: &Path) -> Result<(String, usize), FsError> {
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 16 * 1024];
    let mut total: usize = 0;
    loop {
        let n = reader.read(&mut buf).map_err(|source| FsError::Io {
            path: label.to_path_buf(),
            source,
        })?;
        if n == 0 {
            break;
        }
        total = total.saturating_add(n);
        if total > max_bytes {
            return Err(FsError::FileTooLarge {
                path: label.to_path_buf(),
                limit: max_bytes,
            });
        }
        hasher.update(&buf[..n]);
    }
    Ok((encode_hex_lower(&hasher.finalize()), total))
}

fn encode_hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// Returns true iff `meta` describes a regular file that is neither a symlink nor a Windows reparse point.
pub fn is_ordinary_metadata(meta: &Metadata) -> bool {
    if !meta.file_type().is_file() || meta.file_type().is_symlink() {
        return false;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if (meta.file_attributes() & WIN_FILE_ATTRIBUTE_REPARSE_POINT) != 0 {
            return false;
        }
    }
    true
}

/// Returns true iff `meta` describes a regular directory that is neither a symlink nor a Windows reparse point.
pub fn is_ordinary_dir_metadata(meta: &Metadata) -> bool {
    if !meta.file_type().is_dir() || meta.file_type().is_symlink() {
        return false;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if (meta.file_attributes() & WIN_FILE_ATTRIBUTE_REPARSE_POINT) != 0 {
            return false;
        }
    }
    true
}

/// Verify that `path` exists as an ordinary directory (neither symlink nor Windows reparse point/junction).
pub fn ensure_ordinary_dir(path: &Path) -> Result<Metadata, FsError> {
    let lmeta = fs::symlink_metadata(path).map_err(|source| FsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    if !is_ordinary_dir_metadata(&lmeta) {
        return Err(FsError::NotOrdinaryFile {
            path: path.to_path_buf(),
        });
    }
    Ok(lmeta)
}

/// Open an ordinary file read-only, refusing symlinks/reparse points and non-regular files.
pub fn open_ordinary_read(path: &Path) -> Result<(File, Metadata), FsError> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        ensure_ordinary_dir(parent)?;
    }
    let lmeta = fs::symlink_metadata(path).map_err(|source| FsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    if !is_ordinary_metadata(&lmeta) {
        return Err(FsError::NotOrdinaryFile {
            path: path.to_path_buf(),
        });
    }

    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc_o_nofollow());
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        opts.custom_flags(WIN_FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = opts.open(path).map_err(|source| FsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let fmeta = file.metadata().map_err(|source| FsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    if !is_ordinary_metadata(&fmeta) {
        return Err(FsError::NotOrdinaryFile {
            path: path.to_path_buf(),
        });
    }
    Ok((file, fmeta))
}

#[cfg(unix)]
fn libc_o_nofollow() -> i32 {
    // O_NOFOLLOW varies by Linux architecture (ARM/ARM64 = 0x8000, x86/x86_64/riscv = 0x20000).
    #[cfg(all(target_os = "linux", any(target_arch = "arm", target_arch = "aarch64")))]
    {
        0x8000
    }
    #[cfg(all(target_os = "linux", not(any(target_arch = "arm", target_arch = "aarch64"))))]
    {
        0x20000
    }
    #[cfg(any(target_os = "macos", target_os = "freebsd", target_os = "openbsd", target_os = "netbsd"))]
    {
        0x0100
    }
    #[cfg(not(any(
        target_os = "linux",
        target_os = "macos",
        target_os = "freebsd",
        target_os = "openbsd",
        target_os = "netbsd"
    )))]
    {
        0
    }
}

/// Read an ordinary file up to `max_bytes` strictly bounded (never reading unbounded memory).
pub fn read_ordinary_bytes(path: &Path, max_bytes: usize) -> Result<Vec<u8>, FsError> {
    let (file, meta) = open_ordinary_read(path)?;
    if meta.len() > max_bytes as u64 {
        return Err(FsError::FileTooLarge {
            path: path.to_path_buf(),
            limit: max_bytes,
        });
    }
    let cap = (meta.len() as usize).min(max_bytes);
    let mut out = Vec::with_capacity(cap);
    file.take((max_bytes as u64).saturating_add(1))
        .read_to_end(&mut out)
        .map_err(|source| FsError::Io {
            path: path.to_path_buf(),
            source,
        })?;
    if out.len() > max_bytes {
        return Err(FsError::FileTooLarge {
            path: path.to_path_buf(),
            limit: max_bytes,
        });
    }
    Ok(out)
}

/// Durably replace `path` with `data` using a same-directory temp file, `sync_all`, and atomic rename.
/// Refuses to overwrite a symlink, reparse point, or directory, or traverse a reparse-point parent.
pub fn write_atomic_bytes(path: &Path, data: &[u8]) -> Result<(), FsError> {
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).ok_or_else(|| {
        FsError::InvalidPath {
            path: path.to_path_buf(),
        }
    })?;
    if let Ok(existing) = fs::symlink_metadata(path) {
        if !is_ordinary_metadata(&existing) {
            return Err(FsError::NotOrdinaryFile {
                path: path.to_path_buf(),
            });
        }
    }
    fs::create_dir_all(parent).map_err(|source| FsError::Io {
        path: parent.to_path_buf(),
        source,
    })?;
    ensure_ordinary_dir(parent)?;
    let leaf = path
        .file_name()
        .and_then(OsStr::to_str)
        .unwrap_or("tmp");
    let seq = TEMP_SEQ.fetch_add(1, Ordering::Relaxed);
    let tmp_name = format!(".{leaf}.tmp-{}-{seq}", std::process::id());
    let tmp_path = parent.join(tmp_name);

    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&tmp_path)
        .map_err(|source| FsError::Io {
            path: tmp_path.clone(),
            source,
        })?;

    let write_res = (|| -> Result<(), FsError> {
        f.write_all(data).map_err(|source| FsError::Io {
            path: tmp_path.clone(),
            source,
        })?;
        f.sync_all().map_err(|source| FsError::Io {
            path: tmp_path.clone(),
            source,
        })?;
        drop(f);
        fs::rename(&tmp_path, path).map_err(|source| FsError::Io {
            path: path.to_path_buf(),
            source,
        })?;
        Ok(())
    })();

    if write_res.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    write_res
}

/// Create a `Command` that never flashes a console window on Windows (`CREATE_NO_WINDOW`).
pub fn command_no_window<S: AsRef<OsStr>>(program: S) -> Command {
    let mut cmd = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(WIN_CREATE_NO_WINDOW);
    }
    cmd
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn atomic_write_and_bounded_read_roundtrip_with_sha256() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested").join("record.json");
        let payload = b"{\"hello\":\"world\"}\n";
        write_atomic_bytes(&path, payload).unwrap();

        let got = read_ordinary_bytes(&path, 1024).unwrap();
        assert_eq!(got, payload);
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_hex(&got),
            "6a47c31b7b7c3b9a1dbc960669f4674ce088c8fc9d9a4f7e9fcc3f6a81f7b86c"
        );

        let err = read_ordinary_bytes(&path, 5).unwrap_err();
        assert!(matches!(err, FsError::FileTooLarge { limit: 5, .. }));
    }

    #[test]
    fn directory_refused_as_ordinary_file() {
        let dir = tempfile::tempdir().unwrap();
        let err = read_ordinary_bytes(dir.path(), 1024).unwrap_err();
        assert!(matches!(err, FsError::NotOrdinaryFile { .. }));
        let err2 = write_atomic_bytes(dir.path(), b"x").unwrap_err();
        assert!(matches!(err2, FsError::NotOrdinaryFile { .. }));
    }

    #[test]
    fn sha256_reader_enforces_hard_cap() {
        let data = vec![b'a'; 100];
        let (digest, len) = sha256_reader(&data[..], 100, Path::new("mem")).unwrap();
        assert_eq!(len, 100);
        assert_eq!(digest, sha256_hex(&data));
        let err = sha256_reader(&data[..], 99, Path::new("mem")).unwrap_err();
        assert!(matches!(err, FsError::FileTooLarge { limit: 99, .. }));
    }
}
