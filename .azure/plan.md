# Azure Service Health Slack Bot — Delivery and Deployment Record

## Summary

Standalone Flask service that receives Azure Service Health alerts (Common
Alert Schema) via a Secure Webhook Action Group and posts/updates a single
threaded Slack message per subscription + tracking ID through the incident
lifecycle (Active → Updated → Resolved).

This repository was extracted and adapted from a prior combined
Slack-Bolt/support-ticket/service-health implementation. It intentionally
excludes:

- Slack Bolt app and inbound Slack events (`/slack/events`)
- Azure support-ticket code, handlers, and support datasets
- Slack app manifest
- `Reader` and `Support Request Contributor` RBAC roles

The `service_health/` package (parser, routing, auth, storage, Slack
notifier, processor, runtime bootstrap, telemetry) was reused verbatim — it
was already self-contained and had zero dependency on the excluded code.

## Architecture

- **Compute**: Azure Container Apps (single container, Gunicorn, non-root,
  0.5 vCPU / 1Gi, HTTP autoscale 1–3 replicas)
- **Identity**: user-assigned managed identity with exactly three role
  assignments:
  - `AcrPull` on the Container Registry
  - `Key Vault Secrets User` on the Key Vault
  - `Storage Table Data Contributor` on the Storage Account
- **Secrets**: Slack bot token stored in Key Vault, mounted into the
  Container App as a Key Vault secret reference (never a shared key)
- **State**: Azure Table Storage, `PartitionKey` = subscription ID,
  `RowKey` = SHA-256(trackingId), ETag + lease-based idempotency
- **Auth**: Container Apps Easy Auth (Entra ID) validates the token from the
  official AzNS Secure Webhook application (`461e8683-5575-4561-ac7f-899cc907d62a`)
  and requires the `ActionGroupsSecureWebhook` app role and configured
  audience; `/healthz` and `/readyz` remain publicly reachable
  (`unauthenticatedClientAction: AllowAnonymous`)
- **Observability**: Application Insights via `azure-monitor-opentelemetry`
  (requests, dependencies, exceptions, custom counters)
- **Registry**: Azure Container Registry (SKU parameterized, default `Basic`;
  admin user disabled, anonymous pull disabled — override to `Standard`/
  `Premium` via the `acrSkuName` Bicep parameter if your subscription policy
  rejects the cheaper SKUs)
- **Networking**: Key Vault and Storage are provisioned with
  `publicNetworkAccess: 'Disabled'` and reached only via Private Endpoints on
  a dedicated VNet (`infra/modules/network.bicep`), which also provides the
  Container Apps environment's VNet integration subnet. The Container App's
  own public ingress is unaffected — only egress to KV/Storage is routed
  privately.
- **Alerting source**: Activity Log Alert + Secure Action Group
  (`infra/modules/service-health-alert.bicep`), isolated behind
  `scripts/manage_alert_scopes.py` so subscriptions and Management Groups can
  be managed after deployment without reprovisioning the central runtime

## Scope exclusions

- No Slack Bolt / signing secret / inbound events
- No Azure support-ticket workflow (`azure-mgmt-support` not a dependency)
- No `support-rbac.bicep` module (Reader, Support Request Contributor)

## Repository layout

```
app.py                          Minimal Flask entrypoint (3 routes only)
service_health/                 Parser, routing, auth, storage, Slack, runtime, telemetry
infra/main.bicep                Subscription-scope orchestration
infra/modules/                  registry, security, storage, container-app, observability, service-health-alert
scripts/configure_secure_webhook.py    Idempotent Entra app registration/role setup
scripts/manage_alert_scopes.py         Tenant-bound day-2 subscription/MG alert manager
infra/day2/                            Reusable peripheral alert deployment entry point
config/service_health_routes.example.json
test/                           Parser/routing/auth/storage/processor/Slack/app/bootstrap tests
.github/workflows/ci.yml        pytest+flake8, bicep build/lint, docker build
```

## Initial validation proof

All commands were executed directly in this repository during this session.

| Check | Command | Result |
|---|---|---|
| Python tests | `python -m pytest -q` | **32 passed** in 8.93s |
| Python lint | `python -m flake8 .` | **0 findings** |
| Bicep compile | `az bicep build --file infra/main.bicep --stdout` | **Compiled successfully** to ARM JSON |
| Bicep lint | `az bicep lint --file infra/main.bicep` | **0 warnings/errors** |
| Docker build | `docker build -t azure-service-health-slack-bot:validate .` | **Build succeeded**, multi-stage, final image based on `python:3.13-slim-bookworm` |
| Non-root check | `docker exec shb-smoke whoami` | **`app`** (non-root) |
| Gunicorn smoke test | `curl http://localhost:18080/healthz` | **HTTP 200** `{"status":"healthy"}` |
| Gunicorn smoke test | `curl http://localhost:18080/readyz` | **HTTP 200** `{"status":"ready"}` |
| Container logs | `docker logs shb-smoke` | Clean gunicorn startup logs only — **no payload or secret values logged** |
| AZD packaging | `azd package --no-prompt` | **SUCCESS** — image tagged `azure-service-health-slack-bot/app-<env>:azd-deploy-<ts>` |

