# Deployment and operations runbook

This is the complete, validated implementation guide for the
[Azure Service Health Slack Bot](../README.md). It intentionally includes
production safeguards, recovery procedures, day-2 scope management, and
decommissioning steps that are not part of the short project overview.

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
| Confirm deployment acceptance | [End-to-end acceptance record](#end-to-end-acceptance-record) |
| Check the Microsoft platform evidence | [Microsoft platform evidence](microsoft-platform-evidence.md) |

## Architecture

The deployment uses Azure Container Apps, Azure Container Registry, a
user-assigned managed identity, Key Vault, an application Azure Table Storage
account, an isolated Blob Storage operation lock, Log Analytics, and
workspace-based Application Insights.

Azure Monitor matches Service Health events in a subscription Activity Log and
sends the Common Alert Schema payload to the public Container Apps endpoint.
Container Apps authentication validates the Microsoft Entra token. The
application then verifies the Azure Monitor caller application, token audience,
and `ActionGroupsSecureWebhook` app role before parsing the payload.

Key Vault and Table Storage have public network access disabled. The Container
Apps environment reaches both services through private endpoints and private
DNS. The webhook and health probes still use the Container App's public HTTPS
ingress.

![Example operator architecture map for the Azure Service Health event path, private data access, managed identity, and observability.](../img/architecture-flow.svg)

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
| Key Vault | Stores the Slack bot token and exposes the latest enabled version to Container Apps through a versionless reference. |
| Application Storage account and table | Stores incident state, Slack timestamps, ETags, and processing leases. |
| Isolated operation-lock Storage account and blob container | Serializes deployment and day-2 operations without mixing lock metadata with application data. |
| Private endpoints and DNS zones | Provide private Key Vault and Table Storage data paths. |
| Container Registry | Stores the application image. Registry admin access and anonymous pull are disabled. |
| Application Insights and Log Analytics | Receive application telemetry, Container Apps logs, and Key Vault/Table diagnostics. |

An independent operations Action Group receives webhook, dependency,
availability, and replica alerts. It must not depend only on this Slack bot.

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
For Maintenance events, `Planned` maps to `Active`; `InProgress` and
`Rescheduled` map to `Updated`; and `Complete`, `Canceled`, `Cancelled`, and
`Resolved` map to `Resolved`. `RCA` is also rendered as resolved because it is
a post-resolution communication.
Slack renders `Active` with a red circle, `Updated` with an orange circle, and
`Resolved` with a green circle. The Slack app avatar is separate from this
lifecycle indicator.

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
| Application Storage account | `Storage Table Data Contributor` | Read and write incident entities. |

The application uses `ManagedIdentityCredential` with `AZURE_CLIENT_ID` in
production and staging. It uses `DefaultAzureCredential` for local development.
Storage shared-key access is disabled.

### Network boundaries

Key Vault and Storage set `publicNetworkAccess` to `Disabled`. Each service has
a private endpoint and private DNS zone linked to the Container Apps VNet. The
Container Registry and Application Insights ingestion endpoints remain public
Azure endpoints.

When bootstrap runs outside the VNet, the secret lifecycle CLI opens only a
default-deny operator IPv4 `/32`, grants a temporary vault-scoped secret role,
writes through the Key Vault data plane, and restores both controls before
workload provisioning. An approved private operator path skips this temporary
window.

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
- Normal deployments use a versionless Key Vault secret URI. The secret
  lifecycle CLI validates a replacement before writing a new version and can
  temporarily pin a captured prior version for emergency rollback.
- New environments use an infrastructure-only provision followed by one hidden
  Key Vault data-plane transfer. The token is removed from local AZD state
  before workload provisioning; later preview, provision, routing, and rollback
  operations are token-free.
- The day-2 command implements a Management Group as one managed subscription
  alert path per accessible descendant. It does not deploy one native
  Management Group Activity Log Alert.
- The Tenant Root Group contains every subscription and Management Group in the
  tenant. A day-2 Management Group operation that includes the central
  subscription is rejected because it overlaps the immutable AZD-owned baseline
  alert. Use a non-overlapping child Management Group or manage additional
  subscriptions individually.
- The webhook reads at most the configured payload limit plus one byte, including
  chunked requests without `Content-Length`; the default accepted payload limit
  is 256 KiB.
- Azure Table string properties retain at most 32,000 UTF-16 code units. Slack
  rendering has its own smaller display limits, while correlation fingerprints
  continue to use the complete accepted event.
- The templates use Azure public cloud endpoint suffixes.

## Prerequisites

For deployment:

- Python 3.12 or 3.13 for local tooling; the production image is pinned to
  Python 3.13
- current stable [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
  with its managed Bicep CLI at `0.46.1` or later and the `log-analytics`
  extension
- current stable [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- Docker with a Linux container engine
- a dedicated Slack app with token rotation disabled, an `xoxb-` bot token, and
  `chat:write`
- an Azure subscription where you can create the resources in `infra/`
- `Owner`, or `Contributor` plus `User Access Administrator`, at the target
  subscription so Bicep can create resources and role assignments
- Microsoft Entra `Application Administrator` while the Secure Webhook hook runs

The operational CLIs have dependencies that are intentionally excluded from the
runtime image. Install both `requirements.txt` and `requirements-ops.txt` in the
same active virtual environment used to run `configure_secure_webhook.py`,
`manage_slack_token.py`, and `manage_alert_scopes.py`.

The documented Bash commands work in Linux, macOS where the command syntax is
available, and Ubuntu on WSL. On WSL, keep the repository in the Linux file
system, such as `~/src`, rather than a mounted Windows drive. Microsoft
documents that the DrvFS automount root is configurable and Linux permission
metadata is not enabled there by default, so a path-prefix check and
`chmod 600` alone are insufficient. Stage 4 checks the actual mount metadata
before reading the token. See
[Advanced settings configuration in WSL](https://learn.microsoft.com/windows/wsl/wsl-config#automount-settings)
and [File Permissions for WSL](https://learn.microsoft.com/windows/wsl/file-permissions).
Stage 1 pins the Azure subscription and AZD environment before registering the
Container Apps resource providers.

`Microsoft.ContainerService` is the Azure Kubernetes Service namespace and is
not a general Container Apps prerequisite for this deployment.

## Local development

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows, use Ubuntu on WSL and run the Bash commands above. Native Windows
shell commands are outside this repository's supported operational surface.

After activation, `python`, `python -m pip`, and the test commands use the
virtual environment. Activate it again in each new shell.

Copy `.env-example` to `.env`, then set:

- `SLACK_BOT_TOKEN`
- `AZURE_TABLE_ENDPOINT`, using
  `https://<account>.table.core.windows.net` with no path, query, credentials,
  or custom port
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

Use the stages in order. Each checkpoint is a stop/go decision. Do not continue
when a checkpoint fails, even if a later command appears able to run.

The examples use placeholders. Set them only in your local shell and AZD
environment. Do not add real tenant IDs, subscription IDs, user names, Slack
tokens, or channel IDs to tracked files.

### Stage 0: verify the workstation

Prerequisites:

- Bash on Linux, macOS, or Ubuntu on WSL
- Python 3.12 or 3.13
- current stable Azure CLI with its managed Bicep CLI `0.46.1` or later
- Azure CLI `log-analytics` extension
- current stable Azure Developer CLI
- Docker with a running Linux container engine
- Git

Run:

```bash
BICEP_MIN_VERSION="0.46.1"
BICEP_TESTED_VERSION="0.46.1"

python3 --version
az version
BICEP_FROM_PATH="$(az config get bicep.use_binary_from_path \
  --query value -o tsv 2>/dev/null)" || {
  echo "Could not verify the Azure CLI Bicep binary source; stop." >&2
  exit 1
}
test "$(printf '%s\n' "$BICEP_FROM_PATH" | \
  tr '[:upper:]' '[:lower:]')" = "false" || {
  echo "Azure CLI is configured to run bicep from PATH; stop." >&2
  exit 1
}
BICEP_VERSION_OUTPUT="$(az bicep version)" || {
  echo "Azure CLI-managed Bicep is not installed; stop at Stage 0." >&2
  exit 1
}
BICEP_VERSION="$(printf '%s\n' "$BICEP_VERSION_OUTPUT" | \
  sed -nE 's/^Bicep CLI version ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')"
test -n "$BICEP_VERSION" || {
  echo "Could not parse the Azure CLI-managed Bicep version; stop." >&2
  exit 1
}
python3 -c \
  'import sys; p=lambda value: tuple(map(int, value.split("."))); raise SystemExit(p(sys.argv[1]) < p(sys.argv[2]))' \
  "$BICEP_VERSION" "$BICEP_MIN_VERSION" || {
  echo "Bicep $BICEP_VERSION is below required $BICEP_MIN_VERSION; stop." >&2
  exit 1
}
printf 'Azure CLI-managed Bicep %s satisfies minimum %s (tested: %s).\n' \
  "$BICEP_VERSION" "$BICEP_MIN_VERSION" "$BICEP_TESTED_VERSION"
az bicep build --file infra/main.bicep --stdout > /dev/null || {
  echo "Central Bicep build failed; stop." >&2
  exit 1
}
az bicep lint --file infra/main.bicep || {
  echo "Central Bicep lint failed; stop." >&2
  exit 1
}
az bicep build \
  --file infra/day2/service-health-alert-scope.bicep --stdout > /dev/null || {
  echo "Day-2 Bicep build failed; stop." >&2
  exit 1
}
az bicep lint --file infra/day2/service-health-alert-scope.bicep || {
  echo "Day-2 Bicep lint failed; stop." >&2
  exit 1
}
az extension add --name log-analytics --upgrade --yes
az extension show --name log-analytics \
  --query '{name:name,version:version}' -o table
command -v azd
type -a azd
azd version
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker info --format 'os={{.OSType}}'
git --version
```

Expected state:

- Python reports `3.12.x` or `3.13.x`. The operator hook and CLI were verified
  with Ubuntu Python 3.12, while CI and the deployed container use Python 3.13.
- `az bicep version` reports the Azure CLI-managed Bicep CLI at `0.46.1` or
  later, `bicep.use_binary_from_path` is exactly `false`, and both repository
  template graphs build and lint with that exact installed version. `0.46.1` is
  the current repository-tested version. Passing the minimum check does not
  claim that every later Bicep release is compatible; the four build/lint
  commands are the compatibility gate for the installed version.
- Azure CLI reports the installed `log-analytics` extension and its version.
- `command -v azd` identifies the binary that the shell will execute. If
  `type -a azd` lists more than one installation, the first one must be the
  current stable installation.
- AZD reports `1.31.0` or a later stable version. The complete clean deployment,
  bootstrap, image deployment, signed Action Group test, and day-2 previews in
  this guide were exercised with AZD `1.31.0`.
- Every command exits with status `0`.
- Docker reports both a client and server version.
- Docker reports `os=linux`.

Checkpoint: stop until all six tools respond successfully. A Docker client
version without a server version is not sufficient.

Recovery:

- Install or update the tool from the links in [Prerequisites](#prerequisites).
- The repository templates use the Bicep null-forgiving operator in
  `infra/modules/container-app.bicep`. Azure CLI-managed Bicep `0.41.2`
  reproduced `BCP129` against the current graph; `0.46.1` built and linted both
  graphs. Treat `0.46.1` as the supported minimum, not merely the last version
  found on a workstation.
- If the binary-source check fails or returns anything except `false`, run
  `az config set bicep.use_binary_from_path=false`, recheck the setting, and
  rerun the complete Stage 0 block. Do not run a Bicep command before this gate
  passes.
- If `az bicep version` fails, install the repository-tested version with
  `az bicep install --version v0.46.1`, then rerun the complete Stage 0 block.
- If `az bicep version` reports less than `0.46.1`, run the idempotent
  `az bicep upgrade`, recheck `az bicep version`, and rerun all four build/lint
  commands in the Stage 0 block. Stop if the upgrade or any recheck fails.
- `az bicep` uses Azure CLI's self-contained Bicep installation. A standalone
  `bicep --version`, a VS Code extension version, or another deployment host's
  Bicep installation does not satisfy this checkpoint.
- `az bicep install` and `az bicep upgrade` require network access. For an
  offline or air-gapped workstation, follow Microsoft's
  [air-gapped installation procedure](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install#install-on-air-gapped-cloud)
  to obtain the exact platform asset from the official `0.46.1` release, verify
  its SHA-256 digest against the digest published with that release asset, and
  place it in Azure CLI's `.azure/bin` location. Then rerun the complete Stage 0
  block. If the reviewed binary or its published digest cannot be obtained,
  remain stopped; do not substitute an unrelated standalone executable.
- If an older AZD installation shadows the current one, put the current
  installation directory before the older directory in `PATH`, start a new
  shell, and repeat `command -v azd`, `type -a azd`, and `azd version`.
- On WSL, enable the distribution in Docker Desktop under **WSL integration**,
  then rerun `docker info`.
- If the Log Analytics extension cannot be installed, correct Azure CLI
  extension policy or network access before deployment; do not rely on an
  interactive dynamic-install prompt during incident troubleshooting.

### Stage 1: pin the Azure and AZD deployment target

Prerequisites:

- a local directory where the repository can be cloned; on WSL this must be in
  the Linux file system, not a DrvFS mount;
- the target tenant ID;
- the target subscription ID;
- a supported Azure region;
- an active `Owner` assignment, or `Contributor` plus
  `User Access Administrator`, on the target subscription;
- an active Microsoft Entra `Application Administrator` directory role for the
  operator who runs the Secure Webhook hook.

Choose a short environment name with lowercase letters, numbers, and hyphens.
Keep it between 3 and 20 characters and begin and end with a letter or number.

Clone the repository, then replace every angle-bracket placeholder:

```bash
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone https://github.com/ricmmartins/azure-service-health-slack-bot.git
cd azure-service-health-slack-bot

export TARGET_TENANT_ID="<tenant-id>"
export TARGET_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_LOCATION="<azure-region>"
export AZURE_ENV_NAME="<environment-name>"

azd config set auth.useAzCliAuth true
az login --tenant "$TARGET_TENANT_ID"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
azd auth login --check-status --no-prompt

if [[ -e ".azure/$AZURE_ENV_NAME" ]]; then
  echo "AZD environment already exists; stop and follow recovery." >&2
  exit 1
fi
if ! azd env new "$AZURE_ENV_NAME" \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --location "$AZURE_LOCATION" \
    --no-prompt; then
  echo "Could not create the isolated AZD environment." >&2
  exit 1
fi
azd env set AZURE_TENANT_ID "$TARGET_TENANT_ID" \
  -e "$AZURE_ENV_NAME" \
  --no-prompt
azd env select "$AZURE_ENV_NAME" --no-prompt

az account show \
  --query '{tenant:tenantId,subscription:id,name:name,isDefault:isDefault}' \
  -o table
azd auth status --no-prompt
azd env list -e "$AZURE_ENV_NAME" --no-prompt

OPERATOR_OBJECT_ID="$(az ad signed-in-user show --query id -o tsv)"
az role assignment list \
  --assignee "$OPERATOR_OBJECT_ID" \
  --scope "/subscriptions/$TARGET_SUBSCRIPTION_ID" \
  --include-groups \
  --include-inherited \
  --query '[].{role:roleDefinitionName,scope:scope}' \
  -o table
```

Expected state:

- `tenant` and `subscription` match the values you supplied.
- `isDefault` is `True`.
- AZD delegates authentication to the current Azure CLI identity and reports
  that identity as authenticated in the same tenant.
- AZD lists `AZURE_ENV_NAME` as the selected environment.
- The selected AZD environment contains the supplied tenant, subscription, and
  location. `azd env new` does not persist `AZURE_TENANT_ID`; the explicit
  nonsecret `azd env set` is required before the pre-infrastructure lifecycle
  status checkpoint.
- The role table shows the required active Azure role combination. Microsoft
  Entra directory roles do not appear in this Azure RBAC table, so confirm the
  active `Application Administrator` role separately in the Entra admin center.

Verify both tools point to the same target before making any subscription or
directory change:

```bash
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME" &&
  test "$(azd env get-value AZURE_TENANT_ID \
    -e "$AZURE_ENV_NAME" --no-prompt | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$AZURE_ENV_NAME" --no-prompt | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_LOCATION \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_LOCATION"
); then
  echo "Azure CLI and AZD target confirmation failed." >&2
  exit 1
fi
echo "deployment-target-pinned"
```

Proceed only when the final line is `deployment-target-pinned`. Register and
check the Container Apps providers only after this checkpoint:

```bash
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.OperationalInsights --wait
az provider show --namespace Microsoft.App \
  --query '{namespace:namespace,state:registrationState}' -o table
az provider show --namespace Microsoft.OperationalInsights \
  --query '{namespace:namespace,state:registrationState}' -o table
```

Checkpoint:

```bash
test "$(az provider show --namespace Microsoft.App \
  --query registrationState -o tsv)" = "Registered" &&
test "$(az provider show --namespace Microsoft.OperationalInsights \
  --query registrationState -o tsv)" = "Registered" &&
echo "azure-context-ready"
```

Proceed only when the final line is `azure-context-ready`.

Check the regional capacity consumed by this template. These provider usage
endpoints are used directly because the `az quota` extension can temporarily
return an empty result or a stale `Microsoft.Quota` registration error even
after the provider reports `Registered`:

```bash
check_capacity() {
  local provider="$1"
  local api_version="$2"
  local quota_name="$3"
  local required="$4"
  local usage_json

  if ! usage_json="$(
    az rest --method get --url \
      "https://management.azure.com/subscriptions/$TARGET_SUBSCRIPTION_ID/providers/$provider/locations/$AZURE_LOCATION/usages?api-version=$api_version" \
      --query "value[?name.value=='$quota_name'] | [0].{current:currentValue,limit:limit}" \
      -o json
  )"; then
    echo "Could not query $quota_name capacity." >&2
    return 1
  fi
  if ! QUOTA_NAME="$quota_name" REQUIRED="$required" \
    USAGE_JSON="$usage_json" python3 - <<'PY'
import json
import os
import sys

name = os.environ["QUOTA_NAME"]
required = float(os.environ["REQUIRED"])
try:
    value = json.loads(os.environ["USAGE_JSON"])
    current = float(value["current"])
    limit = float(value["limit"])
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
    print(f"Could not parse {name} capacity: {error}", file=sys.stderr)
    raise SystemExit(1)
if limit - current < required:
    print(
        f"{name} has insufficient capacity: "
        f"current={current:g} limit={limit:g} required={required:g}.",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(
    f"{name} current={current:g} limit={limit:g} required={required:g}"
)
PY
  then
    return 1
  fi
}

check_capacity Microsoft.App 2024-03-01 \
  ManagedEnvironmentCount 1 &&
check_capacity Microsoft.Storage 2024-01-01 \
  StorageAccounts 2 &&
check_capacity Microsoft.Network 2024-05-01 \
  VirtualNetworks 1 &&
check_capacity Microsoft.Network 2024-05-01 \
  PrivateEndpoints 2 &&
echo "deployment-capacity-ready"
```

Proceed only when all four usage rows print and the final line is
`deployment-capacity-ready`. This is a minimum check for the quota-governed
resources created by the current template: one application Storage account and
one isolated operation-lock Storage account, one virtual network, two private
endpoints, and one Container Apps managed environment. It is not a guarantee
that Azure has transient regional capacity for every resource at deployment
time.

List policy assignments visible from the target subscription boundary,
including inherited and child-scope assignments:

```bash
az policy assignment list \
  --scope "/subscriptions/$TARGET_SUBSCRIPTION_ID" \
  --disable-scope-strict-match true \
  --query '[].{name:name,scope:scope,enforcementMode:enforcementMode}' \
  -o table
```

Review assignments at the subscription or an ancestor scope for restrictions
on regions, public ingress, private endpoints, resource names, SKUs, and
required tags. Child-scope rows matter only if the deployment targets that
scope. An empty table means no assignment was found by this query; a nonempty
table is not automatically a blocker, but it must be assessed before the Entra
hook or provisioning.

Recovery:

- If the account values are wrong, rerun
  `az login --tenant "$TARGET_TENANT_ID"` and
  `az account set --subscription "$TARGET_SUBSCRIPTION_ID"`; do not rely on a
  previous shell context.
- If Azure CLI authentication expires, rerun
  `az login --tenant "$TARGET_TENANT_ID"`, set the subscription again, and run
  `azd auth login --check-status --no-prompt`. Azure CLI and a separately
  authenticated AZD session can expire independently; delegation avoids
  maintaining two interactive identities.
- If the environment-name guard reports that the environment already exists,
  inspect only
  its nonsecret `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION`,
  and `AZURE_RESOURCE_GROUP` values with separate `azd env get-value` commands.
  Do not run `azd env get-values`, because it can print the stored Slack token.
  If the existing environment has deployment outputs or belongs to another
  target, preserve it and choose a new environment name. Never repin a
  previously deployed environment to a different subscription.
  Only when all nonsecret outputs prove that the environment is unused may you
  remove it with
  `azd env remove "$AZURE_ENV_NAME" -e "$AZURE_ENV_NAME" --force --no-prompt`,
  then rerun stage 1 to create it from scratch.
- If a role is eligible through Privileged Identity Management, activate it and
  rerun the role query.
- If provider registration is forbidden, ask a subscription administrator to
  register the namespaces. Do not substitute `Microsoft.ContainerService`.
- If a provider usage request fails, confirm the corresponding
  `Microsoft.App`, `Microsoft.Storage`, or `Microsoft.Network` namespace is
  registered, wait for propagation, and repeat the direct usage request.
- On WSL, if the repository was cloned on a Windows drive, remove any local
  secret data from that copy and clone again under `~/src` before stage 4.

### Stage 2: create and authorize the Slack app

Prerequisites:

- permission to create or approve an app in the target Slack workspace;
- one destination channel ID for the fallback route;
- any additional destination channel IDs needed by routing rules.

Use a dedicated Slack app for this deployment. Reusing an app couples its
credential lifecycle and channel access to every other consumer.

In <https://api.slack.com/apps>:

1. Create a dedicated Slack app in the target workspace.
2. Under **OAuth & Permissions**, add the bot scope `chat:write`.
3. In **OAuth & Permissions**, confirm token rotation is disabled. Do not enable
   it for this application.
4. Install or reinstall the app to the workspace.
5. Store the resulting `xoxb-` bot token in an approved password manager.
6. Invite the bot to every destination channel.
7. Open each channel's details and record its channel ID.

Expected state: you have one `xoxb-` token and every configured channel shows
the bot as a member. Token rotation is disabled. The service does not need a
signing secret, event subscription, slash command, or inbound app manifest.

Slack's [token rotation documentation](https://docs.slack.dev/authentication/using-token-rotation/)
states that rotating access tokens expire every 12 hours and must be refreshed
with a refresh token. This runtime stores one static `xoxb-` value and has no
OAuth client-secret or refresh-token flow. It does not support rotating
`xoxe.xoxb-*` access tokens.

Checkpoint: do not continue with only channel names. Routing requires channel
IDs, the bot must already be a channel member when using only `chat:write`, and
token rotation must be disabled. If an existing app has token rotation enabled,
stop and create a dedicated app without it. Slack states that token rotation
cannot be disabled after it is enabled.

Recovery:

- If app installation is blocked, request workspace administrator approval.
- If a channel is private, have a member invite the bot.
- If you changed scopes after installation, reinstall the app before using the
  new token.
- If the app is shared, record every owner and consumer before proceeding.
  Prefer replacing it with a dedicated app for this deployment.

### Stage 3: install dependencies and validate the routing file

Prerequisites: stages 0 through 2 are complete.

Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-ops.txt
python -c \
  'from azure.keyvault.secrets import SecretClient; from azure.storage.blob import BlobServiceClient; print("operations-sdk-ready")'

cp config/service_health_routes.example.json \
  config/service_health_routes.json
vi config/service_health_routes.json
```

Replace every synthetic channel and subscription ID. Keep
`default_channel_id`; it is required. Then validate both JSON syntax and the
application routing contract:

```bash
python -m json.tool config/service_health_routes.json >/dev/null &&
python -c 'import json; from pathlib import Path; from service_health.routing import RoutingConfig; RoutingConfig.from_dict(json.loads(Path("config/service_health_routes.json").read_text())); print("routing-valid")' &&
! grep -Eq \
  'C0123456789|C1111111111|C2222222222|00000000-0000-0000-0000-000000000000' \
  config/service_health_routes.json &&
echo "routing-checkpoint-passed"
```

Expected state: the parser prints `routing-valid`, the placeholder scan is
silent, the SDK import prints `operations-sdk-ready`, and the final line is
`routing-checkpoint-passed`.

Checkpoint: inspect the file once more and confirm that its subscription IDs
belong to the intended tenant and its channel IDs belong to the intended Slack
workspace.

Recovery:

- A `JSONDecodeError` identifies malformed JSON. Correct the indicated line and
  rerun both validators.
- `InvalidRoutingConfiguration` identifies a missing channel, invalid rule, or
  invalid priority.
- If the placeholder scan stops the command before the final line, replace the
  remaining example value.

### Stage 4: load nonsecret inputs

Prerequisites:

- the shell variables from stage 1 are still set;
- the terminal is in the repository root;
- the validated routing file from stage 3 exists;
- `requirements-ops.txt` is installed in the active virtual environment.

Production also requires an independent operations Action Group. Prefer a
dedicated operations resource group that has its own lifecycle and owners. It
must not be the bot Secure Webhook Action Group
`ag-${AZURE_ENV_NAME}-service-health`, because that path is one of the
components the operations alerts must monitor.

The minimum new-adopter receiver below is an approved on-call email mailbox.
Replace every placeholder in the current shell only. Use a role mailbox rather
than a personal address when policy permits, do not enable shell tracing, and
do not paste the real address into the repository or shared command output:

```bash
export OPERATIONS_RESOURCE_GROUP="<separate-operations-resource-group>"
export OPERATIONS_LOCATION="<azure-region>"
export OPERATIONS_ACTION_GROUP_NAME="<operations-action-group-name>"
export OPERATIONS_ACTION_GROUP_SHORT_NAME="<short-display-name>"
export OPERATIONS_EMAIL_ADDRESS="<approved-on-call-email-address>"
readonly OPERATIONS_EMAIL_RECEIVER_NAME="primary-on-call-email"

export OPERATIONS_PRIMARY_OWNER="<primary-owner-role-or-alias>"
export OPERATIONS_BACKUP_OWNER="<different-backup-owner-role-or-alias>"
export OPERATIONS_ON_CALL_DESTINATION="email-receiver:primary-on-call-email"
export OPERATIONS_RUNBOOK_URI="https://<approved-runbook>"
```

Fail closed before creating anything. The selected AZD environment, its
explicit name, Azure CLI tenant/subscription, and the proposed independent
resource group must all match the intended target:

```bash
: "${AZURE_ENV_NAME:?Set the explicit AZD environment name from stage 1.}"
: "${TARGET_TENANT_ID:?Set the target tenant from stage 1.}"
: "${TARGET_SUBSCRIPTION_ID:?Set the target subscription from stage 1.}"
: "${OPERATIONS_RESOURCE_GROUP:?Set a separate operations resource group.}"
: "${OPERATIONS_LOCATION:?Set the operations resource-group region.}"
: "${OPERATIONS_ACTION_GROUP_NAME:?Set the operations Action Group name.}"
: "${OPERATIONS_ACTION_GROUP_SHORT_NAME:?Set the short display name.}"
: "${OPERATIONS_EMAIL_ADDRESS:?Set the approved receiver address.}"
: "${OPERATIONS_PRIMARY_OWNER:?Set the primary owner evidence.}"
: "${OPERATIONS_BACKUP_OWNER:?Set the backup owner evidence.}"
: "${OPERATIONS_ON_CALL_DESTINATION:?Set the on-call receiver alias.}"
: "${OPERATIONS_RUNBOOK_URI:?Set the HTTPS runbook URI.}"

if [[ "$OPERATIONS_RESOURCE_GROUP" == *'<'* ||
      "$OPERATIONS_LOCATION" == *'<'* ||
      "$OPERATIONS_ACTION_GROUP_NAME" == *'<'* ||
      "$OPERATIONS_ACTION_GROUP_SHORT_NAME" == *'<'* ||
      "$OPERATIONS_EMAIL_ADDRESS" == *'<'* ||
      "$OPERATIONS_PRIMARY_OWNER" == *'<'* ||
      "$OPERATIONS_BACKUP_OWNER" == *'<'* ||
      "$OPERATIONS_RUNBOOK_URI" == *'<'* ]]; then
  echo "Replace every operations placeholder before continuing." >&2
  exit 1
fi
if ! (
  [[ "$OPERATIONS_RESOURCE_GROUP" =~ ^[A-Za-z0-9._-]+$ ]] &&
  [[ "$OPERATIONS_ACTION_GROUP_NAME" =~ ^[A-Za-z0-9._-]+$ ]] &&
  [[ "$OPERATIONS_ACTION_GROUP_SHORT_NAME" =~ ^[A-Za-z0-9_-]{1,12}$ ]]
); then
  echo "Operations resource names contain unsupported characters or length." >&2
  exit 1
fi

SELECTED_AZD_ENV_NAME="$(
  azd env get-value AZURE_ENV_NAME --no-prompt
)"
BOT_RESOURCE_GROUP="rg-$AZURE_ENV_NAME"
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$SELECTED_AZD_ENV_NAME" = "$AZURE_ENV_NAME" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME" &&
  test "${OPERATIONS_RESOURCE_GROUP,,}" != "${BOT_RESOURCE_GROUP,,}" &&
  test "$OPERATIONS_PRIMARY_OWNER" != "$OPERATIONS_BACKUP_OWNER" &&
  [[ "$OPERATIONS_RUNBOOK_URI" == https://* ]]
); then
  echo "Independent operations target confirmation failed." >&2
  exit 1
fi
echo "operations-action-group-target-confirmed"
```

Stop unless the final line is
`operations-action-group-target-confirmed`. Reusing the central bot resource
group is rejected so a later bot decommission does not silently remove the
independent receiver. An existing operations resource group is acceptable, but
the Action Group name must be new; this procedure refuses to overwrite an
existing receiver configuration. Hold an exclusive approved change window for
this exact resource group and Action Group name until post-create verification
finishes:

```bash
if ! OPERATIONS_GROUP_EXISTS="$(
  az group exists \
    --name "$OPERATIONS_RESOURCE_GROUP" \
    --subscription "$TARGET_SUBSCRIPTION_ID"
)"; then
  echo "Could not verify the operations resource group." >&2
  exit 1
fi
case "$OPERATIONS_GROUP_EXISTS" in
  true)
    ;;
  false)
    az group create \
      --name "$OPERATIONS_RESOURCE_GROUP" \
      --location "$OPERATIONS_LOCATION" \
      --subscription "$TARGET_SUBSCRIPTION_ID" \
      --only-show-errors \
      --output none
    ;;
  *)
    echo "Unexpected resource-group existence result." >&2
    exit 1
    ;;
esac

if ! EXISTING_OPERATIONS_ACTION_GROUPS_JSON="$(
  az monitor action-group list \
    --resource-group "$OPERATIONS_RESOURCE_GROUP" \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --only-show-errors \
    -o json
)"; then
  echo "Could not inspect existing operations Action Groups." >&2
  exit 1
fi
if ! EXISTING_OPERATIONS_ACTION_GROUPS_JSON="$EXISTING_OPERATIONS_ACTION_GROUPS_JSON" \
    OPERATIONS_ACTION_GROUP_NAME="$OPERATIONS_ACTION_GROUP_NAME" \
    python3 - <<'PY'
import json
import os

expected = os.environ["OPERATIONS_ACTION_GROUP_NAME"].casefold()
groups = json.loads(os.environ["EXISTING_OPERATIONS_ACTION_GROUPS_JSON"])
raise SystemExit(
    1 if any(str(group.get("name", "")).casefold() == expected
             for group in groups) else 0
)
PY
then
  echo "Action Group already exists; inspect it and choose a new name." >&2
  exit 1
fi

az monitor action-group create \
  --resource-group "$OPERATIONS_RESOURCE_GROUP" \
  --name "$OPERATIONS_ACTION_GROUP_NAME" \
  --short-name "$OPERATIONS_ACTION_GROUP_SHORT_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location Global \
  --enabled true \
  --action email "$OPERATIONS_EMAIL_RECEIVER_NAME" \
    "$OPERATIONS_EMAIL_ADDRESS" usecommonalertschema \
  --only-show-errors \
  --output none

OPERATIONS_ACTION_GROUP_ID="$(
  az monitor action-group show \
    --resource-group "$OPERATIONS_RESOURCE_GROUP" \
    --name "$OPERATIONS_ACTION_GROUP_NAME" \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --query id -o tsv
)"
BOT_ACTION_GROUP_ID="/subscriptions/$TARGET_SUBSCRIPTION_ID/resourceGroups/rg-$AZURE_ENV_NAME/providers/Microsoft.Insights/actionGroups/ag-$AZURE_ENV_NAME-service-health"

verify_operations_action_group() {
  local action_group_json
  if ! action_group_json="$(
    az monitor action-group show \
    --resource-group "$OPERATIONS_RESOURCE_GROUP" \
    --name "$OPERATIONS_ACTION_GROUP_NAME" \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --only-show-errors \
    -o json
  )"; then
    return 1
  fi
  ACTION_GROUP_JSON="$action_group_json" \
  EXPECTED_ACTION_GROUP_ID="$OPERATIONS_ACTION_GROUP_ID" \
  EXPECTED_EMAIL_ADDRESS="$OPERATIONS_EMAIL_ADDRESS" \
  EXPECTED_EMAIL_RECEIVER_NAME="$OPERATIONS_EMAIL_RECEIVER_NAME" \
  python3 - <<'PY'
import json
import os

data = json.loads(os.environ["ACTION_GROUP_JSON"])
receivers = data.get("emailReceivers") or []
other_receiver_keys = (
    "armRoleReceivers",
    "automationRunbookReceivers",
    "azureAppPushReceivers",
    "azureFunctionReceivers",
    "eventHubReceivers",
    "incidentReceivers",
    "itsmReceivers",
    "logicAppReceivers",
    "smsReceivers",
    "voiceReceivers",
    "webhookReceivers",
)
valid_receivers = [
    receiver for receiver in receivers
    if str(receiver.get("name", "")).casefold()
       == os.environ["EXPECTED_EMAIL_RECEIVER_NAME"].casefold()
    and str(receiver.get("emailAddress", "")).casefold()
       == os.environ["EXPECTED_EMAIL_ADDRESS"].casefold()
    and receiver.get("useCommonAlertSchema") is True
]
valid = (
    str(data.get("id", "")).casefold()
    == os.environ["EXPECTED_ACTION_GROUP_ID"].casefold()
    and str(data.get("location", "")).casefold() == "global"
    and data.get("enabled") is True
    and len(receivers) == 1
    and len(valid_receivers) == 1
    and all(not (data.get(key) or []) for key in other_receiver_keys)
)
raise SystemExit(0 if valid else 1)
PY
}

