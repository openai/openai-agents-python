pub fn dispatch() {
    let mut args = std::env::args_os();
    args.next();
    let argv1 = args.next().unwrap_or_default();
    #[cfg(target_os = "macos")]
    if argv1 == codex_exec_server::darwin_command::WRAPPER_ARG
        || argv1 == codex_exec_server::darwin_command::JOB_ARG
    {
        let result = if argv1 == codex_exec_server::darwin_command::WRAPPER_ARG {
            codex_exec_server::darwin_command::wrapper_main(args)
        } else {
            codex_exec_server::darwin_command::job_main(args)
        };
        std::process::exit(result.unwrap_or_else(|error| {
            eprintln!("Managed local command failed: {error}");
            125
        }));
    }
}
