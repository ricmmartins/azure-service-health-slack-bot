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

Stay in the checkout established before Stage 0. Replace every angle-bracket
placeholder; this block does not clone another repository:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo

export TARGET_TENANT_ID="<tenant-id>"
export TARGET_SUBSCRIPTION_ID="<subscription-id>"
export AZURE_LOCATION="<azure-region>"
export AZURE_ENV_NAME="<environment-name>"

runbook_require_profiles
python3 - <<'PY'
import os
import re
import uuid

for key in ("TARGET_TENANT_ID", "TARGET_SUBSCRIPTION_ID"):
    assert str(uuid.UUID(os.environ[key])) == os.environ[key].lower(), key
assert re.fullmatch(r"[a-z0-9][a-z0-9-]{1,18}[a-z0-9]", os.environ["AZURE_ENV_NAME"])
assert re.fullmatch(r"[a-z0-9]+", os.environ["AZURE_LOCATION"])
PY
azd config set auth.useAzCliAuth true
test "$(az cloud show --query name -o tsv)" = AzureCloud
az login --tenant "$TARGET_TENANT_ID"
az account set --subscription "$TARGET_SUBSCRIPTION_ID"
azd auth login --check-status --no-prompt
GRAPH_TENANT="$(az account get-access-token --resource-type ms-graph \
  --query tenant -o tsv)"
test "${GRAPH_TENANT,,}" = "${TARGET_TENANT_ID,,}"

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
}
```

Expected state:

- `tenant` and `subscription` match the values you supplied.
- `isDefault` is `True`.
- The default Microsoft Graph token metadata reports the approved tenant.
  `--subscription` on a Graph request is not a tenant pin. An ARM account
  match alone does not authorize a Graph write. If the metadata check fails,
  stop and reauthenticate only the dedicated profile with
  `az login --tenant "$TARGET_TENANT_ID"`; never clear the corporate profile.
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
}
```

Proceed only when the final line is `deployment-target-pinned`. Register and
check the Container Apps providers only after this checkpoint:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.OperationalInsights --wait
az provider show --namespace Microsoft.App \
  --query '{namespace:namespace,state:registrationState}' -o table
az provider show --namespace Microsoft.OperationalInsights \
  --query '{namespace:namespace,state:registrationState}' -o table
}
```

Checkpoint:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
test "$(az provider show --namespace Microsoft.App \
  --query registrationState -o tsv)" = "Registered" &&
test "$(az provider show --namespace Microsoft.OperationalInsights \
  --query registrationState -o tsv)" = "Registered" &&
echo "azure-context-ready" || { echo "Provider checkpoint failed." >&2; exit 1; }
}
```

Proceed only when the final line is `azure-context-ready`.

Check the regional capacity consumed by this template. These provider usage
endpoints are used directly because the `az quota` extension can temporarily
return an empty result or a stale `Microsoft.Quota` registration error even
after the provider reports `Registered`:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
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
echo "deployment-capacity-ready" || { echo "Capacity checkpoint failed." >&2; exit 1; }
}
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
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
az policy assignment list \
  --scope "/subscriptions/$TARGET_SUBSCRIPTION_ID" \
  --disable-scope-strict-match true \
  --query '[].{name:name,scope:scope,enforcementMode:enforcementMode}' \
  -o table
}
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
