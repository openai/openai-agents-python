//! Companion executable for Codex's sandbox, filesystem, apply-patch and SDK hook helpers.

use std::env;
use std::ffi::OsStr;
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, Shutdown, TcpStream};
use std::thread;

fn relay_stdio(port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let key = env::var("AGENTS_CODEX_HOOK_KEY")?;
    let mut connection = TcpStream::connect((Ipv4Addr::LOCALHOST, port))?;
    connection.write_all(key.as_bytes())?;
    let mut requests = connection.try_clone()?;
    thread::spawn(move || {
        let _ = io::copy(&mut io::stdin().lock(), &mut requests);
        let _ = requests.shutdown(Shutdown::Write);
    });
    // The process exits when the relay closes, even if Codex still holds stdin open.
    let mut stdout = io::stdout().lock();
    // On Linux, io::copy can hold the stdout pipe lock while waiting for the next reply.
    let mut buffer = [0; 8192];
    loop {
        let count = match connection.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(count) => count,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err.into()),
        };
        stdout.write_all(&buffer[..count])?;
        stdout.flush()?;
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os().skip(1);
    if args.next().as_deref() == Some(OsStr::new("--sdk-hook-stdio")) {
        let port = args
            .next()
            .and_then(|arg| arg.into_string().ok())
            .ok_or("Missing SDK hook relay port")?
            .parse::<u16>()?;
        return relay_stdio(port);
    }
    codex_arg0::arg0_dispatch_or_else(|_| async {
        eprintln!("this executable is an internal Codex execution helper");
        std::process::exit(1);
    })?;
    Ok(())
}
