# Production hardening plan

> **Status:** Validated
>
> **Canonical implementation base:**
> `fa8b5213296607088d56ecc42513e1402951967a`
>
> **Target migration environment:** existing `service-health-mgmt-test` AZD
> environment, with its current subscription and location unchanged
>
> **Execution constraint:** Repository implementation is approved. Azure and
> Slack deployment or mutation remain prohibited in this session.

## Goal and mode

This is an Azure Prepare **MODIFY** workflow for an existing AZD + Bicep
deployment. It removes the production blocker that retains
`SLACK_BOT_TOKEN` in `.azure/<environment>/.env`, passes it to every ARM
deployment, creates a Key Vault version on every provision, and pins Container
Apps to that version.

The hardened contract will:

- transfer the token once through the Key Vault data plane, never through
  AZD/Bicep parameters or deployment history;
- remove it from AZD state immediately after durable transfer;
- use a versionless Container Apps Key Vault reference normally;
- keep later previews, provisions, routing changes, and rotations token-free;
- preserve the active revision, baseline alert, Action Group Secure Webhook, and
  protected API during migration;
- add precise bootstrap, migration, rotation, locking, recovery, and rollback;
- close surgical monitoring, ownership, rollout, supply-chain, test, threat,
  release, and documentation gaps without adding broad features.

## Scope and constraints

| Attribute | Decision |
|---|---|
| Classification | Production hardening |
| Scale | Small central service; potentially many subscription alert paths |
| Budget | Balanced; reuse the current platform and an existing independent operations Action Group |
| Azure target | Existing binding of `service-health-mgmt-test`; exact subscription/location are runtime confirmation gates, not source values |
| Recipe | Existing AZD + Bicep |
| Non-goals | No Service Bus, war rooms, acknowledgement flow, broad application features, or deployment |

No token may appear in source, AZD state after bootstrap, process arguments,
environment variables, temporary files, template parameters, deployment
history, stdout, stderr, exceptions, or support bundles.

## Current findings

- `infra/main.parameters.json` expands `SLACK_BOT_TOKEN`.
- `infra/main.bicep` accepts and forwards a secure token parameter.
- `infra/modules/security.bicep` creates a new secret version and returns
  `secretUriWithVersion`.
- `infra/modules/container-app.bicep` pins that version.
- A direct Key Vault rotation can therefore be overwritten by a later provision.
- Key Vault already has RBAC, private access, soft delete, 90-day retention, and
  purge protection; the runtime identity is already least-privileged.
- Secure Webhook creation and application authorization already fail closed and
  must be preserved.
- Day-2 subscription/MG operations already deploy disabled, signed-test, then
  enable; however, independent operators can race and partial MG expansion has
  no durable recovery journal.
- Application Insights and Log Analytics exist, but actionable alerts, an
  independent operations receiver, and Key Vault/Storage audit diagnostics do
  not.
- Table Storage retains incident communications, subscription IDs, channel IDs,
  and Slack timestamps without an explicit retention decision.
- GitHub Actions and the Python base image use mutable references; CI lacks
  dependency/image vulnerability gates.
- Release tags are `v0.2.1`, Azure template metadata is `0.0.1`, and
  `SECURITY.md` supports `0.2.x`.

## Architecture

The runtime path stays unchanged:

`Service Health Activity Log Alert -> Secure Webhook Action Group -> Container
Apps Easy Auth -> Flask authorization -> Table coordination -> Slack Web API`.

The managed identity resolves:

`https://<vault>.vault.azure.net/secrets/slack-bot-token`

instead of a versioned URI. Azure Container Apps supports versionless Key Vault
references and refreshes a newer version, restarting revisions that consume it
through environment variables. Rotation still explicitly restarts and validates
the revision rather than relying only on the refresh interval.

A new Python CLI owns secret lifecycle operations:

`operator memory -> Azure Key Vault SecretClient -> Key Vault data plane`.

It uses `getpass`, `AzureCliCredential`, and `azure-keyvault-secrets`. The token
is never passed to `az`, `azd`, a child process, a file, or process environment.
SDK body logging stays disabled and errors are redacted against the in-memory
token.

For an operator outside the VNet, the CLI:

1. Confirms tenant, subscription, environment, resource group, vault, app,
   Action Group, and alert identities.
