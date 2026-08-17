import json
import os
import subprocess
import urllib.error
from datetime import datetime, timezone

import pytest

from scripts.manage_alert_scopes import (
    AzureCli,
    ScopeManagerError,
    WORKLOAD_TAG,
)
from scripts.configure_secure_webhook import (
    LEGACY_TOKEN_ENV_NAME,
    OPERATIONS_ACTION_GROUP_ENV_NAME,
    OPERATIONS_BACKUP_OWNER_ENV_NAME,
    OPERATIONS_ON_CALL_ENV_NAME,
    OPERATIONS_PRIMARY_OWNER_ENV_NAME,
    OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME,
    OPERATIONS_RUNBOOK_ENV_NAME,
    TOKEN_MIGRATION_MARKER_ENV_NAME,
)
from scripts.operation_lock import (
    DEFAULT_LOCK_NAME,
    OperationJournal,
    OperationLockError,
)
from scripts.manage_slack_token import (
    BASELINE_ALERT_ENV_NAME,
    DEPLOY_WORKLOAD_ENV_NAME,
    EXPECTED_BOT_USER_ID_ENV_NAME,
    EXPECTED_TEAM_ID_ENV_NAME,
    IncompleteProvisioningError,
    PREVIOUS_SECRET_VERSION_ENV_NAME,
    SECRET_LATEST_VERSION_ENV_NAME,
    SECRET_NAME,
    SECRET_VERSION_ENV_NAME,
    SanitizedAzureCliCredential,
    TOKEN_FORMAT_PATTERN,
    SlackTokenManager,
    _TemporaryRoleAssignment,
    _TemporaryVaultNetworkAccess,
    default_acceptance_checker,
    parse_dotenv_value,
    read_local_token,
    remove_local_token_line,
)
from fake_blob_lock import FakeBlobService


SUBSCRIPTION_ID = "central-sub"
RESOURCE_GROUP = "rg-central"
CONTAINER_APP_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    "/providers/Microsoft.App/containerApps/ca-central"
)
CENTRAL = {
    "EnvironmentName": "production",
    "SubscriptionId": SUBSCRIPTION_ID,
    "ResourceGroup": RESOURCE_GROUP,
    "ContainerAppId": CONTAINER_APP_ID,
    "ProtectedAlertEnabled": True,
}
VALID_TOKEN = "xoxb-valid-token-123"


class FakeAzure:
    """Models exactly the Azure CLI surface manage_slack_token.py depends
    on: caller identity, Key Vault discovery/network/RBAC, the shared ARM
    lock/journal resources, and nonsecret acceptance/active-deployment
    metadata checks."""

    def __init__(self):
        self.calls = []
        self.locks = {}
        self.blob_service_factory = FakeBlobService()
        self.locks = self.blob_service_factory.store
        self.deployments = {}
        self._etag_counter = 0
        self.vault_name = "kv-central"
        self.vault_id = (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
            "/providers/Microsoft.KeyVault/vaults/kv-central"
        )
        self.vault_uri = "https://kv-central.vault.azure.net/"
        self.vault_public_network_access = "Disabled"
        self.vault_default_action = "Deny"
        self.vault_bypass = "None"
        self.vault_ip_rules = []
        self.caller_object_id = "caller-object-id"
        self._assignment_counter = 0
        self.role_assignments = {}
        self.container_app_provisioning_state = "Succeeded"
        self.container_app_revision = "ca-central--ready"
        self.running_deployments = []

    def _next_etag(self):
        self._etag_counter += 1
        return f"etag-{self._etag_counter}"

    def invoke(self, *args):
        self.calls.append(args)
        arguments = list(args)
        if arguments[:2] == ["account", "show"]:
            return {
                "id": SUBSCRIPTION_ID,
                "tenantId": "central-tenant",
                "user": {"name": "operator@example.com"},
            }
        if arguments[:3] == ["deployment", "group", "list"]:
            return list(self.running_deployments)
        if arguments[0] == "rest":
            uri = arguments[arguments.index("--uri") + 1]
            if "/providers/Microsoft.Resources/deployments/" in uri:
                return self._deployment(arguments, uri)
            if "/providers/Microsoft.App/containerApps/" in uri:
                return {
                    "properties": {
                        "provisioningState": self.container_app_provisioning_state,
                        "latestReadyRevisionName": self.container_app_revision,
                    }
                }
            raise AssertionError(f"Unsupported rest uri: {uri}")
        if arguments[:2] == ["resource", "list"]:
            if (
                "--resource-type" in arguments
                and arguments[arguments.index("--resource-type") + 1]
                == "Microsoft.Storage/storageAccounts"
            ):
                return [
                    {
                        "name": "stlockcentral",
                        "tags": {
                            "service-health-purpose": "operation-lock"
                        },
                    }
                ]
            return [
                {
                    "name": self.vault_name,
                    "id": self.vault_id,
                    "tags": {"workload": WORKLOAD_TAG},
                }
            ]
        if arguments[:3] == ["storage", "account", "show"]:
            return {
                "primaryEndpoints": {
                    "blob": "https://stlockcentral.blob.core.windows.net/"
                }
            }
        if arguments[:4] == ["storage", "account", "keys", "list"]:
            return [{"value": "fake-lock-key"}]
        if arguments[:2] == ["keyvault", "show"]:
            return {
                "properties": {
                    "vaultUri": self.vault_uri,
                    "publicNetworkAccess": self.vault_public_network_access,
                    "networkAcls": {
                        "defaultAction": self.vault_default_action,
                        "bypass": self.vault_bypass,
                        "ipRules": [dict(rule) for rule in self.vault_ip_rules],
                    },
                }
            }
        if arguments[:2] == ["keyvault", "update"]:
            if "--public-network-access" in arguments:
                self.vault_public_network_access = arguments[
                    arguments.index("--public-network-access") + 1
                ]
            if "--default-action" in arguments:
                self.vault_default_action = arguments[
                    arguments.index("--default-action") + 1
                ]
            if "--bypass" in arguments:
                self.vault_bypass = arguments[arguments.index("--bypass") + 1]
            return None
        if arguments[:3] == ["keyvault", "network-rule", "add"]:
            ip_address = arguments[arguments.index("--ip-address") + 1]
            self.vault_ip_rules.append({"value": ip_address})
            return None
        if arguments[:3] == ["keyvault", "network-rule", "remove"]:
            ip_address = arguments[arguments.index("--ip-address") + 1]
            self.vault_ip_rules = [
                rule for rule in self.vault_ip_rules if rule["value"] != ip_address
            ]
            return None
        if arguments[:3] == ["ad", "signed-in-user", "show"]:
            return {"id": self.caller_object_id}
        if arguments[:3] == ["role", "assignment", "list"]:
            return [
                {
                    "id": assignment_id,
                    "name": assignment_id.rstrip("/").rsplit("/", 1)[-1],
                }
                for assignment_id in self.role_assignments
            ]
        if arguments[:3] == ["role", "assignment", "create"]:
            assignment_name = arguments[arguments.index("--name") + 1]
            assignment_id = (
                f"{self.vault_id}/providers/Microsoft.Authorization/"
                f"roleAssignments/{assignment_name}"
            )
            assignment = {"id": assignment_id, "name": assignment_name}
            self.role_assignments[assignment_id] = assignment
            return assignment
        if arguments[:3] == ["role", "assignment", "delete"]:
            assignment_id = arguments[arguments.index("--ids") + 1]
            self.role_assignments.pop(assignment_id, None)
            return None
        if arguments[:3] == ["containerapp", "revision", "restart"]:
            return None
        raise AssertionError(f"Unsupported call: {arguments}")

    @staticmethod
    def _name(uri, marker):
        return uri.split(marker, 1)[1].split("?", 1)[0]

    @staticmethod
    def _headers(arguments):
        if "--headers" not in arguments:
            return []
        start = arguments.index("--headers") + 1
        values = []
        for value in arguments[start:]:
            if value == "--body":
                break
            values.append(value)
        return values

    @staticmethod
    def _body(arguments):
        if "--body" not in arguments:
            return None
        path = arguments[arguments.index("--body") + 1][1:]
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _lock(self, arguments, uri):
        method = arguments[arguments.index("--method") + 1].lower()
        name = DEFAULT_LOCK_NAME
        headers = self._headers(arguments)
        if method == "get":
            resource = self.locks.get(name)
            if resource is None:
                raise ScopeManagerError(
                    f"Azure CLI command failed for {uri}",
                    status_code=404,
                    error_code="NotFound",
                )
            return resource
        if method == "put":
            if name in self.locks:
                raise ScopeManagerError(f"Azure CLI command failed: 409 Conflict for {uri}")
            resource = {
                "id": uri,
                "name": name,
                "etag": self._next_etag(),
                "properties": self._body(arguments)["properties"],
            }
            self.locks[name] = resource
            return resource
        if method == "delete":
            resource = self.locks.get(name)
            if resource is None:
                return None
            for header in headers:
                if header.startswith("If-Match=") and header.split("=", 1)[1] != resource["etag"]:
                    raise ScopeManagerError(f"Azure CLI command failed: 412 PreconditionFailed for {uri}")
            del self.locks[name]
            return None
        raise AssertionError(f"Unsupported lock method: {method}")

    def _deployment(self, arguments, uri):
        method = arguments[arguments.index("--method") + 1].lower()
        name = self._name(uri, "/deployments/")
        if method == "get":
            resource = self.deployments.get(name)
            if resource is None:
                raise ScopeManagerError(
                    f"Azure CLI command failed for {uri}",
                    status_code=404,
                    error_code="DeploymentNotFound",
                )
            return resource
        if method == "put":
            value = self._body(arguments)["properties"]["template"]["outputs"]["journalState"]["value"]
            resource = {
                "etag": self._next_etag(),
                "properties": {
                    "outputs": {"journalState": {"value": value}}
                },
            }
            self.deployments[name] = resource
            return resource
        if method == "delete":
            resource = self.deployments.get(name)
            for header in self._headers(arguments):
                if (
                    header.startswith("If-Match=")
                    and resource is not None
                    and header.split("=", 1)[1] != resource["etag"]
                ):
                    raise ScopeManagerError(
                        "Journal precondition failed",
                        status_code=412,
                        error_code="PreconditionFailed",
                    )
            self.deployments.pop(name, None)
            return None
        raise AssertionError(f"Unsupported deployment method: {method}")


