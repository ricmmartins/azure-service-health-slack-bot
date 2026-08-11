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
APP_OBJECT_ID = "22222222-2222-2222-2222-222222222222"
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
        if method == "GET" and "/applications?" in uri:
            return {"value": []}
        if method == "POST" and uri.endswith("/applications"):
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
    assert mutation_paths[0][1].endswith("/applications")
    assert any("/owners/$ref" in uri for _method, uri in mutation_paths)
    assert mutation_paths[-1][1].endswith("/appRoleAssignments")
    assert result == azd.values


def test_new_application_rolls_back_when_object_id_cannot_be_persisted():
    deleted = []

    def handler(method, uri, body):
        if method == "GET" and "/applications?" in uri:
            return {"value": []}
        if method == "POST" and uri.endswith("/applications"):
            return application(role=False, display_name=body["displayName"])
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


def test_display_name_collision_and_duplicate_role_are_rejected():
    duplicate_apps = FakeGraph(
        lambda _method, uri, _body: (
            {"value": [application(), application()]}
            if "/applications?" in uri
            else None
        )
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
    with pytest.raises(ScopeManagerError, match="unexpected owners"):
        SecureWebhookConfigurator(
            FakeAzure(), graph, FakeAzd()
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )

    assert not [
        call for call in graph.calls if call[0] in {"POST", "PATCH"}
    ]


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

    with pytest.raises(ScopeManagerError, match="unexpected owners"):
        SecureWebhookConfigurator(
            FakeAzure(), FakeGraph(handler), FakeAzd()
        ).configure(
            display_name,
            application_object_id=APP_OBJECT_ID,
            application_client_id=APP_ID,
        )

    result = SecureWebhookConfigurator(
        FakeAzure(), FakeGraph(handler), FakeAzd()
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


def test_legacy_rerun_allows_new_caller_and_persists_owner_baseline():
    display_name = "legacy-shared-environment"
    account = {
        "tenantId": TENANT_ID,
        "user": {"type": "user", "name": "new@example.com"},
    }
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
            return {
                "value": [
                    {"id": "original-user-id"},
                    {"id": "azns-sp-id"},
                ]
            }
        if method == "POST" and "/owners/$ref" in uri:
            owner_posts.append(body["@odata.id"])
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

    result = SecureWebhookConfigurator(
        FakeAzure(account), FakeGraph(handler), FakeAzd()
    ).configure(
        display_name,
        application_object_id=APP_OBJECT_ID,
        application_client_id=APP_ID,
    )

    assert owner_posts == [
        "https://graph.microsoft.com/v1.0/directoryObjects/new-user-id"
    ]
    assert set(result["SERVICE_HEALTH_API_OWNER_IDS"].split(",")) == {
        "original-user-id",
        "new-user-id",
        "azns-sp-id",
    }


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
