"""Run inside the selected job using the installed SDK's exec_command."""

import errno
import http.client
import json
import os
import socket
import struct
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


def tcp_denied(family, address, port):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect((address, port))
    except OSError as error:
        assert error.errno in (
            errno.EPERM,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ECONNREFUSED,
        )
        return
    raise AssertionError(f"connected to {address}:{port}")


def proxy_request(method, target, headers):
    proxy = urlsplit(os.environ["HTTP_PROXY"])
    connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=10)
    try:
        connection.request(method, target, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def proxy_denied(host, port):
    for method, target in [("GET", f"http://{host}:{port}/"), ("CONNECT", f"{host}:{port}")]:
        status, body = proxy_request(method, target, {"Host": f"{host}:{port}"})
        assert status == 403, (method, status, body)


@contextmanager
def socks_request(command, destination):
    proxy = urlsplit(os.environ["ALL_PROXY"])
    with socket.create_connection((proxy.hostname, proxy.port), timeout=10) as client:
        with client.makefile("rwb") as data:
            client.sendall(b"\x05\x01\x00")
            assert data.read(2) == b"\x05\x00"
            client.sendall(bytes([5, command, 0]) + destination)
            version, result, reserved, family = data.read(4)
            assert (version, reserved) == (5, 0)
            af = {1: socket.AF_INET, 4: socket.AF_INET6}[family]
            address = socket.inet_ntop(af, data.read({1: 4, 4: 16}[family]))
            port = struct.unpack("!H", data.read(2))[0]
            yield result, af, (address, port), data


def socks_denied(host, port):
    target = b"\x03" + bytes([len(host)]) + host.encode() + struct.pack("!H", port)
    for command in [1, 3]:  # CONNECT and UDP ASSOCIATE
        destination = target if command == 1 else b"\x01" + bytes(6)
        with socks_request(command, destination) as (result, af, relay, _):
            if command == 1:
                assert result != 0
            elif result == 0:
                with socket.socket(af, socket.SOCK_DGRAM) as udp:
                    udp.settimeout(1)
                    udp.sendto(b"\x00\x00\x00" + target + b"job", relay)
                    try:
                        packet = udp.recv(512)
                    except TimeoutError:
                        pass
                    else:
                        raise AssertionError(f"unexpected UDP reply {packet!r}")


def exchange(path, challenge):
    with socket.socket(socket.AF_UNIX) as server:
        server.settimeout(5)
        server.bind(path)
        server.listen(1)
        pid = os.fork()
        if pid == 0:
            try:
                server.close()
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(path)
                    client.sendall(challenge)
                    assert client.recv(128) == challenge[::-1]
            except BaseException:
                os._exit(1)
            os._exit(0)
        try:
            connection, _ = server.accept()
            with connection:
                received = connection.recv(128)
                assert received == challenge
                connection.sendall(received[::-1])
        finally:
            _, status = os.waitpid(pid, 0)
            if not path.startswith("\0"):
                os.unlink(path)
        assert status == 0


def main():
    mode, token, challenge, v4, v6, hidden, inherited = sys.argv[1:]
    addresses = ("/job/peer.sock", "\0" + challenge)
    Path("/job/ordinary").write_text(challenge)
    for target in ["/outside/forbidden", "/job/.git/forbidden", "/tmp/forbidden"]:
        try:
            Path(target).write_text(token)
        except OSError as error:
            assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), (target, error)
        else:
            raise AssertionError(target)
    if mode == "restricted":
        for address in addresses:
            for operation in ("bind", "connect"):
                with socket.socket(socket.AF_UNIX) as peer:
                    try:
                        getattr(peer, operation)(address)
                    except PermissionError as error:
                        assert error.errno == errno.EPERM
                    else:
                        raise AssertionError((operation, address))
    else:
        assert mode == "permitted"
        for address in addresses:
            exchange(address, challenge.encode())
        for name in (hidden, "\x00" + token):
            with socket.socket(socket.AF_UNIX) as peer:
                try:
                    peer.connect(name)
                except OSError as error:
                    assert error.errno in (errno.ENOENT, errno.ECONNREFUSED), error
                else:
                    raise AssertionError("host Unix socket reached")
        for fd in Path("/proc/self/fd").iterdir():
            try:
                assert os.readlink(fd) != inherited, "inherited host connection"
            except FileNotFoundError:
                pass
        for family in (socket.AF_VSOCK, socket.AF_NETLINK):
            try:
                socket.socket(family, socket.SOCK_STREAM)
            except PermissionError as error:
                assert error.errno == errno.EPERM
            else:
                raise AssertionError(f"socket family {family} escaped the filter")
        proxy_denied("127.0.0.1", int(v4))
        host = f"{token}.job.ipc.test"
        proxy_denied(host, int(v4))
        socks_denied(host, int(v4))
        try:
            socket.getaddrinfo(host, 80)
        except socket.gaierror:
            pass
        else:
            raise AssertionError("host DNS resolved inside job")
    for family, address, port in [
        (socket.AF_INET, "127.0.0.1", int(v4)),
        (socket.AF_INET6, "::1", int(v6)),
        (socket.AF_INET, "192.0.2.1", 443),
        (socket.AF_INET6, "2001:db8::1", 443),
    ]:
        tcp_denied(family, address, port)
    print(json.dumps({"mode": mode, "challenge": challenge}))


