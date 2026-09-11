# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through the channels in OpenAI's [Coordinated Vulnerability Disclosure Policy](https://openai.com/policies/coordinated-vulnerability-disclosure-policy/). Follow that policy for disclosure coordination and bug bounty eligibility. Our [security contact and PGP key information](https://cdn.openai.com/security.txt) are available for encrypted reports.

Do not open a public GitHub issue, discussion, or pull request containing an undisclosed vulnerability or sensitive reproduction details. Use the disclosure policy's reporting channels even if GitHub's private vulnerability reporting feature is unavailable for this repository.

Include the following information in a private report:

- The affected `openai-agents` version or commit, Python version, operating system, and relevant optional dependencies or provider configuration.
- The affected component, expected and observed behavior, and the security impact, including who can control the input and which trust boundary is crossed.
- A minimal reproduction using synthetic data and placeholder credentials, plus sanitized logs or tracebacks when useful.

Remove API keys, authorization headers, cookies, tokens, signing material, customer data, personal information, and private prompts, responses, tool arguments, files, or audio from reports and attachments. Check URLs, logs, tracebacks, trace exports, session records, and screenshots for sensitive values. If an exposed credential is involved, report the exposure privately and have its owner revoke or rotate it; do not include the credential itself or test it against a live service.

## Scope

This policy covers the OpenAI Agents Python SDK in this repository and the `openai-agents` package, including agent execution, tools and MCP integration, sessions and resumed state, tracing, Realtime and Voice integrations, sandbox adapters, examples, dependencies, and the build, CI, and publishing path that produces distributed artifacts.

Security issues in dependencies or external providers can affect this SDK. Explain the affected SDK path and environment so maintainers can coordinate with the responsible project. A dependency used only for development, examples, or releases still needs impact assessment; it is not automatically harmless or a production vulnerability.

## Trust boundaries and security review

Applications choose credentials, providers, tools, storage, and execution permissions. Tool implementations and local subprocesses can exercise the host application's privileges. Applications must authorize those capabilities and isolate untrusted execution using appropriate controls. Model output, tool and MCP responses, remote content, and serialized state do not by themselves authorize access to host resources or credentials.

Security review should verify that changes preserve these properties:

- Credentials and sensitive payloads stay within their intended request, storage, and telemetry destinations. Redaction controls must be checked at the actual logging, exception, tracing, and serialization paths; a tracing option does not sanitize every output channel.
- SDK-managed tool approvals, sandbox host-path boundaries, and application-controlled grants cannot be bypassed by untrusted input or resumed state.
- Requests, parsing, file materialization, and cleanup preserve the applicable authorization, resource-ownership, and filesystem boundaries.
- Untrusted contributions cannot obtain CI secrets or publishing authority. Published artifacts must come from the reviewed release source through the authorized publishing workflow.

Reports should identify a reachable security consequence on an affected SDK, development, or release path. A prompt injection or unsafe application-defined tool does not by itself establish an SDK defect; a bypass of an SDK-enforced boundary remains reportable. Maintainers assess severity using impact, reachability, required privileges, and exposure rather than scanner severity alone. A completed scan does not establish that all controls are healthy or that its findings are resolved.

## Contributing security fixes

Coordinate undisclosed fixes through the private reporting process before publishing a patch or regression test. See [CONTRIBUTING.md](CONTRIBUTING.md#security-checklist) for safe development practices and [AGENTS.md](AGENTS.md#security-checklist) for agent review requirements.
