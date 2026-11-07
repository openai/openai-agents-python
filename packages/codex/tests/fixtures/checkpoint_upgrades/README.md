# Checkpoint upgrade captures

Each producer directory contains original `SessionState.to_json()` output from the public API. The manifest records each file's hash and SDK envelope version, the exact producer source snapshot, and its matching native wheel identity. These captures use synthetic data. They are source snapshots, not stable v1 releases. Do not edit or format their JSON bytes.

The initial producer is `e283d9a5d6d73c5f8097423c18a42625b85bacdf`, with native artifact `13581901969` and its verified pristine wheel receipt. `native_wheel_sha256` hashes the named wheel inside that artifact. It is distinct from the downloaded GitHub artifact archive hash. `native_receipt_sha256` hashes the wheel's pristine receipt. SDK formats 4, 5, and 6 lower to native formats 2, 3, and 4 respectively. Native manual restoration validates format 3 before lowering its idle portion to format 2.

`test_checkpoint_upgrades.py` verifies capture hashes and continues idle, settled manual, and CodeMode state with the current native binding. It checks accepted tool results, fresh one-use manual authority, output identity, future-format rejection and foreign-native-revision rejection. The normal suite discovers this file. The installed-wheel qualification workflow also runs it against the candidate wheel outside the source checkout. Retain these tests and captures for each later candidate; no data rewrite is required when the existing reader preserves the old contract.

## Reproduce a capture

Use an immutable producer checkout with its frozen dependencies and matching native artifact verified through the maintained build tooling. Set `PRODUCER` to that checkout, `FIXTURES` to this directory, and `ARTIFACTS` to the extracted artifact directory. Use absolute paths. Run the producer's interpreter so the SDK wrapper source and native extension belong to the same snapshot.

```sh
env -u OPENAI_API_KEY OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1 \
  "$PRODUCER/.venv/bin/python" "$FIXTURES/capture.py" \
  --source "$PRODUCER" --output "$FIXTURES/e283d9a5" \
  --receipt "$ARTIFACTS/openai_agents-0.24.0a6-cp310-abi3-manylinux_2_39_x86_64.whl.pristine.json" \
  --artifact "$ARTIFACTS/openai_agents-0.24.0a6-cp310-abi3-manylinux_2_39_x86_64.whl" \
  --artifact-id 13581901969
```

The generator imports the producer's public and private SDK modules and its existing synthetic Responses peer. It prints their import paths for local verification, then uses a controlled `/tmp/agents-checkpoint-capture` working directory. No private checkout path belongs in a committed manifest or capture. The producer rejects the stale manual reply after sealing. Credentials and tool results are synthetic.

UUIDs and timestamps vary on each run. Recapture deliberately and review the bytes and manifest together. Run Ruff before capture so `capture_script_sha256` identifies the exact generator used. Keep the producer source and matching native wheel immutable.

These scenarios do not represent every historical preview producer. Existing schema regression tests continue to cover the accepted envelope versions. These snapshots establish no cross-language restore, downgrade, native-pin migration, or stable-release upgrade.