if ! (
  test -n "$OPERATIONS_ACTION_GROUP_ID" &&
  test "${OPERATIONS_ACTION_GROUP_ID,,}" != "${BOT_ACTION_GROUP_ID,,}" &&
  verify_operations_action_group
); then
  echo "Independent operations Action Group verification failed." >&2
  exit 1
fi
echo "operations-action-group-created"
```

Azure sends a one-time-passcode request for a new email receiver. Microsoft
documents that an unverified receiver cannot receive alerts or test
notifications after enforcement is active. Have the receiver owner complete
that verification, then send a real Action Group test to the configured
receiver. The CLI requires the receiver definition even though the saved Action
Group already contains it:

```bash
if ! az monitor action-group test-notifications create \
    --resource-group "$OPERATIONS_RESOURCE_GROUP" \
    --action-group-name "$OPERATIONS_ACTION_GROUP_NAME" \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --alert-type servicehealth \
    --add-action email "$OPERATIONS_EMAIL_RECEIVER_NAME" \
      "$OPERATIONS_EMAIL_ADDRESS" usecommonalertschema \
    --only-show-errors \
    --output none; then
  echo "Operations receiver test failed; do not record readiness evidence." >&2
  exit 1
fi
OPERATIONS_RECEIVER_TEST_SENT_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

