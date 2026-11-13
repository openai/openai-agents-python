# Push a reviewed Git commit from macOS

The [host Git example](host_git.py) uses a Python function tool for an authenticated push. The function runs in the application process and uses its Git credential helper. The example uses scripted model responses and requires only Git credentials. The application's host callback applies its own destination and operation policy. Native `NetworkPolicy` governs executor commands.

Use a trusted checkout whose `origin` has one credential-free HTTPS GitHub URL for fetch and push. Review the commit, Git configuration, hooks, and credential helper before authorizing the push. Keep the checkout's Git configuration and hooks under application control throughout the operation. This development-only example is absent from the alpha.7 archive. Follow the [matching source-build setup](../../CONTRIBUTING.md#build-the-sdk-from-source), activate its Python environment, and run from the repository root.

```sh
PYTHONPATH=. python -m examples.codex.host_git \
  --checkout /absolute/path/to/trusted-checkout \
  --remote-url https://github.com/OWNER/SCRATCH-REPOSITORY.git \
  --commit FULL_REVIEWED_COMMIT_SHA \
  --branch sdk-host-git-UNIQUE_TEST_ID
```

The application selects the checkout, repository, commit, and new branch before opening the Session. The tool accepts no model arguments. It verifies the exact remote branch SHA and prints a `verified` result. A repeat with the same commit verifies the existing branch. A branch pointing to another commit causes the operation to fail. After a timeout or cancellation, check the remote branch before retrying because the push can finish before the application receives the result.

[Choose another SDK example](README.md).
