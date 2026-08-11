# Azure Service Health Slack Bot

This Flask service receives Azure Service Health notifications from an Azure
Monitor Secure Webhook Action Group and posts them to Slack. It keeps one root
message per subscription and tracking ID. Newer notifications update that root
and add a broadcast reply to its thread.

This is a community reference implementation. Azure Service Health remains the
source of truth. The service does not acknowledge incidents, open Azure support
requests, receive Slack events, or replace an incident management system.

## Start here

| Goal | Read |
|---|---|
| Evaluate the design | [Architecture](#architecture), [Security model](#security-model), and [Known limits](#known-limits) |
| Run locally | [Local development](#local-development) |
| Deploy | [Deploy with Azure Developer CLI](#deploy-with-azure-developer-cli) |
| Add subscriptions | [Manage alert scopes](#manage-alert-scopes) |
| Operate or troubleshoot | [Operations](#operations) |
| Check the Microsoft platform evidence | [Microsoft platform evidence](docs/microsoft-platform-evidence.md) |

## Architecture

The deployment uses Azure Container Apps, Azure Container Registry, a
user-assigned managed identity, Key Vault, Azure Table Storage, Log Analytics,
and workspace-based Application Insights.

Azure Monitor matches Service Health events in a subscription Activity Log and
sends the Common Alert Schema payload to the public Container Apps endpoint.
Container Apps authentication validates the Microsoft Entra token. The
application then verifies the Azure Monitor caller application, token audience,
and `ActionGroupsSecureWebhook` app role before parsing the payload.

Key Vault and Table Storage have public network access disabled. The Container
Apps environment reaches both services through private endpoints and private
DNS. The webhook and health probes still use the Container App's public HTTPS
ingress.

![Example operator architecture map for the Azure Service Health event path, private data access, managed identity, and observability.](img/architecture-flow.svg)

The diagram is an example deployment in East US 2. All regional resources use
the AZD location selected during provisioning. The Activity Log Alert, Action
Group, and private DNS zones use the Azure `Global` location.

### Main resources

| Resource | Purpose |
|---|---|
| Container App | Runs the Flask service with public HTTPS ingress, one to three replicas, and single revision mode. |
| Container Apps environment | Provides the VNet-integrated compute boundary and sends platform logs to Log Analytics. |
| Action Group | Sends a Microsoft Entra authenticated Secure Webhook request using Common Alert Schema. |
| Activity Log Alert | Matches `category = ServiceHealth` for one subscription. |
| User-assigned managed identity | Pulls the image and accesses Key Vault and Table Storage without application credentials. |
| Key Vault | Stores the Slack bot token and exposes it to Container Apps through a Key Vault secret reference. |
| Storage account and table | Stores incident state, Slack timestamps, ETags, and processing leases. |
| Private endpoints and DNS zones | Provide private Key Vault and Table Storage data paths. |
| Container Registry | Stores the application image. Registry admin access and anonymous pull are disabled. |
| Application Insights and Log Analytics | Receive application telemetry and Container Apps logs. |

Application Insights can also create a platform-managed Failure Anomalies smart
detection rule. That rule is not part of the Service Health delivery path.

## HTTP routes

| Route | Access | Purpose |
|---|---|---|
| `POST /api/service-health` | Microsoft Entra token plus application checks | Receives Common Alert Schema notifications. |
| `GET /healthz` | Public | Reports process liveness. |
| `GET /readyz` | Public | Confirms required configuration and client construction. |

`/readyz` does not perform a Table Storage transaction. Use an accepted test
notification and Application Insights dependency telemetry to verify managed
identity, private DNS, and Table data-plane access.

## Service Health payload mapping

Azure Monitor places common fields in `data.essentials` and the Activity Log
event in `data.alertContext`. This service requires:

- `schemaId = azureMonitorCommonAlertSchema`
- `alertContext.eventSource = ServiceHealth`
- a subscription ID in `alertContext.subscriptionId`, `essentials.alertId`, or
  `essentials.alertTargetIDs`
- `properties.trackingId`, `title`, `communication`, `impactStartTime`, and
  `impactedServices`
- `alertContext.level`
- `submissionTimestamp` or `eventTimestamp`

Microsoft documents `properties.impactedServices` as an escaped JSON string.
The parser also accepts an already-decoded list for compatibility. Each service
contains `ServiceName` and `ImpactedRegions`, whose entries contain
`RegionName`.

Microsoft documents Activity Log status values such as `Active` and `Resolved`,
with additional stage values that depend on the incident type. `Updated` in
this application is a local lifecycle label: a newer accepted nonterminal
notification for an existing Slack incident is rendered as an update. It is
not presented as a complete list of Azure Service Health stage values.

See the official [Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema)
and [Service Health event properties](https://learn.microsoft.com/azure/service-health/service-health-event-properties).

## Routing

Set either `SERVICE_HEALTH_ROUTES_JSON` or
`SERVICE_HEALTH_ROUTES_FILE`. The example is
`config/service_health_routes.example.json`.

`default_channel_id` is required. Rules can filter by `subscription_ids`,
`services`, and `regions`. Every filter supplied by a rule must match. The
highest priority wins, followed by the most specific rule and then file order.
The first selected channel is stored with the incident and remains fixed for
that incident.

Use Slack channel IDs, not names. The bot must be a member of every destination
channel unless you grant the broader `chat:write.public` scope. This guide uses
only `chat:write` and explicit channel membership.

## Idempotency and lifecycle

The Table entity key is the normalized subscription ID plus a SHA-256 hash of
the tracking ID. ETags and a 30-second lease coordinate concurrent replicas.

The first accepted notification calls `chat.postMessage`. Each newer accepted
notification updates the root with `chat.update`, checkpoints the root state,
and posts a broadcast thread reply. Identical notifications and notifications
at or below the stored submission watermark return `200` without calling Slack.

Transient Slack or Storage failures return `503`, which is one of the status
codes Azure Monitor treats as retryable for webhooks. Invalid payloads and
permanent Slack errors return a nonretryable `4xx`.

## Security model

### Webhook authentication

The Action Group uses Secure Webhook authentication. The preprovision hook:

1. Creates or loads the protected API app registration by persisted object and
   client IDs.
2. Configures Microsoft Entra v2 access tokens and the
   `api://<client-id>` identifier URI.
3. Creates an application-only `ActionGroupsSecureWebhook` app role.
4. Ensures service principals exist for the API and the official Azure Monitor
   AzNS AAD Webhook application
   (`461e8683-5575-4561-ac7f-899cc907d62a`).
5. Adds the deployment caller and AzNS service principal as verified owners.
6. Assigns the app role to the AzNS service principal.

The official Secure Webhook procedure requires the Microsoft Entra
`Application Administrator` role to configure this relationship. This is a
deployment-time role, not a runtime permission.

Container Apps authentication accepts the API client ID and its identifier URI
as audiences and restricts the authenticated application to AzNS. The Flask
application independently checks the caller application claim, audience, and
app role from the Easy Auth principal header.

`globalValidation.unauthenticatedClientAction` is `AllowAnonymous` so the public
health probes work. The Flask webhook route returns `401` when the Easy Auth
principal is absent and `403` when its claims do not meet the application
policy.

### Managed identity and RBAC

The deployment creates these direct role assignments for one user-assigned
managed identity:

| Scope | Role | Use |
|---|---|---|
| Container Registry | `AcrPull` | Pull the application image. |
| Key Vault | `Key Vault Secrets User` | Resolve the Slack token reference. |
| Storage account | `Storage Table Data Contributor` | Read and write incident entities. |

The application uses `ManagedIdentityCredential` with `AZURE_CLIENT_ID` in
production and staging. It uses `DefaultAzureCredential` for local development.
Storage shared-key access is disabled.

### Network boundaries

Key Vault and Storage set `publicNetworkAccess` to `Disabled`. Each service has
a private endpoint and private DNS zone linked to the Container Apps VNet. The
Container Registry and Application Insights ingestion endpoints remain public
Azure endpoints.

The Bicep private DNS suffixes and the documented Service Health Secure Webhook
flow target Azure public cloud. Do not assume this template works unchanged in
a sovereign cloud.

## Known limits

- There is a crash window after Slack accepts a write and before Table Storage
  records its timestamp. An initial post can leave an untracked root, and a
  thread reply can be duplicated on replay.
- Slack does not provide a downstream idempotency key for these message calls,
  so the service cannot guarantee exactly-once delivery across Slack and Table
  Storage.
- The deployment pins a versioned Key Vault secret URI. Rotate the token through
  AZD and run provisioning so the Container App reference moves to the new
  secret version.
- The bootstrap workflow stores `SLACK_BOT_TOKEN` as plaintext in
  `.azure/<environment>/.env` because the target Key Vault does not exist before
  the first provision. Microsoft recommends against storing secrets in AZD
  environment files. Restrict local file access and do not copy, log, or commit
  the environment directory.
- The day-2 command implements a Management Group as one managed subscription
  alert path per accessible descendant. It does not deploy one native
  Management Group Activity Log Alert.
- The templates use Azure public cloud endpoint suffixes.

## Prerequisites

For deployment:

- Python 3.13
- current stable [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
  with Bicep
- current stable [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- Docker with a Linux container engine
- a Slack app with an `xoxb-` bot token and `chat:write`
- an Azure subscription where you can create the resources in `infra/`
- `Owner`, or `Contributor` plus `User Access Administrator`, at the target
  subscription so Bicep can create resources and role assignments
- Microsoft Entra `Application Administrator` while the Secure Webhook hook runs

The documented Bash commands work in Linux, macOS where the command syntax is
available, and Ubuntu on WSL. `base64 -w0` is GNU syntax, so macOS users should
use an equivalent no-wrap base64 command.

Register the resource providers documented for Container Apps:

```bash
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.OperationalInsights --wait
```

`Microsoft.ContainerService` is the Azure Kubernetes Service namespace and is
not a general Container Apps prerequisite for this deployment.

## Local development

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Copy `.env-example` to `.env`, then set:

- `SLACK_BOT_TOKEN`
- `AZURE_TABLE_ENDPOINT`
- one routing source
- `SERVICE_HEALTH_EXPECTED_AUDIENCE` only when `APP_ENV` is not
  `development` or `test`

The example routing file uses synthetic channel and subscription IDs. For local
Table access, sign in with an identity that has Storage Table data permissions:

```bash
az login
python app.py
```

Build the production image with:

```bash
docker build -t azure-service-health-slack-bot .
```

The Docker build retries package download through Microsoft's Python package
proxy if the configured package index fails. Override `PIP_INDEX_URL` or
`PIP_FALLBACK_INDEX_URL` with Docker build arguments when required by your
network policy.

## Deploy with Azure Developer CLI

### 1. Create the Slack app

1. Create a Slack app at <https://api.slack.com/apps>.
2. Under **OAuth & Permissions**, add the bot scope `chat:write`.
3. Install the app to the workspace and keep the `xoxb-` token private.
4. Invite the bot to every configured destination channel.

The service calls only `chat.postMessage` and `chat.update`. It does not need a
Slack signing secret, event subscriptions, slash commands, or an inbound app
manifest.

### 2. Clone and sign in

```bash
git clone https://github.com/ricmmartins/azure-service-health-slack-bot.git
cd azure-service-health-slack-bot
az login
azd auth login
az account show --query '{tenant:tenantId,subscription:id,name:name}' -o table
azd auth status
```

This runbook signs in to Azure CLI and AZD explicitly. Use the normal browser
flows required by your tenant's Conditional Access policy.

### 3. Create routing

```bash
cp config/service_health_routes.example.json config/service_health_routes.json
```

Replace every synthetic value. `default_channel_id` is mandatory. In Slack,
open a channel's details and copy its channel ID.

### 4. Create the AZD environment

```bash
azd env new

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

Hidden input prevents terminal echo, and the command history contains only the
variable name. The expanded token is still passed to the local `azd` process
and stored as plaintext in the selected AZD environment file. This bootstrap
design does not meet Microsoft's preferred AZD secret-reference pattern because
the target vault is created by the same provision. Restrict access to the
workstation and `.azure/<environment>/.env`, never commit or copy the
environment, and do not run `azd env get-values` in logs.

The routing document is base64 encoded because AZD substitutes parameter values
into `infra/main.parameters.json` before JSON parsing. Bicep decodes the value
before setting the container's plain JSON
`SERVICE_HEALTH_ROUTES_JSON` environment variable.

### 5. Preview or provision

Register the providers from [Prerequisites](#prerequisites), then provision:

```bash
azd provision
```

`azure.yaml` registers `scripts/configure_secure_webhook.py` as a Python
`preprovision` hook. AZD infers the Python runtime from the file extension,
installs dependencies from the nearest `requirements.txt`, and runs the hook
before provisioning.

The preview command needs the values produced by that hook. Run the hook
explicitly before the first preview of a new environment:

```bash
ENV_NAME="$(azd env get-value AZURE_ENV_NAME)"
azd hooks run preprovision -e "$ENV_NAME" --no-prompt
azd provision --preview -e "$ENV_NAME" --no-prompt
```

Microsoft documents the preview flag and independent hook execution. It does
not document a guarantee about whether preview invokes lifecycle hooks. The
explicit command above is therefore a project requirement, not a general AZD
platform claim.

Provisioning creates the resource group, network, private endpoints, Log
Analytics, Application Insights, Key Vault and token secret, Storage account
and table, Container Registry, Container Apps environment and app, Action
Group, and baseline Activity Log Alert.

### 6. Deploy the image

```bash
azd deploy
```

AZD builds the Docker image, pushes it to the registry, and updates the
Container App. `azd up` runs provision and deploy as one workflow.

### 7. Verify the deployment

Capture nonsecret outputs:

```bash
APP_URI="$(azd env get-value SERVICE_APP_URI)"
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP)"
APP_NAME="$(azd env get-value SERVICE_APP_NAME)"
ENV_NAME="$(azd env get-value AZURE_ENV_NAME)"
ACTION_GROUP="ag-${ENV_NAME}-service-health"
```

Check public probes and unauthenticated webhook rejection:

```bash
test "$(curl -fsS "$APP_URI/healthz")" = '{"status":"healthy"}'
test "$(curl -fsS "$APP_URI/readyz")" = '{"status":"ready"}'

HTTP_CODE="$(curl -sS -o /tmp/service-health-response.json -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  "$APP_URI/api/service-health")"
test "$HTTP_CODE" = "401"
python3 -m json.tool /tmp/service-health-response.json
rm -f /tmp/service-health-response.json
```

Check the Azure resource contract:

```bash
az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query '{state:properties.runningStatus,min:properties.template.scale.minReplicas,external:properties.configuration.ingress.external}' \
  -o yaml

az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
  --query '{location:location,enabled:enabled,aad:webhookReceivers[0].useAadAuth,commonSchema:webhookReceivers[0].useCommonAlertSchema}' \
  -o yaml
```

Use Azure Monitor's signed Service Health test. The test command requires the
receiver definition even when an Action Group already exists:

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
```

Confirm an HTTP `200` webhook request in Container App logs and a formatted
message in Slack. Microsoft REST examples show a completed operation state.
Live tests for this project have also returned `Complete` for the operation and
`Succeeded` for the receiver. The day-2 command accepts only the explicit states
covered by its tests and leaves a new alert disabled for any other result.

Do not repeat a failing test in a tight loop. Azure Monitor retries eligible
webhook failures and, after the retry sequence fails, suppresses Action Group
calls to that endpoint for 15 minutes.

## Manage alert scopes

The initial deployment creates one alert for its subscription. Add other
subscriptions with the Python day-2 command:

```bash
python scripts/manage_alert_scopes.py list
python scripts/manage_alert_scopes.py add-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000"
```

Management Group commands expand accessible descendants into managed
subscription alert paths:

```bash
python scripts/manage_alert_scopes.py add-management-group \
  --management-group-id "platform"
python scripts/manage_alert_scopes.py migrate-to-management-group \
  --management-group-id "platform" --what-if
python scripts/manage_alert_scopes.py migrate-to-management-group \
  --management-group-id "platform"
```

Use `--environment-name <name>` when the tenant has more than one central
deployment. Use `--json` for machine-readable output.

Add operations support `--what-if`. Remove and migration operations require
interactive confirmation. `--force` supplies that confirmation only for
preapproved noninteractive automation; it does not bypass tenant, permission,
coverage, ownership, or signed-test checks.

The command checks the exact Azure operations it needs before mutation. A
typical assignment is `Contributor` on each target subscription plus
`Monitoring Contributor` at the Management Group scope used by a Management
Group command. The operator also needs enough read access to enumerate the
Management Group and every managed subscription. The command fails closed when
it cannot prove membership or coverage.

Each new path is deployed disabled, tested through Azure Monitor's signed
Secure Webhook test, and enabled only after the test succeeds. The AZD-owned
baseline alert remains outside day-2 ownership.

## Operations

### Application Insights queries

The application configures the Azure Monitor OpenTelemetry Distro with
`APPLICATIONINSIGHTS_CONNECTION_STRING`. Useful starting queries for a
workspace-based Application Insights resource are:

```kusto
AppRequests
| where Name has "/api/service-health"
| summarize count(), percentile(DurationMs, 95) by ResultCode

AppTraces
| where Message has "Service Health"
| project TimeGenerated, SeverityLevel, Message,
    rootMessageTs = Properties.message_ts,
    threadReplyTs = Properties.thread_reply_ts

AppDependencies
| where Target has_any ("slack.com", "table.core.windows.net")
| summarize count(), failures=countif(Success == false) by Target, ResultCode
```

Alert on sustained webhook `503` responses, dependency failures, and missing
successful deliveries when an incident is expected.

### Update routing

Routing is revision configuration, not image content:

```bash
ROUTES_B64="$(base64 -w0 config/service_health_routes.json)"
azd env set SERVICE_HEALTH_ROUTES_JSON_B64 "$ROUTES_B64"
unset ROUTES_B64
azd provision
```

### Rotate the Slack token

Capture the replacement token with the hidden input procedure in deployment
step 4, update `SLACK_BOT_TOKEN`, and run `azd provision`. Bicep creates a Key
Vault secret version and updates the versioned Container Apps secret reference.

### Roll back application code

The Container App uses single revision mode. List revisions, then activate a
known good revision:

```bash
az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --query '[].{name:name,active:properties.active,created:properties.createdTime}' \
  -o table

az containerapp revision activate \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --revision "<known-good-revision>"
```

For routing or token rollback, restore the previous value and run
`azd provision`.

### Troubleshooting

| Symptom | Check |
|---|---|
| Browser login is blocked | Follow the tenant's Conditional Access policy. Check both explicit sign-ins unless AZD is configured to delegate authentication to Azure CLI. |
| Docker is unavailable from WSL | Run `docker context show` and `docker info`; confirm Docker Desktop uses Linux containers and enables WSL integration. |
| Secure Webhook setup is forbidden | Confirm the Azure CLI identity has the Microsoft Entra `Application Administrator` role. |
| Signed test returns `401` or `403` | Check Easy Auth audiences, the AzNS allowed application, and the app role assignment. |
| Signed test succeeds but Slack is empty | Check the bot token, `chat:write`, channel IDs, and bot membership. |
| Corrected webhook receives no calls | Wait 15 minutes after Azure Monitor exhausts webhook retries. |
| `/readyz` returns `503` | Check required environment values and Container App logs. |
| `/readyz` returns `200`, but delivery fails | Test Table access through a signed notification and inspect dependency telemetry. |
| Day-2 discovery is ambiguous | Pass `--environment-name` and verify the deployment tags. |
| A new day-2 alert stays disabled | Correct the signed Secure Webhook test failure, observe any retry cooldown, and rerun the idempotent add command. |

### Decommission

First inventory day-2 resources:

```bash
python scripts/manage_alert_scopes.py list --json
```

The day-2 remove commands preserve Service Health coverage and are not a
full-decommission switch. If you intend to end coverage, verify the
`service-health-managed-by=manage-alert-scopes` tags and manually delete only
the listed peripheral resource groups in their target subscriptions. Those
groups are outside the central AZD resource group and `azd down` does not remove
them.

Capture the protected API client ID, delete the central Azure resources, and
then delete the project-created app registration:

```bash
API_CLIENT_ID="$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID)"
azd down
az ad app delete --id "$API_CLIENT_ID"
unset API_CLIENT_ID
```

Do not delete Microsoft's AzNS AAD Webhook service principal.

This deployment enables Key Vault purge protection with a 90-day retention
period. The deleted vault cannot be purged or have its name reused until that
period expires. `azd down --purge` cannot override purge protection. If you need
to redeploy sooner, use a different AZD environment name or follow the Key Vault
recovery procedure and recreate the deleted integrations and role assignments.

## Tests

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m flake8 .
az bicep build --file infra/main.bicep --stdout
az bicep lint --file infra/main.bicep
az bicep build --file infra/day2/service-health-alert-scope.bicep --stdout
az bicep lint --file infra/day2/service-health-alert-scope.bicep
```

The test suite covers payload parsing, routing, authorization, Table Storage
coordination, Slack rendering and error classification, Flask routes, runtime
configuration, Secure Webhook setup, and day-2 scope management.

## Community and support

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change. Use
[SUPPORT.md](SUPPORT.md) for support boundaries. Report suspected
vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

This project is licensed under the [MIT License](LICENSE).
