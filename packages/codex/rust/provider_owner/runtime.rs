//! One bounded Provider runtime; every acquisition still owns a fresh scope.
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use tokio::sync::{MutexGuard, OwnedRwLockReadGuard, OwnedRwLockWriteGuard, RwLock};

use codex_core::config::ExecutionConfig;
use pyo3::prelude::*;
use serde_json::Value;

use super::{Home, Lease, Phase, ProviderResource, close_home};
use crate::{Engine, config, engine::EngineState, py_error};

#[derive(PartialEq, Eq)]
struct RuntimeKey {
    config: Value,
    runtime: Value,
    api_key: String,
    environment: BTreeMap<OsString, OsString>,
    process_cwd: PathBuf,
    share_model_http: bool,
}

impl RuntimeKey {
    fn ambient_matches(&self) -> bool {
        self.environment == std::env::vars_os().collect()
            && std::env::current_dir().is_ok_and(|cwd| cwd == self.process_cwd)
    }
}

pub(super) struct PreparedRuntime {
    key: RuntimeKey,
    engine: Option<Engine>,
    profile: codex_core::config::ManagedExecution,
    home: Home,
    runtime_json: String,
    last_release: std::sync::Mutex<Instant>,
    retiring: AtomicBool,
    expiry: Option<tokio::task::JoinHandle<()>>,
}

// A read checkout remains with the acquisition lease through joined cleanup.
const PROCESS_IDLE: Duration = Duration::from_secs(5);
static PROCESS_EXITING: AtomicBool = AtomicBool::new(false);
type ProcessSlot = Arc<RwLock<Option<PreparedRuntime>>>;

fn process_slot() -> PyResult<&'static ProcessSlot> {
    static SLOT: OnceLock<(u32, ProcessSlot)> = OnceLock::new();
    let (creator, slot) = SLOT.get_or_init(|| (std::process::id(), Arc::new(RwLock::new(None))));
    if *creator != std::process::id() {
        return Err(py_error("Implicit runtime belongs to another process"));
    }
    Ok(slot)
}

pub(crate) fn check_process_runtime_pid() -> PyResult<()> {
    process_slot().map(|_| ())
}

pub(super) struct ProcessCheckout(Option<OwnedRwLockReadGuard<Option<PreparedRuntime>>>);

impl ProcessCheckout {
    pub(super) fn release(mut self) {
        if let Some(entry) = self.0.as_ref().and_then(|slot| slot.as_ref()) {
            *entry
                .last_release
                .lock()
                .unwrap_or_else(|error| error.into_inner()) = Instant::now();
        }
        self.0.take();
    }
}

impl Drop for ProcessCheckout {
    fn drop(&mut self) {
        if let Some(slot) = self.0.as_ref() {
            if let Some(entry) = slot.as_ref() {
                // An unjoined acquisition must never return an admitting parent.
                entry.retiring.store(true, Ordering::Release);
            }
        }
    }
}

enum RuntimeSlot<'a> {
    Provider(MutexGuard<'a, Option<PreparedRuntime>>),
    Process(OwnedRwLockWriteGuard<Option<PreparedRuntime>>),
}

impl std::ops::Deref for RuntimeSlot<'_> {
    type Target = Option<PreparedRuntime>;

    fn deref(&self) -> &Self::Target {
        match self {
            Self::Provider(slot) => slot,
            Self::Process(slot) => slot,
        }
    }
}

impl std::ops::DerefMut for RuntimeSlot<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        match self {
            Self::Provider(slot) => slot,
            Self::Process(slot) => slot,
        }
    }
}

impl RuntimeSlot<'_> {
    fn retain(self, lease: &Lease) -> PyResult<()> {
        if let Self::Process(slot) = self {
            let checkout = OwnedRwLockWriteGuard::downgrade(slot);
            lease.record.lock().map_err(py_error)?.process_runtime =
                Some(ProcessCheckout(Some(checkout)));
        }
        Ok(())
    }
}

async fn close_process_slot(slot: &mut Option<PreparedRuntime>) -> PyResult<()> {
    if let Some(entry) = slot.as_mut() {
        entry.retiring.store(true, Ordering::Release);
        entry.close().await?;
    }
    slot.take();
    Ok(())
}

// The writer waits for every acquisition and any running expiry cleanup.
pub(crate) async fn close_process_runtime() -> PyResult<()> {
    retire_process_slot(process_slot()?.clone().write_owned().await).await
}

// The same checkout that keeps the implicit engine live also proves it
// is idle before a telemetry generation retires it.
pub(crate) async fn close_idle_process_runtime() -> PyResult<bool> {
    let Ok(slot) = process_slot()?.clone().try_write_owned() else {
        return Ok(false);
    };
    retire_process_slot(slot).await?;
    Ok(true)
}

