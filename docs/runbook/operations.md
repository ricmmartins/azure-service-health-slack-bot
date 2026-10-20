# Operations and troubleshooting

## Operations

### Existing-environment setup

For day-2, routing, rotation, recovery, rollback, or decommission in a new
terminal, first start the child Bash from
[safe shell setup](00-setup.md#safe-shell-and-repository-setup), then run this block.
Use the existing checkout and the same dedicated CLI profiles used for
deployment. Change `REPOSITORY_PATH` if the deployment checkout is elsewhere.
Do not clone over it, pull an unreviewed revision, or initialize a new
environment. Set tenant, subscription, and region from the approved deployment
record, not from an unverified current CLI default:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
umask 077
REPOSITORY_PATH="$HOME/src/azure-service-health-slack-bot"
test -e "$REPOSITORY_PATH/.git"
cd "$REPOSITORY_PATH"
source scripts/runbook_guard.sh
runbook_require_repo
export AZURE_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azure"
export AZD_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azd"
test -d "$AZURE_CONFIG_DIR"
test -d "$AZD_CONFIG_DIR"
export AZURE_ENV_NAME="<environment-name>"
export TARGET_TENANT_ID="<approved-tenant-id>"
export TARGET_SUBSCRIPTION_ID="<approved-subscription-id>"
export AZURE_LOCATION="<approved-region>"
az login --tenant "$TARGET_TENANT_ID"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
azd auth login --check-status --no-prompt
azd env select "$AZURE_ENV_NAME" --no-prompt
source .venv/bin/activate
runbook_require_target
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP -e "$AZURE_ENV_NAME" --no-prompt)"
APP_NAME="$(azd env get-value SERVICE_APP_NAME -e "$AZURE_ENV_NAME" --no-prompt)"
APP_URI="$(azd env get-value SERVICE_APP_URI -e "$AZURE_ENV_NAME" --no-prompt)"
EXPECTED_BASELINE_ENABLED="$(azd env get-value SERVICE_HEALTH_BASELINE_ALERT_ENABLED -e "$AZURE_ENV_NAME" --no-prompt)"
case "$EXPECTED_BASELINE_ENABLED" in
  true|false) ;;
  *) echo "Missing approved baseline state; inspect the deployment record." >&2; exit 1 ;;
esac
ACTION_GROUP="ag-${AZURE_ENV_NAME}-service-health"
test -n "$RESOURCE_GROUP"
echo "existing-environment-ready"
}
```

`EXPECTED_BASELINE_ENABLED` lets the Stage 9 signed test run against an already
accepted enabled baseline without disabling coverage or editing the test.
The test checks the observed state against that persisted expectation.
Run `runbook_require_target` at each operation boundary as shown below. It
checks the physical repository root and origin, dedicated profiles, ARM target,
default Graph token tenant metadata, selected AZD environment, and all four
nonsecret AZD bindings. It neither reads local secret values nor performs
Graph writes. For expired authentication, repeat this setup. If the
workstation or checkout moved, repeat Stage 0's tool and filesystem checks.

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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
test -n "$RESOURCE_GROUP"
WORKSPACE_ID="$(az monitor log-analytics workspace list \
  --resource-group "$RESOURCE_GROUP" \
  --query '[0].customerId' -o tsv)"
test -n "$WORKSPACE_ID"

az monitor log-analytics query \
  --workspace "$WORKSPACE_ID" \
  --analytics-query \
  'AppDependencies | where TimeGenerated > ago(1h) | where Target =~ "slack.com" or Target contains ".table.core.windows.net" | project TimeGenerated, Name, Target, ResultCode, Success | order by TimeGenerated desc' \
  -o table
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
runbook_require_target
python -m json.tool config/service_health_routes.json >/dev/null
python -c 'import json; from pathlib import Path; from service_health.routing import RoutingConfig; RoutingConfig.from_dict(json.loads(Path("config/service_health_routes.json").read_text()))'
if grep -Eq 'C0123456789|C1111111111|00000000-0000-0000-0000-000000000000' config/service_health_routes.json; then
  echo "Replace the routing example identifiers." >&2
  exit 1
fi

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

SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd hooks run preprovision \
  -e "$AZURE_ENV_NAME" --no-prompt
SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd provision --preview \
  -e "$AZURE_ENV_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" \
  --no-prompt
}
```

