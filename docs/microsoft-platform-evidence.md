# Microsoft platform evidence

This record maps platform-sensitive documentation claims to current Microsoft
sources. It was reviewed on 2026-08-11 against repository baseline
`42b115740f9f6bb92b1e95b422168ba9710c0846`.

An official source documents Microsoft platform behavior. An implementation
source is repository code, Bicep, or a test. An empirical source is a bounded
observation from a deployed test and is not a Microsoft guarantee.

Microsoft documentation can change and does not specify every timing,
interaction, or response variant. This record therefore avoids claiming
complete certainty where the platform contract is incomplete.

## Azure Monitor and Service Health

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| Common Alert Schema places common fields in `data.essentials` and alert-specific fields in `data.alertContext`. | [Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema) | Official. The application also validates required Service Health fields. |
| Service Health Activity Log data includes `trackingId`, `title`, `communication`, `impactStartTime`, and `impactedServices`. | [Service Health event properties](https://learn.microsoft.com/azure/service-health/service-health-event-properties) | Official. The service rejects payloads that cannot provide its incident key or Slack content. |
| `impactedServices` is represented as escaped JSON with `ServiceName`, `ImpactedRegions`, and `RegionName`. | [Service Health event properties](https://learn.microsoft.com/azure/service-health/service-health-event-properties) | Official. `service_health/parser.py` also accepts an already-decoded list as a compatibility path. |
| Activity Log event status and stage are separate concepts. Service Health notifications can have stages beyond the application's local labels. | [Azure Activity Log event schema](https://learn.microsoft.com/azure/azure-monitor/essentials/activity-log-schema), [Service Health event properties](https://learn.microsoft.com/azure/service-health/service-health-event-properties) | Official for the platform fields. `Updated` is an implementation label for a newer accepted nonterminal notification, not a complete Azure stage taxonomy. |
| A Service Health Activity Log Alert can call an Action Group. | [Create Service Health alerts with Bicep](https://learn.microsoft.com/azure/service-health/alerts-activity-log-service-notifications-bicep) | Official. The baseline template creates the alert at subscription scope. |
| A Service Health alert rule scope can contain only one subscription, which must be the subscription where the rule is created. Multiple subscriptions and other scope types are not supported by that rule. | [Service Health alert template samples](https://learn.microsoft.com/azure/azure-monitor/alerts/resource-manager-alerts-service-health) | Official. The repository's Management Group workflow therefore creates managed subscription paths. |
| An Action Group used by a Service Health alert is created in the `Global` location in Azure public cloud. | [Create and manage Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups) | Official. `infra/modules/service-health-alert.bicep` follows this requirement. |
| Common Alert Schema can be enabled on the webhook receiver. | [Create and manage Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups), [Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema) | Official. |
| Azure Monitor retries eligible webhook failures, including documented `408`, `429`, `503`, and `504` responses, and can suppress a failed endpoint for 15 minutes after retries are exhausted. | [Create and manage Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups) | Official. The README does not generalize retry behavior to undocumented status codes. |
| Azure Monitor test notifications support the `servicehealth` alert type. | [Test Action Groups in the Azure portal](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#test-an-action-group-in-the-azure-portal) | Official. The repository uses the corresponding Azure CLI command. |

## Secure Webhook and Microsoft Entra

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| Secure Webhook uses a protected API application, Microsoft Entra v2 tokens, an application permission, and the `ActionGroupsSecureWebhook` app role. | [Create and manage Action Groups, Secure Webhook](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#secure-webhook) | Official. |
| The Azure Monitor AzNS AAD Webhook application ID is `461e8683-5575-4561-ac7f-899cc907d62a`. | [Create and manage Action Groups, Secure Webhook](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#secure-webhook) | Official for Azure public cloud. |
| The AzNS service principal must be an owner of the protected API application for Secure Webhook creation, modification, and testing. | [Create and manage Action Groups, Secure Webhook](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#secure-webhook) | Official. The project verifies this ownership before provisioning. |
| The official setup procedure calls for the Microsoft Entra `Application Administrator` role. | [Create and manage Action Groups, Secure Webhook](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#secure-webhook) | Official. |
| The deployment caller is also added as a verified owner of the protected API application. | `scripts/configure_secure_webhook.py` and its tests | Implementation-specific. This supports idempotent maintenance but is not described as a Microsoft platform requirement. |
| Container Apps authentication can restrict accepted token audiences and client applications. | [Authentication and authorization in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/authentication) | Official. `service_health/auth.py` adds application-level caller, audience, and app-role checks. |
| `AllowAnonymous` at the Container Apps authentication layer permits requests to reach the application; the application can still reject the protected route. | [Authentication and authorization in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/authentication) | Official for authentication behavior. The `401` and `403` route policy is implementation-specific. |

## Container Apps, identity, and secrets

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| New Container Apps environments require the `Microsoft.App` and `Microsoft.OperationalInsights` resource providers. | [Build and deploy from a repository to Azure Container Apps](https://learn.microsoft.com/azure/container-apps/quickstart-repo-to-cloud#setup) | Official. `Microsoft.ContainerService` is not required by this implementation. |
| Container Apps supports user-assigned managed identities and managed identity authentication for Azure resources. | [Managed identities in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/managed-identity) | Official. |
| A managed identity can pull from Azure Container Registry when granted a suitable pull role. | [Azure Container Apps image pull with managed identity](https://learn.microsoft.com/azure/container-apps/managed-identity-image-pull) | Official. The deployment uses `AcrPull`, matching the registry's configured RBAC mode. |
| Container Apps can reference a Key Vault secret URI by using a managed identity. | [Manage secrets in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/manage-secrets) | Official. |
| A versionless Key Vault reference can track a newer secret version, but a versioned reference is pinned to that version. | [Manage secrets in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/manage-secrets) | Official. This repository passes a versioned secret URI, so the runbook requires reprovisioning after rotation. |
| Single revision mode keeps one revision active for normal updates and supports revision management. | [Revisions in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/revisions), [Manage revisions](https://learn.microsoft.com/azure/container-apps/revisions-manage) | Official. The rollback command still requires an operator-selected known-good revision. |
| HTTP scale rules can scale a Container App by concurrent HTTP requests. | [Set scaling rules in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/scale-app) | Official. The configured threshold of 25 is an implementation choice. |
| Azure CLI can retrieve Container App system and console logs with `az containerapp logs show`, and can list revisions with `az containerapp revision list`. | [View log streams in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/log-streaming#view-log-streams-via-the-azure-cli) | Official. The deployment checkpoints use these commands for focused recovery. |

## Key Vault, Storage, and networking

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| `Key Vault Secrets User` can read secret contents. | [Azure built-in roles for security](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/security#key-vault-secrets-user) | Official. The role assignment is scoped to the deployed vault. |
| `Storage Table Data Contributor` grants Table data read, write, and delete access. | [Azure built-in roles for storage](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/storage#storage-table-data-contributor) | Official. Storage management roles alone do not imply Table data access. |
| Key Vault and Storage support private endpoints and private DNS for data-plane access. | [Integrate Key Vault with Private Link](https://learn.microsoft.com/azure/key-vault/general/private-link-service), [Use private endpoints for Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-private-endpoints) | Official. The exact subnets and DNS links are implementation choices in `infra/modules/network.bicep`. |
| A purge-protected Key Vault cannot be purged until its retention period expires, and its name cannot be reused during that period. | [Azure Key Vault soft-delete overview](https://learn.microsoft.com/azure/key-vault/general/soft-delete-overview) | Official. `azd down --purge` cannot override this Azure resource policy. |
| Recovering a soft-deleted Key Vault does not restore associated Azure role assignments or integrations. | [Azure Key Vault soft-delete overview](https://learn.microsoft.com/azure/key-vault/general/soft-delete-overview) | Official. Recovery is not equivalent to restoring the original deployment. |
| The private DNS zone suffixes in the templates target Azure public cloud. | `infra/modules/network.bicep` | Implementation-specific. Sovereign cloud adaptation has not been validated. |

## Application Insights and OpenTelemetry

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| The Azure Monitor OpenTelemetry Distro for Python can be configured with `configure_azure_monitor()` and `APPLICATIONINSIGHTS_CONNECTION_STRING`. | [Enable Azure Monitor OpenTelemetry for Python applications](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable?tabs=python) | Official. `service_health/telemetry.py` applies this integration. |
| Workspace-based Application Insights uses tables including `AppRequests`, `AppTraces`, and `AppDependencies`. | [Application Insights log-based metrics](https://learn.microsoft.com/azure/azure-monitor/app/metrics-overview), [Application Insights data model](https://learn.microsoft.com/azure/azure-monitor/app/data-model-complete) | Official. A table has data only after the corresponding telemetry is emitted. |
| Failure Anomalies is a platform-managed smart detection capability associated with Application Insights. | [Failure Anomalies smart detection](https://learn.microsoft.com/azure/azure-monitor/alerts/proactive-failure-diagnostics) | Official. It is not part of the Service Health webhook flow. |
| `/readyz` validates configuration and client construction but does not execute a Table data-plane operation. | `service_health/routes.py`, `service_health/runtime.py`, and route tests | Implementation-specific. The runbook uses a signed notification and dependency telemetry for end-to-end verification. |

## Azure Developer CLI

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| AZD supports lifecycle hooks, infers the hook runtime from file extension, and installs Python dependencies from the nearest project file. | [Write AZD hooks in Python, JavaScript, TypeScript, or .NET](https://learn.microsoft.com/azure/developer/azure-developer-cli/hooks-multi-language#python-hooks) | Official. The project registers a Python `preprovision` hook in `azure.yaml`. |
| AZD can run a hook independently with `azd hooks run`. | [Azure Developer CLI command reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference) | Official. |
| `azd provision --preview` previews provisioning changes. | [Azure Developer CLI command reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference) | Official. |
| `azd env new` can bind a named environment to a subscription and location, `azd env select` sets the active environment, and AZD stores local configuration under `.azure/<environment-name>`. | [Azure Developer CLI command reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference#azd-env-new), [Work with AZD environments](https://learn.microsoft.com/azure/developer/azure-developer-cli/work-with-environments) | Official. The deployment guide creates and selects the environment before provider registration, hooks, or preview. |
| Run the project preprovision hook before the first preview so Entra-derived environment values exist. | `azure.yaml`, `scripts/configure_secure_webhook.py`, and hook tests | Implementation-specific. Microsoft documentation does not state a general guarantee about preview invoking lifecycle hooks. |
| AZD supports explicit `azd auth login`. It can also be configured to delegate authentication to Azure CLI. | [Azure Developer CLI command reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference#azd-auth-login), [Authenticate AZD through Azure CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/use-terraform-for-azd#option-1-sign-in-once-with-azure-cli-recommended) | Official. This runbook uses explicit Azure CLI and AZD sign-in commands. |
| `azd down` removes resources managed by the selected AZD environment, subject to Azure resource deletion behavior. It does not delete local application files. | [Azure Developer CLI command reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference#azd-down) | Official in general. It does not override Key Vault purge protection, discover peripheral resource groups created by the repository's day-2 command, or remove local AZD credential data. |
| `azd env remove <environment>` removes the local AZD environment and supports `--force` and inherited `--no-prompt` flags. | [Azure Developer CLI command reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference#azd-env-remove) | Official. This is a separate local cleanup step after `azd down`. |
| `azd env set` stores the expanded value in the selected local AZD environment file. Microsoft warns not to store secrets in AZD `.env` files and provides secret-reference alternatives. | [Manage AZD environment variables](https://learn.microsoft.com/azure/developer/azure-developer-cli/manage-environment-variables#secrets-and-sensitive-data-considerations) | Official. This project has a bootstrap limitation because its target vault is created by the same provision that consumes the token. The guide removes the plaintext entry after provision instead of relying on an `azd env unset` command that is not present in the current command reference. |
| Every current infrastructure provision consumes `SLACK_BOT_TOKEN` and writes it as the value of a new versioned Key Vault secret. The Bicep parameter does not reject an empty value. | `infra/main.parameters.json`, `infra/main.bicep`, and `infra/modules/security.bicep` | Implementation-specific. Operators must restore the real token before each provision, never supply an empty value, and remove the local copy afterward. A production-safe unattended flow requires a design change. |

## Day-2 scope management

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| A subscription Activity Log Alert requires management-plane permission to create the alert and its supporting resources. | [Azure Monitor roles, permissions, and security](https://learn.microsoft.com/azure/azure-monitor/roles-permissions-security), [Activity Log Alert resource reference](https://learn.microsoft.com/azure/templates/microsoft.insights/activitylogalerts) | Official for Azure permissions. `scripts/manage_alert_scopes.py` checks its exact operations before mutation. |
| Azure CLI can list a user's transitive group assignments and assignments inherited from parent scopes. | [List Azure role assignments](https://learn.microsoft.com/cli/azure/role/assignment#az-role-assignment-list) | Official. The deployment checkpoint uses `--include-groups` and `--include-inherited`; Microsoft Entra directory roles remain a separate check. |
| The repository's Management Group command enumerates accessible descendant subscriptions and manages one subscription path for each. | `scripts/manage_alert_scopes.py` and `test/test_manage_alert_scopes.py` | Implementation-specific. |
| New day-2 alerts are deployed disabled, tested, and enabled only after an accepted signed-test result. | `scripts/manage_alert_scopes.py` and its tests | Implementation-specific safety contract. |
| Live signed tests have returned operation state `Complete` and receiver state `Succeeded`; Microsoft REST examples commonly show `Completed`. | Maintainer deployment observations and [Action Groups test notifications REST API](https://learn.microsoft.com/rest/api/monitor/action-groups/create-notifications-at-action-group-resource-level) | Empirical for `Complete` and `Succeeded`. The command accepts only explicit tested variants; these observations are not promoted as universal platform values. |
| Day-2 peripheral resource groups are outside the central AZD resource group. | `scripts/manage_alert_scopes.py` and `infra/day2/service-health-alert-scope.bicep` | Implementation-specific. `azd down` does not remove them. |

## Application consistency guarantees

| Documentation claim | Evidence | Classification and limit |
|---|---|---|
| ETags and a short processing lease coordinate concurrent replicas. | `service_health/state.py`, `service_health/routes.py`, and concurrency tests | Implementation-specific. |
| Slack and Table Storage do not participate in one atomic transaction. | `service_health/routes.py` and Slack state-transition tests | Implementation-specific. A crash after a Slack write but before the Table checkpoint can leave an untracked root or duplicate a reply. |
| The initial routing decision is sticky for an incident. | `service_health/routing.py`, `service_health/state.py`, and routing tests | Implementation-specific. |
| The service returns retryable `503` responses for classified transient Slack or Storage failures. | `service_health/slack.py`, `service_health/routes.py`, and error-mapping tests | Implementation-specific and aligned with the documented Action Group retry status list. |
