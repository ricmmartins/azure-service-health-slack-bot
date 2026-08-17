import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.configure_secure_webhook as webhook
from scripts.configure_secure_webhook import (
    LEGACY_TOKEN_ENV_NAME,
    NONSECRET_AZD_DEFAULTS,
    READ_ONLY_PREVIEW_ENV_NAME,
    TOKEN_MIGRATION_MARKER_ENV_NAME,
    AzdCli,
    GraphClient,
    ScopeManagerError,
    SecureWebhookConfigurator,
    enforce_production_readiness,
    expected_anchor_action_group_id,
    read_only_preview_enabled,
    resolve_caller_owner_object_id,
    validate_read_only_preview,
)


TENANT_ID = "tenant-a"
APP_ID = "11111111-1111-1111-1111-111111111111"
APP_OBJECT_ID = "22222222-2222-2222-2222-222222222222"
ROLE_ID = "role-id"
AZNS_APP_ID = "461e8683-5575-4561-ac7f-899cc907d62a"
PREVIEW_TENANT_ID = "33333333-3333-3333-3333-333333333333"
PREVIEW_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"


class FakeAzure:
    def __init__(self, account=None):
        self.account = account or {
            "tenantId": TENANT_ID,
            "user": {"type": "user", "name": "alice@example.com"},
        }
        self.calls = []

    def invoke(self, *args):
        self.calls.append(args)
        if args[:2] == ("account", "show"):
            return self.account
        return None


class FakeGraph:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, uri, body=None, headers=None):
        self.calls.append((method, uri, body))
        return self.handler(method, uri, body)


class FakeAzd:
    def __init__(self, environments=None):
        self.values = {}
        self._environments = (
            [
                {
                    "Name": "production",
                    "DotEnvPath": str(
                        Path(__file__).resolve().parent.parent / ".env-example"
                    ),
                }
            ]
            if environments is None
            else environments
        )

    def set_environment_value(self, name, value):
        self.values[name] = value

    def get_environment_value(self, name):
        return self.values.get(name, "")

    def list_environments(self):
        return self._environments


def _preview_project(
    tmp_path,
    *,
    environment_name="production",
    overrides=None,
    omitted=(),
    extra_lines=(),
):
    values = {
        "AZURE_ENV_NAME": environment_name,
        "AZURE_LOCATION": "eastus",
        "AZURE_SUBSCRIPTION_ID": PREVIEW_SUBSCRIPTION_ID,
        "AZURE_TENANT_ID": PREVIEW_TENANT_ID,
        "SERVICE_HEALTH_API_CLIENT_ID": APP_ID,
        "SERVICE_HEALTH_API_IDENTIFIER_URI": f"api://{APP_ID}",
        "SERVICE_HEALTH_API_OBJECT_ID": APP_OBJECT_ID,
        "SERVICE_HEALTH_BASELINE_ALERT_ENABLED": "false",
        "SERVICE_HEALTH_DEPLOY_WORKLOAD": "false",
        "SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID": "",
        "SERVICE_HEALTH_ROUTES_JSON_B64": "e30=",
        "SERVICE_HEALTH_SECRET_VERSION": "",
    }
    values.update(overrides or {})
    environment_directory = tmp_path / ".azure" / environment_name
    environment_directory.mkdir(parents=True)
    dotenv_path = environment_directory / ".env"
    lines = [
        f"{name}={value}"
        for name, value in values.items()
        if name not in omitted
    ]
    lines.extend(extra_lines)
    dotenv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dotenv_path


def _preview_azure(
    calls,
    *,
    tenant_id=PREVIEW_TENANT_ID,
    subscription_id=PREVIEW_SUBSCRIPTION_ID,
):
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {"tenantId": tenant_id, "id": subscription_id}
                ),
                "stderr": "",
            },
        )()

    return webhook.AzureCli(runner=runner, max_read_attempts=1)


def test_read_only_preview_opt_in_is_strict():
    assert read_only_preview_enabled({}) is False
    assert (
        read_only_preview_enabled(
            {READ_ONLY_PREVIEW_ENV_NAME: "true"}
        )
        is True
    )
    assert (
        read_only_preview_enabled(
            {READ_ONLY_PREVIEW_ENV_NAME: " TRUE "}
        )
        is True
    )
    for invalid in ("", "false", "1", "yes"):
        with pytest.raises(ScopeManagerError, match="omitted or set exactly"):
            read_only_preview_enabled(
                {READ_ONLY_PREVIEW_ENV_NAME: invalid}
            )


def test_read_only_preview_uses_only_sanitized_account_show(
    tmp_path,
    monkeypatch,
):
    _preview_project(tmp_path)
    calls = []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-preview-process-canary")

    validate_read_only_preview(
        "production",
        project_root=tmp_path,
        azure=_preview_azure(calls),
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1:3] == ["account", "show"]
    assert "SLACK_BOT_TOKEN" not in kwargs["env"]
    assert all(
        "xoxb-preview-process-canary" not in value
        for value in kwargs["env"].values()
    )
    assert not any(
        argument in {"set", "create", "update", "delete", "post", "patch"}
        for argument in command
    )


def test_read_only_preview_requires_explicit_environment_before_runner(
    tmp_path,
):
    calls = []
    with pytest.raises(ScopeManagerError, match="explicit AZD environment"):
        validate_read_only_preview(
            "",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )
    assert calls == []


