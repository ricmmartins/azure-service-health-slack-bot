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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
export OPERATIONS_RESOURCE_GROUP="<separate-operations-resource-group>"
export OPERATIONS_LOCATION="<azure-region>"
export OPERATIONS_ACTION_GROUP_NAME="<operations-action-group-name>"
export OPERATIONS_ACTION_GROUP_SHORT_NAME="<short-display-name>"
export OPERATIONS_EMAIL_ADDRESS="<approved-on-call-email-address>"
OPERATIONS_EMAIL_RECEIVER_NAME="primary-on-call-email"

export OPERATIONS_PRIMARY_OWNER="<primary-owner-role-or-alias>"
export OPERATIONS_BACKUP_OWNER="<different-backup-owner-role-or-alias>"
export OPERATIONS_ON_CALL_DESTINATION="email-receiver:primary-on-call-email"
export OPERATIONS_RUNBOOK_URI="https://<approved-runbook>"
}
```

Fail closed before creating anything. The selected AZD environment, its
explicit name, Azure CLI tenant/subscription, and the proposed independent
resource group must all match the intended target:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
```

Azure sends a one-time-passcode request for a new email receiver. Microsoft
documents that an unverified receiver cannot receive alerts or test
notifications after enforcement is active. Have the receiver owner complete
that verification, then send a real Action Group test to the configured
receiver. The CLI requires the receiver definition even though the saved Action
Group already contains it:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
EXPECTED_BASELINE_ENABLED=false
azd env set SERVICE_HEALTH_SECRET_VERSION "" \
  -e "$AZURE_ENV_NAME" --no-prompt
}
```

For production, set `SERVICE_HEALTH_ENVIRONMENT_CLASS=production` and configure
all readiness metadata from the confirmed test before bootstrap can enable the
workload:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
}
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
[Migrate an existing deployment](operations.md#migrate-an-existing-deployment).
The status object is diagnostic and does not contain tenant, subscription, or
resource-group identity, so it must never authorize a mutating lifecycle
command. Stage 6 performs a separate target and uniqueness gate before
bootstrap.

---

[Previous: Stage 3](stage-3.md) | [Next: Stage 5](stage-5.md)
