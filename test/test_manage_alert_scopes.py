import io
import json
import subprocess
from copy import deepcopy

import pytest

import scripts.manage_alert_scopes as scope_cli
import scripts.manage_alert_scopes as manage_alert_scopes
from scripts.manage_alert_scopes import (
    AzureCli,
    MANAGER_TAG,
    ScopeManager,
    ScopeManagerError,
    is_unreadable_subscription_error,
    resource_coordinates,
    resource_suffix,
)


TENANT = "tenant-1"
CENTRAL_SUBSCRIPTION = "central-sub"
TARGET_SUBSCRIPTION = "target-sub"
GROUP_ID = "platform"


class FakeAzure:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda _args: None)

    def invoke(self, *args):
        self.calls.append(args)
        return self.handler(args)


def central():
    return {
        "EnvironmentName": "production",
        "TenantId": TENANT,
        "SubscriptionId": CENTRAL_SUBSCRIPTION,
        "ResourceGroup": "rg-production",
        "Location": "eastus",
        "WebhookUri": "https://app.example/api/service-health",
        "SecureWebhookClientId": "client-id",
        "SecureWebhookObjectId": "object-id",
        "SecureWebhookIdentifierUri": "api://client-id",
        "AnchorActionGroupId": (
            f"/subscriptions/{CENTRAL_SUBSCRIPTION}/resourceGroups/rg-production/"
            "providers/Microsoft.Insights/actionGroups/ag-production-service-health"
        ),
        "ProtectedAlertId": (
            f"/subscriptions/{CENTRAL_SUBSCRIPTION}/resourceGroups/rg-production/"
            "providers/Microsoft.Insights/activityLogAlerts/baseline"
        ),
        "ProtectedScopeKind": "subscription",
        "ProtectedScopeId": CENTRAL_SUBSCRIPTION,
        "Accounts": [
            {"id": CENTRAL_SUBSCRIPTION, "tenantId": TENANT, "state": "Enabled"},
            {"id": TARGET_SUBSCRIPTION, "tenantId": TENANT, "state": "Enabled"},
        ],
    }


def scope_member(
    subscription_id=TARGET_SUBSCRIPTION,
    scope_kind="subscription",
    scope_id=None,
    enabled=True,
    action_group_enabled=True,
    orphan=False,
):
    scope_id = scope_id or subscription_id
    base = (
        f"/subscriptions/{subscription_id}/resourceGroups/rg-alerts/"
        "providers/Microsoft.Insights"
    )
    return {
        "ScopeKind": scope_kind,
        "ScopeId": scope_id,
        "ScopeResourceId": f"/subscriptions/{subscription_id}",
        "AlertId": None if orphan else f"{base}/activityLogAlerts/alert-{subscription_id}",
        "ActionGroupId": f"{base}/actionGroups/ag-{subscription_id}",
        "Enabled": enabled and not orphan,
        "ActionGroupEnabled": action_group_enabled,
        "TenantId": TENANT,
        "ManagedBy": MANAGER_TAG,
        "MemberSubscriptionId": subscription_id,
        "OrphanedActionGroup": orphan,
    }


def action_group_resource(item):
    return {
        "id": item["ActionGroupId"],
        "properties": {"enabled": item["ActionGroupEnabled"]},
        "tags": {
            "workload": "azure-service-health-slack-bot",
            "azd-env-name": "production",
            "service-health-managed-by": MANAGER_TAG,
            "service-health-central-subscription": CENTRAL_SUBSCRIPTION,
            "service-health-scope-kind": item["ScopeKind"],
            "service-health-scope-id": item["ScopeId"],
            "service-health-member-subscription": item[
                "MemberSubscriptionId"
            ],
        },
    }


def manager(azure=None, scopes=None, should_process=None, confirm=None):
    result = ScopeManager(
        azure or FakeAzure(),
        should_process=should_process,
        confirm_destructive=confirm,
    )
    result.central = central()
    result.scopes = scopes or []
    return result


def permission_response():
    return {"value": [{"actions": ["*"], "notActions": []}]}


