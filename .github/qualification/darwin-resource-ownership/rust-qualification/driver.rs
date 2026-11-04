#[path = "../rust/coalition.rs"]
mod coalition;

use std::io;
use std::io::Read;
use std::io::Write;
use std::os::fd::FromRawFd;
use std::os::unix::net::UnixStream;
use std::time::Duration;

use coalition::CoalitionOwner;

const DRAIN_TIMEOUT: Duration = Duration::from_secs(4);

fn step(input: &mut impl Read, expected: u8) -> io::Result<()> {
    let mut command = [0];
    input.read_exact(&mut command)?;
    if command[0] == expected {
        Ok(())
    } else {
        Err(io::Error::other("unexpected qualification command"))
    }
}

fn main() -> io::Result<()> {
    let fd = std::env::args()
        .nth(1)
        .and_then(|fd| fd.parse::<i32>().ok())
        .filter(|fd| *fd > 2)
        .ok_or_else(|| io::Error::other("expected the inherited helper socket descriptor"))?;
    // SAFETY: The harness launches this binary with its accepted Unix socket
    // in pass_fds. This is the sole owner of that descriptor in this process.
    let peer = unsafe { UnixStream::from_raw_fd(fd) };
    CoalitionOwner::check_supported()?;
    let owner = CoalitionOwner::acquire(&peer)?;
    if CoalitionOwner::current(owner.id()).is_ok() {
        return Err(io::Error::other(
            "the wrapper unexpectedly belongs to the authorized helper coalition",
        ));
    }
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    writeln!(
        output,
        "READY {} {} {}",
        owner.helper_pid(),
        owner.id(),
        owner.live_tasks()?
    )?;
    output.flush()?;

    step(&mut input, b't')?;
    match owner.drain(Duration::ZERO, true) {
        Err(error) if error.kind() == io::ErrorKind::TimedOut => {
            writeln!(output, "TIMED_OUT {}", owner.live_tasks()?)?;
        }
        Err(error) => return Err(error),
        Ok(()) => return Err(io::Error::other("reported a drain with a live member")),
    }
    output.flush()?;

    step(&mut input, b'd')?;
    owner.drain(DRAIN_TIMEOUT, true)?;
    writeln!(output, "PARKED {}", owner.live_tasks()?)?;
    output.flush()?;

    step(&mut input, b'z')?;
    owner.drain(DRAIN_TIMEOUT, false)?;
    writeln!(output, "FINAL {}", owner.live_tasks()?)?;
    output.flush()
}