These initial checks intentionally stopped before deployment. Live deployment
validation and the resulting corrections are recorded below.

## Post-merge revalidation (2026-08-06)

After PR #1 merged to `main`, the full validation suite was re-run from a
clean checkout of `main` to confirm nothing regressed and to prep the README
deployment guide:

| Check | Command | Result |
|---|---|---|
| Python tests | `python -m pytest -q` | **32 passed** |
| Python lint | `python -m flake8 .` | **0 findings** |
| Bicep compile | `az bicep build --file infra/main.bicep --stdout` | **Compiled successfully** |
| Bicep lint | `az bicep lint --file infra/main.bicep` | **0 warnings/errors** |
| Docker build | `docker build -t azure-service-health-slack-bot:validate2 .` | **Build succeeded** |
| Non-root check | `docker exec shb-smoke2 whoami` | **`app`** |
| Gunicorn smoke test | `curl /healthz`, `curl /readyz` | **HTTP 200** on both |
| Container logs | `docker logs shb-smoke2` | Clean — no payload/secret logging |
| AZD packaging | `azd package --no-prompt` | **SUCCESS** |
| CI on `main` | `gh run view` (push trigger) | **All 3 jobs green**: Python tests/lint, Bicep build/lint, Docker build |
| CI on PR #1 | `gh run list` | **Green** (merged after passing) |

This post-merge pass also stopped before deployment. Following it, `README.md`
was expanded with the step-by-step deployment guide that was later exercised
against real WSL, Azure, and Slack environments.

## Test coverage

- `test/test_service_health.py` — Common Alert Schema parsing (native and
  escaped `impactedServices`, numeric/text `level`, Active/Updated/Resolved
  normalization), routing precedence and fallback channel, Easy Auth/app-role
  validation, Table Storage ETag/lease idempotency (`CREATE`/`UPDATE`/
  `DUPLICATE`/`STALE`/`BUSY`), processor error mapping, Slack message
  rendering and transient/permanent error classification, Flask blueprint
  endpoint behavior.
- `test/test_app.py` — minimal Flask app: exactly three routes registered,
  `/healthz` and `/readyz` public and healthy, `/api/service-health` content
  type validation, Slack client constructed from environment, runtime lazily
  created and cached.
- `test/test_bootstrap.py` — `service_health.runtime.create_service_health_runtime`:
  `DefaultAzureCredential` used outside production, `ManagedIdentityCredential`
  used in production/staging, `AZURE_CLIENT_ID` required in production,
  `AZURE_TABLE_ENDPOINT` required.

## Known limitations (documented, not defects)

- There is an unavoidable crash window between a successful first
  `chat.postMessage` and persisting `messageTs` to Table Storage. Recovery is
  manual (see README Operations section). A transactional queue would close
  this window but is out of scope for this MVP.

## First live infrastructure validation (2026-08-06)

A real end-to-end deployment was executed in an internal Microsoft dev/test
subscription to prove the Azure infrastructure path, using nonfunctional test
Slack configuration. No subscription IDs, resource names, tenant details, or
credentials are retained in this record. This uncovered and fixed three real
bugs, plus one environment-specific accommodation:

1. **The original setup command silently failed on Windows.**
   Its inline `az rest --body <jsonstring>` boundary corrupted embedded quotes
   and did not propagate the child-process exit code, so it reported success
   while leaving `identifierUri="api://"` (empty app ID). The canonical Python
   implementation writes JSON to a temporary UTF-8 file, passes
   `az rest --body @tempfile`, and checks every subprocess result.
2. **`SERVICE_HEALTH_ROUTES_JSON` parameter substitution was broken as
   documented.** `azd` substitutes `${VAR}` into `infra/main.parameters.json`
   as raw text *before* parsing it as JSON, so a JSON-valued env var
   containing quotes corrupts the parameter file. Fixed by passing the
   routing config as base64 (`SERVICE_HEALTH_ROUTES_JSON_B64`, decoded in
   `main.bicep` via `base64ToString(...)`) — README updated accordingly.
3. **Key Vault and Storage got `publicNetworkAccess` forced to `Disabled`**
   by tenant policy regardless of the Bicep-requested `'Enabled'` value,
   which broke the Container App's managed-identity secret/table access.
   Fixed by adding `infra/modules/network.bicep` (VNet + two subnets) with
   Private Endpoints + Private DNS Zones for Key Vault and Table Storage,
   and VNet-integrating the Container Apps environment.
4. **ACR SKU `Basic`/`Standard` rejected** (`SkuNotSupported`) by this
   specific subscription; `Premium` succeeded. Made the SKU a Bicep
   parameter (`acrSkuName`, default `Basic`) instead of hardcoding, so most
   users keep the cheaper default and only override if their subscription
   requires it.

Also required registering the `Microsoft.App` and `Microsoft.ContainerService`
resource providers on the subscription (`az provider register`), which were
not previously registered — a one-time step, not a repo bug.