read -r -p \
  "Type RECEIVED only after the intended on-call receiver confirms the test: " \
  OPERATIONS_RECEIVER_CONFIRMATION
if [[ "$OPERATIONS_RECEIVER_CONFIRMATION" != "RECEIVED" ]]; then
  echo "Receiver delivery is unconfirmed; production readiness is blocked." >&2
  exit 1
fi
if ! verify_operations_action_group; then
  echo "Saved receiver changed or failed verification after the test." >&2
  exit 1
fi

OPERATIONS_RECEIVER_TESTED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
if ! OPERATIONS_RECEIVER_TEST_SENT_AT="$OPERATIONS_RECEIVER_TEST_SENT_AT" \
    OPERATIONS_RECEIVER_TESTED_AT="$OPERATIONS_RECEIVER_TESTED_AT" \
    python3 - <<'PY'
import os
from datetime import datetime

sent = datetime.fromisoformat(
    os.environ["OPERATIONS_RECEIVER_TEST_SENT_AT"].replace("Z", "+00:00")
)
confirmed = datetime.fromisoformat(
    os.environ["OPERATIONS_RECEIVER_TESTED_AT"].replace("Z", "+00:00")
)
elapsed = (confirmed - sent).total_seconds()
raise SystemExit(0 if 0 <= elapsed <= 900 else 1)
PY
then
  echo "Receiver confirmation exceeded 15 minutes; run a fresh test." >&2
  exit 1
