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
from scripts.operation_lock import DEFAULT_LOCK_NAME
from fake_blob_lock import FakeBlobClient, FakeBlobError, FakeBlobService


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
        "etag": '"etag-action-group"',
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


def test_azure_cli_scrubs_slack_env_and_preserves_azure_auth(monkeypatch):
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-process-canary-123")
    monkeypatch.setenv("SLACK_ACCESS_TOKEN", "not-token-shaped")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "legitimate-sp-secret")
    monkeypatch.setenv(
        "AZURE_FEDERATED_TOKEN_FILE", "/tmp/federated-token-file"
    )

    AzureCli(runner=runner).invoke("account", "show")

    assert "SLACK_BOT_TOKEN" not in captured["env"]
    assert "SLACK_ACCESS_TOKEN" not in captured["env"]
    assert captured["env"]["AZURE_CLIENT_ID"] == "client-id"
    assert (
        captured["env"]["AZURE_CLIENT_SECRET"]
        == "legitimate-sp-secret"
    )
    assert (
        captured["env"]["AZURE_FEDERATED_TOKEN_FILE"]
        == "/tmp/federated-token-file"
    )
    assert all(
        "xoxb-process-canary-123" not in str(value)
        for value in (captured["command"], captured["env"])
    )


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


def test_azure_cli_bypasses_batch_launcher_when_bundled_python_exists(
    monkeypatch,
    tmp_path,
):
    launcher = tmp_path / "wbin" / "az.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@echo off", encoding="utf-8")
    bundled_python = tmp_path / "python.exe"
    bundled_python.write_bytes(b"")
    monkeypatch.setattr(
        scope_cli.shutil,
        "which",
        lambda _name: str(launcher),
    )
    cli = AzureCli()
    commands = []
    cli.runner = lambda command, **_kwargs: (
        commands.append(command)
        or subprocess.CompletedProcess(command, 0, "{}", "")
    )

    cli.invoke(
        "rest",
        "--method",
        "get",
        "--url",
        "https://example.invalid/items?api-version=1&$skiptoken=next",
    )

    assert commands[0][:3] == [
        str(bundled_python),
        "-IBm",
        "azure.cli",
    ]
    assert (
        "https://example.invalid/items?api-version=1&$skiptoken=next"
        in commands[0]
    )


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


def test_azure_cli_extracts_error_code_from_wrapped_json():
    detail = (
        'ERROR: Not Found({"error":{"code":"DeploymentNotFound",'
        '"message":"The deployment could not be found."}})'
    )

    assert scope_cli._azure_error_metadata(detail) == (
        None,
        "DeploymentNotFound",
    )


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


def test_official_webhook_test_accepts_documented_completed_result():
    item = scope_member()

    def successful(_args):
        return {
            "state": "Completed",
            "actionDetails": [
                {
                    "Name": "slack-service-health",
                    "MechanismType": "SecureWebhook",
                    "Status": "Completed",
                }
            ],
        }

    assert manager(FakeAzure(successful)).official_webhook_test(item) == "Complete"


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("Succeeded", "Succeeded"),
        ("Complete", "Complete"),
        ("completed", "Completed"),
    ],
)
def test_official_webhook_test_rejects_unrecognized_result_values(state, status):
    item = scope_member()

    def result(_args):
        return {
            "state": state,
            "actionDetails": [
                {
                    "Name": "slack-service-health",
                    "MechanismType": "SecureWebhook",
                    "Status": status,
                }
            ],
        }

    with pytest.raises(ScopeManagerError):
        manager(FakeAzure(result)).official_webhook_test(item)


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
    events = []

    def handler(args):
        if args[:2] == ("resource", "show"):
            return action_group_resource(orphan)
        if args[:4] == ("monitor", "activity-log", "alert", "list"):
            events.append("scan")
            return []
        if args[:2] == ("resource", "delete"):
            events.append("delete")
        return None

    azure = FakeAzure(handler)
    instance = manager(azure, scopes=[orphan])
    instance.assert_operation_membership_unchanged = (
        lambda: events.append("revalidate")
    )

    instance.remove_scope_resources(orphan)

    assert azure.calls[-1] == (
        "resource",
        "delete",
        "--ids",
        orphan["ActionGroupId"],
    )
    assert events[-2:] == ["scan", "delete"]


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


