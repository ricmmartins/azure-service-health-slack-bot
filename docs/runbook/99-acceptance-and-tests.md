# Acceptance, references, and tests

## End-to-end acceptance record

The following path was exercised from a clean environment on 2026-08-15. It is
a historical deployment record, not validation of every current runbook block:

| Gate | Accepted evidence |
|---|---|
| Workstation | Python 3.12, Azure CLI 2.81.0, Azure CLI-managed Bicep 0.46.1, AZD 1.31.0, Docker client/server 28.3.3, and Git 2.43.0 responded successfully. |
| Repository | The full test suite, Flake8, dependency audit, both Bicep build/lint graphs, Docker build, and AZD packaging passed after the operational fixes documented here. |
| Infrastructure preview | Infrastructure-only preview showed the expected phase-one resources and no Container App, Action Group, or alert before bootstrap. |
| Bootstrap | The hidden `xoxb-` transfer completed, Key Vault held an enabled latest version, local plaintext was absent, migration marker was set, and temporary firewall/RBAC/lock/journal state was restored. |
| Workload | `azd deploy` published the application image and Container Apps single revision mode converged to exactly one healthy active revision. |
| Security boundary | `/healthz` and `/readyz` returned `200`; an anonymous webhook POST returned `401`. |
| Signed delivery | Action Group test notification returned Secure Webhook `Succeeded`; the app logged an authenticated HTTP `200` and `Service Health incident processed`; the formatted message appeared in the configured Slack channel. |
| Multi-scope safety | Subscription what-if produced an expiring artifact-bound fingerprint. Tenant Root Group what-if was correctly rejected when it overlapped the immutable central baseline. |

Do not copy environment-specific IDs from another deployment. Reproduce every
gate with the target tenant, subscription, region, Slack workspace, channel, and
approved Management Group hierarchy.
This historical record does not establish that the central baseline was
enabled. New acceptance also requires the explicit Stage 9 cutover and
`baseline-coverage-enabled`, with the actual enabled state in the change record.
The offline Bash regression suite validates ordering, state, and failure
handling with command doubles; it does not replace live signed-delivery,
private-network, permission-propagation, or human receipt evidence.

## Official Microsoft references

The repository's fail-closed checks add application-specific safety around the
documented Azure behavior; they do not replace Azure documentation. The
following Microsoft Learn pages are the authoritative platform references used
by this runbook:

| Operational claim | Microsoft documentation |
|---|---|
| AZD stores environment values under `.azure/<environment>/.env`; secrets must not be committed or printed. | [Manage Azure Developer CLI environment variables](https://learn.microsoft.com/azure/developer/azure-developer-cli/manage-environment-variables) |
| Container Apps built-in authentication validates Microsoft Entra tokens and can reject unauthenticated requests with HTTP `401`. | [Authentication and authorization in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/authentication) |
| Action Groups support Microsoft Entra-authenticated Secure Webhooks and Common Alert Schema. | [Create and manage Azure Monitor action groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups) |
| Blob leases provide acquire, renew, release, and break operations used by the distributed operation lock. | [Create and manage a blob lease with Python](https://learn.microsoft.com/azure/storage/blobs/storage-blob-lease-python) |
| The Python Key Vault client requires `azure-keyvault-secrets` and Azure Identity authentication. | [Quickstart: Azure Key Vault secret client library for Python](https://learn.microsoft.com/azure/key-vault/secrets/quick-create-python) |
| The Tenant Root Group contains all Management Groups and subscriptions in the directory. | [What are Azure Management Groups?](https://learn.microsoft.com/azure/governance/management-groups/overview) |

See [Microsoft platform evidence](../microsoft-platform-evidence.md) for the
larger source-to-control mapping, including permissions, API behavior, limits,
and the repository control that depends on each fact.

## Tests

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -r requirements-ops.txt
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m flake8 .
python -m pip_audit -r requirements.txt -r requirements-ops.txt
az bicep build --file infra/main.bicep --stdout
az bicep lint --file infra/main.bicep
az bicep build --file infra/day2/service-health-alert-scope.bicep --stdout
az bicep lint --file infra/day2/service-health-alert-scope.bicep
python scripts/manage_alert_scopes.py --help >/dev/null
python scripts/manage_slack_token.py --help >/dev/null
python scripts/configure_secure_webhook.py --help >/dev/null
}
```

The test suite covers payload parsing, routing, authorization, Table Storage
coordination, Slack rendering and error classification, Flask routes, runtime
configuration, Secure Webhook setup, token lifecycle secrecy/failure states,
distributed operation locks, infrastructure contracts, and day-2 scope
management.

## Community and support

Read [CONTRIBUTING.md](../../CONTRIBUTING.md) before proposing a change. Use
[SUPPORT.md](../../SUPPORT.md) for support boundaries. Report suspected
vulnerabilities privately as described in [SECURITY.md](../../SECURITY.md).

## License

This project is licensed under the [MIT License](../../LICENSE).