class FakeAzd:
    def __init__(self, values=None, environments=None):
        operations_action_group = (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-operations/"
            "providers/Microsoft.Insights/actionGroups/ag-operations"
        )
        self.values = {
            "AZURE_ENV_NAME": "production",
            "SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID": (
                operations_action_group
            ),
            "SERVICE_HEALTH_OPERATIONS_PRIMARY_OWNER": "primary@example.com",
            "SERVICE_HEALTH_OPERATIONS_BACKUP_OWNER": "backup@example.com",
            "SERVICE_HEALTH_OPERATIONS_ON_CALL_DESTINATION": "24x7-on-call",
            "SERVICE_HEALTH_OPERATIONS_RUNBOOK_URI": (
                "https://example.com/runbooks/service-health"
            ),
            "SERVICE_HEALTH_OPERATIONS_RECEIVER_TEST_EVIDENCE": json.dumps(
                {
                    "status": "Succeeded",
                    "testedAt": datetime.now(timezone.utc).isoformat(),
                    "actionGroupId": operations_action_group,
                }
            ),
            **dict(values or {}),
        }
        self.environments = environments if environments is not None else []
        self.provision_calls = 0
        self.get_calls = []

    def get_environment_value(self, name):
        self.get_calls.append(name)
        return self.values.get(name, "")

    def set_environment_value(self, name, value):
        self.values[name] = value

    def list_environments(self):
        return self.environments

    def provision(self):
        self.provision_calls += 1


class FakeSecretProperties:
    def __init__(self, version, enabled=True):
        self.version = version
        self.enabled = enabled


class FakeSecret:
    def __init__(self, version, value, enabled=True):
        self.properties = FakeSecretProperties(version, enabled)
        self.value = value


class FakeSecretClient:
    def __init__(self):
        self.secrets = {}
        self._version_counter = 0
        self.set_calls = []
        self.get_calls = []

    def set_secret(self, name, value):
        self.set_calls.append((name, value))
        self._version_counter += 1
        version = f"v{self._version_counter}"
        secret = FakeSecret(version, value)
        self.secrets[name] = secret
        return secret

    def get_secret(self, name, version=None):
        self.get_calls.append((name, version))
        if name not in self.secrets:
            raise KeyError(name)
        return self.secrets[name]

    def list_properties_of_secrets(self):
        return [
            secret.properties for secret in self.secrets.values()
        ]


class FakeSlackClient:
    def __init__(self, token, team_id="T111", user_id="U222"):
        self.token = token
        self.team_id = team_id
        self.user_id = user_id
        self.auth_test_calls = 0

    def auth_test(self):
        self.auth_test_calls += 1
        return {"ok": True, "team_id": self.team_id, "user_id": self.user_id}


def make_manager(
    azure=None,
    azd=None,
    prompt_token=None,
    secret_client=None,
    slack_client=None,
    public_ip="203.0.113.5",
    acceptance_checker=None,
    provisioning_checker=None,
    active_deployment_checker=None,
    sleep=None,
):
    secret_client = secret_client or FakeSecretClient()
    slack_holder = {}

    def slack_client_factory(token):
        client = slack_client or FakeSlackClient(token)
        slack_holder["client"] = client
        return client

    manager = SlackTokenManager(
        azure or FakeAzure(),
        azd or FakeAzd(),
        credential_factory=lambda: "fake-credential",
        secret_client_factory=lambda vault_uri, credential: secret_client,
        slack_client_factory=slack_client_factory,
        prompt_token=prompt_token or (lambda: VALID_TOKEN),
        public_ip_resolver=lambda: public_ip,
        resource_not_found_exception_factory=lambda: KeyError,
        acceptance_checker=acceptance_checker or (lambda azure, central: None),
        provisioning_checker=provisioning_checker
        or (lambda azure, central: None),
        active_deployment_checker=active_deployment_checker or (lambda azure, central: False),
        sleep=sleep or (lambda _seconds: None),
    )
    manager.scope_manager.get_central_deployment = lambda: dict(CENTRAL)
    manager._central = dict(CENTRAL)
    manager.requested_environment_name = "production"
    manager._secret_client = secret_client
    manager._slack_holder = slack_holder
    return manager


