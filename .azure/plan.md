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
- **Registry**: Azure Container Registry (Basic SKU, admin user disabled,
  anonymous pull disabled)
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