fi
OPERATIONS_RECEIVER_TEST_EVIDENCE="$(
  OPERATIONS_ACTION_GROUP_ID="$OPERATIONS_ACTION_GROUP_ID" \
  OPERATIONS_RECEIVER_TESTED_AT="$OPERATIONS_RECEIVER_TESTED_AT" \
  python3 - <<'PY'
import json
import os

print(json.dumps({
    "status": "Succeeded",
    "testedAt": os.environ["OPERATIONS_RECEIVER_TESTED_AT"],
    "actionGroupId": os.environ["OPERATIONS_ACTION_GROUP_ID"],
}, separators=(",", ":")))
PY
)"
```

This synthetic test proves that Azure Monitor dispatched the selected sample
through this Action Group and that the intended receiver observed it. It does
not prove that the bot, Slack, application alerts, or a real Azure incident work
end to end. Keep the successful CLI result, UTC confirmation timestamp, receiver
alias, owners, and runbook in the approved operational record; do not store the
mailbox address in repository defaults.

Email is the minimum procedure, not the only supported receiver. Azure Monitor
also supports SMS, voice, Azure app push, ARM-role email, Logic Apps, Azure
Functions, ITSM, and webhooks subject to receiver-specific prerequisites and
regional/service limits. Use an independently operated destination, enable
Common Alert Schema separately on each receiver type that supports it, and
perform an actual delivery test for every receiver relied on for on-call
coverage.

If the receiver does not observe the test, do not create evidence. Verify email
OTP status, the documented Azure Monitor sender allowlist, spam filtering,
receiver configuration, and Action Group enabled state; save any correction,
then rerun one test. Do not retry in a tight loop because Action Group tests are
rate limited. Evidence must describe the configured Action Group, must not be future-dated,
must follow the test by no more than 15 minutes, and must be refreshed after
receiver changes and at least every 90 days.

Load only routing and operational settings into AZD:

```bash
ROUTES_B64="$(
  python -c 'import base64, pathlib; print(base64.b64encode(pathlib.Path("config/service_health_routes.json").read_bytes()).decode())'
)"
azd env set SERVICE_HEALTH_ROUTES_JSON_B64 "$ROUTES_B64" \
  -e "$AZURE_ENV_NAME" --no-prompt
unset ROUTES_B64

azd env set SERVICE_HEALTH_DEPLOY_WORKLOAD false \
  -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_BASELINE_ALERT_ENABLED false \
  -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_SECRET_VERSION "" \
  -e "$AZURE_ENV_NAME" --no-prompt
```

For production, set `SERVICE_HEALTH_ENVIRONMENT_CLASS=production` and configure
all readiness metadata from the confirmed test before bootstrap can enable the
workload:

```bash
azd env set SERVICE_HEALTH_ENVIRONMENT_CLASS production -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID \
  "$OPERATIONS_ACTION_GROUP_ID" -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_OPERATIONS_PRIMARY_OWNER \
  "$OPERATIONS_PRIMARY_OWNER" -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_OPERATIONS_BACKUP_OWNER \
  "$OPERATIONS_BACKUP_OWNER" -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_OPERATIONS_ON_CALL_DESTINATION \
  "$OPERATIONS_ON_CALL_DESTINATION" -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_OPERATIONS_RUNBOOK_URI \
  "$OPERATIONS_RUNBOOK_URI" -e "$AZURE_ENV_NAME" --no-prompt
azd env set SERVICE_HEALTH_OPERATIONS_RECEIVER_TEST_EVIDENCE \
  "$OPERATIONS_RECEIVER_TEST_EVIDENCE" -e "$AZURE_ENV_NAME" --no-prompt

unset OPERATIONS_EMAIL_ADDRESS OPERATIONS_RECEIVER_CONFIRMATION
```

Record receiver evidence only after a real test reaches the independent
destination. Evidence is rejected when it targets another Action Group, is
future-dated, or is older than 90 days. These readiness values are operational
gates, not globally required Bicep parameters, so existing nonproduction
environments remain backward compatible.

Do not set `SLACK_BOT_TOKEN` with `azd env set`, a process environment variable,
or a template parameter. `scripts/manage_slack_token.py` is the only supported
production secret-transfer path. It reads new credentials through a hidden
prompt and talks directly to the Key Vault data plane.

The routing document is base64 encoded because AZD substitutes parameter values
into `infra/main.parameters.json` before JSON parsing. Bicep decodes the value
before setting the container's plain JSON
`SERVICE_HEALTH_ROUTES_JSON` environment variable.

Checkpoint:

```bash
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
```

For a new environment before the phase-one infrastructure provision, the
command returns the following structured invariants. It does not emit a
lifecycle-state string:

<!-- status-contract:pre-infrastructure -->
```json
{
  "Environment": "<selected AZD environment>",
  "KeyVaultName": null,
  "SecretVersion": "",
  "LatestSecretVersion": "",
  "PreviousSecretVersion": "",
  "LegacyTokenPresent": false,
  "MigrationMarkerSet": false,
  "Bootstrapped": false
}
```

`Environment` must name the selected environment. Before resources exist,
`KeyVaultName` is `null`; all three version fields are empty; and all four
boolean fields are `false`. For an existing legacy environment,
`LegacyTokenPresent` is `true` and `Bootstrapped` is `false`; stop ordinary
preview/provision and follow
[Migrate an existing deployment](#migrate-an-existing-deployment).
The status object is diagnostic and does not contain tenant, subscription, or
resource-group identity, so it must never authorize a mutating lifecycle
command. Stage 6 performs a separate target and uniqueness gate before
bootstrap.

### Stage 5: reconcile Microsoft Entra and preview Azure changes

Prerequisites:

- the stage 4 nonsecret-input checkpoint passed;
- `Application Administrator` is active for the Azure CLI user;
- the operator can create enterprise applications in the target tenant.

The hook reads the active Azure CLI account through `az account show` and can
change Microsoft Entra. Set and verify the Azure CLI subscription again, then
reselect the AZD environment before running it:

```bash
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
azd env select "$AZURE_ENV_NAME" --no-prompt
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME" &&
  test "$(azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$AZURE_ENV_NAME" --no-prompt | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_LOCATION \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_LOCATION"
); then
  echo "Entra mutation target confirmation failed." >&2
  exit 1
fi
echo "entra-mutation-target-confirmed"
```

Do not run the hook unless the final line is
`entra-mutation-target-confirmed`. Run the project hook explicitly:

```bash
azd hooks run preprovision \
  -e "$AZURE_ENV_NAME" \
  --no-prompt
```

Expected state: the hook exits with status `0` after creating or reconciling the
protected API application, service principal, identifier URI,
`ActionGroupsSecureWebhook` role, verified owners, and AzNS role assignment.
This stage changes Microsoft Entra, but does not provision the Azure resources.
Run only one mutating hook for an environment at a time. Its immutable,
environment-derived Microsoft Graph application key reconciles a simultaneous
first-create race without retaining two protected API applications, but the
later owner, role, service-principal, and AZD writes are not one atomic
transaction and therefore remain intentionally single-operator.

Check the nonsecret hook outputs without printing the local Slack token:

```bash
test -n "$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)" &&
test -n "$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)" &&
test -n "$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI \
  -e "$AZURE_ENV_NAME" --no-prompt)" &&
echo "entra-hook-ready"
```

Preview the Azure Resource Manager changes:

```bash
: "${AZURE_ENV_NAME:?Set the explicit AZD environment name from stage 1.}"
SELECTED_AZD_ENV_NAME="$(
  azd env get-value AZURE_ENV_NAME --no-prompt
)"
if ! (
  test "$SELECTED_AZD_ENV_NAME" = "$AZURE_ENV_NAME" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME"
); then
  echo "Selected AZD environment does not match AZURE_ENV_NAME." >&2
  exit 1
fi
echo "preview-environment-confirmed"

if ! SERVICE_HEALTH_READ_ONLY_PREVIEW=true \
    azd hooks run preprovision \
      --no-prompt \
      --environment "$AZURE_ENV_NAME"; then
  echo "Read-only hook validation failed; preview is blocked." >&2
  exit 1
fi
SERVICE_HEALTH_READ_ONLY_PREVIEW=true \
  azd provision \
    --preview \
    --no-prompt \
    --subscription "$TARGET_SUBSCRIPTION_ID" \
    --location "$AZURE_LOCATION" \
    --environment "$AZURE_ENV_NAME"
```

This procedure requires the adopter to name the current environment explicitly
and proves that the selected AZD environment is the same environment before
either command runs. Never preview one environment before provisioning another.

The explicit hook command guarantees the validation runs even on an AZD version
that does not invoke lifecycle hooks for preview. The same opt-in on the exact
preview command guarantees any preview-triggered hook invocation remains
nonmutating. The hook reads only
`.azure/$AZURE_ENV_NAME/.env`, refuses any Slack-token entry,
validates the persisted nonsecret deployment and Secure Webhook identifiers,
and permits only `az account show` to confirm the exact tenant and subscription.
It exits before AZD environment writes, Microsoft Graph changes, ARM/RBAC,
Key Vault, or Slack access. Any value other than the explicit `true` opt-in is
rejected; omitting the variable preserves normal provision behavior.

Expected state: the nonmutating hook validation and IaC preview exit with status
`0`, show planned creates for the central resources, and show no unexpected
deletes or changes outside the selected subscription.

The named AZD environment was created with an explicit subscription and
location. The hook proves those persisted local values match the active
read-only account result before the preview continues.

`azd up` runs the hook, provisioning, packaging, and deployment lifecycle. A
verified deployment used it successfully after the explicit hook and reviewed
preview. This guide deliberately keeps provisioning and deployment as separate
stages so their checkpoints remain independent; do not use `azd up` to bypass
stages 4 and 5.

Checkpoint: proceed only after `entra-mutation-target-confirmed` and
`entra-hook-ready` appear and a human has reviewed the complete preview. The
preview for a new environment must show infrastructure only: no Container App,
Action Group, or baseline alert. Save
the preview in an approved operational record if your change process requires
it, but redact local paths and IDs before sharing.

Microsoft documents the preview flag and independent hook execution. It does
not document a guarantee about whether preview invokes lifecycle hooks, so the
explicit nonmutating hook command immediately before preview is mandatory.

Recovery:

- A directory authorization error usually means `Application Administrator` is
  inactive or the Azure CLI signed into the wrong tenant. Correct the context
  and rerun the hook; it is designed to reconcile existing objects.
- If the hook reports conflicting persisted application IDs, stop and inspect
  the named app registration. Do not delete an existing application to force a
  clean run.
- If preview shows a delete or the wrong subscription, stop, correct the AZD
  environment binding, and rerun preview.

### Stage 6: provision infrastructure and transfer the Slack token

Prerequisites: the infrastructure-only stage 5 preview was reviewed and
approved.

Run:

```bash
azd provision \
  -e "$AZURE_ENV_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" \
  --no-prompt

EXPECTED_RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP \
  -e "$AZURE_ENV_NAME" --no-prompt)" || {
  echo "Could not read the provisioned resource group; bootstrap is blocked." >&2
  exit 1
}
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = \
    "$(printf '%s\n' "$TARGET_TENANT_ID" | tr '[:upper:]' '[:lower:]')" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = \
    "$(printf '%s\n' "$TARGET_SUBSCRIPTION_ID" | \
      tr '[:upper:]' '[:lower:]')" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME" &&
  test "$(azd env get-value AZURE_TENANT_ID \
    -e "$AZURE_ENV_NAME" --no-prompt | \
      tr '[:upper:]' '[:lower:]')" = \
    "$(printf '%s\n' "$TARGET_TENANT_ID" | tr '[:upper:]' '[:lower:]')" &&
  test "$(azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$AZURE_ENV_NAME" --no-prompt | \
      tr '[:upper:]' '[:lower:]')" = \
    "$(printf '%s\n' "$TARGET_SUBSCRIPTION_ID" | \
      tr '[:upper:]' '[:lower:]')" &&
  test -n "$EXPECTED_RESOURCE_GROUP"
); then
  echo "Bootstrap target confirmation failed." >&2
  exit 1
fi

TARGET_TENANT_ID_LC="$(printf '%s\n' "$TARGET_TENANT_ID" | \
  tr '[:upper:]' '[:lower:]')"
if ! ENABLED_SUBSCRIPTIONS="$(
  az account list \
    --query "[?state=='Enabled' && tenantId=='$TARGET_TENANT_ID_LC'].id" \
    -o tsv
)"; then
  echo "Could not enumerate enabled subscriptions; bootstrap is blocked." >&2
  exit 1