2. Acquires the local AZD lock and cloud operation lock.
3. Adds a temporary vault-scoped `Key Vault Secrets Officer` assignment for the
   current caller.
4. Enables Key Vault public access with `defaultAction=Deny` and only the
   operator-supplied IPv4 `/32`.
5. Writes the secret through the data plane and verifies only nonsecret metadata.
6. Restores the exact prior network configuration and removes only the temporary
   role assignment it created.
7. Removes the exact `SLACK_BOT_TOKEN=` line from AZD state under lock.
8. Runs the token-free workload provision and verifies the resulting reference.

`--private-network` skips the temporary public path when approved private access
already exists.

## Distributed operation lock

Bootstrap, migration, rotation, rollback, and day-2 mutations share one atomic
Blob-create plus finite-lease mutex in a dedicated lock-only storage account.
The private blob contains only nonsecret operation metadata. The isolated
storage key is obtained through the management plane and remains only in Python
memory; it never enters a child-process boundary, journal, or log.

The blob contains schema version, environment, command, random nonce, caller
object ID, start time, and expiry. Atomic create and a renewable 60-second lease
serialize owners. Immediate read-back must prove the nonce. The owner renews and
revalidates before irreversible transitions and deletes only through its lease.

Expired locks are never auto-broken. `recover-lock` requires target
reconfirmation, proof that no relevant ARM deployment or tool process is active,
and explicit confirmation. AZD's `.env.lock` remains the same-workstation guard.

## State transitions

### New environment

| State | Invariant | Next |
|---|---|---|
| `ABSENT` | No central deployment | `INFRA_READY` |
| `INFRA_READY` | Vault, identity, private networking, storage, registry, and observability exist; workload and baseline alert absent | `SECRET_STAGED` |
| `SECRET_STAGED` | Enabled canonical secret version exists; temporary access is closing | `READY_DISABLED` |
| `READY_DISABLED` | AZD is token-free; workload uses versionless URI; baseline alert disabled | `ACTIVE` |
| `ACTIVE` | App, Secure Webhook, signed test, Slack, Table, and monitoring pass | normal operation |

Bootstrap performs:

1. infrastructure-only provision with
   `SERVICE_HEALTH_DEPLOY_WORKLOAD=false`;
2. Key Vault data-plane transfer and local token removal;
3. token-free workload provision with the new baseline alert disabled;
4. explicit application deployment, signed acceptance, and alert activation.

Existing environments default to preserving workload and alert resources. An
absent flag must never imply deletion.

### Existing `service-health-mgmt-test`

| State | Invariant | Next |
|---|---|---|
| `LEGACY_ACTIVE` | Active app uses a versioned URI; AZD contains token | `MIGRATION_STAGED` |
| `MIGRATION_STAGED` | Current state captured; same token written as a new canonical version; app still pinned | `MIGRATION_TOKEN_FREE` |
| `MIGRATION_TOKEN_FREE` | Local token absent; temporary access closed; app still serves | `ACTIVE_VERSIONLESS` |
| `ACTIVE_VERSIONLESS` | Token-free provision uses versionless URI; alert/Secure Webhook preserved | normal operation |
| `ROLLBACK_PINNED` | App deliberately references captured prior version; local token remains absent | repaired versionless state |

The migration CLI proves the tagged app, vault, Action Group, alert, and active
revision before mutation. Plain preview/provision against an unmigrated legacy
contract fails closed.

### Rotation

1. Acquire locks and capture latest enabled version, current URI, active
   revision, Action Group, and alert state.
2. Read replacement through `getpass`.
3. Require Slack `auth.test` to match expected workspace/app/bot identity.
4. Write a new Key Vault version.
5. Explicitly restart the active revision.
6. Require healthy probes, unauthenticated `401`, signed Service Health test,
   Slack delivery, and Table dependency success.
7. Close temporary access and release locks.
8. Revoke the old Slack credential only after an owner-approved checkpoint.

On failure after write, provision the captured prior version through a nonsecret
emergency pin, restart and validate, then enter `ROLLBACK_PINNED`. Never restore
plaintext to AZD.

## Failure semantics