def test_resource_identifiers_are_stable_and_strict():
    assert resource_suffix("subscription", "ABC") == resource_suffix(
        "subscription", "abc"
    )
    assert resource_suffix("subscription", "abc").startswith("sub-")
    assert resource_suffix("managementGroup", "abc").startswith("mg-")
    assert resource_coordinates(
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Insights/actionGroups/ag"
    ) == ("sub", "rg", "ag")
    with pytest.raises(ScopeManagerError, match="Unsupported Azure resource ID"):
        resource_coordinates("/providers/Microsoft.Management/managementGroups/mg")


@pytest.mark.parametrize(
    "message",
    [
        "AuthorizationFailed",
        "SubscriptionNotFound",
        "does not have authorization to perform action 'x/resourceGroups/read'",
        "The subscription 'x' could not be found",
    ],
)
def test_only_expected_discovery_errors_are_skippable(message):
    assert is_unreadable_subscription_error(ScopeManagerError(message))
    assert not is_unreadable_subscription_error(ScopeManagerError("network failed"))


def test_azure_cli_retries_transient_reads_and_parses_json():
    results = iter(
        [
            subprocess.CompletedProcess([], 1, "", "429 TooManyRequests"),
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), ""),
        ]
    )
    sleeps = []
    cli = AzureCli(runner=lambda *_args, **_kwargs: next(results), sleep=sleeps.append)

    assert cli.invoke("account", "show") == {"ok": True}
    assert sleeps == [1.0]


def test_azure_cli_invokes_resolved_windows_command_path(monkeypatch):
    monkeypatch.setattr(
        scope_cli.shutil,
        "which",
        lambda _name: r"C:\Program Files\Azure CLI\az.cmd",
    )
    cli = AzureCli()
    commands = []
    cli.runner = lambda command, **_kwargs: (
        commands.append(command)
        or subprocess.CompletedProcess(command, 0, "{}", "")
    )

    cli.invoke("account", "show")

    assert commands[0][0] == r"C:\Program Files\Azure CLI\az.cmd"


def test_azure_cli_never_retries_mutations():
    calls = []

    def run(*_args, **_kwargs):
        calls.append(True)
        return subprocess.CompletedProcess([], 1, "", "429 TooManyRequests")

    with pytest.raises(ScopeManagerError, match="Azure CLI command failed"):
        AzureCli(runner=run, sleep=lambda _: None).invoke(
            "resource", "delete", "--ids", "/resource"
        )
    assert len(calls) == 1


def test_azure_cli_rejects_invalid_json():
    result = subprocess.CompletedProcess([], 0, "not-json", "")
    with pytest.raises(ScopeManagerError, match="invalid JSON"):
        AzureCli(runner=lambda *_args, **_kwargs: result).invoke("account", "show")


def test_subscription_tenant_validation_is_exact():
    good = manager(
        FakeAzure(
            lambda args: {
                "id": args[-1],
                "tenantId": TENANT,
                "state": "Enabled",
            }
        )
    )
    assert good.test_subscription_tenant(TARGET_SUBSCRIPTION)["tenantId"] == TENANT

    wrong = manager(
        FakeAzure(
            lambda _args: {
                "id": TARGET_SUBSCRIPTION,
                "tenantId": "other",
                "state": "Enabled",
            }
        )
    )
    with pytest.raises(ScopeManagerError, match="Multi-tenant"):
        wrong.test_subscription_tenant(TARGET_SUBSCRIPTION)


def test_azure_identifiers_are_compared_case_insensitively(monkeypatch):
    instance = manager(scopes=[scope_member(subscription_id="ABC-DEF")])
    instance.central["ProtectedScopeId"] = "ABC-DEF"
    assert instance.protected_baseline_covers_subscription("abc-def") is True
    assert instance.unique_scope("subscription", "abc-def") is not None

    instance.central["ProtectedScopeId"] = CENTRAL_SUBSCRIPTION
    monkeypatch.setattr(instance, "test_subscription_tenant", lambda _id: {})
    monkeypatch.setattr(
        instance, "subscription_covered_by_management_group", lambda _id: False
    )
    result = instance.add_scope("subscription", "abc-def")
    assert result["Status"] == "AlreadyPresent"