| Check | Command | Result |
|---|---|---|
| Bicep compile (post-fix) | `az bicep build --file infra/main.bicep --stdout` | **Compiled successfully** |
| Bicep lint (post-fix) | `az bicep lint --file infra/main.bicep` | 1 benign warning (hardcoded `privatelink.table.core.windows.net` zone name) |
| Python tests (post-fix) | `python -m pytest -q` | **32 passed** |
| Python lint (post-fix) | `python -m flake8 .` | **0 findings** |
| Real `azd provision` | `azd provision --no-prompt` | **SUCCESS** — resource group, VNet, 2 Private Endpoints, Log Analytics, App Insights, Key Vault (+secret), Storage Account/Table, ACR, Container Apps Environment (VNet-integrated), Container App, Entra app registration + `ActionGroupsSecureWebhook` role + AzNS role assignment, Action Group, and Activity Log Alert created |
| Real `azd deploy` | `azd deploy --no-prompt` | **SUCCESS** — real app image built, pushed to ACR, deployed as the active Container App revision |
| Live `/healthz` | `curl https://<fqdn>/healthz` | **HTTP 200** `{"status":"healthy"}` |
| Live `/readyz` | `curl https://<fqdn>/readyz` | **HTTP 200** `{"status":"ready"}` (configuration/client construction readiness; the signed lifecycle test and dependency telemetry separately prove Table data-plane access) |
| Live webhook auth | `curl -X POST https://<fqdn>/api/service-health` (no token) | **HTTP 401** `authentication_required` — confirms Easy Auth is enforced |
| Live Action Group/Alert | `az monitor action-group list` / `az monitor activity-log alert list` | Both resources exist with `Global` location |

At this stage, Slack delivery was the only untested boundary.

## Full WSL, Azure, and Slack validation (2026-08-07)

The README procedure was then run from Ubuntu on WSL with a real Slack app and
channel. Credentials were entered only through a hidden terminal prompt and
were never copied into chat, commands, logs, source files, or this record. The
run proved `auth.test`, a direct `chat.postMessage`, and Azure Monitor's
official `servicehealth` Action Group test through the Secure Webhook into the
configured Slack channel.

The exercise reconciled these production details:

1. **Authentication under Conditional Access.** Azure CLI and AZD use separate
   sessions. Device-code flow can be blocked by tenant policy, so both logins
   must use interactive browser authentication from a browser-capable WSL
   session (`az login`, `azd auth login --use-device-code=false`).
2. **Docker/WSL readiness.** Docker Desktop must use its WSL 2 backend, Linux
   containers, and integration for the active distribution. Validate the
   effective context and daemon with `docker context show`, `docker info`, and
   a disposable `hello-world` container before invoking AZD.
3. **Secret handling.** Capture the `xoxb` bot token with Bash `read -s`, pass
   only the variable reference to `azd env set` or Slack's HTTP `Authorization`
   header, and immediately unset it. This prevents terminal echo and
   literal-token shell history. AZD's local environment remains
   credential-bearing and must never be printed or committed.
4. **Slack least privilege and routing.** Grant only granular `chat:write`;
   avoid `chat:write.public` and explicitly invite the bot to every configured
   channel. Configure channel IDs, not display names. The ID is available from
   channel details opened through the channel name or three-dots menu; API-based
   discovery uses `conversations.list` but requires additional read scopes.
   Messages retain top-level `text` as the notification and screen-reader
   fallback for their Block Kit content.
5. **ACR SKU compatibility.** `Basic` cannot accept the Premium-only untagged
   manifest retention policy. The registry module now omits that policy while
   preserving disabled admin and anonymous pull access.
6. **Secure Webhook trust.** The official AzNS service principal must both own
   the protected API application and receive its application-only
   `ActionGroupsSecureWebhook` role. The setup script enforces both
   relationships idempotently.
7. **Entra v2 audience.** The issued token can contain the client ID GUID as
   `aud` rather than only `api://<client-id>`. Easy Auth and the application
   explicitly accept and validate both representations.
8. **Service Health location.** The Activity Log Alert and Action Group are
   `Global`; Service Health does not support a regional Action Group.
9. **Webhook retry suppression.** Repeated failed tests can exhaust Azure
   Monitor's webhook retries and suppress all Action Group calls to the
   endpoint for 15 minutes. After correcting a failure, wait for the cooldown
   before running one official test.
10. **CLI test receiver behavior.** `test-notifications create` does not reuse
    receivers from the named Action Group. The validated command reads the
    deployed URI, object ID, and identifier URI, then supplies the Secure
    Webhook receiver with `--add-action webhook ... useaadauth ...
    usecommonalertschema`.

| Live check | Expected and observed result |
|---|---|
| `GET /healthz` | HTTP 200, `{"status":"healthy"}` |
| `GET /readyz` | HTTP 200, `{"status":"ready"}` |
| Unauthenticated `POST /api/service-health` | HTTP 401, `authentication_required` |
| Slack `auth.test` | `ok: true` for the configured bot |
| Slack `chat.postMessage` | Visible test message in the configured channel |
| Action Group and alert location | Both `Global` |
| Secure Webhook receiver | Entra auth and Common Alert Schema enabled with protected API object/identifier values |
| `az monitor action-group test-notifications create --alert-type servicehealth --add-action webhook ...` | Operation state `Complete`; signed request accepted and formatted Service Health message delivered to Slack |
| Container App logs | `POST /api/service-health` HTTP 200 from `IcMBroadcaster/1.0`, with no credential or payload logging |

