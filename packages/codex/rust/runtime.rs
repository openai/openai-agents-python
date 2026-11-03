//! Companion executable for Codex's unmodified sandbox and execution helper modes.

#[cfg(target_os = "linux")]
mod linux_supervisor;

fn main() -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use std::path::Path;

        let mut args = std::env::args_os();
        let executable = args.next().expect("the runtime has argv[0]");
        // Only the SDK's explicit installed-runtime entrypoint owns this mode.
        // Codex's helper aliases keep their original argv and dispatch behavior.
        if Path::new(&executable).file_name() == Some("codex-sdk-runtime".as_ref())
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
