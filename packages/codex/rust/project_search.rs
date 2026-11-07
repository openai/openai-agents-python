//! Bounded structured search through the selected executor's filesystem authority.
use std::sync::Arc;

use codex_extension_api::{ExtensionData, ToolContributor};
use codex_file_system::{EnvironmentAccess, ReadFileOptions, WalkEntryKind, WalkOptions};
use codex_tools::{
    FunctionCallError, JsonToolOutput, ResponsesApiTool, ToolCall, ToolExecutor,
    ToolExecutorFuture, ToolName, ToolOutput, ToolSpec,
};
use codex_utils_path_uri::PathUri;
use globset::GlobBuilder;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

const RESPONSE_BYTES: usize = 64 * 1024;
const FILE_BYTES: usize = 1024 * 1024;
const TOTAL_BYTES: usize = 16 * FILE_BYTES;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Options {
    pub root: PathUri,
    pub tools: Vec<SearchTool>,
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SearchTool {
    ProjectGlob,
    ProjectGrep,
}
impl SearchTool {
    fn name(self) -> &'static str {
        match self {
            Self::ProjectGlob => "project_glob",
            Self::ProjectGrep => "project_grep",
        }
    }
    fn grep(self) -> bool {
        matches!(self, Self::ProjectGrep)
    }
}

