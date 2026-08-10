#!/usr/bin/env python3
"""Safely manage peripheral Azure Service Health alert scopes."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote


WORKLOAD_TAG = "azure-service-health-slack-bot"
MANAGER_TAG = "manage-alert-scopes"
ALERT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "infra"
    / "day2"
    / "service-health-alert-scope.bicep"
)
READ_RETRY_MARKERS = (
    "429",
    "TooManyRequests",
    "throttl",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
)


class ScopeManagerError(RuntimeError):
    """A fail-closed operational error."""


class AzureCli:
    """JSON-only Azure CLI boundary with bounded retries for read operations."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        max_read_attempts: int = 3,
    ) -> None:
        self.runner = runner
        self.sleep = sleep
        self.max_read_attempts = max_read_attempts
        self.executable = (
            shutil.which("az") if runner is subprocess.run else "az"
        )

    @staticmethod
    def _is_read(arguments: list[str]) -> bool:
        if not arguments:
            return False
        if arguments[0] in {"account", "group"}:
            return len(arguments) > 1 and arguments[1] in {"show", "list"}
        if arguments[0] == "resource":
            return len(arguments) > 1 and arguments[1] in {"show", "list"}
        if arguments[:3] in (
            ["monitor", "action-group", "list"],
            ["monitor", "activity-log", "alert"],
        ):
            return "list" in arguments[:5]
        return (
            arguments[0] == "rest"
            and "--method" in arguments
            and arguments[arguments.index("--method") + 1].lower() == "get"
        )

    def invoke(self, *arguments: str) -> Any:
        if self.executable is None:
            raise ScopeManagerError(
                "Azure CLI is required. Install it, run 'az login', and retry."
            )
        args = list(arguments)
        attempts = self.max_read_attempts if self._is_read(args) else 1
        command = [
            self.executable,
            *args,
            "--only-show-errors",
            "--output",
            "json",
        ]
        for attempt in range(1, attempts + 1):
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                text = result.stdout.strip()
                if not text:
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ScopeManagerError(
                        f"Azure CLI returned invalid JSON for: az {' '.join(args)}"
                    ) from exc
            detail = "\n".join(
                value.strip() for value in (result.stdout, result.stderr) if value.strip()
            )
            retryable = any(marker.lower() in detail.lower() for marker in READ_RETRY_MARKERS)
            if not retryable or attempt == attempts:
                raise ScopeManagerError(
                    f"Azure CLI command failed: az {' '.join(args)}\n{detail}"
                )
            self.sleep(float(2 ** (attempt - 1)))
        raise AssertionError("unreachable")