def macos_command():
    mode, token, v4, v6 = sys.argv[1:]
    workspace = Path.cwd()
    (workspace / "ordinary").write_text(token)
    for protected in (workspace / ".git" / "protected", workspace.parent / "protected"):
        try:
            protected.write_text(token)
        except PermissionError:
            pass
        else:
            raise AssertionError(f"protected write succeeded: {protected}")

    socket_path = str(workspace / "peer.sock")
    listener = str(workspace / "control.sock")
    domains = mode in ("domains", "domains-deny-mapped", "domains-deny-only")
    if mode == "permitted":
        exchange(socket_path, token.encode())
    else:
        assert domains or mode == "restricted"
        for operation, path in (("bind", socket_path), ("connect", listener)):
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    getattr(client, operation)(path)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"ungranted Unix socket {operation} succeeded")
    if mode != "restricted":
        proxy = urlsplit(os.environ["HTTP_PROXY"])
        assert proxy.scheme == "http" and proxy.hostname == "127.0.0.1" and proxy.port
        url = f"http://127.0.0.1:{v4}/control"
        status, body = proxy_request("GET", url, {"X-Unix-Socket": listener})
        if mode == "permitted":
            assert (status, body) == (200, b"job-local-control")
            proxy_denied("127.0.0.1", int(v4))
        else:
            assert status == 403, (status, body)
            status, body = proxy_request("GET", url, {})
            if mode == "domains-deny-only":
                assert status == 403, (status, body)
            else:
                assert (status, body) == (200, b"job-local-control")
            proxy_denied("localhost", int(v4))
            proxy_denied("[::1]", int(v6))

    if domains:
        mapped = socket.inet_pton(socket.AF_INET6, "::ffff:127.0.0.1")
        destination = b"\x04" + mapped + struct.pack("!H", int(v4))
        with socks_request(1, destination) as (result, _, _, data):
            if mode != "domains":
                assert result == 2, f"expected SOCKS5 policy denial, got {result}"
            else:
                assert result == 0, result
                data.write(b"GET /control HTTP/1.0\r\n\r\n")
                data.flush()
                assert data.read().endswith(b"\r\n\r\njob-local-control")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
    else:
        for family, address, port in [
            (socket.AF_INET, "127.0.0.1", v4),
            (socket.AF_INET6, "::1", v6),
        ]:
            try:
                with socket.socket(family, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect((address, int(port)))
            except PermissionError:
                pass
            else:
                raise AssertionError(f"command reached the live TCP control at {address}:{port}")
    print(json.dumps({"mode": mode, "challenge": token}))


if __name__ == "__main__":
    if sys.platform == "darwin":
        macos_command()
    else:
        main()
