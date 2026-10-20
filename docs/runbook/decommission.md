# Production ownership and decommission

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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
azd env select "$AZURE_ENV_NAME" --no-prompt
TARGET_TENANT_ID="$(azd env get-value AZURE_TENANT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)"
TARGET_SUBSCRIPTION_ID="$(azd env get-value AZURE_SUBSCRIPTION_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)"
DEPLOYMENT_LOCATION="$(azd env get-value AZURE_LOCATION \
  -e "$AZURE_ENV_NAME" --no-prompt)"
az login --tenant "$TARGET_TENANT_ID"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
runbook_require_target
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
DECOMMISSION_RECORD_DIR="$HOME/.local/state/azure-service-health-slack-bot"
mkdir -p "$DECOMMISSION_RECORD_DIR"
chmod 700 "$DECOMMISSION_RECORD_DIR"
DECOMMISSION_RECORD="$DECOMMISSION_RECORD_DIR/decommission-$AZURE_ENV_NAME.json"
python - "$DECOMMISSION_RECORD" \
  "$DECOMMISSION_ENV_NAME" "$DECOMMISSION_TENANT_ID" \
  "$DECOMMISSION_SUBSCRIPTION_ID" "$DECOMMISSION_LOCATION" \
  "$DECOMMISSION_RESOURCE_GROUP" "$DECOMMISSION_API_CLIENT_ID" \
  "$DECOMMISSION_API_OBJECT_ID" "$DECOMMISSION_API_IDENTIFIER_URI" \
  "$DECOMMISSION_API_DISPLAY_NAME" "$DECOMMISSION_REPOSITORY_PATH" \
  "$DECOMMISSION_LOCAL_ENV_PATH" "$DECOMMISSION_PROVENANCE" <<'PY'
import json
import sys
from pathlib import Path

names = (
    "ENV_NAME", "TENANT_ID", "SUBSCRIPTION_ID", "LOCATION", "RESOURCE_GROUP",
    "API_CLIENT_ID", "API_OBJECT_ID", "API_IDENTIFIER_URI", "API_DISPLAY_NAME",
    "REPOSITORY_PATH", "LOCAL_ENV_PATH", "PROVENANCE",
)
record = dict(zip(names, sys.argv[2:], strict=True))
with Path(sys.argv[1]).open("x", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\n")
PY
unset ACTION_GROUP_JSON GRAPH_APP_JSON
echo "decommission-identifiers-retained"
}
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

The private JSON recovery record contains only these nonsecret identifiers,
not the AZD environment or credentials. Preserve it in the approved change
record before deletion. An existing file is a stop condition: do not overwrite
prior provenance. The strict shell discards variables on failure, so this
record, rather than shell history, is the recovery source after a partial
decommission. Start a fresh child shell with existing-environment setup while
local AZD state remains available. Reconfirm the approved tenant and inspect
which Azure resource group, Graph application, and local environment still
exist before resuming only the unfinished phase. Do not rerun the original
inventory after its Action Group has been deleted, and do not repeat a Graph
delete merely because a previous verification failed. If the state cannot be
proven, stop for the primary and backup owners to reconcile the saved record.

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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
read -r -p "Type DELETE after approved peripheral cleanup and central/Entra deletion review: " DELETE_CONFIRMATION
test "$DELETE_CONFIRMATION" = DELETE

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

runbook_require_target
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
}
```

Do not delete Microsoft's AzNS AAD Webhook service principal.
The app deletion checkpoint proves absence from the active Graph application
collection, not permanent erasure. Microsoft Entra normally retains deleted
applications for 30 days. Do not purge the deleted application or service
principal as part of this procedure; permanent deletion requires separate
approval. See [restore a deleted application](https://learn.microsoft.com/entra/identity-platform/howto-restore-app).

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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
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
}
```

The final line must be `decommission-local-credentials-removed`. If another
environment is intentionally retained, remove or rotate its credential before
accepting the decommission checkpoint.
This checkpoint covers the local AZD environment only. The dedicated
`AZURE_CONFIG_DIR` and `AZD_CONFIG_DIR` profiles still contain CLI login state.
After all approved verification is finished, sign out of the dedicated Azure
CLI profile with `az logout` in this child shell, then exit it. Do not sign out
of, clear, or delete the shared corporate profile. Retain or remove the two
dedicated directories only under the workstation credential-retention policy;
never include them in a support bundle.

This deployment enables Key Vault purge protection with a 90-day retention
period. The deleted vault cannot be purged or have its name reused until that
period expires. `azd down --purge` cannot override purge protection. If you need
to redeploy sooner, use a different AZD environment name or follow the Key Vault
recovery procedure and recreate the deleted integrations and role assignments.