fi
MATCHED_CENTRAL_TARGETS=""
for subscription_id in $ENABLED_SUBSCRIPTIONS; do
  if ! groups_json="$(
    az group list \
      --subscription "$subscription_id" \
      --tag workload=azure-service-health-slack-bot \
      -o json
  )"; then
    echo "Could not inspect subscription $subscription_id; bootstrap is blocked." >&2
    exit 1
  fi
  if ! matching_groups="$(
    printf '%s' "$groups_json" | python3 -c \
      'import json,sys; expected=sys.argv[1].casefold(); print("\n".join(item["name"] for item in json.load(sys.stdin) if str(item.get("tags", {}).get("azd-env-name", "")).casefold() == expected))' \
      "$AZURE_ENV_NAME"
  )"; then
    echo "Could not validate environment tags; bootstrap is blocked." >&2
    exit 1
  fi
  for resource_group in $matching_groups; do
    MATCHED_CENTRAL_TARGETS="${MATCHED_CENTRAL_TARGETS}${subscription_id}|${resource_group}
"
  done
done
NORMALIZED_MATCHES="$(printf '%s' "$MATCHED_CENTRAL_TARGETS" | \
  sed '/^$/d' | tr '[:upper:]' '[:lower:]')"
EXPECTED_MATCH="$(printf '%s|%s' \
  "$TARGET_SUBSCRIPTION_ID" "$EXPECTED_RESOURCE_GROUP" | \
  tr '[:upper:]' '[:lower:]')"
test "$NORMALIZED_MATCHES" = "$EXPECTED_MATCH" || {
  echo "The environment name is absent or ambiguous across subscriptions; bootstrap is blocked." >&2
  exit 1
}
echo "bootstrap-target-confirmed"

python scripts/manage_slack_token.py bootstrap \
  --environment-name "$AZURE_ENV_NAME"
```

Run the lifecycle CLI with the virtual environment's `python`. A system Python
without `requirements-ops.txt` fails before secret persistence with
`ModuleNotFoundError: No module named 'azure.storage'`. If that happens, stop,
activate `.venv`, install both requirements files from Stage 3, confirm
`operations-sdk-ready`, verify `status` still reports `Bootstrapped: false`, and
then retry.

The first command creates only the network, private endpoints, Log Analytics,
Application Insights, Key Vault, application Storage account/table, isolated
operation-lock Storage account/container, registry, and managed identity. The
read-only gate then proves the active account and selected AZD environment have
the approved tenant, subscription, and resource group, and that no other
enabled subscription in that tenant has a central resource group tagged with
the same environment name, using the same case-insensitive comparison as the
lifecycle CLI. Stop unless `bootstrap-target-confirmed` is the final line before
the lifecycle CLI. The bootstrap command then:

1. validates the selected environment, then collects the hidden `xoxb-`
   credential and explicit operator IPv4 before holding the distributed lock;
2. acquires the local and central operation locks;
3. validates the credential with Slack `auth.test`;
4. writes it through the Key Vault data plane and verifies nonsecret metadata;
5. restores temporary vault network/RBAC access exactly;
6. proves no local AZD token remains;
7. clears the emergency version pin and performs a token-free workload
   provision using the versionless secret URI;
8. records the latest verified secret version before acceptance so rerunning
   `bootstrap` after a final provision failure resumes token-free instead of
   requesting or writing another credential.

The hidden token prompt accepts only the Slack **Bot User OAuth Token**, whose
value starts with `xoxb-`. Copy it from **OAuth & Permissions**, paste it
directly into the hidden prompt, and never place it in a command, shell variable,
file, clipboard-reading helper, transcript, or chat. At the network prompt,
enter only the explicit public IPv4 address, for example `203.0.113.10`; the CLI
adds the `/32` rule. Do not type the literal `/32` suffix.

AZD's `SERVICE_APP_RESOURCE_EXISTS` signal makes that provision an image-safe
upsert: an existing Container App keeps its currently deployed image, while a
new app starts from a digest-pinned bootstrap image until `azd deploy` publishes
the application image. This prevents migration, rotation, rollback, and routing
provisions from replacing the active workload with a mutable placeholder.

If the operator is outside approved private network access, supply the exact
operator IPv4 `/32` when prompted by the hardened command. Never use an
automatically discovered third-party address. Any failure to restore Key Vault
networking or temporary RBAC blocks workload provisioning.

Expected state: the resource group additionally contains the Container Apps
environment/app, Action Group, disabled baseline Activity Log Alert, monitoring
diagnostics, availability test, and actionable alerts when an independent
operations Action Group ID was supplied.

Capture outputs and inspect the central resources:

```bash
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP \
  -e "$AZURE_ENV_NAME" --no-prompt)"
APP_NAME="$(azd env get-value SERVICE_APP_NAME \
  -e "$AZURE_ENV_NAME" --no-prompt)"
APP_URI="$(azd env get-value SERVICE_APP_URI \
  -e "$AZURE_ENV_NAME" --no-prompt)"
ACTION_GROUP="ag-${AZURE_ENV_NAME}-service-health"

test -n "$RESOURCE_GROUP"
test -n "$APP_NAME"
test -n "$APP_URI"
az group show --name "$RESOURCE_GROUP" \
  --query '{name:name,location:location,state:properties.provisioningState}' \
  -o table
az containerapp show --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --query '{name:name,state:properties.provisioningState,external:properties.configuration.ingress.external}' \
  -o table
az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" \
  --query '{location:location,enabled:enabled,aad:webhookReceivers[0].useAadAuth,commonSchema:webhookReceivers[0].useCommonAlertSchema}' \
  -o table
```

Run the machine checkpoint:

```bash
test -n "$RESOURCE_GROUP" &&
test -n "$APP_NAME" &&
test -n "$APP_URI" &&
test "$(az group show --name "$RESOURCE_GROUP" \
  --query properties.provisioningState -o tsv)" = "Succeeded" &&
test "$(az containerapp show --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query properties.provisioningState -o tsv)" = "Succeeded" &&
test "$(az containerapp show --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query properties.configuration.ingress.external -o tsv)" = "true" &&
test "$(az monitor action-group show --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" --query location -o tsv | \
  tr '[:upper:]' '[:lower:]')" = "global" &&
test "$(az monitor action-group show --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" --query enabled -o tsv)" = "true" &&
test "$(az monitor action-group show --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" \
  --query 'webhookReceivers[0].useAadAuth' -o tsv)" = "true" &&
test "$(az monitor action-group show --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" \
  --query 'webhookReceivers[0].useCommonAlertSchema' -o tsv)" = "true" &&
echo "azure-resources-ready"
```

Expected checkpoint values:

- the resource group and Container App provisioning states are `Succeeded`;
- Container App external ingress is `True`;
- the Action Group location is `Global`;
- `enabled`, `aad`, and `commonSchema` are `True`.

Checkpoint: do not deploy application code until all expected values are
present and the machine checkpoint prints `azure-resources-ready`. Provisioning
success alone is not enough if the Action Group contract is wrong.

Run the structured status checkpoint again:

```bash
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
```

For the new environment after infrastructure provision and successful
bootstrap, the JSON must have these invariants:

<!-- status-contract:post-bootstrap -->
```json
{
  "Environment": "<selected AZD environment>",
  "KeyVaultName": "<deployed vault name>",
  "SecretVersion": "",
  "LatestSecretVersion": "<enabled version id>",
  "PreviousSecretVersion": "",
  "LegacyTokenPresent": false,
  "MigrationMarkerSet": true,
  "Bootstrapped": true
}
```

`Environment`, `KeyVaultName`, and `LatestSecretVersion` must be nonempty;
`SecretVersion` and `PreviousSecretVersion` are empty for a first bootstrap.
The bootstrap command separately validates the enabled latest Key Vault
version, versionless Container Apps reference, and absence of temporary
firewall, RBAC, lock, or journal residue; those details are not additional
fields in the status JSON.

Run an independent nonsecret cleanup checkpoint:

```bash
KEY_VAULT_NAME="$(azd env get-value SERVICE_HEALTH_KEY_VAULT_NAME \
  -e "$AZURE_ENV_NAME" --no-prompt)"
KEY_VAULT_ID="$(az keyvault show --name "$KEY_VAULT_NAME" \
  --query id -o tsv)"
CALLER_OBJECT_ID="$(az ad signed-in-user show --query id -o tsv)"

test "$(python scripts/manage_slack_token.py lock-status \
  --environment-name "$AZURE_ENV_NAME" --json | \
  python -c 'import json,sys; print(json.load(sys.stdin)["Status"])')" = \
  "Unlocked" &&
test "$(az deployment group list --resource-group "$RESOURCE_GROUP" \
  --query "length([?starts_with(name, 'service-health-journal')])" \
  -o tsv)" = "0" &&
test "$(az keyvault show --name "$KEY_VAULT_NAME" \
  --query 'length(properties.networkAcls.ipRules)' -o tsv)" = "0" &&
test "$(az role assignment list --scope "$KEY_VAULT_ID" \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --query "length([?roleDefinitionName=='Key Vault Secrets Officer'])" \
  -o tsv)" = "0" &&
echo "bootstrap-cleanup-proven"
```

If the vault intentionally had pre-existing IP rules or the operator already
had `Key Vault Secrets Officer`, compare against the pre-bootstrap snapshot
instead of requiring zero. The invariant is exact restoration, not globally
empty configuration.

Recovery:

- For `MissingSubscriptionRegistration`, register the namespace named in the
  error and rerun the same idempotent provision command.
- For role-assignment failures, verify the Azure RBAC assignments from stage 1
  and allow for propagation before retrying.
- For a regional capacity or policy error, do not change regions without a new
  preview. Update `AZURE_LOCATION`, rerun preview, and obtain approval again.
- If bootstrap fails after the secret write, do not restore plaintext. Correct
  the reported nonsecret cleanup or provisioning state and rerun the same
  `python scripts/manage_slack_token.py bootstrap --environment
  "$AZURE_ENV_NAME"` command. When a verified latest version is already
  recorded and the Container App is positively observed as incomplete, it
  reacquires the distributed lock and journal, reruns only the token-free
  workload provision and acceptance path, and reports `BootstrapRecovered`;
  it does not prompt for or write another token. Authentication,
  authorization, or control-plane read failures stop without provisioning.

### Stage 7: build and deploy the application

Prerequisites:

- the stage 6 resource checkpoint passed;
- Docker still reports a running Linux server;
- the current directory is the repository root.

Run:

```bash
docker info --format 'os={{.OSType}}'
command -v docker-credential-desktop.exe >/dev/null 2>&1 || true
azd deploy -e "$AZURE_ENV_NAME" --no-prompt
```

On WSL with Docker Desktop, do not replace `PATH` with a minimal Linux-only
value before `azd deploy`. Docker may need the Windows credential helper under
`/mnt/c/Program Files/Docker/Docker/resources/bin`. If packaging fails with
`docker-credential-desktop.exe: executable file not found`, restore the normal
WSL `PATH`, confirm `docker info`, and rerun the idempotent `azd deploy`.

Expected state: AZD builds the Docker image, pushes it to the deployed registry,
updates the Container App, and reports a successful service deployment.

Inspect active revisions:

```bash
az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query '[?properties.active].{name:name,active:properties.active,created:properties.createdTime}' \
  -o table

test "$(az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query 'length([?properties.active])' -o tsv)" = "1" &&
echo "application-revision-ready"
```

Checkpoint: single revision mode must show exactly one active revision and its
`active` value must be `True`. The final line must be
`application-revision-ready`.

Recovery:

- If Docker cannot connect, fix the engine before rerunning
  `azd deploy -e "$AZURE_ENV_NAME" --no-prompt`.
- If image push or pull fails, inspect the managed identity's `AcrPull` role and
  allow for role-assignment propagation.
- If the new revision does not become active, inspect
  `az containerapp logs show --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" --type system --tail 100`
  before retrying or activating a known-good revision.

### Stage 8: verify probes and the authentication boundary

Prerequisites: stage 7 shows one active revision.

Run:

```bash
test "$(curl -fsS "$APP_URI/healthz")" = '{"status":"healthy"}' &&
test "$(curl -fsS "$APP_URI/readyz")" = '{"status":"ready"}' &&
HTTP_CODE="$(curl -sS -o /tmp/service-health-response.json -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  "$APP_URI/api/service-health")" &&
test "$HTTP_CODE" = "401" &&
python3 -m json.tool /tmp/service-health-response.json &&
rm -f /tmp/service-health-response.json &&
echo "ingress-auth-checkpoint-passed"
```