pub struct Contributor(pub Options);
impl ToolContributor for Contributor {
    fn tools(
        &self,
        _: &ExtensionData,
        _: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        self.0
            .tools
            .iter()
            .map(|tool| {
                Arc::new(Search {
                    root: self.0.root.clone(),
                    tool: *tool,
                }) as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>
            })
            .collect()
    }
}
struct Search {
    root: PathUri,
    tool: SearchTool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Arguments {
    pattern: String,
    #[serde(default = "default_path")]
    path: String,
    #[serde(default = "default_max")]
    max_results: usize,
    #[serde(default)]
    glob: Option<String>,
    #[serde(default)]
    context_lines: usize,
}
fn default_path() -> String {
    ".".into()
}
fn default_max() -> usize {
    100
}

#[derive(Default, Serialize)]
struct ResultBody {
    matches: Vec<Value>,
    truncated: bool,
    truncation_reasons: Vec<&'static str>,
    errors: Vec<SearchError>,
    skipped_binary_files: usize,
    skipped_invalid_utf8_files: usize,
}
#[derive(Serialize)]
struct SearchError {
    path: String,
    code: &'static str,
}
impl ResultBody {
    fn partial(&mut self, reason: &'static str) {
        self.truncated = true;
        if !self.truncation_reasons.contains(&reason) {
            self.truncation_reasons.push(reason);
        }
    }
    fn error(&mut self, path: &str, code: &'static str) {
        self.errors.push(SearchError {
            path: path.into(),
            code,
        });
    }
    // Also charge JSON string escaping used by direct text outputs. Never slice JSON.
    fn fit(mut self, budget: usize) -> Result<Value, FunctionCallError> {
        loop {
            let value = serde_json::to_value(&self).expect("search response serializes");
            if encoded_size(&value) <= budget {
                return Ok(value);
            }
            self.partial("response_bytes");
            if !self.matches.is_empty() {
                self.matches.truncate(self.matches.len() / 2);
                continue;
            }
            if !self.errors.is_empty() {
                self.errors.truncate(self.errors.len() / 2);
                continue;
            }
            return Err(FunctionCallError::RespondToModel(
                "project search response budget is too small".into(),
            ));
        }
    }
}
fn encoded_size(value: &Value) -> usize {
    let text = value.to_string();
    serde_json::to_string(&text)
        .expect("JSON string serializes")
        .len()
}
fn relative_path(value: &str) -> bool {
    // URL normalization must not change the path approved by application policy.
    !value.is_empty()
        && !value.starts_with('/')
        && !value.contains(['\\', ':'])
        && !value.chars().any(char::is_control)
        && value
            .split('/')
            .all(|part| part != ".." && !part.is_empty())
}
fn glob(pattern: &str) -> Result<globset::GlobMatcher, ()> {
    if pattern.len() > 4096 || pattern.contains(['\\', '\0']) {
        return Err(());
    }
    GlobBuilder::new(pattern)
        .literal_separator(true)
        .backslash_escape(false)
        .build()
        .map(|g| g.compile_matcher())
        .map_err(|_| ())
}
fn relative(root: &PathUri, path: &PathUri) -> Option<String> {
    // PathUri::strip_prefix also supports remote executor platform conventions.
    path.relative_path_from(root).map(|p| {
        if matches!(
            root.infer_path_convention(),
            Some(codex_utils_path_uri::PathConvention::Windows)
        ) {
            p.replace('\\', "/")
        } else {
            p
        }
    })
}

impl<'call> ToolExecutor<ToolCall<'call>> for Search {
    fn tool_name(&self) -> ToolName {
        ToolName::new(None, self.tool.name())
    }
    fn spec(&self) -> ToolSpec {
        let mut properties = json!({
            "pattern": {"type":"string", "description": if self.tool.grep() { "Case-sensitive Rust regular expression (no backtracking)." } else { "Case-sensitive glob, relative to the selected subtree." }},
            "path": {"type":"string", "default":".", "description":"Relative subtree under the fixed project root; no absolute paths, parent components, URIs, backslashes or control characters."},
            "max_results": {"type":"integer", "minimum":1, "maximum":1000, "default":100}
        });
        if self.tool.grep() {
            properties["glob"] = json!({"type":"string", "default":"**/*"});
            properties["context_lines"] =
                json!({"type":"integer", "minimum":0, "maximum":5, "default":0});
        }
        ToolSpec::Function(ResponsesApiTool {
            name: self.tool.name().into(),
            description: "Search the fixed project root without a shell. Includes hidden entries, ignores no ignore files, rejects symlinks. Scans at most 10000 entries, 1000 directories and depth 64; grep reads at most 1 MiB per file and 16 MiB total; response at most 64 KiB. Bounded scan and response; inspect truncated, truncation_reasons and errors. Narrow the pattern or subtree and rerun for partial results; no snapshot guarantee.".into(),
            strict: false, defer_loading: None,
            parameters: serde_json::from_value(json!({"type":"object", "properties":properties, "required":["pattern"], "additionalProperties":false})).expect("static search schema"),
            output_schema: None,
        })
    }
    fn supports_parallel_tool_calls(&self) -> bool {
        true
    }
    fn handle<'a>(&'a self, call: ToolCall<'call>) -> ToolExecutorFuture<'a>
    where
        ToolCall<'call>: 'a,
    {
        Box::pin(async move {
            let budget = call.response_byte_budget(RESPONSE_BYTES);
            let parsed: Result<Arguments, _> = serde_json::from_str(call.function_arguments()?);
            let result = match parsed {
                Ok(args) => match call.environments.first() {
                    Some(env) => search(env.fs(), &self.root, self.tool, args).await,
                    None => {
                        let mut r = ResultBody::default();
                        r.error(".", "path_unavailable");
                        r
                    }
                },
                Err(_) => {
                    let mut r = ResultBody::default();
                    r.error(".", "invalid_argument");
                    r
                }
            };
            let success = result.errors.is_empty();
            Ok(Box::new(JsonToolOutput::with_success(
                result.fit(budget)?,
                Some(success),
            )) as Box<dyn ToolOutput>)
        })
    }
}

async fn search(
    fs: &dyn EnvironmentAccess,
    root: &PathUri,
    tool: SearchTool,
    args: Arguments,
) -> ResultBody {
    let mut result = ResultBody::default();
    if !relative_path(&args.path) {
        result.error(&args.path, "invalid_path");
        return result;
    }
    if !(1..=1000).contains(&args.max_results)
        || args.context_lines > 5
        || (!tool.grep() && (args.glob.is_some() || args.context_lines != 0))
    {
        result.error(&args.path, "invalid_argument");
        return result;
    }
    let matcher = match glob(if tool.grep() {
        args.glob.as_deref().unwrap_or("**/*")
    } else {
        &args.pattern
    }) {
        Ok(matcher) => matcher,
        Err(()) => {
            result.error(&args.path, "invalid_pattern");
            return result;
        }
    };
    let regex = if tool.grep() {
        if args.pattern.len() > 4096 {
            result.error(&args.path, "invalid_pattern");
            return result;
        }
        match regex::RegexBuilder::new(&args.pattern)
            .size_limit(1024 * 1024)
            .dfa_size_limit(1024 * 1024)
            .build()
        {
            Ok(regex) => Some(regex),
            Err(_) => {
                result.error(&args.path, "invalid_pattern");
                return result;
            }
        }
    } else {
        None
    };
    let subtree = if args.path == "." {
        root.clone()
    } else {
        match root.join(&args.path) {
            Ok(path) => path,
            Err(_) => {
                result.error(&args.path, "invalid_path");
                return result;
            }
        }
    };
    let walk = match fs
        .walk(
            &subtree,
            WalkOptions {
                no_follow: true,
                max_depth: 64,
                max_directories: 1000,
                max_entries: 10_000,
                follow_directory_symlinks: false,
                prune_hidden_directories: false,
            },
        )
        .await
    {
        Ok(walk) => walk,
        Err(_) => {
            result.error(&args.path, "path_unavailable");
            return result;
        }
    };
    if walk.truncated {
        result.partial("scan_limit");
    }
    for error in walk.errors {
        result.error(
            &relative(root, &error.path).unwrap_or_else(|| args.path.clone()),
            "path_unavailable",
        );
        result.partial("read_errors");
    }
    let mut entries = walk.entries;
    entries.sort_by(|a, b| a.path.to_string().cmp(&b.path.to_string()));
    let mut total_bytes = 0;
    let mut match_bytes = 0usize;
    for entry in entries {
        if entry.kind != WalkEntryKind::File {
            continue;
        }
        let Some(subpath) = relative(&subtree, &entry.path) else {
            result.error(&args.path, "path_unavailable");
            result.partial("read_errors");
            continue;
        };
        if !matcher.is_match(&subpath) {
            continue;
        }
        let Some(path) = relative(root, &entry.path) else {
            continue;
        };
        if let Some(regex) = &regex {
            if entry.kind != WalkEntryKind::File {
                continue;
            }
            let remaining = TOTAL_BYTES - total_bytes;
            if remaining == 0 {
                result.partial("total_bytes");
                break;
            }
            let cap = FILE_BYTES.min(remaining);
            let bytes = match fs
                .read_file(
                    &entry.path,
                    ReadFileOptions {
                        follow_symlinks: false,
                        max_bytes: Some(cap as u64),
                    },
                )
                .await
            {
                Ok(bytes) => bytes,
                Err(_) => {
                    // Failed reads still consume their full allocation in the scan budget.
                    total_bytes += cap;
                    // Remote errors share kinds across permission and read-limit failures.
                    result.error(&path, "path_unavailable");
                    result.partial("read_errors");
                    continue;
                }
            };
            total_bytes += bytes.len();
            if bytes.contains(&0) {
                result.skipped_binary_files += 1;
                result.partial("binary_files");
                continue;
            }
            let text = match std::str::from_utf8(&bytes) {
                Ok(text) => text,
                Err(_) => {
                    result.skipped_invalid_utf8_files += 1;
                    result.partial("invalid_utf8_files");
                    continue;
                }
            };
            let lines: Vec<_> = text.lines().collect();
            for (index, line) in lines.iter().enumerate() {
                if !regex.is_match(line) {
                    continue;
                }
                if result.matches.len() == args.max_results {
                    result.partial("max_results");
                    return result;
                }
                let context = |start: usize, end: usize| -> Vec<Value> {
                    (start..end)
                        .map(|i| json!({"line_number":i+1,"line":lines[i]}))
                        .collect()
                };
                let item = json!({"path":path,"line_number":index+1,"line":line,"before":context(index.saturating_sub(args.context_lines),index),"after":context(index+1,(index+1+args.context_lines).min(lines.len()))});
                match_bytes += encoded_size(&item);
                result.matches.push(item);
                if match_bytes > RESPONSE_BYTES {
                    result.partial("response_bytes");
                    return result;
                }
            }
        } else {
            if result.matches.len() == args.max_results {
                result.partial("max_results");
                break;
            }
            let item = Value::String(path);
            match_bytes += encoded_size(&item);
            result.matches.push(item);
            if match_bytes > RESPONSE_BYTES {
                result.partial("response_bytes");
                break;
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_exec_server::LOCAL_FS;
    use codex_file_system::FileSystemEnvironmentAccessor;
    fn args(pattern: &str) -> Arguments {
        Arguments {
            pattern: pattern.into(),
            path: ".".into(),
            max_results: 100,
            glob: None,
            context_lines: 0,
        }
    }
    #[tokio::test]
    async fn nested_root_paths_context_hidden_and_no_matches() {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().canonicalize().unwrap();
        std::fs::create_dir(base.join("nested")).unwrap();
        std::fs::create_dir(base.join("directory.txt")).unwrap();
        std::fs::write(base.join("nested/.hidden.txt"), "before\nneedle\nafter\n").unwrap();
        let root = PathUri::from_host_native_path(&base).unwrap();
        let fs = FileSystemEnvironmentAccessor::unrestricted(&LOCAL_FS);
        let mut a = args("need(le)");
        a.path = "nested".into();
        a.context_lines = 1;
        let result = search(&fs, &root, SearchTool::ProjectGrep, a).await;
        assert_eq!(
            result.matches,
            vec![
                json!({"path":"nested/.hidden.txt","line_number":2,"line":"needle","before":[{"line_number":1,"line":"before"}],"after":[{"line_number":3,"line":"after"}]})
            ]
        );
        assert!(!result.truncated);
        assert!(result.errors.is_empty());
        let result = search(&fs, &root, SearchTool::ProjectGlob, args("**/*.txt")).await;
        assert_eq!(result.matches, vec![json!("nested/.hidden.txt")]);
        let result = search(&fs, &root, SearchTool::ProjectGrep, args("absent")).await;
        assert!(result.matches.is_empty());
        assert!(!result.truncated);
        assert!(result.errors.is_empty());
    }
    #[tokio::test]
    async fn invalid_arguments_and_result_limit_are_structured() {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().canonicalize().unwrap();
        std::fs::write(base.join("file"), "needle\n".repeat(10)).unwrap();
        let root = PathUri::from_host_native_path(&base).unwrap();
        let fs = FileSystemEnvironmentAccessor::unrestricted(&LOCAL_FS);
        for (tool, a, code) in [
            (SearchTool::ProjectGrep, args("["), "invalid_pattern"),
            (SearchTool::ProjectGlob, args("["), "invalid_pattern"),
            (
                SearchTool::ProjectGrep,
                Arguments {
                    path: "../escape".into(),
                    ..args("x")
                },
                "invalid_path",
            ),
            (
                SearchTool::ProjectGrep,
                Arguments {
                    max_results: 0,
                    ..args("x")
                },
                "invalid_argument",
            ),
            (
                SearchTool::ProjectGrep,
                Arguments {
                    path: "missing".into(),
                    ..args("x")
                },
                "path_unavailable",
            ),
        ] {
            let result = search(&fs, &root, tool, a).await;
            assert!(result.matches.is_empty());
            assert_eq!(result.errors[0].code, code);
        }
        let result = search(
            &fs,
            &root,
            SearchTool::ProjectGrep,
            Arguments {
                max_results: 3,
                ..args("needle")
            },
        )
        .await;
        assert_eq!(result.matches.len(), 3);
        assert!(result.truncated);
        assert_eq!(result.truncation_reasons, vec!["max_results"]);
    }
    #[tokio::test]
    async fn control_paths_cannot_select_a_different_subtree() {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().canonicalize().unwrap();
        std::fs::create_dir(base.join("secrets")).unwrap();
        std::fs::write(base.join("secrets/secret.txt"), "private needle\n").unwrap();
        let root = PathUri::from_host_native_path(&base).unwrap();
        let fs = FileSystemEnvironmentAccessor::unrestricted(&LOCAL_FS);
        for control in ['\t', '\n', '\r'] {
            for tool in [SearchTool::ProjectGlob, SearchTool::ProjectGrep] {
                let path = format!("sec{control}rets");
                let result = search(
                    &fs,
                    &root,
                    tool,
                    Arguments {
                        path: path.clone(),
                        ..args(if tool.grep() { "needle" } else { "*" })
                    },
                )
                .await;
                assert!(result.matches.is_empty());
                assert!(!result.truncated);
                assert_eq!(result.errors.len(), 1);
                assert_eq!(result.errors[0].path, path);
                assert_eq!(result.errors[0].code, "invalid_path");
            }
        }
    }

    #[tokio::test]
    async fn skipped_and_oversized_files_are_explicit_partial_results() {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().canonicalize().unwrap();
        std::fs::write(base.join("binary"), b"needle\0").unwrap();
        std::fs::write(base.join("invalid"), [0xff]).unwrap();
        std::fs::File::create(base.join("large"))
            .unwrap()
            .set_len((FILE_BYTES + 1) as u64)
            .unwrap();
        let root = PathUri::from_host_native_path(&base).unwrap();
        let fs = FileSystemEnvironmentAccessor::unrestricted(&LOCAL_FS);
        let result = search(&fs, &root, SearchTool::ProjectGrep, args("needle")).await;
        assert!(result.matches.is_empty());
        assert!(result.truncated);
        assert_eq!(result.skipped_binary_files, 1);
        assert_eq!(result.skipped_invalid_utf8_files, 1);
        assert!(result.truncation_reasons.contains(&"read_errors"));
        assert_eq!(result.errors[0].code, "path_unavailable");
    }

    #[test]
    fn fit_accounts_for_escaped_json_and_keeps_valid_envelope() {
        let mut result = ResultBody::default();
        result.matches = vec![json!("\"\\\n".repeat(1000)); 10];
        let value = result.fit(1024).unwrap();
        assert!(encoded_size(&value) <= 1024);
        assert_eq!(value["truncated"], true);
        assert!(
            value["truncation_reasons"]
                .as_array()
                .unwrap()
                .contains(&json!("response_bytes"))
        );
        assert!(serde_json::from_str::<Value>(&value.to_string()).is_ok());
    }
}
