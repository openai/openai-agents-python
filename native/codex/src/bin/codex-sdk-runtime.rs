//! Companion executable for Codex's sandbox, filesystem and apply-patch helpers.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    codex_arg0::arg0_dispatch_or_else(|_| async {
        eprintln!("this executable is an internal Codex execution helper");
        std::process::exit(1);
    })?;
    Ok(())
}