async fn retire_process_slot(
    mut slot: OwnedRwLockWriteGuard<Option<PreparedRuntime>>,
) -> PyResult<()> {
    if let Some(entry) = slot.as_mut() {
        entry.retiring.store(true, Ordering::Release);
        if let Some(expiry) = entry.expiry.take() {
            // Write ownership proves the timer is not running retirement.
            expiry.abort();
            let _ = expiry.await;
        }
    }
    close_process_slot(&mut slot).await
}

pub(crate) fn close_process_runtime_at_exit() {
    let Ok(pool) = process_slot() else {
        return;
    };
    PROCESS_EXITING.store(true, Ordering::Release);
    close_idle_at_exit(pool);
}

fn close_idle_at_exit(pool: &ProcessSlot) {
    // Never wait for an active acquisition during interpreter shutdown.
    let Ok(slot) = pool.clone().try_write_owned() else {
        return;
    };
    let Some(entry) = slot.as_ref() else {
        return;
    };
    entry.retiring.store(true, Ordering::Release);
    let (finished, waiting) = std::sync::mpsc::sync_channel(1);
    pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
        if retire_process_slot(slot).await.is_err() {
            tracing::warn!("implicit runtime exit cleanup failed; ownership remains retained");
        }
        let _ = finished.send(());
    });
    // A timeout leaves the task and write guard owning cleanup, without nested block_on.
    let _ = waiting.recv_timeout(Duration::from_secs(1));
}

fn schedule_expiry(entry: &mut PreparedRuntime) {
    entry.expiry = Some(pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
        let mut delay = PROCESS_IDLE;
        loop {
            tokio::time::sleep(delay).await;
            let Ok(pool) = process_slot() else {
                return;
            };
            // Do not queue behind a long response and suppress matching checkouts.
            delay = match pool.clone().try_write_owned() {
                Ok(mut slot) => {
                    let Some(entry) = slot.as_ref() else {
                        return;
                    };
                    let idle = entry
                        .last_release
                        .lock()
                        .unwrap_or_else(|error| error.into_inner())
                        .elapsed();
                    if entry.retiring.load(Ordering::Acquire) || idle >= PROCESS_IDLE {
                        if close_process_slot(&mut slot).await.is_err() {
                            tracing::warn!(
                                "implicit runtime retirement failed; cleanup remains owned"
                            );
                        }
                        return;
                    }
                    PROCESS_IDLE - idle
                }
                Err(_) => PROCESS_IDLE,
            };
        }
    }));
}

impl PreparedRuntime {
    async fn close(&self) -> PyResult<()> {
        if let Some(engine) = &self.engine {
            crate::close_engine_owner(engine.inner.clone()).await?;
        }
        close_home(&self.home).map_err(py_error)
    }
}

impl Drop for PreparedRuntime {
    fn drop(&mut self) {
        let Some(engine) = self.engine.take() else {
            return;
        };
        if engine.current().is_err() {
            return;
        }
        let home = self.home.clone();
        // Keep the home alive through best-effort finalization if a caller drops
        // an unclosed Provider. Explicit close retains errors for a real retry.
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            if crate::close_engine_owner(engine.inner.clone())
                .await
                .is_ok()
            {
                let _ = close_home(&home);
            } else if let Ok(mut home) = home.lock() {
                if let Some(home) = home.take() {
                    let _ = home.keep();
                }
            }
        });
    }
}

impl ProviderResource {
    pub(super) async fn close_prepared_runtime(&self) -> PyResult<()> {
        let mut entry = self.prepared_runtime.lock().await;
        if let Some(runtime) = entry.as_ref() {
            runtime.close().await?;
        }
        entry.take();
        Ok(())
    }
}

impl PreparedRuntime {
    fn scope(&self, source: String) -> PyResult<EngineState> {
        if !self.key.ambient_matches() {
            return Err(py_error("runtime ambient inputs changed"));
        }
        let runtime = &self.runtime_json;
        guard_shared_sources(runtime)?;
        let parent = self
            .engine
            .as_ref()
            .expect("runtime owner exists")
            .current()?;
        for path in [
            &parent.service_config.codex_home,
            &parent.service_config.cwd,
        ] {
            let metadata = std::fs::symlink_metadata(path.as_path()).map_err(py_error)?;
            if !metadata.is_dir() || metadata.file_type().is_symlink() {
                return Err(py_error("runtime directory changed"));
            }
        }
        // No provenance rewrite: all fields and source paths belong to this same runtime.
        EngineState::scoped(
            std::sync::Arc::clone(&parent),
            parent.service_config.clone(),
            source,
            runtime.clone(),
            self.profile.clone(),
        )
        .map_err(py_error)
    }
}

