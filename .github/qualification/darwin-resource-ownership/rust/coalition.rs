use std::ffi::CStr;
use std::io;
use std::mem::MaybeUninit;
use std::os::fd::AsFd;
use std::os::fd::AsRawFd;
use std::sync::OnceLock;
use std::thread;
use std::time::Duration;
use std::time::Instant;

use libc::c_int;
use libc::c_void;
use libc::mach_port_t;
use libc::pid_t;

// XNU bsd/sys/proc_info_private.h and osfmk/mach/task_info.h.
const PROC_PIDUNIQIDENTIFIERINFO: c_int = 17;
const PROC_PIDCOALITIONINFO: c_int = 20;
const TASK_AUDIT_TOKEN: libc::task_flavor_t = 15;
const RESCAN_INTERVAL: Duration = Duration::from_millis(20);

#[repr(C)]
struct ProcUniqueIdentifierInfo {
    uuid: [u8; 16],
    unique_id: u64,
    parent_unique_id: u64,
    pid_version: u32,
    original_parent_pid_version: u32,
    reserved: [u64; 2],
}

const _: () = assert!(size_of::<ProcUniqueIdentifierInfo>() == 56);

#[derive(Clone, Copy, PartialEq, Eq)]
struct ProcessIdentity {
    unique_id: u64,
    pid_version: u32,
}

#[repr(transparent)]
struct AuditToken([u32; 8]);

impl AuditToken {
    fn pid(&self) -> pid_t {
        self.0[5] as pid_t
    }

    fn pid_version(&self) -> u32 {
        self.0[7]
    }
}

type Signal = unsafe extern "C" fn(*mut AuditToken, c_int) -> c_int;
type ResourceUsage = unsafe extern "C" fn(u64, *mut c_void, usize) -> c_int;
type TaskNameForPid = unsafe extern "C" fn(mach_port_t, pid_t, *mut mach_port_t) -> c_int;

unsafe extern "C" {
    fn mach_port_deallocate(task: mach_port_t, port: mach_port_t) -> c_int;
}

struct KernelApi {
    signal: Signal,
    resource_usage: ResourceUsage,
    task_name_for_pid: TaskNameForPid,
}

fn kernel_api() -> io::Result<&'static KernelApi> {
    static API: OnceLock<Result<KernelApi, String>> = OnceLock::new();
    API.get_or_init(|| {
        let libproc = c"/usr/lib/libproc.dylib";
        let libsystem = c"/usr/lib/libSystem.B.dylib";
        let signal = system_symbol(libproc, c"proc_signal_with_audittoken")?;
        let resource_usage = system_symbol(libsystem, c"coalition_info_resource_usage")?;
        let task_name_for_pid = system_symbol(libsystem, c"task_name_for_pid")?;
        // SAFETY: These are the exported functions with the signatures above.
        Ok(unsafe {
            KernelApi {
                signal: std::mem::transmute::<*mut c_void, Signal>(signal),
                resource_usage: std::mem::transmute::<*mut c_void, ResourceUsage>(resource_usage),
                task_name_for_pid: std::mem::transmute::<*mut c_void, TaskNameForPid>(
                    task_name_for_pid,
                ),
            }
        })
    })
    .as_ref()
    .map_err(|error| io::Error::new(io::ErrorKind::Unsupported, error.clone()))
}

fn system_symbol(library: &CStr, name: &CStr) -> Result<*mut c_void, String> {
    // SAFETY: Both inputs are NUL-terminated and name system library exports.
    let handle = unsafe { libc::dlopen(library.as_ptr(), libc::RTLD_LOCAL | libc::RTLD_NOW) };
    if !handle.is_null() {
        // SAFETY: dlopen returned a valid handle.
        let address = unsafe { libc::dlsym(handle, name.as_ptr()) };
        if !address.is_null() {
            // Retain the handle for the lifetime of the cached function pointer.
            return Ok(address);
        }
        // SAFETY: No pointer from this handle is retained.
        unsafe { libc::dlclose(handle) };
    }
    Err(format!(
        "managed local execution requires macOS support for {}",
        name.to_string_lossy()
    ))
}

