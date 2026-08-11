# Implementation and validation plan

This document records the intended repository contract without tenant-specific
identifiers or dated deployment output. Microsoft platform claims are mapped to
official sources in
[`docs/microsoft-platform-evidence.md`](../docs/microsoft-platform-evidence.md).

## Objective

Deliver Azure Service Health notifications to Slack through an authenticated
Azure Monitor Secure Webhook while preserving incident state across retries,
updates, and concurrent Container App replicas.

The implementation must:

- accept Azure Monitor Common Alert Schema Service Health payloads;
- authenticate the Azure Monitor AzNS caller and required app role;
- route incidents to configured Slack channel IDs;
- keep one Slack root message per subscription and tracking ID;
- update the root and add a broadcast thread reply for newer notifications;
- suppress stale and duplicate notifications;
- use managed identity for Azure resource access;
- keep Key Vault and Table Storage data paths private;
- expose liveness and readiness probes;
- provide tested day-2 subscription and Management Group fan-out commands.

## Scope

The project targets Azure public cloud. It uses public-cloud private DNS
suffixes and the documented public-cloud AzNS application identity.

The central AZD deployment creates:

- a resource group;
- a VNet, subnets, private endpoints, and private DNS links;
- Log Analytics and workspace-based Application Insights;
- Key Vault with RBAC, soft delete, purge protection, and a versioned Slack
  token secret;
- a StorageV2 account with shared-key access disabled and a Table;
- Azure Container Registry;
- a user-assigned managed identity and direct role assignments;
- a Container Apps environment and Container App;
- a Global Action Group with a Secure Webhook receiver;
- a baseline subscription Service Health Activity Log Alert.

The day-2 command can create tagged peripheral resource groups in other
subscriptions. Those resources are not part of the central AZD resource group.

## Trust and identity

### Deployment identity

The operator needs:

- Azure resource permissions sufficient to create the template resources and
  role assignments, typically `Owner` or `Contributor` plus
  `User Access Administrator` at the target subscription;
- Microsoft Entra `Application Administrator` while the Secure Webhook
  preprovision hook configures the protected API relationship.

### Runtime identity

The Container App uses one user-assigned managed identity. The central
deployment assigns:

- `AcrPull` on the registry;
- `Key Vault Secrets User` on the vault;
- `Storage Table Data Contributor` on the storage account.

Production and staging use `ManagedIdentityCredential` with
`AZURE_CLIENT_ID`. Development uses `DefaultAzureCredential`.

### Webhook identity

Container Apps authentication validates Microsoft Entra v2 tokens for the
protected API audience and restricts the client application to the Azure
Monitor AzNS AAD Webhook service principal. Flask then checks:

- the Easy Auth principal header exists and decodes;
- the caller application is AzNS;
- the audience matches the configured protected API;
- the `ActionGroupsSecureWebhook` app role is present.

Public health probes use anonymous ingress. The protected webhook enforces its
own `401` and `403` responses.

## Event processing contract

1. Validate authentication and Common Alert Schema.
2. Require `eventSource = ServiceHealth`.
3. Resolve the subscription ID and tracking ID.
4. Normalize the platform lifecycle into the application's active, updated, or
   resolved presentation.
5. Select the highest-priority matching route.
6. Acquire the Table entity through ETag and lease coordination.
7. Ignore a duplicate or stale submission watermark.
8. Create the first Slack root, or update the existing root and add a broadcast
   reply.
9. Persist the accepted state and Slack timestamps.

`Updated` is an application label for a newer accepted nonterminal
notification. It is not a claim that Azure emits only three Service Health
stages.

## Consistency boundary

Slack and Table Storage cannot be committed atomically. The implementation
reduces duplicate work with ETags, leases, and submission watermarks, but keeps
two explicit failure windows:

- Slack accepts an initial root and the process stops before the first state
  checkpoint, leaving an untracked root;
- Slack accepts a thread reply and the process stops before the final
  checkpoint, allowing a duplicate reply on replay.

These are bounded implementation risks, not exactly-once guarantees.

## Network contract

Key Vault and Storage public network access are disabled. Private endpoints and
private DNS provide the Container Apps data path. The Container App ingress,
Container Registry endpoint, Azure Monitor ingestion, and Slack endpoint remain
public endpoints.