def operation_journal(azure, operation_id="test-operation"):
    journal = OperationJournal(azure, SUBSCRIPTION_ID, RESOURCE_GROUP)
    journal.record(
        operation_id,
        {"Command": "test", "Target": "test", "State": "Started"},
    )
    return journal, operation_id


def test_key_vault_credential_uses_scrubbed_cli_environment(monkeypatch):
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "accessToken": "azure-access-token",
                    "expires_on": 2000000000,
                }
            ),
            "",
        )

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-sdk-boundary-canary")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "sp-secret")
    credential = SanitizedAzureCliCredential(
        AzureCli(runner=runner)
    )

    token = credential.get_token(
        "https://vault.azure.net/.default"
    )

    assert token.token == "azure-access-token"
    assert "SLACK_BOT_TOKEN" not in captured["env"]
    assert captured["env"]["AZURE_CLIENT_SECRET"] == "sp-secret"
    assert "xoxb-sdk-boundary-canary" not in str(captured)


def test_infrastructure_only_fallback_uses_nonsecret_selected_target():
    azure = FakeAzure()
    azd = FakeAzd(
        {
            "AZURE_ENV_NAME": "production",
            "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
            "AZURE_TENANT_ID": "central-tenant",
            "AZURE_RESOURCE_GROUP": RESOURCE_GROUP,
        }
    )
    manager = make_manager(azure, azd)
    manager._central = None

    def no_workload_deployment():
        raise ScopeManagerError("Container App not found")

    manager.scope_manager.get_central_deployment = no_workload_deployment

    assert manager.central() == {
        "EnvironmentName": "production",
        "TenantId": "central-tenant",
        "SubscriptionId": SUBSCRIPTION_ID,
        "ResourceGroup": RESOURCE_GROUP,
        "Location": "",
    }


def test_explicit_environment_mismatch_fails_before_mutation():
    manager = make_manager(azd=FakeAzd({"AZURE_ENV_NAME": "other"}))
    manager.requested_environment_name = "production"

    with pytest.raises(ScopeManagerError, match="does not match requested"):
        manager.central()


def test_mutation_requires_explicit_environment_before_provision():
    azd = FakeAzd()
    manager = make_manager(azd=azd)
    manager.requested_environment_name = None

    with pytest.raises(ScopeManagerError, match="--environment-name"):
        manager.bootstrap()

    assert azd.provision_calls == 0


def test_missing_atomic_lock_storage_is_bootstrapped_infrastructure_only(
    monkeypatch,
):
    azd = FakeAzd({DEPLOY_WORKLOAD_ENV_NAME: "true"})
    manager = make_manager(azd=azd)
    states = iter([False, True])
    monkeypatch.setattr(
        manager, "_lock_storage_exists", lambda: next(states)
    )

    manager._ensure_lock_infrastructure()

    assert azd.provision_calls == 1
    assert azd.values[DEPLOY_WORKLOAD_ENV_NAME] == "true"


def test_default_acceptance_checks_probes_auth_boundary_and_signed_delivery(
    monkeypatch,
):
    azure = FakeAzure()
    real_invoke = azure.invoke

    def invoke(*args):
        if args[:4] == (
            "monitor",
            "action-group",
            "test-notifications",
            "create",
        ):
            azure.calls.append(args)
            return {
                "state": "Completed",
                "actionDetails": [
                    {
                        "Name": "slack-service-health",
                        "MechanismType": "SecureWebhook",
                        "Status": "Succeeded",
                    }
                ],
            }
        return real_invoke(*args)

    azure.invoke = invoke
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def urlopen(request, timeout):
        requests.append(request)
        if not isinstance(request, str):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None
            )
        return Response()

    monkeypatch.setattr(
        "scripts.manage_slack_token.urllib.request.urlopen", urlopen
    )
    central = {
        **CENTRAL,
        "WebhookUri": "https://app.example/api/service-health",
        "AnchorActionGroupId": (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
            "/providers/Microsoft.Insights/actionGroups/ag-central"
        ),
        "SecureWebhookObjectId": "app-object-id",
        "SecureWebhookIdentifierUri": "api://service-health",
    }

    default_acceptance_checker(azure, central)

    assert requests[:2] == [
        "https://app.example/healthz",
        "https://app.example/readyz",
    ]
    assert requests[2].full_url == central["WebhookUri"]
    assert any(
        call[:4]
        == (
            "monitor",
            "action-group",
            "test-notifications",
            "create",
        )
        for call in azure.calls
    )


def _dotenv_azd(tmp_path, environment_name, secret=None, values=None):
    dotenv_path = tmp_path / f"{environment_name}.env"
    contents = []
    if secret is not None:
        contents.append(f"{LEGACY_TOKEN_ENV_NAME}={secret}")
    dotenv_path.write_text(
        "\n".join(contents) + ("\n" if contents else ""), encoding="utf-8"
    )
    azd = FakeAzd(
        values=values,
        environments=[{"Name": environment_name, "DotEnvPath": str(dotenv_path)}],
    )
    return azd, dotenv_path


def test_status_is_token_free_and_touches_no_lock_or_journal():
    azure = FakeAzure()
    azd = FakeAzd({SECRET_LATEST_VERSION_ENV_NAME: "v3"})
    manager = make_manager(azure, azd)

    result = manager.status()

    assert result == {
        "Environment": "production",
        "KeyVaultName": "kv-central",
        "SecretVersion": "",
        "LatestSecretVersion": "v3",
        "PreviousSecretVersion": "",
        "LegacyTokenPresent": False,
        "MigrationMarkerSet": False,
        "Bootstrapped": True,
    }
    assert azure.locks == {}
    assert azure.deployments == {}
    assert not any(
        call[0] == "rest" and "locks" in call[call.index("--uri") + 1]
        for call in azure.calls
        if call[0] == "rest"
    )


def test_status_never_calls_get_environment_value_for_legacy_token(tmp_path):
    azd, _ = _dotenv_azd(tmp_path, "production", secret="xoxb-legacy")
    manager = make_manager(azd=azd)

    manager.status()

    assert LEGACY_TOKEN_ENV_NAME not in azd.get_calls


def test_status_reports_legacy_token_and_migration_marker(tmp_path):
    azd, _ = _dotenv_azd(
        tmp_path,
        "production",
        secret="xoxb-legacy",
        values={TOKEN_MIGRATION_MARKER_ENV_NAME: "true"},
    )
    manager = make_manager(azd=azd)

    result = manager.status()

    assert result["LegacyTokenPresent"] is True
    assert result["MigrationMarkerSet"] is True
    assert result["Bootstrapped"] is False


def test_status_bootstrapped_is_false_if_legacy_token_still_present_locally(tmp_path):
    azd, _ = _dotenv_azd(
        tmp_path,
        "production",
        secret="xoxb-legacy",
        values={SECRET_LATEST_VERSION_ENV_NAME: "v7"},
    )
    manager = make_manager(azd=azd)

    result = manager.status()

    assert result["Bootstrapped"] is False


