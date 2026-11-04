//! Run the production Darwin worker from a minimal test executable.

extern crate self as codex_exec_server;
extern crate self as codex_protocol;
extern crate self as codex_sandboxing;
extern crate self as codex_utils_absolute_path;

#[path = "production/arg0.rs"]
mod arg0;
#[path = "production/codex-rs/exec-server/src/darwin_coalition.rs"]
mod darwin_coalition;
#[path = "production/codex-rs/exec-server/src/darwin_command.rs"]
mod darwin_command;
#[path = "production/scm.rs"]
mod sandboxed_file_open;
#[path = "production/codex-rs/sandboxing/src/spawn.rs"]
mod spawn;
#[path = "production/codex-rs/sandboxing/src/terminal_queries.rs"]
mod terminal_queries;

pub use spawn::SpawnRequest;
pub use spawn::spawn_process;

// These unused Windows request field types avoid pulling the platform-independent
// protocol graph into this macOS-only executable.
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
use std::fs::File;
use std::io;
use std::os::fd::AsRawFd;
use std::os::fd::RawFd;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use anyhow::Result;
use anyhow::ensure;
use codex_utils_pty::SpawnedProcess;
use darwin_command::CommandControl;

const LIMIT: Duration = Duration::from_secs(8);
const TEST_PROFILE: &str = r#"
    (version 1)
    (allow default)
    (deny file-write*)
    (allow file-write* (subpath (param "TEST_ROOT")))
"#;

struct Harness {
    executable: PathBuf,
    fixture: PathBuf,
    root: PathBuf,
}

enum Stop {
    Exit,
    Interrupt,
    Close,
}

impl Harness {
    async fn launch(
        &self,
        mode: &str,
        args: &[String],
        tty: bool,
        fds: &[RawFd],
    ) -> Result<(SpawnedProcess, Arc<CommandControl>)> {
        let mut command = vec![
            "/usr/bin/sandbox-exec".to_owned(),
            "-D".to_owned(),
            format!("TEST_ROOT={}", self.root.display()),
            "-p".to_owned(),
            TEST_PROFILE.to_owned(),
            self.fixture
                .to_str()
                .context("fixture path is not UTF-8")?
                .to_owned(),
            mode.to_owned(),
        ];
        command.extend_from_slice(args);
        darwin_command::spawn(
            &self.executable,
            &command,
            &self.root,
            &HashMap::new(),
            &None,
            tty,
            tty,
            fds,
        )
        .await
        .with_context(|| format!("launch {mode}"))
    }

    async fn check_start_gate(&self) -> Result<()> {
        let marker = self.root.join("before-start");
        let args = [marker.display().to_string()];
        let (mut process, control) = self.launch("mark", &args, false, &[]).await?;
        tokio::time::sleep(Duration::from_millis(150)).await;
        ensure!(!process.session.has_exited(), "wrapper exited before START");
        process.session.request_terminate();
        finish(&mut process, &control, 128 + libc::SIGKILL).await?;
        ensure!(
            control.wait_started().await.is_err(),
            "STARTED without START"
        );
        ensure!(!marker.exists(), "payload ran before START");
        let (mut process, control) = self.launch("mark", &args, false, &[]).await?;
        start(&control).await?;
        finish(&mut process, &control, 0).await?;
        ensure!(marker.exists(), "the fixture cannot write even after START");
        println!("PASS start gate and pre-START cancellation");
        Ok(())
    }

    async fn check_detached(&self, stop: Stop) -> Result<()> {
        let (label, mode, exit_code) = match stop {
            Stop::Exit => ("natural exit", "orphan", 23),
            Stop::Interrupt => ("interrupt", "interrupt", 128 + libc::SIGINT),
            Stop::Close => ("control close", "interrupt", 128 + libc::SIGKILL),
        };
        let gate = self.root.join(format!("{label}-gate"));
        let marker = self.root.join(format!("{label}-marker"));
        let (mut process, control) = self
            .launch(
                mode,
                &[gate.display().to_string(), marker.display().to_string()],
                false,
                &[],
            )
            .await?;
        start(&control).await?;
        let output = output_through(&mut process, "\n").await?;
        let pid = output
            .lines()
            .find_map(|line| line.strip_prefix("DETACHED "))
            .with_context(|| format!("detached PID missing from {output:?}"))?
            .parse::<libc::pid_t>()?;
        ensure!(pid > 1, "invalid detached PID {pid}");
        match stop {
            Stop::Exit => {}
            Stop::Interrupt => control.interrupt()?,
            Stop::Close => process.session.request_terminate(),
        }
        finish(&mut process, &control, exit_code).await?;
        File::create(gate)?;
        assert_gone(pid).await?;
        tokio::time::sleep(Duration::from_millis(80)).await;
        ensure!(!marker.exists(), "detached command wrote after its drain");
        println!("PASS {label}: detached PID {pid} drained before completion");
        Ok(())
    }