Expected state:

- `/healthz` returns exactly `{"status":"healthy"}`;
- `/readyz` returns exactly `{"status":"ready"}`;
- an unauthenticated webhook request returns HTTP `401`;
- the final line is `ingress-auth-checkpoint-passed`.

Checkpoint: all three responses must match. A public `200` from the webhook is a
security failure and must block the signed test.

Recovery:

- A probe timeout points to ingress, revision, or startup failure. Inspect
  system and console logs.
- A readiness `503` points to missing required environment values or client
  construction failure.
- A webhook response other than `401` requires inspection of Container Apps
  authentication and Flask authorization before proceeding.

### Stage 9: send the signed Service Health test

Prerequisites:

- the stage 8 checkpoint passed;
- the bot is present in the configured Slack destination;
- the operator can invoke Action Group test notifications.

The Azure CLI test requires the receiver definition even though the Action
Group already exists:

```bash
URI="$(az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" \
  --query 'webhookReceivers[0].serviceUri' -o tsv)"
OBJECT_ID="$(az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" \
  --query 'webhookReceivers[0].objectId' -o tsv)"
IDENTIFIER_URI="$(az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP" \
  --query 'webhookReceivers[0].identifierUri' -o tsv)"

test -n "$URI" &&
test -n "$OBJECT_ID" &&
test -n "$IDENTIFIER_URI" &&
az monitor action-group test-notifications create \
  --resource-group "$RESOURCE_GROUP" \
  --action-group-name "$ACTION_GROUP" \
  --alert-type servicehealth \
  --add-action webhook slack-service-health "$URI" \
    useaadauth "$OBJECT_ID" "$IDENTIFIER_URI" usecommonalertschema \
  --only-show-errors \
  -o json
```

Expected state: the Azure operation completes, the receiver succeeds, the
Container App records an HTTP `200` request, and a formatted Service Health root
message appears in the configured Slack channel. Microsoft REST examples show
`Completed`; live project tests have also returned operation state `Complete`
and receiver state `Succeeded`.

The built-in test sends Azure-owned synthetic values, including its
subscription and tracking identifiers, and it can render as `Resolved`. Those
values do not describe a real incident. A later test submission for the same
synthetic incident can exercise root correlation, `chat.update`, and the thread
reply, but it is not proof of byte-identical duplicate suppression. Exact
duplicate handling and the complete Active/Updated/Resolved state machine are
covered by the automated tests because the Action Group test command does not
allow custom lifecycle payloads.

Inspect recent application logs:

```bash
az containerapp logs show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --type console \
  --tail 100
```

Checkpoint: deployment is accepted only when the signed test succeeds and the
Slack message appears in the intended channel. After telemetry arrives, use the
[Application Insights queries](#application-insights-queries) to confirm the
Table and Slack dependencies.

The successful Azure CLI response has `state` equal to `Complete` or
`Completed`, and the Secure Webhook action has `Status: Succeeded`. The
Container App console log must show the signed `POST /api/service-health` with
HTTP `200` and `Service Health incident processed`. Finally, a human must
confirm that the formatted message appeared in the configured Slack channel.
The Azure-owned synthetic subscription and tracking IDs in that message are
expected and are not production incident data.

Recovery:

- A `401` or `403` indicates an audience, allowed-client, owner, or app-role
  mismatch. Reconcile the hook and inspect the Container Apps authentication
  settings.
- A signed HTTP `200` with no Slack message points to token, scope, routing, or
  channel membership.
- A `503` points to a transient Slack or Storage dependency failure. Inspect
  dependencies and retry only after correction.
- Do not repeat a failing test in a tight loop. Azure Monitor retries eligible
  failures and can suppress calls to the endpoint for 15 minutes after
  exhausting the retry sequence.

## Manage alert scopes

The initial deployment creates one alert for its subscription. Pin the central
environment and Azure CLI context before every day-2 command:

```bash
export AZURE_ENV_NAME="<environment-name>"
TARGET_TENANT_ID="$(
  azd env get-value AZURE_TENANT_ID \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
TARGET_SUBSCRIPTION_ID="$(
  azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME"
); then
  echo "Day-2 target confirmation failed." >&2
  exit 1
fi
echo "day2-target-confirmed"
source .venv/bin/activate
```

Stop unless `day2-target-confirmed` prints. If Azure CLI is signed into another
tenant, run `az login --tenant "$TARGET_TENANT_ID"` and repeat the complete
check. List coverage or add a subscription with the environment name supplied
explicitly:

```bash
python scripts/manage_alert_scopes.py list \
  --environment-name "$AZURE_ENV_NAME"
python scripts/manage_alert_scopes.py add-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
python scripts/manage_alert_scopes.py add-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fingerprint-from-reviewed-what-if>"
```

The execution creates the target resource group, Action Group, and disabled
Activity Log Alert, sends the signed Secure Webhook test, and enables the alert
only after that test succeeds. To remove the temporary or retired path, obtain
and review a fresh destructive fingerprint, then execute the exact reviewed
operation:

```bash
python scripts/manage_alert_scopes.py remove-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
python scripts/manage_alert_scopes.py remove-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fresh-reviewed-fingerprint>"
```

Management Group commands expand accessible descendants into managed
subscription alert paths:

```bash
python scripts/manage_alert_scopes.py add-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
python scripts/manage_alert_scopes.py add-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fingerprint-from-reviewed-what-if>"
python scripts/manage_alert_scopes.py migrate-to-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" \
  --what-if --json
python scripts/manage_alert_scopes.py migrate-to-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fingerprint-from-reviewed-what-if>"
python scripts/manage_alert_scopes.py migrate-from-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
python scripts/manage_alert_scopes.py migrate-from-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fingerprint-from-reviewed-what-if>"
python scripts/manage_alert_scopes.py remove-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
python scripts/manage_alert_scopes.py remove-management-group \
  --management-group-id "platform" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fresh-reviewed-fingerprint>"
```

Use the immutable Management Group **ID**, not its display name. Start with
`--what-if --json`; it enumerates accessible descendants and performs no
mutation. The Tenant Root Group ID is the tenant ID and includes every
subscription in the hierarchy. If it includes the central subscription, the
CLI intentionally fails with `overlaps the immutable azd-owned baseline alert`.
Do not bypass that guard. Create or select a non-overlapping child Management
Group, or use `add-subscription` for the required descendants.

Use `migrate-from-management-group` when retiring a Management Group path.
It creates disabled individual replacements for every descendant subscription,
performs the signed test, enables and verifies every replacement, and only then
removes the Management Group-owned alert resources. This avoids both a coverage
gap and the overlap deadlock that separate add/remove commands intentionally
reject.

Always pass `--environment-name`; do not rely on discovery when more than one
deployment can exist. Use `--json` for machine-readable output.

All mutating scope commands require the exact `ExecutionFingerprint` from a
reviewed `--what-if --json` result. The fingerprint expires after 15 minutes and
binds the tenant, subscription, environment, command parameters, Management
Group descendants, current managed scopes, and the manager/Bicep artifact
hashes. The command rechecks those bindings before every serial deploy, enable,
or delete and stops on drift. `--what-if` never acquires the lock or writes a
journal. Remove and migration operations additionally require interactive
confirmation when they execute. `--force` supplies that confirmation only for
preapproved noninteractive automation; it does not bypass review, tenant,
permission, coverage, ownership, or signed-test checks.

The command checks the exact Azure operations it needs before mutation. A
typical assignment is `Contributor` on each target subscription plus
`Monitoring Contributor` at the Management Group scope used by a Management
Group command. The operator also needs enough read access to enumerate the
Management Group and every managed subscription. The command fails closed when
it cannot prove membership or coverage.

Each new path is deployed disabled, tested through Azure Monitor's signed
Secure Webhook test, and enabled only after the test succeeds. The AZD-owned
baseline alert remains outside day-2 ownership. There is no separate day-2
`test` command: both `add-subscription` and `add-management-group` perform this
signed test as part of their fail-closed execution. Removal does not send a new
notification; it requires a fresh reviewed fingerprint and revalidates current
membership, ownership, and Action Group references immediately before deletion.

Scope management creates alert resources; it does not edit the central routing
document. The required default channel receives a newly added subscription
until a more specific rule matches. If a subscription or Management Group
member needs its own destination, update and provision routing first, invite
the bot to that channel, and then add the scope. The signed Action Group test
uses a synthetic subscription ID, so it cannot validate a real
subscription-specific rule. It normally exercises the default route, although
a service or region rule that matches the synthetic payload can take
precedence.

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
| where Target =~ "slack.com" or Target contains ".table.core.windows.net"
| summarize count(), failures=countif(Success == false) by Target, ResultCode
```

Run these queries against the Log Analytics workspace linked to Application
Insights. If the Application Insights Logs view appears empty immediately after
a signed test, query the workspace directly:

```bash
WORKSPACE_ID="$(az monitor log-analytics workspace list \
  --resource-group "$RESOURCE_GROUP" \
  --query '[0].customerId' -o tsv)"

az monitor log-analytics query \
  --workspace "$WORKSPACE_ID" \
  --analytics-query \
  'AppDependencies | where TimeGenerated > ago(1h) | where Target =~ "slack.com" or Target contains ".table.core.windows.net" | project TimeGenerated, Name, Target, ResultCode, Success | order by TimeGenerated desc' \
  -o table
```

Correlated repeat processing can emit a failed in-process
`TableClient.create_entity` span while the external Table request reports an
accepted `409`, the webhook returns `200`, and processing continues through the
existing entity. Treat it as the expected reservation collision only when the
request, external dependency, subsequent checkpoints, and absence of an
application exception all confirm that handled path.

The deployed operations rules alert on webhook `5xx` responses, permanent
processing failures such as a rejected Slack destination, sustained Slack or
Table dependency failures, and availability failures. Also investigate missing
successful deliveries when an incident is expected.

### Update routing

Routing is revision configuration, not image content. Confirm the tenant,
subscription, environment, and location immediately before changing it:

```bash
export AZURE_ENV_NAME="<environment-name>"
TARGET_TENANT_ID="$(
  azd env get-value AZURE_TENANT_ID \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
TARGET_SUBSCRIPTION_ID="$(
  azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
AZURE_LOCATION="$(
  azd env get-value AZURE_LOCATION \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME" &&
  test -n "$AZURE_LOCATION"
); then
  echo "Routing target confirmation failed." >&2
  exit 1
fi
echo "routing-target-confirmed"

if ! ROUTES_B64="$(
  python -c 'import base64, pathlib; print(base64.b64encode(pathlib.Path("config/service_health_routes.json").read_bytes()).decode())'
)"; then
  echo "Could not encode the routing file." >&2
  exit 1
fi
if ! azd env set SERVICE_HEALTH_ROUTES_JSON_B64 "$ROUTES_B64" \
  -e "$AZURE_ENV_NAME" --no-prompt; then
  unset ROUTES_B64
  echo "Could not store routing in the selected AZD environment." >&2
  exit 1
fi
unset ROUTES_B64

azd provision --preview \
  -e "$AZURE_ENV_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" \
  --no-prompt
```

Proceed only when preview succeeds and contains the intended routing change.
Provision the same explicit environment:

```bash
azd provision \
  -e "$AZURE_ENV_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" \
  --no-prompt
```

Stop unless `routing-target-confirmed` prints. Routing preview and provision are
token-free. Review preview before running the second command, and reject any
change to the Key Vault secret value, a version pin, or the deployed image.

### Rotate the Slack token

This procedure replaces a long-lived static `xoxb-` credential. It is not
Slack's expiring token rotation mode, which this runtime does not support. Keep
that Slack setting disabled.

Confirm the complete target, then use the lifecycle CLI. It receives the token
only through a hidden prompt, validates the expected Slack identity, creates a
Key Vault version through the data plane, clears the emergency pin, performs a
token-free provision/restart, and runs the configured acceptance checks:

```bash
export AZURE_ENV_NAME="<environment-name>"
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
python scripts/manage_slack_token.py rotate \
  --environment-name "$AZURE_ENV_NAME"
```

Do not revoke the prior Slack credential until the primary and backup owners
confirm `/healthz`, `/readyz`, unauthenticated `401`, signed Service Health
delivery, intended Slack message, Table dependency success, and independent
monitoring. On failure, the CLI records the previous version for rollback; do
not paste either credential into AZD.

Roll back the secret reference with the recorded nonsecret version:

```bash
python scripts/manage_slack_token.py rollback \
  --environment-name "$AZURE_ENV_NAME"