def test_status_tolerates_unresolvable_key_vault():
    azure = FakeAzure()

    def broken_invoke(*args):
        if list(args)[:2] == ["resource", "list"]:
            return []
        return FakeAzure.invoke(azure, *args)

    azure.invoke = broken_invoke
    manager = make_manager(azure)

    result = manager.status()

    assert result["KeyVaultName"] is None


def test_bootstrap_writes_secret_and_cleans_up_temporary_access():
    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(azure, azd)

    result = manager.bootstrap()

    assert result["Status"] == "Bootstrapped"
    assert result["SecretVersion"] == "v1"
    assert azd.values[SECRET_LATEST_VERSION_ENV_NAME] == "v1"
    assert azd.values[SECRET_VERSION_ENV_NAME] == ""
    assert azd.values[TOKEN_MIGRATION_MARKER_ENV_NAME] == "true"
    assert azd.values[EXPECTED_TEAM_ID_ENV_NAME] == "T111"
    assert azd.values[EXPECTED_BOT_USER_ID_ENV_NAME] == "U222"
    # Disabled-first, two-phase: workload enabled only after the secret is
    # written; baseline alert stays disabled as a separate operator choice.
    assert azd.values[DEPLOY_WORKLOAD_ENV_NAME] == "true"
    assert azd.values[BASELINE_ALERT_ENV_NAME] == "false"
    assert azd.provision_calls == 2
    # Temporary network and RBAC access must be fully rolled back.
    assert azure.vault_ip_rules == []
    assert azure.role_assignments == {}
    # The shared lock/journal must be released and cleared on success.
    assert azure.locks == {}
    assert azure.deployments == {}
    (secret_name, secret_value) = manager._secret_client.set_calls[0]
    assert secret_name == SECRET_NAME
    assert secret_value == VALID_TOKEN


def test_bootstrap_waits_for_temporary_rbac_propagation():
    class ForbiddenError(RuntimeError):
        status_code = 403
        error_code = "Forbidden"

    class DelayedSecretClient(FakeSecretClient):
        def __init__(self):
            super().__init__()
            self.readiness_calls = 0

        def list_properties_of_secrets(self):
            self.readiness_calls += 1
            if self.readiness_calls < 3:
                raise ForbiddenError("role not propagated")
            return super().list_properties_of_secrets()

    delays = []
    credentials = []
    secret_client = DelayedSecretClient()
    manager = make_manager(
        secret_client=secret_client,
        sleep=delays.append,
    )
    manager.credential_factory = lambda: (
        credentials.append(object()) or credentials[-1]
    )

    result = manager.bootstrap()

    assert result["Status"] == "Bootstrapped"
    assert secret_client.readiness_calls == 3
    assert delays == [2, 4]
    assert len(credentials) == 3


def test_bootstrap_fails_closed_after_bounded_rbac_window():
    class ForbiddenError(RuntimeError):
        status_code = 403
        error_code = "Forbidden"

    class BlockedSecretClient(FakeSecretClient):
        def list_properties_of_secrets(self):
            raise ForbiddenError("still forbidden")

    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(
        azure=azure,
        azd=azd,
        secret_client=BlockedSecretClient(),
    )

    with pytest.raises(ScopeManagerError, match="bounded 10-minute"):
        manager.bootstrap()

    assert azd.provision_calls == 1
    assert azure.role_assignments == {}
    assert azure.vault_ip_rules == []
    assert azure.locks == {}
    assert len(azure.deployments) == 1


def test_bootstrap_is_idempotent_when_already_bootstrapped():
    azure = FakeAzure()
    azd = FakeAzd({SECRET_LATEST_VERSION_ENV_NAME: "v9"})
    manager = make_manager(azure, azd)

    result = manager.bootstrap()

    assert result == {"Status": "AlreadyBootstrapped", "SecretVersion": "v9"}
    assert azure.calls == []
    assert azd.provision_calls == 0


def test_bootstrap_resumes_token_free_after_final_provision_failure():
    azure = FakeAzure()
    azd = FakeAzd({SECRET_LATEST_VERSION_ENV_NAME: "v9"})
    checks = iter([
        IncompleteProvisioningError("not provisioned"),
        None,
    ])

    def provisioning_checker(_azure, _central):
        result = next(checks)
        if result is not None:
            raise result

    manager = make_manager(
        azure,
        azd,
        prompt_token=lambda: (_ for _ in ()).throw(
            AssertionError("token prompt must not run")
        ),
        provisioning_checker=provisioning_checker,
    )

    result = manager.bootstrap()

    assert result == {
        "Status": "BootstrapRecovered",
        "SecretVersion": "v9",
    }
    assert azd.provision_calls == 1
    assert azd.values[DEPLOY_WORKLOAD_ENV_NAME] == "true"
    assert azd.values[SECRET_VERSION_ENV_NAME] == ""
    assert azure.locks == {}
    assert azure.deployments == {}


def test_bootstrap_does_not_mutate_on_provisioning_check_read_failure():
    azure = FakeAzure()
    azd = FakeAzd({SECRET_LATEST_VERSION_ENV_NAME: "v9"})
    manager = make_manager(
        azure,
        azd,
        provisioning_checker=lambda _azure, _central: (
            (_ for _ in ()).throw(
                ScopeManagerError("authorization failed")
            )
        ),
    )

    with pytest.raises(ScopeManagerError, match="authorization failed"):
        manager.bootstrap()

    assert azd.provision_calls == 0
    assert azure.locks == {}
    assert azure.deployments == {}


def test_bootstrap_phase_one_provisions_before_any_vault_lookup():
    azure = FakeAzure()
    azd = FakeAzd()
    order = []
    real_invoke = azure.invoke
    real_provision = azd.provision

    def tracked_invoke(*args):
        order.append(("azure", args[0]))
        return real_invoke(*args)

    def tracked_provision():
        order.append(("azd", "provision"))
        return real_provision()

    azure.invoke = tracked_invoke
    azd.provision = tracked_provision
    manager = make_manager(azure, azd)

    manager.bootstrap()

    provision_indexes = [i for i, entry in enumerate(order) if entry == ("azd", "provision")]
    vault_lookup_indexes = [i for i, entry in enumerate(order) if entry[1] == "resource"]
    assert len(provision_indexes) == 2
    assert provision_indexes[0] < vault_lookup_indexes[0]
    assert provision_indexes[1] > vault_lookup_indexes[0]


def test_bootstrap_rejects_invalid_token_format_before_any_mutation():
    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(azure, azd, prompt_token=lambda: "not-a-slack-token")

    with pytest.raises(ScopeManagerError, match="xoxb"):
        manager.bootstrap()

    assert azure.vault_ip_rules == []
    assert azure.role_assignments == {}
    assert SECRET_LATEST_VERSION_ENV_NAME not in azd.values
    assert azure.locks == {}
    assert azure.deployments == {}