/// Ownership of a dedicated resource coalition created by launchd.
///
/// The caller authenticates the registered job and keeps the helper from
/// spawning work until acquisition succeeds. Once draining begins, the caller
/// must fence dispatch and keep the helper parked or let it exit.
pub(crate) struct CoalitionOwner {
    id: u64,
    helper_pid: pid_t,
    helper_identity: ProcessIdentity,
}

impl CoalitionOwner {
    pub(crate) fn check_supported() -> io::Result<()> {
        kernel_api().map(|_| ())
    }

    pub(crate) fn acquire(peer: &impl AsFd) -> io::Result<Self> {
        let api = kernel_api()?;
        let token = peer_audit_token(peer)?;
        // SAFETY: These process identity getters do not require arguments.
        let (uid, euid, self_pid) = unsafe { (libc::getuid(), libc::geteuid(), libc::getpid()) };
        if token.0[1] != euid || token.0[3] != uid || token.pid() == self_pid {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "launchd helper peer identity does not match the managed caller",
            ));
        }
        let (helper_identity, id) = verified_membership(&token)?;
        if id == process_coalition(self_pid)? {
            return Err(io::Error::other(
                "launchd helper shares the caller's resource coalition",
            ));
        }
        let owner = Self {
            id,
            helper_pid: token.pid(),
            helper_identity,
        };
        if owner.live_tasks()? != 1 {
            return Err(io::Error::other(
                "launchd helper must be alone in its coalition before dispatch",
            ));
        }
        // Check that this host permits the read-only task-name lookup before
        // dispatching any payload.
        let helper_token = task_audit_token(api, owner.helper_pid)?;
        if helper_token.0 != token.0 {
            return Err(io::Error::other("launchd helper audit token changed"));
        }
        owner.require_helper()?;
        Ok(owner)
    }

    /// Used by the helper after the wrapper has authorized this coalition ID.
    pub(crate) fn current(expected_id: u64) -> io::Result<Self> {
        let api = kernel_api()?;
        // SAFETY: getpid does not require arguments.
        let pid = unsafe { libc::getpid() };
        let token = task_audit_token(api, pid)?;
        let (helper_identity, id) = verified_membership(&token)?;
        if token.pid() != pid || id != expected_id {
            return Err(io::Error::other(
                "helper is outside the authorized resource coalition",
            ));
        }
        Ok(Self {
            id,
            helper_pid: pid,
            helper_identity,
        })
    }

    pub(crate) fn id(&self) -> u64 {
        self.id
    }

    pub(crate) fn helper_pid(&self) -> pid_t {
        self.helper_pid
    }

    pub(crate) fn live_tasks(&self) -> io::Result<u64> {
        let api = kernel_api()?;
        // XNU copies only the requested prefix of coalition_resource_usage.
        // The first two u64s are tasks_started and tasks_exited, read together
        // under the coalition lock.
        let mut counts = [0_u64; 2];
        // SAFETY: counts is writable for the exact size supplied.
        let result = unsafe {
            (api.resource_usage)(self.id, counts.as_mut_ptr().cast(), size_of_val(&counts))
        };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        counts[0]
            .checked_sub(counts[1])
            .ok_or_else(|| io::Error::other("invalid resource coalition task counts"))
    }

    /// Keep the helper alive while it reports the command outcome. The wrapper
    /// can then let it exit and perform a second drain for zero live tasks.
    pub(crate) fn drain(&self, timeout: Duration, keep_helper: bool) -> io::Result<()> {
        let api = kernel_api()?;
        // SAFETY: getpid does not require arguments.
        if !keep_helper && self.helper_pid == unsafe { libc::getpid() } {
            return Err(io::Error::other("a coalition helper must preserve itself"));
        }
        let started = Instant::now();
        let mut last_failure = None;
        loop {
            let live = match self.live_tasks() {
                Ok(live) => live,
                // XNU can reap a previously observed coalition only after its
                // last task has left. The ID is never reused during this boot.
                Err(err) if !keep_helper && is_gone(&err) => return Ok(()),
                Err(err) => return Err(err),
            };
            if keep_helper {
                self.require_helper()?;
            }
            if live == u64::from(keep_helper) {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                let detail = last_failure.unwrap_or_default();
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    format!(
                        "resource coalition {} still has {live} live tasks{detail}",
                        self.id
                    ),
                ));
            }

            // Enumeration can race fork, exit, and exec. It locates targets,
            // while the kernel's task counts above establish completion.
            for pid in list_pids()? {
                if started.elapsed() >= timeout {
                    break;
                }
                if pid <= 1 || process_coalition(pid).ok() != Some(self.id) {
                    continue;
                }
                match self.kill_member(api, pid, keep_helper) {
                    Ok(()) => {}
                    Err(err) if is_gone(&err) => {}
                    Err(err) => {
                        last_failure = Some(format!(" (last target error for PID {pid}: {err})"))
                    }
                }
            }
            thread::sleep(RESCAN_INTERVAL.min(timeout.saturating_sub(started.elapsed())));
        }
    }

    fn require_helper(&self) -> io::Result<()> {
        if process_identity(self.helper_pid)? == self.helper_identity {
            Ok(())
        } else {
            Err(io::Error::other("launchd helper identity changed"))
        }
    }

    fn kill_member(&self, api: &KernelApi, pid: pid_t, keep_helper: bool) -> io::Result<()> {
        let mut token = task_audit_token(api, pid)?;
        if token.pid() != pid {
            return Err(io::Error::from_raw_os_error(libc::ESRCH));
        }
        let (identity, id) = verified_membership(&token)?;
        if id == self.id
            && !(keep_helper && pid == self.helper_pid && identity == self.helper_identity)
        {
            // SAFETY: token is the complete audit token returned by the
            // kernel. XNU validates its PID and version at signal delivery.
            let error = unsafe { (api.signal)(&mut token, libc::SIGKILL) };
            if error != 0 {
                return Err(io::Error::from_raw_os_error(error));
            }
        }
        Ok(())
    }
}

