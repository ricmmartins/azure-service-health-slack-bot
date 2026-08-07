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
  (`infra/modules/service-health-alert.bicep`), isolated so it can be
  redeployed per additional subscription

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
scripts/configure-secure-webhook.ps1   Idempotent Entra app registration/role setup
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
| PowerShell parse | AST parse of `scripts/configure-secure-webhook.ps1` via `[System.Management.Automation.Language.Parser]::ParseFile` | **PARSE OK**, 0 errors |
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
| Real `azd provision` | `azd provision --no-prompt` | **SUCCESS** — resource group, VNet, 2 Private Endpoints, Log Analytics, App Insights, Key Vault (+secret), Storage Account/Table, ACR, Container Apps Environment (VNet-integrated), Container App, Entra app registration + `ActionGroupsSecureWebhook` role + AzNS role assignment, Action Group, and Activity Log Alert created |
| Real `azd deploy` | `azd deploy --no-prompt` | **SUCCESS** — real app image built, pushed to ACR, deployed as the active Container App revision |
| Live `/healthz` | `curl https://<fqdn>/healthz` | **HTTP 200** `{"status":"healthy"}` |
| Live `/readyz` | `curl https://<fqdn>/readyz` | **HTTP 200** `{"status":"ready"}` (confirms live Key Vault + Table Storage connectivity through the Private Endpoints) |
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
