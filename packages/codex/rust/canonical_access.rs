//! Observe completed I/O through an audited extension tool's issued authority.
//! Discovery, command text and remote server internals never enter this decorator.
use std::task::Poll;

use super::*;
use codex_tools::{ToolEnvironment, ToolSearchInfo};

pub struct AccessContributor(pub Arc<dyn ToolContributor>);

impl AccessContributor {
    fn wrap(
        &self,
        tools: Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>>,
        thread: &ExtensionData,
        step: Arc<StepAccess>,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        let Some(rules) = thread.get::<Rules>() else {
            return tools;
        };
        tools
            .into_iter()
            .map(|inner| {
                Arc::new(AccessExecutor {
                    inner,
                    rules: rules.clone(),
                    step: step.clone(),
                }) as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>
            })
            .collect()
    }
}

impl ToolContributor for AccessContributor {
    fn tools(
        &self,
        session: &ExtensionData,
        thread: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        self.wrap(
            self.0.tools(session, thread),
            thread,
            Arc::new(StepAccess::default()),
        )
    }

    fn tools_for_step(
        &self,
        session: &ExtensionData,
        thread: &ExtensionData,
        step: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        self.wrap(
            self.0.tools_for_step(session, thread, step),
            thread,
            step.get_or_init(StepAccess::default),
        )
    }
}

struct AccessExecutor {
    inner: Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>,
    rules: Arc<Rules>,
    step: Arc<StepAccess>,
}

impl<'call> ToolExecutor<ToolCall<'call>> for AccessExecutor {
    fn tool_name(&self) -> ToolName {
        self.inner.tool_name()
    }
    fn spec(&self) -> ToolSpec {
        self.inner.spec()
    }
    fn exposure(&self) -> ToolExposure {
        self.inner.exposure()
    }
    fn search_info(&self) -> Option<ToolSearchInfo> {
        self.inner.search_info()
    }
    fn supports_parallel_tool_calls(&self) -> bool {
        self.inner.supports_parallel_tool_calls()
    }
    fn handle<'a>(&'a self, call: ToolCall<'call>) -> ToolExecutorFuture<'a>
    where
        ToolCall<'call>: 'a,
    {
        Box::pin(async move {
            let context = self
                .step
                .0
                .lock()
                .expect("canonical rules step poisoned")
                .clone();
            let access: Vec<_> = call
                .environments
                .iter()
                .map(|env| BoundAccess {
                    inner: env.fs(),
                    environment_id: &env.environment_id,
                    cwd: &env.cwd,
                    turn: &call.turn_id,
                    call_id: &call.call_id,
                    rules: &self.rules,
                    context: context.as_ref(),
                    key: Arc::new(env.fs().cache_key()),
                })
                .collect();
            let environments = access
                .iter()
                .map(|fs| ToolEnvironment::new(fs.environment_id.to_owned(), fs.cwd.clone(), fs))
                .collect();
            // A new short borrow, never extend the original accessor's lifetime.
            let decorated = ToolCall {
                environments,
                ..call.clone()
            };
            self.inner.handle(decorated).await
        })
    }
}

struct BoundAccess<'a> {
    inner: &'a dyn EnvironmentAccess,
    environment_id: &'a str,
    cwd: &'a PathUri,
    turn: &'a str,
    call_id: &'a str,
    rules: &'a Arc<Rules>,
    context: Option<&'a Arc<AccessContext>>,
    key: Arc<EnvironmentAccessKey>,
}

impl BoundAccess<'_> {
    fn observed(&self, path: &PathUri, kind: AccessKind) {
        let Some(context) = self.context else {
            return;
        };
        let mut scope = self.rules.0.lock().expect("canonical rules scope poisoned");
        let _ = scope.observe(AccessObservation {
            context,
            key: Some(self.key.as_ref().clone()),
            owner_turn: self.turn,
            call_id: self.call_id,
            environment_id: self.environment_id,
            cwd: self.cwd,
            path: path.clone(),
            kind,
        });
    }
}

