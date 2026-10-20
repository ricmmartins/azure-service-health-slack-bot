# Deployment and operations

This is the operator guide for the Azure Service Health Slack Bot. It uses
Bash, Azure CLI, and Azure Developer CLI (`azd`). You do not need to know,
edit, or run Bicep: `azd` uses the repository infrastructure templates
internally.

The exact copy/paste command blocks live in the
[validated command reference](runbook/README.md). This page tells you
which path and command block to use.

## Choose a path

| Goal | Start here |
|---|---|
| Install for the first time | [First deployment path](#first-deployment-path) |
| Continue after a command stopped | [Resume after a stopped stage](#resume-after-a-stopped-stage) |
| Update or operate an existing deployment | [Existing-environment setup](#existing-environment-setup) |
| Add subscriptions or a Management Group | [Manage alert scopes](#manage-alert-scopes) |
| Remove the solution | [Decommission](#decommission) |
| Develop locally | [Local development](#local-development) |

## Prerequisites

### Supported workstation

| Operating system | Supported path |
|---|---|
| Linux | Native Bash 4.4+ |
| macOS | Homebrew Bash 4.4+ |
| Windows | Ubuntu on WSL; keep the checkout under `~/`, not `/mnt/c` |

Native Windows shells and Git Bash are not supported for deployment. Docker
must provide a running Linux container engine.

Install current stable versions of:

- Git;
- Python 3.12 or 3.13;
- Azure CLI;
- Azure Developer CLI (`azd`);
- Docker.

The first workstation check prepares the internal template tooling
automatically. Do not install or invoke Bicep separately.

### Information and access

Have these ready before starting:

- tenant ID, subscription ID, Azure region name (for example astus2), and a new AZD environment name (a short label for this deployment, such as service-health-prod; see [Stage 1](runbook/stage-1.md));
- Azure `Owner`, or `Contributor` plus `User Access Administrator`;
- temporary Microsoft Entra `Application Administrator`;
- a dedicated Slack app with `chat:write`, token rotation disabled, an
  `xoxb-` bot token, workspace ID, and destination channel ID;
- an independent operations Action Group receiver;
- primary and backup owners, on-call destination, and an HTTPS operations
  runbook;
- your public IPv4 unless the operator already has an approved private path to
  Key Vault.

Never place the Slack token in a command, environment variable, AZD value,
tracked file, issue, or chat. The supported lifecycle command asks for it
through a hidden prompt.

## First deployment path

Use one child Bash from setup through acceptance. Each stage is a stop/go
checkpoint: stop on an error and do not skip ahead.

1. **Open the deployment shell and checkout.**
   - New clone: [Fresh checkout](#fresh-checkout).
   - Existing clone: [Existing checkout](#existing-checkout).
2. **Validate the workstation.**
   Run [Stage 0](runbook/stage-0.md#stage-0-verify-the-workstation).
3. **Pin the Azure target.**
   Run [Stage 1](runbook/stage-1.md#stage-1-pin-the-azure-and-azd-deployment-target).
4. **Create the Slack app.**
   Complete [Stage 2](runbook/stage-2.md#stage-2-create-and-authorize-the-slack-app).
5. **Install dependencies.**
   Run [Stage 3](runbook/stage-3.md#stage-3-install-dependencies-and-validate-the-routing-file).
6. **Load nonsecret configuration and test the independent receiver.**
   Run [Stage 4](runbook/stage-4.md#stage-4-load-nonsecret-inputs).
7. **Review the identity and Azure preview.**
   Run [Stage 5](runbook/stage-5.md#stage-5-reconcile-microsoft-entra-and-preview-azure-changes).
8. **Provision infrastructure and transfer the Slack token.**
   Run [Stage 6](runbook/stage-6.md#stage-6-provision-infrastructure-and-transfer-the-slack-token).
9. **Build and deploy the application.**
   Run [Stage 7](runbook/stage-7.md#stage-7-build-and-deploy-the-application).
10. **Verify health and authentication.**
    Run [Stage 8](runbook/stage-8.md#stage-8-verify-probes-and-the-authentication-boundary).
11. **Send the signed test and accept the deployment.**
    Follow [Accept the deployment](#accept-the-deployment): signed test, then
    [authorize baseline coverage](#authorize-and-enable-baseline-coverage).

Do not replace this sequence with a direct `azd up`. The staged path keeps the
Slack token out of ARM/AZD state and leaves real alerts disabled until the
signed test succeeds.

### Fresh checkout

Start a child shell:

```console
bash --noprofile --norc
```

Then copy the single
[fresh-checkout block](runbook/00-setup.md#fresh-checkout). If its
destination already exists, use [Existing checkout](#existing-checkout);
do not delete or overwrite it.

### Existing checkout

Start a new child Bash and copy the
[existing-checkout block](runbook/00-setup.md#existing-checkout). Change
only `REPOSITORY_PATH` if your checkout uses another local path. The dedicated
Azure CLI and AZD profile paths must remain unchanged.

## Accept the deployment

### Send the signed Service Health test

Run the [signed test command](runbook/stage-9.md#stage-9-send-the-signed-service-health-test).
The deployment is not accepted until:

- the Action Group test reports a successful Secure Webhook action;
- the Container App records an authenticated HTTP `200`;
- Table and Slack dependency telemetry is present;
- the formatted message appears in the intended Slack channel.

The test uses Azure-owned synthetic incident and subscription values. It proves
authenticated delivery, not subscription-specific routing.

### Authorize and enable baseline coverage

After the signed test, follow the
[baseline cutover](runbook/stage-9.md#authorize-and-enable-baseline-coverage).
Real Service Health delivery remains disabled until the explicit acceptance and
preview gates pass.

## Resume after a stopped stage

Do not skip forward. Restore the [existing checkout](#existing-checkout). If
the AZD environment or Azure resources already exist, use
[existing-environment setup](#existing-environment-setup), then repeat only
the failed stage using its **Recovery** guidance. Do not repeat `git clone`,
`azd env new`, or token bootstrap for an existing environment.

## Manage alert scopes

The first deployment covers one subscription. Additional coverage is a day-2
operation and does not require redeploying the central application.

| Current state | Goal | Command |
|---|---|---|
| Central deployment only | Add another subscription | `add-subscription` |
| No overlapping individual alerts | Add accessible descendants of a Management Group | `add-management-group` |
| Descendants already have individual alerts | Move them under Management Group management | `migrate-to-management-group` |
| Leave Management Group management | Create individual replacements first | `migrate-from-management-group` |
| Alternate coverage already exists | Remove Management Group-owned paths | `remove-management-group` |

Management Groups are logical input only. The CLI creates one Activity Log
Alert and Action Group path per accessible descendant subscription.

Run the [validated scope procedure](runbook/scopes.md#manage-alert-scopes).
Every mutation requires a fresh reviewed `--what-if --json` fingerprint.

## Operations

### Existing-environment setup

Every new operations terminal starts with the
[existing-environment setup block](runbook/operations.md#existing-environment-setup).
It restores the approved tenant, subscription, region, AZD environment,
virtual environment, and resource names before any mutation.

### Update routing

Use [Update routing](runbook/operations.md#update-routing). Invite the bot to
the destination channel before provisioning the new routing configuration.

### Rotate the Slack token

Use [Rotate the Slack token](runbook/operations.md#rotate-the-slack-token).
The command receives the replacement token through a hidden prompt and validates
the new revision before completing.

### Application Insights queries

Use the
[Application Insights queries](runbook/operations.md#application-insights-queries)
for request, dependency, and Service Health processing evidence.

### Recover an abandoned operation lock

Use [lock recovery](runbook/operations.md#recover-an-abandoned-operation-lock)
only after proving that no deployment or operational command is still active.

### Roll back application code

Use [Roll back application code](runbook/operations.md#roll-back-application-code)
to select a known-good revision image, then repeat probes and the signed test.

## Troubleshooting

Start with the
[troubleshooting table](runbook/operations.md#troubleshooting). The most
common first-run cases are:

| Symptom | Action |
|---|---|
| Checkout destination already exists | Use [Existing checkout](#existing-checkout); do not clone over it. |
| Internal template tooling is absent | Rerun Stage 0; it installs and validates the tested tooling automatically. |
| `runbook_require_repo: command not found` | The terminal was restarted and lost its state. Follow [Resume after a stopped stage](#resume-after-a-stopped-stage), then reload your values as in [Stage 3 recovery](runbook/stage-3.md). |
| Docker is unavailable | Start the Linux container engine and verify `docker info`. |
| A command stopped | Follow [Resume after a stopped stage](#resume-after-a-stopped-stage). |
| Signed test succeeds but Slack is empty | Verify token, `chat:write`, channel ID, and bot membership. |
| Preview targets the wrong tenant or subscription | Stop and rerun Stage 1 with the approved IDs. |

## Decommission

Use the [validated decommission procedure](runbook/decommission.md#decommission).
It inventories day-2 coverage, confirms the exact target, removes Azure
resources, verifies the protected Entra application identity before deletion,
and removes local AZD state separately.

Key Vault purge protection prevents immediate name reuse. A later clean
deployment must use a new AZD environment name.

## Local development

Use the [local development procedure](local-development.md#local-development).
Local development may use a `.env` file; production secret transfer must use
the hidden Key Vault lifecycle prompt.

## Architecture

Azure Monitor sends Service Health Common Alert Schema to a Microsoft
Entra-authenticated Container Apps webhook. The application validates the
caller, routes and deduplicates the event, stores incident state in Azure Table
Storage, and creates or updates the Slack incident thread.

See the [architecture reference](design.md#architecture) and the
[architecture diagram](../img/architecture-flow.svg).

## Security model

- Secure Webhook authentication plus application-level caller, audience, and
  app-role checks;
- managed identity for Key Vault, Storage, and image pull;
- private Key Vault and Table Storage data paths;
- hidden Slack token transfer directly to Key Vault;
- disabled alerts until signed acceptance;
- target, ownership, preview, fingerprint, and operation-lock guards.

See the [complete security model](design.md#security-model).

## Known limits

- Slack and Table Storage cannot provide exactly-once delivery across both
  systems.
- Signed tests use synthetic Azure values and do not prove
  subscription-specific routing.
- Management Group expansion covers only descendants visible to the operator.
- The Tenant Root Group is rejected when it overlaps the central baseline.
- Only one mutating deployment or operation may run per environment at a time.

See [all known limits](design.md#known-limits).

## Tests

The repository validates application behavior, operational CLIs, the published
Bash command blocks, Bicep templates used internally by `azd`, the Docker image,
and documentation contracts. Run the
[test commands](runbook/99-acceptance-and-tests.md#tests).

Repository tests cannot prove permissions, policies, quotas, regional
availability, Entra propagation, or Slack receipt in your tenant. Stages 8 and
9 are the required live acceptance tests.
