//! Run the pinned exec-server implementation for container integration tests.

use codex_exec_server::{ExecServerRuntimeOptions, run_main};
use codex_http_client::{HttpClientFactory, OutboundProxyPolicy};

fn main() -> anyhow::Result<()> {
    codex_arg0::arg0_dispatch_or_else(|paths| async move {
        // Fresh-wheel checks run the installed helper at its profiled path.
        let helper = std::env::args_os()
            .nth(2)
            .map(Into::into)
            .or(paths.codex_linux_sandbox_exe);
        let runtime = ExecServerRuntimeOptions::new(std::env::current_exe()?, helper)?;
        let address = std::env::args()
            .nth(1)
            .unwrap_or_else(|| "ws://0.0.0.0:4500".to_owned());
        run_main(
            &address,
            runtime,
            HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
        )
        .await
        .map_err(anyhow::Error::from_boxed)
    })
}