Proceed only when preview succeeds and contains the intended routing change.
Provision the same explicit environment:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
azd provision \
  -e "$AZURE_ENV_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" \
  --no-prompt
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
python scripts/manage_slack_token.py rotate \
  --environment-name "$AZURE_ENV_NAME"
}
```

Do not revoke the prior Slack credential until the primary and backup owners
confirm `/healthz`, `/readyz`, unauthenticated `401`, signed Service Health
delivery, intended Slack message, Table dependency success, and independent
monitoring. On failure, the CLI records the previous version for rollback; do
not paste either credential into AZD.

Roll back the secret reference with the recorded nonsecret version:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py rollback \
  --environment-name "$AZURE_ENV_NAME"
}
```

This sets only `SERVICE_HEALTH_SECRET_VERSION`, performs a token-free provision,
and leaves an explicit version pin. The CLI reports `Status: ROLLED_BACK` only
after its automated acceptance checks pass; human Slack receipt and dependency
telemetry remain required. After repair and complete acceptance, rotate again
to return to the versionless reference.

### Migrate an existing deployment

Existing environments that retain `SLACK_BOT_TOKEN` are blocked from ordinary
preview and provision. First capture the current
revision, versioned secret URI, Secure Webhook receiver, baseline/day-2 alert
states, vault network/RBAC state, and independent operations receiver. Review a
what-if showing no deletions, replacements, or unintended alert changes.

Freeze AZD, routing, token, and day-2 mutations, then run:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
python scripts/manage_slack_token.py migrate \
  --environment-name "$AZURE_ENV_NAME"
}
```

Migration reads the exact local legacy entry directly under a private file lock;
it never asks AZD or a child process to return the value. The active workload
remains pinned while the same credential is staged. Only after secret metadata,
temporary-access cleanup, and atomic local removal succeed does it perform the
token-free versionless provision.

Acceptance requires the original alert and Secure Webhook lifecycle to remain
intact, one healthy active revision, successful signed Slack/Table delivery, an
empty emergency pin, no local token line, and no temporary RBAC/firewall/lock/
journal residue. After local cleanup, `migrate` cannot be rerun: it requires the
legacy local entry that has already been removed. Never restore plaintext.
If cleanup was proven and the migration marker, latest version metadata, and
selected target are intact, use the Stage 5 **read-only preview block**, review
the unchanged image, secret reference, and baseline state, then run the
standalone token-free provision block in
[Update routing](#update-routing) without rerunning its routing-input block.
Repeat Stages 7's revision checkpoint, 8, and 9 against the expected baseline
state. Do not call the migration successful until those gates pass. Missing
metadata, temporary-access residue, or an ambiguous partial state requires the
primary and backup owners to reconcile the preserved journal first.

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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py lock-status \
  --environment-name "$AZURE_ENV_NAME" --json
}
```

Only after reviewing `StaleBlocking` and excluding active operations:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py recover-lock \
  --environment-name "$AZURE_ENV_NAME" --force
}
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

The Container App uses single revision mode. Do not use revision activation as
an image rollback: activation does not establish which image the latest
single-mode template will run. Select an approved previous revision, retrieve
only its image reference, and deploy that image as a new revision. This leaves
current routing, secret references, authentication, and scale settings intact:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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

read -r -p "Approved known-good revision name: " KNOWN_GOOD_REVISION
test -n "$KNOWN_GOOD_REVISION"
KNOWN_GOOD_IMAGE="$(az containerapp revision show \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --revision "$KNOWN_GOOD_REVISION" \
  --query 'properties.template.containers[0].image' -o tsv)"
test -n "$KNOWN_GOOD_IMAGE"
read -r -p "Type ROLLBACK after confirming this revision's image in the change record: " ROLLBACK
test "$ROLLBACK" = ROLLBACK
runbook_require_target
az containerapp update \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --image "$KNOWN_GOOD_IMAGE" --output none
test "$(az containerapp show \
  --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --query 'properties.template.containers[0].image' -o tsv)" = "$KNOWN_GOOD_IMAGE"
}
```

Stop unless `rollback-target-confirmed` prints. Repeat Stages 7's revision
checkpoint, 8, and the signed test in 9. Existing-environment setup preserves
the expected enabled baseline state for those retests. Record the actual image
digest and active revision before releasing the change window. For routing or
token rollback, use the explicit routing or secret rollback procedure.

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
