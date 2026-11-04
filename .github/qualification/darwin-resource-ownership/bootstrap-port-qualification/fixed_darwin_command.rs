//! Own each managed local command in a dedicated launchd resource coalition.

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, Read, Seek, Write};
use std::net::Shutdown;
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::AsyncReadExt;
use tokio::sync::watch;

use crate::darwin_coalition::CoalitionOwner;
use crate::sandboxed_file_open::{receive_file_descriptor, send_file_descriptor};

pub const WRAPPER_ARG: &str = "--codex-sdk-darwin-command";
pub const JOB_ARG: &str = "--codex-sdk-darwin-job";
const START: u8 = 1;
const STARTED: u8 = 2;
const DRAINED: u8 = 3;
const INTERRUPT: u8 = 4;
const EXITED: u8 = 5;
const SETUP_TIMEOUT: Duration = Duration::from_secs(5);
const DRAIN_TIMEOUT: Duration = Duration::from_secs(4);
const POLL_INTERVAL: Duration = Duration::from_millis(10);
const MAX_REQUEST_SIZE: usize = 16 * 1024 * 1024;

#[derive(serde::Serialize, serde::Deserialize)]
struct JobRequest {
    command: Vec<String>,
    cwd: PathBuf,
    env: HashMap<String, String>,
    arg0: Option<String>,
    tty: bool,
    inherited_fds: Vec<RawFd>,
}

struct DarwinCommand {
    argv: Vec<String>,
    inherited_fds: Vec<RawFd>,
    control: UnixStream,
    _wrapper_control: UnixStream,
    _request_file: File,
}

/// Keep the target environment out of the unsandboxed wrapper and launchd job.
fn prepare(
    helper: &Path,
    command: &[String],
    cwd: &Path,
    env: &HashMap<String, String>,
    arg0: &Option<String>,
    tty: bool,
    inherited_fds: &[RawFd],
) -> io::Result<DarwinCommand> {
    let request = JobRequest {
        command: command.to_vec(),
        cwd: cwd.to_owned(),
        env: env.clone(),
        arg0: arg0.clone(),
        tty,
        inherited_fds: inherited_fds.to_vec(),
    };
    let directory = codex_uds::prepare_shared_daemon_socket_directory()?;
    let mut file = tempfile::tempfile_in(directory)?;
    serde_json::to_writer(&mut file, &request)?;
    file.rewind()?;
    let (control, wrapper_control) = UnixStream::pair()?;
    let mut wrapper_fds = inherited_fds.to_vec();
    wrapper_fds.extend([wrapper_control.as_raw_fd(), file.as_raw_fd()]);
    Ok(DarwinCommand {
        argv: vec![
            helper.to_str().ok_or_else(invalid_message)?.to_owned(),
            WRAPPER_ARG.to_owned(),
            wrapper_control.as_raw_fd().to_string(),
            file.as_raw_fd().to_string(),
        ],
        inherited_fds: wrapper_fds,
        control,
        _wrapper_control: wrapper_control,
        _request_file: file,
    })
}

/// Launch the trusted wrapper. The target waits for `start`, which the
/// caller sends after recording the process in its cancellation scope.
#[allow(clippy::too_many_arguments)]
pub async fn spawn(
    helper: &Path,
    command: &[String],
    cwd: &Path,
    env: &HashMap<String, String>,
    arg0: &Option<String>,
    tty: bool,
    stdin_open: bool,
    inherited_fds: &[RawFd],
) -> io::Result<(codex_utils_pty::SpawnedProcess, Arc<CommandControl>)> {
    let launch = prepare(helper, command, cwd, env, arg0, tty, inherited_fds)?;
    let mut spawned = codex_sandboxing::spawn_process(codex_sandboxing::SpawnRequest {
        command: &launch.argv,
        cwd: Path::new("/"),
        env: &HashMap::new(),
        arg0: &None,
        sandbox: codex_sandboxing::SandboxType::None,
        windows_sandbox: None,
        tty,
        stdin_open,
        inherited_fds: codex_utils_pty::ChildFds::Launchd(
            &launch.inherited_fds,
            inherited_fds.is_empty(),
        ),
    })
    .await
    .map_err(|error| io::Error::other(error.to_string()))?;
    let control = Arc::new(CommandControl::new(launch.control)?);
    spawned.session.set_stop_action({
        let control = Arc::clone(&control);
        move || control.stop()
    });
    Ok((spawned, control))
}