The canonical reproducible commands, troubleshooting, cooldown warning,
cleanup, and rollback procedure now live in the README rather than a duplicate
runbook.

## Isolated live day-2 E2E validation plan

**Status: Validated.** Pre-deployment checks and the isolated Bicep what-if
passed on 2026-08-09. Live execution is authorized only inside the boundary
below; cleanup still requires separate confirmation.

### Approved isolation boundary

| Item | Approved value |
|---|---|
| Test tenant | `cc8ad65c-a10c-42a1-9fdc-65d99db48492` |
| Central subscription | Management — `09f7fca2-63df-4326-b31c-aec3bcbb23db` |
| Region | East US 2 |
| Child test Management Group membership | Connectivity — `d61e43e0-4793-4b0e-ac08-002e8c18763f`; Identity — `5f48510d-3bdc-43e0-babf-bb7860b6f76b` |
| Required exclusion | The central Management subscription remains outside the child test Management Group. |

**Hard guard:** the healthy `rg-service-health-test` environment in the
separate tenant whose ID starts with `16b3` is out of scope. Validation must
fail closed if discovery, the selected Azure context, any resource ID, or any
planned operation points to that tenant, resource group, or its resources.

### Temporary footprint and secret handling

The isolated run may later create a new child Management Group and move only
the two approved test subscriptions into it. The Management subscription may
host a separate AZD environment with its own resource group, VNet/subnets,
Private Endpoints and DNS links, managed identity and role assignments,
Container Registry, Key Vault, Storage account/table, Log Analytics,
Application Insights, Container Apps environment/app, Entra protected API,
baseline Action Group/Activity Log Alert, and day-2 peripheral alert resource
groups. None of these resources may reuse the active environment.

Expected temporary cost comes mainly from the always-ready Container App
(`minReplicas: 1`), Container Registry, two Private Endpoints, Log Analytics /
Application Insights ingestion, and minimal Storage transactions. Management
Groups, Activity Log Alerts, and low-volume Action Groups have no expected
material direct charge. Record actual resource names and timestamps before
creation so cost and cleanup can be audited.

Slack bot token and channel configuration must be entered or reused only from
local hidden prompts / protected AZD environment storage. Never echo, print,
log, paste into command arguments, commit, or include credentials in validation
evidence. Unset transient shell variables immediately after use.

### Validation order and gates

1. Read-only preflight: prove the signed-in tenant, all three subscription
   tenant IDs, current Management Group ancestry, provider registration, and
   required RBAC. Stop if any value differs from the approved boundary.
2. Review Bicep build/lint output and an Azure deployment what-if for the new
   isolated environment. Redact secret values and stop on deletion/replacement
   of any pre-existing resource. A cloud what-if is deferred until the isolated
   environment parameters and locally held Slack values are available.
3. After the current plan-only hold is lifted, create the child test Management
   Group, place only Connectivity and Identity beneath it, and re-prove that
   Management remains outside.
4. Deploy one new central environment into Management, configure its signed
   Secure Webhook, and validate `/healthz`, `/readyz`, unauthenticated webhook
   rejection, baseline alert ownership, and Slack delivery.
5. Run day-2 `list`; prove the AZD-owned baseline alert and anchor Action Group
   are not returned as manager-owned scopes.
6. Add Connectivity and Identity individually. Require successful official
   signed Secure Webhook tests, repeat both adds to prove idempotency, and verify
   `list` reports exactly the two enabled day-2 scopes without baseline overlap.
7. Run `migrate-to-management-group -WhatIf`; require a create/validate/enable-
   before-delete plan and no central-runtime operation. Then, only with explicit
   destructive confirmation, run the migration and prove every descendant
   subscription replacement is tested before its individual alert is disabled
   and removed.
8. Run `list` again; prove effective coverage for both child subscriptions, no
   duplicate delivery, no individually managed overlap, and an unchanged
   central baseline/runtime.
9. Capture resource IDs, timestamps, signed-test states, command exit codes, and
   sanitized outputs. Do not capture payloads or credentials.
10. Cleanup is a separate destructive phase requiring new confirmation. Before
    deleting or moving anything, inventory the isolated resources, verify no
    active-environment IDs are present, restore subscription ancestry safely,
    and confirm no required coverage gap will be introduced.

### Pre-deployment validation proof