def test_read_only_preview_rejects_missing_and_ambiguous_dotenv(
    tmp_path,
    monkeypatch,
):
    calls = []
    (tmp_path / ".azure").mkdir()
    with pytest.raises(ScopeManagerError, match="exactly one"):
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )

    environment_directory = tmp_path / ".azure" / "production"
    environment_directory.mkdir()
    with pytest.raises(ScopeManagerError, match="regular local"):
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )

    monkeypatch.setattr(
        webhook.Path,
        "iterdir",
        lambda _path: iter(
            [environment_directory, environment_directory]
        ),
    )
    with pytest.raises(ScopeManagerError, match="exactly one"):
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )
    assert calls == []


def test_read_only_preview_rejects_token_entry_without_reading_runner(
    tmp_path,
):
    secret = "xoxb-preview-dotenv-canary"
    _preview_project(
        tmp_path,
        extra_lines=(f"  export {LEGACY_TOKEN_ENV_NAME}={secret}",),
    )
    calls = []

    with pytest.raises(ScopeManagerError, match="credential entry") as exc_info:
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )

    assert secret not in str(exc_info.value)
    assert calls == []


@pytest.mark.parametrize(
    "missing_name",
    [
        "AZURE_LOCATION",
        "AZURE_TENANT_ID",
        "SERVICE_HEALTH_API_CLIENT_ID",
        "SERVICE_HEALTH_API_OBJECT_ID",
        "SERVICE_HEALTH_API_IDENTIFIER_URI",
        "SERVICE_HEALTH_ROUTES_JSON_B64",
    ],
)
def test_read_only_preview_rejects_missing_required_nonsecret_value(
    tmp_path,
    missing_name,
):
    _preview_project(tmp_path, omitted=(missing_name,))
    calls = []
    with pytest.raises(ScopeManagerError, match=missing_name):
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )
    assert calls == []


@pytest.mark.parametrize(
    "name,value,error",
    [
        ("AZURE_TENANT_ID", "not-a-uuid", "AZURE_TENANT_ID"),
        ("AZURE_SUBSCRIPTION_ID", "not-a-uuid", "AZURE_SUBSCRIPTION_ID"),
        (
            "SERVICE_HEALTH_API_CLIENT_ID",
            "not-a-uuid",
            "SERVICE_HEALTH_API_CLIENT_ID",
        ),
        (
            "SERVICE_HEALTH_API_OBJECT_ID",
            "not-a-uuid",
            "SERVICE_HEALTH_API_OBJECT_ID",
        ),
        (
            "SERVICE_HEALTH_API_IDENTIFIER_URI",
            f"api://{APP_OBJECT_ID}",
            "identifier URI",
        ),
        (
            "SERVICE_HEALTH_DEPLOY_WORKLOAD",
            "maybe",
            "true or false",
        ),
    ],
)
def test_read_only_preview_rejects_malformed_target_identifiers(
    tmp_path,
    name,
    value,
    error,
):
    _preview_project(tmp_path, overrides={name: value})
    calls = []
    with pytest.raises(ScopeManagerError, match=error):
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls),
        )
    assert calls == []


@pytest.mark.parametrize(
    "account_overrides,error",
    [
        ({"tenant_id": APP_ID}, "tenant"),
        ({"subscription_id": APP_OBJECT_ID}, "subscription"),
    ],
)
def test_read_only_preview_rejects_active_account_target_mismatch(
    tmp_path,
    account_overrides,
    error,
):
    _preview_project(tmp_path)
    calls = []
    with pytest.raises(ScopeManagerError, match=error):
        validate_read_only_preview(
            "production",
            project_root=tmp_path,
            azure=_preview_azure(calls, **account_overrides),
        )
    assert len(calls) == 1
    assert calls[0][0][1:3] == ["account", "show"]


def test_read_only_preview_does_not_require_disabled_production_evidence(
    tmp_path,
):
    _preview_project(
        tmp_path,
        overrides={"SERVICE_HEALTH_ENVIRONMENT_CLASS": "production"},
    )
    calls = []
    validate_read_only_preview(
        "production",
        project_root=tmp_path,
        azure=_preview_azure(calls),
    )
    assert len(calls) == 1


def test_normal_hook_path_remains_default_without_preview_opt_in():
    assert read_only_preview_enabled({"AZURE_ENV_NAME": "production"}) is False


def test_main_preview_exits_before_normal_mutation_boundaries(
    monkeypatch,
    capsys,
):
    sentinel_azure = object()
    validation_calls = []

    class ForbiddenBoundary:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("normal mutation boundary must not be created")

    monkeypatch.setenv("AZURE_ENV_NAME", "production")
    monkeypatch.setenv(READ_ONLY_PREVIEW_ENV_NAME, "true")
    monkeypatch.setattr(webhook, "AzureCli", lambda: sentinel_azure)
    monkeypatch.setattr(webhook, "AzdCli", ForbiddenBoundary)
    monkeypatch.setattr(webhook, "GraphClient", ForbiddenBoundary)
    monkeypatch.setattr(
        webhook,
        "SecureWebhookConfigurator",
        ForbiddenBoundary,
    )
    monkeypatch.setattr(
        webhook,
        "validate_read_only_preview",
        lambda environment_name, **kwargs: validation_calls.append(
            (environment_name, kwargs)
        ),
    )

    assert webhook.main([]) == 0
    assert validation_calls == [
        (
            "production",
            {
                "project_root": Path(webhook.__file__).resolve().parent.parent,
                "azure": sentinel_azure,
            },
        )
    ]
    assert "no state was changed" in capsys.readouterr().out