/// Check the complete ownership path before an SDK-managed local session
/// can accept model work. Seatbelt enforcement has its own launch probe.
pub async fn check_supported(helper: &Path) -> io::Result<()> {
    CoalitionOwner::check_supported()?;
    let (process, control) = spawn(
        helper,
        &["/usr/bin/true".to_owned()],
        Path::new("/"),
        &HashMap::new(),
        &None,
        /*tty*/ false,
        /*stdin_open*/ false,
        &[],
    )
    .await?;
    control.start()?;
    let verified = tokio::time::timeout(SETUP_TIMEOUT, async {
        control.wait_started().await?;
        if process.exit_rx.await.ok() != Some(0) || !control.wait_drained().await {
            return Err(io::Error::other(
                "managed local launchd readiness command did not finish",
            ));
        }
        Ok(())
    })
    .await;
    verified.unwrap_or_else(|_| {
        Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "managed local launchd readiness timed out",
        ))
    })
}

#[derive(Clone, Copy, Default)]
struct ControlState {
    started: bool,
    drained: bool,
    closed: bool,
}

pub struct CommandControl {
    socket: UnixStream,
    state: watch::Receiver<ControlState>,
}

impl CommandControl {
    fn new(socket: UnixStream) -> io::Result<Self> {
        socket.set_nonblocking(true)?;
        let mut reader = tokio::net::UnixStream::from_std(socket.try_clone()?)?;
        let (updates, state) = watch::channel(ControlState::default());
        tokio::spawn(async move {
            loop {
                match reader.read_u8().await {
                    Ok(STARTED) => updates.send_modify(|state| state.started = true),
                    Ok(DRAINED) => updates.send_modify(|state| state.drained = true),
                    _ => break,
                }
            }
            updates.send_modify(|state| state.closed = true);
        });
        Ok(Self { socket, state })
    }

    pub fn start(&self) -> io::Result<()> {
        (&self.socket).write_all(&[START])
    }

    pub fn interrupt(&self) -> io::Result<()> {
        (&self.socket).write_all(&[INTERRUPT])
    }

    pub fn stop(&self) {
        let _ = self.socket.shutdown(Shutdown::Write);
    }

    pub async fn wait_started(&self) -> io::Result<()> {
        let mut state = self.state.clone();
        match state.wait_for(|state| state.started || state.closed).await {
            Ok(state) if state.started => Ok(()),
            _ => Err(io::Error::other(
                "managed local launchd command did not start",
            )),
        }
    }

    pub async fn wait_drained(&self) -> bool {
        let mut state = self.state.clone();
        state
            .wait_for(|state| state.drained || state.closed)
            .await
            .is_ok_and(|state| state.drained)
    }

    pub fn is_drained(&self) -> bool {
        self.state.borrow().drained
    }
}

impl Drop for CommandControl {
    fn drop(&mut self) {
        self.stop();
    }
}

pub fn wrapper_main(args: impl Iterator<Item = std::ffi::OsString>) -> io::Result<i32> {
    let mut args = args;
    let control_fd = fd_arg(args.next())?;
    let request_fd = fd_arg(args.next())?;
    if control_fd == request_fd || args.next().is_some() {
        return Err(invalid_message());
    }
    for fd in [control_fd, request_fd] {
        // SAFETY: F_GETFD only checks whether the descriptor exists.
        if unsafe { libc::fcntl(fd, libc::F_GETFD) } == -1 {
            return Err(io::Error::last_os_error());
        }
    }
    // SAFETY: The private wrapper owns the descriptors from the native spawn.
    let mut control = unsafe { UnixStream::from_raw_fd(control_fd) };
    let request_file = unsafe { File::from_raw_fd(request_fd) };
    cloexec(&control)?;
    cloexec(&request_file)?;
    let mut request: JobRequest =
        serde_json::from_reader(request_file.take(MAX_REQUEST_SIZE as u64))?;
    if request.tty
        && !request.env.contains_key("SHELL")
        && let Ok(shell) = std::env::var("SHELL")
    {
        request.env.insert("SHELL".to_owned(), shell);
    }
    for fd in &request.inherited_fds {
        if *fd < 3 || *fd == control_fd || *fd == request_fd {
            return Err(invalid_message());
        }
        // SAFETY: The parent explicitly kept each of these descriptors open.
        cloexec(unsafe { BorrowedFd::borrow_raw(*fd) })?;
    }

    match read_byte(&mut control)? {
        Some(START) => {}
        None => {
            control.write_all(&[DRAINED])?;
            return Ok(128 + libc::SIGKILL);
        }
        Some(_) => return Err(invalid_message()),
    }

    let (mut job, mut worker) = LaunchdJob::connect()?;
    let owner = CoalitionOwner::acquire(&worker)?;
    if job.registered_pid()? != owner.helper_pid() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "socket peer is not the registered launchd command worker",
        ));
    }
    // A disconnected core may still have queued START. Once we send the
    // request, this exact coalition owns any resulting command.
    let result = run_managed(&mut control, &mut worker, &owner, &request);
    let _ = worker.shutdown(Shutdown::Write);
    owner.drain(DRAIN_TIMEOUT, /*keep_helper*/ false)?;
    job.finish()?;
    control.write_all(&[DRAINED])?;
    result
}