def test_management_group_expands_paginated_descendants():
    pages = {
        "page-2": {
            "value": [
                {
                    "type": "/subscriptions",
                    "name": TARGET_SUBSCRIPTION,
                },
                {
                    "type": "/subscriptions",
                    "name": "child-2",
                },
            ]
        }
    }

    def handler(args):
        url = args[args.index("--url") + 1]
        if "/descendants?" in url:
            return {
                "value": [
                    {
                        "type": "/managementGroups",
                        "name": "child-mg",
                    }
                ],
                "nextLink": "page-2",
            }
        if url == "page-2":
            return pages[url]
        return {"properties": {"tenantId": TENANT}}

    instance = manager(FakeAzure(handler))
    instance.central["Accounts"].append(
        {"id": "child-2", "tenantId": TENANT, "state": "Enabled"}
    )

    coverage = instance.get_management_group_coverage(GROUP_ID)

    assert coverage["SubscriptionIds"] == ["child-2", TARGET_SUBSCRIPTION]
    assert coverage["DescendantManagementGroupIds"] == ["child-mg"]


def test_management_group_rejects_foreign_tenant_and_inaccessible_descendants():
    foreign = manager(FakeAzure(lambda _args: {"tenantId": "other"}))
    with pytest.raises(ScopeManagerError, match="not proven"):
        foreign.get_management_group_coverage(GROUP_ID)

    def handler(args):
        url = args[args.index("--url") + 1]
        if "/descendants?" in url:
            return {
                "value": [
                    {"type": "/subscriptions", "name": "inaccessible-sub"}
                ]
            }
        return {"tenantId": TENANT}

    inaccessible = manager(FakeAzure(handler))
    with pytest.raises(ScopeManagerError, match="not accessible"):
        inaccessible.get_management_group_coverage(GROUP_ID)


def test_membership_requires_exact_unique_current_descendants():
    member = scope_member(
        scope_kind="managementGroup", scope_id=GROUP_ID
    )
    instance = manager()
    logical = instance.new_management_group_state(GROUP_ID, [member])
    exact = {"SubscriptionIds": [TARGET_SUBSCRIPTION]}
    changed = {"SubscriptionIds": [TARGET_SUBSCRIPTION, "new-sub"]}

    assert instance.membership_state(logical, exact)["Complete"] is True
    state = instance.membership_state(logical, changed)
    assert state["Complete"] is False
    assert state["MissingIds"] == ["new-sub"]
    with pytest.raises(ScopeManagerError, match="exact alert member"):
        instance.assert_membership_complete(logical, changed)


def test_permission_check_honors_not_actions_and_fails_before_mutation():
    azure = FakeAzure(
        lambda _args: {
            "value": [
                {
                    "actions": ["Microsoft.Insights/*"],
                    "notActions": ["Microsoft.Insights/activityLogAlerts/delete"],
                }
            ]
        }
    )
    instance = manager(azure)
    with pytest.raises(ScopeManagerError, match="activityLogAlerts/delete"):
        instance.assert_permissions(
            f"/subscriptions/{TARGET_SUBSCRIPTION}",
            TARGET_SUBSCRIPTION,
            ["Microsoft.Insights/activityLogAlerts/delete"],
        )


def test_official_webhook_test_requires_exact_complete_secure_receiver_result():
    item = scope_member()

    def successful(_args):
        return {
            "state": "Complete",
            "actionDetails": [
                {
                    "Name": "slack-service-health",
                    "MechanismType": "SecureWebhook",
                    "Status": "Succeeded",
                }
            ],
        }

    assert manager(FakeAzure(successful)).official_webhook_test(item) == "Complete"

    def incomplete(_args):
        return {"state": "Complete", "actionDetails": []}

    with pytest.raises(ScopeManagerError, match="exactly one result"):
        manager(FakeAzure(incomplete)).official_webhook_test(item)


def test_deployment_validation_accepts_arm_id_casing_differences():
    item = scope_member()

    def handler(_args):
        return {
            "properties": {
                "enabled": False,
                "scopes": [item["ScopeResourceId"].upper()],
                "condition": {
                    "allOf": [{"field": "category", "equals": "ServiceHealth"}]
                },
                "actions": {
                    "actionGroups": [
                        {"actionGroupId": item["ActionGroupId"].upper()}
                    ]
                },
            }
        }

    manager(FakeAzure(handler)).assert_deployed_scope_state(item)