def test_action_group_delete_stops_on_reference_added_after_alert_delete():
    item = scope_member()
    list_calls = 0
    deletes = []

    def handler(args):
        nonlocal list_calls
        if args[:2] == ("resource", "show"):
            return action_group_resource(item)
        if args[:4] == (
            "monitor",
            "activity-log",
            "alert",
            "list",
        ):
            list_calls += 1
            if list_calls <= len(central()["Accounts"]):
                return []
            return [
                {
                    "id": "/subscriptions/other/alerts/new-reference",
                    "actions": {
                        "actionGroups": [
                            {"actionGroupId": item["ActionGroupId"]}
                        ]
                    },
                }
            ]
        if args[:2] == ("resource", "delete"):
            deletes.append(args[-1])
        return None

    instance = manager(FakeAzure(handler), scopes=[item])

    with pytest.raises(ScopeManagerError, match="appeared after review"):
        instance.remove_scope_resources(item)

    assert deletes == [item["AlertId"]]


@pytest.mark.parametrize(
    "resource_type, reference_shape",
    (
        (
            "Microsoft.Insights/metricAlerts",
            {"properties": {"actions": [{"actionGroupId": None}]}},
        ),
        (
            "Microsoft.Insights/scheduledQueryRules",
            {"properties": {"actions": {"actionGroups": [None]}}},
        ),
        (
            "Microsoft.AlertsManagement/smartDetectorAlertRules",
            {"properties": {"actionGroups": {"groupIds": [None]}}},
        ),
        (
            "Microsoft.AlertsManagement/actionRules",
            {"properties": {"actions": [{"actionGroupIds": [None]}]}},
        ),
    ),
)
def test_action_group_delete_checks_all_monitor_rule_references(
    resource_type,
    reference_shape,
):
    item = scope_member()
    rule_id = (
        f"/subscriptions/{TARGET_SUBSCRIPTION}/resourceGroups/rg-alerts/"
        f"providers/{resource_type}/rule"
    )
    if resource_type.endswith("metricAlerts"):
        reference_shape["properties"]["actions"][0][
            "actionGroupId"
        ] = item["ActionGroupId"]
    elif resource_type.endswith("scheduledQueryRules"):
        reference_shape["properties"]["actions"]["actionGroups"][0] = (
            item["ActionGroupId"]
        )
    elif resource_type.endswith("smartDetectorAlertRules"):
        reference_shape["properties"]["actionGroups"]["groupIds"][0] = (
            item["ActionGroupId"]
        )
    else:
        reference_shape["properties"]["actions"][0][
            "actionGroupIds"
        ][0] = item["ActionGroupId"]
    rule = {"id": rule_id, **reference_shape}

    def handler(args):
        if args[:4] == (
            "monitor",
            "activity-log",
            "alert",
            "list",
        ):
            return []
        if args[:2] == ("resource", "list"):
            requested_type = args[args.index("--resource-type") + 1]
            return [{"id": rule_id}] if requested_type == resource_type else []
        if args[:2] == ("resource", "show"):
            resource_id = args[args.index("--ids") + 1]
            return (
                action_group_resource(item)
                if resource_id == item["ActionGroupId"]
                else rule
            )
        return None

    with pytest.raises(ScopeManagerError, match="still reference"):
        manager(
            FakeAzure(handler), scopes=[item]
        ).remove_scope_resources(item)