fn run_managed(
    control: &mut UnixStream,
    worker: &mut UnixStream,
    owner: &CoalitionOwner,
    request: &JobRequest,
) -> io::Result<i32> {
    worker.write_all(&owner.id().to_le_bytes())?;
    let bytes = serde_json::to_vec(request)?;
    if bytes.len() > MAX_REQUEST_SIZE {
        return Err(invalid_message());
    }
    let len = u32::try_from(bytes.len()).map_err(|_| invalid_message())?;
    worker.write_all(&len.to_le_bytes())?;
    worker.write_all(&bytes)?;
    for fd in [0, 1, 2].iter().chain(&request.inherited_fds) {
        // SAFETY: The wrapper holds the stdio and explicitly inherited fds.
        send_file_descriptor(&*worker, unsafe { BorrowedFd::borrow_raw(*fd) })?;
    }

    loop {
        let [core_ready, worker_ready] = readable([control, worker], None)?;
        if core_ready {
            match read_byte(control)? {
                Some(INTERRUPT) => worker.write_all(&[INTERRUPT])?,
                None => return Ok(128 + libc::SIGKILL),
                Some(_) => return Err(invalid_message()),
            }
        }
        if worker_ready {
            match read_byte(worker)? {
                Some(STARTED) => control.write_all(&[STARTED])?,
                Some(EXITED) => {
                    let mut status = [0; 4];
                    worker.read_exact(&mut status)?;
                    return Ok(i32::from_le_bytes(status));
                }
                Some(_) => return Err(invalid_message()),
                None => {
                    return Err(io::Error::other(
                        "launchd command worker exited before reporting status",
                    ));
                }
            }
        }
    }
}

pub fn job_main(args: impl Iterator<Item = std::ffi::OsString>) -> io::Result<i32> {
    let mut args = args;
    let directory = PathBuf::from(args.next().ok_or_else(invalid_message)?);
    let wrapper_pid = args
        .next()
        .and_then(|pid| pid.to_str()?.parse::<libc::pid_t>().ok())
        .filter(|pid| *pid > 1)
        .ok_or_else(invalid_message)?;
    if args.next().is_some() {
        return Err(invalid_message());
    }
    let mut wrapper = UnixStream::connect(directory.join("s"))?;
    if peer_pid(&wrapper)? != wrapper_pid {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "invalid Darwin command wrapper",
        ));
    }
    wrapper.set_read_timeout(Some(SETUP_TIMEOUT))?;
    wrapper.set_write_timeout(Some(SETUP_TIMEOUT))?;
    // The nonce is in the protected directory and never in a job argument.
    wrapper.write_all(&std::fs::read(directory.join("n"))?)?;

    let mut id = [0; 8];
    wrapper.read_exact(&mut id)?;
    let owner = CoalitionOwner::current(u64::from_le_bytes(id))?;
    let result = run_job(&mut wrapper);
    // If the wrapper dies, EOF transfers responsibility to this worker.
    // It has no further dispatch path and can drain while excluding itself.
    owner.drain(DRAIN_TIMEOUT, /*keep_helper*/ true)?;
    result
}

