# Azure Service Health Slack Bot — Delivery Plan

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
  1 vCPU / 1Gi min, HTTP autoscale 1–3 replicas)
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
  rejects the cheaper SKUs, as MCAPS-Hybrid-REQ subscriptions do)
- **Networking**: Key Vault and Storage are provisioned with
  `publicNetworkAccess: 'Disabled'` and reached only via Private Endpoints on
  a dedicated VNet (`infra/modules/network.bicep`), which also provides the
  Container Apps environment's VNet integration subnet. The Container App's
  own public ingress is unaffected — only egress to KV/Storage is routed
  privately.
- **Alerting source**: Activity Log Alert + Secure Action Group
  (`infra/modules/service-health-alert.bicep`), isolated so it can be
  redeployed per additional subscription

## Explicit exclusions (per requirements)

- No Slack Bolt / signing secret / inbound events
- No Azure support-ticket workflow (`azure-mgmt-support` not a dependency)
- No `support-rbac.bicep` module (Reader, Support Request Contributor)
- No deployment performed by this work, and no Microsoft Graph writes were
  made — `scripts/configure-secure-webhook.ps1` is provided but was not
  executed against any tenant

## Repository layout

```
app.py                          Minimal Flask entrypoint (3 routes only)
service_health/                 Parser, routing, auth, storage, Slack, runtime, telemetry
infra/main.bicep                Subscription-scope orchestration
infra/modules/                  registry, security, storage, container-app, observability, service-health-alert
scripts/configure-secure-webhook.ps1   Idempotent Entra app registration/role setup
config/service_health_routes.example.json
test/                           Parser/routing/auth/storage/processor/Slack/app/bootstrap tests
.github/workflows/ci.yml        pytest+flake8, bicep build/lint, docker build
```

## Validation proof

All commands were executed directly in this repository during this session.

| Check | Command | Result |
|---|---|---|
| Python tests | `python -m pytest -q` | **32 passed** in 8.93s |
| Python lint | `python -m flake8 .` | **0 findings** |
| PowerShell parse | AST parse of `scripts/configure-secure-webhook.ps1` via `[System.Management.Automation.Language.Parser]::ParseFile` | **PARSE OK**, 0 errors |
| Bicep compile | `az bicep build --file infra/main.bicep --stdout` | **Compiled successfully** to ARM JSON |
| Bicep lint | `az bicep lint --file infra/main.bicep` | **0 warnings/errors** |
| Docker build | `docker build -t azure-service-health-slack-bot:validate .` | **Build succeeded**, multi-stage, final image based on `python:3.13-slim-bookworm` |
| Non-root check | `docker exec shb-smoke whoami` | **`app`** (non-root) |
| Gunicorn smoke test | `curl http://localhost:18080/healthz` | **HTTP 200** `{"status":"healthy"}` |
| Gunicorn smoke test | `curl http://localhost:18080/readyz` | **HTTP 200** `{"status":"ready"}` |
| Container logs | `docker logs shb-smoke` | Clean gunicorn startup logs only — **no payload or secret values logged** |
| AZD packaging | `azd package --no-prompt` | **SUCCESS** — image tagged `azure-service-health-slack-bot/app-<env>:azd-deploy-<ts>` |

No deployment (`azd provision` / `azd deploy`) and no Microsoft Graph writes
were performed, per the requirement to avoid live changes.

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

No deployment or Graph writes were performed. Following this revalidation,
`README.md` was expanded with a full step-by-step deployment guide covering
Slack app creation, AZD provisioning (including what
`configure-secure-webhook.ps1` does under the hood), image deployment,
end-to-end verification, multi-subscription rollout, routing updates, and
teardown.

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
- `configure-secure-webhook.ps1` requires interactive Graph/Azure CLI
  authentication and was validated only for syntax (AST parse), not executed
  against a tenant, per the "no Graph writes" requirement.

## Live deployment validation (2026-08-06, follow-up session)

A real end-to-end deployment was executed in an internal Microsoft dev/test
subscription (`MCAPS-Hybrid-REQ-71914-2024-ricardomac`) to prove the whole
pipeline works, using a placeholder Slack bot token and placeholder routing
config (`SLACK_BOT_TOKEN=xoxb-PLACEHOLDER-REPLACE-ME`, a fallback-only routes
JSON) since only the account owner can create a real Slack app/token. This
uncovered and fixed three real bugs, plus one environment-specific
accommodation, all committed here:

1. **`scripts/configure-secure-webhook.ps1` silently failed on Windows.**
   `az rest --body <jsonstring>` gets mangled by PowerShell → `az.cmd` →
   `cmd.exe` argument passing, corrupting embedded quotes; the script also
   never checked `$LASTEXITCODE`, so failures were swallowed and it reported
   success while leaving `identifierUri="api://"` (empty app ID). Fixed with
   a new `Invoke-GraphRest` helper that writes the JSON body to a temp file
   and calls `az rest --body @tempfile`, and checks the exit code.
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
| PowerShell parse (post-fix) | AST parse of `configure-secure-webhook.ps1` | **PARSE OK** |
| Real `azd provision` | `azd provision --no-prompt` | **SUCCESS** — resource group, VNet, 2 Private Endpoints, Log Analytics, App Insights, Key Vault (+secret), Storage Account/Table, ACR, Container Apps Environment (VNet-integrated), Container App, Entra app registration + `ActionGroupsSecureWebhook` role + AzNS role assignment, Action Group, Activity Log Alert all created in `rg-ricmmartins-turbo-guacamole` |
| Real `azd deploy` | `azd deploy --no-prompt` | **SUCCESS** — real app image built, pushed to ACR, deployed as the active Container App revision |
| Live `/healthz` | `curl https://<fqdn>/healthz` | **HTTP 200** `{"status":"healthy"}` |
| Live `/readyz` | `curl https://<fqdn>/readyz` | **HTTP 200** `{"status":"ready"}` (confirms live Key Vault + Table Storage connectivity through the Private Endpoints) |
| Live webhook auth | `curl -X POST https://<fqdn>/api/service-health` (no token) | **HTTP 401** `authentication_required` — confirms Easy Auth is enforced |
| Live Action Group/Alert | `az monitor action-group list` / `az monitor activity-log alert list` | Both exist: `ag-ricmmartins-turbo-guacamole-service-health`, `ala-ricmmartins-turbo-guacamole-service-health` |

Not exercised live: an actual Slack message post (blocked on the placeholder
bot token — this is expected and requires the account owner to create a real
Slack app, per the Slack app-creation constraint noted above). The
`/readyz`-confirmed live connectivity to Key Vault and Table Storage, plus
the 401 on the unauthenticated webhook call, demonstrate the rest of the
pipeline (Managed Identity, Key Vault, Storage, Easy Auth, VNet/Private
Endpoints) is genuinely live and working, not just compiled/tested in
isolation.