def test_main_rejects_invalid_preview_opt_in_before_any_cli(
    monkeypatch,
    capsys,
):
    class ForbiddenCli:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("CLI must not be created")

    monkeypatch.setenv("AZURE_ENV_NAME", "production")
    monkeypatch.setenv(READ_ONLY_PREVIEW_ENV_NAME, "false")
    monkeypatch.setattr(webhook, "AzureCli", ForbiddenCli)
    monkeypatch.setattr(webhook, "AzdCli", ForbiddenCli)

    assert webhook.main([]) == 1
    assert "omitted or set exactly to true" in capsys.readouterr().err


def test_main_without_preview_opt_in_runs_existing_configure_path(
    monkeypatch,
):
    configure_calls = []

    class NormalAzd(FakeAzd):
        def __init__(self, environment_name):
            super().__init__()
            assert environment_name == "production"

    class NormalConfigurator:
        def __init__(self, azure, graph, azd):
            assert azure == "azure"
            assert graph == "graph"
            assert isinstance(azd, NormalAzd)

        def configure(self, display_name, **kwargs):
            configure_calls.append((display_name, kwargs))
            return {}

    monkeypatch.setenv("AZURE_ENV_NAME", "production")
    monkeypatch.delenv(READ_ONLY_PREVIEW_ENV_NAME, raising=False)
    monkeypatch.setattr(webhook, "AzureCli", lambda: "azure")
    monkeypatch.setattr(webhook, "AzdCli", NormalAzd)
    monkeypatch.setattr(webhook, "GraphClient", lambda _azure: "graph")
    monkeypatch.setattr(
        webhook,
        "SecureWebhookConfigurator",
        NormalConfigurator,
    )

    assert webhook.main([]) == 0
    assert len(configure_calls) == 1
    assert configure_calls[0][0] == (
        "Azure Service Health Slack Bot - production"
    )
    assert configure_calls[0][1]["environment_name"] == "production"


def test_direct_provision_readiness_rejects_anchor_action_group():
    azd = FakeAzd()
    azd.values.update(
        {
            "AZURE_SUBSCRIPTION_ID": "sub-1",
            "AZURE_RESOURCE_GROUP": "rg-production",
            "SERVICE_HEALTH_ENVIRONMENT_CLASS": "production",
            "SERVICE_HEALTH_OPERATIONS_PRIMARY_OWNER": "primary",
            "SERVICE_HEALTH_OPERATIONS_BACKUP_OWNER": "backup",
            "SERVICE_HEALTH_OPERATIONS_ON_CALL_DESTINATION": "on-call",
            "SERVICE_HEALTH_OPERATIONS_RUNBOOK_URI": (
                "https://example.com/runbook"
            ),
        }
    )
    anchor = expected_anchor_action_group_id(azd, "production")
    azd.values["SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID"] = anchor
    azd.values[
        "SERVICE_HEALTH_OPERATIONS_RECEIVER_TEST_EVIDENCE"
    ] = json.dumps(
        {
            "status": "Succeeded",
            "testedAt": datetime.now(timezone.utc).isoformat(),
            "actionGroupId": anchor,
        }
    )

    with pytest.raises(ScopeManagerError, match="independent"):
        enforce_production_readiness(
            azd,
            "production",
            anchor_action_group_id=anchor,
        )


def application(
    role=True,
    display_name="Azure Service Health Slack Bot - production",
):
    return {
        "id": APP_OBJECT_ID,
        "appId": APP_ID,
        "displayName": display_name,
        "api": {"requestedAccessTokenVersion": 2},
        "identifierUris": [f"api://{APP_ID}"],
        "appRoles": (
            [
                {
                    "id": ROLE_ID,
                    "value": "ActionGroupsSecureWebhook",
                    "isEnabled": True,
                    "allowedMemberTypes": ["Application"],
                }
            ]
            if role
            else []
        ),
    }


@pytest.mark.parametrize("user_type", ["user", "USER"])
def test_delegated_caller_owner_uses_graph_me(user_type):
    graph = FakeGraph(
        lambda _method, uri, _body: (
            {"id": "delegated-user-id"} if "/me?" in uri else None
        )
    )
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": user_type, "name": "alice@example.com"},
    }

    assert (
        resolve_caller_owner_object_id(account, graph)
        == "delegated-user-id"
    )
    assert len(graph.calls) == 1
    assert "/me?" in graph.calls[0][1]


@pytest.mark.parametrize(
    "response,error",
    [
        ({"id": None}, "returned no object id"),
        (None, "returned no object id"),
    ],
)
def test_delegated_caller_owner_requires_id(response, error):
    graph = FakeGraph(lambda *_args: response)
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": "user", "name": "alice@example.com"},
    }
    with pytest.raises(ScopeManagerError, match=error):
        resolve_caller_owner_object_id(account, graph)


@pytest.mark.parametrize("user_type", ["servicePrincipal", "SERVICEPRINCIPAL"])
def test_app_only_caller_owner_resolves_service_principal(user_type):
    graph = FakeGraph(
        lambda _method, uri, _body: (
            {"value": [{"id": "caller-sp-object-id"}]}
            if "servicePrincipals?" in uri
            else None
        )
    )
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": user_type, "name": APP_ID},
    }

    assert (
        resolve_caller_owner_object_id(account, graph)
        == "caller-sp-object-id"
    )
    assert "/me?" not in graph.calls[0][1]
    assert f"appId eq '{APP_ID}'" in graph.calls[0][1]


@pytest.mark.parametrize(
    "name,response,error",
    [
        ("", {"value": []}, "client id"),
        (APP_ID, {"value": []}, "no service principal"),
        (
            APP_ID,
            {"value": [{"id": "one"}, {"id": "two"}]},
            "multiple service principal",
        ),
        (APP_ID, {"value": [{"id": ""}]}, "has no object id"),
    ],
)
def test_app_only_caller_owner_fails_closed(name, response, error):
    graph = FakeGraph(lambda *_args: response)
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": "servicePrincipal", "name": name},
    }
    with pytest.raises(ScopeManagerError, match=error):
        resolve_caller_owner_object_id(account, graph)


