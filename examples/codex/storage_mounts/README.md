# Native local storage mounts

`Environment.local(mounts=...)` lets native tools read an existing directory
outside their writable workspace. On Linux, the native sandbox binds the source
at the same path with read-only access. Shell commands and native file tools use
that policy. The SDK does not copy the directory.

Run this example with a build that includes storage mounts. Supply credentials
through your normal SDK configuration. Create a workspace separate from your
data directory, then run from the repository root:

```sh
uv run python examples/codex/storage_mounts/main.py /absolute/workspace /absolute/data
```

The example accepts existing directories and resolves their paths in trusted
application code. Pass `strategy="local_bind"` explicitly. `read_only` defaults
to `True`. Equivalent dictionaries accept only `source`, `destination`,
`strategy`, and `read_only`.

## Supported paths

Source and destination must name the same existing canonical absolute Linux
directory. Mount paths cannot contain symlinks. Mounts cannot overlap each other,
the working directory, writable roots, or private SDK state. Native validation
checks filesystem aliases as well as path spelling. Keep SDK storage profiles,
runtime homes, and saved logins outside mounted data.

Remote executors, macOS, Windows, relocated destinations, writable mounts, and
other strategies or providers are unsupported. Use `writable_roots` when tools
need write access to an additional directory. The SDK rejects replacement of
`writable_roots` while mounts are active because replacement rebuilds the
filesystem policy.

An already mounted FUSE directory can qualify as a local path under these same
rules. The application must establish and maintain that filesystem. This API
does not configure FUSE, cloud credentials, object stores, or storage providers.

## Ownership and lifetime

The caller owns the source directory, its contents, and its ancestors. Keep the
host and source ancestors trusted while sessions run. Read-only access applies
to the sandbox view; external writers can still change the source. The mount is
not a snapshot.

Native command namespaces own the bind mounts. Existing session cancellation
and cleanup retire those namespaces. The SDK never unmounts the host directory,
changes its permissions, or deletes caller data, including after startup failure
or cancellation. A checkpoint supplies no mount authority. Restore with the
current trusted `Environment`; the saved executor-policy receipt must match its
effective read and write policy.

Linux isolation tests require a host that can run the native sandbox. Tests
marked `requires_native_linux_sandbox` skip inside the outer Codex sandbox.
Those skips do not prove read-only enforcement; use matching native CI coverage.