fn run_job(wrapper: &mut UnixStream) -> io::Result<i32> {
    let mut size = [0; 4];
    wrapper.read_exact(&mut size)?;
    let size = u32::from_le_bytes(size) as usize;
    if size > MAX_REQUEST_SIZE {
        return Err(invalid_message());
    }
    let mut bytes = vec![0; size];
    wrapper.read_exact(&mut bytes)?;
    let request: JobRequest = serde_json::from_slice(&bytes)?;
    let stdin = receive_file_descriptor(wrapper)?;
    let stdout = receive_file_descriptor(wrapper)?;
    let stderr = receive_file_descriptor(wrapper)?;
    let uses_portable_status = request.tty && request.inherited_fds.is_empty();
    let min_fd = request
        .inherited_fds
        .iter()
        .copied()
        .max()
        .unwrap_or(2)
        .checked_add(1)
        .ok_or_else(invalid_message)?;
    let mut inherited = Vec::<(OwnedFd, RawFd)>::new();
    for fd in request.inherited_fds {
        let descriptor = receive_file_descriptor(wrapper)?;
        let descriptor = rustix::io::fcntl_dupfd_cloexec(&descriptor, min_fd)?;
        inherited.push((descriptor, fd));
    }
    // Keep target numbers occupied before std spawns, so dup2 in the
    // pre-exec callback cannot overwrite its child-error reporting pipe.
    let mut reserved = Vec::new();
    for (source, target) in &inherited {
        // SAFETY: This trusted launchd worker is single-threaded. The source
        // is owned, and an unused target is reserved until spawn returns.
        unsafe {
            if libc::fcntl(*target, libc::F_GETFD) == -1 {
                if libc::dup2(source.as_raw_fd(), *target) == -1 {
                    return Err(io::Error::last_os_error());
                }
                let fd = OwnedFd::from_raw_fd(*target);
                cloexec(&fd)?;
                reserved.push(fd);
            }
        }
    }

    let (program, args) = request.command.split_first().ok_or_else(invalid_message)?;
    let mut command = Command::new(program);
    command
        .args(args)
        .current_dir(request.cwd)
        .env_clear()
        .envs(request.env);
    command.stdin(stdin).stdout(stdout).stderr(stderr);
    if let Some(arg0) = request.arg0 {
        command.arg0(arg0);
    }
    let tty = request.tty;
    // SAFETY: This callback only makes descriptor, session, and task-port calls.
    unsafe {
        command.pre_exec(move || {
            clear_launchd_ports()?;
            if tty {
                codex_utils_pty::pty::configure_child_terminal()?;
            } else if libc::setsid() == -1 {
                return Err(io::Error::last_os_error());
            }
            for (source, target) in &inherited {
                if libc::dup2(source.as_raw_fd(), *target) == -1 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    drop(command);
    drop(reserved);
    wrapper.write_all(&[STARTED])?;
    while let None = child.try_wait()? {
        if readable([wrapper], Some(POLL_INTERVAL))?[0] {
            match read_byte(wrapper)? {
                Some(INTERRUPT) => {
                    // The worker owns this unreaped child. The process group
                    // started with the same ID, which cannot yet be recycled.
                    unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGINT) };
                }
                None => return Ok(0),
                Some(_) => return Err(invalid_message()),
            }
        }
    }
    let status = child.wait()?;
    let status = if uses_portable_status {
        status.code().unwrap_or(1)
    } else {
        status
            .code()
            .or_else(|| status.signal().map(|signal| 128 + signal))
            .unwrap_or(-1)
    };
    wrapper.write_all(&[EXITED])?;
    wrapper.write_all(&status.to_le_bytes())?;
    Ok(0)
}

// The worker needs its launchd domain, but the payload must not inherit
// authority to start unrelated jobs. exec inherits these task port slots.
fn clear_launchd_ports() -> io::Result<()> {
    const TASK_BOOTSTRAP_PORT: libc::c_int = 4;
    const MACH_PORT_NULL: libc::mach_port_t = 0;
    unsafe extern "C" {
        fn mach_ports_register(
            task: libc::mach_port_t,
            ports: *const libc::mach_port_t,
            count: libc::mach_msg_type_number_t,
        ) -> libc::kern_return_t;
        fn task_set_special_port(
            task: libc::mach_port_t,
            which: libc::c_int,
            port: libc::mach_port_t,
        ) -> libc::kern_return_t;
    }

    let registered_ports = [MACH_PORT_NULL; 3];
    #[allow(deprecated)]
    // SAFETY: The forked child's libSystem has initialized its own task port.
    let task = unsafe { libc::mach_task_self_ };
    // SAFETY: These Mach calls clear the current task's port slots. The
    // registered port array is valid for the duration of the call.
    let cleared = unsafe {
        mach_ports_register(task, registered_ports.as_ptr(), 3) == libc::KERN_SUCCESS
            && task_set_special_port(task, TASK_BOOTSTRAP_PORT, MACH_PORT_NULL)
                == libc::KERN_SUCCESS
    };
    if cleared {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(libc::EPERM))
    }
}

struct LaunchdJob {
    _directory: tempfile::TempDir,
    service: Option<String>,
}

impl LaunchdJob {
    fn connect() -> io::Result<(Self, UnixStream)> {
        let directory = tempfile::Builder::new()
            .prefix("j-")
            .tempdir_in(codex_uds::prepare_shared_daemon_socket_directory()?)?;
        let socket = directory.path().join("s");
        let listener = UnixListener::bind(socket)?;
        listener.set_nonblocking(true)?;
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        std::fs::write(directory.path().join("n"), &nonce)?;
        let label = format!("com.openai.codex.sdk-command.{}", uuid::Uuid::new_v4());
        let plist = directory.path().join("job.plist");
        let program = std::env::current_exe()?;
        std::fs::write(
            &plist,
            format!(
                r#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array><string>{}</string><string>{JOB_ARG}</string><string>{}</string><string>{}</string></array>
<key>RunAtLoad</key><true/>
<key>LaunchOnlyOnce</key><true/>
</dict></plist>"#,
                xml_path(&program)?,
                xml_path(directory.path())?,
                std::process::id(),
            ),
        )?;

        // Select a single domain before any bootstrap side effect. Own its
        // service label even if launchctl times out after registering it.
        let uid = unsafe { libc::geteuid() };
        let deadline = Instant::now() + SETUP_TIMEOUT;
        let gui = format!("gui/{uid}");
        let domain = if launchctl(&["print".as_ref(), gui.as_ref()], deadline, false)?.success {
            gui
        } else {
            format!("user/{uid}")
        };
        let job = Self {
            _directory: directory,
            service: Some(format!("{domain}/{label}")),
        };
        if !launchctl(
            &["bootstrap".as_ref(), domain.as_ref(), plist.as_os_str()],
            deadline,
            false,
        )?
        .success
        {
            return Err(io::Error::other(
                "could not register a managed local launchd command",
            ));
        }

        loop {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    // Darwin's accept inherits the listener's nonblocking mode.
                    // The worker protocol uses blocking I/O with timeouts.
                    stream.set_nonblocking(false)?;
                    stream.set_read_timeout(Some(
                        deadline
                            .saturating_duration_since(Instant::now())
                            .max(POLL_INTERVAL),
                    ))?;
                    stream.set_write_timeout(Some(SETUP_TIMEOUT))?;
                    let mut proof = [0; 32];
                    stream.read_exact(&mut proof)?;
                    if proof != nonce.as_bytes() {
                        return Err(io::Error::new(
                            io::ErrorKind::PermissionDenied,
                            "invalid launchd worker proof",
                        ));
                    }
                    return Ok((job, stream));
                }
                Err(err)
                    if err.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline =>
                {
                    std::thread::sleep(POLL_INTERVAL);
                }
                Err(err) if err.kind() == io::ErrorKind::WouldBlock => {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "launchd command worker did not connect",
                    ));
                }
                Err(err) => return Err(err),
            }
        }
    }

    fn registered_pid(&self) -> io::Result<libc::pid_t> {
        let service = self.service.as_ref().ok_or_else(invalid_message)?;
        let result = launchctl(
            &["print".as_ref(), service.as_ref()],
            Instant::now() + SETUP_TIMEOUT,
            true,
        )?;
        let mut lines = result.stdout.lines();
        if !result.success
            || lines.next().and_then(|line| line.strip_suffix(" = {")) != Some(service.as_str())
        {
            return Err(io::Error::other(
                "could not inspect managed local launchd service",
            ));
        }
        let mut pids = lines
            .filter_map(|line| line.trim().strip_prefix("pid = "))
            .filter_map(|pid| pid.parse::<libc::pid_t>().ok());
        let pid = pids
            .next()
            .filter(|pid| *pid > 1)
            .ok_or_else(invalid_message)?;
        if pids.next().is_some() {
            return Err(invalid_message());
        }
        Ok(pid)
    }

    fn finish(&mut self) -> io::Result<()> {
        let Some(service) = &self.service else {
            return Ok(());
        };
        let deadline = Instant::now() + Duration::from_secs(1);
        let result = launchctl(&["bootout".as_ref(), service.as_ref()], deadline, false)?;
        if !result.success {
            let inspection = launchctl(&["print".as_ref(), service.as_ref()], deadline, true)?;
            let label = service.rsplit('/').next().ok_or_else(invalid_message)?;
            if inspection.success
                || !inspection.stderr.contains("Could not find service")
                || !inspection.stderr.contains(&format!("\"{label}\""))
            {
                return Err(io::Error::other(
                    "could not remove managed local launchd service",
                ));
            }
        }
        self.service = None;
        Ok(())
    }
}