def test_unknown_caller_type_is_rejected():
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": "managedIdentity", "name": "identity"},
    }
    with pytest.raises(ScopeManagerError, match="unsupported"):
        resolve_caller_owner_object_id(account, FakeGraph(lambda *_args: None))


def test_graph_client_uses_temp_file_and_removes_it():
    class RecordingAzure:
        def __init__(self):
            self.body_path = None
            self.body = None
            self.arguments = ()

        def invoke(self, *args):
            self.arguments = args
            body_argument = args[args.index("--body") + 1]
            self.body_path = Path(body_argument[1:])
            assert self.body_path.exists()
            self.body = json.loads(self.body_path.read_text(encoding="utf-8"))
            return {"id": "created"}

    azure = RecordingAzure()
    response = GraphClient(azure).request(
        "POST",
        "https://graph.microsoft.com/v1.0/applications",
        {"displayName": 'Name with "quotes"'},
        headers={"Prefer": "create-if-missing"},
    )

    assert response == {"id": "created"}
    assert azure.body == {"displayName": 'Name with "quotes"'}
    assert "Prefer=create-if-missing" in azure.arguments
    assert not azure.body_path.exists()


def test_graph_collections_follow_pagination_for_existing_owners():
    next_link = "https://graph.microsoft.com/v1.0/owners-next-page"
    display_name = "Azure Service Health Slack Bot - production"

    def handler(method, uri, _body):
        assert method == "GET"
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [{"id": "user-id"}],
                "@odata.nextLink": next_link,
            }
        if uri == next_link:
            return {"value": [{"id": "azns-sp-id"}]}
        if "/me?" in uri:
            return {"id": "user-id"}
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {
                        "resourceId": "api-sp-id",
                        "appRoleId": ROLE_ID,
                    }
                ]
            }
        raise AssertionError(uri)

    graph = FakeGraph(handler)
    SecureWebhookConfigurator(FakeAzure(), graph, FakeAzd()).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
    )

    assert not [
        call for call in graph.calls if call[0] in ("POST", "PATCH")
    ]
    assert any(uri == next_link for _method, uri, _body in graph.calls)


def test_invalid_graph_collection_response_fails_closed():
    graph = FakeGraph(lambda *_args: None)
    with pytest.raises(ScopeManagerError, match="invalid collection response"):
        SecureWebhookConfigurator(
            FakeAzure(), graph, FakeAzd()
        ).configure("invalid-response")


def test_existing_configuration_is_idempotent():
    display_name = "Azure Service Health Slack Bot - production"

    def handler(method, uri, _body):
        assert method == "GET"
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        if "/me?" in uri:
            return {"id": "user-id"}
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {
                        "resourceId": "api-sp-id",
                        "appRoleId": ROLE_ID,
                    }
                ]
            }
        raise AssertionError(uri)

    azure = FakeAzure()
    graph = FakeGraph(handler)
    azd = FakeAzd()

    result = SecureWebhookConfigurator(azure, graph, azd).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_tenant_id=TENANT_ID,
    )

    assert not [
        call for call in graph.calls if call[0] in ("POST", "PATCH")
    ]
    assert result == azd.values
    assert result["AZURE_TENANT_ID"] == TENANT_ID
    assert result["SERVICE_HEALTH_API_CLIENT_ID"] == APP_ID
    assert result["SERVICE_HEALTH_API_OBJECT_ID"] == APP_OBJECT_ID
    assert result["SERVICE_HEALTH_API_IDENTIFIER_URI"] == f"api://{APP_ID}"


def test_v2_token_patch_preserves_existing_api_authorization_settings():
    display_name = "existing-api-settings"
    existing = application(display_name=display_name)
    existing["api"] = {
        "requestedAccessTokenVersion": 1,
        "oauth2PermissionScopes": [{"id": "scope-id", "value": "existing"}],
        "preAuthorizedApplications": [
            {"appId": AZNS_APP_ID, "delegatedPermissionIds": ["scope-id"]}
        ],
    }
    patches = []

    def handler(method, uri, body):
        if method == "GET" and f"/applications/{APP_OBJECT_ID}?" in uri:
            return existing
        if method == "PATCH" and f"/applications/{APP_OBJECT_ID}" in uri:
            patches.append(body)
            return None
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        if "/me?" in uri:
            return {"id": "user-id"}
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {
                        "resourceId": "api-sp-id",
                        "appRoleId": ROLE_ID,
                    }
                ]
            }
        raise AssertionError((method, uri, body))

    SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(handler), FakeAzd()
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
    )

    assert patches == [
        {
            "api": {
                "requestedAccessTokenVersion": 2,
                "oauth2PermissionScopes": [
                    {"id": "scope-id", "value": "existing"}
                ],
                "preAuthorizedApplications": [
                    {
                        "appId": AZNS_APP_ID,
                        "delegatedPermissionIds": ["scope-id"],
                    }
                ],
            }
        }
    ]