Readiness verifies configuration and SDK client construction. It does not issue
a Table data-plane request. End-to-end verification therefore requires an
authenticated test notification and dependency telemetry.

## AZD lifecycle

`azure.yaml` registers `scripts/configure_secure_webhook.py` as a Python
`preprovision` hook. The hook creates or reconciles:

- the protected API application and service principal;
- the v2 token version and identifier URI;
- the `ActionGroupsSecureWebhook` application role;
- the Azure Monitor AzNS service principal;
- verified application owners;
- the AzNS app-role assignment;
- AZD environment values consumed by Bicep.

The hook is idempotent and fails closed on conflicting existing configuration.
Run it explicitly before the first `azd provision --preview` because the
preview needs its output values. This is a repository requirement; Microsoft
does not document a general guarantee that preview executes lifecycle hooks.

The Slack token is hidden during interactive input and stored as plaintext in
the selected local AZD environment because the target vault does not exist
before provisioning. The expanded value is also passed to the local AZD
process. Microsoft recommends secret references instead of plaintext AZD
environment values. A two-phase or external-vault design is outside the current
implementation. The local environment file must be protected and must not be
committed, copied, or printed into logs.

## Day-2 scope management

The command supports discovery, subscription add and remove, Management Group
fan-out, migration, `--what-if`, and JSON output.

Before mutation it verifies:

- discovery resolves one central environment;
- the target tenant matches the central tenant;
- required management-plane operations are effective;
- a Management Group target is visible and can be enumerated;
- every intended subscription is a proven descendant;
- coverage gaps are not introduced;
- resources selected for removal carry the expected ownership tags.

New alert paths are deployed disabled. The command invokes Azure Monitor's
signed Service Health test and enables the alert only after an explicit
accepted result. `--force` supplies confirmation for preapproved automation; it
does not bypass safety checks.

A Management Group command creates one managed subscription path for each
accessible descendant. This behavior is implemented by the repository and is
not described as a native Management Group Activity Log Alert.

## Cleanup contract

Cleanup has three separate surfaces:

1. Day-2 peripheral resource groups must be inventoried and deliberately
   removed in their target subscriptions. They are outside the central AZD
   resource group.
2. `azd down` removes the central environment resources according to Azure
   deletion behavior.
3. The project-created protected API app registration must be deleted
   separately. The Microsoft-owned AzNS service principal must not be deleted.

Key Vault purge protection is enabled with a 90-day retention period. A deleted
vault remains a recoverable platform object and its name cannot be reused until
retention expires. `azd down --purge` cannot override that setting. Recovery
does not restore deleted role assignments or integrations.

## Validation

Repository validation consists of:

```bash
python -m pytest -q
python -m flake8 .
az bicep build --file infra/main.bicep --stdout
az bicep lint --file infra/main.bicep
az bicep build --file infra/day2/service-health-alert-scope.bicep --stdout
az bicep lint --file infra/day2/service-health-alert-scope.bicep
docker build -t azure-service-health-slack-bot .
```

Deployment acceptance also requires:

- healthy `/healthz` and ready `/readyz` responses;
- an unauthenticated webhook request rejected with `401`;
- a signed Azure Monitor Service Health test accepted by the application;
- a correctly formatted Slack message in the selected channel;
- a successful Table dependency visible in telemetry;
- the intended managed identity, role assignments, private endpoints, and
  disabled public data-plane access;
- duplicate and newer-notification behavior matching the automated tests.

Live platform response values recorded during maintenance are empirical
observations. They must be labeled as such and not promoted to general
Microsoft guarantees.

## Maintenance rules

- Keep the README focused on clean installation and operator tasks.
- Keep the central deployment sequence gated by prerequisites, expected state,
  a checkpoint, and focused recovery for every stage.
- Add a Microsoft source to the evidence record when a platform-sensitive claim
  changes.
- Put repository-specific behavior next to its implementation or test evidence.
- Label deployed observations as empirical.
- Do not store tenant IDs, subscription IDs, object IDs, app IDs, endpoint
  hostnames, Slack tokens, or other environment-specific values in this file.
- Do not claim exactly-once Slack delivery or immediate Key Vault name reuse.