| Check | Result |
|---|---|
| Python tests and lint | **48 passed**; flake8 clean |
| Main and day-2 Bicep build/lint | **Passed**; existing `core.windows.net` environment-URL warning in `network.bicep` plus CLI upgrade notice only |
| Docker image build | **Passed** as `azure-service-health-slack-bot:day2-final`; container user is `app` |
| Azure identity and boundary | **Passed**: Azure CLI tenant and all three subscriptions match `cc8ad65c-a10c-42a1-9fdc-65d99db48492`; Management remains outside the isolated child MG |
| Slack preflight | **Passed**: token format, `auth.test`, and a direct `chat.postMessage` to the configured channel; token never printed and clipboard cleared |
| Secure Webhook identity | **Passed**: isolated Entra application, service principal, AzNS ownership, and app-role assignment configured idempotently |
| Cloud deployment what-if | **Passed** via `az deployment sub what-if`: 25 creates in `rg-shb-day2-e2e-9fdc`, 0 deletes, 0 existing-resource modifications; two expected unresolved new role-assignment IDs |
| Azure policy visibility | **Passed**: no policy assignments visible at the isolated subscription scope and no policy denial in what-if |
| AZD package | **Passed** for the isolated environment |
| Live resource validation | **Passed** for the approved isolated boundary; details below. Cleanup completed 2026-08-10. |

### Live day-2 E2E proof

The approved isolated run completed on 2026-08-09. It did not query, deploy,
update, or delete the healthy `rg-service-health-test` environment in the
separate `16b3...` tenant.

| Check | Observed result |
|---|---|
| Child topology | Created `mg-shb-day2-e2e-9fdc`; Connectivity and Identity are descendants; Management remains outside. |
| Central runtime | Provisioned only in `rg-shb-day2-e2e-9fdc`; ACR image build and Container App revision succeeded. |
| Runtime endpoints | `GET /healthz` and `GET /readyz` returned HTTP 200; unauthenticated `POST /api/service-health` returned HTTP 401. |
| Slack boundary | `auth.test` and direct `chat.postMessage` succeeded without printing the token. |
| Baseline protection | Central alert `ala-shb-day2-e2e-9fdc-service-health` remained enabled, subscription-scoped to Management, and has no day-2 manager tag. `list` did not return it. |
| Individual adds | Connectivity and Identity each created one disabled peripheral path, passed the official signed test, enabled successfully, and returned `AlreadyPresent` on repeat. |
| Signed test contract | Azure returned overall state `Complete` and receiver status `Succeeded`; both are now required exactly. |
| MG preview | `migrate-to-management-group -WhatIf` planned the logical MG plus both overlapping subscriptions and made no mutation. |
| MG migration | Created and tested one subscription-scoped member path in Connectivity and one in Identity, enabled both, then removed the two individual paths. |
| Final inventory | `list` reports one enabled logical `managementGroup` scope, two covered descendants, two alert IDs, no delivery overlap, and the cleanup-required orphan Action Group retained by the failed pre-fan-out attempt. |
| Central immutability | Baseline remained enabled and scoped to Management after migration; day-2 resources exist only in descendant peripheral resource groups. |

Live validation found and corrected three Azure-contract issues:

1. The Action Group API requires the deployment caller, as well as AzNS, to
   own the protected API application. The setup script now adds both owners
   idempotently.
2. The official test uses `Complete` for the operation and `Succeeded` for the
   Secure Webhook receiver, not `Completed`.
3. Activity Log Alerts cannot natively target one selected Management Group.
   `tenantScope` represents the whole tenant and cannot be combined with
   non-empty scopes. Logical Management Group coverage now enumerates
   descendants and manages one subscription-scoped member path per descendant.

### Cleanup record — 2026-08-10

Validated and cleaned. All temporary E2E resources have been removed. The
following authoritative proof was recorded on 2026-08-10.

| Item | Observed result |
|---|---|
| Temporary Management Group | `mg-shb-day2-e2e-9fdc` absent; no longer present under tenant root `cc8ad65c-a10c-42a1-9fdc-65d99db48492`. |
| Management subscription ancestry | `09f7fca2-63df-4326-b31c-aec3bcbb23db` restored as direct child of tenant root. |
| Connectivity subscription ancestry | `d61e43e0-4793-4b0e-ac08-002e8c18763f` restored as direct child of tenant root. |
| Identity subscription ancestry | `5f48510d-3bdc-43e0-babf-bb7860b6f76b` restored as direct child of tenant root. |
| Resource groups (all three subscriptions) | 0 resource groups containing `shb-day2-e2e-9fdc` across Management, Connectivity, and Identity, including auto-managed `ME_*` groups. |
| Activity Log Alerts | 0 Activity Log Alerts matching `shb-day2-e2e-9fdc` across all subscriptions. |
| Action Groups | 0 Action Groups matching `shb-day2-e2e-9fdc` across all subscriptions, including the cleanup-required orphan from the failed pre-fan-out attempt. |
| Entra application | Display name `Azure Service Health Slack Bot - shb-day2-e2e-9fdc`, appId `dc5a4e15-f71b-4380-af70-0f5e3803a1ac`, and its service principal are absent. |
| AZD local environment | Local AZD test environment for `shb-day2-e2e-9fdc` absent. |
| DPAPI token artifact | DPAPI-encrypted token artifact for the E2E run absent. |
| Production endpoint health | `ca-service-health-test.gentleforest-19f9d19f.eastus2.azurecontainerapps.io` returns `GET /healthz` HTTP 200, `GET /readyz` HTTP 200, unauthenticated `POST /api/service-health` HTTP 401; production environment unaffected. |

## Python operational CLI portability acceptance (2026-08-10)