| Failure | Behavior |
|---|---|
| Target/ownership mismatch | Nonzero before lock or mutation |
| Lock contention | Nonzero with only owner/command/expiry metadata |
| Infrastructure phase fails | No token requested or transferred |
| Temporary RBAC/network fails | Restore completed changes; no secret write |
| Token format or Slack identity fails | Clear memory; no Key Vault write |
| Secret write fails | Restore access; keep prior active version |
| Secret succeeds but access cleanup fails | Block workload provision; identify only nonsecret residual IDs |
| AZD token removal fails | Keep app pinned; block phase 2; require secure cleanup recovery |
| Existing phase 2 fails | Keep/restore captured version and prove prior revision/alert |
| New phase 2 fails | Alert stays disabled/absent; retry token-free |
| Acceptance test fails | Do not enable a new alert; migration alert stays on prior verified path |
| Rollback verification fails | Enter `ROLLBACK_INDETERMINATE`; require operator intervention |
| Lock release fails | Leave lock; block future mutations until explicit recovery |

## Security and threat boundaries

- Secret values remain only in Python memory and Key Vault encrypted transport
  and storage.
- Tests use canaries and assert no leak through every process/file/log boundary.
- ARM contains only vault/secret names, versionless URI, and optional nonsecret
  rollback version.
- Runtime identity stays `Key Vault Secrets User`.
- Temporary Secrets Officer access is vault-scoped; existing broader assignments
  are reported but never removed.
- Steady state remains private endpoint only; temporary public access is
  default-deny `/32` and must be proven restored.
- No application secret-administration endpoint is added.
- Purge protection and 90-day retention remain enabled; rollback never
  deletes/recreates the vault.
- Stored Service Health communications and routing metadata require a named
  retention decision before production. Recommend 90 days after last resolved
  update; if automatic pruning is outside this surgical change, the owner must
  explicitly accept and document manual retention review.

## Monitoring and ownership

Require an existing independent operations Action Group with primary owner,
backup owner, on-call destination, and tested receiver. Do not route all
operational alerts through the Slack bot being monitored.

Add:

- Key Vault `AuditEvent` diagnostics;
- Storage Table read/write/delete diagnostics;
- webhook `5xx` alert;
- sustained Slack/Table dependency failure alert;
- `/healthz` availability test and alert;
- supported Container Apps zero-ready-replica/restart alert;
- stale operation-lock operational check/alert;
- severity, window, threshold, owner, and runbook link on every rule.

Document weekly signed canary/channel checks, Azure Monitor retry behavior,
dashboard queries, support-bundle redaction, retention, escalation, rotation,
lock recovery, pinned rollback, and purge-protection recovery.

## Multi-subscription and Management Group safety

Keep one alert path per accessible descendant. Do not claim native MG Activity
Log Alert support.

Harden `manage_alert_scopes.py` to:

- acquire the shared cloud lock for mutations;
- fingerprint discovered membership and compare before every deploy/enable/delete;
- stop on membership drift;
- persist a nonsecret journal of member states for resume/rollback;
- preserve disabled-first, signed-test, enable-last;
- report exact partial/disabled/orphaned resources and idempotent recovery;
- keep `--what-if` nonmutating and include the fingerprint execution must match;
- keep serial mutation rather than add unsafe parallel fan-out.

## Slack production requirements

- Dedicated Slack app with workspace administrator approval.
- `chat:write` only unless an exception is documented.
- Bot invited to every configured channel.
- Primary and backup Slack app owners.
- Slack token rotation remains disabled because this runtime does not implement
  Slack's 12-hour refresh-token flow.
- Recovery copy held in an approved password manager through acceptance, not AZD.
- Every replacement validated with `auth.test`.
- Token-use IP restrictions only if stable Container Apps egress is deliberately
  engineered and proven.
- Quarterly scope, owner, installation, and membership review.

## Files affected

