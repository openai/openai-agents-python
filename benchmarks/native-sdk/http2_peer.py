"""Optional TLS HTTP/1.1 + HTTP/2 transport for the existing synthetic peer."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import signal
import ssl
from types import SimpleNamespace

HYPERCORN_VERSION = "0.18.0"
MAX_BODY = 64 * 1024 * 1024


class WebSocket:
    """Adapt ASGI messages to the existing peer's WebSocket exchange."""

    def __init__(self, receive, send):
        self.receive, self.send = receive, send
        self.disconnected = False

    async def prepare(self, request):
        assert (await self.receive())["type"] == "websocket.connect"
        await self.send({"type": "websocket.accept"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        from aiohttp import WSMsgType

        event = await self.receive()
        if event["type"] == "websocket.disconnect":
            self.disconnected = True
            raise StopAsyncIteration
        assert event["type"] == "websocket.receive"
        return SimpleNamespace(
            type=WSMsgType.TEXT if event.get("text") is not None else WSMsgType.BINARY,
            data=event.get("text", event.get("bytes")),
        )

    async def send_str(self, text):
        await self.send({"type": "websocket.send", "text": text})

    async def send_json(self, value):
        await self.send_str(json.dumps(value))

    async def close(self):
        if not self.disconnected:
            self.disconnected = True
            await self.send({"type": "websocket.close", "code": 1000})


class Application:
    def __init__(self, peer, encode):
        self.peer, self.encode = peer, encode

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        from multidict import CIMultiDict

        assert scope["path"] == "/v1/responses"
        secure = scope["benchmark_ssl_object"]
        request = SimpleNamespace(
            headers=CIMultiDict(
                (key.decode("latin1"), value.decode("latin1"))
                for key, value in scope["headers"]
            ),
            transport=SimpleNamespace(get_extra_info=lambda name: secure),
            version=SimpleNamespace(
                major=int(scope["http_version"].split(".")[0]),
                minor=0 if scope["http_version"] == "2" else 1,
            ),
        )
        if scope["type"] == "websocket":
            await self.peer.websocket(request, socket=WebSocket(receive, send))
            return
        assert scope["type"] == "http" and scope["method"] == "POST"

        async def read():
            from aiohttp.web import HTTPRequestEntityTooLarge

            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    raise ConnectionError("Model request disconnected")
                assert event["type"] == "http.request"
                body.extend(event.get("body", b""))
                if len(body) > MAX_BODY:
                    raise HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=len(body))
                if not event.get("more_body", False):
                    return bytes(body)

        request.read = read
        started = False

        async def deliver(response, chunks, streaming, *, status=200):
            nonlocal started
            if started:
                raise ConnectionError("Response failed after its headers were sent")
            started = True
            headers = [(b"content-type", b"text/event-stream" if streaming else b"application/json")]
            if streaming:
                headers.append((b"cache-control", b"no-cache"))
                await send({"type": "http.response.start", "status": status, "headers": headers})
                events = self.peer.events(response, chunks)
                for index, event in enumerate(events):
                    frame = b"event: " + event["type"].encode() + b"\ndata: " + self.encode(event) + b"\n\n"
                    await send({"type": "http.response.body", "body": frame,
                                "more_body": index + 1 < len(events)})
                    self.peer.response_bytes += len(frame)
                    if event["type"] == "response.output_text.delta":
                        self.peer.emitted_text_chunks += 1
            else:
                body = self.encode(response)
                headers.append((b"content-length", str(len(body)).encode()))
                await send({"type": "http.response.start", "status": status, "headers": headers})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                self.peer.response_bytes += len(body)
            return True

        await self.peer.handle(request, deliver=deliver)


def connection_server(original):
    """Pin this private Hypercorn seam to the declared benchmark dependency."""

    class ObservedServer(original):
        async def _initiate_server_close(self):
            await super()._initiate_server_close()
            if self.context.terminated.is_set():
                # Whole-server shutdown must not wait on idle TLS close-notify.
                self.writer.transport.abort()

        async def run(self):
            secure = self.writer.get_extra_info("ssl_object")
            original_app = self.app

            async def tagged(scope, *args):
                return await original_app({**scope, "benchmark_ssl_object": secure}, *args)

            self.app = tagged
            return await super().run()

    return ObservedServer


async def serve(args, peer, encode):
    if importlib.metadata.version("hypercorn") != HYPERCORN_VERSION:
        raise RuntimeError("The dual peer requires hypercorn==" + HYPERCORN_VERSION)
    peer.http_server = {"mode": "dual", "hypercorn": HYPERCORN_VERSION,
                        "h2": importlib.metadata.version("h2")}
    from aiohttp import web
    from hypercorn.asyncio import serve as hypercorn_serve
    import hypercorn.asyncio.run as hypercorn_run
    from hypercorn.config import Config
    from hypercorn.logging import Logger

    stop, ready = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.tls_cert, args.tls_key)
    context.set_alpn_protocols(["h2", "http/1.1"])
    context.set_servername_callback(
        lambda connection, name, _: peer.tls_names.__setitem__(connection, name)
    )

    class ReadyLogger(Logger):
        async def info(self, message, *values, **kwargs):
            await super().info(message, *values, **kwargs)
            if message.startswith("Running on https://"):
                ready.set()

    class PeerConfig(Config):
        def create_ssl_context(self):
            return context

        def create_sockets(self):
            if not hasattr(self, "benchmark_sockets"):
                self.benchmark_sockets = super().create_sockets()
            return self.benchmark_sockets

    config = PeerConfig()
    config.bind = [f"127.0.0.1:{args.tls_port}"]
    config.certfile, config.keyfile = args.tls_cert, args.tls_key
    config.alpn_protocols = ["h2", "http/1.1"]
    config.logger_class = ReadyLogger
    config.accesslog = config.errorlog = None
    config.graceful_timeout = 2
    sockets = config.create_sockets()
    original_server = hypercorn_run.TCPServer
    hypercorn_run.TCPServer = connection_server(original_server)
    control = web.Application()

    async def stats(request):
        if request.query.get("retired") == "1":
            await asyncio.wait_for(peer.websockets_retired.wait(), timeout=10)
        return web.json_response(peer.snapshot(summary=request.query.get("summary") == "1"))

    control.router.add_get("/stats", stats)
    async def health(request):
        return web.json_response({"ok": True})

    control.router.add_get("/health", health)
    runner = web.AppRunner(control, access_log=None)
    await runner.setup()
    task = None
    try:
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        task = asyncio.create_task(
            hypercorn_serve(Application(peer, encode), config, shutdown_trigger=stop.wait, mode="asgi")
        )
        await asyncio.wait_for(ready.wait(), timeout=10)
        if task.done():
            await task
            raise RuntimeError("Dual peer stopped before readiness")
        port = sockets.secure_sockets[0].getsockname()[1]
        origin = "https://api.openai.com" if port == 443 else f"https://api.openai.com:{port}"
        print(json.dumps({"base_url": origin + "/v1",
                          "control_base_url": f"http://127.0.0.1:{runner.addresses[0][1]}/v1"}), flush=True)
        await stop.wait()
    finally:
        stop.set()
        try:
            if task is not None:
                await task
        finally:
            await runner.cleanup()
            hypercorn_run.TCPServer = original_server
            for socket in sockets.secure_sockets:
                socket.close()
            for name in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(name)
