### Stage 5: reconcile Microsoft Entra and preview Azure changes

Prerequisites:

- the stage 4 nonsecret-input checkpoint passed;
- `Application Administrator` is active for the Azure CLI user;
- the operator can create enterprise applications in the target tenant.

The hook reads the active Azure CLI account through `az account show` and can
change Microsoft Entra. Set and verify the Azure CLI subscription again, then
reselect the AZD environment before running it:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
```

Do not run the hook unless the final line is
`entra-mutation-target-confirmed`. Run the project hook explicitly:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
azd hooks run preprovision \
  -e "$AZURE_ENV_NAME" \
  --no-prompt
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
test -n "$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)" &&
test -n "$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID \
  -e "$AZURE_ENV_NAME" --no-prompt)" &&
test -n "$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI \
  -e "$AZURE_ENV_NAME" --no-prompt)" &&
echo "entra-hook-ready" || { echo "Entra output checkpoint failed." >&2; exit 1; }
}
```

Preview the Azure Resource Manager changes:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
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

---

[Previous: Stage 4](stage-4.md) | [Next: Stage 6](stage-6.md)
