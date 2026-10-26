# External sandbox provider package

This directory is a separately installable Python distribution. It demonstrates importing a client from outside `agents`, registering package-specific options through subclass creation, and passing the client to `SandboxRunConfig`. It has no entry-point registration and does not patch the SDK's import namespace.

`ExampleDockerClient` is a concrete adapter over the SDK's Docker backend, not an independently implemented container runtime. It translates its `ExampleDockerOptions` into Docker options and delegates session creation, cleanup, resume, and serialization to `DockerSandboxClient`. It intentionally keeps the `docker` backend ID and Docker state format. Use the built-in Docker client directly when you do not need an adapter; this package exists to demonstrate the external-package boundary.

Pass a typed `ExampleDockerOptions` instance to this adapter. `SandboxRunConfig` interprets dictionary options using the `docker` backend ID, so the adapter rejects those and other Docker options before creating a session. The example options also reject unknown fields. Use `DockerSandboxClient` directly when configuring networking or other Docker-specific settings.

## Install and run

From the SDK repository root, with Python 3.10+, `uv`, and a running Docker daemon:

```sh
uv venv /tmp/agents-provider-demo
uv pip install --python /tmp/agents-provider-demo/bin/python ./examples/sandbox/external_provider
docker pull python:3.14-slim
/tmp/agents-provider-demo/bin/python -m example_docker_provider.demo
```

On Windows, use the environment's `Scripts/python.exe` in place of `bin/python`. This example is not published on PyPI; install it from this directory. The package declares the SDK range `>=0.24.0,<0.25.0`; widen that range only after compatibility testing. To validate against your SDK checkout instead of a published SDK, install both local projects in the same command:

```sh
uv pip install --python /tmp/agents-provider-demo/bin/python . ./examples/sandbox/external_provider
```

Expected output:

```text
external provider works
```

The demo uses a real Docker container and a deterministic `ScriptedModel`. It needs no OpenAI API key, makes no model request, and disables trace export. Installing dependencies and pulling the image require network access. You can select an already available compatible image with `--image`; the image must supply the shell and filesystem tools required by the Docker backend. The default Docker networking behavior is unchanged.

The runner owns the session created through `client=` and performs session cleanup. The application owns and closes the Docker SDK connection. The adapter returns the delegate's instrumented session unchanged, so it does not create a second session wrapper or a competing cleanup mechanism.

See [Build a sandbox provider](../../../docs/sandbox/custom-providers.md) for the authoring contract, type registration, and the additional work required for a new execution runtime such as a micro-VM backend.