def test_group_alert_activation_rolls_back_all_attempted_members():
    first = scope_member(
        subscription_id="sub-1",
        scope_kind="managementGroup",
        scope_id=GROUP_ID,
        enabled=False,
    )
    second = scope_member(
        subscription_id="sub-2",
        scope_kind="managementGroup",
        scope_id=GROUP_ID,
        enabled=False,
    )
    logical = manager().new_management_group_state(GROUP_ID, [first, second])
    updates = []

    def handler(args):
        resource_id = args[args.index("--ids") + 1]
        value = args[args.index("--set") + 1].endswith("true")
        updates.append((resource_id, value))
        if resource_id == second["AlertId"] and value:
            raise ScopeManagerError("uncertain update")
        return {"properties": {"enabled": value}}

    instance = manager(FakeAzure(handler))
    with pytest.raises(ScopeManagerError, match="restored"):
        instance.set_alert_enabled(logical, True)
    assert first["Enabled"] is False
    assert second["Enabled"] is False
    assert updates[-2:] == [(second["AlertId"], False), (first["AlertId"], False)]


def test_add_subscription_is_idempotent_without_azure_mutation(monkeypatch):
    existing = scope_member()
    instance = manager(scopes=[existing])
    monkeypatch.setattr(instance, "test_subscription_tenant", lambda _id: {})
    monkeypatch.setattr(
        instance, "protected_baseline_covers_subscription", lambda _id: False
    )
    monkeypatch.setattr(
        instance, "subscription_covered_by_management_group", lambda _id: False
    )

    result = instance.add_scope("subscription", TARGET_SUBSCRIPTION)

    assert result["Status"] == "AlreadyPresent"
    assert instance.azure.calls == []


