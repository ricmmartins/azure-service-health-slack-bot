# Support

This repository is a community reference implementation, not an official
Microsoft or Slack support channel.

## GitHub issues

Use [GitHub issues](https://github.com/ricmmartins/azure-service-health-slack-bot/issues)
for reproducible bugs in this repository, focused feature requests,
documentation corrections, and questions about the implementation. Search
existing issues first and identify the version or commit you tested.

Safe diagnostic details include:

- Reproduction steps using synthetic values
- The application version or commit SHA
- Sanitized configuration structure and non-sensitive feature flags
- Secret lifecycle state names and Key Vault secret version identifiers, without
  secret values
- Redacted error types, HTTP status codes, timestamps with time zone, and
  Azure region
- Expected and actual behavior

Do not post tokens, secrets, credentials, customer data, webhook payloads,
customer incident communications, private logs, or tenant IDs when those IDs
are sensitive. Redact subscription IDs, resource IDs, Slack workspace and
channel IDs, hostnames, and other identifying values unless they are both
necessary and safe to disclose.

Before attaching operational output, inspect it for authorization headers,
Slack token prefixes, AZD environment values, temporary Key Vault firewall
rules, caller identifiers, and operation-journal contents. The supported
secret-lifecycle commands redact tokens, but support bundles still require a
human review.

Create bundles from a small explicit allowlist of sanitized diagnostics. Never
archive the repository or working directory wholesale. Exclude `.azure/`,
`.env*`, operation journals, management-lock metadata, temporary firewall
snapshots, CLI transcripts, shell history, crash/core dumps, container layers,
webhook payloads, and local password-manager exports. Scan the final archive for
Slack token prefixes, bearer/authorization headers, tenant and subscription
identifiers, channel IDs, and customer incident text before it leaves the
approved support boundary.

Report suspected vulnerabilities privately as described in
[SECURITY.md](SECURITY.md), not in an issue.

## Product support

Use [Azure Support](https://azure.microsoft.com/support/options/) for Azure
platform incidents, Azure Service Health or Azure Monitor delivery behavior,
subscription access, quota, billing, and production resource failures.

Use [Slack Support](https://slack.com/help/requests/new) for Slack outages,
workspace administration, app approval, token lifecycle, API availability,
rate limits, and other Slack platform or account issues.

When engaging either provider, follow that provider's approved secure support
process rather than copying private case material into this repository.