def member(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else default


def nested(value: Any, *path: str) -> Any:
    for segment in path:
        value = member(value, segment)
        if value is None:
            return None
    return value


def azure_property(value: Any, name: str) -> Any:
    direct = member(value, name)
    if direct is not None:
        return direct
    return member(member(value, "properties", {}), name)


def tag(value: Any, name: str) -> Any:
    return member(member(value, "tags", {}), name)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalized_id(value: Any) -> str:
    return str(value).casefold()


def same_id(left: Any, right: Any) -> bool:
    return normalized_id(left) == normalized_id(right)


def contains_id(values: Iterable[Any], target: Any) -> bool:
    normalized_target = normalized_id(target)
    return any(normalized_id(value) == normalized_target for value in values)


def id_set(values: Iterable[Any]) -> set[str]:
    return {normalized_id(value) for value in values}


def unique_ids(values: Iterable[Any]) -> list[str]:
    by_normalized = {
        normalized_id(value): str(value)
        for value in values
        if str(value)
    }
    return sorted(by_normalized.values(), key=normalized_id)


def resource_coordinates(resource_id: str) -> tuple[str, str, str]:
    match = re.match(
        r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/.+/([^/]+)$",
        resource_id,
        re.IGNORECASE,
    )
    if not match:
        raise ScopeManagerError(f"Unsupported Azure resource ID: {resource_id}")
    return match.group(1), match.group(2), match.group(3)


def resource_suffix(scope_kind: str, scope_id: str) -> str:
    digest = hashlib.sha256(
        f"{scope_kind}|{scope_id.lower()}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{'sub' if scope_kind == 'subscription' else 'mg'}-{digest}"


def is_unreadable_subscription_error(error: Exception) -> bool:
    text = str(error)
    patterns = (
        r"\bAuthorizationFailed\b",
        r"\bSubscriptionNotFound\b",
        r"does not have authorization to perform action '[^']*resourceGroups/read'",
        r"The subscription '[^']*' could not be found",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def is_active(scope: dict[str, Any]) -> bool:
    return bool(scope.get("Enabled")) and bool(scope.get("ActionGroupEnabled"))


class ScopeManager:
    """Business logic for tenant-bound day-2 scope management."""

    def __init__(
        self,
        azure: AzureCli,
        environment_name: str | None = None,
        should_process: Callable[[str, str], bool] | None = None,
        confirm_destructive: Callable[[str], bool] | None = None,
        warning: Callable[[str], None] | None = None,
    ) -> None:
        self.azure = azure
        self.environment_name = environment_name
        self.should_process = should_process or (lambda _target, _operation: True)
        self.confirm_destructive = confirm_destructive or (lambda _question: False)
        self.warning = warning or (lambda text: print(f"WARNING: {text}", file=sys.stderr))
        self.central: dict[str, Any] = {}
        self.scopes: list[dict[str, Any]] = []
        self.management_group_cache: dict[str, dict[str, Any]] = {}

    def initialize(self) -> None:
        self.central = self.get_central_deployment()
        self.scopes = self.get_managed_scopes()

    def get_central_deployment(self) -> dict[str, Any]:
        current = self.azure.invoke("account", "show")
        current_tenant = str(member(current, "tenantId", ""))
        if not current_tenant:
            raise ScopeManagerError(
                "The active Azure CLI context has no tenant. Run 'az login' for the central deployment tenant."
            )
        accounts = [
            item
            for item in as_list(self.azure.invoke("account", "list"))
            if member(item, "state") == "Enabled"
            and same_id(member(item, "tenantId"), current_tenant)
        ]
        if not accounts:
            raise ScopeManagerError(
                "No enabled Azure subscriptions are available. Run 'az login' with access to the central deployment."
            )

        candidates: list[dict[str, Any]] = []
        for account in accounts:
            account_id = str(member(account, "id", ""))
            try:
                groups = as_list(
                    self.azure.invoke(
                        "group",
                        "list",
                        "--subscription",
                        account_id,
                        "--tag",
                        f"workload={WORKLOAD_TAG}",
                    )
                )
            except ScopeManagerError as exc:
                if not is_unreadable_subscription_error(exc):
                    raise
                self.warning(
                    f"Skipping subscription '{account_id}' during central deployment discovery: "
                    "resource groups are not readable (stale or inaccessible cached subscription)."
                )
                continue
            for group in groups:
                environment = str(tag(group, "azd-env-name") or "")
                if not environment or (
                    self.environment_name
                    and not same_id(environment, self.environment_name)
                ):
                    continue
                group_name = str(member(group, "name", ""))
                apps = as_list(
                    self.azure.invoke(
                        "resource",
                        "list",
                        "--subscription",
                        account_id,
                        "--resource-group",
                        group_name,
                        "--resource-type",
                        "Microsoft.App/containerApps",
                    )
                )
                app = [
                    item
                    for item in apps
                    if same_id(member(item, "name"), f"ca-{environment}")
                ]
                if len(app) != 1:
                    continue
                action_groups = as_list(
                    self.azure.invoke(
                        "monitor",
                        "action-group",
                        "list",
                        "--subscription",
                        account_id,
                        "--resource-group",
                        group_name,
                    )
                )
                anchor = [
                    item
                    for item in action_groups
                    if same_id(
                        member(item, "name"),
                        f"ag-{environment}-service-health",
                    )
                ]
                if len(anchor) != 1:
                    continue
                baseline_alerts = as_list(
                    self.azure.invoke(
                        "monitor",
                        "activity-log",
                        "alert",
                        "list",
                        "--subscription",
                        account_id,
                        "--resource-group",
                        group_name,
                    )
                )
                anchor_id = str(member(anchor[0], "id", ""))
                baseline = []
                for alert in baseline_alerts:
                    actions = as_list(
                        nested(alert, "actions", "actionGroups")
                        or nested(alert, "properties", "actions", "actionGroups")
                    )
                    if any(
                        same_id(member(item, "actionGroupId"), anchor_id)
                        for item in actions
                    ):
                        baseline.append(alert)
                if not baseline:
                    continue
                if len(baseline) != 1:
                    raise ScopeManagerError(
                        f"Central deployment '{environment}' has multiple baseline alerts bound "
                        f"to its anchor Action Group '{anchor_id}'."
                    )
                baseline_actions = as_list(
                    nested(baseline[0], "actions", "actionGroups")
                    or nested(baseline[0], "properties", "actions", "actionGroups")
                )
                baseline_scopes = as_list(azure_property(baseline[0], "scopes"))
                if (
                    len(baseline_actions) != 1
                    or not same_id(
                        member(baseline_actions[0], "actionGroupId"), anchor_id
                    )
                    or len(baseline_scopes) != 1
                ):
                    raise ScopeManagerError(
                        f"Central deployment '{environment}' has inconsistent baseline alert metadata."
                    )
                scope_resource_id = str(baseline_scopes[0])
                subscription_match = re.match(
                    r"^/subscriptions/([^/]+)$", scope_resource_id, re.IGNORECASE
                )
                group_match = re.match(
                    r"^/providers/Microsoft.Management/managementGroups/([^/]+)$",
                    scope_resource_id,
                    re.IGNORECASE,
                )
                if subscription_match:
                    protected_kind, protected_id = "subscription", subscription_match.group(1)
                elif group_match:
                    protected_kind, protected_id = "managementGroup", group_match.group(1)
                else:
                    raise ScopeManagerError(
                        f"Central deployment '{environment}' has unsupported baseline alert scope "
                        f"'{scope_resource_id}'."
                    )
                receivers = [
                    item
                    for item in as_list(azure_property(anchor[0], "webhookReceivers"))
                    if member(item, "useAadAuth") is True
                    and str(member(item, "serviceUri", "")).rstrip("/").endswith(
                        "/api/service-health"
                    )
                ]
                if len(receivers) != 1:
                    continue
                app_id = str(member(app[0], "id", ""))
                details = self.azure.invoke(
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"{app_id}?api-version=2024-03-01",
                    "--subscription",
                    account_id,
                )
                fqdn = str(
                    nested(details, "properties", "configuration", "ingress", "fqdn")
                    or ""
                )
                webhook_uri = str(member(receivers[0], "serviceUri", "")).rstrip("/")
                if not fqdn or webhook_uri != f"https://{fqdn}/api/service-health":
                    raise ScopeManagerError(
                        f"Central deployment '{environment}' has inconsistent Container App "
                        "and Action Group webhook metadata."
                    )
                auth = self.azure.invoke(
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"{app_id}/authConfigs/current?api-version=2024-03-01",
                    "--subscription",
                    account_id,
                )
                client_id = str(
                    nested(
                        auth,
                        "properties",
                        "identityProviders",
                        "azureActiveDirectory",
                        "registration",
                        "clientId",
                    )
                    or ""
                )
                tenant_id = str(member(receivers[0], "tenantId", ""))
                if (
                    not client_id
                    or not tenant_id
                    or not same_id(tenant_id, member(account, "tenantId"))
                ):
                    raise ScopeManagerError(
                        f"Central deployment '{environment}' has incomplete or cross-tenant "
                        "Secure Webhook metadata."
                    )
                candidates.append(
                    {
                        "EnvironmentName": environment,
                        "TenantId": tenant_id,
                        "SubscriptionId": account_id,
                        "ResourceGroup": group_name,
                        "Location": str(member(group, "location", "")),
                        "ContainerAppId": app_id,
                        "WebhookUri": webhook_uri,
                        "SecureWebhookClientId": client_id,
                        "SecureWebhookObjectId": str(member(receivers[0], "objectId", "")),
                        "SecureWebhookIdentifierUri": str(
                            member(receivers[0], "identifierUri", "")
                        ),
                        "AnchorActionGroupId": anchor_id,
                        "ProtectedAlertId": str(member(baseline[0], "id", "")),
                        "ProtectedScopeKind": protected_kind,
                        "ProtectedScopeId": protected_id,
                        "ProtectedScopeResourceId": scope_resource_id,
                        "Accounts": accounts,
                    }
                )
        if not candidates:
            suffix = (
                f" for environment '{self.environment_name}'"
                if self.environment_name
                else ""
            )
            raise ScopeManagerError(
                f"No central Azure Service Health Slack Bot deployment was discovered{suffix}. "
                "Verify Reader access and deployment tags."
            )
        if len(candidates) > 1:
            names = ", ".join(
                f"{item['EnvironmentName']} [{item['SubscriptionId']}]"
                for item in candidates
            )
            raise ScopeManagerError(
                f"Multiple central deployments were discovered ({names}). Specify --environment-name."
            )
        central = candidates[0]
        for required in (
            "SecureWebhookObjectId",
            "SecureWebhookIdentifierUri",
            "SecureWebhookClientId",
            "WebhookUri",
            "ProtectedAlertId",
            "ProtectedScopeKind",
            "ProtectedScopeId",
        ):
            if not str(central.get(required, "")).strip():
                raise ScopeManagerError(
                    f"Central deployment discovery could not prove '{required}'. No changes were made."
                )
        return central

    def same_webhook_receiver(self, action_group: dict[str, Any]) -> bool:
        for receiver in as_list(azure_property(action_group, "webhookReceivers")):
            if (
                str(member(receiver, "serviceUri", "")).rstrip("/")
                == self.central["WebhookUri"]
                and same_id(member(receiver, "tenantId"), self.central["TenantId"])
                and same_id(
                    member(receiver, "objectId"),
                    self.central["SecureWebhookObjectId"],
                )
                and same_id(
                    member(receiver, "identifierUri"),
                    self.central["SecureWebhookIdentifierUri"],
                )
                and member(receiver, "useAadAuth") is True
                and member(receiver, "useCommonAlertSchema") is True
            ):
                return True
        return False

    def assert_action_group_ownership(
        self,
        action_group: dict[str, Any],
        scope_member: dict[str, Any],
    ) -> None:
        action_group_id = str(member(action_group, "id", ""))
        if action_group_id and not same_id(
            action_group_id, scope_member["ActionGroupId"]
        ):
            raise ScopeManagerError(
                f"Action Group '{scope_member['ActionGroupId']}' returned an inconsistent resource ID."
            )
        expected_tags = {
            "workload": WORKLOAD_TAG,
            "azd-env-name": self.central["EnvironmentName"],
            "service-health-managed-by": MANAGER_TAG,
            "service-health-central-subscription": self.central["SubscriptionId"],
            "service-health-scope-kind": scope_member["ScopeKind"],
            "service-health-scope-id": scope_member["ScopeId"],
            "service-health-member-subscription": scope_member[
                "MemberSubscriptionId"
            ],
        }
        inconsistent = [
            name
            for name, expected in expected_tags.items()
            if not same_id(tag(action_group, name), expected)
        ]
        if inconsistent:
            raise ScopeManagerError(
                f"Action Group '{scope_member['ActionGroupId']}' has inconsistent manager "
                f"ownership metadata: {', '.join(inconsistent)}."
            )

    def get_managed_scopes(self) -> list[dict[str, Any]]:
        scopes: list[dict[str, Any]] = []
        referenced_action_groups: set[str] = set()
        managed_action_groups: list[dict[str, Any]] = []
        accounts = [
            item
            for item in self.central["Accounts"]
            if same_id(member(item, "tenantId"), self.central["TenantId"])
            and member(item, "state") == "Enabled"
        ]
        for account in accounts:
            account_id = str(member(account, "id", ""))
            alerts = as_list(
                self.azure.invoke(
                    "monitor", "activity-log", "alert", "list", "--subscription", account_id
                )
            )
            for alert in alerts:
                if (
                    not same_id(tag(alert, "workload"), WORKLOAD_TAG)
                    or not same_id(
                        tag(alert, "azd-env-name"),
                        self.central["EnvironmentName"],
                    )
                    or not same_id(
                        tag(alert, "service-health-managed-by"), MANAGER_TAG
                    )
                ):
                    continue
                alert_id = str(member(alert, "id", ""))
                if same_id(alert_id, self.central["ProtectedAlertId"]):
                    continue
                alert_scopes = as_list(azure_property(alert, "scopes"))
                if len(alert_scopes) != 1:
                    raise ScopeManagerError(
                        f"Alert '{alert_id}' has an ambiguous scope configuration."
                    )
                actions = as_list(
                    nested(alert, "actions", "actionGroups")
                    or nested(alert, "properties", "actions", "actionGroups")
                )
                if len(actions) != 1:
                    raise ScopeManagerError(
                        f"Alert '{alert_id}' does not have exactly one Action Group."
                    )
                action_group_id = str(member(actions[0], "actionGroupId", ""))
                if same_id(
                    action_group_id, self.central["AnchorActionGroupId"]
                ):
                    continue
                action_group = self.azure.invoke(
                    "resource",
                    "show",
                    "--ids",
                    action_group_id,
                    "--api-version",
                    "2023-01-01",
                )
                if not self.same_webhook_receiver(action_group):
                    raise ScopeManagerError(
                        f"Managed Action Group '{action_group_id}' does not match the central "
                        "signed Common Alert Schema receiver."
                    )
                all_of = as_list(member(azure_property(alert, "condition"), "allOf"))
                conditions = [
                    item
                    for item in all_of
                    if member(item, "field") == "category"
                    and member(item, "equals") == "ServiceHealth"
                ]
                if len(all_of) != 1 or len(conditions) != 1:
                    raise ScopeManagerError(
                        f"Alert '{alert_id}' is not an unrestricted Service Health category rule."
                    )
                scope_resource_id = str(alert_scopes[0])
                match = re.match(
                    r"^/subscriptions/([^/]+)$", scope_resource_id, re.IGNORECASE
                )
                if not match:
                    raise ScopeManagerError(
                        f"Alert '{alert_id}' must use a subscription scope. Azure Activity Log "
                        "Alerts do not support Management Group descendant fan-out natively."
                    )
                member_subscription_id = match.group(1)
                kind = str(tag(alert, "service-health-scope-kind") or "")
                scope_id = str(tag(alert, "service-health-scope-id") or "")
                tagged_member = str(
                    tag(alert, "service-health-member-subscription") or ""
                )
                if (
                    kind not in ("subscription", "managementGroup")
                    or not scope_id
                    or (
                        tagged_member
                        and not same_id(tagged_member, member_subscription_id)
                    )
                    or (
                        kind == "subscription"
                        and not same_id(scope_id, member_subscription_id)
                    )
                ):
                    raise ScopeManagerError(
                        f"Alert '{alert_id}' has inconsistent day-2 ownership metadata."
                    )
                scope_member = {
                    "ScopeKind": kind,
                    "ScopeId": scope_id,
                    "ScopeResourceId": scope_resource_id,
                    "AlertId": alert_id,
                    "ActionGroupId": action_group_id,
                    "Enabled": bool(azure_property(alert, "enabled")),
                    "ActionGroupEnabled": bool(
                        azure_property(action_group, "enabled")
                    ),
                    "TenantId": self.central["TenantId"],
                    "ManagedBy": str(
                        tag(alert, "service-health-managed-by") or ""
                    ),
                    "MemberSubscriptionId": member_subscription_id,
                    "OrphanedActionGroup": False,
                }
                self.assert_action_group_ownership(
                    action_group, scope_member
                )
                scopes.append(scope_member)
                referenced_action_groups.add(normalized_id(action_group_id))
            for action_group in as_list(
                self.azure.invoke(
                    "monitor", "action-group", "list", "--subscription", account_id
                )
            ):
                if (
                    same_id(tag(action_group, "workload"), WORKLOAD_TAG)
                    and same_id(
                        tag(action_group, "azd-env-name"),
                        self.central["EnvironmentName"],
                    )
                    and same_id(
                        tag(action_group, "service-health-managed-by"),
                        MANAGER_TAG,
                    )
                ):
                    managed_action_groups.append(action_group)
        for action_group in managed_action_groups:
            action_group_id = str(member(action_group, "id", ""))
            if (
                same_id(action_group_id, self.central["AnchorActionGroupId"])
                or normalized_id(action_group_id) in referenced_action_groups
            ):
                continue
            kind = str(tag(action_group, "service-health-scope-kind") or "")
            scope_id = str(tag(action_group, "service-health-scope-id") or "")
            member_subscription_id = str(
                tag(action_group, "service-health-member-subscription") or ""
            )
            match = re.match(
                r"^/subscriptions/([^/]+)/", action_group_id, re.IGNORECASE
            )
            if not member_subscription_id and match:
                member_subscription_id = match.group(1)
            if (
                kind not in ("subscription", "managementGroup")
                or not scope_id
                or (
                    kind == "subscription"
                    and not same_id(scope_id, member_subscription_id)
                )
                or not re.match(
                    rf"^/subscriptions/{re.escape(member_subscription_id)}/",
                    action_group_id,
                    re.IGNORECASE,
                )
            ):
                raise ScopeManagerError(
                    f"Orphaned manager-owned Action Group '{action_group_id}' has inconsistent "
                    "ownership metadata."
                )
            scopes.append(
                {
                    "ScopeKind": kind,
                    "ScopeId": scope_id,
                    "ScopeResourceId": f"/subscriptions/{member_subscription_id}",
                    "AlertId": None,
                    "ActionGroupId": action_group_id,
                    "Enabled": False,
                    "ActionGroupEnabled": bool(
                        azure_property(action_group, "enabled")
                    ),
                    "TenantId": self.central["TenantId"],
                    "ManagedBy": MANAGER_TAG,
                    "MemberSubscriptionId": member_subscription_id,
                    "OrphanedActionGroup": True,
                }
            )
        result = [item for item in scopes if item["ScopeKind"] == "subscription"]
        group_ids = unique_ids(
            item["ScopeId"]
            for item in scopes
            if item["ScopeKind"] == "managementGroup"
        )
        for group_id in group_ids:
            members = [
                item
                for item in scopes
                if item["ScopeKind"] == "managementGroup"
                and same_id(item["ScopeId"], group_id)
            ]
            operational = [
                item for item in members if not item.get("OrphanedActionGroup")
            ]
            result.append(
                self.new_management_group_state(
                    group_id, members, operational_members=operational
                )
            )
        return result

    def get_management_group_coverage(self, group_id: str) -> dict[str, Any]:
        key = group_id.lower()
        if key in self.management_group_cache:
            return self.management_group_cache[key]
        encoded = quote(group_id, safe="")
        group = self.azure.invoke(
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com/providers/Microsoft.Management/managementGroups/{encoded}?api-version=2021-04-01",
            "--subscription",
            self.central["SubscriptionId"],
        )
        tenant_id = str(azure_property(group, "tenantId") or "")
        if not tenant_id or not same_id(tenant_id, self.central["TenantId"]):
            raise ScopeManagerError(
                f"Management Group '{group_id}' is not proven to belong to central tenant "
                f"'{self.central['TenantId']}'."
            )
        subscription_ids: dict[str, str] = {}
        management_group_ids: dict[str, str] = {}
        next_url: str | None = (
            "https://management.azure.com/providers/Microsoft.Management/"
            f"managementGroups/{encoded}/descendants?api-version=2020-05-01"
        )
        while next_url:
            descendants = self.azure.invoke(
                "rest",
                "--method",
                "get",
                "--url",
                next_url,
                "--subscription",
                self.central["SubscriptionId"],
            )
            for item in as_list(member(descendants, "value")):
                item_type = str(member(item, "type", ""))
                if re.search(r"/managementGroups$", item_type, re.IGNORECASE):
                    descendant_id = str(member(item, "name", ""))
                    if descendant_id and not same_id(descendant_id, group_id):
                        management_group_ids[normalized_id(descendant_id)] = descendant_id
                elif re.search(r"/subscriptions$", item_type, re.IGNORECASE):
                    item_id = str(member(item, "name", ""))
                    if not item_id:
                        match = re.search(
                            r"/subscriptions/([^/]+)$",
                            str(member(item, "id", "")),
                            re.IGNORECASE,
                        )
                        item_id = match.group(1) if match else ""
                    if item_id:
                        subscription_ids[normalized_id(item_id)] = item_id
            next_url = str(member(descendants, "nextLink", "") or "") or None
        known_ids = {
            str(member(item, "id", ""))
            for item in self.central["Accounts"]
            if same_id(member(item, "tenantId"), self.central["TenantId"])
            and member(item, "state") == "Enabled"
        }
        known_ids = id_set(known_ids)
        inaccessible = sorted(
            (
                original
                for normalized, original in subscription_ids.items()
                if normalized not in known_ids
            ),
            key=normalized_id,
        )
        if inaccessible:
            raise ScopeManagerError(
                f"Coverage for Management Group '{group_id}' cannot be proven. These descendant "
                f"subscriptions are not accessible: {', '.join(inaccessible)}."
            )
        coverage = {
            "ManagementGroupId": group_id,
            "TenantId": tenant_id,
            "SubscriptionIds": sorted(
                subscription_ids.values(), key=normalized_id
            ),
            "DescendantManagementGroupIds": sorted(
                management_group_ids.values(), key=normalized_id
            ),
        }
        self.management_group_cache[key] = coverage
        return coverage

    def assert_permissions(
        self, scope: str, subscription_id: str, operations: Iterable[str]
    ) -> None:
        permissions = self.azure.invoke(
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{scope}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01",
            "--subscription",
            subscription_id,
        )
        permission_sets = as_list(member(permissions, "value"))
        if not permission_sets:
            raise ScopeManagerError(
                f"Azure returned no effective permissions for '{scope}'. No changes were made."
            )
        missing: list[str] = []
        for operation in operations:
            allowed = False
            for permission_set in permission_sets:
                included = any(
                    fnmatch.fnmatchcase(operation.lower(), str(pattern).lower())
                    for pattern in as_list(member(permission_set, "actions"))
                )
                excluded = any(
                    fnmatch.fnmatchcase(operation.lower(), str(pattern).lower())
                    for pattern in as_list(member(permission_set, "notActions"))
                )
                if included and not excluded:
                    allowed = True
                    break
            if not allowed:
                missing.append(operation)
        if missing:
            raise ScopeManagerError(
                f"Missing Azure permissions at '{scope}': {', '.join(missing)}. Assign "
                "Contributor for the target subscription resource group and Monitoring "
                "Contributor for Management Group alert scope, then retry."
            )

    def assert_add_permissions(
        self, deployment_subscription_id: str, management_group_id: str | None = None
    ) -> None:
        self.assert_permissions(
            f"/subscriptions/{deployment_subscription_id}",
            deployment_subscription_id,
            (
                "Microsoft.Resources/subscriptions/resourceGroups/write",
                "Microsoft.Resources/deployments/write",
                "Microsoft.Insights/actionGroups/write",
                "Microsoft.Insights/activityLogAlerts/write",
                "Microsoft.Insights/CreateNotifications/Write",
                "Microsoft.Insights/NotificationStatus/Read",
            ),
        )
        if management_group_id:
            self.assert_permissions(
                f"/providers/Microsoft.Management/managementGroups/{management_group_id}",
                self.central["SubscriptionId"],
                ("Microsoft.Insights/activityLogAlerts/write",),
            )

    @staticmethod
    def scope_members(
        scope: dict[str, Any], include_orphans: bool = False
    ) -> list[dict[str, Any]]:
        members = [item for item in as_list(scope.get("Members")) if item is not None]
        if not members:
            members = [scope]
        if not include_orphans:
            members = [
                item
                for item in members
                if not item.get("OrphanedActionGroup") and item.get("AlertId")
            ]
        return members

    def membership_state(
        self, scope: dict[str, Any], coverage: dict[str, Any]
    ) -> dict[str, Any]:
        members = self.scope_members(scope, include_orphans=True)
        operational = self.scope_members(scope)
        member_ids = [str(item.get("MemberSubscriptionId") or "") for item in operational]
        unique = unique_ids(member_ids)
        expected = unique_ids(coverage["SubscriptionIds"])
        unique_normalized = id_set(unique)
        expected_normalized = id_set(expected)
        orphaned = unique_ids(
            str(item.get("MemberSubscriptionId") or "")
            for item in members
            if item.get("OrphanedActionGroup") or not item.get("AlertId")
        )
        return {
            "Complete": (
                all(member_ids)
                and len(member_ids) == len(unique)
                and unique_normalized == expected_normalized
            ),
            "MemberIds": unique,
            "ExpectedIds": expected,
            "MissingIds": [
                item for item in expected if not contains_id(unique, item)
            ],
            "UnexpectedIds": [
                item for item in unique if not contains_id(expected, item)
            ],
            "HasDuplicates": len(member_ids) != len(unique_normalized),
            "OrphanedIds": orphaned,
            "RepairIds": sorted(set(expected) - set(unique)),
        }

    def assert_membership_complete(
        self,
        scope: dict[str, Any],
        coverage: dict[str, Any],
        operation: str = "use Management Group coverage",
    ) -> dict[str, Any]:
        state = self.membership_state(scope, coverage)
        if state["Complete"]:
            return state
        details = []
        if state["MissingIds"]:
            details.append(f"missing: {', '.join(state['MissingIds'])}")
        if state["UnexpectedIds"]:
            details.append(f"unexpected: {', '.join(state['UnexpectedIds'])}")
        if state["HasDuplicates"]:
            details.append("duplicate or blank member IDs")
        if state["OrphanedIds"]:
            details.append(
                f"orphaned Action Groups: {', '.join(state['OrphanedIds'])}"
            )
        raise ScopeManagerError(
            f"Cannot {operation} because Management Group '{scope['ScopeId']}' does not have "
            f"an exact alert member for every current descendant ({'; '.join(details)})."
        )

    def assert_remove_permissions(self, scope: dict[str, Any]) -> None:
        for item in self.scope_members(scope, include_orphans=True):
            resource_id = item.get("AlertId") or item["ActionGroupId"]
            subscription_id, resource_group, _ = resource_coordinates(resource_id)
            operations = ["Microsoft.Insights/actionGroups/delete"]
            if item.get("AlertId"):
                operations.append("Microsoft.Insights/activityLogAlerts/delete")
            self.assert_permissions(
                f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
                subscription_id,
                operations,
            )

    def official_webhook_test(self, scope: dict[str, Any]) -> str:
        for item in self.scope_members(scope):
            subscription_id, resource_group, name = resource_coordinates(
                item["ActionGroupId"]
            )
            result = self.azure.invoke(
                "monitor",
                "action-group",
                "test-notifications",
                "create",
                "--subscription",
                subscription_id,
                "--resource-group",
                resource_group,
                "--action-group-name",
                name,
                "--alert-type",
                "servicehealth",
                "--add-action",
                "webhook",
                "slack-service-health",
                self.central["WebhookUri"],
                "useaadauth",
                self.central["SecureWebhookObjectId"],
                self.central["SecureWebhookIdentifierUri"],
                "usecommonalertschema",
            )
            state = str(azure_property(result, "state") or "")
            if state != "Complete":
                raise ScopeManagerError(
                    "Official signed Secure Webhook test did not complete successfully for "
                    f"subscription '{item['MemberSubscriptionId']}' (state: '{state}'). "
                    "The new alert remains disabled."
                )
            details = [
                detail
                for detail in as_list(azure_property(result, "actionDetails"))
                if member(detail, "Name") == "slack-service-health"
                and member(detail, "MechanismType") == "SecureWebhook"
            ]
            if len(details) != 1:
                raise ScopeManagerError(
                    "Official signed Secure Webhook test did not return exactly one result "
                    "for 'slack-service-health'. The new alert remains disabled."
                )
            status = str(member(details[0], "Status", ""))
            if status != "Succeeded":
                raise ScopeManagerError(
                    "Official signed Secure Webhook receiver test failed for subscription "
                    f"'{item['MemberSubscriptionId']}' (status: '{status}'; "
                    f"detail: '{member(details[0], 'Detail', '')}'). The new alert remains disabled."
                )
        return "Complete"

    def assert_deployed_scope_state(self, scope: dict[str, Any]) -> None:
        for item in self.scope_members(scope):
            alert = self.azure.invoke(
                "resource",
                "show",
                "--ids",
                item["AlertId"],
                "--api-version",
                "2020-10-01",
            )
            scopes = as_list(azure_property(alert, "scopes"))
            conditions = [
                condition
                for condition in as_list(
                    nested(alert, "properties", "condition", "allOf")
                )
                if member(condition, "field") == "category"
                and member(condition, "equals") == "ServiceHealth"
            ]
            action_groups = as_list(
                nested(alert, "properties", "actions", "actionGroups")
            )
            if (
                bool(azure_property(alert, "enabled"))
                or len(scopes) != 1
                or not same_id(scopes[0], item["ScopeResourceId"])
                or len(conditions) != 1
                or len(action_groups) != 1
                or not same_id(
                    member(action_groups[0], "actionGroupId"),
                    item["ActionGroupId"],
                )
            ):
                raise ScopeManagerError(
                    f"Deployed alert '{item['AlertId']}' does not match the expected disabled "
                    "Service Health rule. It was not enabled."
                )

    def _set_enabled(
        self,
        scope: dict[str, Any],
        enabled: bool,
        resource_key: str,
        state_key: str,
        api_version: str,
        label: str,
    ) -> None:
        attempted: list[tuple[dict[str, Any], bool]] = []
        try:
            for item in self.scope_members(scope):
                original = bool(item[state_key])
                attempted.append((item, original))
                updated = self.azure.invoke(
                    "resource",
                    "update",
                    "--ids",
                    item[resource_key],
                    "--api-version",
                    api_version,
                    "--set",
                    f"properties.enabled={'true' if enabled else 'false'}",
                )
                if azure_property(updated, "enabled") is not enabled:
                    raise ScopeManagerError(
                        f"Azure did not confirm enabled={enabled} for '{item[resource_key]}'."
                    )
                item[state_key] = enabled
        except ScopeManagerError as update_error:
            rollback_errors = []
            for item, original in reversed(attempted):
                try:
                    rolled_back = self.azure.invoke(
                        "resource",
                        "update",
                        "--ids",
                        item[resource_key],
                        "--api-version",
                        api_version,
                        "--set",
                        f"properties.enabled={'true' if original else 'false'}",
                    )
                    if azure_property(rolled_back, "enabled") is not original:
                        raise ScopeManagerError("Azure did not confirm the rollback.")
                    item[state_key] = original
                except ScopeManagerError as rollback_error:
                    rollback_errors.append(
                        f"{item[resource_key]}: {rollback_error}"
                    )
            if rollback_errors:
                raise ScopeManagerError(
                    f"{label} state update failed and rollback was incomplete. Manual "
                    f"intervention is required. Update error: {update_error} Rollback errors: "
                    f"{'; '.join(rollback_errors)}"
                ) from update_error
            raise ScopeManagerError(
                f"{label} state update failed; previously attempted members were restored. "
                f"{update_error}"
            ) from update_error
        scope[state_key] = enabled

    def set_alert_enabled(self, scope: dict[str, Any], enabled: bool) -> None:
        self._set_enabled(
            scope,
            enabled,
            "AlertId",
            "Enabled",
            "2020-10-01",
            "Alert",
        )

    def set_action_group_enabled(
        self, scope: dict[str, Any], enabled: bool
    ) -> None:
        self._set_enabled(
            scope,
            enabled,
            "ActionGroupId",
            "ActionGroupEnabled",
            "2023-01-01",
            "Action Group",
        )

    def current_enabled(
        self, item: dict[str, Any], resource_key: str, state_key: str, api_version: str
    ) -> bool:
        resource = self.azure.invoke(
            "resource",
            "show",
            "--ids",
            item[resource_key],
            "--api-version",
            api_version,
        )
        enabled = azure_property(resource, "enabled")
        if not isinstance(enabled, bool):
            raise ScopeManagerError(
                f"Azure did not return a boolean enabled state for '{item[resource_key]}'."
            )
        item[state_key] = enabled
        return enabled

    def new_scope_member(
        self, scope_kind: str, scope_id: str, target_subscription_id: str
    ) -> dict[str, Any]:
        suffix = resource_suffix(scope_kind, scope_id)
        deployment = self.azure.invoke(
            "deployment",
            "sub",
            "create",
            "--subscription",
            target_subscription_id,
            "--name",
            f"service-health-{suffix}",
            "--location",
            self.central["Location"],
            "--template-file",
            str(ALERT_TEMPLATE_PATH),
            "--parameters",
            f"environmentName={self.central['EnvironmentName']}",
            f"location={self.central['Location']}",
            f"webhookUri={self.central['WebhookUri']}",
            f"secureWebhookObjectId={self.central['SecureWebhookObjectId']}",
            f"secureWebhookIdentifierUri={self.central['SecureWebhookIdentifierUri']}",
            f"tenantId={self.central['TenantId']}",
            f"scopeKind={scope_kind}",
            f"scopeId={scope_id}",
            f"targetSubscriptionId={target_subscription_id}",
            f"centralSubscriptionId={self.central['SubscriptionId']}",
            f"resourceSuffix={suffix}",
            "alertEnabled=false",
        )
        outputs = nested(deployment, "properties", "outputs") or {}
        alert_id = str(nested(outputs, "activityLogAlertId", "value") or "")
        action_group_id = str(nested(outputs, "actionGroupId", "value") or "")
        if not action_group_id:
            raise ScopeManagerError("Day-2 deployment did not return 'actionGroupId'.")
        if not alert_id:
            raise ScopeManagerError(
                "Day-2 deployment did not return 'activityLogAlertId'."
            )
        state = {
            "ScopeKind": scope_kind,
            "ScopeId": scope_id,
            "ScopeResourceId": f"/subscriptions/{target_subscription_id}",
            "AlertId": alert_id,
            "ActionGroupId": action_group_id,
            "Enabled": False,
            "ActionGroupEnabled": True,
            "TenantId": self.central["TenantId"],
            "ManagedBy": MANAGER_TAG,
            "MemberSubscriptionId": target_subscription_id,
            "OrphanedActionGroup": False,
        }
        self.assert_deployed_scope_state(state)
        return state

    def new_management_group_state(
        self,
        scope_id: str,
        members: list[dict[str, Any]],
        operational_members: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        operational = operational_members if operational_members is not None else members
        member_ids = [str(item.get("MemberSubscriptionId") or "") for item in operational]
        if len(member_ids) != len(id_set(member_ids)):
            raise ScopeManagerError(
                f"Management Group '{scope_id}' has duplicate alert members."
            )
        return {
            "ScopeKind": "managementGroup",
            "ScopeId": scope_id,
            "ScopeResourceId": (
                f"/providers/Microsoft.Management/managementGroups/{scope_id}"
            ),
            "AlertId": [item.get("AlertId") for item in operational],
            "ActionGroupId": [item.get("ActionGroupId") for item in members],
            "Enabled": bool(operational)
            and all(bool(item.get("Enabled")) for item in operational),
            "ActionGroupEnabled": bool(operational)
            and all(bool(item.get("ActionGroupEnabled")) for item in operational),
            "TenantId": self.central.get("TenantId"),
            "ManagedBy": MANAGER_TAG,
            "MemberSubscriptionIds": unique_ids(member_ids),
            "Members": members,
        }

    def new_scope_resources(
        self, scope_kind: str, scope_id: str
    ) -> dict[str, Any]:
        if scope_kind == "subscription":
            return self.new_scope_member(scope_kind, scope_id, scope_id)
        coverage = self.get_management_group_coverage(scope_id)
        if not coverage["SubscriptionIds"]:
            raise ScopeManagerError(
                f"Management Group '{scope_id}' has no descendant subscriptions to cover."
            )
        members = [
            self.new_scope_member("managementGroup", scope_id, subscription_id)
            for subscription_id in coverage["SubscriptionIds"]
        ]
        return self.new_management_group_state(scope_id, members)

    def test_subscription_tenant(self, subscription_id: str) -> dict[str, Any]:
        account = self.azure.invoke(
            "account", "show", "--subscription", subscription_id
        )
        if member(account, "state") != "Enabled":
            raise ScopeManagerError(
                f"Subscription '{subscription_id}' is not enabled."
            )
        tenant_id = str(member(account, "tenantId", ""))
        if not same_id(tenant_id, self.central["TenantId"]):
            raise ScopeManagerError(
                f"Subscription '{subscription_id}' belongs to tenant '{tenant_id}', not "
                f"central tenant '{self.central['TenantId']}'. Multi-tenant scope management "
                "is not supported."
            )
        return account

    def overlaps_for_management_group(
        self,
        coverage: dict[str, Any],
        exclude_management_group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        overlaps = [
            scope
            for scope in self.scopes
            if is_active(scope)
            and scope["ScopeKind"] == "subscription"
            and contains_id(coverage["SubscriptionIds"], scope["ScopeId"])
        ]
        for other in [
            scope
            for scope in self.scopes
            if scope["ScopeKind"] == "managementGroup"
            and not same_id(scope["ScopeId"], exclude_management_group_id)
        ]:
            other_coverage = self.get_management_group_coverage(other["ScopeId"])
            shares = bool(
                id_set(coverage["SubscriptionIds"])
                & id_set(other_coverage["SubscriptionIds"])
            )
            nested_group = (
                contains_id(
                    coverage["DescendantManagementGroupIds"],
                    other["ScopeId"],
                )
                or contains_id(
                    other_coverage["DescendantManagementGroupIds"],
                    coverage["ManagementGroupId"],
                )
            )
            if shares or nested_group:
                self.assert_membership_complete(
                    other, other_coverage, "evaluate overlapping scope changes"
                )
                overlaps.append(other)
        return overlaps

    def subscription_covered_by_management_group(
        self, subscription_id: str, exclude_management_group_id: str | None = None
    ) -> bool:
        for scope in [
            item
            for item in self.scopes
            if item["ScopeKind"] == "managementGroup"
            and not same_id(item["ScopeId"], exclude_management_group_id)
        ]:
            coverage = self.get_management_group_coverage(scope["ScopeId"])
            if not contains_id(coverage["SubscriptionIds"], subscription_id):
                continue
            self.assert_membership_complete(
                scope,
                coverage,
                f"prove replacement coverage for subscription '{subscription_id}'",
            )
            matching = [
                item
                for item in self.scope_members(scope)
                if same_id(item["MemberSubscriptionId"], subscription_id)
            ]
            if len(matching) != 1:
                raise ScopeManagerError(
                    f"Management Group '{scope['ScopeId']}' does not have exactly one member "
                    f"for subscription '{subscription_id}'."
                )
            if is_active(matching[0]):
                return True
        return False

    def protected_baseline_covers_subscription(self, subscription_id: str) -> bool:
        if self.central["ProtectedScopeKind"] == "subscription":
            return same_id(self.central["ProtectedScopeId"], subscription_id)
        coverage = self.get_management_group_coverage(
            self.central["ProtectedScopeId"]
        )
        return contains_id(coverage["SubscriptionIds"], subscription_id)

    def protected_baseline_overlaps_management_group(
        self, coverage: dict[str, Any]
    ) -> bool:
        if self.central["ProtectedScopeKind"] == "subscription":
            return contains_id(
                coverage["SubscriptionIds"],
                self.central["ProtectedScopeId"],
            )
        if same_id(
            coverage["ManagementGroupId"], self.central["ProtectedScopeId"]
        ):
            return True
        baseline = self.get_management_group_coverage(
            self.central["ProtectedScopeId"]
        )
        return bool(
            id_set(coverage["SubscriptionIds"])
            & id_set(baseline["SubscriptionIds"])
        ) or (
            contains_id(
                coverage["DescendantManagementGroupIds"],
                self.central["ProtectedScopeId"],
            )
            or contains_id(
                baseline["DescendantManagementGroupIds"],
                coverage["ManagementGroupId"],
            )
        )

    def remove_scope_resources(self, scope: dict[str, Any]) -> None:
        items = self.scope_members(scope, include_orphans=True)
        for item in items:
            if not same_id(item.get("ManagedBy"), MANAGER_TAG):
                raise ScopeManagerError(
                    f"Refusing to delete alert '{item.get('AlertId')}' because it is not owned "
                    "by the day-2 scope manager."
                )
            if (
                same_id(item.get("AlertId"), self.central["ProtectedAlertId"])
                or same_id(
                    item.get("ActionGroupId"),
                    self.central["AnchorActionGroupId"],
                )
            ):
                raise ScopeManagerError(
                    "Refusing to delete the azd-owned central baseline alert or anchor Action Group."
                )
        other_members = [
            member_item
            for other in self.scopes
            if not (
                other["ScopeKind"] == scope["ScopeKind"]
                and same_id(other["ScopeId"], scope["ScopeId"])
            )
            for member_item in self.scope_members(other, include_orphans=True)
        ]
        expected_alert_ids = id_set(
            item["AlertId"] for item in items if item.get("AlertId")
        )
        for item in items:
            shared_with_managed_scope = any(
                same_id(other.get("ActionGroupId"), item["ActionGroupId"])
                for other in other_members
            )
            if shared_with_managed_scope:
                continue
            action_group = self.azure.invoke(
                "resource",
                "show",
                "--ids",
                item["ActionGroupId"],
                "--api-version",
                "2023-01-01",
            )
            self.assert_action_group_ownership(action_group, item)
            unexpected_references = []
            for account in self.central["Accounts"]:
                if (
                    member(account, "state") != "Enabled"
                    or not same_id(
                        member(account, "tenantId"),
                        self.central["TenantId"],
                    )
                ):
                    continue
                account_id = str(member(account, "id", ""))
                alerts = as_list(
                    self.azure.invoke(
                        "monitor",
                        "activity-log",
                        "alert",
                        "list",
                        "--subscription",
                        account_id,
                    )
                )
                for alert in alerts:
                    actions = as_list(
                        nested(alert, "actions", "actionGroups")
                        or nested(
                            alert,
                            "properties",
                            "actions",
                            "actionGroups",
                        )
                    )
                    if not any(
                        same_id(
                            member(action, "actionGroupId"),
                            item["ActionGroupId"],
                        )
                        for action in actions
                    ):
                        continue
                    alert_id = str(member(alert, "id", ""))
                    if normalized_id(alert_id) not in expected_alert_ids:
                        unexpected_references.append(alert_id or "<unknown>")
            if unexpected_references:
                raise ScopeManagerError(
                    f"Refusing to delete Action Group '{item['ActionGroupId']}' because other "
                    f"Activity Log Alerts still reference it: {', '.join(unique_ids(unexpected_references))}."
                )
        for item in items:
            if item.get("AlertId"):
                self.azure.invoke("resource", "delete", "--ids", item["AlertId"])
            referenced = any(
                same_id(other.get("ActionGroupId"), item["ActionGroupId"])
                for other in other_members
            )
            if (
                not same_id(
                    item["ActionGroupId"],
                    self.central["AnchorActionGroupId"],
                )
                and not referenced
            ):
                self.azure.invoke(
                    "resource", "delete", "--ids", item["ActionGroupId"]
                )
        self.scopes = [
            item
            for item in self.scopes
            if not (
                item["ScopeKind"] == scope["ScopeKind"]
                and same_id(item["ScopeId"], scope["ScopeId"])
            )
        ]

    def refresh(self) -> None:
        self.scopes = self.get_managed_scopes()
        self.management_group_cache = {}

    def unique_scope(
        self, scope_kind: str, scope_id: str
    ) -> dict[str, Any] | None:
        found = [
            item
            for item in self.scopes
            if item["ScopeKind"] == scope_kind
            and same_id(item["ScopeId"], scope_id)
        ]
        if len(found) > 1:
            raise ScopeManagerError(
                f"Multiple managed alerts exist for {scope_kind} '{scope_id}'. "
                "Resolve duplicates manually."
            )
        return found[0] if found else None

    def assert_management_group_removal_coverage(
        self, scope_id: str
    ) -> dict[str, Any] | None:
        scope = self.unique_scope("managementGroup", scope_id)
        if scope is None:
            return None
        coverage = self.get_management_group_coverage(scope_id)
        subscriptions = unique_ids(
            list(coverage["SubscriptionIds"])
            + [
                str(item.get("MemberSubscriptionId") or "")
                for item in self.scope_members(scope)
                if item.get("MemberSubscriptionId")
            ]
        )
        uncovered = []
        for subscription_id in subscriptions:
            individual = any(
                is_active(item)
                and item["ScopeKind"] == "subscription"
                and same_id(item["ScopeId"], subscription_id)
                for item in self.scopes
            )
            other_group = self.subscription_covered_by_management_group(
                subscription_id, exclude_management_group_id=scope_id
            )
            if not individual and not other_group:
                uncovered.append(subscription_id)
        if uncovered:
            raise ScopeManagerError(
                f"Removing Management Group '{scope_id}' would leave subscriptions uncovered: "
                f"{', '.join(uncovered)}. Add replacement coverage first."
            )
        return scope

    def add_scope(
        self,
        scope_kind: str,
        scope_id: str,
        leave_disabled: bool = False,
        allow_subscription_overlap: bool = False,
    ) -> dict[str, Any]:
        existing = [
            item
            for item in self.scopes
            if item["ScopeKind"] == scope_kind
            and same_id(item["ScopeId"], scope_id)
        ]
        if len(existing) > 1:
            raise ScopeManagerError(
                f"Multiple managed alerts already exist for {scope_kind} '{scope_id}'. "
                "Resolve the duplicate resources before retrying."
            )
        coverage = None
        management_group_for_permissions = None
        if scope_kind == "subscription":
            self.test_subscription_tenant(scope_id)
            if self.protected_baseline_covers_subscription(scope_id):
                raise ScopeManagerError(
                    f"Subscription '{scope_id}' is already covered by the immutable azd-owned "
                    "baseline alert. Adding a day-2 alert would duplicate delivery."
                )
            if self.subscription_covered_by_management_group(scope_id):
                raise ScopeManagerError(
                    f"Subscription '{scope_id}' is already covered by an enabled Management "
                    "Group alert. Adding an individual alert would duplicate delivery."
                )
            deployment_ids = [scope_id]
        else:
            coverage = self.get_management_group_coverage(scope_id)
            if self.protected_baseline_overlaps_management_group(coverage):
                raise ScopeManagerError(
                    f"Management Group '{scope_id}' overlaps the immutable azd-owned baseline "
                    "alert. Choose a non-overlapping scope; the day-2 manager will not modify "
                    "the baseline."
                )
            overlaps = self.overlaps_for_management_group(
                coverage, exclude_management_group_id=scope_id
            )
            group_overlaps = [
                item for item in overlaps if item["ScopeKind"] == "managementGroup"
            ]
            sub_overlaps = [
                item for item in overlaps if item["ScopeKind"] == "subscription"
            ]
            if group_overlaps or (sub_overlaps and not allow_subscription_overlap):
                raise ScopeManagerError(
                    f"Management Group '{scope_id}' overlaps existing managed scopes. Use "
                    "migrate-to-management-group for individual subscription alerts; nested "
                    "Management Group overlaps must be removed first."
                )
            deployment_ids = list(coverage["SubscriptionIds"])
        membership = None
        if scope_kind == "managementGroup" and len(existing) == 1:
            membership = self.membership_state(existing[0], coverage)
            if membership["HasDuplicates"] or membership["UnexpectedIds"]:
                self.assert_membership_complete(
                    existing[0], coverage, "repair the logical scope automatically"
                )
        complete = scope_kind == "subscription" or (
            len(existing) == 1 and membership["Complete"]
        )
        if (
            len(existing) == 1
            and complete
            and is_active(existing[0])
            and not leave_disabled
        ):
            return {
                "Status": "AlreadyPresent",
                "TestStatus": "NotRun",
                "Scope": existing[0],
            }
        for subscription_id in deployment_ids:
            self.assert_add_permissions(
                subscription_id, management_group_for_permissions
            )
        operation = (
            "Validate and enable existing alert scope"
            if existing
            else "Create disabled alert scope, test Secure Webhook, and enable"
        )
        if not self.should_process(f"{scope_kind} '{scope_id}'", operation):
            return {
                "Status": "Planned",
                "TestStatus": "NotRun",
                "Scope": existing[0] if existing else None,
            }
        if (
            len(existing) == 1
            and scope_kind == "subscription"
            and not existing[0].get("OrphanedActionGroup")
            and existing[0].get("AlertId")
        ):
            state = existing[0]
        elif scope_kind == "subscription":
            state = self.new_scope_resources(scope_kind, scope_id)
        elif existing:
            members = [
                item
                for item in self.scope_members(existing[0])
                if not contains_id(
                    membership["RepairIds"], item["MemberSubscriptionId"]
                )
            ]
            members.extend(
                self.new_scope_member("managementGroup", scope_id, subscription_id)
                for subscription_id in membership["RepairIds"]
            )
            state = self.new_management_group_state(scope_id, members)
        else:
            state = self.new_scope_resources(scope_kind, scope_id)
        test_status = self.official_webhook_test(state)
        if not state["ActionGroupEnabled"]:
            self.set_action_group_enabled(state, True)
        if scope_kind == "managementGroup":
            self.management_group_cache = {}
            current_coverage = self.get_management_group_coverage(scope_id)
            self.assert_membership_complete(
                state, current_coverage, "activate the logical scope"
            )
        if not leave_disabled:
            self.set_alert_enabled(state, True)
        self.scopes = [
            item
            for item in self.scopes
            if not (
                item["ScopeKind"] == scope_kind
                and same_id(item["ScopeId"], scope_id)
            )
        ] + [state]
        if leave_disabled and not state["Enabled"]:
            status = "ValidatedDisabled"
        elif leave_disabled:
            status = "ValidatedPreserved"
        else:
            status = "Added"
        return {"Status": status, "TestStatus": test_status, "Scope": state}

    def remove_subscription(self, scope_id: str) -> dict[str, Any]:
        self.test_subscription_tenant(scope_id)
        existing = [
            item
            for item in self.scopes
            if item["ScopeKind"] == "subscription"
            and same_id(item["ScopeId"], scope_id)
        ]
        if not existing:
            return {"Status": "AlreadyAbsent", "ScopeId": scope_id}
        if len(existing) > 1:
            raise ScopeManagerError(
                f"Multiple individual alerts exist for subscription '{scope_id}'. "
                "Resolve duplicates manually."
            )
        if not self.subscription_covered_by_management_group(scope_id):
            raise ScopeManagerError(
                f"Removing subscription '{scope_id}' would leave a coverage gap. Add or "
                "migrate to an enabled Management Group alert first."
            )
        self.assert_remove_permissions(existing[0])
        if not self.should_process(
            f"subscription '{scope_id}'",
            "Delete Activity Log Alert and unshared Action Group",
        ):
            return {"Status": "Planned", "ScopeId": scope_id}
        if not self.confirm_destructive(
            f"Remove the individual alert for subscription '{scope_id}'? Management Group "
            "coverage has been verified."
        ):
            return {"Status": "Cancelled", "ScopeId": scope_id}
        self.refresh()
        self.test_subscription_tenant(scope_id)
        current = self.unique_scope("subscription", scope_id)
        if current is None:
            return {"Status": "AlreadyAbsent", "ScopeId": scope_id}
        if not self.subscription_covered_by_management_group(scope_id):
            raise ScopeManagerError(
                f"Coverage changed after confirmation. Subscription '{scope_id}' is no longer "
                "proven to be covered; no resources were deleted."
            )
        self.assert_remove_permissions(current)
        self.remove_scope_resources(current)
        return {"Status": "Removed", "ScopeId": scope_id}

    def remove_management_group(self, scope_id: str) -> dict[str, Any]:
        existing = self.assert_management_group_removal_coverage(scope_id)
        if existing is None:
            return {"Status": "AlreadyAbsent", "ScopeId": scope_id}
        self.assert_remove_permissions(existing)
        if not self.should_process(
            f"Management Group '{scope_id}'",
            "Delete Activity Log Alert and unshared Action Group",
        ):
            return {"Status": "Planned", "ScopeId": scope_id}
        if not self.confirm_destructive(
            f"Remove the Management Group alert '{scope_id}'? Replacement coverage has been "
            "verified for every accessible descendant subscription."
        ):
            return {"Status": "Cancelled", "ScopeId": scope_id}
        self.refresh()
        current = self.assert_management_group_removal_coverage(scope_id)
        if current is None:
            return {"Status": "AlreadyAbsent", "ScopeId": scope_id}
        self.assert_remove_permissions(current)
        self.remove_scope_resources(current)
        return {"Status": "Removed", "ScopeId": scope_id}

    def migrate_management_group(self, scope_id: str) -> dict[str, Any]:
        coverage = self.get_management_group_coverage(scope_id)
        other_groups = [
            item
            for item in self.overlaps_for_management_group(
                coverage, exclude_management_group_id=scope_id
            )
            if item["ScopeKind"] == "managementGroup"
        ]
        if other_groups:
            raise ScopeManagerError(
                f"Management Group '{scope_id}' overlaps another enabled Management Group "
                "alert. Nested Management Group migrations are not automatic."
            )
        overlaps = [
            item
            for item in self.scopes
            if item["ScopeKind"] == "subscription"
            and contains_id(coverage["SubscriptionIds"], item["ScopeId"])
            and (is_active(item) or item.get("OrphanedActionGroup"))
        ]
        existing = [
            item
            for item in self.scopes
            if item["ScopeKind"] == "managementGroup"
            and same_id(item["ScopeId"], scope_id)
        ]
        if len(existing) > 1:
            raise ScopeManagerError(
                f"Multiple alerts exist for Management Group '{scope_id}'. Resolve duplicates manually."
            )
        complete = bool(existing) and self.membership_state(
            existing[0], coverage
        )["Complete"]
        add_result = None
        if not existing or not complete or not is_active(existing[0]):
            add_result = self.add_scope(
                "managementGroup",
                scope_id,
                leave_disabled=True,
                allow_subscription_overlap=True,
            )
            if add_result["Status"] == "Planned":
                return {
                    "Status": "Planned",
                    "ManagementGroupId": scope_id,
                    "OverlappingSubscriptions": [
                        item["ScopeId"] for item in overlaps
                    ],
                }
            state = add_result["Scope"]
        else:
            state = existing[0]
        if not overlaps:
            self.management_group_cache = {}
            current_coverage = self.get_management_group_coverage(scope_id)
            self.assert_membership_complete(
                state, current_coverage, "complete migration"
            )
            if not is_active(state):
                if not self.should_process(
                    f"Management Group '{scope_id}'",
                    "Enable validated Activity Log Alert",
                ):
                    return {
                        "Status": "ValidatedDisabled",
                        "ManagementGroupId": scope_id,
                        "RemovedSubscriptions": [],
                    }
                self.set_alert_enabled(state, True)
            return {
                "Status": "Migrated",
                "ManagementGroupId": scope_id,
                "RemovedSubscriptions": [],
            }
        for overlap in overlaps:
            self.assert_remove_permissions(overlap)
        subscription_list = ", ".join(item["ScopeId"] for item in overlaps)
        if not self.should_process(
            f"Management Group '{scope_id}'",
            "Enable Management Group alert and remove overlapping individual alerts: "
            f"{subscription_list}",
        ):
            return {
                "Status": "Planned",
                "ManagementGroupId": scope_id,
                "OverlappingSubscriptions": [
                    item["ScopeId"] for item in overlaps
                ],
            }
        if not self.confirm_destructive(
            f"Enable Management Group '{scope_id}', then remove the overlapping individual "
            f"alerts for: {subscription_list}?"
        ):
            return {
                "Status": "Cancelled",
                "ManagementGroupId": scope_id,
                "ValidatedAlertId": state["AlertId"],
            }
        confirmed_ids = unique_ids(item["ScopeId"] for item in overlaps)
        self.refresh()
        coverage = self.get_management_group_coverage(scope_id)
        if any(
            item["ScopeKind"] == "managementGroup"
            for item in self.overlaps_for_management_group(
                coverage, exclude_management_group_id=scope_id
            )
        ):
            raise ScopeManagerError(
                f"Coverage changed after confirmation. Management Group '{scope_id}' now "
                "overlaps another enabled Management Group alert."
            )
        state = self.unique_scope("managementGroup", scope_id)
        if state is None:
            raise ScopeManagerError(
                "The validated Management Group alert disappeared after confirmation. "
                "No subscription alerts were removed."
            )
        members = self.scope_members(state)
        self.assert_membership_complete(
            state, coverage, "continue migration after confirmation"
        )
        current_overlaps = [
            item
            for item in self.scopes
            if item["ScopeKind"] == "subscription"
            and contains_id(coverage["SubscriptionIds"], item["ScopeId"])
            and (is_active(item) or item.get("OrphanedActionGroup"))
        ]
        current_ids = unique_ids(item["ScopeId"] for item in current_overlaps)
        if id_set(confirmed_ids) != id_set(current_ids):
            raise ScopeManagerError(
                "Coverage changed after confirmation. Overlapping subscriptions are now "
                f"'{', '.join(current_ids)}'; rerun the migration."
            )
        for overlap in current_overlaps:
            self.assert_remove_permissions(overlap)
        overlap_by_id = {
            normalized_id(item["ScopeId"]): item for item in current_overlaps
        }
        for replacement in members:
            subscription_id = replacement["MemberSubscriptionId"]
            original = overlap_by_id.get(normalized_id(subscription_id))
            if original is None:
                if not replacement["Enabled"]:
                    self.set_alert_enabled(replacement, True)
                continue
            if not self.current_enabled(
                replacement,
                "ActionGroupId",
                "ActionGroupEnabled",
                "2023-01-01",
            ):
                raise ScopeManagerError(
                    f"Replacement Action Group is disabled for subscription '{subscription_id}'. "
                    "The original alert remains enabled; no handoff occurred."
                )
            if original["Enabled"]:
                self.set_alert_enabled(original, False)
            try:
                if not replacement["Enabled"]:
                    self.set_alert_enabled(replacement, True)
            except ScopeManagerError as enable_error:
                try:
                    replacement_enabled = self.current_enabled(
                        replacement,
                        "AlertId",
                        "Enabled",
                        "2020-10-01",
                    )
                except ScopeManagerError as read_error:
                    raise ScopeManagerError(
                        f"Replacement alert state is indeterminate for subscription "
                        f"'{subscription_id}' after an enable failure. The original alert remains "
                        "disabled to avoid duplicate delivery. Immediate manual intervention is "
                        f"required. Enable error: {enable_error} State-read error: {read_error}"
                    ) from read_error
                if replacement_enabled:
                    raise ScopeManagerError(
                        f"Replacement alert is enabled for subscription '{subscription_id}' after "
                        "an uncertain enable response. The original alert remains disabled, "
                        f"preserving one active path. Manual review is required. {enable_error}"
                    ) from enable_error
                try:
                    self.set_alert_enabled(original, True)
                except ScopeManagerError as rollback_error:
                    raise ScopeManagerError(
                        f"Replacement alert failed for subscription '{subscription_id}', and its "
                        "original alert could not be re-enabled. Immediate manual intervention is "
                        f"required. Replacement error: {enable_error} Rollback error: {rollback_error}"
                    ) from rollback_error
                raise ScopeManagerError(
                    f"Replacement alert failed for subscription '{subscription_id}'. Its original "
                    f"alert was re-enabled, so coverage remains intact. {enable_error}"
                ) from enable_error
        state["Enabled"] = all(item["Enabled"] for item in members)
        for subscription_id in confirmed_ids:
            original = overlap_by_id[normalized_id(subscription_id)]
            replacement = next(
                item
                for item in members
                if same_id(item["MemberSubscriptionId"], subscription_id)
            )
            try:
                alert_enabled = self.current_enabled(
                    replacement, "AlertId", "Enabled", "2020-10-01"
                )
                action_group_enabled = self.current_enabled(
                    replacement,
                    "ActionGroupId",
                    "ActionGroupEnabled",
                    "2023-01-01",
                )
            except ScopeManagerError as state_error:
                raise ScopeManagerError(
                    f"Replacement state is indeterminate for subscription '{subscription_id}'. "
                    "Its disabled original was not deleted. Inspect both paths and restore exactly "
                    f"one active path before retrying. {state_error}"
                ) from state_error
            if not alert_enabled or not action_group_enabled:
                if original.get("OrphanedActionGroup"):
                    raise ScopeManagerError(
                        f"Replacement coverage became inactive for subscription '{subscription_id}'. "
                        "The orphaned Action Group was retained; no original alert exists to restore."
                    )
                try:
                    self.set_alert_enabled(original, True)
                except ScopeManagerError as restore_error:
                    raise ScopeManagerError(
                        f"Replacement coverage became inactive for subscription '{subscription_id}', "
                        "and its original alert could not be restored. Immediate manual intervention "
                        f"is required. {restore_error}"
                    ) from restore_error
                raise ScopeManagerError(
                    f"Replacement coverage became inactive for subscription '{subscription_id}'. "
                    "Its original alert was restored and was not deleted."
                )
            try:
                self.remove_scope_resources(original)
            except ScopeManagerError as delete_error:
                raise ScopeManagerError(
                    "Replacement coverage is enabled and the original alert is disabled, but an "
                    "overlapping subscription resource could not be deleted. No duplicate delivery "
                    f"is active; rerun migration to finish cleanup. {delete_error}"
                ) from delete_error
        return {
            "Status": "Migrated",
            "ManagementGroupId": scope_id,
            "RemovedSubscriptions": confirmed_ids,
            "TestStatus": add_result["TestStatus"] if add_result else "NotRun",
        }

    def report(self) -> list[dict[str, Any]]:
        coverages: dict[str, dict[str, Any]] = {}
        memberships: dict[str, dict[str, Any]] = {}
        for scope in [
            item for item in self.scopes if item["ScopeKind"] == "managementGroup"
        ]:
            coverages[scope["ScopeId"]] = self.get_management_group_coverage(
                scope["ScopeId"]
            )
            memberships[scope["ScopeId"]] = self.membership_state(
                scope, coverages[scope["ScopeId"]]
            )
        report = []
        for scope in self.scopes:
            if scope["ScopeKind"] == "subscription":
                self.test_subscription_tenant(scope["ScopeId"])
                covering_groups = [
                    item["ScopeId"]
                    for item in self.scopes
                    if item["ScopeKind"] == "managementGroup"
                    and is_active(item)
                    and memberships[item["ScopeId"]]["Complete"]
                    and contains_id(
                        coverages[item["ScopeId"]]["SubscriptionIds"],
                        scope["ScopeId"],
                    )
                ]
                protected = self.protected_baseline_covers_subscription(
                    scope["ScopeId"]
                )
                effective = (
                    "Covered" if is_active(scope) or covering_groups else "Disabled"
                )
                overlaps = []
                if is_active(scope) and covering_groups:
                    overlaps.append(f"Duplicate with MG: {', '.join(covering_groups)}")
                if is_active(scope) and protected:
                    overlaps.append("Duplicate with protected baseline")
                detail = (
                    f"{scope['ScopeId']}; orphaned Action Group requires repair or cleanup"
                    if scope.get("OrphanedActionGroup")
                    else scope["ScopeId"]
                )
                covered_ids = [scope["ScopeId"]]
            else:
                coverage = coverages[scope["ScopeId"]]
                membership = memberships[scope["ScopeId"]]
                effective = (
                    "Incomplete"
                    if not membership["Complete"]
                    else "Covered"
                    if is_active(scope)
                    else "Disabled"
                )
                individual = [
                    item["ScopeId"]
                    for item in self.scopes
                    if item["ScopeKind"] == "subscription"
                    and is_active(item)
                    and contains_id(
                        coverage["SubscriptionIds"], item["ScopeId"]
                    )
                ]
                group_overlaps = []
                for item in self.scopes:
                    if (
                        item["ScopeKind"] != "managementGroup"
                        or same_id(item["ScopeId"], scope["ScopeId"])
                        or not is_active(item)
                    ):
                        continue
                    other = coverages[item["ScopeId"]]
                    if (
                        contains_id(
                            coverage["DescendantManagementGroupIds"],
                            item["ScopeId"],
                        )
                        or contains_id(
                            other["DescendantManagementGroupIds"],
                            scope["ScopeId"],
                        )
                    ):
                        group_overlaps.append(item["ScopeId"])
                overlaps = []
                if individual:
                    overlaps.append(f"Subscriptions: {', '.join(individual)}")
                if group_overlaps:
                    overlaps.append(
                        f"Management Groups: {', '.join(group_overlaps)}"
                    )
                if is_active(
                    scope
                ) and self.protected_baseline_overlaps_management_group(coverage):
                    overlaps.append("Protected baseline")
                if membership["Complete"]:
                    detail = (
                        f"{len(coverage['SubscriptionIds'])} descendant subscription(s)"
                    )
                    if membership["OrphanedIds"]:
                        detail += (
                            "; orphaned Action Groups requiring cleanup: "
                            + ", ".join(membership["OrphanedIds"])
                        )
                    covered_ids = list(coverage["SubscriptionIds"])
                else:
                    issues = []
                    if membership["MissingIds"]:
                        issues.append(
                            f"missing members: {', '.join(membership['MissingIds'])}"
                        )
                    if membership["UnexpectedIds"]:
                        issues.append(
                            "unexpected members: "
                            + ", ".join(membership["UnexpectedIds"])
                        )
                    if membership["HasDuplicates"]:
                        issues.append("duplicate or blank member IDs")
                    if membership["OrphanedIds"]:
                        issues.append(
                            "orphaned Action Groups: "
                            + ", ".join(membership["OrphanedIds"])
                        )
                    detail = (
                        f"{len(coverage['SubscriptionIds'])} descendant(s); "
                        + "; ".join(issues)
                    )
                    covered_ids = sorted(
                        {
                            item["MemberSubscriptionId"]
                            for item in self.scope_members(scope)
                            if is_active(item)
                        }
                    )
            report.append(
                {
                    "Environment": self.central["EnvironmentName"],
                    "TenantId": self.central["TenantId"],
                    "ScopeKind": scope["ScopeKind"],
                    "ScopeId": scope["ScopeId"],
                    "EffectiveCoverage": effective,
                    "CoverageDetail": detail,
                    "CoveredSubscriptionIds": covered_ids,
                    "Enabled": scope["Enabled"],
                    "ActionGroupEnabled": scope["ActionGroupEnabled"],
                    "AlertId": scope["AlertId"],
                    "ActionGroupId": scope["ActionGroupId"],
                    "Overlap": "; ".join(overlaps),
                }
            )
        return report

    def execute(
        self,
        command: str,
        subscription_id: str | None = None,
        management_group_id: str | None = None,
    ) -> Any:
        self.initialize()
        if command == "list":
            return self.report()
        if command in ("add-subscription", "remove-subscription"):
            if not subscription_id:
                raise ScopeManagerError(
                    f"--subscription-id is required for {command}."
                )
            if command == "add-subscription":
                return self.add_scope("subscription", subscription_id)
            return self.remove_subscription(subscription_id)
        if not management_group_id:
            raise ScopeManagerError(
                f"--management-group-id is required for {command}."
            )
        if command == "add-management-group":
            return self.add_scope("managementGroup", management_group_id)
        if command == "remove-management-group":
            return self.remove_management_group(management_group_id)
        return self.migrate_management_group(management_group_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage Azure Service Health subscription and logical Management Group "
            "alert scopes without redeploying the central runtime."
        )
    )
    parser.add_argument(
        "command",
        choices=(
            "list",
            "add-subscription",
            "remove-subscription",
            "add-management-group",
            "remove-management-group",
            "migrate-to-management-group",
        ),
    )
    parser.add_argument("--subscription-id")
    parser.add_argument("--management-group-id")
    parser.add_argument("--environment-name")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Approve a destructive operation noninteractively.",
    )
    parser.add_argument(
        "--what-if",
        action="store_true",
        help="Validate and report the planned operation without mutation.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    return parser


def format_text(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "No manager-owned day-2 alert scopes found."
        columns = (
            "Environment",
            "ScopeKind",
            "ScopeId",
            "EffectiveCoverage",
            "Enabled",
            "ActionGroupEnabled",
            "CoverageDetail",
            "Overlap",
        )
        widths = {
            column: max(
                len(column),
                *(len(str(item.get(column, ""))) for item in value),
            )
            for column in columns
        }
        header = "  ".join(column.ljust(widths[column]) for column in columns)
        divider = "  ".join("-" * widths[column] for column in columns)
        rows = [
            "  ".join(str(item.get(column, "")).ljust(widths[column]) for column in columns)
            for item in value
        ]
        return "\n".join((header, divider, *rows))
    return json.dumps(value, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def should_process(target: str, operation: str) -> bool:
        if args.what_if:
            print(f"WHAT IF: {operation}: {target}", file=sys.stderr)
            return False
        return True

    def confirm(question: str) -> bool:
        if args.force:
            return True
        if not sys.stdin.isatty():
            raise ScopeManagerError(
                "Destructive operations require interactive confirmation or --force for "
                "pre-approved noninteractive automation."
            )
        print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
        response = input().strip().lower()
        return response in ("y", "yes")

    try:
        result = ScopeManager(
            AzureCli(),
            environment_name=args.environment_name,
            should_process=should_process,
            confirm_destructive=confirm,
        ).execute(
            args.command,
            subscription_id=args.subscription_id,
            management_group_id=args.management_group_id,
        )
    except (ScopeManagerError, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