def test_missing_configuration_is_created_in_safe_order(monkeypatch):
    generated_role_id = "generated-role-id"
    monkeypatch.setattr(
        "scripts.configure_secure_webhook.uuid.uuid4",
        lambda: generated_role_id,
    )

    def handler(method, uri, body):
        if method == "GET" and "(uniqueName=" in uri:
            raise ScopeManagerError("not found", status_code=404)
        if method == "GET" and "displayName eq" in uri:
            return {"value": []}
        if method == "PATCH" and "(uniqueName=" in uri:
            return application(role=False, display_name=body["displayName"])
        if method == "PATCH" and f"/applications/{APP_OBJECT_ID}" in uri:
            assert body["identifierUris"] == [f"api://{APP_ID}"]
            assert body["appRoles"][0]["id"] == generated_role_id
            return None
        if method == "GET" and f"appId eq '{APP_ID}'" in uri:
            return {"value": []}
        if method == "POST" and uri.endswith("/servicePrincipals"):
            object_id = (
                "api-sp-id"
                if body["appId"] == APP_ID
                else "azns-sp-id"
            )
            return {"id": object_id, "appId": body["appId"]}
        if method == "GET" and f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": []}
        if method == "GET" and "/owners?" in uri:
            return {"value": []}
        if method == "GET" and "/me?" in uri:
            return {"id": "user-id"}
        if method == "POST" and "/owners/$ref" in uri:
            return None
        if method == "GET" and "/appRoleAssignments?" in uri:
            return {"value": []}
        if method == "POST" and uri.endswith("/appRoleAssignments"):
            assert body == {
                "principalId": "azns-sp-id",
                "resourceId": "api-sp-id",
                "appRoleId": generated_role_id,
            }
            return None
        raise AssertionError((method, uri, body))

    graph = FakeGraph(handler)
    azd = FakeAzd()
    result = SecureWebhookConfigurator(FakeAzure(), graph, azd).configure(
        "Azure Service Health Slack Bot - production"
    )

    mutation_paths = [
        (method, uri) for method, uri, _body in graph.calls if method != "GET"
    ]
    assert "(uniqueName=" in mutation_paths[0][1]
    assert any("/owners/$ref" in uri for _method, uri in mutation_paths)
    assert mutation_paths[-1][1].endswith("/appRoleAssignments")
    assert result == azd.values


def test_application_creation_race_adopts_unique_name_winner():
    display_name = "concurrent-environment"
    unique_name = "azure-service-health-slack-bot-concurrent"
    winner = {
        **application(display_name=display_name),
        "id": "33333333-3333-3333-3333-333333333333",
        "appId": "44444444-4444-4444-4444-444444444444",
        "uniqueName": unique_name,
    }
    unique_reads = 0

    def handler(method, uri, body):
        nonlocal unique_reads
        if method == "GET" and "(uniqueName=" in uri:
            unique_reads += 1
            if unique_reads == 1:
                raise ScopeManagerError("not found", status_code=404)
            return winner
        if method == "GET" and "displayName eq" in uri:
            return {"value": []}
        if method == "PATCH" and "(uniqueName=" in uri:
            return None
        raise AssertionError((method, uri, body))

    result, created = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(handler), FakeAzd()
    )._application(display_name, "", "", unique_name)

    assert result == winner
    assert created is False


def test_new_application_rolls_back_when_object_id_cannot_be_persisted():
    deleted = []

    def handler(method, uri, body):
        if method == "GET" and "(uniqueName=" in uri:
            raise ScopeManagerError("not found", status_code=404)
        if method == "GET" and "displayName eq" in uri:
            return {"value": []}
        if method == "PATCH" and "(uniqueName=" in uri:
            return application(role=False, display_name=body["displayName"])
        if method == "GET" and "/me?" in uri:
            return {"id": "user-id"}
        if method == "GET" and f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if method == "GET" and "/owners?" in uri:
            return {"value": [{"id": "user-id"}]}
        if method == "DELETE" and f"/applications/{APP_OBJECT_ID}" in uri:
            deleted.append(uri)
            return None
        raise AssertionError((method, uri, body))

    class FailingAzd(FakeAzd):
        def set_environment_value(self, name, value):
            del value
            assert name == "SERVICE_HEALTH_API_OBJECT_ID"
            raise ScopeManagerError("injected persistence failure")

    with pytest.raises(ScopeManagerError, match="persistence failure"):
        SecureWebhookConfigurator(
            FakeAzure(),
            FakeGraph(handler),
            FailingAzd(),
        ).configure("new-application")

    assert deleted == [
        f"https://graph.microsoft.com/v1.0/applications/{APP_OBJECT_ID}"
    ]


def test_new_application_persists_object_id_before_followup_graph_reads():
    def handler(method, uri, body):
        if method == "GET" and "(uniqueName=" in uri:
            raise ScopeManagerError("not found", status_code=404)
        if method == "GET" and "displayName eq" in uri:
            return {"value": []}
        if method == "PATCH" and "(uniqueName=" in uri:
            return application(role=False, display_name=body["displayName"])
        if method == "GET" and "/me?" in uri:
            raise ScopeManagerError("injected Graph read failure")
        raise AssertionError((method, uri, body))

    graph = FakeGraph(handler)
    azd = FakeAzd()
    with pytest.raises(ScopeManagerError, match="Graph read failure"):
        SecureWebhookConfigurator(
            FakeAzure(),
            graph,
            azd,
        ).configure("new-application")

    assert azd.values == {
        "SERVICE_HEALTH_API_OBJECT_ID": APP_OBJECT_ID,
    }
    assert not [
        call for call in graph.calls if call[0] == "DELETE"
    ]