| File | Planned change |
|---|---|
| `.azure/plan.md` | Workspace source of truth and progressive status |
| `azure.yaml` | Preserve Secure Webhook lifecycle; add legacy secret-contract preflight if needed |
| `infra/main.parameters.json` | Remove token; add nonsecret phase, alert, monitoring, and rollback inputs |
| `infra/main.bicep` | Remove secret parameter; two-phase graph and independent operations actions |
| `infra/modules/security.bicep` | Stop secret writes; preserve vault/private endpoint/purge; output versionless URI |
| `infra/modules/container-app.bicep` | Versionless default and optional emergency version pin |
| `infra/modules/observability.bicep`, `infra/modules/operations-monitoring.bicep` | Diagnostics, alerts, independent operations receiver |
| `infra/modules/service-health-alert.bicep` | New baseline remains disabled through acceptance |
| `scripts/manage_slack_token.py` | Bootstrap, migrate, status, rotate, rollback, lock recovery |
| `scripts/operation_lock.py` | Shared lock and nonsecret operation journal |
| `scripts/configure_secure_webhook.py` | Reuse target/provenance checks; reject unsafe legacy path |
| `scripts/manage_alert_scopes.py` | Lock, membership drift, journal, exact recovery |
| `requirements-ops.txt` | Pin Key Vault Secrets SDK outside the runtime image |
| `test/fake_operational_cli.py` | Model Key Vault, lock, RBAC/network, and conflicts |
| `test/test_manage_slack_token.py` | State, secrecy, cleanup, migration, rotation, rollback |
| `test/test_configure_secure_webhook.py` | Legacy/preflight and lifecycle compatibility |
| `test/test_manage_alert_scopes.py` | Contention, drift, partial fan-out, recovery |
| `test/test_cli_subprocess.py` | Entrypoints and process-boundary secrecy |
| `.github/workflows/ci.yml` | Security audit/scan and immutable action pins |
| `.github/dependabot.yml` | Maintain pinned CI/base dependencies |
| `Dockerfile` | Pin production base image by digest |
| `README.md` | Bootstrap, migration, rotation, rollback, ownership runbooks |
| `docs/microsoft-platform-evidence.md` | Versionless, data-plane, network, monitoring, lock evidence |
| `SECURITY.md` | Update supported release line |
| `SUPPORT.md`, `CONTRIBUTING.md` | Sanitized secret-lifecycle and production-change rules |

`.env-example` retains a local-development token only and explicitly excludes
that path from production bootstrap.

## Test matrix

| Area | Required cases |
|---|---|
| Secret absence | No token in Bicep, parameters, AZD output, subprocess args/env, files, errors, stdout/stderr |
| Bootstrap | New phase success/retry; missing vault/secret; alert disabled |
| Migration | Active versioned-to-versionless; no alert/revision disruption; idempotent rerun |
| Cleanup | RBAC, firewall, write, role-removal, AZD cleanup failures |
| Locks | Local/cross-host contention, nonce mismatch, expiry, owner release, stale recovery |
| Rotation | Bad token, identity mismatch, write/restart/test failure, pin rollback, versionless repair |
| Bicep | Infrastructure-only/workload graphs, versionless default, emergency pin, no secret resource |
| Monitoring | Bindings, thresholds, diagnostics, independent receiver |
| Day-2 | Membership drift, concurrent mutation, partial resume, rollback failure |
| Compatibility | Routes, auth, Table schema, Slack rendering, Secure Webhook, outputs |

Validation:

```text
python -m pytest -q
python -m flake8 .
python -m pip_audit -r requirements.txt
az bicep build --file infra/main.bicep --stdout
az bicep lint --file infra/main.bicep
az bicep build --file infra/day2/service-health-alert-scope.bicep --stdout
az bicep lint --file infra/day2/service-health-alert-scope.bicep
docker build -t azure-service-health-slack-bot:ci .
container scan with zero unfixed critical findings
```

Nonproduction acceptance proves token-free AZD, latest enabled secret,
versionless URI, healthy active revision, `401` unauthenticated webhook, signed
test, Slack and Table dependency success, intended alert states, tested
independent monitoring, and absence of temporary RBAC/firewall/lock/journal.

## `service-health-mgmt-test` migration

1. Release `v0.3.0`; name change, Slack, Azure, backup, and rollback owners.
2. Read-only capture tenant/subscription/location/environment/resource IDs,
   pinned secret version, active revision/image, Action Group/Secure Webhook,
   baseline/day-2 states, vault RBAC/network/purge state, and operations receiver.
3. Stop on identity, ownership, topology, policy, or quota mismatch.
4. Run status, dry-run lock, configure independent operations receiver, and
   review Bicep what-if for no deletes/replacements/alert disablement.
5. Freeze AZD, routing, token, and day-2 mutations.
6. Run `manage_slack_token.py migrate --environment-name
   service-health-mgmt-test`.
7. Stage the same credential, remove AZD plaintext, close temporary access, and
   perform token-free versionless provision without disabling the existing alert.