fn guard_shared_sources(runtime: &str) -> PyResult<()> {
    let loader = config::RuntimeOptions::parse(runtime)
        .and_then(config::RuntimeOptions::loader_overrides)
        .map_err(py_error)?;
    if !loader.ignore_user_config
        || !loader.ignore_project_config
        || !loader.ignore_user_and_project_exec_policy_rules
        || !loader.ignore_managed_requirements
        || !loader.ignore_login_requirements
        || loader.packaged_defaults_path.is_some()
        || loader.user_config_path.is_some()
        || loader.user_config_profile.is_some()
    {
        return Err(py_error("incompatible shared runtime sources"));
    }
    for file in [
        &loader.managed_config_path,
        &loader.system_config_path,
        &loader.system_requirements_path,
    ] {
        let parent = file
            .as_deref()
            .and_then(std::path::Path::parent)
            .ok_or_else(|| py_error("shared runtime requires explicit source parents"))?;
        match std::fs::symlink_metadata(parent) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            _ => return Err(py_error("shared runtime source parent changed")),
        }
    }
    Ok(())
}

impl Lease {
    fn generated_home(&self) -> PyResult<super::NativeProviderHome> {
        self.check_live()?;
        let directory = tempfile::Builder::new()
            .prefix("native-agent-")
            .tempdir()
            .map_err(py_error)?;
        std::fs::create_dir(directory.path().join("workspace")).map_err(py_error)?;
        let name = directory.path().to_string_lossy().into_owned();
        let inner = std::sync::Arc::new(std::sync::Mutex::new(Some(directory)));
        self.record
            .lock()
            .map_err(py_error)?
            .homes
            .push(inner.clone());
        Ok(super::NativeProviderHome { inner, name })
    }

    fn runtime_at_home(raw: &str, home: &super::NativeProviderHome) -> PyResult<String> {
        let mut runtime: Value = serde_json::from_str(raw).map_err(py_error)?;
        runtime["codex_home"] = serde_json::json!(home.name);
        runtime["cwd"] = serde_json::json!(std::path::Path::new(&home.name).join("workspace"));
        config::prepare_runtime(&runtime.to_string()).map_err(py_error)
    }

    async fn dedicated_generated(
        &self,
        source: String,
        raw: String,
        api_key: String,
        pool: Option<codex_http_client::SdkModelHttpPool>,
    ) -> PyResult<(EngineState, Option<super::NativeProviderHome>)> {
        let home = self.generated_home()?;
        let runtime = Self::runtime_at_home(&raw, &home)?;
        let state = dedicated(source, runtime, api_key, pool, None).await?;
        Ok((state, Some(home)))
    }

