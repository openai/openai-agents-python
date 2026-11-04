//! Invoke the unchanged production wrapper against an already prepared command.

extern crate self as codex_exec_server;
extern crate self as codex_protocol;
extern crate self as codex_sandboxing;
extern crate self as codex_utils_absolute_path;

#[path = "../wrapper-qualification/production/arg0.rs"]
mod arg0;
#[path = "../wrapper-qualification/production/codex-rs/exec-server/src/darwin_coalition.rs"]
mod darwin_coalition;
#[cfg_attr(feature = "clear-bootstrap", path = "fixed_darwin_command.rs")]
#[cfg_attr(
    all(feature = "blocking-worker", not(feature = "clear-bootstrap")),
    path = "blocking_darwin_command.rs"
)]
#[cfg_attr(
    not(feature = "blocking-worker"),
    path = "../wrapper-qualification/production/codex-rs/exec-server/src/darwin_command.rs"
)]
mod darwin_command;
#[path = "../wrapper-qualification/production/scm.rs"]
mod sandboxed_file_open;
#[path = "../wrapper-qualification/production/codex-rs/sandboxing/src/spawn.rs"]
mod spawn;
#[path = "../wrapper-qualification/production/codex-rs/sandboxing/src/terminal_queries.rs"]
mod terminal_queries;

pub use spawn::SpawnRequest;
pub use spawn::spawn_process;

// Type placeholders only for unused Windows-only fields.
pub mod config_types {
    pub enum WindowsSandboxLevel {}
}
pub mod models {
    pub enum PermissionProfile {}
}
pub enum AbsolutePathBuf {}
pub enum WindowsSandboxFilesystemOverrides {}
pub enum WindowsSandboxProxySettingsMode {}
#[derive(PartialEq, Eq)]
pub enum SandboxType {
    None,
    WindowsRestrictedToken,
}

use std::collections::HashMap;
use std::io::Write;
use std::path::Path;
use std::time::Duration;

use anyhow::Context;
use anyhow::Result;
use anyhow::ensure;

mod seatbelt_daemon;

#[derive(Debug)]
enum SeatbeltPreparationError {
    FileSystem(String),
}
use tokio::sync::mpsc;

async fn collect(mut receiver: mpsc::Receiver<Vec<u8>>) -> Vec<u8> {
    let mut output = Vec::new();
    while let Some(bytes) = receiver.recv().await {
        output.extend(bytes);
    }
    output
}

async fn run(tty: bool, cwd: &Path, command: &[String]) -> Result<i32> {
    let executable = std::env::current_exe()?;
    darwin_command::check_supported(&executable)
        .await
        .context("launchd readiness probe")?;
    let (process, control) = darwin_command::spawn(
        &executable,
        command,
        cwd,
        &HashMap::new(),
        &None,
        tty,
        /* stdin_open */ tty,
        &[],
    )
    .await?;
    let stdout = tokio::spawn(collect(process.stdout_rx));
    let stderr = tokio::spawn(collect(process.stderr_rx));
    control.start()?;
    let started = control.wait_started().await;
    let status = process.exit_rx.await?;
    std::io::stdout().write_all(&stdout.await?)?;
    std::io::stderr().write_all(&stderr.await?)?;
    started.context("payload did not reach STARTED")?;
    ensure!(
        control.wait_drained().await && control.is_drained(),
        "wrapper did not drain"
    );
    Ok(status)
}

fn main() -> Result<()> {
    arg0::dispatch();
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if let [mode, directory] = args.as_slice()
        && mode == "supervisor-policy"
    {
        let rules = seatbelt_daemon::protection_policy(Path::new(directory))
            .map_err(|SeatbeltPreparationError::FileSystem(error)| anyhow::anyhow!(error))?;
        println!("{rules}");
        return Ok(());
    }
    let [mode, cwd, command @ ..] = args.as_slice() else {
        anyhow::bail!("expected tty/pipe mode, cwd, and prepared argv");
    };
    let tty = match mode.as_str() {
        "tty" => true,
        "pipe" => false,
        _ => anyhow::bail!("expected tty or pipe"),
    };
    let result = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(async {
            tokio::time::timeout(Duration::from_secs(18), run(tty, Path::new(cwd), command)).await
        })??;
    std::process::exit(result);
}
