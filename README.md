# Azure Service Health Slack Bot

A standalone, production-oriented Flask service that receives Azure Service
Health alerts through Azure Monitor's Common Alert Schema and posts them to
Slack. Service Health alerts create one root message per subscription and
tracking ID, then update that same message through Active, Updated, and
Resolved states so human replies remain in its thread.

This repository intentionally has **no** Slack Bolt app, no inbound Slack
events, and no Azure support-ticket workflow. It only initializes a Slack
`WebClient` for outbound messages.

## Architecture

The production deployment uses Azure Container Apps, Azure Table Storage,
Key Vault, a user-assigned managed identity, Azure Container Registry, and
workspace-based Application Insights. Azure Monitor sends Activity Log Alerts
with Common Alert Schema to `POST /api/service-health` through a Secure
Webhook Action Group. Container Apps Easy Auth validates the Entra token and
the application also requires the official AzNS caller application and the
`ActionGroupsSecureWebhook` app role.

```
Azure Monitor (Service Health) --> Activity Log Alert --> Secure Action Group
    --> POST /api/service-health (Easy Auth + app role check)
    --> parse Common Alert Schema --> route by subscription/service/region
    --> Azure Table Storage (ETag/lease idempotency) --> Slack chat.postMessage/chat.update
```

## Routes

| Route | Purpose |
|---|---|
| `POST /api/service-health` | Authenticated Common Alert Schema webhook |
| `GET /healthz` | Process liveness (public) |
| `GET /readyz` | Service Health configuration readiness (public) |

## Prerequisites

- Python 3.13 or Docker
- A Slack app with a bot token (`chat:write` scope) invited to every
  configured destination channel
- Azure CLI and Azure Developer CLI (`azd`)
- An Azure subscription where you can create the resources in `infra/`
- `Application Administrator` (or equivalent Graph permission) while running
  the Secure Webhook setup script

## Local development

1. Copy `.env-example` to `.env`.
2. Set `SLACK_BOT_TOKEN`, a Table endpoint accessible through
   `DefaultAzureCredential`, and a routing file or inline routing JSON.
3. Install and run:

```sh
pip install -r requirements.txt
python app.py
```

Build the production image with `docker build -t azure-service-health-slack-bot .`.
The builder falls back to Microsoft's Python package proxy when direct PyPI
downloads are blocked by a corporate network. Override `PIP_INDEX_URL` or
`PIP_FALLBACK_INDEX_URL` with Docker build arguments when required.

## Service Health routing

Set either `SERVICE_HEALTH_ROUTES_JSON` or `SERVICE_HEALTH_ROUTES_FILE`. See
`config/service_health_routes.example.json`. `default_channel_id` is required
as a fallback. Rules may filter by `subscription_ids`, `services`, and
`regions`. All supplied filters on a rule must match; highest priority wins,
then greatest specificity, then file order. The first selected channel is
stored with the incident and remains fixed for that incident's lifecycle.

## Idempotency and lifecycle

The incident key is the normalized subscription ID (`PartitionKey`) plus a
SHA-256 hash of the tracking ID (`RowKey`). Azure Table ETags and a short
processing lease coordinate concurrent replicas. Identical retries and stale
updates (an older `submissionTimestamp` than what's stored) return `200`
without calling Slack. Transient Slack or Storage failures return `503` so
Azure Monitor can retry; invalid payloads and permanent Slack errors (for
example an unknown channel) return `4xx` and are not retried.

## Security

- **Easy Auth (Entra ID)**: validates the caller's Microsoft Entra token.
- **App-level check**: the app additionally requires the official AzNS AAD
  Webhook application ID (`461e8683-5575-4561-ac7f-899cc907d62a`), the
  `ActionGroupsSecureWebhook` app role, and the configured audience —
  see `service_health/auth.py`.
- **Managed Identity**: the Container App uses a user-assigned managed
  identity with exactly three roles: **AcrPull** (registry), **Key Vault
  Secrets User** (Slack bot token), and **Storage Table Data Contributor**
  (incident state). No Reader or Support Request Contributor roles are
  granted.
- **Public probes**: `/healthz` and `/readyz` remain publicly reachable
  (Easy Auth `unauthenticatedClientAction: AllowAnonymous`); only
  `/api/service-health` enforces the app-role check.
- **No secret logging**: request/response bodies and Slack/Azure credentials
  are never logged.

## Deploy with AZD

No deployment is performed automatically by this repository. Configure an AZD
environment before provisioning:

```sh
az login
azd auth login
azd env new
azd env set SLACK_BOT_TOKEN "<xoxb-token>"
azd env set SERVICE_HEALTH_ROUTES_JSON '{"default_channel_id":"C0123456789","rules":[]}'
azd provision
azd deploy
```

The pre-provision hook runs `scripts/configure-secure-webhook.ps1`. It creates
or reuses the protected API app registration, app role, API service
principal, and AzNS app-role assignment, then writes the resulting IDs to the
AZD environment. The script is idempotent (safe to re-run) and requires
Microsoft Graph application administration permission. Azure CLI and AZD
maintain separate authentication sessions, so both `az login` and
`azd auth login` are required on a clean workstation.

`infra/modules/service-health-alert.bicep` is deliberately isolated so the
Activity Log Alert can be repeated for additional subscriptions while the
Container App remains central. Deploy that module at the target subscription
with the central webhook and Secure Webhook app values.

Secrets are copied from AZD environment parameters into Key Vault during
provisioning and exposed to the Container App only as Key Vault secret
references. Runtime access to Key Vault and Table Storage uses the
user-assigned managed identity and RBAC — never shared keys.

## Operations

Application Insights receives requests, dependencies, exceptions, logs, and
custom counters (`service_health.requests`, `service_health.lifecycle`).
Useful starting queries:

```kusto
AppRequests
| where Name has "/api/service-health"
| summarize count(), percentile(DurationMs, 95) by ResultCode

AppTraces
| where Message has "Service Health"
| project TimeGenerated, SeverityLevel, Message, Properties

AppDependencies
| where Target has_any ("slack.com", "table.core.windows.net")
| summarize count(), failures=countif(Success == false) by Target, ResultCode
```

Alert on sustained webhook `503`s, Slack or Table dependency failures, and no
successful webhook requests when incidents are expected. For a permanent
Slack error, verify the configured channel IDs and bot channel membership.

There is an unavoidable MVP crash window after a successful first
`chat.postMessage` and before `messageTs` is persisted. Exactly-once creation
would require a transactional queue, which is intentionally out of scope.
Reconcile by finding the message using its tracking ID, updating the Table
entity, and then replaying the alert.

## Tests

```sh
pip install -r requirements-test.txt
pytest
flake8 .
```

Tests cover the Common Alert Schema parser, routing rules, Easy Auth/app-role
authorization, Table Storage idempotency, the processing state machine, Slack
message rendering and error classification, the Flask endpoints, and runtime
bootstrap/credential selection.

## License

This project is licensed under the [MIT License](LICENSE).