impl EnvironmentAccess for BoundAccess<'_> {
    fn cache_key(&self) -> EnvironmentAccessKey {
        self.inner.cache_key()
    }
    fn canonicalize<'a>(&'a self, path: &'a PathUri) -> ExecutorFileSystemFuture<'a, PathUri> {
        self.inner.canonicalize(path)
    }
    fn read_file<'a>(
        &'a self,
        path: &'a PathUri,
        options: ReadFileOptions,
    ) -> ExecutorFileSystemFuture<'a, Vec<u8>> {
        Box::pin(async move {
            let result = self.inner.read_file(path, options).await?;
            self.observed(path, AccessKind::ExecutorRead);
            Ok(result)
        })
    }
    fn read_file_stream<'a>(
        &'a self,
        path: &'a PathUri,
    ) -> ExecutorFileSystemFuture<'a, FileSystemReadStream> {
        Box::pin(async move {
            let mut stream = self.inner.read_file_stream(path).await?;
            let Some(context) = self.context else {
                return Ok(stream);
            };
            // Even the opaque cache key contains an Arc to the sandbox. Only
            // the issued callback, never its returned stream, may keep it alive.
            let rules = Arc::downgrade(self.rules);
            let context = Arc::downgrade(context);
            let key = Arc::downgrade(&self.key);
            let owner_turn = self.turn.to_owned();
            let call_id = self.call_id.to_owned();
            let environment_id = self.environment_id.to_owned();
            let cwd = self.cwd.clone();
            let path = path.clone();
            let mut eligible = true;
            Ok(FileSystemReadStream::new(futures_util::stream::poll_fn(
                move |cx| {
                    let result = stream.poll_next_unpin(cx);
                    match &result {
                        Poll::Ready(Some(Err(_))) => eligible = false,
                        Poll::Ready(None) if std::mem::take(&mut eligible) => {
                            if let (Some(rules), Some(context), Some(key)) =
                                (rules.upgrade(), context.upgrade(), key.upgrade())
                            {
                                let mut scope =
                                    rules.0.lock().expect("canonical rules scope poisoned");
                                let _ = scope.observe(AccessObservation {
                                    context: &context,
                                    key: Some(key.as_ref().clone()),
                                    owner_turn: &owner_turn,
                                    call_id: &call_id,
                                    environment_id: &environment_id,
                                    cwd: &cwd,
                                    path: path.clone(),
                                    kind: AccessKind::ExecutorRead,
                                });
                            }
                        }
                        _ => {}
                    }
                    result
                },
            )))
        })
    }
    fn write_file<'a>(
        &'a self,
        path: &'a PathUri,
        contents: Vec<u8>,
        options: WriteFileOptions,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        Box::pin(async move {
            self.inner.write_file(path, contents, options).await?;
            self.observed(path, AccessKind::ExecutorWrite);
            Ok(())
        })
    }
    fn create_directory<'a>(
        &'a self,
        path: &'a PathUri,
        options: CreateDirectoryOptions,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        self.inner.create_directory(path, options)
    }
    fn get_metadata<'a>(
        &'a self,
        path: &'a PathUri,
        options: GetMetadataOptions,
    ) -> ExecutorFileSystemFuture<'a, FileMetadata> {
        self.inner.get_metadata(path, options)
    }
    fn read_directory<'a>(
        &'a self,
        path: &'a PathUri,
    ) -> ExecutorFileSystemFuture<'a, Vec<ReadDirectoryEntry>> {
        self.inner.read_directory(path)
    }
    fn walk<'a>(
        &'a self,
        path: &'a PathUri,
        options: WalkOptions,
    ) -> ExecutorFileSystemFuture<'a, WalkOutcome> {
        self.inner.walk(path, options)
    }
    fn remove<'a>(
        &'a self,
        path: &'a PathUri,
        options: RemoveOptions,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        self.inner.remove(path, options)
    }
    fn copy<'a>(
        &'a self,
        from: &'a PathUri,
        to: &'a PathUri,
        options: CopyOptions,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        self.inner.copy(from, to, options)
    }
}