8. Verify revision, Secure Webhook, token absence without displaying the file,
   URI, probes, unauthorized rejection, signed event, Slack, Table, monitoring,
   day-2 inventory, and lock cleanup.
9. Repeat the proven process in production only after explicit deployment
   approval.

## Rollback

1. Freeze mutations and acquire lock.
2. Set the nonsecret emergency pin to the captured previous enabled version.
3. Provision only safe app/monitoring configuration.
4. Restart/reactivate the known-good revision.
5. Verify probes, auth rejection, signed delivery, Slack, and Table.
6. Preserve Secure Webhook and alerts unless evidence proves they changed.
7. Mark `ROLLBACK_PINNED`; block ordinary rotation until complete repair.
8. Release lock only after known-good state is proven.

For a new environment, keep workload alerts disabled and retry phase 2
token-free. Never use vault purge/name reuse as rollback.

## Provisioning and quota impact

No new compute, VNet, private endpoint, registry, or Container Apps environment
is introduced. One Standard LRS lock-only storage account is added beside the
existing application Storage account because ARM management resources do not
provide the required atomic mutex semantics. Other net-new objects are secret
versions, diagnostic settings, a minimal alert set, and one availability test.
Fresh-environment preflight must allow two Storage accounts in total; migration
preflight must allow the one additional lock account and validate alert-rule,
diagnostic-setting, and existing policy limits. No Azure query or deployment
occurs in planning.

## Compatibility and release

- Runtime routes, payloads, Table schema, Slack messages, and Secure Webhook are
  backward-compatible.
- Secret name/environment variable remain compatible.
- Operator workflow is intentionally breaking: existing environments require a
  one-time migration before normal preview/provision.
- Day-2 mutators require management-plane key-list access to the dedicated
  lock-only storage account.
- Emergency rollback is token-free and version-pinned.
- Release as `v0.3.0`; align AZD metadata and security support docs.
- Commits must be authored exclusively by
  `Ricardo Martins <44813563+ricmmartins@users.noreply.github.com>` with no
  Copilot/AI trailer.

## Risks

| Risk | Mitigation |
|---|---|
| Temporary public vault path | Default-deny `/32`, short TTL, exact restore, private-runner option |
| RBAC delay | Bounded retry before secret write |
| Abandoned lock | Pre-lock interactive input, nonce revalidation, conservative lease, read-only status, and explicit confirmed recovery |
| Bad latest version | Slack identity validation, prior version capture, signed acceptance, emergency pin |
| Migration disruption | Same-token staging while app remains pinned |
| MG drift | Fingerprint/recheck, serial mutation, journal |
| Monitoring loop | Independent operations Action Group |
| Incident-data retention | Named owner decision and documented review gate |
| Supply-chain drift | Immutable pins, dependency audit, image scan, Dependabot |
| Purge-protected recreation delay | Never delete as rollback |

## Execution progress

- [x] Plan approved and implementation authorized.
- [x] Canonical base and approved source commits compared without live mutation.
- [x] Reconcile operation-owned temporary RBAC and exact firewall recovery.
- [x] Harden structured lock release, read-only status, and stale recovery.
- [x] Model rollback terminal states without success-shaped uncertainty.
- [x] Require a current reviewed what-if execution fingerprint for day-2 mutation.
- [x] Add production monitoring readiness, unconditional diagnostics, and
  process-boundary secret controls.
- [x] Complete fault-injection coverage and full offline validation.
- [x] Mark this plan Ready for Validation and invoke `azure-validate`.
- [x] Shared target, redaction, nonce-revalidated lock, and durable journal primitives.
- [x] Secret lifecycle CLI and exhaustive failure/secrecy tests.
- [x] Token-free Bicep contract, infrastructure-only state, versionless default,
  emergency pin, disabled-first baseline, and active-image-safe reprovisioning.
- [x] Monitoring diagnostics, availability/dependency/webhook/replica alerts,
  and independent Action Group input.
- [x] Day-2 concurrency, what-if safety, and per-transition Management Group
  membership drift hardening.
- [x] Immutable CI/base-image controls and vulnerability gates.
- [x] Production runbooks, ownership/retention guidance, and platform evidence.
- [x] Repository validation and specialist security review.
- [x] Azure validation handoff invoked; live-bound validation is blocked because
  this isolated worktree has no AZD environment binding.

## Execution order

