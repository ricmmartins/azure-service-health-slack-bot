# Stage 9: send the signed Service Health test

Prerequisites:

- the stage 8 checkpoint passed;
- the bot is present in the configured Slack destination;
- the operator can invoke Action Group test notifications.

The Azure CLI test requires the receiver definition even though the Action
Group already exists. The baseline alert must still be disabled; the Action
Group itself must be enabled so the synthetic test can run:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
runbook_load_outputs
test "$(az monitor activity-log alert show \
  --resource-group "$RESOURCE_GROUP" \
  --name "ala-$AZURE_ENV_NAME-service-health" \
  --query enabled -o tsv)" = "${EXPECTED_BASELINE_ENABLED:-false}"
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

SIGNED_TEST_RESULT="$(
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
)" || { echo "Signed test failed." >&2; exit 1; }
SIGNED_TEST_RESULT="$SIGNED_TEST_RESULT" python - <<'PY'
import json
import os

result = json.loads(os.environ["SIGNED_TEST_RESULT"])
data = result.get("properties", result)
actions = [item for item in data.get("actionDetails", [])
           if item.get("Name") == "slack-service-health"
           and item.get("MechanismType") == "SecureWebhook"]
assert data.get("state") in ("Complete", "Completed"), "Test did not complete"
assert len(actions) == 1, "Secure Webhook receiver result missing or ambiguous"
assert actions[0].get("Status") in ("Succeeded", "Completed"), "Receiver failed"
print("signed-test-dispatched")
PY
unset SIGNED_TEST_RESULT
SIGNED_TEST_AT="$(date -u +%s)"
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
runbook_load_outputs
az containerapp logs show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --type console \
  --tail 100
}
```

Checkpoint: deployment is accepted only when the signed test succeeds and the
Slack message appears in the intended channel. After telemetry arrives, use the
[Application Insights queries](operations.md#application-insights-queries) to confirm the
Table and Slack dependencies.

The successful Azure CLI response has `state` equal to `Complete` or
`Completed`, and the Secure Webhook action has `Status: Succeeded`. The
Container App console log must show the signed `POST /api/service-health` with
HTTP `200` and `Service Health incident processed`. Finally, a human must
confirm that the formatted message appeared in the configured Slack channel.
The Azure-owned synthetic subscription and tracking IDs in that message are
expected and are not production incident data.

### Authorize and enable baseline coverage

Bootstrap and image deployment do **not** enable the baseline. Until this
cutover completes, real Service Health events in the central subscription do
not reach the bot. After the signed test above, inspect the logs and
[workspace telemetry](operations.md#application-insights-queries), and have the primary and
backup owners authorize cutover. Run this block in the same child shell within
15 minutes of the successful test. If it is older, repeat Stage 9 first.

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
: "${SIGNED_TEST_AT:?Run the signed Stage 9 test first.}"
test "$(( $(date -u +%s) - SIGNED_TEST_AT ))" -ge 0
test "$(( $(date -u +%s) - SIGNED_TEST_AT ))" -le 900
read -r -p "Type ACCEPTED only after Slack receipt, HTTP 200, Table/Slack telemetry and owner approval: " ACCEPTANCE
test "$ACCEPTANCE" = ACCEPTED
test "$(python scripts/manage_slack_token.py lock-status \
  --environment-name "$AZURE_ENV_NAME" --json |
  python -c 'import json,sys; print(json.load(sys.stdin)["Status"])')" = Unlocked
azd env set SERVICE_HEALTH_BASELINE_ALERT_ENABLED true \
  -e "$AZURE_ENV_NAME" --no-prompt
SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd hooks run preprovision \
  -e "$AZURE_ENV_NAME" --no-prompt
SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd provision --preview \
  -e "$AZURE_ENV_NAME" --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" --no-prompt
read -r -p "Type ENABLE only if preview enables only the baseline, without image/secret changes: " CUTOVER
test "$CUTOVER" = ENABLE
runbook_require_target
runbook_load_outputs
azd provision -e "$AZURE_ENV_NAME" \
  --subscription "$TARGET_SUBSCRIPTION_ID" \
  --location "$AZURE_LOCATION" --no-prompt
test "$(az monitor activity-log alert show \
  --resource-group "$RESOURCE_GROUP" \
  --name "ala-$AZURE_ENV_NAME-service-health" \
  --query enabled -o tsv)" = true
echo "baseline-coverage-enabled"
EXPECTED_BASELINE_ENABLED=true
}
```

The final line is the coverage checkpoint, separate from signed-test acceptance.
If preview is rejected or provisioning fails, keep the change window open and
set `SERVICE_HEALTH_BASELINE_ALERT_ENABLED` back to `false` in this explicit
environment. Review a fresh read-only preview and provision it to verify the
actual alert is disabled before recording rollback. A local AZD value alone
does not change Azure. Never reset `SERVICE_HEALTH_DEPLOY_WORKLOAD` to `false`
on an accepted deployment as a rollback shortcut.

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

---

[Previous: Stage 8](stage-8.md) | [Next: accept the deployment](../deployment-and-operations.md#accept-the-deployment)
