# Default-host container smoke

The `--default-host-setup` profile has been exercising the normal `https://api.openai.com/v1` address against a local TLS peer. Container-only hostname mapping and a temporary trusted certificate authority (CA) have been preserving hostname verification without a custom endpoint or certificate override.

Supported workload: fresh, final-only, no-tool calls with the default streaming response mode. This example has been selecting native HTTP/SSE. Buffered responses, retained Sessions, tool calls and live observation workloads have been remaining outside this profile.

## Prerequisites

- Linux x86-64, Docker, `uv` and the GNU `timeout` command.
- One existing native wheel for the selected source revision, with its recorded SHA-256. Required compatibility: Ubuntu 24.04/glibc 2.39 and the container's architecture.
- A complete, relocatable standard CPython 3.13.16 directory, including `bin`, `lib` and its bundled libraries. The same copied interpreter for SDK and peer environments.
- Package access during image provisioning; offline networking during the smoke. No SDK source build in this example.

The supplied three-line Dockerfile has been providing OS setup tools only. It has not been provisioning Python 3.13.16 or SDK environments. The complete image recipe below has been adding those prerequisites. Explicit `certifi==2026.10.23` in both environments: setup has been importing certifi for every interpreter before installing the temporary CA. Neither SDK nor peer dependencies have been guaranteeing that import transitively.

## Provision the image

Starting directory: the repository root. `WHEEL` and `CPYTHON_DIR` below: absolute paths to the existing wheel and complete Python directory. Fresh `LAB`: separate from the SDK checkout and any measured environment.

```sh
set -eu
BENCH="$PWD/benchmarks/native-sdk"
WHEEL="/absolute/path/to/exact-native-wheel.whl"
CPYTHON_DIR="/absolute/path/to/cpython-3.13.16-linux-x86_64-gnu"
LAB="$(mktemp -d /tmp/agents-default-host.XXXXXX)"
CTX="$LAB/context"
mkdir "$CTX" "$LAB/output" "$LAB/docker-config"
export DOCKER_CONFIG="$LAB/docker-config"
IMAGE="agents-default-host-smoke-$$:local"
CONTAINER="agents-default-host-smoke-$$"
sha256sum "$WHEEL" > "$LAB/wheel.sha256"
cp -a "$CPYTHON_DIR" "$CTX/python"
cp "$(command -v uv)" "$CTX/uv"
cp "$WHEEL" "$CTX/native.whl"
basename "$WHEEL" > "$CTX/wheel-name.txt"
cp -a "$BENCH" "$CTX/bench"
cat > "$CTX/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates openssl libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY python/ /opt/python/
COPY uv /usr/local/bin/uv
COPY native.whl wheel-name.txt /input/
COPY bench/ /bench/
RUN wheel_name="$(cat /input/wheel-name.txt)" && mv /input/native.whl "/input/$wheel_name" \
    && uv venv --python /opt/python/bin/python3.13 /opt/sdk \
    && uv venv --python /opt/python/bin/python3.13 /opt/peer \
    && uv pip install --no-build --compile-bytecode --python /opt/sdk/bin/python "/input/$wheel_name" 'pydantic==2.12.3' 'typing-extensions==4.16.0' 'jsonschema==4.26.0' 'referencing==0.37.0' 'certifi==2026.10.23' \
    && uv pip install --no-build --compile-bytecode --python /opt/peer/bin/python 'aiohttp==3.14.3' 'openai==3.41.0' 'pydantic==2.12.3' 'certifi==2026.10.23' \
    && uv pip freeze --python /opt/sdk/bin/python > /input/sdk-packages.txt \
    && uv pip freeze --python /opt/peer/bin/python > /input/peer-packages.txt
DOCKERFILE
timeout --signal=TERM --kill-after=15s 600s \
  docker build --rm --force-rm --tag "$IMAGE" "$CTX" > "$LAB/build.log" 2>&1
docker image inspect "$IMAGE" > "$LAB/image.json"
```

The pins have been selecting direct dependencies; complete installed versions have been remaining in the image's two package inventories. A writable task-local `DOCKER_CONFIG` has been avoiding writes to the host's Docker configuration. No host credentials, home, runtime, trust-store or Docker-socket mounts in the smoke container.

## Run one offline functional smoke

Root inside the disposable container: required for CA installation and port 443. Container writable layer only for OS/certifi trust changes and keys; output-directory mount only for results. No host trust changes.

```sh
cleanup_container() {
  docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup_container EXIT INT TERM
timeout --signal=TERM --kill-after=15s 180s \
  docker run --rm --init --name "$CONTAINER" --network none \
    --add-host api.openai.com:127.0.0.1 \
    --mount "type=bind,src=$LAB/output,dst=/out" \
    "$IMAGE" env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    /bin/sh -ec '
      /opt/peer/bin/python -B /bench/container-default-host/setup.py \
        --directory /run/sdk-benchmark \
        --python /opt/sdk/bin/python --python /opt/peer/bin/python
      cp /input/sdk-packages.txt /input/peer-packages.txt /out/
      exec env SDK_BENCH_SERVER_PYTHON=/opt/peer/bin/python \
        OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1 PYTHONHASHSEED=0 \
        /opt/sdk/bin/python -B /bench/worker.py --engine v1 \
        --default-host-setup /run/sdk-benchmark/setup.json \
        --native-transport http --case oneshot_text --iterations 1 \
        --sample-index 0 --output /out/native-smoke.json
    ' > "$LAB/container.log" 2>&1
test -z "$(docker ps --all --quiet --filter "name=^/${CONTAINER}$")"
docker image rm "$IMAGE"
rm -rf -- "$CTX"
```

Expected result: exit 0, `errors: 0`, `server.errors: 0`, `server.requests: 2`, `default_host: true` and `wire_transport: "sse"` in `native-smoke.json`. One prefill and one measured call, each in a fresh conversation. The worker has been checking exact output, input/history hashes, actual Host/SNI and framing. Container absence after exit and explicit image removal: required cleanup checks. On failure or timeout, the trap has been removing the exact container. Docker-state reconciliation: still required after build cancellation or a partial image. Logs and package inventories in `LAB`; deletion after review and export.

A functional smoke has been passing on Ubuntu 24.04/glibc 2.39 with CPython 3.13.16 and one native wheel: two HTTP/SSE requests, zero worker/peer errors, and joined cleanup. That proof has been covering one native worker, not the comparison controller, stable SDK, WebSockets, HTTP/2, other wheels or complete workload suite. This procedure has been retaining worker timing fields without supporting a performance or memory claim.
