# Azure Service Health Slack Bot

A standalone, production-oriented Flask service that receives Azure Service
Health alerts through Azure Monitor's Common Alert Schema and posts them to
Slack. Service Health alerts create one root message per subscription and
tracking ID, then update that same message through Active, Updated, and
Resolved states so human replies remain in its thread.

This repository intentionally has **no** Slack Bolt app, no inbound Slack
events, and no Azure support-ticket workflow. It only initializes a Slack
`WebClient` for outbound messages.

## Contents

- [Architecture](#architecture)
- [Routes](#routes)
- [Prerequisites](#prerequisites)
- [Local development](#local-development)
- [Service Health routing](#service-health-routing)
- [Idempotency and lifecycle](#idempotency-and-lifecycle)
- [Security](#security)
- [Deploy with AZD](#deploy-with-azd)
- [Step-by-step deployment guide](#step-by-step-deployment-guide)
- [Operations](#operations)
- [Tests](#tests)

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

## Step-by-step deployment guide

This is the complete, from-zero walkthrough: create the Slack app, provision
Azure, wire up the Secure Webhook, and verify an end-to-end alert. Every
command is safe to re-run.

### 0. Prerequisites

- Azure subscription with permission to create resource groups and role
  assignments (`Owner` or `Contributor` + `User Access Administrator`) at the
  target subscription.
- A Microsoft Entra role that can create app registrations and app roles for
  the Secure Webhook app (`Application Administrator` or `Cloud Application
  Administrator`, or an admin who can grant consent once).
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
  [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
  and PowerShell 7+ (`pwsh`) installed locally.
- A Slack workspace where you can create/install apps.

### 1. Create the Slack app and bot token

1. Go to <https://api.slack.com/apps> and click **Create New App** → **From
   scratch**. Give it a name (e.g. `Azure Service Health`) and pick your
   workspace.
2. Open **OAuth & Permissions** in the left sidebar. Under **Scopes → Bot
   Token Scopes**, add `chat:write` (and `chat:write.public` if you want the
   bot to post to public channels without being explicitly invited).
3. Click **Install to Workspace** at the top of the same page, review the
   permissions, and approve.
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`) from **OAuth &
   Permissions** — you'll use this as `SLACK_BOT_TOKEN`.
5. Invite the bot to every channel it needs to post in, e.g.
   `/invite @Azure Service Health` in each destination channel — the bot
   token alone does not grant channel membership.

This app only ever calls `chat.postMessage`/`chat.update`; it never receives
events, so **no Signing Secret, Event Subscriptions, slash commands, or app
manifest are required.**

### 2. Clone the repo and sign in

```sh
git clone https://github.com/ricmmartins/azure-service-health-slack-bot.git
cd azure-service-health-slack-bot
az login
azd auth login
```

`az` and `azd` keep separate credential caches, so both logins are required
even if you're already signed in to one.

### 3. Create the routing configuration

Copy the example and edit it for your subscriptions/services/regions/channels
(Slack channel IDs, not names — right-click a channel → **View channel
details** → copy the ID at the bottom):

```sh
cp config/service_health_routes.example.json config/service_health_routes.json
```

`default_channel_id` is mandatory; it's where anything that doesn't match a
rule is posted. See [Service Health routing](#service-health-routing) above
for the matching rules.

### 4. Initialize the AZD environment

```sh
azd env new                      # prompts for an environment name, subscription, and Azure region
azd env set SLACK_BOT_TOKEN "xoxb-..."
azd env set SERVICE_HEALTH_ROUTES_JSON "$(Get-Content config/service_health_routes.json -Raw)"   # PowerShell
# bash/zsh equivalent:
# azd env set SERVICE_HEALTH_ROUTES_JSON "$(cat config/service_health_routes.json)"
```

`AZURE_ENV_NAME`, `AZURE_LOCATION`, and `AZURE_SUBSCRIPTION_ID` are captured
by `azd env new`; every other required Bicep parameter is filled in
automatically by the provisioning steps below.

### 5. Provision Azure infrastructure

```sh
azd provision
```

This runs `scripts/configure-secure-webhook.ps1` as a `preprovision` hook
before touching any Azure resource. The script:

1. Creates (or reuses) an Entra app registration that represents the
   protected webhook API and sets its `identifierUri` to `api://<app-id>`.
2. Adds an app role named `ActionGroupsSecureWebhook` (application-only) if
   it doesn't already exist.
3. Ensures a service principal exists for that app, and for Microsoft's
   official **AzNS AAD Webhook** application
   (`461e8683-5575-4561-ac7f-899cc907d62a`).
4. Grants the AzNS service principal the `ActionGroupsSecureWebhook` app
   role on your API app — this is what lets Azure Monitor's Secure Webhook
   Action Group call your endpoint with a verifiable Entra token.
5. Writes `AZURE_TENANT_ID`, `SERVICE_HEALTH_API_CLIENT_ID`,
   `SERVICE_HEALTH_API_OBJECT_ID`, and `SERVICE_HEALTH_API_IDENTIFIER_URI`
   into the AZD environment for the Bicep deployment to consume.

`azd provision` then deploys `infra/main.bicep`, which creates the resource
group, Container Apps environment, Container Registry, Key Vault (with the
Slack bot token as a secret), Storage Account/Table, Application Insights,
the Container App itself (Easy Auth wired to the app registration from the
script), and the Activity Log Alert + Secure Action Group targeting the
current subscription.

### 6. Deploy the application image

```sh
azd deploy
```

Builds the Docker image from this repo, pushes it to the new Container
Registry, and updates the Container App revision. `azd up` combines steps 5
and 6 (`azd provision && azd deploy`) if you prefer a single command.

### 7. Verify the deployment

```sh
curl https://<SERVICE_APP_URI>/healthz   # -> {"status":"healthy"}
curl https://<SERVICE_APP_URI>/readyz    # -> {"status":"ready"}
```

`SERVICE_APP_URI` is printed as an `azd provision`/`azd deploy` output (also
retrievable with `azd env get-values`). Both probes are public; no token is
needed.

To verify the full alert path without waiting for a real Azure incident,
trigger the Activity Log Alert's action group manually from the Azure
Portal (**Monitor → Alerts → Action groups → `ag-<env>-service-health` →
Test action group**, category **Service Health**), or send Azure Monitor's
[Common Alert Schema sample payload](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema)
through the portal's "Test action group" webhook tester, which signs the
request the same way the real Action Group does. A directly unauthenticated
`curl POST` to `/api/service-health` will correctly get rejected by Easy
Auth — that's expected and confirms the endpoint is protected.

### 8. Add more subscriptions (optional)

The Container App and Secure Webhook app registration are central; only the
Activity Log Alert + Action Group need to exist per subscription. Deploy
`infra/modules/service-health-alert.bicep` directly at any additional
subscription, reusing the same webhook URI and Secure Webhook identity:

```sh
az account set --subscription "<other-subscription-id>"
az deployment sub create \
  --location "<azure-region>" \
  --template-file infra/modules/service-health-alert.bicep \
  --parameters environmentName="<azd-env-name>" \
               webhookUri="https://<SERVICE_APP_URI>/api/service-health" \
               secureWebhookObjectId="<SERVICE_HEALTH_API_OBJECT_ID>" \
               secureWebhookIdentifierUri="<SERVICE_HEALTH_API_IDENTIFIER_URI>" \
               tenantId="<AZURE_TENANT_ID>" \
               targetSubscriptionId="<other-subscription-id>" \
               tags='{"azd-env-name":"<azd-env-name>"}'
```

Pull the `SERVICE_HEALTH_API_*` and `AZURE_TENANT_ID` values from
`azd env get-values` in the environment used for the original deployment.

### 9. Update routing or redeploy later

Editing `config/service_health_routes.json` requires re-running steps 4 and
6 (`azd env set SERVICE_HEALTH_ROUTES_JSON ...` then `azd deploy`) so the new
JSON reaches the Container App's environment variable — no image rebuild is
required, but restarting the revision picks up the new routing.

### 10. Tear down

```sh
azd down --purge
```

`--purge` also removes the soft-deleted Key Vault so the name can be reused.
This does not remove the Entra app registration created by
`configure-secure-webhook.ps1` or the app role assignment on the AzNS
service principal — remove those manually from **Entra ID → App
registrations** if you're fully decommissioning the integration.

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
