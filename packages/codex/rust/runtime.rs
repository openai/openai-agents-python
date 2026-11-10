//! Companion executable for the SDK's native sandbox and execution helpers.

#[cfg(target_os = "linux")]
mod linux_supervisor;
#[cfg(target_os = "linux")]
mod local_sandbox;

fn main() -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use std::path::Path;

        let mut args = std::env::args_os();
        let executable = args.next().expect("the runtime has argv[0]");
        let name = Path::new(&executable).file_name();
        if name == Some("codex-linux-sandbox".as_ref()) {
            if args.next().as_deref() == Some(local_sandbox::APPARMOR_USERNS_PROBE_ARG.as_ref())
                && args.next().is_none()
            {
                std::process::exit(i32::from(!apparmor_restricts_userns()));
            }
            // The process and filesystem sandboxes use this alias to find
            // bubblewrap. Do not search host or workspace PATH entries.
            // SAFETY: this entry point runs before any helper thread starts.
            unsafe {
                std::env::set_var(
                    "CODEX_SDK_BWRAP_SEARCH_PATH",
                    local_sandbox::SYSTEM_EXECUTABLE_PATH,
                );
                std::env::remove_var("CARGO_BIN_EXE_bwrap");
            }
        }
        // Only the SDK's explicit installed-runtime entrypoint owns this mode.
        // Codex's helper aliases keep their original argv and dispatch behavior.
        if name == Some("codex-sdk-runtime".as_ref())
            && args.next().as_deref() == Some("--sdk-unix-local".as_ref())
        {
            let helper = Path::new(&executable).with_file_name("codex-linux-sandbox");
            return linux_supervisor::run(&helper, args);
        }
    }
    codex_arg0::arg0_dispatch_or_else(|_| async {
        anyhow::bail!("this executable is an internal Codex execution helper")
    })
}

#[cfg(target_os = "linux")]
fn apparmor_restricts_userns() -> bool {
    // SAFETY: This one-shot helper is single-threaded and exits after the probe.
    if unsafe { libc::unshare(libc::CLONE_NEWUSER) } != 0 {
        return false;
    }
    // Ubuntu's stock policy allows creation but confines the new namespace.
    let restricted = std::fs::read_to_string("/proc/self/attr/apparmor/current")
        .is_ok_and(|label| label.trim_end() == "unprivileged_userns (enforce)");
    // SAFETY: If allowed, this creates a private network namespace for this helper alone.
    restricted
        && unsafe { libc::unshare(libc::CLONE_NEWNET) } == -1
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}