def test_migration_keeps_original_enabled_when_replacement_enable_fails(
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
    monkeypatch.setattr(
        instance,
        "current_enabled",
        lambda item, resource_key, *_args: (
            True if resource_key == "ActionGroupId" else bool(item["Enabled"])
        ),
    )
    transitions = []

    def set_enabled(scope, enabled, **_kwargs):
        transitions.append((scope["ScopeKind"], enabled))
        if scope is replacement and enabled:
            raise ScopeManagerError("enable failed")
        scope["Enabled"] = enabled

    monkeypatch.setattr(instance, "set_alert_enabled", set_enabled)

    with pytest.raises(ScopeManagerError, match="coverage remains intact"):
        instance.migrate_management_group(GROUP_ID)
    assert transitions == [("managementGroup", True)]
    assert original["Enabled"] is True


def test_migration_preserves_coverage_if_replacement_enable_is_uncertain(
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

    with pytest.raises(ScopeManagerError, match="duplicate delivery"):
        instance.migrate_management_group(GROUP_ID)
    assert original["Enabled"] is True


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


def test_interactive_confirmation_eof_fails_closed(monkeypatch, capsys):
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
        InteractiveInput(""),
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

    assert result == 1
    assert captured.out == ""
    assert (
        captured.err
        == "Delete managed resources? [y/N] ERROR: Destructive operations require "
        "interactive confirmation or --force for pre-approved noninteractive automation.\n"
    )


class LockingFakeAzure(FakeAzure):
    """A FakeAzure that also models the shared ARM lock/journal resources
    (immutable role assignments and Microsoft.Resources/deployments), so
    ScopeManager.execute()'s lock/journal integration can be exercised
    without any real Azure call."""

    def __init__(self, handler=None):
        super().__init__(handler)
        self.blob_service_factory = FakeBlobService()
        self.locks = self.blob_service_factory.store
        self.deployments = {}
        self._etag_counter = 0

    def _next_etag(self):
        self._etag_counter += 1
        return f"etag-{self._etag_counter}"

    def invoke(self, *args):
        self.calls.append(args)
        arguments = list(args)
        if (
            arguments[:2] == ["resource", "list"]
            and "--resource-type" in arguments
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
        if arguments[:3] == ["storage", "account", "show"]:
            return {
                "primaryEndpoints": {
                    "blob": "https://stlockcentral.blob.core.windows.net/"
                }
            }
        if arguments[:4] == ["storage", "account", "keys", "list"]:
            return [{"value": "fake-lock-key"}]
        if arguments[0] == "rest":
            uri = arguments[arguments.index("--uri") + 1]
            if "/providers/Microsoft.Resources/deployments/" in uri:
                return self._deployment(arguments, uri)
        return self.handler(args)

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
                "properties": {"outputs": {"journalState": {"value": value}}},
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


def locking_manager(azure=None, scopes=None):
    instance = ScopeManager(azure or LockingFakeAzure())
    instance.central = central()
    instance.scopes = scopes or []
    instance.initialize = lambda: None
    instance._caller_identity = lambda: "caller-object-id"
    return instance


def test_execute_mutating_command_acquires_lock_and_clears_journal_on_success(
    monkeypatch,
):
    azure = LockingFakeAzure(lambda _args: {"user": {"name": "operator@example.com"}})
    instance = locking_manager(azure, scopes=[scope_member()])
    monkeypatch.setattr(
        instance,
        "add_scope",
        lambda *_a, **_k: {"Status": "Added"},
    )

    result = instance.execute("add-subscription", subscription_id=TARGET_SUBSCRIPTION)

    assert result == {"Status": "Added"}
    # Lock is released and the journal entry is cleared after success.
    assert azure.locks == {}
    assert azure.deployments == {}
    assert any(
        call[:4] == ("storage", "account", "keys", "list")
        for call in azure.calls
    )


def test_execute_releases_lock_when_initial_journal_write_fails(monkeypatch):
    class JournalWriteFailure(LockingFakeAzure):
        def _deployment(self, arguments, uri):
            method = arguments[arguments.index("--method") + 1].lower()
            if method == "put":
                raise ScopeManagerError("journal unavailable")
            return super()._deployment(arguments, uri)

    azure = JournalWriteFailure(
        lambda _args: {"user": {"name": "operator@example.com"}}
    )
    instance = locking_manager(azure, scopes=[scope_member()])
    monkeypatch.setattr(
        instance,
        "add_scope",
        lambda *_a, **_k: {"Status": "Added"},
    )

    with pytest.raises(ScopeManagerError, match="journal unavailable"):
        instance.execute(
            "add-subscription",
            subscription_id=TARGET_SUBSCRIPTION,
        )

    assert azure.locks == {}


def test_execute_list_and_what_if_never_touch_lock_or_journal(monkeypatch):
    def handler(args):
        if args[:2] == ("account", "show"):
            return {"user": {"name": "operator@example.com"}}
        raise AssertionError(f"Unexpected call: {args}")

    azure = LockingFakeAzure(handler)
    instance = locking_manager(azure, scopes=[])
    monkeypatch.setattr(instance, "report", lambda: [])
    planned_operations = []

    def report_plan(target, operation):
        planned_operations.append((target, operation))
        return True

    instance.should_process = report_plan
    mutations = []

    def add_scope(*_args, **_kwargs):
        if instance.should_process("scope", "add"):
            mutations.append("add")
            return {"Status": "Added"}
        return {"Status": "Skipped"}

    monkeypatch.setattr(instance, "add_scope", add_scope)

    list_result = instance.execute("list")
    what_if_result = instance.execute(
        "add-subscription", subscription_id=TARGET_SUBSCRIPTION, what_if=True
    )

    assert list_result == []
    assert what_if_result["Status"] == "Skipped"
    assert what_if_result["ExecutionFingerprint"].startswith("v1:")
    assert what_if_result["ExecutionExpiresAt"] > 0
    assert mutations == []
    assert planned_operations == [("scope", "add")]
    assert azure.locks == {}
    assert azure.deployments == {}
    assert not any(
        arg[:4] == ("storage", "account", "keys", "list")
        or (
            arg[0] == "rest"
            and "Microsoft.Resources/deployments"
            in arg[arg.index("--uri") + 1]
        )
        for arg in azure.calls
    )


def test_execute_records_failure_state_and_never_clears_journal(monkeypatch):
    azure = LockingFakeAzure(lambda _args: {"user": {"name": "operator@example.com"}})
    instance = locking_manager(azure, scopes=[scope_member()])

    def boom(*_a, **_k):
        raise ScopeManagerError("simulated failure")

    monkeypatch.setattr(instance, "add_scope", boom)

    with pytest.raises(ScopeManagerError, match="simulated failure"):
        instance.execute("add-subscription", subscription_id=TARGET_SUBSCRIPTION)

    # The lock itself is always released (owner-only), but the journal entry
    # documenting the failure is deliberately preserved for manual recovery.
    assert azure.locks == {}
    assert len(azure.deployments) == 1
    (entry,) = azure.deployments.values()
    state = entry["properties"]["outputs"]["journalState"]["value"]
    assert state["State"] == "Failed"
    assert state["Error"] == "simulated failure"
    assert instance.operation_target is None


def test_execute_surfaces_lock_contention_as_operation_lock_error(monkeypatch):
    azure = LockingFakeAzure(lambda _args: {"user": {"name": "operator@example.com"}})
    instance = locking_manager(azure, scopes=[scope_member()])
    called = []
    monkeypatch.setattr(
        instance, "add_scope", lambda *_a, **_k: called.append(1) or {"Status": "Added"}
    )
    # Simulate a concurrent operation already holding the lock.
    azure.locks[DEFAULT_LOCK_NAME] = {
        "data": json.dumps(
            {
                "environment": "production",
                "command": "remove-subscription",
                "target": "subscription 'other'",
                "caller": "someone-else@example.com",
                "nonce": "other-nonce",
                "startedAt": 1000.0,
                "expiresAt": 100000000.0,
            }
        ).encode(),
        "lease_id": "other-lease",
    }

    with pytest.raises(
        manage_alert_scopes.OperationLockError,
        match="another operation appears to be in progress",
    ):
        instance.execute("add-subscription", subscription_id=TARGET_SUBSCRIPTION)

    assert called == []
    # The pre-existing lock (owned by the other operation) must be untouched.
    assert json.loads(
        azure.locks[DEFAULT_LOCK_NAME]["data"]
    )["nonce"] == "other-nonce"
    assert instance.operation_target is None


def test_execute_journal_fingerprint_reflects_current_membership(monkeypatch):
    azure = LockingFakeAzure(lambda _args: {"user": {"name": "operator@example.com"}})
    existing = scope_member()
    instance = locking_manager(azure, scopes=[existing])
    captured_states = []

    def add_scope(*_a, **_k):
        # Snapshot the journal mid-flight, before the entry is cleared.
        (entry,) = azure.deployments.values()
        captured_states.append(
            entry["properties"]["outputs"]["journalState"]["value"]
        )
        return {"Status": "Added"}

    monkeypatch.setattr(instance, "add_scope", add_scope)
    monkeypatch.setattr(
        instance,
        "_target_member_ids",
        lambda kind, scope_id: [existing["MemberSubscriptionId"]],
    )

    instance.execute("add-subscription", subscription_id=TARGET_SUBSCRIPTION)

    assert len(captured_states) == 1
    assert captured_states[0]["State"] == "Started"
    expected_fingerprint = manage_alert_scopes.membership_fingerprint(
        "subscription", TARGET_SUBSCRIPTION, [existing["MemberSubscriptionId"]]
    )
    assert captured_states[0]["Fingerprint"] == expected_fingerprint


def reviewed_manager(monkeypatch, *, clock):
    azure = LockingFakeAzure(
        lambda args: (
            {
                "id": CENTRAL_SUBSCRIPTION,
                "tenantId": TENANT,
                "user": {"name": "operator@example.com"},
            }
            if args[:2] == ("account", "show")
            else None
        )
    )
    instance = locking_manager(azure, scopes=[])
    instance.enforce_review_gate = True
    instance.clock = clock
    monkeypatch.setattr(
        instance,
        "add_scope",
        lambda *_args, **_kwargs: {"Status": "Added"},
    )
    return instance


def test_reviewed_fingerprint_allows_exact_unexpired_execution(monkeypatch):
    instance = reviewed_manager(monkeypatch, clock=lambda: 1000.0)
    plan = instance.execute(
        "add-subscription",
        subscription_id=TARGET_SUBSCRIPTION,
        what_if=True,
    )
    instance.approved_execution_fingerprint = plan[
        "ExecutionFingerprint"
    ]

    assert instance.execute(
        "add-subscription", subscription_id=TARGET_SUBSCRIPTION
    ) == {"Status": "Added"}


def test_reviewed_fingerprint_expiry_blocks_mutation(monkeypatch):
    now = [1000.0]
    instance = reviewed_manager(monkeypatch, clock=lambda: now[0])
    plan = instance.execute(
        "add-subscription",
        subscription_id=TARGET_SUBSCRIPTION,
        what_if=True,
    )
    instance.approved_execution_fingerprint = plan[
        "ExecutionFingerprint"
    ]
    now[0] = float(plan["ExecutionExpiresAt"] + 1)

    with pytest.raises(ScopeManagerError, match="expired"):
        instance.execute(
            "add-subscription", subscription_id=TARGET_SUBSCRIPTION
        )


def test_reviewed_fingerprint_binds_command_parameters(monkeypatch):
    instance = reviewed_manager(monkeypatch, clock=lambda: 1000.0)
    plan = instance.execute(
        "add-subscription",
        subscription_id=TARGET_SUBSCRIPTION,
        what_if=True,
    )
    instance.approved_execution_fingerprint = plan[
        "ExecutionFingerprint"
    ]

    with pytest.raises(ScopeManagerError, match="does not match"):
        instance.execute(
            "add-subscription", subscription_id="different-subscription"
        )


def test_reviewed_fingerprint_detects_management_group_drift(monkeypatch):
    instance = reviewed_manager(monkeypatch, clock=lambda: 1000.0)
    coverage = {
        "ManagementGroupId": GROUP_ID,
        "TenantId": TENANT,
        "SubscriptionIds": [TARGET_SUBSCRIPTION],
        "DescendantManagementGroupIds": [],
    }
    monkeypatch.setattr(
        instance,
        "get_management_group_coverage",
        lambda _scope_id: deepcopy(coverage),
    )
    monkeypatch.setattr(
        instance,
        "_mutating_dispatch",
        lambda *_args, **_kwargs: {"Status": "Planned"},
    )
    plan = instance.execute(
        "add-management-group",
        management_group_id=GROUP_ID,
        what_if=True,
    )
    instance.approved_execution_fingerprint = plan[
        "ExecutionFingerprint"
    ]
    coverage["SubscriptionIds"].append("new-descendant")

    with pytest.raises(ScopeManagerError, match="does not match"):
        instance.execute(
            "add-management-group", management_group_id=GROUP_ID
        )


def test_new_management_group_drift_guard_uses_live_descendants(
    monkeypatch,
):
    instance = manager(scopes=[])
    coverage = {
        "SubscriptionIds": [TARGET_SUBSCRIPTION, "second-sub"],
    }
    monkeypatch.setattr(
        instance,
        "get_management_group_coverage",
        lambda _scope_id: coverage,
    )
    expected = manage_alert_scopes.membership_fingerprint(
        "managementGroup",
        GROUP_ID,
        coverage["SubscriptionIds"],
    )
    instance.operation_target = (
        "managementGroup",
        GROUP_ID,
        expected,
    )

    instance.assert_operation_membership_unchanged()


def test_reviewed_fingerprint_detects_managed_scope_drift(monkeypatch):
    instance = reviewed_manager(monkeypatch, clock=lambda: 1000.0)
    plan = instance.execute(
        "add-subscription",
        subscription_id=TARGET_SUBSCRIPTION,
        what_if=True,
    )
    instance.approved_execution_fingerprint = plan[
        "ExecutionFingerprint"
    ]
    instance.scopes = [scope_member()]

    with pytest.raises(ScopeManagerError, match="does not match"):
        instance.execute(
            "add-subscription", subscription_id=TARGET_SUBSCRIPTION
        )


def test_review_context_rechecks_managed_scope_before_each_mutation(
    monkeypatch,
):
    instance = reviewed_manager(monkeypatch, clock=lambda: 1000.0)
    expires_at = 1500
    payload = instance._review_payload(
        "add-subscription",
        TARGET_SUBSCRIPTION,
        None,
        expires_at,
    )
    instance._review_context = {
        "expiresAt": expires_at,
        "target": payload["target"],
        "command": payload["command"],
        "managementGroup": None,
        "managedScopes": payload["managedScopes"],
        "artifacts": payload["artifacts"],
    }
    current_scopes = []
    monkeypatch.setattr(
        instance,
        "get_managed_scopes",
        lambda: deepcopy(current_scopes),
    )

    instance.assert_operation_membership_unchanged()
    current_scopes.append(scope_member())

    with pytest.raises(ScopeManagerError, match="changed after review"):
        instance.assert_operation_membership_unchanged()


def test_post_mutation_transition_rejects_unrelated_scope_drift(
    monkeypatch,
):
    target = scope_member(enabled=False)
    unrelated = scope_member(
        subscription_id="unrelated-subscription",
        enabled=True,
    )
    instance = manager(scopes=[target, unrelated])
    instance._review_context = {
        "managedScopes": instance._managed_scope_snapshot(
            [target, unrelated]
        )
    }
    expected_target = {**target, "Enabled": True}
    drifted_unrelated = {**unrelated, "Enabled": False}
    monkeypatch.setattr(
        instance,
        "get_managed_scopes",
        lambda: [expected_target, drifted_unrelated],
    )

    with pytest.raises(ScopeManagerError, match="exact intended change"):
        instance._accept_expected_managed_scope_transition(
            expected_target
        )


def test_compensating_restore_bypasses_drift_but_revalidates_lock():
    original = scope_member(enabled=False)
    renewals = []

    class FakeLock:
        def renew(self, handle):
            renewals.append(handle)

    def handler(args):
        if args[:2] == ("resource", "update"):
            return {"properties": {"enabled": True}}
        raise AssertionError(f"Unexpected call: {args}")

    instance = manager(FakeAzure(handler), scopes=[original])
    instance._review_context = {
        "expiresAt": 0,
        "managedScopes": [],
    }
    handle = object()
    instance.operation_lock_context = (FakeLock(), handle)

    instance.set_alert_enabled(
        original, True, compensating_restore=True
    )

    assert renewals == [handle]
    assert original["Enabled"] is True


def test_lock_release_failure_retains_blocking_journal(monkeypatch):
    azure = LockingFakeAzure(
        lambda _args: {"user": {"name": "operator@example.com"}}
    )
    monkeypatch.setattr(
        FakeBlobClient,
        "delete_blob",
        lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                FakeBlobError("release failed", 500)
            )
        ),
    )
    instance = locking_manager(azure, scopes=[])
    monkeypatch.setattr(
        instance,
        "add_scope",
        lambda *_args, **_kwargs: {"Status": "Added"},
    )

    with pytest.raises(
        manage_alert_scopes.OperationLockError,
        match="release failed",
    ):
        instance.execute(
            "add-subscription", subscription_id=TARGET_SUBSCRIPTION
        )

    (entry,) = azure.deployments.values()
    state = entry["properties"]["outputs"]["journalState"]["value"]
    assert state["State"] == "CompletedLockReleaseFailed"
    assert DEFAULT_LOCK_NAME in azure.locks