    pub(crate) async fn conversation_engine(
        &self,
        source: String,
        raw: String,
        api_key: String,
        share: bool,
        process_runtime: bool,
    ) -> PyResult<(EngineState, Option<super::NativeProviderHome>)> {
        let value: Value = serde_json::from_str(&raw).map_err(py_error)?;
        if value.get("codex_home").is_some() || value.get("cwd").is_some() {
            return Err(py_error("atomic opening owns its runtime paths"));
        }
        self.check_live()?;
        crate::telemetry::engine_starting()
            .await
            .map_err(py_error)?;
        let pool = if share {
            Some(self.model_http_pool()?)
        } else {
            None
        };
        let eligible = value.as_object().is_some_and(|runtime| {
            runtime.len() == 3
                && runtime.get("strict_config").and_then(Value::as_bool) == Some(true)
                && runtime
                    .get("base_instructions")
                    .is_some_and(Value::is_string)
                && runtime
                    .get("loader")
                    .and_then(Value::as_object)
                    .is_some_and(|loader| {
                        loader.len() == 2
                            && ["ignore_user_config", "ignore_project_config"]
                                .iter()
                                .all(|key| loader.get(*key).and_then(Value::as_bool) == Some(true))
                    })
        });
        let available = {
            let record = self.record.lock().map_err(py_error)?;
            record.phase == Phase::Opening
                && record.engines.is_empty()
                && record.threads.is_empty()
                && record.homes.is_empty()
        };
        if !eligible || !available || api_key.trim().is_empty() {
            return self.dedicated_generated(source, raw, api_key, pool).await;
        }
        let Ok(process_cwd) = std::env::current_dir() else {
            return self.dedicated_generated(source, raw, api_key, pool).await;
        };
        let key = RuntimeKey {
            config: serde_json::from_str(&source).map_err(py_error)?,
            runtime: value,
            api_key: api_key.clone(),
            environment: std::env::vars_os().collect(),
            process_cwd,
            share_model_http: share,
        };
        let resource = self
            .resource
            .upgrade()
            .ok_or_else(|| py_error("Provider is closed"))?;
        let process_runtime = process_runtime && !PROCESS_EXITING.load(Ordering::Acquire);
        if process_runtime {
            if let Ok(slot) = process_slot()?.clone().try_read_owned() {
                if let Some(entry) = slot.as_ref() {
                    if !entry.retiring.load(Ordering::Acquire) && entry.key == key {
                        if let Ok(scope) = entry.scope(source.clone()) {
                            self.record.lock().map_err(py_error)?.process_runtime =
                                Some(ProcessCheckout(Some(slot)));
                            return Ok((scope, None));
                        }
                        entry.retiring.store(true, Ordering::Release);
                    }
                }
            }
        }
        let process_checkout = if process_runtime {
            process_slot()?
                .clone()
                .try_write_owned()
                .ok()
                .filter(|slot| slot.is_none())
        } else {
            None
        };
        let process_runtime = process_checkout.is_some();
        let mut slot = match process_checkout {
            Some(slot) => RuntimeSlot::Process(slot),
            None => RuntimeSlot::Provider(resource.prepared_runtime.lock().await),
        };
        if let Some(entry) = slot.as_ref() {
            if entry.key == key {
                if let Ok(scope) = entry.scope(source.clone()) {
                    return Ok((scope, None));
                }
            }
            drop(slot);
            return self.dedicated_generated(source, raw, api_key, pool).await;
        }
        // Until transfer, the opening lease owns this home for cancellation cleanup.
        let home = self.generated_home()?;
        let runtime = Self::runtime_at_home(&raw, &home)?;
        let ambient = ExecutionConfig::ambient_inputs();
        let (loaded, cloud) = config::load(&source, &runtime, None)
            .await
            .map_err(py_error)?;
        let template = match ExecutionConfig::try_new(loaded.clone(), ambient) {
            Ok(template) if guard_shared_sources(&runtime).is_ok() => template,
            _ => {
                drop(slot);
                let state =
                    dedicated(source, runtime, api_key, pool, Some((loaded, cloud, None))).await?;
                return Ok((state, Some(home)));
            }
        };
        let profile = template.profile();
        drop(template);
        let parent = dedicated(
            source.clone(),
            runtime.clone(),
            api_key.clone(),
            pool.clone(),
            Some((loaded, cloud, Some(profile.clone()))),
        )
        .await?;
        let entry = PreparedRuntime {
            key,
            engine: Some(Engine::new(parent)),
            profile,
            home: home.inner.clone(),
            runtime_json: runtime,
            last_release: std::sync::Mutex::new(Instant::now()),
            retiring: AtomicBool::new(false),
            expiry: None,
        };
        // Transfer cleanup to the Provider before admitting a scope. No scope gets a Home handle.
        self.record
            .lock()
            .map_err(py_error)?
            .homes
            .retain(|owned| !std::sync::Arc::ptr_eq(owned, &home.inner));
        if !entry.key.ambient_matches() {
            if process_runtime {
                *slot = Some(entry);
                close_process_slot(&mut slot).await?;
            } else {
                entry.close().await?;
            }
            drop(slot);
            return self.dedicated_generated(source, raw, api_key, pool).await;
        }
        let result = entry.scope(source).map(|scope| (scope, None));
        *slot = Some(entry);
        if process_runtime {
            let entry = slot.as_mut().expect("published runtime");
            entry.retiring.store(result.is_err(), Ordering::Release);
            schedule_expiry(entry);
            slot.retain(self)?;
        }
        result
    }
}

type Prepared = (
    codex_core_api::Config,
    codex_config::CloudConfigBundleLoader,
    Option<codex_core::config::ManagedExecution>,
);

async fn dedicated(
    source: String,
    runtime: String,
    api_key: String,
    pool: Option<codex_http_client::SdkModelHttpPool>,
    prepared: Option<Prepared>,
) -> PyResult<EngineState> {
    let mut state = EngineState::new_with_configuration(
        source,
        runtime,
        false,
        Some(api_key),
        None,
        None,
        true,
        prepared,
    )
    .await
    .map_err(py_error)?;
    state.model_http_pool = pool;
    Ok(state)
}

#[cfg(all(test, target_os = "linux"))]
#[path = "runtime_tests.rs"]
mod tests;
