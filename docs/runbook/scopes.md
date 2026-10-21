# Manage alert scopes

## Manage alert scopes

The initial deployment creates one alert for its subscription and leaves it
disabled until the Stage 9 cutover. Complete
[existing-environment setup](operations.md#existing-environment-setup) before every new
operations session, then pin the central environment before every day-2 command:

You do not need to decide between individual subscriptions and a Management
Group during the initial deployment. Start with the central subscription, add
independent subscriptions as needed, and migrate them to a non-overlapping
Management Group later without a coverage gap.

### Choose the scope command

| Current state | Goal | Command |
|---|---|---|
| Central deployment only | Add a second, third, or other independent subscription | `add-subscription` |
| No managed descendant overlaps | Add all currently accessible descendants of a Management Group | `add-management-group` |
| One or more descendants already have individual managed alerts | Replace those individual paths with Management Group-managed paths | `migrate-to-management-group` |
| An uncovered subscription was added to an already managed Management Group | Reconcile membership and create the missing subscription-scoped path | Rerun `add-management-group` |
| A newly added descendant already has an individual managed alert | Move that alert under the existing Management Group-managed scope | Rerun `migrate-to-management-group` |
| Management Group coverage should become individual coverage | Create, test, and enable individual replacements before retiring the group path | `migrate-from-management-group` |
| Management Group coverage is no longer required and alternate coverage already exists | Remove the group-owned paths | `remove-management-group` |

Management Group membership changes do not create Azure Monitor resources by
themselves. Rerun the appropriate preview and execution after adding or moving
a subscription in the Azure hierarchy. `add-management-group` repairs missing
uncovered descendants; use `migrate-to-management-group` instead when a new
descendant overlaps an existing individual managed alert.

Each logical Management Group remains a day-2 input only. The CLI enumerates
its visible descendants and deploys one Activity Log Alert and Action Group
path per subscription. You can therefore adopt Management Group management
after operating with one or more individual subscriptions; rebuilding the
central deployment is not required.

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
AZURE_LOCATION="$(azd env get-value AZURE_LOCATION -e "$AZURE_ENV_NAME" --no-prompt)"
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
runbook_require_target
}
```

Stop unless `day2-target-confirmed` prints. If Azure CLI is signed into another
tenant, run `az login --tenant "$TARGET_TENANT_ID"` and repeat the complete
check. List coverage or add a subscription with the environment name supplied
explicitly:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_alert_scopes.py list \
  --environment-name "$AZURE_ENV_NAME"
python scripts/manage_alert_scopes.py add-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
}
```

Review the preview and replace the fingerprint before pasting execution:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_alert_scopes.py add-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fingerprint-from-reviewed-what-if>"
}
```

The execution creates the target resource group, Action Group, and disabled
Activity Log Alert, sends the signed Secure Webhook test, and enables the alert
only after that test succeeds. To remove the temporary or retired path, obtain
and review a fresh destructive fingerprint, then execute the exact reviewed
operation:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_alert_scopes.py remove-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
}
```

After review:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_alert_scopes.py remove-subscription \
  --subscription-id "00000000-0000-0000-0000-000000000000" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fresh-reviewed-fingerprint>"
}
```

Management Group commands expand accessible descendants into managed
subscription alert paths. Choose **one** operation from the table above;
do not run all migration and removal commands in sequence:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
SCOPE_COMMAND="add-management-group"
MANAGEMENT_GROUP_ID="<management-group-id>"
case "$SCOPE_COMMAND" in
  add-management-group|migrate-to-management-group|migrate-from-management-group|remove-management-group) ;;
  *) echo "Choose a supported Management Group operation." >&2; exit 1 ;;
esac
test -n "$MANAGEMENT_GROUP_ID"
[[ "$MANAGEMENT_GROUP_ID" != *'<'* ]]
python scripts/manage_alert_scopes.py "$SCOPE_COMMAND" \
  --management-group-id "$MANAGEMENT_GROUP_ID" \
  --environment-name "$AZURE_ENV_NAME" --what-if --json
}
```

Review that one operation, then supply its fresh fingerprint:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
python scripts/manage_alert_scopes.py "$SCOPE_COMMAND" \
  --management-group-id "$MANAGEMENT_GROUP_ID" \
  --environment-name "$AZURE_ENV_NAME" \
  --execution-fingerprint "<fresh-reviewed-fingerprint>"
}
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
reviewed `--what-if --json` result. The fingerprint must be used to start the
execution within 15 minutes and binds the tenant, subscription, environment, command parameters, Management
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

### Recipients for added subscriptions

Every added subscription sends its Service Health alerts only to the bot
through a Secure Webhook. Choose the Slack destination with the routing rules
(`subscription_ids`, `services`, `regions`), not with the scope commands:
the scope commands create and remove Azure alert resources and never change the
destination. Day-2 Action Groups have no email receiver. Email notifications
for platform operations are configured once on the central operations Action
group, not per subscription. To receive Service Health email for an added
subscription, create a separate Action Group with an email receiver in the
Azure portal; the bot does not manage it.
