//! The unchanged native Code Mode host, packaged beside the Python extension.
#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    codex_code_mode_host::run_main(codex_code_mode_host::DEFAULT_LISTEN_URL).await
}
