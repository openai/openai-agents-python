//! Check the source files and destinations of a saved native edit.

use std::collections::{BTreeMap, HashMap};
use std::io::ErrorKind;
use std::path::{Component, PathBuf};
use std::time::Duration;

use codex_exec_server::{EnvironmentAccess, GetMetadataOptions, ReadFileOptions};
use codex_protocol::error::{CodexErr, Result as CodexResult};
use codex_protocol::protocol::FileChange;
use codex_utils_path_uri::PathUri;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

type Changes = HashMap<PathBuf, FileChange>;
type Paths<'a> = BTreeMap<&'a PathBuf, (bool, PathUri)>;

const MAX_PATHS: usize = 32;
const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
const MAX_SCAN_BYTES: u64 = 16 * 1024 * 1024;

fn invalid(message: impl Into<String>) -> CodexErr {
    CodexErr::InvalidRequest(message.into())
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

// Plan and validate every source and move destination before any file I/O.
// The bool marks destinations that must be absent.
fn paths(changes: &Changes) -> CodexResult<Paths<'_>> {
    if changes.is_empty() || changes.len() > MAX_PATHS {
        return Err(invalid("Pending patch scans require 1 to 32 paths"));
    }
    let mut paths: BTreeMap<_, _> = changes
        .iter()
        .map(|(path, change)| (path, matches!(change, FileChange::Add { .. })))
        .collect();
    for change in changes.values() {
        if let FileChange::Update {
            move_path: Some(destination),
            ..
        } = change
        {
            if paths.len() == MAX_PATHS {
                return Err(invalid("Pending patch scans are limited to 32 paths"));
            }
            if paths.insert(destination, true).is_some() {
                return Err(invalid("Patch move destinations must be distinct"));
            }
        }
    }
    paths
        .into_iter()
        .map(|(path, absent)| {
            if !path.is_absolute()
                || path
                    .components()
                    .any(|p| matches!(p, Component::ParentDir | Component::CurDir))
            {
                return Err(invalid("Patch paths must be absolute and normalized"));
            }
            if path.to_str().is_none() {
                return Err(invalid("Patch approval paths require UTF-8"));
            }
            let uri =
                PathUri::from_host_native_path(path).map_err(|error| invalid(error.to_string()))?;
            Ok((path, (absent, uri)))
        })
        .collect()
}

fn receipt_files<'a>(paths: &Paths<'_>, receipt: &'a Value) -> CodexResult<&'a Map<String, Value>> {
    let root = receipt
        .as_object()
        .ok_or_else(|| invalid("Invalid patch approval preconditions"))?;
    let files = root
        .get("files")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid("Invalid patch approval file map"))?;
    if root.len() != 2
        || !matches!(root.get("version").and_then(Value::as_u64), Some(1 | 2))
        || files.len() != paths.len()
    {
        return Err(invalid("Invalid patch approval version or file count"));
    }
    for (path, (absent, _)) in paths {
        match (absent, path.to_str().and_then(|p| files.get(p))) {
            (true, Some(Value::Null)) => {}
            (false, Some(Value::String(hash))) if valid_digest(hash) => {}
            _ => return Err(invalid("Saved files do not match the pending patch")),
        }
    }
    Ok(files)
}

pub(super) async fn capture(fs: &dyn EnvironmentAccess, changes: &Changes) -> CodexResult<Value> {
    let files = scan(fs, changes, paths(changes)?, true).await?;
    Ok(json!({"version": 1, "files": files}))
}

pub(super) async fn validate(
    fs: &dyn EnvironmentAccess,
    changes: &Changes,
    receipt: &Value,
) -> CodexResult<()> {
    let paths = paths(changes)?;
    let expected = receipt_files(&paths, receipt)?;
    let redacted = receipt["version"] == 2;
    if redacted
        && changes.values().any(|change| match change {
            FileChange::Delete { content } => !content.is_empty(),
            FileChange::Update { unified_diff, .. } => !valid_digest(unified_diff),
            FileChange::Add { .. } => false,
        })
    {
        return Err(invalid("Invalid redacted patch changes"));
    }
    if &scan(fs, changes, paths, !redacted).await? != expected {
        return Err(invalid("Pending patch files have changed"));
    }
    Ok(())
}

