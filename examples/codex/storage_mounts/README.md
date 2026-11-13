# Storage mounts for native sessions

`Environment.local(mounts=...)` lets native tools read an existing directory outside their writable workspace. On Linux, the native sandbox binds the source at the same path with read-only access. Shell commands and native file tools use that policy. The SDK does not copy the directory.

For the local-mount example, install the [alpha.7 wheel and matching example archive](../../../docs/sdk/installation.md#examples-and-upgrades), or use a [matching source build](../../../CONTRIBUTING.md#build-the-sdk-from-source). Activate that build's Python environment. This example requires Linux and a host that supports the native sandbox. Supply credentials through your normal SDK configuration; model requests can incur charges. Create a workspace separate from your data directory, then run from the repository root:

```sh
python examples/codex/storage_mounts/main.py /absolute/workspace /absolute/data
```

The model lists the mounted files and describes their names. Its exact answer varies. Session exit retires the native mounts and leaves both directories intact.

The example accepts existing directories and resolves their paths in trusted application code. `LocalStorageMount(source=path)` defaults `destination` to `source`, `strategy` to `"local_bind"`, and `read_only` to `True`. Equivalent dictionaries accept only `source`, `destination`, `strategy`, and `read_only`, with the same defaults.

## Supported paths

Source and destination must name the same existing canonical absolute Linux directory. Mount paths cannot contain symlinks. Mounts cannot overlap each other, the working directory, writable roots, or private SDK state. Native validation checks filesystem aliases as well as path spelling. Keep SDK storage profiles, runtime homes, and saved logins outside mounted data.

At preflight, mount entries may only be regular files, directories, or symlinks. Named pipes (FIFOs), Unix sockets, devices, and other special nodes are rejected without opening them. The check never follows symlinks.

Preflight also rejects an existing regular file shared by hard links between a mount and the working directory or an additional writable root. Links wholly inside read-only mounts or outside writable grants are allowed.

Each validation traverses the mounted directory trees. The SDK must be able to list every source directory and inspect its entries. If any source file has multiple links, validation also traverses the writable grants once. Otherwise it skips that writable scan. Required inspection failures stop startup. The traversal cost grows with the entries in those directory trees; there is no silent scan limit.

Remote executors, macOS, Windows, relocated destinations, writable mounts, and other strategies or providers are unsupported. Use `writable_roots` when tools need write access to an additional directory. The SDK rejects replacement of `writable_roots` while mounts are active because replacement rebuilds the filesystem policy.

An already mounted FUSE directory can qualify as a local path under these same rules. The application must establish and maintain that filesystem. This API does not configure FUSE, cloud credentials, object stores, or storage providers.

## Ownership and lifetime

The caller owns the source directory, its contents, and its ancestors. Keep the host and source ancestors trusted while sessions run. Read-only access applies to the sandbox view; external writers can still change the source. The mount is not a snapshot.

The entry-type and hardlink checks cover only the tree seen during validation. They do not monitor later nodes, links, replacements, or host writes. Keep the source stable during validation. The application must trust host processes and source changes throughout the session.

Native command namespaces own the bind mounts. Existing session cancellation and cleanup retire those namespaces. The SDK never unmounts the host directory, changes its permissions, or deletes caller data, including after startup failure or cancellation. A checkpoint supplies no mount authority. Restore with the current trusted `Environment`; the saved executor-policy receipt must match its effective read and write policy.

Linux isolation tests require a host that can run the native sandbox. Tests marked `requires_native_linux_sandbox` skip inside the outer Codex sandbox. Those skips do not prove read-only enforcement; use matching native CI coverage.

## Provider storage for native sessions

This section is an integration scaffold. Your application supplies an authenticated executor connector, prepared provider resources, and storage credentials. The managed-workspace controls below require a [matching source build](../../../CONTRIBUTING.md#build-the-sdk-from-source). For alpha.7, use its [provider-storage guide](https://github.com/openai/openai-agents-python/blob/v0.24.0-alpha.7/examples/codex/storage_mounts/README.md#provider-storage-for-native-sessions) and example files.

`agents.sandbox.sandbox_environment` adapts the existing sandbox resource clients to native `Agent` sessions. It uses their typed `Manifest` and mount pipeline; it does not run a Classic agent or another model loop. The shared resource classes keep their existing `agents.classic` identities. Install the dependencies required by the selected resource provider.

Pass a client, an application-authorized `Manifest`, trusted provider options, and `connect`. Both the adapter and `render_storage_layout` require a `Manifest` instance; they do not parse manifest dictionaries. Validate untrusted values before constructing a manifest. Construction alone does not authorize its paths, resources, or credential selections. Provider-option dictionaries require the existing `type` discriminator; typed provider options also work. The adapter captures the manifest and options before creation. Every mount must specify its `mount_strategy`; the adapter does not infer or replace strategies.

`connect(session)` receives the concrete `SandboxSession` returned by that client, after `session.start()` completes. It must return an async context manager that connects an authenticated native executor gateway to **that exact sandbox** and yields an `Environment`. The image and host must support the native executor's standard sandbox. An ordinary container may block the nested user namespaces that Linux isolation needs. Supplying a URL to an unrelated executor does not connect the mounted storage to native tools.

The adapter does not deploy a WSS gateway, install storage drivers, provision cloud accounts, or select credentials. The connector owns its gateway and relay processes. Native retirement happens before connector exit; the adapter then attempts `session.aclose()` and `client.delete(session)`, including when startup or an earlier cleanup stage fails. Native Session cancellation joins source entry, including a late resource returned by creation. If creation raises before returning a session, cleanup of that unreturned resource remains the provider's responsibility.

### Connect a Docker sandbox

This recipe uses a live model and provider resources, which can incur charges.

The provider example uses Docker's existing Rclone volume strategy. Prepare the host driver and an image that supports the native executor. Install the Docker Python client in the active SDK environment with `python -m pip install docker`. Docker must be running and the configured image must already be available. Supply your trusted gateway connector as an importable async context manager. Run from the repository root, with your connector's package installed or importable on `PYTHONPATH`:

```sh
PYTHONPATH=. python examples/codex/storage_mounts/provider.py \
  --connect your_gateway:connect --image YOUR_PREPARED_IMAGE --bucket YOUR_BUCKET
```

The connector is application code, not a bundled deployment service. It receives the created session, uses that session's provider handles to reach its native executor, and owns gateway cleanup. Configure model credentials through the normal SDK settings. The example reads optional AWS credentials from the application environment and passes them only to the storage declaration.

The model lists `/workspace/data` through the connector's executor. A successful run prints the final answer and then closes the gateway and owned sandbox according to the client's disposal policy. The adapter does not delete the source bucket or its objects; manage their retention separately.

### Choose resource ownership

Pass `session=` to borrow an existing live sandbox. Cleanup closes the connector and leaves the borrowed sandbox to its owner. For a created sandbox or one selected with `authorized_resume_state=`, the client's disposal policy applies. In Python Modal, this can terminate a selected existing sandbox. The application must authorize that disposal.

`snapshot=`, `session=`, and `authorized_resume_state=` are mutually exclusive. Before resuming, authorize the provider identity, snapshot fields, and mount access against current host policy. Deserialized provider state grants no access by itself. Create a fresh source for each native Session, including restore.

<a id="managed-conversation-and-workspace-capture"></a>

### Capture and resume a managed workspace

With `sandbox_environment(..., managed_workspace=True)`, call `await session.suspend_workspace()` to capture an owned sandbox. The returned `ManagedWorkspaceCheckpoint` pairs conversation state with the provider receipt. Capture retires native execution, saves the workspace snapshot, and cleans up the owned resource.

When restoring a decoded or generic pair, supply an independently trusted `authorized_resume_checkpoint=expected_pair` alongside the authorized provider state. An original in-memory capture retains the SDK's private record of the retired pair. Borrowed live sessions cannot use managed retirement.

The adapter accepts `LocalSnapshotSpec`, `RemoteSnapshotSpec`, or their built snapshots as snapshot factories. Without an explicit factory, new managed workspaces use the SDK's local snapshot directory. Directory setup requests POSIX mode `0700`.

Your application supplies provider credentials, an authenticated gateway, current manifest authorization, and safe checkpoint storage. These excerpts extend the client, manifest, options, and connector in the [Docker recipe above](#connect-a-docker-sandbox):

```python
from agents.sandbox import ManagedWorkspaceCheckpoint, sandbox_environment


async def capture_workspace(agent, *, client, manifest, options, connect):
    source = sandbox_environment(
        client,
        manifest=manifest,
        options=options,
        connect=connect,
        managed_workspace=True,
    )
    async with agent.session(environment=source) as session:
        await (await session.submit("Inspect the workspace.")).get_result()
        return await session.suspend_workspace()


async def resume_workspace(
    agent, payload, *, client, manifest, options, connect, authorized_state, trusted_expected_pair
):
    checkpoint = ManagedWorkspaceCheckpoint.from_json(payload)
    source = sandbox_environment(
        client,
        manifest=manifest,
        options=options,
        connect=connect,
        managed_workspace=True,
        authorized_resume_state=authorized_state,
        authorized_resume_checkpoint=trusted_expected_pair,
    )
    async with agent.session(environment=source, state=checkpoint) as session:
        return await (await session.submit("Continue in the saved workspace.")).get_result()
```

`payload` contains serialized `checkpoint.to_json()` data. Load `trusted_expected_pair` from independently authorized application storage or configuration. Parsing the same untrusted payload a second time does not authorize it. `authorized_state` supplies current provider-state authorization matching that pair. Each function creates a fresh source for its native Session.

<a id="existing-supported-matrix"></a>

### Provider and mount support

These are the existing resource implementations available to the adapter, not claims of live cloud qualification. Providers validate strategies and credential boundaries before their creation effects. Their credential requirements and in-container exposure acknowledgements still apply.

| Backend/strategy | Storage entries | Requirements and limits |
| --- | --- | --- |
| Docker `DockerVolumeMountStrategy` | S3, GCS with `rclone` or `mountpoint`; R2, Azure Blob, Box with `rclone` | Matching host volume driver; provider-supported credentials. No S3 Files volume driver. |
| Docker `InContainerMountStrategy` | S3/GCS with Rclone or Mountpoint patterns; R2/Box with Rclone; Azure Blob with Rclone or Fuse; S3 Files with `S3FilesMountPattern` | Compatible image, mount privileges and tools. S3 Files needs an existing filesystem, identity and network reachability. |
| Modal `ModalCloudBucketMountStrategy` | S3, R2, GCS | Provider-supported secrets or compatible credentials. |
| Cloudflare `CloudflareBucketMountStrategy` | S3, R2, GCS | Native bucket API; GCS uses compatible HMAC credentials. |
| E2B `E2BCloudBucketMountStrategy` / Daytona `DaytonaCloudBucketMountStrategy` | S3, GCS, R2, Azure Blob, Box through Rclone | Compatible FUSE/runtime support and credential exposure policy. No S3 Files path established. |
| Blaxel `BlaxelCloudBucketMountStrategy` | S3, R2, GCS | Provider FUSE helpers and supported credentials. |
| Vercel `VercelCloudBucketMountStrategy` | S3 | Provider Mountpoint path; explicit supported credentials. |

Common typed entries, patterns and strategies are re-exported from `agents.sandbox`. Provider-specific strategies remain in `agents.classic.extensions.sandbox.<provider>` modules. The Docker client used above is in `agents.classic.sandbox.sandboxes.docker`. `read_only=True` is forwarded to the existing mount implementation; layout guidance alone does not enforce access. No live cloud services were used to qualify this adapter.

After successful gateway entry, the adapter adds filesystem and mount guidance to the native Session. This describes the effective working directory, executor read-only mode, filesystem layout, storage access, and applicable remote-mount tool policy. The adapter also adds guidance for restored and borrowed resources. Current executor and tool grants enforce access; the guidance explains layout and tool routing.

`render_storage_layout(manifest)` is a separate JSON helper with `workspace` and `storage` entries. Each entry has `path`, `access` (`read_only` or `read_write`), and an optional caller description. The helper excludes provider credentials, settings, environment values, and file contents. Descriptions and manifest entry names are model-visible caller text, so keep secrets out of them.

Render the prepared `session.state.manifest` to use the provider's resolved paths. If you build instructions before creating the sandbox, set the manifest's effective provider root explicitly. For example, Vercel replaces the default `/workspace` root with `/vercel/sandbox`; use `Manifest(root="/vercel/sandbox", entries=...)` with Vercel-compatible entries before rendering its layout. The Docker example above explicitly uses its `/workspace` root.