```

This sets only `SERVICE_HEALTH_SECRET_VERSION`, performs a token-free provision,
and enters the deliberate `ROLLBACK_PINNED` state. After repair and complete
acceptance, rotate again to return to the versionless reference.

### Migrate an existing deployment

Existing environments that retain `SLACK_BOT_TOKEN` are blocked from ordinary
preview and provision. For `service-health-mgmt-test`, first capture the current
revision, versioned secret URI, Secure Webhook receiver, baseline/day-2 alert
states, vault network/RBAC state, and independent operations receiver. Review a
what-if showing no deletions, replacements, or unintended alert changes.

Freeze AZD, routing, token, and day-2 mutations, then run:

```bash
python scripts/manage_slack_token.py status \
  --environment-name service-health-mgmt-test --json
python scripts/manage_slack_token.py migrate \
  --environment-name service-health-mgmt-test
```

Migration reads the exact local legacy entry directly under a private file lock;
it never asks AZD or a child process to return the value. The active workload
remains pinned while the same credential is staged. Only after secret metadata,
temporary-access cleanup, and atomic local removal succeed does it perform the
token-free versionless provision.

Acceptance requires the original alert and Secure Webhook lifecycle to remain
intact, one healthy active revision, successful signed Slack/Table delivery, an
empty emergency pin, no local token line, and no temporary RBAC/firewall/lock/
journal residue. A failure after local cleanup must be retried from Key Vault;
never restore plaintext.

### Recover an abandoned operation lock

Locks never expire into automatic deletion. The central mutex is an atomically
created private blob with a renewable 60-second Azure Blob lease in a dedicated
lock-only storage account. The account contains no application/customer data;
shared-key access is used only in Python memory because the operator already has
management-plane access to retrieve that isolated account key. No key enters
arguments, environment variables, journals, or logs. Active commands renew the
lease and revalidate the operation nonce before each mutation phase. Before
recovery, prove the recorded environment and target IDs match, the 15-minute
metadata expiry has passed, the finite Blob lease has expired, and no relevant
ARM deployment or lifecycle/day-2 command is active. Then run the explicit
recovery:

```bash
python scripts/manage_slack_token.py lock-status \
  --environment-name "$AZURE_ENV_NAME" --json
python scripts/manage_slack_token.py recover-lock \
  --environment-name "$AZURE_ENV_NAME" --force
```

Recovery performs its own read-only status check and refuses an active lease.
Stop unless the status is `StaleBlocking` and every active operation can be
ruled out. Preserve a failed operation journal until the primary and backup
owners reconcile every partial resource.

The lock is shared by token lifecycle and day-2 scope commands, so the
`manage_slack_token.py lock-status` and `recover-lock` commands are also the
supported recovery surface after an interrupted `manage_alert_scopes.py`
operation. Never delete the lock blob directly. Azure Blob leases can remain
unavailable briefly after expiry; the explicit recovery path proves ownership,
environment, metadata expiry, lease state, and final absence.

### Roll back application code

The Container App uses single revision mode. Confirm the target and load the
resource names from that explicit environment before activating a known-good
revision:

```bash
export AZURE_ENV_NAME="<environment-name>"
TARGET_TENANT_ID="$(
  azd env get-value AZURE_TENANT_ID \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
TARGET_SUBSCRIPTION_ID="$(
  azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$AZURE_ENV_NAME" --no-prompt
)"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP \
  -e "$AZURE_ENV_NAME" --no-prompt)"
APP_NAME="$(azd env get-value SERVICE_APP_NAME \
  -e "$AZURE_ENV_NAME" --no-prompt)"
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME" &&
  test -n "$RESOURCE_GROUP" &&
  test -n "$APP_NAME"
); then
  echo "Rollback target confirmation failed." >&2
  exit 1
fi
echo "rollback-target-confirmed"

az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --query '[].{name:name,active:properties.active,created:properties.createdTime}' \
  -o table

az containerapp revision activate \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --revision "<known-good-revision>"
```

Stop unless `rollback-target-confirmed` prints. For routing or token rollback,
restore the previous value through the explicit routing or rotation procedure.

### Troubleshooting

| Symptom | Check |
|---|---|
| Browser or device-code login is blocked | Follow the tenant's Conditional Access policy and use an allowed interactive browser flow. This runbook delegates AZD authentication to Azure CLI; rerun `az login`, `az account set`, and `azd auth login --check-status --no-prompt`. |
| `azd version` is older than expected after an update | Run `command -v azd` and `type -a azd`; put the current installation before the older installation in `PATH` and start a new shell. |
| `az quota list` is empty or reports stale `Microsoft.Quota` registration | Use the provider-specific usage requests in stage 1 and wait for provider propagation. Registration state alone does not prove available capacity. |
| Docker is unavailable from WSL | Run `docker context show` and `docker info`; confirm Docker Desktop uses Linux containers and enables WSL integration. |
| `azd deploy` cannot find `docker-credential-desktop.exe` | Restore the normal WSL `PATH` or add Docker Desktop's resources directory, then confirm `command -v docker-credential-desktop.exe` and rerun `azd deploy`. Do not rebuild from a Windows-mounted checkout. |
| An operational CLI reports `No module named 'azure.storage'` or `azure.keyvault` | Activate `.venv`, install `requirements.txt` and `requirements-ops.txt`, run the Stage 3 SDK import checkpoint, and retry. |
| Secure Webhook setup is forbidden | Confirm the Azure CLI identity has the Microsoft Entra `Application Administrator` role. |
| Signed test returns `401` or `403` | Check Easy Auth audiences, the AzNS allowed application, and the app role assignment. |
| Signed test succeeds but Slack is empty | Check the bot token, `chat:write`, channel IDs, and bot membership. |
| Corrected webhook receives no calls | Wait 15 minutes after Azure Monitor exhausts webhook retries. |
| `/readyz` returns `503` | Check required environment values and Container App logs. |
| `/readyz` returns `200`, but delivery fails | Test Table access through a signed notification and inspect dependency telemetry. |
| Application Insights Logs appears empty after a test | Query the linked Log Analytics workspace directly with the command in the operations section. |
| Noninteractive `az containerapp exec` fails with a TTY or WebSocket error | Do not use administrative exec as proof of webhook delivery. Use probes, the signed Action Group test, Container App logs, Table state, and workspace telemetry. |
| Day-2 discovery is ambiguous | Pass `--environment-name` and verify the deployment tags. |
| A new day-2 alert stays disabled | Correct the signed Secure Webhook test failure, observe any retry cooldown, and rerun the idempotent add command. |
| Ordinary provision reports a legacy token | Stop. Run lifecycle `status`, then the migration procedure; do not delete or display the value manually. |
| A lifecycle operation reports cleanup residue | Do not provision. Restore the exact prior vault network/RBAC state and reconcile the preserved journal. |
| The central operation lock is expired | Prove no ARM deployment or tool operation is active, then use the explicit `recover-lock --force` procedure. |
| A day-2 journal deployment name exceeds 64 characters | Update to a repository revision containing deterministic journal-name truncation and hashing. Do not shorten subscription or Management Group IDs manually; rerun the what-if because the execution fingerprint binds the code artifacts. |
| A Management Group preview overlaps the baseline alert | The group contains the central subscription. Use a non-overlapping child Management Group or add the required subscriptions individually. Do not modify or adopt the AZD-owned baseline into day-2 management. |

## End-to-end acceptance record

The following path was exercised from a clean environment on 2026-08-15. It is
the minimum evidence required before calling another deployment operational:

| Gate | Accepted evidence |
|---|---|
| Workstation | Python 3.12, Azure CLI 2.81.0, Azure CLI-managed Bicep 0.46.1, AZD 1.31.0, Docker client/server 28.3.3, and Git 2.43.0 responded successfully. |
| Repository | 381 tests, Flake8, dependency audit, both Bicep build/lint graphs, Docker build, and AZD packaging passed after the operational fixes documented here. |
| Infrastructure preview | Infrastructure-only preview showed the expected phase-one resources and no Container App, Action Group, or alert before bootstrap. |
| Bootstrap | The hidden `xoxb-` transfer completed, Key Vault held an enabled latest version, local plaintext was absent, migration marker was set, and temporary firewall/RBAC/lock/journal state was restored. |
| Workload | `azd deploy` published the application image and Container Apps single revision mode converged to exactly one healthy active revision. |
| Security boundary | `/healthz` and `/readyz` returned `200`; an anonymous webhook POST returned `401`. |
| Signed delivery | Action Group test notification returned Secure Webhook `Succeeded`; the app logged an authenticated HTTP `200` and `Service Health incident processed`; the formatted message appeared in the configured Slack channel. |
| Multi-scope safety | Subscription what-if produced an expiring artifact-bound fingerprint. Tenant Root Group what-if was correctly rejected when it overlapped the immutable central baseline. |

Do not copy environment-specific IDs from another deployment. Reproduce every
gate with the target tenant, subscription, region, Slack workspace, channel, and
approved Management Group hierarchy.

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

See [Microsoft platform evidence](microsoft-platform-evidence.md) for the
larger source-to-control mapping, including permissions, API behavior, limits,
and the repository control that depends on each fact.

### Production ownership and retention

Production acceptance requires named primary and backup owners for Azure,
Slack, routing, secret rotation, and rollback. The independent operations
Action Group must have an on-call destination outside this bot and a tested
receiver. Record these owners, the HTTPS runbook, Action Group resource ID, and
the successful receiver-test evidence in the approved change record and the
nonsecret readiness values, not in repository defaults.

The independent operations Action Group is not owned by the bot deployment and
must not be deleted by the bot decommission procedure. Cleanup or replacement
requires a separate destructive approval: inventory every alert that references
its exact resource ID, obtain primary and backup owner approval, preserve on-call
coverage during migration, verify the replacement receiver, and only then
remove the old Action Group or its resource group.

Run a signed canary and verify Slack plus Table dependency telemetry weekly.
Review Slack scopes, app owners, installation, and channel membership quarterly.
Review dependency pins and container scan results on every release.

Table Storage contains operational/customer incident communications,
subscription IDs, channel IDs, and Slack timestamps. The recommended production
retention is 90 days after the last resolved update. If automated pruning is not
implemented, the data owner must document and perform the equivalent manual
review and deletion. Never place payloads, tokens, tenant data, or unredacted
journals in support bundles. Build support bundles from an explicit allowlist;
exclude `.azure/`, `.env*`, operation journals, lock metadata, Key Vault
firewall snapshots, command transcripts, crash dumps, container layers, and
any file containing Slack token prefixes or authorization headers.

### Decommission

First inventory day-2 resources and retain the nonsecret identifiers needed
after Azure resources are gone. Keep the same shell open through the complete
procedure:

```bash
export AZURE_ENV_NAME="<environment-name>"
azd env select "$AZURE_ENV_NAME" --no-prompt
TARGET_TENANT_ID="$(azd env get-value AZURE_TENANT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)"
TARGET_SUBSCRIPTION_ID="$(azd env get-value AZURE_SUBSCRIPTION_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)"
DEPLOYMENT_LOCATION="$(azd env get-value AZURE_LOCATION \
  -e "$AZURE_ENV_NAME" --no-prompt)"
az login --tenant "$TARGET_TENANT_ID"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
if ! (
  test "$(az account show --query tenantId -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_TENANT_ID,,}" &&
  test "$(az account show --query id -o tsv | \
    tr '[:upper:]' '[:lower:]')" = "${TARGET_SUBSCRIPTION_ID,,}" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$AZURE_ENV_NAME" --no-prompt)" = "$AZURE_ENV_NAME"
); then
  echo "Decommission inventory target confirmation failed." >&2
  exit 1
fi
echo "decommission-target-confirmed"

source .venv/bin/activate
if ! python scripts/manage_alert_scopes.py list \
  --environment-name "$AZURE_ENV_NAME" --json; then
  echo "Could not inventory day-2 resources; refusing decommission." >&2
  exit 1
fi

API_CLIENT_ID="$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)"
API_OBJECT_ID="$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)"
API_IDENTIFIER_URI="$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI \
  -e "$AZURE_ENV_NAME" --no-prompt)"
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP \
  -e "$AZURE_ENV_NAME" --no-prompt)"
ACTION_GROUP="ag-${AZURE_ENV_NAME}-service-health"
EXPECTED_API_DISPLAY_NAME="Azure Service Health Slack Bot - ${AZURE_ENV_NAME}"
REPOSITORY_PATH="$(pwd -P)"
LOCAL_ENV_PATH="$REPOSITORY_PATH/.azure/$AZURE_ENV_NAME"

if ! (
  test -n "$API_CLIENT_ID" &&
  test -n "$API_OBJECT_ID" &&
  test "$API_IDENTIFIER_URI" = "api://$API_CLIENT_ID" &&
  test -n "$DEPLOYMENT_LOCATION" &&
  test -n "$RESOURCE_GROUP" &&
  test -d "$LOCAL_ENV_PATH"
); then
  echo "Required decommission identifiers are missing or inconsistent." >&2
  exit 1
