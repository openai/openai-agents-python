use std::io;
#[cfg(unix)]
pub(crate) fn send_file_descriptor(
    socket: impl std::os::fd::AsFd,
    file: std::os::fd::BorrowedFd<'_>,
) -> io::Result<()> {
    use rustix::net::SendAncillaryBuffer;
    use rustix::net::SendAncillaryMessage;
    use rustix::net::SendFlags;
    use std::io::IoSlice;

    let descriptors = [file];
    let mut space = [std::mem::MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(1))];
    let mut control = SendAncillaryBuffer::new(&mut space);
    if !control.push(SendAncillaryMessage::ScmRights(&descriptors)) {
        return Err(io::Error::other("missing file-descriptor control header"));
    }
    if rustix::net::sendmsg(
        socket,
        &[IoSlice::new(&[0])],
        &mut control,
        SendFlags::empty(),
    )? != 1
    {
        return Err(io::Error::other(
            "fs sandbox helper did not transfer its opened file descriptor",
        ));
    }
    Ok(())
}

// File-descriptor passing is only available on Unix.
#[cfg(unix)]
pub(crate) fn receive_file_descriptor(
    socket: &std::os::unix::net::UnixStream,
) -> io::Result<std::os::fd::OwnedFd> {
    use rustix::net::RecvAncillaryBuffer;
    use rustix::net::RecvAncillaryMessage;
    use rustix::net::RecvFlags;
    use rustix::net::ReturnFlags;
    use std::io::IoSliceMut;

    let mut byte = [0_u8];
    let mut buffers = [IoSliceMut::new(&mut byte)];
    let mut space = [std::mem::MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(1))];
    let mut control = RecvAncillaryBuffer::new(&mut space);
    // Linux can set close-on-exec while receiving the fd.
    #[cfg(target_os = "linux")]
    let flags = RecvFlags::CMSG_CLOEXEC;
    // Other Unix platforms need the non-atomic fcntl call below.
    #[cfg(not(target_os = "linux"))]
    let flags = RecvFlags::empty();
    let message = rustix::net::recvmsg(socket, &mut buffers, &mut control, flags)?;
    if message.bytes != 1 || message.flags.contains(ReturnFlags::CTRUNC) {
        return Err(io::Error::other("invalid file-descriptor control message"));
    }
    let descriptor = control
        .drain()
        .find_map(|message| match message {
            RecvAncillaryMessage::ScmRights(mut descriptors) => descriptors.next(),
            _ => None,
        })
        .ok_or_else(|| io::Error::other("missing transferred file descriptor"))?;
    // macOS cannot set this atomically, so the fd is briefly inheritable.
    // Shell and filesystem helper launches close inherited fds to limit that race.
    #[cfg(not(target_os = "linux"))]
    rustix::io::fcntl_setfd(&descriptor, rustix::io::FdFlags::CLOEXEC)?;
    Ok(descriptor)
}