1. Implement shared target, redaction, lock, and journal primitives.
2. Implement secret lifecycle CLI and exhaustive failure/secrecy tests.
3. Remove token from Bicep/AZD and add two-phase/versionless/pin states.
4. Add monitoring, diagnostics, ownership inputs, and disabled-first baseline.
5. Harden day-2 concurrency and membership drift.
6. Harden CI/supply-chain checks.
7. Rewrite runbooks, Slack requirements, support guidance, and evidence.
8. Validate and obtain specialist security review.
9. Mark workspace `.azure/plan.md` `Ready for Validation` and invoke
   `azure-validate`.
10. Do not deploy; hand off the validated runbook to an approved deployment
    session.

## Section 7: Validation Proof

- [x] All authorized offline validation checks pass.
  - [x] 1. AZD installation: `azd version` returned `1.24.1`.
  - [x] 2. Schema validation: AZD package parsing, Bicep compilation, and
    infrastructure contract tests passed.
  - [x] 3. Environment setup: commit `cb41776d094bf9fe41912d42f8e0e443d176e64f`
    was exported source-only into
    `/home/rmmartins/azure-service-health-validation-cb41776`; the archive
    contained no `.azure` state or secrets. Its validation binding was copied
    only from the previously proven nonsecret binding and contained exactly 17
    allowlisted keys, zero Slack credential keys, and zero token-shaped values.
    The active source binding was never copied or modified.
  - [x] 4-5. Authentication/subscription/location: Azure CLI and AZD auth
    checks passed; the active account matched the binding's tenant/subscription,
    subscription name `Management`, environment `service-health-mgmt-test`, and
    location `East US 2`.
  - [x] 6 and 11. Aspire checks: not applicable; this is a Python project.
  - [x] 7a. Direct ARM preview: `az deployment sub what-if` ran against the
    approved binding and reconciled source `fa8b521`; it returned 34 changes
    (`Create=6`, `Deploy=24`, `Ignore=2`, `Unsupported=2`) and zero deletes.
    The two unsupported evaluations were existing ACR-pull and Table-data role
    assignments whose principal IDs are runtime references. Direct AZD preview
    was intentionally not run because its preprovision hook can mutate Entra and
    AZD state; the direct ARM what-if validated the same Bicep graph without
    executing hooks. Follow-up deployment inventory proved neither what-if name
    was persisted.
  - [x] 7b. Safe AZD preview: from the source-only Linux-native workspace and
    nonsecret validation binding, guarded local hook validation passed with
    `PYTHONPATH=. AZURE_ENV_NAME=service-health-mgmt-test
    SERVICE_HEALTH_READ_ONLY_PREVIEW=true python3
    scripts/configure_secure_webhook.py` and printed read-only validation
    success: `Read-only preview hook validation passed; no state was changed.`
    The exact official command
    `SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd provision --preview --no-prompt
    --environment service-health-mgmt-test` then passed in 25 seconds. AZD
    explicitly reported preview/no changes and `SUCCESS`, targeting subscription
    `Management` (`09f7fca2-63df-4326-b31c-aec3bcbb23db`), location
    `East US 2`, and resource group `rg-service-health-mgmt-test`. It skipped
    the existing resource group, Key Vault, virtual network, and primary
    storage; modified the existing Container App, Container Apps Environment,
    ACR, Application Insights, two private endpoints, and Log Analytics
    workspace; created only the isolated operation-lock storage account
    `stlockhp4t3qa26ya7g`; and reported zero deletes.
  - [x] 8. Build verification: Docker image built successfully as
    `sha256:1ac1693fc19142e3eba6a35cc3a8def5c9c0435bc13efa82618c1068c6aa54e3`.
  - [x] Bicep compilation and lint: central and day-2 graphs passed
    `az bicep build` and `az bicep lint`.
  - [x] 9. AZD package: `azd package --all --no-prompt` completed. AZD 1.24.1
    created a local-only packaging environment automatically; it was removed
    immediately, and final `azd env list --output json` returned `[]`.
    It was not rerun for the preview-safety fix because the command is known to
    create/mutate local AZD environment state, which is prohibited in this turn.
  - [x] 10. Azure Policy validation: read-only subscription and resource-group
    policy assignment queries and subscription exemption query succeeded; each
    returned zero applicable records.
  - [x] Critical image scan: Trivy `0.69.3` found zero unfixed critical
    vulnerabilities.
  - [x] Automated tests: `python -m pytest -q` passed `341` tests.
  - [x] Static lint: `python -m flake8 .` passed.
  - [x] Dependency audit: `pip-audit` reported no known vulnerabilities across
    runtime, operations, and test requirements.
  - [x] Diff integrity: `git diff --check` passed.
  - [x] Credential/leak scan: tracked files contained no unapproved
    credential-shaped material; token canary tests passed.
  - [x] Specialist review: final blocker-only review found no unresolved
    production defect after the atomic Blob lease, recovery, drift,
    monitoring, and secret-boundary fixes.
  - [x] Preview-hook blocker fixed: the explicit
    `SERVICE_HEALTH_READ_ONLY_PREVIEW=true` path resolves only the selected local
    dotenv file, rejects Slack-token entries, validates persisted nonsecret
    deployment/Secure Webhook target values, permits only a target-matching
    `az account show`, and exits before every mutation boundary.
  - [x] Offline tests prove preview-hook fail-closed behavior and zero mutating
    runner calls.
  - [x] Focused security review found one documentation target-confusion risk;
    the runbook now gates the literal preview command to the designated
    `service-health-mgmt-test` migration target.
  - [x] Security recheck found that AZD does not guarantee preview-hook execution;
    the runbook now requires an explicit nonmutating hook validation immediately
    before the exact preview command.
  - [x] Docker and dependency audits were not rerun because this fix changes no
    image, runtime dependency, or operations dependency surface.
  - [x] Environment-bound read-only preview passed without mutation. Before and
    after SHA-256 fingerprints proved the live source `.env` and Linux-native
    validation `.env` were unchanged; the subscription deployment inventory was
    byte-for-byte unchanged. No token was displayed, exported, copied, passed
    through arguments/environment/temp files/logs, or otherwise exposed.

