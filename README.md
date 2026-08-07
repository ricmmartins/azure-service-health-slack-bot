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

Key Vault and the Storage account are provisioned with public network access
disabled and are reachable only through Private Endpoints on a dedicated
VNet (also used for the Container Apps environment's VNet integration); this
keeps the deployment compliant with tenants/policies that require
`publicNetworkAccess: Disabled` on these resource types. The Container App's
public ingress (health probes and the secure webhook) is unaffected.

![Operator architecture map showing the numbered Azure Service Health event path through Global Activity Log Alert and Secure Action Group resources into an Easy Auth protected Container App in East US 2 and onward to Slack, with separate delivery, private network, managed identity RBAC, data, and observability paths.](img/architecture-flow.svg)

*Operator map: numbered blue arrows are the business event path; orange is
container delivery, green is private network/data access, dashed purple is
RBAC/control, and dotted gray is telemetry. Azure service symbols use the
official [Microsoft Azure Architecture Icons](https://learn.microsoft.com/azure/architecture/icons/)
V24 under Microsoft's published icon terms.*

### Why these resources exist

The Azure portal shows both workload resources and Azure-generated supporting
resources. This table explains how each visible resource relates to the
operator map instead of treating the resource group as a flat inventory.

| Resource type | Location | Purpose and relationship |
|---|---|---|
| Azure Container Registry (ACR) | East US 2 | Receives the Docker image built by `azd`, then supplies that image to each Container App revision. The managed identity receives only `AcrPull`; registry admin access remains disabled. |
| Action Group | Global | Converts the matched alert into an Entra-authenticated Secure Webhook request using Common Alert Schema. Its receiver targets the Container App's public HTTPS endpoint. |
| Activity Log Alert | Global | Matches `ServiceHealth` events at subscription or management-group scope and invokes the Action Group. Service Health alerts require Global location. |
| Application Insights | East US 2 | Collects OpenTelemetry requests, dependencies, exceptions, and custom metrics from the application. It is workspace-based and feeds the supporting Failure Anomalies alert. |
| Container App | East US 2 | Hosts public health probes and the Easy Auth protected webhook. The application performs authorization, parsing, routing, Table-backed idempotency, and Slack posting/updating. |
| Container Apps Environment | East US 2 | Provides the VNet-integrated compute boundary and sends platform logs to Log Analytics. The application runs as a revision inside this environment. |
| Failure Anomalies | Global (Azure-generated) | Supporting Application Insights smart-detection alert created automatically by Azure. It monitors failure-rate anomalies but is not part of the Service Health-to-Slack business flow. |
| User-assigned Managed Identity | East US 2 | Authenticates the running revision to ACR, Key Vault, and Table Storage without application credentials. Its roles are exactly `AcrPull`, `Key Vault Secrets User`, and `Storage Table Data Contributor`. |
| Key Vault | East US 2 | Stores the Slack `xoxb-` token. Public network access is disabled; the Container App resolves and reaches it through the Key Vault Private DNS zone and private endpoint. |
| Log Analytics workspace | East US 2 | Stores Container Apps platform/application logs and backs workspace-based Application Insights for KQL queries and retention. |
| Key Vault Private Endpoint | East US 2 | Gives Key Vault a private IP in the private-endpoint subnet and is the Container App's private data path to the Slack token. |
| Table Storage Private Endpoint | East US 2 | Gives the Storage Table service a private IP in the private-endpoint subnet and carries incident-state, lease, and deduplication traffic. |
| Network interfaces (2, Azure-generated) | East US 2 | Azure creates one NIC implementation detail for each private endpoint. They appear in the portal resource list but are intentionally summarized inside the two private-endpoint nodes rather than cluttering the main flow. |
| Key Vault Private DNS zone | Global | `privatelink.vaultcore.azure.net`; linked to the VNet so the public Key Vault hostname resolves to the Key Vault private endpoint. |
| Table Storage Private DNS zone | Global | `privatelink.table.core.windows.net`; linked to the VNet so the Table endpoint resolves to the Storage private endpoint. |
| Storage account and Table | East US 2 | Persists incident lifecycle state, Slack message timestamps, ETags, and leases for deduplication. HTTPS/TLS 1.2 is enforced, shared-key access is disabled, and data flows through the private endpoint. |
| Virtual network | East US 2 | Contains the delegated Container Apps infrastructure subnet and the private-endpoint subnet, keeping Key Vault and Table egress private while leaving the authenticated webhook ingress public. |

## Routes

| Route | Purpose |
|---|---|
| `POST /api/service-health` | Authenticated Common Alert Schema webhook |
| `GET /healthz` | Process liveness (public) |
| `GET /readyz` | Service Health configuration readiness (public) |

The webhook consumes Azure Monitor's
[Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema):
standard fields are under `data.essentials`, and alert-specific fields are
under `data.alertContext`. For Service Health, the app confirms
`eventSource: ServiceHealth` and reads `subscriptionId`, Active/Resolved
`status`, and `Properties.title`, `Properties.communication`,
`Properties.trackingId`, and `Properties.impactedServices`. The last field is
itself escaped JSON containing `ServiceName` and `RegionName`, as documented in
[Service Health notification properties](https://learn.microsoft.com/azure/service-health/service-health-notifications-properties).

## Prerequisites

- Python 3.13 for local development
- Current stable [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
  [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
  PowerShell 7+ (`pwsh`), and Docker with a Linux container engine
- A Slack app with an [`xoxb-` bot token](https://docs.slack.dev/authentication/tokens/#bot)
  limited to the granular [`chat:write` scope](https://docs.slack.dev/reference/scopes/chat.write)
  and invited to every configured destination channel
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
  Container Apps documents that the configured client ID is always an allowed
  audience and that the default App ID URI is
  `api://<APPLICATION_CLIENT_ID>`; see
  [Microsoft Entra authentication for Container Apps](https://learn.microsoft.com/azure/container-apps/authentication-entra).
- **App-level check**: the app additionally requires the official AzNS AAD
  Webhook application ID (`461e8683-5575-4561-ac7f-899cc907d62a`), the
  `ActionGroupsSecureWebhook` app role, and the configured audience —
  see `service_health/auth.py`. Entra v2 tokens identify the audience with the
  API client ID; both that GUID and its `api://<client-id>` identifier URI are
  accepted explicitly.
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
environment before provisioning. The full, security-safe WSL procedure is in
the [step-by-step deployment guide](#step-by-step-deployment-guide). In
particular, do not paste the Slack token directly into a command:

```bash
az login
azd auth login
azd env new
read -rsp "Slack bot token (input hidden): " SLACK_BOT_TOKEN; printf '\n'
azd env set SLACK_BOT_TOKEN "$SLACK_BOT_TOKEN"
unset SLACK_BOT_TOKEN
```

> **Why base64?** `azd`'s parameter substitution does a raw text replace of
> `${VAR}` into `infra/main.parameters.json` *before* parsing it as JSON, so a
> raw JSON value containing quotes can corrupt the parameter file. Passing the
> routing config as base64 avoids the problem entirely — Bicep decodes it
> (`base64ToString(...)`) before writing it to the container's
> `SERVICE_HEALTH_ROUTES_JSON` environment variable.

The pre-provision hook runs `scripts/configure-secure-webhook.ps1`. It creates
or reuses the protected API app registration, app role, API service
principal, AzNS ownership, and AzNS app-role assignment, then writes the
resulting IDs to the AZD environment. Azure Monitor requires both ownership of
the protected API app by the AzNS service principal and the
`ActionGroupsSecureWebhook` role assignment. The script is idempotent (safe to
re-run) and requires Microsoft Graph application administration permission.
Azure CLI and AZD maintain separate authentication sessions, so both
`az login` and `azd auth login` are required on a clean workstation.

`infra/modules/service-health-alert.bicep` is deliberately isolated so the
Activity Log Alert can be repeated for additional subscriptions while the
Container App remains central. Deploy that module at the target subscription
with the central webhook and Secure Webhook app values.

Secrets are copied from AZD environment parameters into Key Vault during
provisioning and exposed to the Container App only as Key Vault secret
references. Runtime access to Key Vault and Table Storage uses the
user-assigned managed identity and RBAC — never shared keys.

The registry defaults to the `Basic` SKU and intentionally has no untagged
manifest retention policy. Azure Container Registry retention is a
[preview, Premium-only feature](https://learn.microsoft.com/azure/container-registry/container-registry-retention-policy);
configuring it on `Basic` causes provisioning to fail.

### Multi-subscription / tenant-wide alerting (Management Group scope)

No prior Log Analytics or Azure Monitor Logs setup is required for any of
this — Service Health events flow through the platform **Activity Log**,
which is enabled by default on every Azure subscription at no cost. This
means the bot works out of the box for any Azure customer, even one with a
brand-new subscription and no monitoring configured.

By default the Activity Log Alert is scoped to the single subscription
where you provision (`targetSubscriptionId`). If a customer manages many
subscriptions under one or more **management groups**, you can scope the
alert to a management group instead, so one deployment captures Service
Health events for every subscription under it:

```sh
azd env set AZURE_MANAGEMENT_GROUP_ID "<management-group-id>"
azd provision
```

This overrides the `managementGroupId` parameter in
`infra/modules/service-health-alert.bicep`, changing the alert's `scopes`
from `/subscriptions/<subscriptionId>` to
`/providers/Microsoft.Management/managementGroups/<management-group-id>`.
Requirements:

- The principal running `azd provision`/`az deployment` needs **Monitoring
  Contributor** (or **Contributor**) on the management group, in addition
  to the usual subscription-level roles for the rest of the stack.
- The Container App, Key Vault, Storage, and networking resources are still
  deployed once, into the target subscription — only the alert's scope
  changes, so no extra Container Apps deployments are needed per
  subscription.
- Leave `AZURE_MANAGEMENT_GROUP_ID` unset (the default) to keep the
  existing single-subscription behavior.

## Step-by-step deployment guide

This is the complete, from-zero WSL walkthrough: create the Slack app, safely
capture its token, provision Azure, wire up the Secure Webhook, and verify an
end-to-end alert. Unless noted otherwise, run commands from an Ubuntu WSL
terminal in the repository root.

### 0. Prerequisites

- Azure subscription with permission to create resource groups and role
  assignments (`Owner` or `Contributor` + `User Access Administrator`) at the
  target subscription.
- A Microsoft Entra role that can create app registrations and app roles for
  the Secure Webhook app (`Application Administrator` or `Cloud Application
  Administrator`, or an admin who can grant consent once).
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
  [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
  PowerShell 7+ (`pwsh`), Git, and Python 3 installed inside WSL.
- Docker Desktop using the WSL 2 backend, Linux containers, and WSL integration
  enabled for the distribution where you run this guide. Docker recommends
  WSL 2.1.5 or later.
- A Slack workspace where you can create/install apps. You don't need a new
  workspace for every test — reuse any workspace where you have permission to
  create apps (e.g. your team's), or create a free one for testing at
  <https://slack.com/get-started#/createnew> in about a minute (no credit
  card, no company approval needed).

The official Linux installer for `azd` is:

```bash
curl -fsSL https://aka.ms/install-azd.sh | bash
```

Use current stable tool releases. This deployment was revalidated with Azure
CLI 2.84.0, Docker 29.6.2, and the WSL 2 backend. `azd` 1.24.1 completed the
deployment, but 1.30.0 was current at the time of the audit and is preferred.
Verify the local toolchain and Docker engine before continuing:

```bash
az version
azd version
pwsh --version
docker context show
docker info --format 'engine={{.ServerVersion}} os={{.OSType}}'
docker run --rm hello-world
```

The Docker `os` must be `linux`. In Windows PowerShell, the Docker Desktop
context is normally `desktop-linux`; inside an integrated WSL distribution it
may appear as `default`. If the daemon is unavailable or reports Windows
containers, start Docker Desktop, select **Use the WSL 2 based engine**, enable
the distribution under **Settings → Resources → WSL Integration**, switch to
Linux containers, and reopen WSL. Do not install a second Docker Engine inside
the distribution when using Docker Desktop.

### 1. Create the Slack app and bot token

1. Go to <https://api.slack.com/apps> and click **Create New App** → **From
   scratch**. Give it a name (e.g. `Azure Service Health`) and pick your
   workspace.
2. Open **OAuth & Permissions** in the left sidebar. Under **Scopes → Bot
   Token Scopes**, add only `chat:write`. Do not add the broader
   `chat:write.public` scope; preserve least privilege by explicitly inviting
   the bot to each destination channel.
3. Click **Install to Workspace** at the top of the same page, review the
   permissions, and approve.
4. Leave the **Bot User OAuth Token** (starts with `xoxb-`) in the Slack UI
   until step 4. Treat the token as a secret: do not paste it into chat, source
   files, screenshots, logs, or a literal shell command.
5. Invite the bot to every channel it needs to post in, e.g.
   `/invite @Azure Service Health` in each destination channel — the bot
   token alone does not grant channel membership.

This app only ever calls `chat.postMessage`/`chat.update`; it never receives
events, so **no Signing Secret, Event Subscriptions, slash commands, or app
manifest are required.** Its Slack messages include both blocks and a top-level
`text` value, which Slack uses as the notification and screen-reader
[accessibility fallback](https://docs.slack.dev/reference/methods/chat.postMessage/#accessibility-considerations).

### 2. Clone the repo and sign in

```sh
git clone https://github.com/ricmmartins/azure-service-health-slack-bot.git
cd azure-service-health-slack-bot
az login
azd auth login
az account show --query '{tenant:tenantId,subscription:id,name:name}' -o table
azd auth login --check-status
```

`az` and `azd` keep separate credential caches, so both logins are required
even if you're already signed in to one. Both commands use interactive browser
authentication by default. If Conditional Access blocks device-code flow, do
not add `--use-device-code`; use the browser flow from a WSL session that can
open the Windows browser. `azd auth login --use-device-code=false` explicitly
forces browser authentication. A headless Cloud Shell or SSH session that can
only offer device code will not bypass that tenant policy.

### 3. Create the routing configuration

Copy the example and edit it for your subscriptions/services/regions/channels
(Slack channel IDs, not names):

```bash
cp config/service_health_routes.example.json config/service_health_routes.json
```

To discover the ID in Slack, open the destination channel, select the channel
name or the **three dots** menu, choose **View channel details**, and select
**Copy channel ID** at the bottom. Public channel IDs normally start with `C`;
private channel IDs can start with `G`. A copied channel URL also contains the
ID, but do not configure `#channel-name`.

Slack's API-based discovery method is
[`conversations.list`](https://docs.slack.dev/reference/methods/conversations.list),
but it requires additional read scopes. The UI procedure above avoids expanding
this bot's permissions beyond `chat:write`.

`default_channel_id` is mandatory; it's where anything that doesn't match a
rule is posted. See [Service Health routing](#service-health-routing) above
for the matching rules. Confirm that the bot is a member of every configured
channel.

### 4. Initialize the AZD environment

```bash
# Prompts for an environment name, subscription, and Azure region.
azd env new

# Paste only at the hidden prompt. The token never appears on screen or in
# shell history; the command history contains only the variable name.
read -rsp "Slack bot token (input hidden): " SLACK_BOT_TOKEN; printf '\n'
while [[ "$SLACK_BOT_TOKEN" != xoxb-* ]]; do
  unset SLACK_BOT_TOKEN
  echo "Expected an xoxb token; try again." >&2
  read -rsp "Slack bot token (input hidden): " SLACK_BOT_TOKEN; printf '\n'
done
azd env set SLACK_BOT_TOKEN "$SLACK_BOT_TOKEN"
unset SLACK_BOT_TOKEN

ROUTES_B64="$(base64 -w0 config/service_health_routes.json)"
azd env set SERVICE_HEALTH_ROUTES_JSON_B64 "$ROUTES_B64"
unset ROUTES_B64
```

> `SERVICE_HEALTH_ROUTES_JSON_B64` must be base64-encoded (see the note in
> [Deploy with AZD](#deploy-with-azd) for why). Bicep decodes it before it
> reaches the container as the plain-JSON `SERVICE_HEALTH_ROUTES_JSON`
> environment variable — the application itself never sees base64.

`azd env set` persists the token in the selected local AZD environment so it
can be passed as a secure Bicep parameter into Key Vault. AZD environment
`.env` files are ignored by this repository's `*.env` rule, but they still
contain a credential: protect the workstation, never commit or print them, and
do not use `azd env get-values` in logs because it prints every value. Rotate
the Slack token immediately if it is ever exposed.

`AZURE_ENV_NAME`, `AZURE_LOCATION`, and `AZURE_SUBSCRIPTION_ID` are captured
by `azd env new`; every other required Bicep parameter is filled in
automatically by the provisioning steps below.

### 5. Provision Azure infrastructure

Register the providers that were absent in the live test subscription, then
provision:

```bash
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.ContainerService --wait
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
4. Adds the AzNS service principal as an owner of the protected API app, as
   required by Azure Monitor to create, modify, and test Secure Webhook actions.
5. Grants the AzNS service principal the `ActionGroupsSecureWebhook` app
   role on your API app — this is what lets Azure Monitor's Secure Webhook
   Action Group call your endpoint with a verifiable Entra token.
6. Writes `AZURE_TENANT_ID`, `SERVICE_HEALTH_API_CLIENT_ID`,
   `SERVICE_HEALTH_API_OBJECT_ID`, and `SERVICE_HEALTH_API_IDENTIFIER_URI`
   into the AZD environment for the Bicep deployment to consume.

`azd provision` then deploys `infra/main.bicep`, which creates the resource
group, Container Apps environment, Container Registry, Key Vault (with the
Slack bot token as a secret), Storage Account/Table, Application Insights,
the Container App itself (Easy Auth wired to the app registration from the
script), and the Activity Log Alert + Secure Action Group targeting the
current subscription. The Service Health Activity Log Alert and Action Group
are both created in the `Global` location. Service Health notifications require
a Global Action Group; a regional Action Group does not work for this alert
type. Microsoft documents this requirement, the Secure Webhook AzNS ownership
and application-role setup, and retry behavior in
[Create and manage Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups).

The protected API exposes `api://<client-id>`, requests Entra v2 tokens, and
configures Easy Auth to accept both valid audience forms: the client ID GUID
that Entra v2 commonly emits and `api://<client-id>`. The application performs
the same normalization before checking the audience claim.

### 6. Deploy the application image

```sh
azd deploy
```

Builds the Docker image from this repo, pushes it to the new Container
Registry, and updates the Container App revision. `azd up` combines steps 5
and 6 (`azd provision && azd deploy`) if you prefer a single command.

### 7. Verify the deployment

Capture only the nonsecret outputs needed by the checks:

```bash
APP_URI="$(azd env get-value SERVICE_APP_URI)"
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP)"
APP_NAME="$(azd env get-value SERVICE_APP_NAME)"
ENV_NAME="$(azd env get-value AZURE_ENV_NAME)"
ACTION_GROUP="ag-${ENV_NAME}-service-health"
ACTIVITY_ALERT="ala-${ENV_NAME}-service-health"
API_CLIENT_ID="$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID)"
API_IDENTIFIER_URI="$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI)"
ACR_LOGIN_SERVER="$(azd env get-value AZURE_CONTAINER_REGISTRY_ENDPOINT)"
ACR_NAME="${ACR_LOGIN_SERVER%%.*}"
STORAGE_NAME="$(az storage account list --resource-group "$RESOURCE_GROUP" --query '[0].name' -o tsv)"
KEY_VAULT_NAME="$(az keyvault list --resource-group "$RESOURCE_GROUP" --query '[0].name' -o tsv)"
IDENTITY_ID="$(az containerapp show --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --query 'keys(identity.userAssignedIdentities)[0]' -o tsv)"
IDENTITY_PRINCIPAL_ID="$(az identity show --ids "$IDENTITY_ID" --query principalId -o tsv)"
```

`SERVICE_APP_URI` already includes `https://`; do not prepend another scheme.

#### 7.1 Health, readiness, and authentication boundary

```bash
test "$(curl -fsS "$APP_URI/healthz")" = '{"status":"healthy"}'
test "$(curl -fsS "$APP_URI/readyz")" = '{"status":"ready"}'

WEBHOOK_RESPONSE="$(mktemp)"
HTTP_CODE="$(curl -sS -o "$WEBHOOK_RESPONSE" -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  "$APP_URI/api/service-health")"
test "$HTTP_CODE" = "401"
python3 -m json.tool "$WEBHOOK_RESPONSE"
rm -f "$WEBHOOK_RESPONSE"
```

Both probes are intentionally public. `/readyz` returning `200` also proves
the production configuration can initialize with managed identity, Key Vault,
and Table Storage. The unauthenticated webhook must return HTTP `401` with
`authentication_required`; `200` would be a security regression.

#### 7.2 Slack authentication and a visible test post

[`auth.test`](https://docs.slack.dev/reference/methods/auth.test) is Slack's
supported authentication connectivity check. This test reads the `xoxb-` token
at a hidden prompt, sends it only in the HTTP `Authorization` header, and unsets
it immediately after both calls. The literal token is never displayed, written
to disk, or placed in shell history. The test prints only nonsecret Slack IDs
and creates one visible
[`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage)
post in the configured fallback channel.

```bash
CHANNEL_ID="$(python3 -c \
  'import json; print(json.load(open("config/service_health_routes.json"))["default_channel_id"])')"

read -rsp "Slack bot token (input hidden): " SLACK_BOT_TOKEN; printf '\n'
if [[ "$SLACK_BOT_TOKEN" != xoxb-* ]]; then
  unset SLACK_BOT_TOKEN
  echo "Expected an xoxb bot token." >&2
  exit 1
fi

AUTH_RESPONSE="$(mktemp)"
POST_PAYLOAD="$(mktemp)"
POST_RESPONSE="$(mktemp)"
cleanup_slack_test() {
  unset SLACK_BOT_TOKEN
  rm -f "$AUTH_RESPONSE" "$POST_PAYLOAD" "$POST_RESPONSE"
}
trap cleanup_slack_test EXIT

# Supplying the header through stdin keeps the expanded token out of curl's
# command-line arguments as well as shell history.
slack_api() {
  curl --silent --show-error --fail-with-body \
    --config <(printf 'header = "Authorization: Bearer %s"\n' "$SLACK_BOT_TOKEN") \
    "$@"
}

slack_api https://slack.com/api/auth.test > "$AUTH_RESPONSE"

SLACK_CHANNEL_ID="$CHANNEL_ID" python3 - <<'PY' > "$POST_PAYLOAD"
import json
import os
import sys

json.dump({
    "channel": os.environ["SLACK_CHANNEL_ID"],
    "text": "Azure Service Health deployment validation",
}, sys.stdout)
PY

slack_api \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary "@$POST_PAYLOAD" \
  https://slack.com/api/chat.postMessage > "$POST_RESPONSE"
unset SLACK_BOT_TOKEN

AUTH_RESPONSE="$AUTH_RESPONSE" POST_RESPONSE="$POST_RESPONSE" python3 - <<'PY'
import json
import os

def load_result(method, path):
    with open(path, encoding="utf-8") as response:
        result = json.load(response)
    if not result.get("ok"):
        raise SystemExit(f"{method} failed: {result.get('error', 'unknown_error')}")
    return result

auth = load_result("auth.test", os.environ["AUTH_RESPONSE"])
message = load_result("chat.postMessage", os.environ["POST_RESPONSE"])
print(f"Slack auth OK: team={auth.get('team_id')} bot={auth.get('user_id')}")
print(f"Slack post OK: channel={message.get('channel')} ts={message.get('ts')}")
PY

cleanup_slack_test
trap - EXIT
```

Delete the test message in Slack if it is not needed.

#### 7.3 Azure resources, Secure Webhook, and secret wiring

```bash
(
  set -euo pipefail
  expect() {
    [[ "$1" == "$2" ]] || {
      printf 'Expected %s, got %s\n' "$2" "$1" >&2
      return 1
    }
  }

  expect "$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --query properties.runningStatus -o tsv)" "Running"
  expect "$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --query properties.provisioningState -o tsv)" "Succeeded"
  expect "$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --query properties.template.scale.minReplicas -o tsv)" "1"
  expect "$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --query properties.configuration.ingress.external -o tsv)" "true"
  expect "$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --query properties.configuration.ingress.allowInsecure -o tsv)" "false"

  AUTH_RESOURCE_TYPE="Microsoft.App/containerApps/authConfigs"
  expect "$(az resource show -g "$RESOURCE_GROUP" --resource-type "$AUTH_RESOURCE_TYPE" \
    --name "$APP_NAME/current" --api-version 2024-03-01 \
    --query properties.platform.enabled -o tsv)" "true"
  expect "$(az resource show -g "$RESOURCE_GROUP" --resource-type "$AUTH_RESOURCE_TYPE" \
    --name "$APP_NAME/current" --api-version 2024-03-01 \
    --query 'properties.identityProviders.azureActiveDirectory.validation.defaultAuthorizationPolicy.allowedApplications[0]' \
    -o tsv)" "461e8683-5575-4561-ac7f-899cc907d62a"
  ACTUAL_AUDIENCES="$(az resource show -g "$RESOURCE_GROUP" \
    --resource-type "$AUTH_RESOURCE_TYPE" --name "$APP_NAME/current" \
    --api-version 2024-03-01 \
    --query 'properties.identityProviders.azureActiveDirectory.validation.allowedAudiences' \
    -o tsv | sort)"
  EXPECTED_AUDIENCES="$(printf '%s\n%s\n' "$API_CLIENT_ID" "$API_IDENTIFIER_URI" | sort)"
  expect "$ACTUAL_AUDIENCES" "$EXPECTED_AUDIENCES"

  expect "$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
    --query location -o tsv | tr '[:upper:]' '[:lower:]')" "global"
  expect "$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
    --query enabled -o tsv)" "true"
  expect "$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
    --query 'webhookReceivers[0].useAadAuth' -o tsv)" "true"
  expect "$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
    --query 'webhookReceivers[0].useCommonAlertSchema' -o tsv)" "true"

  expect "$(az monitor activity-log alert show -g "$RESOURCE_GROUP" -n "$ACTIVITY_ALERT" \
    --query location -o tsv | tr '[:upper:]' '[:lower:]')" "global"
  expect "$(az monitor activity-log alert show -g "$RESOURCE_GROUP" -n "$ACTIVITY_ALERT" \
    --query enabled -o tsv)" "true"
  expect "$(az monitor activity-log alert show -g "$RESOURCE_GROUP" -n "$ACTIVITY_ALERT" \
    --query 'condition.allOf[0].equals' -o tsv)" "ServiceHealth"

  expect "$(az acr show --name "$ACR_NAME" --query sku.name -o tsv)" "Basic"
  expect "$(az acr show --name "$ACR_NAME" --query adminUserEnabled -o tsv)" "false"

  expect "$(az storage account show -g "$RESOURCE_GROUP" -n "$STORAGE_NAME" \
    --query enableHttpsTrafficOnly -o tsv)" "true"
  expect "$(az storage account show -g "$RESOURCE_GROUP" -n "$STORAGE_NAME" \
    --query minimumTlsVersion -o tsv)" "TLS1_2"
  expect "$(az storage account show -g "$RESOURCE_GROUP" -n "$STORAGE_NAME" \
    --query allowSharedKeyAccess -o tsv)" "false"
  expect "$(az storage account show -g "$RESOURCE_GROUP" -n "$STORAGE_NAME" \
    --query publicNetworkAccess -o tsv)" "Disabled"
  STORAGE_ID="$(az storage account show -g "$RESOURCE_GROUP" -n "$STORAGE_NAME" \
    --query id -o tsv)"
  TABLE_NAME="$(az rest --method get \
    --url "https://management.azure.com${STORAGE_ID}/tableServices/default/tables/ServiceHealthIncidents?api-version=2023-05-01" \
    --query name -o tsv)"
  [[ "$TABLE_NAME" == "default/ServiceHealthIncidents" ||
     "$TABLE_NAME" == "ServiceHealthIncidents" ]]

  expect "$(az keyvault show -g "$RESOURCE_GROUP" -n "$KEY_VAULT_NAME" \
    --query properties.enableRbacAuthorization -o tsv)" "true"
  expect "$(az keyvault show -g "$RESOURCE_GROUP" -n "$KEY_VAULT_NAME" \
    --query properties.enablePurgeProtection -o tsv)" "true"
  expect "$(az keyvault show -g "$RESOURCE_GROUP" -n "$KEY_VAULT_NAME" \
    --query properties.softDeleteRetentionInDays -o tsv)" "90"
  expect "$(az keyvault show -g "$RESOURCE_GROUP" -n "$KEY_VAULT_NAME" \
    --query properties.publicNetworkAccess -o tsv)" "Disabled"

  ACTUAL_ROLES="$(az role assignment list --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
    --all --query '[].roleDefinitionName' -o tsv | sort -u)"
  EXPECTED_ROLES="$(printf '%s\n' AcrPull 'Key Vault Secrets User' \
    'Storage Table Data Contributor' | sort)"
  expect "$ACTUAL_ROLES" "$EXPECTED_ROLES"

  SECRET_REFERENCE="$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --query "properties.configuration.secrets[?name=='slack-bot-token'] | [0].keyVaultUrl" \
    -o tsv)"
  [[ "$SECRET_REFERENCE" == \
    "https://${KEY_VAULT_NAME}.vault.azure.net/secrets/slack-bot-token/"* ]]
)
```

These checks use Azure Resource Manager metadata. They confirm that the secret
reference points to a versioned `slack-bot-token` URI, but never request the
secret value. A deployer without Key Vault data-plane access should get an
authorization error if they try to read the value; that is expected and is not
a reason to grant themselves `Key Vault Secrets User`. The Container App's
managed identity is the intended secret reader.

#### 7.4 Official Service Health Action Group test

Use Azure Monitor's signed Service Health test, not a hand-crafted webhook
payload. The CLI command does **not** reuse the receivers stored on the named
Action Group; omitting `--add-action` returns
`BadRequest: There are no valid receivers in the request`. Read the deployed
receiver metadata and pass it back to the
[`test-notifications create` command](https://learn.microsoft.com/cli/azure/monitor/action-group/test-notifications#az-monitor-action-group-test-notifications-create):

```bash
URI="$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
  --query 'webhookReceivers[0].serviceUri' -o tsv)"
OBJECT_ID="$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
  --query 'webhookReceivers[0].objectId' -o tsv)"
IDENTIFIER_URI="$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
  --query 'webhookReceivers[0].identifierUri' -o tsv)"

az monitor action-group test-notifications create \
  --resource-group "$RESOURCE_GROUP" \
  --action-group-name "$ACTION_GROUP" \
  --alert-type servicehealth \
  --add-action webhook slack-service-health "$URI" \
    useaadauth "$OBJECT_ID" "$IDENTIFIER_URI" usecommonalertschema \
  --only-show-errors \
  -o json

az containerapp logs show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --type console \
  --tail 200
```

The equivalent portal path is **Monitor → Alerts → Action groups → select the
action group → Test → Service Health**. A successful result must produce an
operation state of `Complete`, an HTTP `200` `POST /api/service-health` from
`IcMBroadcaster/1.0` in the Container App access log, and a formatted Service
Health message in Slack.

Do not repeatedly run failing tests. Azure Monitor retries retryable webhook
failures up to five times. HTTP `408`, `429`, `503`, and `504`, plus transport
exceptions, are retryable. After the retry sequence is exhausted, Action
Groups suppress all calls to that endpoint for 15 minutes. Fix the underlying
issue, wait the full cooldown, and then run one official `servicehealth` test.
During the cooldown, even correct configuration can appear broken.

### 8. Add more subscriptions (optional)

The Container App and Secure Webhook app registration are central; only the
Activity Log Alert + Action Group need to exist per subscription. Deploy
`infra/modules/service-health-alert.bicep` into a resource group in any
additional subscription, reusing the same webhook URI and Secure Webhook
identity:

```bash
WEBHOOK_URI="$(azd env get-value SERVICE_APP_URI)/api/service-health"
API_OBJECT_ID="$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID)"
API_IDENTIFIER_URI="$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI)"
TENANT_ID="$(azd env get-value AZURE_TENANT_ID)"
ENV_NAME="$(azd env get-value AZURE_ENV_NAME)"

OTHER_SUBSCRIPTION_ID="<other-subscription-id>"
OTHER_RESOURCE_GROUP="<resource-group-in-other-subscription>"
az account set --subscription "$OTHER_SUBSCRIPTION_ID"
az group create --name "$OTHER_RESOURCE_GROUP" --location "<azure-region>"
az deployment group create \
  --resource-group "$OTHER_RESOURCE_GROUP" \
  --template-file infra/modules/service-health-alert.bicep \
  --parameters environmentName="$ENV_NAME" \
               webhookUri="$WEBHOOK_URI" \
               secureWebhookObjectId="$API_OBJECT_ID" \
               secureWebhookIdentifierUri="$API_IDENTIFIER_URI" \
               tenantId="$TENANT_ID" \
               targetSubscriptionId="$OTHER_SUBSCRIPTION_ID" \
               tags="{\"azd-env-name\":\"$ENV_NAME\"}"
```

Pull the `SERVICE_HEALTH_API_*` and `AZURE_TENANT_ID` values from
individual `azd env get-value <name>` calls in the environment used for the
original deployment. Avoid `azd env get-values` because it also prints the
Slack token.

### 9. Update routing or redeploy later

Editing `config/service_health_routes.json` is an infrastructure configuration
change, not an image change. Re-encode it and run provisioning so Bicep creates
a revision with the new environment value; `azd deploy` alone does not update
Container App environment variables:

```bash
ROUTES_B64="$(base64 -w0 config/service_health_routes.json)"
azd env set SERVICE_HEALTH_ROUTES_JSON_B64 "$ROUTES_B64"
unset ROUTES_B64
azd provision
```

### 10. Tear down

Capture the protected API client ID before deleting the AZD resources:

```bash
API_CLIENT_ID="$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID)"
azd down --purge
az ad app delete --id "$API_CLIENT_ID"
unset API_CLIENT_ID
```

`--purge` also removes the soft-deleted Key Vault so the name can be reused.
`azd down` does not remove the Entra app registration created by
`configure-secure-webhook.ps1`; the explicit `az ad app delete` does. Never
delete Microsoft's official AzNS AAD Webhook service principal. If you are
keeping the deployment, do not run these cleanup commands.

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

### Troubleshooting

| Symptom | Check and correction |
|---|---|
| `az` or `azd` device-code login is blocked | Conditional Access can reject device code. Run the normal interactive browser flows (`az login`, `azd auth login --use-device-code=false`) from browser-capable WSL. Do not attempt to bypass the policy from a headless shell. |
| Docker is unavailable from WSL | Run `docker context show` and `docker info`. Enable Docker Desktop's WSL 2 engine, Linux containers, and integration for the current distribution; then reopen WSL. |
| Provisioning fails on ACR retention | `Basic` is the default SKU and must not have an untagged manifest retention policy. Use the current `infra/modules/registry.bicep`; retention is Premium-only. |
| Secure Webhook creation/test reports authorization failure | Re-run `azd provision` after confirming the Azure CLI account has Application Administrator permissions. The AzNS service principal must own the protected API app and hold its `ActionGroupsSecureWebhook` role. |
| Official test returns `401` or `403` | Inspect Container App logs. Easy Auth and the app accept the Entra v2 client-ID GUID audience and `api://<client-id>`; verify both remain in `allowedAudiences` and that the AzNS caller/role checks were not removed. |
| Official test reports success but Slack has no message | Run the Slack validation in step 7.2. Confirm the token is a bot `xoxb` token, the ID is a channel ID rather than a name, the bot is invited, and `chat:write` is granted. |
| Corrected webhook still receives nothing | Failed webhook retries suppress Action Group calls to the endpoint for 15 minutes. Wait for the cooldown before one new `servicehealth` test. |
| `/readyz` returns `503` | Stream console and system logs with `az containerapp logs show`. Verify the managed identity role assignments, Key Vault/Table private endpoints, private DNS links, and that the current revision references the latest Key Vault secret. |

### Rollback

For an application regression, reactivate the previous Container App revision
or deploy a known-good Git commit:

```bash
az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --query '[].{name:name,active:properties.active,created:properties.createdTime}' \
  -o table

az containerapp revision activate \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --revision "<known-good-revision>"
```

For a routing or Slack-token rollback, restore the previous routing file or
securely capture the replacement token with the hidden `read` procedure in
step 4, update the AZD value, and run `azd provision`. Provisioning creates a
new Key Vault secret version and updates the Container App reference; do not
put a token in Git or a literal command. Use `azd down --purge` only for full
decommissioning, not routine rollback.

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