def test_display_name_collision_and_duplicate_role_are_rejected():
    duplicate_apps = FakeGraph(
        lambda _method, uri, _body: (
            (_ for _ in ()).throw(
                ScopeManagerError("not found", status_code=404)
            )
            if "(uniqueName=" in uri
            else (
                {"value": [application(), application()]}
                if "/applications?" in uri
                else None
            )
        ),
    )
    with pytest.raises(ScopeManagerError, match="Refusing to adopt"):
        SecureWebhookConfigurator(
            FakeAzure(), duplicate_apps, FakeAzd()
        ).configure("duplicate")

    display_name = "duplicate-role"
    duplicate_role_app = application(display_name=display_name)
    duplicate_role_app["appRoles"].append(
        {"id": "role-2", "value": "ActionGroupsSecureWebhook"}
    )

    def duplicate_role_handler(method, uri, _body):
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return duplicate_role_app
        if "/me?" in uri:
            return {"id": "user-id"}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        raise AssertionError((method, uri))

    duplicate_roles = FakeGraph(duplicate_role_handler)
    with pytest.raises(ScopeManagerError, match="multiple app roles"):
        SecureWebhookConfigurator(
            FakeAzure(), duplicate_roles, FakeAzd()
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )


@pytest.mark.parametrize(
    "role_update,error",
    [
        ({"isEnabled": False}, "not enabled"),
        ({"allowedMemberTypes": ["User"]}, "does not allow application"),
    ],
)
def test_existing_role_must_remain_application_enabled(role_update, error):
    display_name = "invalid-role"
    existing = application(display_name=display_name)
    existing["appRoles"][0].update(role_update)

    def handler(method, uri, _body):
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return existing
        if "/me?" in uri:
            return {"id": "user-id"}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        raise AssertionError((method, uri))

    graph = FakeGraph(handler)
    with pytest.raises(ScopeManagerError, match=error):
        SecureWebhookConfigurator(
            FakeAzure(), graph, FakeAzd()
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )


def test_existing_application_with_unexpected_owner_is_rejected_before_mutation():
    display_name = "untrusted-existing-app"

    def handler(method, uri, _body):
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if "/me?" in uri:
            return {"id": "user-id"}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "attacker-owner-id"},
                ]
            }
        raise AssertionError((method, uri))

    graph = FakeGraph(handler)
    azd = FakeAzd()
    with pytest.raises(ScopeManagerError, match="unexpected owners"):
        SecureWebhookConfigurator(
            FakeAzure(), graph, azd
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )

    assert not [
        call for call in graph.calls if call[0] != "GET"
    ]
    assert azd.values == {}


def test_legacy_multi_owner_baseline_requires_explicit_immutable_id_adoption():
    display_name = "legacy-multi-owner"

    def handler(method, uri, _body):
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if "/me?" in uri:
            return {"id": "user-one-id"}
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-one-id"},
                    {"id": "user-two-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {
                        "resourceId": "api-sp-id",
                        "appRoleId": ROLE_ID,
                    }
                ]
            }
        raise AssertionError((method, uri))

    rejected_graph = FakeGraph(handler)
    rejected_azd = FakeAzd()
    with pytest.raises(ScopeManagerError, match="unexpected owners"):
        SecureWebhookConfigurator(
            FakeAzure(), rejected_graph, rejected_azd
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )
    assert rejected_azd.values == {}
    assert not [
        call for call in rejected_graph.calls if call[0] != "GET"
    ]

    adopted_azd = FakeAzd()
    result = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(handler), adopted_azd
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        adopt_existing_owner_baseline=True,
    )

    assert set(result["SERVICE_HEALTH_API_OWNER_IDS"].split(",")) == {
        "user-one-id",
        "user-two-id",
        "azns-sp-id",
    }
    assert adopted_azd.values == result

    rerun_graph = FakeGraph(handler)
    rerun = SecureWebhookConfigurator(
        FakeAzure(), rerun_graph, FakeAzd()
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_owner_ids=result["SERVICE_HEALTH_API_OWNER_IDS"],
    )
    assert rerun["SERVICE_HEALTH_API_OWNER_IDS"] == (
        result["SERVICE_HEALTH_API_OWNER_IDS"]
    )
    assert not [
        call for call in rerun_graph.calls if call[0] != "GET"
    ]


def test_owner_baseline_adoption_requires_immutable_application_identity():
    with pytest.raises(ScopeManagerError, match="persisted"):
        SecureWebhookConfigurator(
            FakeAzure(),
            FakeGraph(lambda *_args: None),
            FakeAzd(),
        ).configure(
            "unsafe-display-name-only-adoption",
            adopt_existing_owner_baseline=True,
        )


def test_persisted_tenant_and_application_identity_must_match():
    configurator = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(lambda *_args: None), FakeAzd()
    )
    with pytest.raises(ScopeManagerError, match="tenant"):
        configurator.configure(
            "environment",
            expected_tenant_id="other-tenant",
        )

    with pytest.raises(ScopeManagerError, match="UUID"):
        configurator.configure(
            "environment",
            application_object_id="not-a-uuid",
        )


@pytest.mark.parametrize("persisted_id", ["object", "client"])
def test_partial_identity_persistence_recovers_by_immutable_id(persisted_id):
    display_name = "partial-persistence"

    def handler(method, uri, _body):
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if "/applications?" in uri and f"appId eq '{APP_ID}'" in uri:
            return {"value": [application(display_name=display_name)]}
        if "/me?" in uri:
            return {"id": "user-id"}
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {
                        "resourceId": "api-sp-id",
                        "appRoleId": ROLE_ID,
                    }
                ]
            }
        raise AssertionError((method, uri))

    arguments = (
        {"application_object_id": APP_OBJECT_ID}
        if persisted_id == "object"
        else {"application_client_id": APP_ID}
    )
    graph = FakeGraph(handler)
    result = SecureWebhookConfigurator(
        FakeAzure(), graph, FakeAzd()
    ).configure(display_name, **arguments)

    assert result["SERVICE_HEALTH_API_OBJECT_ID"] == APP_OBJECT_ID
    assert result["SERVICE_HEALTH_API_CLIENT_ID"] == APP_ID
    assert not [
        call for call in graph.calls if call[0] in {"POST", "PATCH"}
    ]