fi

ACTION_GROUP_JSON="$(az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" --name "$ACTION_GROUP" -o json)"
if ! printf '%s' "$ACTION_GROUP_JSON" | python -c '
import json
import sys

data = json.load(sys.stdin)
receivers = data.get("webhookReceivers") or []
tags = data.get("tags") or {}
valid = (
    len(receivers) == 1
    and str(receivers[0].get("objectId", "")).casefold()
        == sys.argv[1].casefold()
    and receivers[0].get("identifierUri") == sys.argv[2]
    and receivers[0].get("useAadAuth") is True
    and tags.get("azd-env-name") == sys.argv[3]
    and tags.get("workload") == "azure-service-health-slack-bot"
)
raise SystemExit(0 if valid else 1)
' "$API_OBJECT_ID" "$API_IDENTIFIER_URI" "$AZURE_ENV_NAME"; then
  echo "Action Group provenance does not match this AZD environment." >&2
  exit 1
fi

GRAPH_APP_JSON="$(az rest --method get \
  --url "https://graph.microsoft.com/v1.0/applications/$API_OBJECT_ID?\$select=id,appId,displayName,identifierUris" \
  -o json)"
if ! printf '%s' "$GRAPH_APP_JSON" | python -c '
import json
import sys

data = json.load(sys.stdin)
valid = (
    str(data.get("id", "")).casefold() == sys.argv[1].casefold()
    and str(data.get("appId", "")).casefold() == sys.argv[2].casefold()
    and data.get("displayName") == sys.argv[3]
    and data.get("identifierUris") == [sys.argv[4]]
)
raise SystemExit(0 if valid else 1)
' "$API_OBJECT_ID" "$API_CLIENT_ID" "$EXPECTED_API_DISPLAY_NAME" \
  "$API_IDENTIFIER_URI"; then
  echo "Microsoft Graph application provenance is ambiguous or mismatched." >&2
  exit 1
fi

read -rp "App object ID from the approved Entra creation record: " \
  CREATION_RECORD_OBJECT_ID
if [[ "$CREATION_RECORD_OBJECT_ID" != "$API_OBJECT_ID" ]]; then
  echo "Independent creation evidence does not match; refusing deletion." >&2
  exit 1
fi

readonly DECOMMISSION_ENV_NAME="$AZURE_ENV_NAME"
readonly DECOMMISSION_TENANT_ID="$TARGET_TENANT_ID"
readonly DECOMMISSION_SUBSCRIPTION_ID="$TARGET_SUBSCRIPTION_ID"
readonly DECOMMISSION_LOCATION="$DEPLOYMENT_LOCATION"
readonly DECOMMISSION_RESOURCE_GROUP="$RESOURCE_GROUP"
readonly DECOMMISSION_API_CLIENT_ID="$API_CLIENT_ID"
readonly DECOMMISSION_API_OBJECT_ID="$API_OBJECT_ID"
readonly DECOMMISSION_API_IDENTIFIER_URI="$API_IDENTIFIER_URI"
readonly DECOMMISSION_API_DISPLAY_NAME="$EXPECTED_API_DISPLAY_NAME"
readonly DECOMMISSION_REPOSITORY_PATH="$REPOSITORY_PATH"
readonly DECOMMISSION_LOCAL_ENV_PATH="$LOCAL_ENV_PATH"
readonly DECOMMISSION_PROVENANCE="$DECOMMISSION_ENV_NAME|$DECOMMISSION_TENANT_ID|$DECOMMISSION_SUBSCRIPTION_ID|$DECOMMISSION_LOCATION|$DECOMMISSION_RESOURCE_GROUP|$DECOMMISSION_API_OBJECT_ID|$DECOMMISSION_API_CLIENT_ID|$DECOMMISSION_API_IDENTIFIER_URI|$DECOMMISSION_REPOSITORY_PATH"
unset ACTION_GROUP_JSON GRAPH_APP_JSON
echo "decommission-identifiers-retained"
```

Do not continue unless both inventory checkpoints print. The provenance
check requires one Secure Webhook receiver, matching deployment tags, the
environment-specific identifier URI, and the exact application display name
used by `scripts/configure_secure_webhook.py`. The creation-record object ID must
come from an approved
[Microsoft Entra audit log](https://learn.microsoft.com/entra/identity/monitoring-health/concept-audit-logs)
or deployment change record independent of the local AZD environment and
deployed Action Group. The current hook does not persist a creation-only marker.
A missing, duplicate, adopted, or legacy app without independent creation
evidence is a stop condition.

The day-2 remove commands preserve Service Health coverage and are not a
full-decommission switch. If you intend to end coverage, verify the
`service-health-managed-by=manage-alert-scopes` tags and manually delete only
the listed peripheral resource groups in their target subscriptions. Those
groups are outside the central AZD resource group and `azd down` does not remove
them.

After removing any intended day-2 peripheral resource groups, delete the
central Azure resources and the project-created app registration. The explicit
environment keeps `azd down` on the selected deployment:

```bash
if ! (
  test "$(pwd -P)" = "$DECOMMISSION_REPOSITORY_PATH" &&
  test "$AZURE_ENV_NAME" = "$DECOMMISSION_ENV_NAME" &&
  test "$(az account show --query tenantId -o tsv)" = \
    "$DECOMMISSION_TENANT_ID" &&
  test "$(az account show --query id -o tsv)" = \
    "$DECOMMISSION_SUBSCRIPTION_ID" &&
  test "$(azd env get-value AZURE_ENV_NAME \
    -e "$DECOMMISSION_ENV_NAME" --no-prompt)" = \
    "$DECOMMISSION_ENV_NAME" &&
  test "$(azd env get-value AZURE_TENANT_ID \
    -e "$DECOMMISSION_ENV_NAME" --no-prompt)" = \
    "$DECOMMISSION_TENANT_ID" &&
  test "$(azd env get-value AZURE_SUBSCRIPTION_ID \
    -e "$DECOMMISSION_ENV_NAME" --no-prompt)" = \
    "$DECOMMISSION_SUBSCRIPTION_ID" &&
  test "$(azd env get-value AZURE_LOCATION \
    -e "$DECOMMISSION_ENV_NAME" --no-prompt)" = \
    "$DECOMMISSION_LOCATION" &&
  test "$(azd env get-value AZURE_RESOURCE_GROUP \
    -e "$DECOMMISSION_ENV_NAME" --no-prompt)" = \
    "$DECOMMISSION_RESOURCE_GROUP" &&
  test "$DECOMMISSION_PROVENANCE" = \
    "$AZURE_ENV_NAME|$TARGET_TENANT_ID|$TARGET_SUBSCRIPTION_ID|$DEPLOYMENT_LOCATION|$RESOURCE_GROUP|$API_OBJECT_ID|$API_CLIENT_ID|$API_IDENTIFIER_URI|$(pwd -P)"
); then
  echo "Decommission target confirmation failed." >&2
  exit 1
fi
echo "decommission-delete-target-confirmed"

if ! azd down -e "$DECOMMISSION_ENV_NAME" --force --no-prompt; then
  echo "AZD reported incomplete Azure resource deletion." >&2
  exit 1
fi
if [[ "$(
  az group exists --name "$DECOMMISSION_RESOURCE_GROUP"
)" != "false" ]]; then
  echo "The central resource group still exists." >&2
  exit 1
fi

if ! (
  test "$(az account show --query tenantId -o tsv)" = \
    "$DECOMMISSION_TENANT_ID" &&
  test "$(az account show --query id -o tsv)" = \
    "$DECOMMISSION_SUBSCRIPTION_ID" &&
  test "$DECOMMISSION_PROVENANCE" = \
    "$DECOMMISSION_ENV_NAME|$DECOMMISSION_TENANT_ID|$DECOMMISSION_SUBSCRIPTION_ID|$DECOMMISSION_LOCATION|$DECOMMISSION_RESOURCE_GROUP|$DECOMMISSION_API_OBJECT_ID|$DECOMMISSION_API_CLIENT_ID|$DECOMMISSION_API_IDENTIFIER_URI|$DECOMMISSION_REPOSITORY_PATH"
); then
  echo "Entra deletion target changed after Azure cleanup." >&2
  exit 1
fi

GRAPH_APP_JSON="$(az rest --method get \
  --url "https://graph.microsoft.com/v1.0/applications/$DECOMMISSION_API_OBJECT_ID?\$select=id,appId,displayName,identifierUris" \
  -o json)"
if ! printf '%s' "$GRAPH_APP_JSON" | python -c '
import json
import sys

data = json.load(sys.stdin)
valid = (
    str(data.get("id", "")).casefold() == sys.argv[1].casefold()
    and str(data.get("appId", "")).casefold() == sys.argv[2].casefold()
    and data.get("displayName") == sys.argv[3]
    and data.get("identifierUris") == [sys.argv[4]]
)
raise SystemExit(0 if valid else 1)
' "$DECOMMISSION_API_OBJECT_ID" "$DECOMMISSION_API_CLIENT_ID" \
  "$DECOMMISSION_API_DISPLAY_NAME" \
  "$DECOMMISSION_API_IDENTIFIER_URI"; then
  echo "Entra application changed after provenance capture; refusing deletion." >&2
  exit 1
fi
unset GRAPH_APP_JSON

if ! az ad app delete --id "$DECOMMISSION_API_OBJECT_ID"; then
  echo "Could not delete the project app registration." >&2
  exit 1
fi
POST_DELETE_MATCH_COUNT="$(
  az ad app list \
    --filter "id eq '$DECOMMISSION_API_OBJECT_ID'" \
    --query 'length(@)' \
    -o tsv
)" || {
  echo "Could not verify project app registration deletion." >&2
  exit 1
}
if [ "$POST_DELETE_MATCH_COUNT" != "0" ]; then
  echo "The project app registration still exists." >&2
  exit 1
fi
echo "decommission-cloud-resources-removed"
```

Do not delete Microsoft's AzNS AAD Webhook service principal.

For the recommended dedicated Slack app, uninstall or delete the app through
Slack administration, remove it from the project channels, and delete its
password-manager record. Do not display the token while confirming revocation.

If the app is shared, decommission only this project's channel membership and
configuration. Do not remove the bot from a channel until the Slack app owner
has confirmed that no other consumer needs its membership there. Do not
uninstall the app or revoke, rotate, or delete its shared credential as part of
this procedure. If replacement is required, the owner must inventory every
consumer, distribute the replacement, verify each consumer has migrated, and
approve revocation of the old credential. Treat that as a separate coordinated
change.

`azd down` deletes Azure resources. It does not delete local application files
or the AZD environment. Hardened environments contain only nonsecret lifecycle
metadata, but legacy environments may still contain plaintext credentials.
Remove the local environment separately, then verify the directory is absent
without reading or printing any value:

```bash
if [[ "$(pwd -P)" != "$DECOMMISSION_REPOSITORY_PATH" ]]; then
  echo "Return to the captured repository before local cleanup." >&2
  exit 1
fi
if ! azd env remove "$DECOMMISSION_ENV_NAME" \
  -e "$DECOMMISSION_ENV_NAME" --force --no-prompt; then
  echo "Could not remove the local AZD environment." >&2
  exit 1
fi
if [[ -e "$DECOMMISSION_LOCAL_ENV_PATH" ]]; then
  echo "The local AZD environment still exists." >&2
  exit 1
fi
echo "decommission-local-credentials-removed"
```

The final line must be `decommission-local-credentials-removed`. If another
environment is intentionally retained, remove or rotate its credential before
accepting the decommission checkpoint.

This deployment enables Key Vault purge protection with a 90-day retention
period. The deleted vault cannot be purged or have its name reused until that
period expires. `azd down --purge` cannot override purge protection. If you need
to redeploy sooner, use a different AZD environment name or follow the Key Vault
recovery procedure and recreate the deleted integrations and role assignments.

## Tests

```bash
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
```

The test suite covers payload parsing, routing, authorization, Table Storage
coordination, Slack rendering and error classification, Flask routes, runtime
configuration, Secure Webhook setup, token lifecycle secrecy/failure states,
distributed operation locks, infrastructure contracts, and day-2 scope
management.

## Community and support

Read [CONTRIBUTING.md](../CONTRIBUTING.md) before proposing a change. Use
[SUPPORT.md](../SUPPORT.md) for support boundaries. Report suspected
vulnerabilities privately as described in [SECURITY.md](../SECURITY.md).

## License

This project is licensed under the [MIT License](../LICENSE).
