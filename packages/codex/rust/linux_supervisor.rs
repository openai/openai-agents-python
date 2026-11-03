//! Per-UnixLocal-exec ownership of Codex's sandbox descendants on Linux.
//!
//! This runs before a Tokio runtime or any thread exists. It never makes the
//! host Python process a subreaper. The direct helper still owns its native
//! mount cleanup; this owner waits for children orphaned during bwrap exit.

use std::ffi::OsString;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

static CANCELLED: AtomicBool = AtomicBool::new(false);
const GRACE: Duration = Duration::from_secs(1);

extern "C" fn mark_cancelled(_: libc::c_int) {
    CANCELLED.store(true, Ordering::Relaxed);
}

fn signal(pid: libc::pid_t, sig: libc::c_int) -> io::Result<()> {
    // The only caller supplies one of our unreaped direct/adopted children.
    if unsafe { libc::kill(pid, sig) } == -1 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error);
        }
    }
    Ok(())
}

fn owned_children(task_children: &mut File) -> io::Result<Vec<libc::pid_t>> {
    // This process remains single threaded and alone reaps these children.
    // Even an exited child retains its PID until the next waitpid below, so
    // no PID read from this private kernel list can be reused before kill.
    task_children.seek(SeekFrom::Start(0))?;
    let mut children = String::new();
    task_children.read_to_string(&mut children)?;
    let mut pids = Vec::new();
    for child in children.split_whitespace() {
        let child: libc::pid_t = child
            .parse()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if child <= 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid owned child PID",
            ));
        }
        pids.push(child);
    }
    Ok(pids)
}

fn terminate_owned_children(task_children: &mut File) -> io::Result<()> {
    for child in owned_children(task_children)? {
        signal(child, libc::SIGKILL)?;
    }
    Ok(())
}

pub(super) fn run(helper: &Path, args: impl Iterator<Item = OsString>) -> anyhow::Result<()> {
    // SAFETY: Linux prctl with integer arguments; this is a fresh executable,
    // not a forked Python host or a process running a Tokio runtime.
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    // Require our task's proc interface before launching any work. Keep the
    // verified descriptor instead of reopening a pathname after child spawn.
    let pid = std::process::id();
    let mut task_children = File::open(format!("/proc/self/task/{pid}/children"))?;
    owned_children(&mut task_children)?;
    // Install before spawning so cancellation can never kill an owner with a
    // live child but no handler. Before this point there are no children.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = mark_cancelled as *const () as libc::sighandler_t;
        libc::sigemptyset(&mut action.sa_mask);
        if libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut()) == -1 {
            return Err(io::Error::last_os_error().into());
        }
    }
    if CANCELLED.load(Ordering::Relaxed) {
        return Ok(());
    }
    let mut command = Command::new(helper);
    command.args(args);
    // Inherit PTY stdin/stdout/stderr. The trusted confined payload bootstrap
    // claims a separate controlling session and foreground process group.
    let child = command.spawn()?;
    let primary = child.id() as libc::pid_t;
    drop(child); // Reaping belongs exclusively to waitpid(-1) below.
    let status = join(primary, &mut task_children)?;
    if let Some(sig) = status.signal() {
        // Preserve the helper's caller-visible negative subprocess return code.
        unsafe {
            libc::signal(sig, libc::SIG_DFL);
            libc::raise(sig);
        }
        std::process::exit(128 + sig);
    }
    std::process::exit(
        status
            .code()
            .expect("reaped helper returned an exit status"),
    );
}

fn join(primary: libc::pid_t, task_children: &mut File) -> io::Result<std::process::ExitStatus> {
    let mut primary_status = None;
    let mut deadline = None;
    let mut failure = None;
    loop {
        if CANCELLED.load(Ordering::Relaxed) && deadline.is_none() {
            if primary_status.is_none() {
                if let Err(error) = signal(primary, libc::SIGTERM) {
                    failure = Some(error);
                }
            }
            deadline = Some(Instant::now() + GRACE);
        }
        // A finished direct helper cannot keep background work alive past the
        // exec boundary. After forced termination children may reparent in
        // several waves. Signal each newly adopted direct child, then reap.
        if primary_status.is_some()
            || failure.is_some()
            || deadline.is_some_and(|at| Instant::now() >= at)
        {
            if let Err(error) = terminate_owned_children(task_children) {
                failure.get_or_insert(error);
                // The proc interface was verified before launch. If it fails
                // after launch, still stop the one unreaped child we own by
                // construction. Never leave until all descendants are reaped.
                if primary_status.is_none() {
                    if let Err(error) = signal(primary, libc::SIGKILL) {
                        failure.get_or_insert(error);
                    }
                }
            }
        }
        let mut status = 0;
        let reaped = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
        if reaped == primary {
            primary_status = Some(std::process::ExitStatus::from_raw(status));
        }
        if reaped > 0 {
            continue;
        }
        if reaped == -1 {
            let error = io::Error::last_os_error();
            match error.raw_os_error() {
                Some(libc::ECHILD) => break,
                Some(libc::EINTR) => continue,
                _ => {
                    failure.get_or_insert(error);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    if let Some(error) = failure {
        return Err(error);
    }
    primary_status.ok_or_else(|| io::Error::other("sandbox helper was not reaped"))
}