This section records the follow-up acceptance work after PR #28, including the
hardening and evidence carried by PR #29. It does not replace the earlier
isolated infrastructure proof. Python remains the canonical operational
implementation, and neither Slack nor the protected deployment was modified.

### Official Microsoft contract verification

Current official Microsoft Learn content was queried through the installed
Learn MCP. No third-party documentation was used.

| Official Microsoft Learn source | Behavior validated against the implementation |
|---|---|
| [AZD multi-language hooks](https://learn.microsoft.com/azure/developer/azure-developer-cli/hooks-multi-language#python-hooks) and [azure.yaml schema](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/azd-schema) | A `preprovision` hook can point directly to a `.py` file. AZD infers the Python executor from the extension, finds the nearest dependency manifest, and runs the same hook on every operating system. |
| [AZD custom prompts: hooks for custom logic](https://learn.microsoft.com/azure/developer/azure-developer-cli/custom-prompts#option-3-hooks-for-custom-logic) | Clean `azd provision` executes `preprovision` before Bicep input discovery. AZD 1.30 preview does not execute lifecycle hooks, so a new environment must run `azd hooks run preprovision -e <environment-name> --no-prompt` before its first preview. |
| [AZD command reference](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/reference) | `azd auth login`, `auth status`, `env new`, `env set`, `env get-value`, `env list`, `provision`, `up`, and `down --purge` forms used by the hook and runbook are current. |
| [`az rest`](https://learn.microsoft.com/en-us/cli/azure/reference-index#az-rest) | ARM and Microsoft Graph requests support explicit methods, URLs, headers, JSON output, and `@file` bodies. The Python Graph boundary uses temporary UTF-8 body files so Windows quoting cannot corrupt JSON. |
| [`az account show`](https://learn.microsoft.com/en-us/cli/azure/account#az-account-show) and [`az account list`](https://learn.microsoft.com/en-us/cli/azure/account#az-account-list) | Tenant and subscription IDs are obtained from the signed-in Azure context and checked exactly before any operation. |
| [Secure Webhook authentication](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/action-groups#configure-authentication-for-secure-webhook) | The fixed AzNS application ID is `461e8683-5575-4561-ac7f-899cc907d62a`; the protected API uses v2 application tokens, exposes `ActionGroupsSecureWebhook`, makes AzNS an owner, and assigns AzNS that application role. |
| [Graph create application](https://learn.microsoft.com/en-us/graph/api/application-post-applications), [update application](https://learn.microsoft.com/en-us/graph/api/application-update), [application resource](https://learn.microsoft.com/en-us/graph/api/resources/application), [API application](https://learn.microsoft.com/en-us/graph/api/resources/apiapplication), and [app role](https://learn.microsoft.com/en-us/graph/api/resources/approle) | Application create/reuse, `identifierUris`, application-only roles, and `requestedAccessTokenVersion: 2` match Graph. PATCH preserves existing nested API authorization fields rather than clearing scopes or preauthorized applications. |
| [List application owners](https://learn.microsoft.com/en-us/graph/api/application-list-owners), [add application owner](https://learn.microsoft.com/en-us/graph/api/application-post-owners), and [get user](https://learn.microsoft.com/en-us/graph/api/user-get) | Owner lookup/add uses the documented `owners/$ref` body. `/me` is used only for delegated user authentication. |
| [Create service principal](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-serviceprincipals) and [list service principals](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list) | The API and official AzNS service principals are found by exact `appId`; missing API/AzNS principals are created only where the documented flow permits, and ambiguous results fail closed. |
| [Create app-role assignment](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignments) and [list app-role assignments](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list-approleassignments) | The assignment body uses exact `principalId`, `resourceId`, and `appRoleId` values and reruns detect the existing assignment idempotently. |
| [Graph paging](https://learn.microsoft.com/en-us/graph/paging) | Collection reads follow `@odata.nextLink` with a bounded page limit. |
| [Container Apps Entra authentication](https://learn.microsoft.com/en-us/azure/container-apps/authentication-entra) and [authConfigs resource](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/containerapps/authconfigs) | Bicep, not the setup CLI, owns Easy Auth client registration, issuer, accepted audiences, allowed AzNS application, HTTPS enforcement, and anonymous route behavior. Secure Webhook is an application-to-application daemon flow, so the setup CLI does not add an interactive redirect URI. |
| [Action Groups](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/action-groups) and [Action Group Bicep resource](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/actiongroups) | Service Health Action Groups are `Global`; receiver fields for AAD auth, object ID, identifier URI, and Common Alert Schema match the resource contract. |
| [Activity Log Alert Bicep resource](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/activitylogalerts) and [Service Health overview](https://learn.microsoft.com/en-us/azure/service-health/overview) | Alerts use the documented `scopes`, Action Group IDs, and `category = ServiceHealth` condition. The CLI implements logical Management Group fan-out as subscription-scoped members rather than claiming unsupported native selected-MG scope behavior. |
| [`test-notifications create`](https://learn.microsoft.com/en-us/cli/azure/monitor/action-group/test-notifications#az-monitor-action-group-test-notifications-create) and [test-notification REST response](https://learn.microsoft.com/en-us/rest/api/monitor/action-groups/create-notifications-at-action-group-resource-level) | The positional Secure Webhook action grammar and `servicehealth` alert type match the CLI reference. Learn examples return `Completed`; the prior isolated Azure run returned `Complete`/`Succeeded`. The manager accepts only those explicit success variants and leaves new alerts disabled for every other value. |
| [Subscription Bicep deployment](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-to-subscription), [resource-group resource](https://learn.microsoft.com/en-us/azure/templates/microsoft.resources/resourcegroups), and [`az deployment sub create`](https://learn.microsoft.com/en-us/cli/azure/deployment/sub#az-deployment-sub-create) | The day-2 template targets a subscription, creates its tagged peripheral resource group, and is deployed with an explicit subscription, location, name, template, and parameters. |
| [`az resource`](https://learn.microsoft.com/en-us/cli/azure/resource), [Action Group list](https://learn.microsoft.com/en-us/cli/azure/monitor/action-group#az-monitor-action-group-list), and [Activity Log Alert list](https://learn.microsoft.com/en-us/cli/azure/monitor/activity-log/alert#az-monitor-activity-log-alert-list) | Discovery, enable/disable updates, exact-ID reads, and deletes use documented commands and API versions. Ownership tags are validated before mutation. |
| [Management Group get](https://learn.microsoft.com/en-us/rest/api/managementgroups/management-groups/get) and [get descendants](https://learn.microsoft.com/en-us/rest/api/managementgroups/management-groups/get-descendants) | Tenant ownership comes from `properties.tenantId`; descendants use the documented `2020-05-01` API, distinguish management groups from subscriptions by type, and follow `nextLink`. |
| [Azure CLI service-principal authentication](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-service-principal) | Noninteractive callers are supported. Setup resolves a caller owner for both delegated users and service-principal sessions and rejects unknown caller types. |

Two official-documentation gaps remain explicit rather than being hidden:

1. The [Authorization permissions operation group](https://learn.microsoft.com/en-us/rest/api/authorization/permissions)
   documents resource and resource-group variants, but not the same effective
   permissions endpoint at subscription or Management Group scope. The endpoint
   is retained because it was exercised successfully in the isolated tenant and
   gives a stricter effective-permission check than reconstructing access from
   role assignments.
2. Microsoft Learn documents `az account show` but not the exact nested
   `user.type`/`user.name` output shape used to distinguish delegated and
   service-principal callers. Tests pin both observed shapes and unknown values
   fail explicitly.

### Python-only operational interface

The Python setup and day-2 CLIs are the sole tracked operational entry points.
The AZD hook points directly to the setup `.py` file, and CI runs the complete
Python suite plus exact CLI contracts on Ubuntu, macOS, and Windows. A
repository regression test rejects retired script extensions and terminology.

### Process, command, and build evidence

| Boundary | Command or exercised contract | Result |
|---|---|---|
| Full Python suite | `python -m pytest -q` | **121 passed** |
| Python lint | `python -m flake8 .` | **0 findings** |
| Canonical CLI suite exactly as documented in CONTRIBUTING | `python -m pytest -q test/test_manage_alert_scopes.py test/test_configure_secure_webhook.py test/test_cli_subprocess.py` | **73 passed** |
| CLI entry points | `python scripts/manage_alert_scopes.py --help`; `python scripts/configure_secure_webhook.py --help` | Both exit 0 with usage on stdout. |
| Cross-platform subprocess contract | Real child processes with stateful fake `az`/`azd` executables | Help, documented command forms, quoted display names, paths with spaces, JSON output, invalid input/exit codes, temporary body-file cleanup, idempotent rerun, repository portability guard, and the native Python AZD hook passed. |
| Installed Azure CLI boundary | `az version` through `AzureCli` with a JMESPath string literal containing `&`, `%`, and double quotes | Passed through a real subprocess on Windows. The MSI `az.cmd` launcher was bypassed through its bundled `python.exe -IBm azure.cli`, and Azure CLI returned the exact special-character value. The same assertion runs on every CI matrix OS. |
| Real documented Management Groups reads | `GET` group and descendants with `api-version=2020-05-01` through the shared Python boundary | Passed against tenant root `cc8ad65c-a10c-42a1-9fdc-65d99db48492`; three descendants returned. |
| Main and day-2 Bicep | The four build/lint commands in CONTRIBUTING | Passed. Existing `core.windows.net` portability warning in `network.bicep` and CLI upgrade notice only. |
| Docker | `docker build -t azure-service-health-slack-bot:ci .` | Passed; image ID `sha256:f8d36805a5aba66b48e720df02c8f392708447fe20f9bfe83ec4ba8f8487f97b`. |

The exact native Python hook from `azure.yaml` is exercised by the subprocess
suite on the Ubuntu, macOS, and Windows GitHub Actions matrix. Previously
updated day-2 invocation forms were executed with their documented tokens
against the isolated tenant; with no central deployment present, each failed
safely before mutation. The subprocess suite additionally exercises every
documented operation parser and JSON form without representing those fakes as
live Azure validation.

### Isolated Azure/Entra operational E2E proof

The user explicitly authorized the destructive run on 2026-08-10. The unique
prefix was `pye2e-20260810-211320-53f3`. The approved boundary was tenant
`cc8ad65c-a10c-42a1-9fdc-65d99db48492` and only these subscriptions:

- Management `09f7fca2-63df-4326-b31c-aec3bcbb23db`;
- Connectivity `d61e43e0-4793-4b0e-ac08-002e8c18763f`; and
- Identity `5f48510d-3bdc-43e0-babf-bb7860b6f76b`.

An initial preflight observed a different active tenant and failed closed before
mutation. After selecting the approved Management subscription, the run
reproved the exact tenant, subscription IDs, caller object ID
`dc341336-dacd-4bf2-a731-524344dd29d9`, Owner access, provider registration, and
direct-tenant-root topology. `before.json` records zero matching resource
groups, Action Groups, Activity Log Alerts, Entra applications, service
principals, role assignments, Management Groups, and AZD environments. The
protected tenant `16b3c013-d300-468d-ac64-7eda0820b6d3`, protected subscription
`00052886-0d2d-493f-8b47-8ca68a0402ad`, and protected resource group
`rg-service-health-test` were not accessed. No Slack API or Slack endpoint was
used.

#### Secure Webhook create and idempotent rerun

The canonical Python setup CLI created the disposable registration and then ran
again with the identical inputs. Both runs exited 0. The second inventory was
identical for all security-sensitive IDs and settings:

| Object | Stable value |
|---|---|
| Application client ID | `67912937-9e68-43db-a7e7-66ed431924d0` |
| Application object ID | `edd08233-20f1-49f7-aaa2-1eace429e85b` |
| Identifier URI | `api://67912937-9e68-43db-a7e7-66ed431924d0` |
| API service principal | `70c5272e-b0f9-4f5d-abb0-53ce268cff25` |
| Secure Webhook app role | `420deba6-7fae-4b4e-8770-4ce59a084423` |
| AzNS assignment | `4dsIL_WU_EuxBoWrV97-HgNuRiZWGuNGo5zRerguZW0` |
| Owners | exact caller and AzNS service-principal IDs |
| Token/API contract | v2 token, application-only `ActionGroupsSecureWebhook` role |
| AZD values | exact tenant, client ID, object ID, and identifier URI |

A disposable Container App receiver in the central prefixed resource group was
used instead of Slack. `POST /api/service-health` returned HTTP 200 before the
real Easy Auth contract was applied. The central Action Group and baseline
Service Health alert then used that receiver.

#### Alert scope add, list, migrate, and remove

The canonical Python scope CLI exercised the complete live lifecycle:

1. Initial `list --json` returned zero manager-owned scopes.
2. Adding the immutable central subscription failed closed with exit 1 because
   the baseline alert already covered it; zero manager resources were created.
3. Connectivity and Identity were added individually. Both returned `Added`,
   Azure Monitor test status `Complete`, and enabled alerts/Action Groups.
4. Re-adding Connectivity returned `AlreadyPresent`, `TestStatus: NotRun`, and
   the same alert and Action Group IDs.
5. A temporary Management Group contained only Connectivity and Identity.
   Migration preview returned exactly those two overlaps and created zero
   member resource groups.
6. Forced migration created and signed-tested both fan-out members, enabled the
   replacements, and removed the two individual paths. `list --json` returned
   one enabled logical Management Group scope covering exactly two descendants
   with no overlap.
7. Signed-tested individual coverage was restored before Management Group
   removal. Removal deleted both fan-out members, and the final list contained
   exactly the two enabled individual scopes with zero Management Group alert
   resources.

The live destructive confirmation path exposed one defect: a TTY-like stdin
that returned EOF produced an uncaught traceback. The CLI now converts
`EOFError` to the existing explicit fail-closed error. A regression test pins
the exit-1/no-traceback behavior. The live retry returned exit 1 with that
message and left both Management Group members unchanged; the separately
pre-approved `--force` removal then completed.

#### Cleanup and zero-residual proof

`pre-cleanup-inventory.json` captured five exact prefixed resource groups, three
Action Groups, three Activity Log Alerts, the temporary Management Group and
its Owner assignment, the application, API service principal, AzNS app-role
assignment, and local AZD environment. Cleanup verified ownership tags and
exact object identities before deleting only those objects.

`after.json`, captured at `2026-08-10T22:57:19Z`, proves:

- zero prefixed resource groups across all three subscriptions;
- zero prefixed Action Groups and Activity Log Alerts;
- zero resources carrying `azd-env-name=pye2e-20260810-211320-53f3`;
- zero matching Entra applications, API service principals, and AzNS
  app-role assignments;
- zero assignments at the deleted temporary Management Group scope;
- zero matching Management Groups and local AZD environments;
- the official AzNS service principal remains intact; and
- Connectivity and Identity are active direct children of tenant root
  `cc8ad65c-a10c-42a1-9fdc-65d99db48492`.

The before and after inventories both contain zero disposable objects. No
unavoidable residual remains. Microsoft Learn MCP verification remains the
separate tool-availability blocker documented above; the official Learn web
fallback is not represented as MCP validation.
