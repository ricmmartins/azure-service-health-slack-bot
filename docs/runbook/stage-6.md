# Stage 6: provision infrastructure and transfer the Slack token

Prerequisites: the infrastructure-only stage 5 preview was reviewed and
approved.

Run:

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
}
```

Run the lifecycle CLI with the virtual environment's `python`. A system Python
without `requirements-ops.txt` fails before secret persistence with
`ModuleNotFoundError: No module named 'azure.storage'`. If that happens, stop,
activate `.venv`, install both requirements files from Stage 3, confirm
`operations-sdk-ready`, verify `status` still reports `Bootstrapped: false`, and
then retry.

`bootstrap` deliberately repeats the infrastructure-only provision internally
before its hidden prompt, even when the first command above already succeeded.
This is an idempotent safety phase, not another credential transfer. Its final
check proves a provisioned Container App with a ready revision, not signed
Slack/Table acceptance; that evidence comes from Stages 8 and 9 after the real
application image is deployed.

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

If bootstrap stops with `KeyBasedAuthenticationNotPermitted`, a subscription
policy disabled shared-key access on the lock Storage account. Nothing was
written. The CLI then authenticates with your `az login` identity, which needs
the **Storage Blob Data Contributor** role on that account. Grant it, wait a
few minutes for propagation, and rerun only the bootstrap command. This only works when the policy turned off keys but left the account's network reachable:

```text
LOCK_SA="$(az storage account list -g "rg-$AZURE_ENV_NAME" \
  --query "[?tags.\"service-health-purpose\"=='operation-lock'].id | [0]" -o tsv)"
az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --role "Storage Blob Data Contributor" --scope "$LOCK_SA"
```

Check the account with `az storage account show -g "rg-$AZURE_ENV_NAME" -n <lock-account>
--query "{sharedKey:allowSharedKeyAccess,publicNetwork:publicNetworkAccess}"`. If it
also reports `publicNetwork: Disabled` and re-enabling it is reverted, a
subscription policy (typically a Modify effect on corporate subscriptions)
forces the account private, and the central lock is unreachable from outside
the network. Standard subscriptions do not have this restriction. Stop, and
either request a policy exemption for the lock Storage account or deploy in a
subscription without these policies. Nothing was written to Key Vault.

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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
```

Run the machine checkpoint:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
echo "azure-resources-ready" || { echo "Azure resource checkpoint failed." >&2; exit 1; }
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_slack_token.py status \
  --environment-name "$AZURE_ENV_NAME" --json
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
RESOURCE_GROUP="rg-$AZURE_ENV_NAME"
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
echo "bootstrap-cleanup-proven" || { echo "Bootstrap cleanup checkpoint failed." >&2; exit 1; }
}
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

---

[Previous: Stage 5](stage-5.md) | [Next: Stage 7](stage-7.md)