impl Drop for LaunchdJob {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}

struct LaunchctlOutput {
    success: bool,
    stdout: String,
    stderr: String,
}

fn launchctl(
    args: &[&std::ffi::OsStr],
    deadline: Instant,
    capture: bool,
) -> io::Result<LaunchctlOutput> {
    let mut command = Command::new("/bin/launchctl");
    command.args(args).env_clear().stdin(Stdio::null());
    // Avoid waiting for a child whose output pipe is full.
    let mut output = None;
    if capture {
        let directory = codex_uds::prepare_shared_daemon_socket_directory()?;
        let stdout = tempfile::tempfile_in(&directory)?;
        let stderr = tempfile::tempfile_in(directory)?;
        command
            .stdout(stdout.try_clone()?)
            .stderr(stderr.try_clone()?);
        output = Some((stdout, stderr));
    } else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
    }
    let mut child = command.spawn()?;
    loop {
        if let Some(status) = child.try_wait()? {
            let (mut stdout, mut stderr) = (String::new(), String::new());
            if let Some((mut out, mut err)) = output {
                out.rewind()?;
                err.rewind()?;
                out.take(64 * 1024).read_to_string(&mut stdout)?;
                err.take(64 * 1024).read_to_string(&mut stderr)?;
            }
            return Ok(LaunchctlOutput {
                success: status.success(),
                stdout,
                stderr,
            });
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "launchctl did not finish",
            ));
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

