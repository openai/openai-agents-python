//! Companion executable for Codex's unmodified sandbox and execution helper modes.

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
            // The process and filesystem sandboxes use this alias to find
            // bubblewrap. Do not search host or workspace PATH entries.
            // SAFETY: this entry point runs before any helper thread starts.
            unsafe {
                std::env::set_var("PATH", local_sandbox::SYSTEM_EXECUTABLE_PATH);
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
