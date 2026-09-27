# Contributing to the OpenAI Agents Python SDK

## Contribution policy

We welcome bug reports, feature requests, minimal reproductions, and root-cause analysis through [GitHub issues](https://github.com/openai/openai-agents-python/issues).

**Pull requests are limited to repository collaborators. We do not accept pull requests from non-collaborators**, including documentation or example changes. If you are not a collaborator, please open an issue instead of preparing a pull request. Include the affected version, expected and actual behavior, and a small, sanitized reproduction when applicable.

Report suspected security vulnerabilities privately as described in [SECURITY.md](SECURITY.md), rather than in issues or pull requests.

The development and pull request instructions below are for maintainers and repository collaborators.

For suspected vulnerabilities, follow [SECURITY.md](SECURITY.md). Keep undisclosed security reports and fixes out of public issues, discussions, and pull requests until disclosure is coordinated.

## Development workflow

Read [AGENTS.md](AGENTS.md) for the repository's scope, compatibility, review, and verification requirements. Use Python 3.10 or newer, `uv`, and `make`. Install the development dependencies with `make sync`, and run Python commands through `uv run`.

Keep changes focused on the agreed outcome. Add regression coverage for changed behavior and follow [tests/README.md](tests/README.md) for test execution. Run focused checks while developing, then the applicable final checks described in [AGENTS.md](AGENTS.md#testing--automated-checks). Use the [pull request template](.github/PULL_REQUEST_TEMPLATE/pull_request_template.md) to explain the problem, change, and validation. Documentation changes follow the repository's verification tiers and release-timing rules.

## Tracing integration listings

The external tracing processors list helps Agents SDK users discover useful, maintained integrations. Listing space is limited by relevance and evidence of user value. Publishing a package or implementing the tracing interface does not, by itself, qualify an integration for inclusion. Integrations remain supported by their maintainers; inclusion is not an OpenAI endorsement, security certification, or support commitment.

### Criteria for new listings

New listings must meet all of the following criteria:

1. **Released tracing support.** The integration is available in a published, installable release and uses the Agents SDK tracing interface. Identify the package version and implementation. A planned integration, generic OpenTelemetry support, or a hooks-only or guardrails-only integration is insufficient.
2. **Useful setup documentation.** Link directly to a maintained Agents SDK integration guide. The guide must cover installation, supported SDK versions, a minimal example, export destination, configuration, and flush or shutdown behavior. Explain which data is captured, the capture defaults and controls, and whether registration preserves or replaces the default OpenAI exporter. Keep marketing claims out of the listing itself.
3. **Compatibility evidence.** Provide reproducible checks against the real Agents SDK and identify the integration and SDK versions tested. Cover trace/span relationships and the advertised event types, plus applicable export failure, flush/shutdown, and sensitive-data controls. Mock-only interface tests and an unrelated green CI badge are insufficient; a sanitized test report or an automated integration test can supply this evidence. Maintainers do not need production credentials or customer payloads to assess a listing.
4. **Independent use.** Provide at least one independently verifiable example of continued use of this integration with the Agents SDK by a user or project outside the integration maintainer's organization. Evidence can be a maintained public application, an independent technical write-up, or a concrete usage report from an unaffiliated user describing the workflow and experience over time. Vendor demos, launch announcements, customer logos, and adoption of a different framework integration do not satisfy this criterion. Small projects can qualify; production scale is not required.
5. **Maintenance ownership.** Identify a responsible maintainer and a working support or issue channel. Show how compatibility problems are tracked and corrected, through release history, issue responses, or a stated maintenance process. Disclose the submitter's affiliation and any commercial relationship with the users offered as evidence.

Stars, download counts, funding, paid promotion, and inclusion in other directories do not substitute for these criteria. Meeting the criteria makes a request eligible for review, not guaranteed acceptance; maintainers should explain any remaining relevance or evidence concern.

### Requesting a listing

Open an issue before preparing a listing change. Include the integration name, your affiliation, the published package and version, the direct setup guide, compatibility evidence, independent-use evidence, and the maintainer/support link. Share only evidence you have permission to disclose. Do not post customer identities without consent, private usage data, credentials, prompts, responses, or recordings. If independent-use evidence cannot be shared safely, explain the limitation; it remains unverified until maintainers can assess suitable evidence.

Keep an accepted listing to a neutral product name and a direct integration-guide link. Do not add promotional copy, tracking or referral links, SDK dependencies, or integration implementation code as part of a listing request.

### Maintainer review and existing entries

Review the submitted evidence against each criterion and record the concrete basis for acceptance or the missing evidence in the issue. Check that the guide and published release describe the same integration. Distinguish a vendor's claim, inspected tests, and independently observed results. Do not run downloaded integration code with credentials or sensitive data merely to assess a listing.

These criteria apply to listing requests opened after this policy is merged. Assess already-open requests under the previous expectations of released tracing support and usable documentation. Existing entries and requests accepted under that process have not been retrospectively verified against the new criteria, and their inclusion does not establish eligibility for future additions.

Existing entries may remain while maintainers review them as concerns arise; this policy does not require a bulk removal or certify the current list. A broken setup guide, unavailable release, discontinued tracing support, or misleading claim warrants review. Give the integration maintainer an opportunity to correct ordinary documentation or maintenance problems. Remove entries when those problems remain unresolved, and remove unsafe or deceptive links promptly. Follow the private reporting process in [SECURITY.md](SECURITY.md) for undisclosed vulnerabilities. Routine link repairs do not require a new adoption review; a replacement product or materially different integration must meet the new criteria.

When declining a request, name the unmet criterion and the evidence that would support reconsideration. Do not speculate about the contributor's motives or equate a small project with poor quality. For example:

> Thanks for building this integration. We are keeping this list focused on maintained integrations with demonstrated value to Agents SDK users. The request does not yet establish independent use of the Agents SDK integration. A maintained public application or a concrete report from an unaffiliated user describing continued use would support reconsideration. We are closing this listing request for now; the integration can continue to use the SDK's public tracing interface.

## Security checklist

### Credentials and sensitive data

- Use synthetic fixtures and obvious placeholder credentials in tests, examples, snapshots, and documentation. Never commit real API keys, tokens, cookies, signing keys, customer data, private prompts or responses, tool payloads, or recordings.
- Provide credentials for explicitly authorized live tests through the approved environment or secret store. Use the minimum necessary access and keep live credentials out of untrusted contributor runs. Do not embed credentials in browser code, commands, URLs, or generated artifacts.
- Inspect diffs and attachments for sensitive data before sharing them. Include logs, exceptions and their chained context, tracebacks, telemetry, session exports, files, and audio in that check. Do not assume a tracing redaction setting sanitizes every channel.
- If a credential is exposed, stop sharing it, report it privately through [SECURITY.md](SECURITY.md), and have its owner revoke or rotate it. Deleting the visible value alone does not invalidate the credential.

### Dependencies and downloaded tools

- Justify new dependencies and review the package source, maintenance history, install or build hooks, transitive dependencies, and lockfile changes. Keep dependency changes scoped and reproducible.
- Assess dependency alerts for the affected runtime, optional integration, development, example, CI, or publishing path. Record reachability and impact instead of dismissing an alert solely because the dependency is not shipped to users.
- For dependency-update automation, apply a documented release-age cooldown to ordinary version updates while allowing security updates immediately. Review security updates promptly; they still require appropriate review and checks. Do not claim a cooldown or update ecosystem is configured without checking the actual configuration.
- Escalate critical or actively exploited findings immediately through the private security process. Record any proposed exception with an owner, mitigation, approving authority, and expiry; an unapproved exception is not an accepted risk.

### CI and publishing

- Treat pull request content, branch names, artifacts, and external downloads as untrusted input. Do not execute contributor-controlled code in a privileged workflow or expose secrets to it, including through `pull_request_target` or a later workflow that consumes contributor artifacts.
- Use explicit, least-privilege workflow and job permissions, review third-party actions, and pin actions to full commit SHAs. Grant write or `id-token` permissions only to jobs that require them. Do not bypass required reviews, secret protections, or security checks to make CI pass.
- Changes to credentials, redaction, requests and redirects, parsing, uploads, tool approvals, MCP, persisted state, sandbox access, dependencies, CI, or releases need focused security review and regression coverage appropriate to the affected boundary.
- Release approval under the shared SDK policy requires CODEOWNERS coverage of release workflows and publishing configuration, required code-owner review of release pull requests, and passing required checks. A separate environment reviewer gate is not required by that policy. Follow any protections currently configured for this repository; this guidance does not authorize removing or bypassing them.
- Preserve the existing PyPI OIDC publishing flow, release-source validation, and artifact handoff in [the publishing workflow](.github/workflows/publish.yml). Do not replace short-lived trusted publishing with long-lived registry tokens or weaken provenance checks for convenience. Follow the [maintainer release procedure](.github/RELEASING.md).
- When assessing publishing readiness, verify the repository-specific registry binding, artifact provenance, publisher access, and recovery arrangements. Workflow configuration alone does not prove those controls are in place.

These requirements describe how to contribute safely. Their presence does not certify repository settings, establish a scan baseline, or close existing security findings. Maintainers must track verified gaps and approved exceptions separately from proposed work.