def test_add_management_group_fans_out_and_tests_before_enable(monkeypatch):
    instance = manager()
    coverage = {
        "ManagementGroupId": GROUP_ID,
        "TenantId": TENANT,
        "SubscriptionIds": ["sub-1", "sub-2"],
        "DescendantManagementGroupIds": [],
    }
    monkeypatch.setattr(instance, "get_management_group_coverage", lambda _id: coverage)
    monkeypatch.setattr(
        instance, "protected_baseline_overlaps_management_group", lambda _coverage: False
    )
    monkeypatch.setattr(
        instance, "overlaps_for_management_group", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(instance, "assert_add_permissions", lambda *_args: None)
    created = []

    def create(_kind, scope_id, subscription_id):
        created.append(subscription_id)
        return scope_member(
            subscription_id=subscription_id,
            scope_kind="managementGroup",
            scope_id=scope_id,
            enabled=False,
        )

    monkeypatch.setattr(instance, "new_scope_member", create)
    order = []
    monkeypatch.setattr(
        instance, "official_webhook_test", lambda _scope: order.append("test") or "Complete"
    )
    monkeypatch.setattr(
        instance,
        "set_alert_enabled",
        lambda scope, value: order.append("enable") or scope.update(Enabled=value),
    )

    result = instance.add_scope("managementGroup", GROUP_ID)

    assert result["Status"] == "Added"
    assert created == ["sub-1", "sub-2"]
    assert order == ["test", "enable"]


def test_remove_refuses_gap_before_confirmation(monkeypatch):
    item = scope_member()
    prompted = []
    instance = manager(scopes=[item], confirm=lambda question: prompted.append(question))
    monkeypatch.setattr(instance, "test_subscription_tenant", lambda _id: {})
    monkeypatch.setattr(
        instance, "subscription_covered_by_management_group", lambda _id: False
    )

    with pytest.raises(ScopeManagerError, match="coverage gap"):
        instance.remove_subscription(TARGET_SUBSCRIPTION)
    assert prompted == []
    assert instance.azure.calls == []


def test_remove_honors_what_if_without_prompt_or_delete(monkeypatch):
    item = scope_member()
    prompts = []
    instance = manager(
        scopes=[item],
        should_process=lambda _target, _operation: False,
        confirm=lambda question: prompts.append(question),
    )
    monkeypatch.setattr(instance, "test_subscription_tenant", lambda _id: {})
    monkeypatch.setattr(
        instance, "subscription_covered_by_management_group", lambda _id: True
    )
    monkeypatch.setattr(instance, "assert_remove_permissions", lambda _scope: None)

    result = instance.remove_subscription(TARGET_SUBSCRIPTION)

    assert result["Status"] == "Planned"
    assert prompts == []
    assert instance.azure.calls == []


def test_remove_rechecks_coverage_after_confirmation(monkeypatch):
    item = scope_member()
    instance = manager(scopes=[item], confirm=lambda _question: True)
    monkeypatch.setattr(instance, "test_subscription_tenant", lambda _id: {})
    coverage = iter([True, False])
    monkeypatch.setattr(
        instance,
        "subscription_covered_by_management_group",
        lambda _id: next(coverage),
    )
    monkeypatch.setattr(instance, "assert_remove_permissions", lambda _scope: None)
    monkeypatch.setattr(instance, "refresh", lambda: None)
    deletes = []
    monkeypatch.setattr(
        instance, "remove_scope_resources", lambda scope: deletes.append(scope)
    )

    with pytest.raises(ScopeManagerError, match="changed after confirmation"):
        instance.remove_subscription(TARGET_SUBSCRIPTION)
    assert deletes == []


@pytest.mark.parametrize(
    "managed_by,alert_id,action_group_id,error",
    [
        (
            "someone-else",
            "normal",
            "normal",
            "not owned",
        ),
        (
            MANAGER_TAG,
            "protected",
            "normal",
            "baseline",
        ),
        (
            MANAGER_TAG,
            "normal",
            "anchor",
            "baseline",
        ),
    ],
)
def test_delete_guards_cannot_be_bypassed(
    managed_by, alert_id, action_group_id, error
):
    item = scope_member()
    item["ManagedBy"] = managed_by
    if alert_id == "protected":
        item["AlertId"] = central()["ProtectedAlertId"]
    if action_group_id == "anchor":
        item["ActionGroupId"] = central()["AnchorActionGroupId"]
    instance = manager(scopes=[item])

    with pytest.raises(ScopeManagerError, match=error):
        instance.remove_scope_resources(item)
    assert instance.azure.calls == []


def test_orphan_cleanup_deletes_only_action_group():
    orphan = scope_member(orphan=True)

    def handler(args):
        if args[:2] == ("resource", "show"):
            return action_group_resource(orphan)
        if args[:4] == ("monitor", "activity-log", "alert", "list"):
            return []
        return None

    azure = FakeAzure(handler)
    instance = manager(azure, scopes=[orphan])

    instance.remove_scope_resources(orphan)

    assert azure.calls[-1] == (
        "resource",
        "delete",
        "--ids",
        orphan["ActionGroupId"],
    )


def test_action_group_delete_revalidates_ownership_and_all_alert_references():
    item = scope_member()
    forged = action_group_resource(item)
    forged["tags"]["service-health-managed-by"] = "someone-else"
    instance = manager(
        FakeAzure(
            lambda args: forged if args[:2] == ("resource", "show") else []
        ),
        scopes=[item],
    )
    with pytest.raises(ScopeManagerError, match="ownership metadata"):
        instance.remove_scope_resources(item)

    def referenced(args):
        if args[:2] == ("resource", "show"):
            return action_group_resource(item)
        if args[:4] == ("monitor", "activity-log", "alert", "list"):
            return [
                {
                    "id": "/subscriptions/other/alerts/unmanaged",
                    "actions": {
                        "actionGroups": [
                            {"actionGroupId": item["ActionGroupId"]}
                        ]
                    },
                }
            ]
        return None

    instance = manager(FakeAzure(referenced), scopes=[item])
    with pytest.raises(ScopeManagerError, match="still reference"):
        instance.remove_scope_resources(item)


def test_migration_restores_original_when_replacement_enable_fails(monkeypatch):
    original = scope_member()
    replacement = scope_member(
        scope_kind="managementGroup",
        scope_id=GROUP_ID,
        enabled=False,
    )
    logical = manager().new_management_group_state(GROUP_ID, [replacement])
    instance = manager(scopes=[original, logical], confirm=lambda _question: True)
    coverage = {
        "ManagementGroupId": GROUP_ID,
        "TenantId": TENANT,
        "SubscriptionIds": [TARGET_SUBSCRIPTION],
        "DescendantManagementGroupIds": [],
    }
    monkeypatch.setattr(instance, "get_management_group_coverage", lambda _id: coverage)
    monkeypatch.setattr(
        instance, "overlaps_for_management_group", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(instance, "assert_remove_permissions", lambda _scope: None)
    monkeypatch.setattr(instance, "refresh", lambda: None)
    monkeypatch.setattr(
        instance,
        "add_scope",
        lambda *_args, **_kwargs: {
            "Status": "ValidatedDisabled",
            "TestStatus": "Complete",
            "Scope": logical,
        },
    )
    monkeypatch.setattr(
        instance,
        "current_enabled",
        lambda item, resource_key, *_args: (
            True if resource_key == "ActionGroupId" else bool(item["Enabled"])
        ),
    )
    transitions = []

    def set_enabled(scope, enabled):
        transitions.append((scope["ScopeKind"], enabled))
        if scope is replacement and enabled:
            raise ScopeManagerError("enable failed")
        scope["Enabled"] = enabled

    monkeypatch.setattr(instance, "set_alert_enabled", set_enabled)

    with pytest.raises(ScopeManagerError, match="coverage remains intact"):
        instance.migrate_management_group(GROUP_ID)
    assert transitions == [
        ("subscription", False),
        ("managementGroup", True),
        ("subscription", True),
    ]
    assert original["Enabled"] is True


def test_migration_keeps_original_disabled_if_uncertain_replacement_is_enabled(
    monkeypatch,
):
    original = scope_member()
    replacement = scope_member(
        scope_kind="managementGroup",
        scope_id=GROUP_ID,
        enabled=False,
    )
    logical = manager().new_management_group_state(GROUP_ID, [replacement])
    instance = manager(scopes=[original, logical], confirm=lambda _question: True)
    coverage = {
        "ManagementGroupId": GROUP_ID,
        "TenantId": TENANT,
        "SubscriptionIds": [TARGET_SUBSCRIPTION],
        "DescendantManagementGroupIds": [],
    }
    monkeypatch.setattr(instance, "get_management_group_coverage", lambda _id: coverage)
    monkeypatch.setattr(
        instance, "overlaps_for_management_group", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(instance, "assert_remove_permissions", lambda _scope: None)
    monkeypatch.setattr(instance, "refresh", lambda: None)
    monkeypatch.setattr(
        instance,
        "add_scope",
        lambda *_args, **_kwargs: {
            "Status": "ValidatedDisabled",
            "TestStatus": "Complete",
            "Scope": logical,
        },
    )

    def current(item, resource_key, *_args):
        if resource_key == "ActionGroupId":
            return True
        return item is replacement

    monkeypatch.setattr(instance, "current_enabled", current)

    def set_enabled(scope, enabled):
        if scope is replacement and enabled:
            raise ScopeManagerError("uncertain response")
        scope["Enabled"] = enabled

    monkeypatch.setattr(instance, "set_alert_enabled", set_enabled)

    with pytest.raises(ScopeManagerError, match="preserving one active path"):
        instance.migrate_management_group(GROUP_ID)
    assert original["Enabled"] is False


def test_central_discovery_validates_tags_receiver_and_baseline_relationship():
    c = central()
    group = {
        "name": c["ResourceGroup"],
        "location": c["Location"],
        "tags": {
            "workload": "azure-service-health-slack-bot",
            "azd-env-name": c["EnvironmentName"],
        },
    }
    app = {
        "name": f"ca-{c['EnvironmentName']}",
        "id": c["ContainerAppId"] if "ContainerAppId" in c else (
            f"/subscriptions/{CENTRAL_SUBSCRIPTION}/resourceGroups/{c['ResourceGroup']}"
            "/providers/Microsoft.App/containerApps/ca-production"
        ),
    }
    anchor = {
        "name": "ag-production-service-health",
        "id": c["AnchorActionGroupId"],
        "webhookReceivers": [
            {
                "serviceUri": c["WebhookUri"],
                "useAadAuth": True,
                "tenantId": TENANT,
                "objectId": c["SecureWebhookObjectId"],
                "identifierUri": c["SecureWebhookIdentifierUri"],
            }
        ],
    }
    baseline = {
        "id": c["ProtectedAlertId"],
        "scopes": [f"/subscriptions/{CENTRAL_SUBSCRIPTION}"],
        "actions": {
            "actionGroups": [{"actionGroupId": c["AnchorActionGroupId"]}]
        },
    }

    def handler(args):
        if args[:2] == ("account", "show"):
            return {
                "id": CENTRAL_SUBSCRIPTION,
                "tenantId": TENANT,
                "state": "Enabled",
            }
        if args[:2] == ("account", "list"):
            return [
                {
                    "id": CENTRAL_SUBSCRIPTION,
                    "tenantId": TENANT,
                    "state": "Enabled",
                }
            ]
        if args[:2] == ("group", "list"):
            return [group]
        if args[:2] == ("resource", "list"):
            return [app]
        if args[:3] == ("monitor", "action-group", "list"):
            return [anchor]
        if args[:4] == ("monitor", "activity-log", "alert", "list"):
            return [baseline]
        url = args[args.index("--url") + 1]
        if "/authConfigs/current" in url:
            return {
                "properties": {
                    "identityProviders": {
                        "azureActiveDirectory": {
                            "registration": {
                                "clientId": c["SecureWebhookClientId"]
                            }
                        }
                    }
                }
            }
        return {
            "properties": {
                "configuration": {
                    "ingress": {"fqdn": "app.example"}
                }
            }
        }

    discovered = ScopeManager(FakeAzure(handler)).get_central_deployment()

    assert discovered["TenantId"] == TENANT
    assert discovered["ProtectedAlertId"] == c["ProtectedAlertId"]
    assert discovered["SecureWebhookClientId"] == c["SecureWebhookClientId"]


def test_reporting_marks_incomplete_management_group_as_ineffective(monkeypatch):
    child = scope_member(
        scope_kind="managementGroup", scope_id=GROUP_ID
    )
    logical = manager().new_management_group_state(GROUP_ID, [child])
    instance = manager(scopes=[logical])
    coverage = {
        "ManagementGroupId": GROUP_ID,
        "TenantId": TENANT,
        "SubscriptionIds": [TARGET_SUBSCRIPTION, "new-sub"],
        "DescendantManagementGroupIds": [],
    }
    monkeypatch.setattr(instance, "get_management_group_coverage", lambda _id: coverage)
    monkeypatch.setattr(
        instance, "protected_baseline_overlaps_management_group", lambda _coverage: False
    )

    report = instance.report()

    assert report[0]["EffectiveCoverage"] == "Incomplete"
    assert "missing members: new-sub" in report[0]["CoverageDetail"]


def test_management_group_state_does_not_mutate_inputs():
    first = scope_member(
        scope_kind="managementGroup", scope_id=GROUP_ID
    )
    before = deepcopy(first)
    manager().new_management_group_state(GROUP_ID, [first])
    assert first == before


def test_interactive_confirmation_prompt_uses_stderr(monkeypatch, capsys):
    class InteractiveInput(io.StringIO):
        def isatty(self):
            return True

    class FakeManager:
        def __init__(self, _azure, **kwargs):
            self.confirm = kwargs["confirm_destructive"]

        def execute(self, *_args, **_kwargs):
            return {"confirmed": self.confirm("Delete managed resources?")}

    monkeypatch.setattr(manage_alert_scopes, "AzureCli", lambda: object())
    monkeypatch.setattr(manage_alert_scopes, "ScopeManager", FakeManager)
    monkeypatch.setattr(
        manage_alert_scopes.sys,
        "stdin",
        InteractiveInput("yes\n"),
    )

    result = manage_alert_scopes.main(
        [
            "remove-subscription",
            "--subscription-id",
            TARGET_SUBSCRIPTION,
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert result == 0
    assert json.loads(captured.out) == {"confirmed": True}
    assert captured.err == "Delete managed resources? [y/N] "