def test_interactive_inputs_are_collected_before_distributed_lock():
    azure = FakeAzure()
    azd = FakeAzd()
    input_states = []

    def prompt_token():
        input_states.append(("token", bool(azure.locks)))
        return VALID_TOKEN

    manager = make_manager(azure, azd, prompt_token=prompt_token)

    def resolve_ip():
        input_states.append(("ip", bool(azure.locks)))
        return "203.0.113.5"

    manager.public_ip_resolver = resolve_ip

    manager.bootstrap()

    assert input_states == [("token", False), ("ip", False)]


def test_bootstrap_rejects_unexpected_slack_team():
    azure = FakeAzure()
    azd = FakeAzd({EXPECTED_TEAM_ID_ENV_NAME: "T999"})
    manager = make_manager(azure, azd)

    with pytest.raises(ScopeManagerError, match="unexpected Slack team"):
        manager.bootstrap()

    assert SECRET_LATEST_VERSION_ENV_NAME not in azd.values
    assert azure.vault_ip_rules == []


def test_bootstrap_never_exposes_token_in_calls_or_errors():
    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(azure, azd, prompt_token=lambda: "not-a-token")

    with pytest.raises(ScopeManagerError) as exc_info:
        manager.bootstrap()

    serialized_calls = json.dumps(azure.calls, default=str)
    assert "not-a-token" not in serialized_calls
    assert "not-a-token" not in str(exc_info.value)
    assert VALID_TOKEN not in serialized_calls


def test_bootstrap_cleans_up_temporary_access_even_when_secret_write_fails():
    azure = FakeAzure()
    azd = FakeAzd()
    secret_client = FakeSecretClient()

    def failing_set_secret(name, value):
        raise RuntimeError("vault unavailable")

    secret_client.set_secret = failing_set_secret
    manager = make_manager(azure, azd, secret_client=secret_client)

    with pytest.raises(ScopeManagerError, match="vault unavailable"):
        manager.bootstrap()

    assert azure.vault_ip_rules == []
    assert azure.role_assignments == {}
    assert azure.locks == {}
    (entry,) = azure.deployments.values()
    assert entry["properties"]["outputs"]["journalState"]["value"]["State"] == "Failed"


def test_bootstrap_releases_lock_when_initial_journal_write_fails():
    class JournalWriteFailure(FakeAzure):
        def _deployment(self, arguments, uri):
            method = arguments[arguments.index("--method") + 1].lower()
            if method == "put":
                raise ScopeManagerError("journal unavailable")
            return super()._deployment(arguments, uri)

    azure = JournalWriteFailure()
    manager = make_manager(azure)

    with pytest.raises(ScopeManagerError, match="journal unavailable"):
        manager.bootstrap()

    assert azure.locks == {}


@pytest.mark.parametrize("final_state", ["Failed", "Completed"])
def test_mutation_releases_lock_when_final_journal_write_fails(final_state):
    class FinalJournalFailure(FakeAzure):
        def _deployment(self, arguments, uri):
            method = arguments[arguments.index("--method") + 1].lower()
            if method == "put":
                state = self._body(arguments)["properties"]["template"][
                    "outputs"
                ]["journalState"]["value"]["State"]
                if state == final_state:
                    raise ScopeManagerError(
                        f"{final_state} journal unavailable"
                    )
            return super()._deployment(arguments, uri)

    azure = FinalJournalFailure()
    manager = make_manager(azure)

    if final_state == "Failed":
        with pytest.raises(RuntimeError, match="primary mutation failure"):
            manager._mutate(
                "test",
                "target",
                lambda _renew: (_ for _ in ()).throw(
                    RuntimeError("primary mutation failure")
                ),
            )
    else:
        with pytest.raises(
            OperationLockError, match="journal could not be finalized"
        ):
            manager._mutate(
                "test",
                "target",
                lambda _renew: {"Status": "Succeeded"},
            )

    assert azure.locks == {}


def test_mutation_preserves_primary_error_when_final_journal_read_fails():
    class FinalJournalReadFailure(FakeAzure):
        def _deployment(self, arguments, uri):
            method = arguments[arguments.index("--method") + 1].lower()
            if method == "get" and self.deployments:
                raise ScopeManagerError("journal read unavailable")
            return super()._deployment(arguments, uri)

    azure = FinalJournalReadFailure()
    manager = make_manager(azure)

    with pytest.raises(RuntimeError, match="primary mutation failure"):
        manager._mutate(
            "test",
            "target",
            lambda _renew: (_ for _ in ()).throw(
                RuntimeError("primary mutation failure")
            ),
        )

    assert azure.locks == {}


def test_execute_surfaces_lock_contention_as_operation_lock_error():
    azure = FakeAzure()
    azure.locks[DEFAULT_LOCK_NAME] = {
        "data": json.dumps(
            {
                "environment": "production",
                "command": "add-subscription",
                "target": "subscription 'other'",
                "caller": "someone-else@example.com",
                "nonce": "other-nonce",
                "startedAt": 1000.0,
                "expiresAt": 100000000.0,
            }
        ).encode(),
        "lease_id": "existing-lease",
    }
    manager = make_manager(azure)

    with pytest.raises(
        OperationLockError, match="another operation appears to be in progress"
    ):
        manager.bootstrap()

    assert azure.role_assignments == {}
    assert azure.vault_ip_rules == []


def test_rotate_validates_before_write_and_retains_previous_version():
    azure = FakeAzure()
    azd = FakeAzd({SECRET_VERSION_ENV_NAME: ""})
    secret_client = FakeSecretClient()
    secret_client.secrets[SECRET_NAME] = FakeSecret("v1", "old-token")
    secret_client._version_counter = 1
    order = []
    slack_client = FakeSlackClient(VALID_TOKEN)
    real_auth_test = slack_client.auth_test

    def tracked_auth_test():
        order.append("validate")
        return real_auth_test()

    slack_client.auth_test = tracked_auth_test
    real_set_secret = secret_client.set_secret

    def tracked_set_secret(name, value):
        order.append("write")
        return real_set_secret(name, value)

    secret_client.set_secret = tracked_set_secret
    manager = make_manager(azure, azd, secret_client=secret_client, slack_client=slack_client)

    result = manager.rotate()

    assert order == ["validate", "write"]
    assert result["SecretVersion"] == "v2"
    assert result["PreviousSecretVersion"] == "v1"
    assert azd.values[SECRET_LATEST_VERSION_ENV_NAME] == "v2"
    assert azd.values[SECRET_VERSION_ENV_NAME] == ""
    assert azd.values[PREVIOUS_SECRET_VERSION_ENV_NAME] == "v1"
    assert azd.provision_calls == 1
    assert azure.vault_ip_rules == []
    assert azure.role_assignments == {}


def test_rotate_with_no_existing_secret_leaves_previous_version_unset():
    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(azure, azd)

    result = manager.rotate()

    assert result["PreviousSecretVersion"] == ""
    assert PREVIOUS_SECRET_VERSION_ENV_NAME not in azd.values


def test_first_secret_acceptance_timeout_is_rollback_not_possible():
    def timed_out(_azure, _central):
        raise TimeoutError("acceptance timed out")

    manager = make_manager(acceptance_checker=timed_out)

    with pytest.raises(
        ScopeManagerError,
        match="ROLLBACK_NOT_POSSIBLE.*no prior enabled secret version",
    ):
        manager.rotate()