fn is_gone(error: &io::Error) -> bool {
    error.raw_os_error() == Some(libc::ESRCH)
}

fn list_pids() -> io::Result<Vec<pid_t>> {
    // SAFETY: A null buffer asks libproc for the current capacity estimate.
    let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    if count <= 0 {
        return Err(io::Error::last_os_error());
    }
    let capacity = count as usize;
    let bytes = c_int::try_from(capacity * size_of::<pid_t>())
        .map_err(|_| io::Error::other("process list size exceeds libproc capacity"))?;
    let mut pids = vec![0; capacity];
    // SAFETY: pids has writable space for bytes bytes.
    let count = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
    if count <= 0 {
        return Err(io::Error::last_os_error());
    }
    pids.truncate(count as usize);
    Ok(pids)
}

fn peer_audit_token(peer: &impl AsFd) -> io::Result<AuditToken> {
    let mut token = AuditToken([0; 8]);
    let expected = size_of::<AuditToken>() as libc::socklen_t;
    let mut size = expected;
    // SAFETY: The socket is borrowed and token and size are writable buffers.
    let result = unsafe {
        libc::getsockopt(
            peer.as_fd().as_raw_fd(),
            libc::SOL_LOCAL,
            libc::LOCAL_PEERTOKEN,
            (&raw mut token).cast(),
            &mut size,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    if size != expected {
        return Err(io::Error::other("invalid launchd helper peer audit token"));
    }
    Ok(token)
}

fn task_audit_token(api: &KernelApi, pid: pid_t) -> io::Result<AuditToken> {
    #[allow(deprecated)]
    // SAFETY: The Mach task self port is initialized by libSystem at startup.
    let self_port = unsafe { libc::mach_task_self_ };
    let mut port = 0;
    // SAFETY: port is writable. This requests only a read-only task name right.
    let result = unsafe { (api.task_name_for_pid)(self_port, pid, &mut port) };
    if result != 0 || port == 0 {
        return Err(io::Error::other(format!(
            "read-only task name lookup for PID {pid} failed (Mach {result})"
        )));
    }
    let mut token = AuditToken([0; 8]);
    let mut count = token.0.len() as u32;
    // SAFETY: The live send right from task_name_for_pid is ours. TASK_AUDIT_TOKEN
    // writes at most count 32-bit words, and the port is released on either path.
    let result = unsafe {
        let result = libc::task_info(
            port,
            TASK_AUDIT_TOKEN,
            token.0.as_mut_ptr().cast(),
            &mut count,
        );
        mach_port_deallocate(self_port, port);
        result
    };
    if result != 0 || count as usize != token.0.len() {
        return Err(io::Error::other(format!(
            "task audit token lookup for PID {pid} failed (Mach {result}, {count} words)"
        )));
    }
    Ok(token)
}

fn verified_membership(token: &AuditToken) -> io::Result<(ProcessIdentity, u64)> {
    let pid = token.pid();
    if pid <= 1 {
        return Err(io::Error::other("invalid managed process audit token"));
    }
    let before = process_identity(pid)?;
    if before.pid_version != token.pid_version() {
        return Err(io::Error::from_raw_os_error(libc::ESRCH));
    }
    // The two identity reads bind the numeric PID membership lookup to the
    // incarnation in the token. Coalition membership is fixed for that task.
    let id = process_coalition(pid)?;
    if before != process_identity(pid)? {
        return Err(io::Error::from_raw_os_error(libc::ESRCH));
    }
    Ok((before, id))
}

fn process_coalition(pid: pid_t) -> io::Result<u64> {
    // SAFETY: PROC_PIDCOALITIONINFO writes five u64s. Entry zero is the
    // resource coalition ID. The other entries are unused here.
    let info: [u64; 5] = unsafe { read_pid_info(pid, PROC_PIDCOALITIONINFO)? };
    if info[0] == 0 {
        Err(io::Error::other("process has no resource coalition"))
    } else {
        Ok(info[0])
    }
}

fn process_identity(pid: pid_t) -> io::Result<ProcessIdentity> {
    // SAFETY: ProcUniqueIdentifierInfo matches the XNU layout for this flavor.
    let info: ProcUniqueIdentifierInfo = unsafe { read_pid_info(pid, PROC_PIDUNIQIDENTIFIERINFO)? };
    Ok(ProcessIdentity {
        unique_id: info.unique_id,
        pid_version: info.pid_version,
    })
}

// SAFETY: T must match the kernel ABI for flavor and accept any bit pattern.
unsafe fn read_pid_info<T>(pid: pid_t, flavor: c_int) -> io::Result<T> {
    let mut info = MaybeUninit::<T>::uninit();
    let size = size_of::<T>() as c_int;
    // SAFETY: The buffer is writable for size bytes and the caller provides
    // the expected layout. An argument of zero excludes zombies.
    let count = unsafe { libc::proc_pidinfo(pid, flavor, 0, info.as_mut_ptr().cast(), size) };
    if count <= 0 {
        return Err(io::Error::last_os_error());
    }
    if count != size {
        return Err(io::Error::other(format!(
            "proc_pidinfo for PID {pid}, flavor {flavor} returned {count} of {size} bytes"
        )));
    }
    // SAFETY: A full successful read initialized the expected ABI buffer.
    Ok(unsafe { info.assume_init() })
}