Azure validation completed through a source-only Git archive and a separate
17-key nonsecret binding. The secret-bearing source binding was not copied or
modified.
The read-only policy checks and direct ARM what-if passed with zero deletes.
No Azure, Entra, Slack, deployed resource, or live AZD environment was mutated,
and `azure-deploy` was not invoked. AZD packaging had earlier created one
local-only transient environment despite `--no-prompt`; it was immediately
removed.

The exact AZD preview gate is now satisfied. Guarded local hook validation and
the official AZD preview ran from the source-only Linux-native workspace and
nonsecret binding. Before/after SHA-256 and deployment-inventory comparisons
proved no source-binding, validation-binding, or live deployment change. No
Graph mutation, AZD environment write, ARM deployment, RBAC operation, Key
Vault/Slack access, token exposure, or `azure-deploy` invocation occurred.

The following sequence is retained as historical evidence for that named
validation environment only:

```bash
test "$AZURE_ENV_NAME" = "service-health-mgmt-test" || {
  echo "This reviewed command is only for service-health-mgmt-test." >&2
  exit 1
}
if ! PYTHONPATH=. AZURE_ENV_NAME=service-health-mgmt-test SERVICE_HEALTH_READ_ONLY_PREVIEW=true python3 scripts/configure_secure_webhook.py; then
  echo "Read-only hook validation failed; preview is blocked." >&2
  exit 1
fi
SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd provision --preview --no-prompt --environment service-health-mgmt-test
```

### Clean-room documentation acceptance for `79c890c8`

The approved fresh-adopter validation identified three documentation-only
defects without mutating Azure, Entra, Slack, AZD environments, or deployed
resources:

- the generic README Stage 5 preview command was incorrectly bound to the
  historical validation environment above;
- Stage 4 named the independent operations readiness inputs but did not provide
  a complete create, verify, real-receiver test, freshness, retry, and ownership
  procedure for a new adopter;
- Stage 1 checked quota for one Storage account although the transitive central
  Bicep graph creates the application Storage account and the isolated
  operation-lock Storage account.

The README now uses the adopter's explicit current environment, verifies that
it is selected, and passes the same name to both the read-only hook and AZD
preview. It documents an independent email receiver creation and observed
synthetic delivery before writing the existing production readiness inputs,
while preserving the bot Secure Webhook boundary and separate destructive
approval for operations-receiver decommission. The capacity procedure now
requires two Storage accounts. Documentation contract tests bind these commands
and the documented Storage increment to the transitive Bicep resource graph.