def test_rotate_rolls_back_pin_and_reprovisions_when_acceptance_check_fails():
    azure = FakeAzure()
    azd = FakeAzd()
    secret_client = FakeSecretClient()
    secret_client.secrets[SECRET_NAME] = FakeSecret("v1", "old-token")
    secret_client._version_counter = 1

    acceptance_calls = 0

    def failing_once_acceptance(azure_arg, central_arg):
        nonlocal acceptance_calls
        acceptance_calls += 1
        if acceptance_calls == 1:
            raise ScopeManagerError("container app not healthy")

    manager = make_manager(
        azure,
        azd,
        secret_client=secret_client,
        acceptance_checker=failing_once_acceptance,
    )

    with pytest.raises(ScopeManagerError, match="ROLLBACK_PINNED"):
        manager.rotate()

    assert azd.values[SECRET_VERSION_ENV_NAME] == "v1"
    assert azd.values[SECRET_LATEST_VERSION_ENV_NAME] == "v2"
    assert azd.provision_calls == 2


def test_rollback_sets_only_secret_version_then_provisions_token_free():
    azure = FakeAzure()
    azd = FakeAzd(
        {
            SECRET_VERSION_ENV_NAME: "",
            PREVIOUS_SECRET_VERSION_ENV_NAME: "v1",
        }
    )
    manager = make_manager(azure, azd)

    result = manager.rollback()

    assert result == {"Status": "ROLLED_BACK", "SecretVersion": "v1"}
    assert azd.values[SECRET_VERSION_ENV_NAME] == "v1"
    assert azd.provision_calls == 1
    # Rollback must never touch Key Vault, network, or RBAC state.
    assert azure.vault_ip_rules == []
    assert azure.role_assignments == {}
    assert not any(call[:2] == ["keyvault", "show"] for call in azure.calls)


def test_rollback_fails_closed_without_a_prior_version():
    manager = make_manager(azd=FakeAzd())

    with pytest.raises(ScopeManagerError, match="ROLLBACK_NOT_POSSIBLE"):
        manager.rollback()


def test_rollback_raises_when_acceptance_check_fails():
    azd = FakeAzd({PREVIOUS_SECRET_VERSION_ENV_NAME: "v1"})

    def failing_acceptance(azure_arg, central_arg):
        raise ScopeManagerError("still not healthy")

    manager = make_manager(azd=azd, acceptance_checker=failing_acceptance)

    with pytest.raises(ScopeManagerError, match="INDETERMINATE.*still not healthy"):
        manager.rollback()


def test_automatic_rollback_reports_failed_when_prior_pin_cannot_provision():
    class RollbackFailingAzd(FakeAzd):
        def provision(self):
            super().provision()
            if self.provision_calls == 2:
                raise ScopeManagerError("pin deployment failed")

    secret_client = FakeSecretClient()
    secret_client.secrets[SECRET_NAME] = FakeSecret("v1", "old-token")
    secret_client._version_counter = 1
    manager = make_manager(
        azd=RollbackFailingAzd(),
        secret_client=secret_client,
        acceptance_checker=lambda _azure, _central: (
            (_ for _ in ()).throw(ScopeManagerError("acceptance failed"))
        ),
    )

    with pytest.raises(
        ScopeManagerError, match="ROLLBACK_FAILED.*pin deployment failed"
    ):
        manager.rotate()


@pytest.mark.parametrize(
    "missing_name",
    (
        OPERATIONS_ACTION_GROUP_ENV_NAME,
        OPERATIONS_PRIMARY_OWNER_ENV_NAME,
        OPERATIONS_BACKUP_OWNER_ENV_NAME,
        OPERATIONS_ON_CALL_ENV_NAME,
        OPERATIONS_RUNBOOK_ENV_NAME,
        OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME,
    ),
)
def test_production_readiness_fails_when_required_metadata_is_missing(
    missing_name,
):
    azd = FakeAzd()
    del azd.values[missing_name]
    manager = make_manager(azd=azd)

    with pytest.raises(ScopeManagerError, match=missing_name):
        manager.assert_production_readiness()


def test_production_readiness_rejects_unproven_receiver_evidence():
    azd = FakeAzd()
    evidence = json.loads(
        azd.values[OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME]
    )
    evidence["status"] = "Failed"
    azd.values[OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME] = json.dumps(evidence)

    with pytest.raises(ScopeManagerError, match="successful test"):
        make_manager(azd=azd).assert_production_readiness()


def test_recover_lock_requires_force():
    azure = FakeAzure()
    manager = make_manager(azure)
    azure.locks[DEFAULT_LOCK_NAME] = {
        "data": json.dumps(
            {
                "environment": "production",
                "command": "bootstrap",
                "target": f"Key Vault secret '{SECRET_NAME}'",
                "caller": "operator@example.com",
                "nonce": "abandoned-nonce",
                "startedAt": 1000.0,
                "expiresAt": 1000.0,
            }
        ).encode(),
        "lease_id": None,
    }

    with pytest.raises(OperationLockError, match="explicit"):
        manager.recover_lock(force=False)

    result = manager.recover_lock(force=True)
    assert result["Status"] == "Recovered"
    assert result["PriorMetadata"]["nonce"] == "abandoned-nonce"


def test_recover_lock_refuses_when_a_deployment_is_actively_running():
    from scripts.manage_slack_token import default_active_deployment_checker

    azure = FakeAzure()
    azure.running_deployments = [
        {"properties": {"provisioningState": "Running"}}
    ]
    manager = make_manager(azure, active_deployment_checker=default_active_deployment_checker)

    with pytest.raises(ScopeManagerError, match="actively running"):
        manager.recover_lock(force=True)


def test_recover_lock_uses_default_active_deployment_checker_against_azure_cli():
    azure = FakeAzure()
    azure.running_deployments = []
    manager = make_manager(azure)
    manager.active_deployment_checker = None

    from scripts.manage_slack_token import default_active_deployment_checker

    manager.active_deployment_checker = default_active_deployment_checker
    # No lock present: recover() itself reports AlreadyAbsent, but the
    # active-deployment check must still have been consulted first.
    result = manager.recover_lock(force=True)
    assert result["Status"] == "AlreadyAbsent"
    assert any(call[:3] == ("deployment", "group", "list") for call in azure.calls)


def test_local_token_line_removal_is_atomic_and_preserves_permissions(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "OTHER_VAR=keep-me\nSLACK_BOT_TOKEN=xoxb-secret-value\nANOTHER=also-keep\n",
        encoding="utf-8",
    )
    dotenv_path.chmod(0o600)

    assert read_local_token(dotenv_path) == "xoxb-secret-value"

    removed = remove_local_token_line(dotenv_path)

    assert removed is True
    remaining = dotenv_path.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN" not in remaining
    assert "OTHER_VAR=keep-me" in remaining
    assert "ANOTHER=also-keep" in remaining
    if os.name != "nt":
        assert (dotenv_path.stat().st_mode & 0o777) == 0o600


def test_local_token_line_removal_is_idempotent_when_file_missing(tmp_path):
    missing_path = tmp_path / ".env"
    assert remove_local_token_line(missing_path) is False