    async fn run(&self) -> Result<()> {
        darwin_command::check_supported(&self.executable).await?;
        self.check_start_gate().await?;

        let (mut retained, control) = self.launch("terminal", &[], true, &[]).await?;
        start(&control).await?;
        output_through(&mut retained, "TERMINAL READY").await?;
        self.check_detached(Stop::Exit).await?;
        self.check_detached(Stop::Interrupt).await?;
        self.check_detached(Stop::Close).await?;
        ensure!(
            !retained.session.has_exited(),
            "the retained handle was stopped"
        );
        terminal_round_trip(&mut retained, &control).await?;
        println!("PASS retained handle with controlling PTY and stdin/stdout");

        let fd_path = self.root.join("inherited-fd");
        std::fs::write(&fd_path, b"permit\n")?;
        let fd = File::open(fd_path)?;
        let (mut transferred, control) = self
            .launch(
                "terminal",
                &[fd.as_raw_fd().to_string()],
                true,
                &[fd.as_raw_fd()],
            )
            .await?;
        start(&control).await?;
        let output = output_through(&mut transferred, "TERMINAL READY").await?;
        ensure!(output.contains("FD READY"), "inherited FD was not readable");
        terminal_round_trip(&mut transferred, &control).await?;
        println!("PASS controlling PTY and explicit SCM_RIGHTS descriptor");
        Ok(())
    }
}

async fn start(control: &CommandControl) -> Result<()> {
    control.start()?;
    tokio::time::timeout(LIMIT, control.wait_started())
        .await
        .context("STARTED deadline")??;
    Ok(())
}

async fn finish(
    process: &mut SpawnedProcess,
    control: &CommandControl,
    expected: i32,
) -> Result<()> {
    let result = tokio::time::timeout(LIMIT, &mut process.exit_rx)
        .await
        .context("command exit deadline")??;
    if result != expected {
        let mut error_output = Vec::new();
        while let Ok(bytes) = process.stderr_rx.try_recv() {
            error_output.extend(bytes);
        }
        anyhow::bail!(
            "command exited {result}, expected {expected}: {}",
            String::from_utf8_lossy(&error_output)
        );
    }
    ensure!(
        tokio::time::timeout(LIMIT, control.wait_drained())
            .await
            .context("drain acknowledgement deadline")?,
        "wrapper closed without a verified drain acknowledgement"
    );
    ensure!(
        control.is_drained(),
        "drain acknowledgement did not persist"
    );
    Ok(())
}

async fn output_through(process: &mut SpawnedProcess, marker: &str) -> Result<String> {
    let mut bytes = Vec::new();
    tokio::time::timeout(LIMIT, async {
        while let Some(chunk) = process.stdout_rx.recv().await {
            bytes.extend(chunk);
            if bytes
                .windows(marker.len())
                .any(|window| window == marker.as_bytes())
            {
                return Ok(String::from_utf8(bytes)?);
            }
        }
        anyhow::bail!(
            "output closed before {marker:?}: {:?}",
            String::from_utf8_lossy(&bytes)
        );
    })
    .await
    .with_context(|| format!("output deadline waiting for {marker:?}"))?
}

async fn terminal_round_trip(process: &mut SpawnedProcess, control: &CommandControl) -> Result<()> {
    process
        .session
        .writer_sender()
        .send(b"round-trip\n".to_vec())
        .await?;
    output_through(process, "ACK round-trip").await?;
    finish(process, control, 17).await
}

async fn assert_gone(pid: libc::pid_t) -> Result<()> {
    tokio::time::timeout(LIMIT, async {
        loop {
            // SAFETY: Signal zero checks existence and cannot deliver a signal.
            let result = unsafe { libc::kill(pid, 0) };
            if result == -1 && io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .with_context(|| format!("detached PID {pid} survived"))
}

fn main() -> Result<()> {
    arg0::dispatch();
    let fixture = std::env::args_os()
        .nth(1)
        .context("expected fixture path")?;
    // SAFETY: This reads the effective UID without changing credentials.
    ensure!(unsafe { libc::geteuid() } != 0, "run as an ordinary user");
    let root = tempfile::tempdir_in(Path::new("/tmp"))?;
    let harness = Harness {
        executable: std::env::current_exe()?,
        fixture: std::fs::canonicalize(fixture)?,
        root: root.path().canonicalize()?,
    };
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(harness.run())
}