def test_legacy_rerun_requires_review_before_adding_new_caller():
    display_name = "legacy-shared-environment"
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": "user", "name": "new@example.com"},
    }
    owners = {"original-user-id", "azns-sp-id"}
    owner_posts = []

    def handler(method, uri, body):
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if "/me?" in uri:
            return {"id": "new-user-id"}
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {"value": [{"id": value} for value in sorted(owners)]}
        if method == "POST" and "/owners/$ref" in uri:
            owner_id = body["@odata.id"].rsplit("/", 1)[-1]
            owner_posts.append(body["@odata.id"])
            owners.add(owner_id)
            return None
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {
                        "resourceId": "api-sp-id",
                        "appRoleId": ROLE_ID,
                    }
                ]
            }
        raise AssertionError((method, uri, body))

    rejected_graph = FakeGraph(handler)
    rejected_azd = FakeAzd()
    with pytest.raises(ScopeManagerError, match="unexpected owners"):
        SecureWebhookConfigurator(
            FakeAzure(account), rejected_graph, rejected_azd
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )
    assert rejected_azd.values == {}
    assert owner_posts == []
    assert not [
        call for call in rejected_graph.calls if call[0] != "GET"
    ]

    adopted_azd = FakeAzd()
    result = SecureWebhookConfigurator(
        FakeAzure(account), FakeGraph(handler), adopted_azd
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        adopt_existing_owner_baseline=True,
    )
    assert owner_posts == [
        "https://graph.microsoft.com/v1.0/directoryObjects/new-user-id"
    ]
    assert set(result["SERVICE_HEALTH_API_OWNER_IDS"].split(",")) == {
        "original-user-id",
        "new-user-id",
        "azns-sp-id",
    }
    assert adopted_azd.values == result

    owner_posts.clear()
    rerun_graph = FakeGraph(handler)
    rerun = SecureWebhookConfigurator(
        FakeAzure(account), rerun_graph, FakeAzd()
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_owner_ids=result["SERVICE_HEALTH_API_OWNER_IDS"],
    )
    assert rerun["SERVICE_HEALTH_API_OWNER_IDS"] == (
        result["SERVICE_HEALTH_API_OWNER_IDS"]
    )
    assert owner_posts == []
    assert not [
        call for call in rerun_graph.calls if call[0] != "GET"
    ]


def test_azd_cli_surfaces_failure_without_exposing_value():
    def runner(command, **_kwargs):
        return type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "failed sensitive-value",
            },
        )()

    with pytest.raises(ScopeManagerError, match=r"SETTING <value>") as exc_info:
        AzdCli(runner=runner).set_environment_value(
            "SETTING", "sensitive-value"
        )
    assert "sensitive-value" not in str(exc_info.value)


def test_azd_cli_refuses_slack_token_process_boundaries():
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        raise AssertionError("runner must not receive Slack token operations")

    azd = AzdCli(runner=runner)

    with pytest.raises(ScopeManagerError, match="cannot be read"):
        azd.get_environment_value("SLACK_BOT_TOKEN")
    with pytest.raises(ScopeManagerError, match="cannot be written"):
        azd.set_environment_value(
            "SLACK_ACCESS_TOKEN", "xoxb-process-boundary-canary"
        )
    assert calls == []


def test_azd_cli_binds_all_mutations_to_explicit_environment():
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": "value", "stderr": ""},
        )()

    azd = AzdCli(runner=runner, environment_name="production")
    azd.set_environment_value("SETTING", "value")
    assert azd.get_environment_value("SETTING") == "value"
    azd.provision()

    assert all(
        "--environment" in command
        and command[command.index("--environment") + 1] == "production"
        for command in commands
    )


def test_azd_cli_reads_value_and_treats_only_missing_key_as_absent():
    def value_runner(_command, **_kwargs):
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": f"{APP_OBJECT_ID}\n",
                "stderr": "",
            },
        )()

    assert (
        AzdCli(runner=value_runner).get_environment_value(
            "SERVICE_HEALTH_API_OBJECT_ID"
        )
        == APP_OBJECT_ID
    )

    def missing_runner(_command, **_kwargs):
        return type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "ERROR: key not found in environment values: "
                    "'SERVICE_HEALTH_API_OBJECT_ID'"
                ),
            },
        )()

    assert (
        AzdCli(runner=missing_runner).get_environment_value(
            "SERVICE_HEALTH_API_OBJECT_ID"
        )
        == ""
    )

    def failed_runner(_command, **_kwargs):
        return type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "environment not found",
            },
        )()

    with pytest.raises(ScopeManagerError, match="environment not found"):
        AzdCli(runner=failed_runner).get_environment_value(
            "SERVICE_HEALTH_API_OBJECT_ID"
        )