// Call only on a live prepared request. In particular, a digest-looking live
// update still needs hashing; a legacy saved request must never be reinterpreted.
pub(super) fn compact(changes: &mut Changes, receipt: &Value) -> CodexResult<bool> {
    let files = receipt_files(&paths(changes)?, receipt)?;
    let mut update_budget = MAX_SCAN_BYTES;
    let mut delete_budget = MAX_SCAN_BYTES;
    // Check everything before redacting, so errors leave the request unchanged.
    for (path, change) in changes.iter() {
        let (content, budget) = match change {
            FileChange::Add { .. } => continue,
            FileChange::Delete { content } => (content, &mut delete_budget),
            FileChange::Update { unified_diff, .. } => (unified_diff, &mut update_budget),
        };
        let size = content.len() as u64;
        if size > MAX_FILE_BYTES.min(*budget) {
            return Err(invalid("Prepared patch exceeds the recovery limit"));
        }
        *budget -= size;
        if matches!(change, FileChange::Delete { .. })
            && path
                .to_str()
                .and_then(|path| files.get(path))
                .and_then(Value::as_str)
                != Some(digest(content.as_bytes()).as_str())
        {
            return Err(invalid(
                "The pending deletion does not match its saved digest",
            ));
        }
    }
    let mut compacted = false;
    for change in changes.values_mut() {
        match change {
            FileChange::Add { .. } => continue,
            FileChange::Delete { content } => content.clear(),
            FileChange::Update { unified_diff, .. } => {
                *unified_diff = digest(unified_diff.as_bytes())
            }
        }
        compacted = true;
    }
    Ok(compacted)
}

async fn scan(
    fs: &dyn EnvironmentAccess,
    changes: &Changes,
    paths: Paths<'_>,
    compare_deleted_content: bool,
) -> CodexResult<Map<String, Value>> {
    tokio::time::timeout(Duration::from_secs(10), async {
        let mut files = Map::new();
        let mut remaining = MAX_SCAN_BYTES;
        for (path, (absent, uri)) in paths {
            // Permission-bound no-follow operations reject ancestor symlinks too.
            let metadata_options = GetMetadataOptions {
                follow_symlinks: false,
            };
            let value = match fs.get_metadata(&uri, metadata_options).await {
                Err(error) if absent && error.kind() == ErrorKind::NotFound => Value::Null,
                Ok(meta) if !absent && meta.is_file && !meta.is_symlink => {
                    let read_options = ReadFileOptions {
                        follow_symlinks: false,
                    };
                    let limit = MAX_FILE_BYTES.min(remaining);
                    let bytes = fs
                        .read_file_bounded(&uri, read_options, limit)
                        .await
                        .map_err(|_| invalid("Cannot read pending patch file"))?;
                    if bytes.len() as u64 > limit {
                        return Err(invalid("Pending patch file exceeds the recovery limit"));
                    }
                    remaining -= bytes.len() as u64;
                    if compare_deleted_content
                        && let Some(FileChange::Delete { content }) = changes.get(path)
                        && content.as_bytes() != bytes.as_slice()
                    {
                        return Err(invalid("The pending deletion no longer matches the file"));
                    }
                    Value::String(digest(&bytes))
                }
                _ => return Err(invalid("Cannot verify pending patch source or destination")),
            };
            // The complete plan has already checked UTF-8 before any I/O.
            files.insert(
                path.to_str().expect("validated UTF-8 path").to_owned(),
                value,
            );
        }
        Ok(files)
    })
    .await
    .map_err(|_| invalid("Pending patch scan exceeded the 10 second limit"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_binds_live_deletions_and_hashes_live_updates_in_both_versions() {
        let directory = std::env::current_dir().unwrap();
        let deleted = directory.join("deleted");
        let updated = directory.join("updated");
        let live_diff = "a".repeat(64);
        let original = Changes::from([
            (
                deleted.clone(),
                FileChange::Delete {
                    content: "private preimage".into(),
                },
            ),
            (
                updated.clone(),
                FileChange::Update {
                    unified_diff: live_diff.clone(),
                    move_path: None,
                },
            ),
        ]);
        for version in [1, 2] {
            let receipt = json!({"version": version, "files": {
                (deleted.to_str().unwrap()): digest(b"private preimage"),
                (updated.to_str().unwrap()): digest(b"original file"),
            }});
            let mut changes = original.clone();
            assert!(compact(&mut changes, &receipt).unwrap());
            assert_eq!(
                changes[&deleted],
                FileChange::Delete {
                    content: String::new()
                }
            );
            assert_eq!(
                changes[&updated],
                FileChange::Update {
                    unified_diff: digest(live_diff.as_bytes()),
                    move_path: None,
                }
            );

            // A mismatched deletion or malformed receipt must not partly redact.
            for bad in [
                json!({"version": version, "files": {
                    (deleted.to_str().unwrap()): digest(b"different file"),
                    (updated.to_str().unwrap()): digest(b"original file"),
                }}),
                json!({"version": version, "files": {}, "unexpected": true}),
            ] {
                let mut changes = original.clone();
                assert!(compact(&mut changes, &bad).is_err());
                assert_eq!(changes, original);
            }
        }
    }
}