fn peer_pid(socket: &UnixStream) -> io::Result<libc::pid_t> {
    let mut pid = 0;
    let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
    // Darwin's SOL_LOCAL=0, LOCAL_PEERPID=2. The kernel supplies the PID.
    let status = unsafe {
        libc::getsockopt(
            socket.as_raw_fd(),
            0,
            2,
            (&mut pid as *mut libc::pid_t).cast(),
            &mut len,
        )
    };
    if status == -1 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != std::mem::size_of::<libc::pid_t>() {
        return Err(invalid_message());
    }
    Ok(pid)
}

fn readable<const N: usize>(
    sockets: [&UnixStream; N],
    timeout: Option<Duration>,
) -> io::Result<[bool; N]> {
    let mut fds = sockets.map(|socket| libc::pollfd {
        fd: socket.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    });
    let millis = timeout.map_or(-1, |duration| duration.as_millis() as libc::c_int);
    loop {
        // SAFETY: fds supplies all N mutable poll descriptors.
        if unsafe { libc::poll(fds.as_mut_ptr(), N as libc::nfds_t, millis) } >= 0 {
            return Ok(fds.map(|fd| fd.revents != 0));
        }
        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            return Err(err);
        }
    }
}

fn read_byte(stream: &mut UnixStream) -> io::Result<Option<u8>> {
    let mut byte = [0];
    loop {
        match stream.read(&mut byte) {
            Ok(0) => return Ok(None),
            Ok(_) => return Ok(Some(byte[0])),
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }
    }
}

fn fd_arg(arg: Option<std::ffi::OsString>) -> io::Result<RawFd> {
    arg.and_then(|arg| arg.to_str()?.parse().ok())
        .filter(|fd| *fd > 2)
        .ok_or_else(invalid_message)
}

fn cloexec(fd: impl std::os::fd::AsFd) -> io::Result<()> {
    rustix::io::fcntl_setfd(fd, rustix::io::FdFlags::CLOEXEC)?;
    Ok(())
}

fn xml_path(path: &Path) -> io::Result<String> {
    Ok(path
        .to_str()
        .ok_or_else(invalid_message)?
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;"))
}

fn invalid_message() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid managed local Darwin command request",
    )
}
