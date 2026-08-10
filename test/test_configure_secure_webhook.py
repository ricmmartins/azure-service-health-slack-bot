import json
from pathlib import Path

import pytest

from scripts.configure_secure_webhook import (
    AzdCli,
    GraphClient,
    ScopeManagerError,
    SecureWebhookConfigurator,
    resolve_caller_owner_object_id,
)


TENANT_ID = "tenant-a"
APP_ID = "11111111-1111-1111-1111-111111111111"
APP_OBJECT_ID = "app-object-id"
ROLE_ID = "role-id"
AZNS_APP_ID = "461e8683-5575-4561-ac7f-899cc907d62a"


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

    def request(self, method, uri, body=None):
        self.calls.append((method, uri, body))
        return self.handler(method, uri, body)


class FakeAzd:
    def __init__(self):
        self.values = {}

    def set_environment_value(self, name, value):
        self.values[name] = value


def application(role=True):
    return {
        "id": APP_OBJECT_ID,
        "appId": APP_ID,
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

        def invoke(self, *args):
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
    )

    assert response == {"id": "created"}
    assert azure.body == {"displayName": 'Name with "quotes"'}
    assert not azure.body_path.exists()


def test_graph_collections_follow_pagination_for_existing_owners():
    next_link = "https://graph.microsoft.com/v1.0/owners-next-page"

    def handler(method, uri, _body):
        assert method == "GET"
        if "/applications?" in uri:
            return {"value": [application()]}
        if f"appId eq '{APP_ID}'" in uri:
            return {"value": [{"id": "api-sp-id", "appId": APP_ID}]}
        if f"appId eq '{AZNS_APP_ID}'" in uri:
            return {"value": [{"id": "azns-sp-id", "appId": AZNS_APP_ID}]}
        if "/owners?" in uri:
            return {
                "value": [{"id": "unrelated-owner"}],
                "@odata.nextLink": next_link,
            }
        if uri == next_link:
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

    graph = FakeGraph(handler)
    SecureWebhookConfigurator(FakeAzure(), graph, FakeAzd()).configure(
        "Azure Service Health Slack Bot - production"
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
    def handler(method, uri, _body):
        assert method == "GET"
        if "/applications?" in uri:
            return {"value": [application()]}
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
        "Azure Service Health Slack Bot - production"
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
    existing = application()
    existing["api"] = {
        "requestedAccessTokenVersion": 1,
        "oauth2PermissionScopes": [{"id": "scope-id", "value": "existing"}],
        "preAuthorizedApplications": [
            {"appId": AZNS_APP_ID, "delegatedPermissionIds": ["scope-id"]}
        ],
    }
    patches = []

    def handler(method, uri, body):
        if method == "GET" and "/applications?" in uri:
            return {"value": [existing]}
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
    ).configure("existing-api-settings")

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
        if method == "GET" and "/applications?" in uri:
            return {"value": []}
        if method == "POST" and uri.endswith("/applications"):
            return application(role=False)
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
    assert mutation_paths[0][1].endswith("/applications")
    assert any("/owners/$ref" in uri for _method, uri in mutation_paths)
    assert mutation_paths[-1][1].endswith("/appRoleAssignments")
    assert result == azd.values


def test_duplicate_application_or_role_is_rejected():
    duplicate_apps = FakeGraph(
        lambda _method, uri, _body: (
            {"value": [application(), application()]}
            if "/applications?" in uri
            else None
        )
    )
    with pytest.raises(ScopeManagerError, match="multiple application"):
        SecureWebhookConfigurator(
            FakeAzure(), duplicate_apps, FakeAzd()
        ).configure("duplicate")

    duplicate_role_app = application()
    duplicate_role_app["appRoles"].append(
        {"id": "role-2", "value": "ActionGroupsSecureWebhook"}
    )
    duplicate_roles = FakeGraph(
        lambda _method, uri, _body: (
            {"value": [duplicate_role_app]}
            if "/applications?" in uri
            else None
        )
    )
    with pytest.raises(ScopeManagerError, match="multiple app roles"):
        SecureWebhookConfigurator(
            FakeAzure(), duplicate_roles, FakeAzd()
        ).configure("duplicate-role")


@pytest.mark.parametrize(
    "role_update,error",
    [
        ({"isEnabled": False}, "not enabled"),
        ({"allowedMemberTypes": ["User"]}, "does not allow application"),
    ],
)
def test_existing_role_must_remain_application_enabled(role_update, error):
    existing = application()
    existing["appRoles"][0].update(role_update)
    graph = FakeGraph(
        lambda _method, uri, _body: (
            {"value": [existing]} if "/applications?" in uri else None
        )
    )
    with pytest.raises(ScopeManagerError, match=error):
        SecureWebhookConfigurator(
            FakeAzure(), graph, FakeAzd()
        ).configure("invalid-role")


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