def _handler_for(display_name):
    def handler(method, uri, _body):
        assert method == "GET"
        if f"/applications/{APP_OBJECT_ID}?" in uri:
            return application(display_name=display_name)
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [
                    {"id": "user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        if "/me?" in uri:
            return {"id": "user-id"}
        if "/appRoleAssignments?" in uri:
            return {
                "value": [
                    {"resourceId": "api-sp-id", "appRoleId": ROLE_ID}
                ]
            }
        raise AssertionError(uri)

    return handler


def test_missing_operational_defaults_are_initialized_without_overwrite():
    display_name = "Azure Service Health Slack Bot - production"
    azd = FakeAzd()
    azd.values["SERVICE_HEALTH_DEPLOY_WORKLOAD"] = "false"

    result = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(_handler_for(display_name)), azd
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_tenant_id=TENANT_ID,
    )

    # Pre-existing value is preserved, not overwritten with the default.
    assert result["SERVICE_HEALTH_DEPLOY_WORKLOAD"] == "false"
    assert azd.values["SERVICE_HEALTH_DEPLOY_WORKLOAD"] == "false"
    # Missing defaults are initialized exactly once.
    for name, default in NONSECRET_AZD_DEFAULTS.items():
        if name == "SERVICE_HEALTH_DEPLOY_WORKLOAD":
            continue
        assert result[name] == default
        assert azd.values[name] == default


def test_ensure_operational_defaults_is_idempotent_on_rerun():
    display_name = "Azure Service Health Slack Bot - production"
    azd = FakeAzd()

    configurator = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(_handler_for(display_name)), azd
    )
    first = configurator.configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_tenant_id=TENANT_ID,
    )
    second = configurator.configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_tenant_id=TENANT_ID,
    )

    assert first == second
    for name, default in NONSECRET_AZD_DEFAULTS.items():
        assert azd.values[name] == default


def _dotenv_azd(tmp_path, environment_name, secret=None):
    dotenv_path = tmp_path / f"{environment_name}.env"
    contents = []
    if secret is not None:
        contents.append(f"{LEGACY_TOKEN_ENV_NAME}={secret}")
    dotenv_path.write_text(
        "\n".join(contents) + ("\n" if contents else ""), encoding="utf-8"
    )
    azd = FakeAzd(
        environments=[{"Name": environment_name, "DotEnvPath": str(dotenv_path)}]
    )
    return azd, dotenv_path


def test_infrastructure_only_provision_allows_lock_bootstrap_with_legacy_token(
    tmp_path,
):
    azd, _ = _dotenv_azd(
        tmp_path, "production", secret="xoxb-legacy-secret-value"
    )
    azd.values["SERVICE_HEALTH_DEPLOY_WORKLOAD"] = "false"
    configurator = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(lambda *_args: None), azd
    )

    configurator.enforce_token_migration_precondition("production")


def test_legacy_token_without_migration_marker_fails_closed_before_any_mutation(tmp_path):
    display_name = "Azure Service Health Slack Bot - production"
    azd, _ = _dotenv_azd(tmp_path, "production", secret="xoxb-legacy-secret-value")
    graph = FakeGraph(_handler_for(display_name))

    with pytest.raises(ScopeManagerError, match="legacy SLACK_BOT_TOKEN"):
        SecureWebhookConfigurator(FakeAzure(), graph, azd).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
            expected_tenant_id=TENANT_ID,
            environment_name="production",
        )

    assert graph.calls == []
    assert NONSECRET_AZD_DEFAULTS.keys().isdisjoint(azd.values)


def test_legacy_token_with_migration_marker_still_fails_closed(tmp_path):
    display_name = "Azure Service Health Slack Bot - production"
    azd, _ = _dotenv_azd(tmp_path, "production", secret="xoxb-legacy-secret-value")
    azd.values[TOKEN_MIGRATION_MARKER_ENV_NAME] = "true"

    with pytest.raises(ScopeManagerError, match="legacy SLACK_BOT_TOKEN"):
        SecureWebhookConfigurator(
            FakeAzure(), FakeGraph(_handler_for(display_name)), azd
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
            expected_tenant_id=TENANT_ID,
            environment_name="production",
        )


def test_legacy_token_precondition_never_prints_or_raises_with_token_value(tmp_path):
    secret = "xoxb-super-secret-value"
    azd, _ = _dotenv_azd(tmp_path, "production", secret=secret)

    configurator = SecureWebhookConfigurator(FakeAzure(), FakeGraph(lambda *_a: None), azd)
    with pytest.raises(ScopeManagerError) as exc_info:
        configurator.enforce_token_migration_precondition("production")

    assert secret not in str(exc_info.value)


def test_legacy_token_precondition_never_calls_get_environment_value_for_token(tmp_path):
    secret = "xoxb-super-secret-value"
    azd, _ = _dotenv_azd(tmp_path, "production", secret=secret)
    original_get = azd.get_environment_value

    def guarded_get(name):
        assert name != LEGACY_TOKEN_ENV_NAME, "must never fetch legacy token via azd"
        return original_get(name)

    azd.get_environment_value = guarded_get
    configurator = SecureWebhookConfigurator(FakeAzure(), FakeGraph(lambda *_a: None), azd)
    with pytest.raises(ScopeManagerError):
        configurator.enforce_token_migration_precondition("production")


def test_legacy_token_precondition_fails_when_environment_name_unresolved():
    azd = FakeAzd(environments=[])
    configurator = SecureWebhookConfigurator(FakeAzure(), FakeGraph(lambda *_a: None), azd)
    with pytest.raises(ScopeManagerError, match="Could not resolve"):
        configurator.enforce_token_migration_precondition()


def test_legacy_token_absent_locally_proceeds_even_if_azd_has_no_value(tmp_path):
    display_name = "Azure Service Health Slack Bot - production"
    azd, _ = _dotenv_azd(tmp_path, "production", secret=None)

    result = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(_handler_for(display_name)), azd
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
        expected_tenant_id=TENANT_ID,
        environment_name="production",
    )

    assert result["SERVICE_HEALTH_DEPLOY_WORKLOAD"] == "false"