def test_local_token_helpers_require_exact_beginning_of_line_match(tmp_path):
    dotenv_path = tmp_path / ".env"
    # An indented occurrence must never be treated as a real assignment.
    dotenv_path.write_text("  SLACK_BOT_TOKEN=xoxb-should-not-match\n", encoding="utf-8")

    with pytest.raises(ScopeManagerError, match="No local"):
        read_local_token(dotenv_path)

    assert remove_local_token_line(dotenv_path) is False
    assert "SLACK_BOT_TOKEN" in dotenv_path.read_text(encoding="utf-8")


def test_read_local_token_fails_closed_on_malformed_quotes(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text('SLACK_BOT_TOKEN="unterminated\n', encoding="utf-8")

    with pytest.raises(ScopeManagerError, match="unterminated quote"):
        read_local_token(dotenv_path)


def test_parse_dotenv_value_strips_matching_quotes_only():
    assert parse_dotenv_value('"xoxb-abc"') == "xoxb-abc"
    assert parse_dotenv_value("xoxb-abc") == "xoxb-abc"
    with pytest.raises(ScopeManagerError):
        parse_dotenv_value('"unterminated')


def test_migrate_reads_local_token_removes_line_and_writes_secret(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SLACK_BOT_TOKEN=" + VALID_TOKEN + "\n", encoding="utf-8")
    azure = FakeAzure()
    azd = FakeAzd(
        {
            DEPLOY_WORKLOAD_ENV_NAME: "false",
            BASELINE_ALERT_ENV_NAME: "true",
        }
    )
    manager = make_manager(azure, azd)

    result = manager.migrate(dotenv_path)

    assert result["Status"] == "Migrated"
    assert result["LocalTokenRemoved"] is True
    assert "SLACK_BOT_TOKEN" not in dotenv_path.read_text(encoding="utf-8")
    assert azd.values[TOKEN_MIGRATION_MARKER_ENV_NAME] == "true"
    assert azd.values[SECRET_LATEST_VERSION_ENV_NAME] == "v1"
    assert azd.values[SECRET_VERSION_ENV_NAME] == ""
    assert azd.provision_calls == 1
    assert azd.values[DEPLOY_WORKLOAD_ENV_NAME] == "false"
    assert azd.values[BASELINE_ALERT_ENV_NAME] == "true"
    assert azure.vault_ip_rules == []
    assert azure.role_assignments == {}


def test_migrate_marker_absent_and_token_not_removed_when_validation_fails(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SLACK_BOT_TOKEN=not-a-real-token\n", encoding="utf-8")
    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(azure, azd)

    with pytest.raises(ScopeManagerError, match="xoxb"):
        manager.migrate(dotenv_path)

    # The migration marker must NOT be set: the migration never actually
    # completed, so ordinary fail-closed provisioning must stay blocked.
    assert TOKEN_MIGRATION_MARKER_ENV_NAME not in azd.values
    assert azd.provision_calls == 0
    # The local legacy token line must NOT have been removed either.
    assert "SLACK_BOT_TOKEN=not-a-real-token" in dotenv_path.read_text(encoding="utf-8")


def test_migrate_refuses_concurrent_local_migration(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SLACK_BOT_TOKEN=" + VALID_TOKEN + "\n", encoding="utf-8")
    lock_path = tmp_path / ".env.migrate.lock"
    lock_path.touch()
    manager = make_manager()

    with pytest.raises(ScopeManagerError, match="already appears to be in progress"):
        manager.migrate(dotenv_path)


def test_migrate_second_run_after_success_fails_closed_with_no_token_left(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SLACK_BOT_TOKEN=" + VALID_TOKEN + "\n", encoding="utf-8")
    azure = FakeAzure()
    azd = FakeAzd()
    manager = make_manager(azure, azd)

    manager.migrate(dotenv_path)

    with pytest.raises(ScopeManagerError, match="No local"):
        manager.migrate(dotenv_path)


def test_temporary_vault_network_access_opens_and_restores_exact_state():
    azure = FakeAzure()
    journal, operation_id = operation_journal(azure)
    azure.vault_public_network_access = "Disabled"
    azure.vault_default_action = "Deny"
    azure.vault_bypass = "AzureServices"
    azure.vault_ip_rules = [{"value": "9.9.9.9/32"}]

    with _TemporaryVaultNetworkAccess(
        azure,
        azure.vault_name,
        SUBSCRIPTION_ID,
        lambda: "203.0.113.5",
        journal,
        operation_id,
    ):
        assert azure.vault_public_network_access == "Enabled"
        assert azure.vault_default_action == "Deny"
        assert azure.vault_ip_rules == [{"value": "203.0.113.5/32"}]

    assert azure.vault_public_network_access == "Disabled"
    assert azure.vault_default_action == "Deny"
    assert azure.vault_bypass == "AzureServices"
    assert azure.vault_ip_rules == [{"value": "9.9.9.9/32"}]


def test_temporary_role_assignment_fails_closed_on_preexisting_access():
    azure = FakeAzure()
    azure.role_assignments["/assignments/preexisting"] = True
    journal, operation_id = operation_journal(azure)

    with pytest.raises(ScopeManagerError, match="pre-existing or ambiguous"):
        with _TemporaryRoleAssignment(
            azure,
            azure.vault_id,
            SUBSCRIPTION_ID,
            journal,
            operation_id,
        ):
            pass

    assert set(azure.role_assignments) == {"/assignments/preexisting"}
    assert not any(
        call[:3] == ("role", "assignment", "delete")
        for call in azure.calls
    )


def test_interrupted_operation_owned_role_grant_is_recovered_on_retry():
    azure = FakeAzure()
    journal, operation_id = operation_journal(
        azure, "slack-token-interrupted"
    )
    real_invoke = azure.invoke

    def interrupted_invoke(*args):
        result = real_invoke(*args)
        if list(args)[:3] == ["role", "assignment", "create"]:
            raise ScopeManagerError("connection lost after grant")
        return result

    azure.invoke = interrupted_invoke
    with pytest.raises(ScopeManagerError, match="connection lost"):
        with _TemporaryRoleAssignment(
            azure,
            azure.vault_id,
            SUBSCRIPTION_ID,
            journal,
            operation_id,
        ):
            pass

    assert len(azure.role_assignments) == 1
    assert (
        journal.read(operation_id)["TemporaryRoleAssignment"]["State"]
        == "GRANT_PENDING"
    )

    azure.invoke = real_invoke
    with _TemporaryRoleAssignment(
        azure,
        azure.vault_id,
        SUBSCRIPTION_ID,
        journal,
        operation_id,
    ):
        assert len(azure.role_assignments) == 1

    assert azure.role_assignments == {}
    assert (
        journal.read(operation_id)["TemporaryRoleAssignment"]["State"]
        == "REVOKED"
    )


def test_temporary_role_cleanup_uses_principal_captured_before_journal_loss():
    azure = FakeAzure()
    journal, operation_id = operation_journal(
        azure, "slack-token-principal-cache"
    )

    with _TemporaryRoleAssignment(
        azure,
        azure.vault_id,
        SUBSCRIPTION_ID,
        journal,
        operation_id,
    ):
        journal.record(operation_id, {})

    list_calls = [
        call
        for call in azure.calls
        if call[:3] == ("role", "assignment", "list")
    ]
    assert list_calls[-1][
        list_calls[-1].index("--assignee-object-id") + 1
    ] == azure.caller_object_id


def test_temporary_vault_network_access_refuses_non_default_deny_start_state():
    azure = FakeAzure()
    azure.vault_default_action = "Allow"
    journal, operation_id = operation_journal(azure)

    with pytest.raises(ScopeManagerError, match="default-deny"):
        with _TemporaryVaultNetworkAccess(
            azure,
            azure.vault_name,
            SUBSCRIPTION_ID,
            lambda: "203.0.113.5",
            journal,
            operation_id,
        ):
            pass


def test_temporary_vault_network_access_raises_if_restore_verification_fails():
    azure = FakeAzure()
    journal, operation_id = operation_journal(azure)

    real_invoke = azure.invoke
    calls = {"update_count": 0}

    def flaky_invoke(*args):
        arguments = list(args)
        if arguments[:2] == ["keyvault", "update"]:
            calls["update_count"] += 1
            if calls["update_count"] == 2:
                # Silently ignore the restore update, so the readback will
                # not match the original snapshot.
                return None
        return real_invoke(*args)

    azure.invoke = flaky_invoke

    with pytest.raises(ScopeManagerError, match="restore verification"):
        with _TemporaryVaultNetworkAccess(
            azure,
            azure.vault_name,
            SUBSCRIPTION_ID,
            lambda: "203.0.113.5",
            journal,
            operation_id,
        ):
            pass


def test_partial_network_open_and_failed_restore_are_journaled():
    azure = FakeAzure()
    journal, operation_id = operation_journal(
        azure, "slack-token-network-interrupted"
    )
    real_invoke = azure.invoke
    update_count = 0

    def partial_open(*args):
        nonlocal update_count
        arguments = list(args)
        if arguments[:2] == ["keyvault", "update"]:
            update_count += 1
            if update_count == 2:
                raise ScopeManagerError("restore write failed")
        if arguments[:3] == ["keyvault", "network-rule", "add"]:
            raise ScopeManagerError("open rule failed")
        return real_invoke(*args)

    azure.invoke = partial_open

    with pytest.raises(ScopeManagerError, match="CLEANUP_INCOMPLETE"):
        with _TemporaryVaultNetworkAccess(
            azure,
            azure.vault_name,
            SUBSCRIPTION_ID,
            lambda: "203.0.113.5",
            journal,
            operation_id,
        ):
            pass

    state = journal.read(operation_id)
    assert state["State"] == "CLEANUP_INCOMPLETE"
    assert (
        state["TemporaryNetworkAccess"]["State"]
        == "CLEANUP_INCOMPLETE"
    )


def test_temporary_vault_network_access_cleanup_failure_prevents_provision():
    azure = FakeAzure()
    azd = FakeAzd()
    secret_client = FakeSecretClient()
    real_invoke = azure.invoke
    calls = {"update_count": 0}

    def flaky_invoke(*args):
        arguments = list(args)
        if arguments[:2] == ["keyvault", "update"]:
            calls["update_count"] += 1
            if calls["update_count"] == 2:
                return None
        return real_invoke(*args)

    azure.invoke = flaky_invoke
    manager = make_manager(azure, azd, secret_client=secret_client)

    with pytest.raises(ScopeManagerError, match="restore verification"):
        manager.bootstrap()

    # The cleanup failure must have prevented the second, post-write
    # provision call from ever happening.
    assert azd.provision_calls == 1
    (entry,) = azure.deployments.values()
    state = entry["properties"]["outputs"]["journalState"]["value"]
    assert state["State"] == "CLEANUP_INCOMPLETE"
    assert (
        state["TemporaryNetworkAccess"]["State"]
        == "CLEANUP_INCOMPLETE"
    )


def test_token_format_pattern_accepts_xoxb_and_rejects_xoxe():
    assert TOKEN_FORMAT_PATTERN.match(
        "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuvwxyz"
    )
    assert not TOKEN_FORMAT_PATTERN.match("xox" + "e-1-abcdefghijklmnop")
    assert not TOKEN_FORMAT_PATTERN.match(
        "xox" + "e.xoxp-1-abcdefghijklmnop"
    )


@pytest.mark.parametrize(
    "leaked_token",
    [
        "xoxb-should-not-leak-123456",
        "XOXB-UPPER_case-123456",
        "xoxe.xoxp-refresh_token-123456",
    ],
)
def test_journal_error_is_redacted_when_underlying_exception_contains_a_token(
    leaked_token,
):
    azure = FakeAzure()
    azd = FakeAzd()
    secret_client = FakeSecretClient()
    leaking_message = f"write failed for {leaked_token}"

    def failing_set_secret(name, value):
        raise RuntimeError(leaking_message)

    secret_client.set_secret = failing_set_secret
    manager = make_manager(azure, azd, secret_client=secret_client)

    with pytest.raises(ScopeManagerError):
        manager.bootstrap()

    (entry,) = azure.deployments.values()
    error_text = entry["properties"]["outputs"]["journalState"]["value"]["Error"]
    assert leaked_token not in error_text
    assert "[REDACTED-SLACK-TOKEN]" in error_text


def test_main_error_output_is_redacted(monkeypatch, capsys):
    from scripts import manage_slack_token as module

    def failing_bootstrap(self):
        raise ScopeManagerError("boom xoxb-leaky-token-value")

    monkeypatch.setattr(module.SlackTokenManager, "bootstrap", failing_bootstrap)
    monkeypatch.setattr(module, "AzureCli", lambda: FakeAzure())
    monkeypatch.setattr(module, "AzdCli", lambda: FakeAzd())

    exit_code = module.main(["bootstrap"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "xoxb-leaky-token-value" not in captured.err
    assert "[REDACTED-SLACK-TOKEN]" in captured.err


def test_default_public_ip_resolver_never_performs_network_io(monkeypatch):
    """The default resolver must be a pure, explicit operator prompt --
    never a call to a third-party "what is my IP" service such as ipify --
    per the plan's "Azure CLI only for nonsecret network metadata" and
    "explicit IPv4 /32" requirements."""
    from scripts import manage_slack_token as module

    assert not hasattr(module, "PUBLIC_IP_DISCOVERY_URL")

    monkeypatch.setattr("builtins.input", lambda prompt="": "203.0.113.9")
    assert module.default_public_ip_resolver() == "203.0.113.9"


def test_temporary_vault_network_access_uses_only_the_injected_resolver():
    """No implicit fallback to any network call: the resolver is always
    the explicitly injected callable, never a module-level network probe."""
    azure = FakeAzure()
    journal, operation_id = operation_journal(azure)
    calls = {"count": 0}

    def explicit_resolver() -> str:
        calls["count"] += 1
        return "198.51.100.7"

    with _TemporaryVaultNetworkAccess(
        azure,
        azure.vault_name,
        SUBSCRIPTION_ID,
        explicit_resolver,
        journal,
        operation_id,
    ):
        assert calls["count"] == 1
        assert any(rule["value"] == "198.51.100.7/32" for rule in azure.vault_ip_rules)
