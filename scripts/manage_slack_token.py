#!/usr/bin/env python3
"""Manage the Slack bot token lifecycle in Azure Key Vault.

The token value itself is never accepted as a command-line argument, never
placed in a process environment variable, never passed to a child process,
never written to a temporary file, and never logged or printed. It only ever
flows through: an interactive `getpass` prompt (bootstrap/rotate), a direct
Python-level read of the exact local AZD dotenv file (migrate only), an
injectable Slack client used solely for `auth.test` validation, and an
injectable Key Vault `SecretClient.set_secret` call. Every command emits only
nonsecret, structured state.

`SERVICE_HEALTH_SECRET_VERSION` is an *emergency pin*: it is normally empty
(versionless/"use latest" mode) and is only ever set to a specific version
during an explicit `rollback`, or transiently by `rotate` if its own
acceptance check fails. The actual latest/previous versions are recorded
separately in `SERVICE_HEALTH_SECRET_LATEST_VERSION` and
`SERVICE_HEALTH_SECRET_PREVIOUS_VERSION` for nonsecret observability.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from getpass import getpass
from pathlib import Path
from typing import Any, Callable, Protocol

try:
    from scripts.manage_alert_scopes import (
        AzureCli,
        ScopeManager,
        ScopeManagerError,
        WORKLOAD_TAG,
        azure_property,
        as_list,
        member,
        nested,
        resource_coordinates,
        same_id,
        tag,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised via direct script execution
    from manage_alert_scopes import (  # type: ignore[no-redef]
        AzureCli,
        ScopeManager,
        ScopeManagerError,
        WORKLOAD_TAG,
        azure_property,
        as_list,
        member,
        nested,
        resource_coordinates,
        same_id,
        tag,
    )

try:
    from scripts.configure_secure_webhook import (
        AzdCli,
        LEGACY_TOKEN_ENV_NAME,
        TOKEN_MIGRATION_MARKER_ENV_NAME,
        enforce_production_readiness,
        local_dotenv_value_present,
        parse_dotenv_value,
        resolve_local_dotenv_path,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised via direct script execution
    from configure_secure_webhook import (  # type: ignore[no-redef]
        AzdCli,
        LEGACY_TOKEN_ENV_NAME,
        TOKEN_MIGRATION_MARKER_ENV_NAME,
        enforce_production_readiness,
        local_dotenv_value_present,
        parse_dotenv_value,
        resolve_local_dotenv_path,
    )

try:
    from scripts.operation_lock import (
        OperationJournal,
        OperationLock,
        OperationLockError,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised via direct script execution
    from operation_lock import (  # type: ignore[no-redef]
        OperationJournal,
        OperationLock,
        OperationLockError,
    )


SECRET_NAME = "slack-bot-token"
KEY_VAULT_RESOURCE_TYPE = "Microsoft.KeyVault/vaults"
ROLE_DEFINITION_NAME = "Key Vault Secrets Officer"
# Emergency pin only: normally empty (versionless "use latest" mode). Never
# set to the latest version as part of ordinary success; only `rollback`
# (and a failed `rotate` acceptance check) ever assign it a concrete value.
SECRET_VERSION_ENV_NAME = "SERVICE_HEALTH_SECRET_VERSION"
SECRET_LATEST_VERSION_ENV_NAME = "SERVICE_HEALTH_SECRET_LATEST_VERSION"
PREVIOUS_SECRET_VERSION_ENV_NAME = "SERVICE_HEALTH_SECRET_PREVIOUS_VERSION"
# Must match scripts.configure_secure_webhook.NONSECRET_AZD_DEFAULTS keys.
DEPLOY_WORKLOAD_ENV_NAME = "SERVICE_HEALTH_DEPLOY_WORKLOAD"
BASELINE_ALERT_ENV_NAME = "SERVICE_HEALTH_BASELINE_ALERT_ENABLED"
EXPECTED_TEAM_ID_ENV_NAME = "SERVICE_HEALTH_SLACK_TEAM_ID"
EXPECTED_BOT_USER_ID_ENV_NAME = "SERVICE_HEALTH_SLACK_BOT_USER_ID"
TOKEN_FORMAT_PATTERN = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
# Matches any Slack-token-shaped substring (xoxb/xoxe/xoxa/xoxp/xoxr-...) so
# it can be scrubbed from exception/journal text regardless of source.
TOKEN_REDACTION_PATTERN = re.compile(r"xox[a-z]-[A-Za-z0-9-]+")
CONTAINER_APP_API_VERSION = "2023-05-01"
ENVIRONMENT_NAME_ENV_NAME = "AZURE_ENV_NAME"
SUBSCRIPTION_ID_ENV_NAME = "AZURE_SUBSCRIPTION_ID"
TENANT_ID_ENV_NAME = "AZURE_TENANT_ID"
RESOURCE_GROUP_ENV_NAME = "AZURE_RESOURCE_GROUP"
RBAC_PROPAGATION_DELAYS = (
    2,
    4,
    8,
    16,
    30,
    60,
    60,
    60,
    60,
    60,
    60,
    60,
    60,
    60,
)


class LifecycleStateError(ScopeManagerError):
    """A lifecycle failure with an explicit non-success terminal state."""

    def __init__(self, lifecycle_state: str, message: str) -> None:
        super().__init__(f"{lifecycle_state}: {message}")
        self.lifecycle_state = lifecycle_state


class SecretClientProtocol(Protocol):
    def set_secret(self, name: str, value: str) -> Any:
        ...

    def get_secret(self, name: str, version: str | None = None) -> Any:
        ...

    def list_properties_of_secrets(self) -> Any:
        ...


class SlackClientProtocol(Protocol):
    def auth_test(self) -> Any:
        ...


def _redact(text: str) -> str:
    """Scrub any Slack-token-shaped substring from a string before it is
    recorded in a journal entry or displayed to an operator. Applied
    defensively to exception text from any source (including third-party
    SDKs), independent of whether the token value is tracked explicitly."""
    return TOKEN_REDACTION_PATTERN.sub("[REDACTED-SLACK-TOKEN]", text)


class SanitizedAzureCliCredential:
    """Azure SDK credential backed by the scrubbed Azure CLI boundary."""

    def __init__(self, azure: AzureCli | None = None) -> None:
        self.azure = azure or AzureCli()

    def get_token(self, *scopes: str, **_kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken
        from datetime import datetime, timezone

        if len(scopes) != 1 or not scopes[0]:
            raise ScopeManagerError(
                "Key Vault credential requires exactly one Azure resource "
                "scope."
            )
        scope = scopes[0]
        resource = (
            scope[: -len("/.default")]
            if scope.endswith("/.default")
            else scope
        )
        response = self.azure.invoke(
            "account",
            "get-access-token",
            "--resource",
            resource,
        )
        token = str(
            member(response, "accessToken")
            or member(response, "token")
            or ""
        )
        raw_expiry = (
            member(response, "expires_on")
            or member(response, "expiresOn")
        )
        if not token or raw_expiry is None:
            raise ScopeManagerError(
                "Azure CLI did not return an access token and expiry."
            )
        try:
            expires_on = int(raw_expiry)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(
                    str(raw_expiry).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ScopeManagerError(
                    "Azure CLI returned an invalid access-token expiry."
                ) from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expires_on = int(parsed.timestamp())
        return AccessToken(token, expires_on)

    def close(self) -> None:
        return None


def default_credential_factory() -> Any:
    return SanitizedAzureCliCredential()


def default_secret_client_factory(vault_uri: str, credential: Any) -> Any:
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=vault_uri, credential=credential)


def default_slack_client_factory(token: str) -> Any:
    from slack_sdk import WebClient

    return WebClient(token=token)


def default_public_ip_resolver() -> str:
    """Resolves the *explicit* caller IPv4 address for a temporary Key
    Vault firewall exception. Deliberately never contacts any third-party
    "what is my IP" service (e.g. ipify): the plan requires Azure CLI only
    for nonsecret network metadata, and an explicit /32 exception means the
    operator states their own address rather than the tool silently
    discovering and transmitting it to an external, non-Azure endpoint.
    This performs no network I/O at all; validation of the resulting value
    happens in `_TemporaryVaultNetworkAccess`."""
    value = input(
        "Enter your current public IPv4 address for the temporary Key "
        "Vault network exception (explicit /32; not auto-discovered): "
    ).strip()
    return value


def default_prompt_token() -> str:
    return getpass("Slack bot token (xoxb-...): ")


def default_not_found_exception_factory() -> type[BaseException]:
    """Lazily imports the real Azure SDK 'not found' exception type, kept
    optional at module-parse time like the other azure-sdk lazy imports.
    Tests inject a lightweight fake exception type instead."""
    from azure.core.exceptions import ResourceNotFoundError

    return ResourceNotFoundError


def default_provisioning_checker(azure: Any, central: dict[str, Any]) -> None:
    """Confirm the provisioned Container App has a ready revision."""
    container_app_id = str(central.get("ContainerAppId") or "")
    if not container_app_id:
        raise ScopeManagerError(
            "Cannot verify acceptance: the central deployment has no "
            "Container App id."
        )
    details = azure.invoke(
        "rest",
        "--method",
        "get",
        "--uri",
        f"https://management.azure.com{container_app_id}"
        f"?api-version={CONTAINER_APP_API_VERSION}",
    )
    state = str(nested(details, "properties", "provisioningState") or "").casefold()
    if state != "succeeded":
        raise ScopeManagerError(
            "Acceptance check failed: the central Container App "
            f"provisioningState is '{state or 'unknown'}', not 'Succeeded'."
        )
    revision = str(nested(details, "properties", "latestReadyRevisionName") or "")
    if not revision:
        raise ScopeManagerError(
            "Acceptance check failed: no latest ready Container App revision."
        )


def default_acceptance_checker(azure: Any, central: dict[str, Any]) -> None:
    """Run token-free probes, auth-boundary, and signed-delivery acceptance."""
    default_provisioning_checker(azure, central)
    webhook_uri = str(central.get("WebhookUri") or "")
    if not webhook_uri.endswith("/api/service-health"):
        raise ScopeManagerError(
            "Acceptance check failed: the Secure Webhook URI is missing."
        )
    app_uri = webhook_uri[: -len("/api/service-health")]
    for path in ("/healthz", "/readyz"):
        try:
            with urllib.request.urlopen(
                f"{app_uri}{path}", timeout=30
            ) as response:
                status = response.status
        except urllib.error.URLError as exc:
            raise ScopeManagerError(
                f"Acceptance check failed for '{path}': {exc.reason}."
            ) from exc
        if status != 200:
            raise ScopeManagerError(
                f"Acceptance check failed for '{path}': HTTP {status}."
            )
    request = urllib.request.Request(
        webhook_uri,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise ScopeManagerError(
                "Acceptance check failed: unauthenticated webhook returned "
                f"HTTP {exc.code}, expected 401."
            ) from exc
    else:
        raise ScopeManagerError(
            "Acceptance check failed: unauthenticated webhook did not return 401."
        )
    subscription_id, resource_group, action_group_name = resource_coordinates(
        central["AnchorActionGroupId"]
    )
    result = azure.invoke(
        "monitor",
        "action-group",
        "test-notifications",
        "create",
        "--subscription",
        subscription_id,
        "--resource-group",
        resource_group,
        "--action-group-name",
        action_group_name,
        "--alert-type",
        "servicehealth",
        "--add-action",
        "webhook",
        "slack-service-health",
        webhook_uri,
        "useaadauth",
        central["SecureWebhookObjectId"],
        central["SecureWebhookIdentifierUri"],
        "usecommonalertschema",
    )
    operation_state = str(azure_property(result, "state") or "")
    action_details = [
        item
        for item in as_list(azure_property(result, "actionDetails"))
        if member(item, "Name") == "slack-service-health"
        and member(item, "MechanismType") == "SecureWebhook"
    ]
    receiver_state = (
        str(member(action_details[0], "Status", ""))
        if len(action_details) == 1
        else ""
    )
    if (
        operation_state not in {"Complete", "Completed"}
        or receiver_state not in {"Succeeded", "Completed"}
    ):
        raise ScopeManagerError(
            "Acceptance check failed: signed Service Health delivery did not "
            "complete successfully. Slack/Table success is not proven."
        )


def default_active_deployment_checker(azure: Any, central: dict[str, Any]) -> bool:
    """Nonsecret ARM metadata check: True if any deployment in the central
    resource group is still actively running. Used to refuse lock recovery
    while a real operation may still be in flight."""
    deployments = as_list(
        azure.invoke(
            "deployment",
            "group",
            "list",
            "--resource-group",
            central["ResourceGroup"],
            "--subscription",
            central["SubscriptionId"],
        )
    )
    for deployment in deployments:
        state = str(
            nested(deployment, "properties", "provisioningState") or ""
        ).casefold()
        if state == "running":
            return True
    return False


def _slack_field(response: Any, name: str) -> str:
    try:
        value = response[name]
    except (KeyError, TypeError, IndexError):
        value = None
    return str(value or "")


class _LocalFileLock:
    """A private, non-ARM advisory lock guarding exclusive local dotenv
    edits. Intentionally independent of the distributed ARM lock, which
    guards the central deployment, not an operator's local filesystem."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "_LocalFileLock":
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ScopeManagerError(
                "A local token migration already appears to be in progress "
                f"('{self.path.name}' lock file exists)."
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._fd is not None:
            os.close(self._fd)
        self.path.unlink(missing_ok=True)
        return False


def remove_local_token_line(
    dotenv_path: Path, name: str = LEGACY_TOKEN_ENV_NAME
) -> bool:
    """Atomically remove the exact `name=...` line (matched only at the
    exact beginning of the line, never an indented occurrence) from a local
    AZD dotenv file, preserving file permissions. Returns True if a line was
    removed. Never logs, prints, or raises with the removed value."""
    if not dotenv_path.is_file():
        return False
    original_stat = dotenv_path.stat()
    lines = dotenv_path.read_text(encoding="utf-8").splitlines(keepends=True)
    prefix = f"{name}="
    kept: list[str] = []
    removed = False
    for line in lines:
        if not removed and line.startswith(prefix):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False
    tmp_path = dotenv_path.with_name(f"{dotenv_path.name}.tmp-{os.getpid()}")
    tmp_path.write_text("".join(kept), encoding="utf-8")
    os.chmod(tmp_path, original_stat.st_mode)
    os.replace(tmp_path, dotenv_path)
    return True


def read_local_token(dotenv_path: Path, name: str = LEGACY_TOKEN_ENV_NAME) -> str:
    """Read the exact `name=...` value directly from a local AZD dotenv
    file (exact beginning-of-line match only). Never invokes a subprocess,
    never reads a process environment variable, and never returns the value
    through any exception message. Malformed (mismatched-quote) values fail
    closed via `parse_dotenv_value` rather than being silently mutated."""
    if not dotenv_path.is_file():
        raise ScopeManagerError(
            "Local AZD environment file not found for migration: "
            f"{dotenv_path.name}."
        )
    prefix = f"{name}="
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            value = parse_dotenv_value(line[len(prefix):])
            if not value:
                raise ScopeManagerError(
                    f"Local '{name}' value is empty; nothing to migrate."
                )
            return value
    raise ScopeManagerError(
        f"No local '{name}' value found for migration in {dotenv_path.name}."
    )


def _normalize_ip_rules(rules: Any) -> list[str]:
    return sorted(str(member(rule, "value", "")) for rule in (rules or []))


def _update_operation_journal(
    journal: OperationJournal,
    operation_id: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    current = journal.read(operation_id) or {}
    updated = {**current, **changes}
    journal.record(operation_id, updated)
    return updated


class _TemporaryVaultNetworkAccess:
    """Opens exactly one explicit IPv4 /32 exception on an otherwise
    default-deny Key Vault firewall, and restores the *exact* prior state
    (publicNetworkAccess, defaultAction, bypass, and all ipRules) on exit,
    verifying via readback both after opening and after restoring. A
    restore-verification failure raises and propagates out of the `with`
    block, so it always occurs before any subsequent provisioning call in
    the caller."""

    def __init__(
        self,
        azure: Any,
        vault_name: str,
        subscription_id: str,
        public_ip_resolver: Callable[[], str],
        journal: OperationJournal,
        operation_id: str,
    ) -> None:
        self.azure = azure
        self.vault_name = vault_name
        self.subscription_id = subscription_id
        self.public_ip_resolver = public_ip_resolver
        self.journal = journal
        self.operation_id = operation_id
        self._added_rule: str | None = None
        self._snapshot: dict[str, Any] | None = None

    def _show(self) -> Any:
        return self.azure.invoke(
            "keyvault",
            "show",
            "--name",
            self.vault_name,
            "--subscription",
            self.subscription_id,
        )

    def __enter__(self) -> "_TemporaryVaultNetworkAccess":
        operation_state = self.journal.read(self.operation_id) or {}
        prior_evidence = operation_state.get("TemporaryNetworkAccess") or {}
        if prior_evidence.get("State") in {
            "OPEN_PENDING",
            "OPEN",
            "CLEANUP_INCOMPLETE",
        }:
            if prior_evidence.get("VaultName") != self.vault_name:
                raise ScopeManagerError(
                    "Stale temporary network evidence belongs to another "
                    "vault; refusing ambiguous recovery."
                )
            prior_snapshot = prior_evidence.get("Snapshot")
            if not isinstance(prior_snapshot, dict):
                raise ScopeManagerError(
                    "Stale temporary network access lacks an exact snapshot; "
                    "manual recovery is required."
                )
            self._snapshot = prior_snapshot
            try:
                self._restore_exact()
            except Exception as restore_exc:
                _update_operation_journal(
                    self.journal,
                    self.operation_id,
                    {
                        "State": "CLEANUP_INCOMPLETE",
                        "TemporaryNetworkAccess": {
                            **prior_evidence,
                            "State": "CLEANUP_INCOMPLETE",
                            "RestoreError": _redact(str(restore_exc)),
                        },
                    },
                )
                raise ScopeManagerError(
                    "CLEANUP_INCOMPLETE: stale operation-owned Key Vault "
                    "network access could not be restored exactly."
                ) from restore_exc
            self._snapshot = None
        current = self._show()
        properties = nested(current, "properties") or {}
        network_acls = member(properties, "networkAcls") or {}
        default_action = str(member(network_acls, "defaultAction", "") or "")
        if default_action.casefold() != "deny":
            raise ScopeManagerError(
                f"Key Vault '{self.vault_name}' network access is not "
                "default-deny; refusing to open a temporary access path."
            )
        self._snapshot = {
            "publicNetworkAccess": str(
                member(properties, "publicNetworkAccess", "") or ""
            ),
            "defaultAction": default_action,
            "bypass": member(network_acls, "bypass"),
            "ipRules": [dict(rule) for rule in (member(network_acls, "ipRules", []) or [])],
        }
        _update_operation_journal(
            self.journal,
            self.operation_id,
            {
                "TemporaryNetworkAccess": {
                    "State": "OPEN_PENDING",
                    "VaultName": self.vault_name,
                    "Snapshot": self._snapshot,
                }
            },
        )
        raw_ip = self.public_ip_resolver()
        try:
            ipaddress.IPv4Address(raw_ip)
        except ValueError as exc:
            raise ScopeManagerError(
                "Could not resolve an explicit caller IPv4 address for "
                "temporary Key Vault network access."
            ) from exc
        rule = f"{raw_ip}/32"
        try:
            self._open(rule)
        except Exception as open_exc:
            try:
                self._restore_exact()
            except Exception as restore_exc:
                _update_operation_journal(
                    self.journal,
                    self.operation_id,
                    {
                        "State": "CLEANUP_INCOMPLETE",
                        "TemporaryNetworkAccess": {
                            "State": "CLEANUP_INCOMPLETE",
                            "VaultName": self.vault_name,
                            "Snapshot": self._snapshot,
                            "OpenError": _redact(str(open_exc)),
                            "RestoreError": _redact(str(restore_exc)),
                        },
                    },
                )
                raise ScopeManagerError(
                    "CLEANUP_INCOMPLETE: temporary Key Vault network access "
                    "failed to open and exact restoration also failed. "
                    f"Open error: {_redact(str(open_exc))} Restore error: "
                    f"{_redact(str(restore_exc))}"
                ) from restore_exc
            raise
        self._added_rule = rule
        _update_operation_journal(
            self.journal,
            self.operation_id,
            {
                "TemporaryNetworkAccess": {
                    "State": "OPEN",
                    "VaultName": self.vault_name,
                    "Snapshot": self._snapshot,
                }
            },
        )
        return self

    def _open(self, rule: str) -> None:
        self.azure.invoke(
            "keyvault",
            "update",
            "--name",
            self.vault_name,
            "--subscription",
            self.subscription_id,
            "--public-network-access",
            "Enabled",
            "--default-action",
            "Deny",
        )
        self._replace_ip_rules([rule])
        current = self._show()
        properties = nested(current, "properties") or {}
        public_network_access = str(
            member(properties, "publicNetworkAccess", "") or ""
        ).casefold()
        network_acls = member(properties, "networkAcls") or {}
        default_action = str(member(network_acls, "defaultAction", "") or "").casefold()
        ip_values = set(_normalize_ip_rules(member(network_acls, "ipRules", [])))
        if public_network_access != "enabled" or default_action != "deny" or rule not in ip_values:
            raise ScopeManagerError(
                "Temporary Key Vault network access verification failed "
                "after opening the exception rule."
            )

    def _replace_ip_rules(self, desired: list[str]) -> None:
        current = self._show()
        current_rules = _normalize_ip_rules(
            nested(current, "properties", "networkAcls", "ipRules") or []
        )
        for current_rule in current_rules:
            self.azure.invoke(
                "keyvault",
                "network-rule",
                "remove",
                "--name",
                self.vault_name,
                "--subscription",
                self.subscription_id,
                "--ip-address",
                current_rule,
            )
        for desired_rule in desired:
            self.azure.invoke(
                "keyvault",
                "network-rule",
                "add",
                "--name",
                self.vault_name,
                "--subscription",
                self.subscription_id,
                "--ip-address",
                desired_rule,
            )

    def _restore_exact(self) -> None:
        if self._snapshot is None:
            return
        snapshot = self._snapshot
        self._replace_ip_rules(_normalize_ip_rules(snapshot["ipRules"]))
        arguments = [
            "keyvault",
            "update",
            "--name",
            self.vault_name,
            "--subscription",
            self.subscription_id,
            "--public-network-access",
            snapshot["publicNetworkAccess"] or "Disabled",
            "--default-action",
            snapshot["defaultAction"] or "Deny",
        ]
        if snapshot.get("bypass") is not None:
            arguments.extend(["--bypass", str(snapshot["bypass"])])
        self.azure.invoke(*arguments)
        current = self._show()
        properties = nested(current, "properties") or {}
        public_network_access = str(member(properties, "publicNetworkAccess", "") or "")
        network_acls = member(properties, "networkAcls") or {}
        default_action = str(member(network_acls, "defaultAction", "") or "")
        bypass = member(network_acls, "bypass")
        ip_rules = member(network_acls, "ipRules", [])
        matches = (
            public_network_access.casefold() == (snapshot["publicNetworkAccess"] or "").casefold()
            and default_action.casefold() == (snapshot["defaultAction"] or "").casefold()
            and bypass == snapshot.get("bypass")
            and _normalize_ip_rules(ip_rules) == _normalize_ip_rules(snapshot["ipRules"])
        )
        if not matches:
            raise ScopeManagerError(
                "Temporary Key Vault network access restore verification "
                "failed: the vault firewall state does not exactly match "
                "the original snapshot."
            )
        _update_operation_journal(
            self.journal,
            self.operation_id,
            {
                "TemporaryNetworkAccess": {
                    "State": "RESTORED",
                    "VaultName": self.vault_name,
                    "Snapshot": self._snapshot,
                }
            },
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._added_rule is None:
            return False
        self._added_rule = None
        try:
            self._restore_exact()
        except Exception as restore_exc:
            _update_operation_journal(
                self.journal,
                self.operation_id,
                {
                    "State": "CLEANUP_INCOMPLETE",
                    "TemporaryNetworkAccess": {
                        "State": "CLEANUP_INCOMPLETE",
                        "VaultName": self.vault_name,
                        "Snapshot": self._snapshot,
                        "RestoreError": _redact(str(restore_exc)),
                    },
                },
            )
            raise
        return False


class _TemporaryRoleAssignment:
    """Creates exactly one Key Vault RBAC role assignment for the signed-in
    caller and removes exactly that assignment (never any pre-existing
    assignment) on exit."""

    def __init__(
        self,
        azure: Any,
        vault_id: str,
        subscription_id: str,
        journal: OperationJournal,
        operation_id: str,
    ) -> None:
        self.azure = azure
        self.vault_id = vault_id
        self.subscription_id = subscription_id
        self.journal = journal
        self.operation_id = operation_id
        self._assignment_id: str | None = None
        self._created = False

    @staticmethod
    def _name(assignment: Any) -> str:
        name = str(member(assignment, "name", "") or "")
        if name:
            return name
        assignment_id = str(member(assignment, "id", "") or "")
        return assignment_id.rstrip("/").rsplit("/", 1)[-1]

    def _list(self, principal_id: str) -> list[Any]:
        return as_list(
            self.azure.invoke(
                "role",
                "assignment",
                "list",
                "--assignee-object-id",
                principal_id,
                "--role",
                ROLE_DEFINITION_NAME,
                "--scope",
                self.vault_id,
                "--subscription",
                self.subscription_id,
            )
        )

    def __enter__(self) -> "_TemporaryRoleAssignment":
        caller = self.azure.invoke("ad", "signed-in-user", "show")
        principal_id = str(member(caller, "id", "") or "")
        if not principal_id:
            raise ScopeManagerError(
                "Could not resolve the signed-in caller's object id for a "
                "temporary Key Vault role assignment."
            )
        assignment_name = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(
                    (
                        self.subscription_id.casefold(),
                        self.vault_id.casefold(),
                        principal_id.casefold(),
                        self.operation_id.casefold(),
                    )
                ),
            )
        )
        existing = self._list(principal_id)
        if existing:
            owned = [
                item for item in existing if self._name(item) == assignment_name
            ]
            unrelated = [
                item for item in existing if self._name(item) != assignment_name
            ]
            state = self.journal.read(self.operation_id) or {}
            evidence = state.get("TemporaryRoleAssignment") or {}
            if unrelated or len(owned) != 1:
                raise ScopeManagerError(
                    "A pre-existing or ambiguous Key Vault Secrets Officer "
                    "assignment exists for this caller. Refusing to reuse or "
                    "remove access that is not proven operation-owned."
                )
            if (
                evidence.get("AssignmentName") != assignment_name
                or evidence.get("PrincipalId") != principal_id
                or not same_id(evidence.get("VaultId"), self.vault_id)
                or evidence.get("State")
                not in {"GRANT_PENDING", "GRANTED", "CLEANUP_INCOMPLETE"}
            ):
                raise ScopeManagerError(
                    "The deterministic temporary role assignment exists but "
                    "the operation journal does not prove ownership. It remains "
                    "untouched and blocks continuation."
                )
            self._assignment_id = str(member(owned[0], "id", "") or "")
            if not self._assignment_id:
                raise ScopeManagerError(
                    "The operation-owned role assignment has no resource id."
                )
            self._created = True
            _update_operation_journal(
                self.journal,
                self.operation_id,
                {
                    "TemporaryRoleAssignment": {
                        **evidence,
                        "State": "GRANTED",
                        "AssignmentId": self._assignment_id,
                        "Resumed": True,
                    }
                },
            )
            return self
        evidence = {
            "State": "GRANT_PENDING",
            "AssignmentName": assignment_name,
            "PrincipalId": principal_id,
            "VaultId": self.vault_id,
            "Role": ROLE_DEFINITION_NAME,
        }
        _update_operation_journal(
            self.journal,
            self.operation_id,
            {"TemporaryRoleAssignment": evidence},
        )
        assignment = self.azure.invoke(
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            principal_id,
            "--assignee-principal-type",
            "User",
            "--role",
            ROLE_DEFINITION_NAME,
            "--name",
            assignment_name,
            "--scope",
            self.vault_id,
            "--subscription",
            self.subscription_id,
        )
        assignment_id = str(member(assignment, "id", "") or "")
        if not assignment_id or self._name(assignment) != assignment_name:
            raise ScopeManagerError(
                "Role assignment creation did not return the deterministic "
                "operation-owned identity; refusing to proceed without a "
                "precise cleanup target."
            )
        self._assignment_id = assignment_id
        self._created = True
        _update_operation_journal(
            self.journal,
            self.operation_id,
            {
                "TemporaryRoleAssignment": {
                    **evidence,
                    "State": "GRANTED",
                    "AssignmentId": assignment_id,
                    "Resumed": False,
                }
            },
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._created and self._assignment_id is not None:
            assignment_id = self._assignment_id
            try:
                self.azure.invoke(
                    "role",
                    "assignment",
                    "delete",
                    "--ids",
                    assignment_id,
                    "--subscription",
                    self.subscription_id,
                )
                principal_id = str(
                    (self.journal.read(self.operation_id) or {})
                    .get("TemporaryRoleAssignment", {})
                    .get("PrincipalId", "")
                )
                if any(
                    same_id(member(item, "id"), assignment_id)
                    for item in self._list(principal_id)
                ):
                    raise ScopeManagerError(
                        "Temporary role assignment deletion was not confirmed "
                        "by read-back."
                    )
            except Exception as cleanup_exc:
                _update_operation_journal(
                    self.journal,
                    self.operation_id,
                    {
                        "State": "CLEANUP_INCOMPLETE",
                        "TemporaryRoleAssignment": {
                            **(
                                (self.journal.read(self.operation_id) or {}).get(
                                    "TemporaryRoleAssignment", {}
                                )
                            ),
                            "State": "CLEANUP_INCOMPLETE",
                            "AssignmentId": assignment_id,
                            "CleanupError": _redact(str(cleanup_exc)),
                        },
                    },
                )
                raise
            _update_operation_journal(
                self.journal,
                self.operation_id,
                {
                    "TemporaryRoleAssignment": {
                        **(
                            (self.journal.read(self.operation_id) or {}).get(
                                "TemporaryRoleAssignment", {}
                            )
                        ),
                        "State": "REVOKED",
                        "AssignmentId": assignment_id,
                    }
                },
            )
            self._assignment_id = None
            self._created = False
        return False


class SlackTokenManager:
    """Business logic for the Slack bot token lifecycle."""

    def __init__(
        self,
        azure: Any,
        azd: Any,
        environment_name: str | None = None,
        credential_factory: Callable[[], Any] = default_credential_factory,
        secret_client_factory: Callable[[str, Any], Any] = default_secret_client_factory,
        slack_client_factory: Callable[[str], Any] = default_slack_client_factory,
        prompt_token: Callable[[], str] = default_prompt_token,
        public_ip_resolver: Callable[[], str] = default_public_ip_resolver,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        resource_not_found_exception_factory: Callable[[], type[BaseException]] = (
            default_not_found_exception_factory
        ),
        acceptance_checker: Callable[[Any, dict[str, Any]], None] = (
            default_acceptance_checker
        ),
        provisioning_checker: Callable[[Any, dict[str, Any]], None] = (
            default_provisioning_checker
        ),
        active_deployment_checker: Callable[[Any, dict[str, Any]], bool] = (
            default_active_deployment_checker
        ),
    ) -> None:
        self.azure = azure
        self.azd = azd
        self.scope_manager = ScopeManager(azure, environment_name=environment_name)
        self.credential_factory = credential_factory
        self.secret_client_factory = secret_client_factory
        self.slack_client_factory = slack_client_factory
        self.prompt_token = prompt_token
        self.public_ip_resolver = public_ip_resolver
        self.clock = clock
        self.sleep = sleep
        self.resource_not_found_exception_factory = resource_not_found_exception_factory
        self.acceptance_checker = acceptance_checker
        self.provisioning_checker = provisioning_checker
        self.active_deployment_checker = active_deployment_checker
        self.requested_environment_name = environment_name
        if environment_name is not None and hasattr(
            self.azd, "environment_name"
        ):
            self.azd.environment_name = environment_name
        self._central: dict[str, Any] | None = None
        self._active_journal: OperationJournal | None = None
        self._active_operation_id: str | None = None

    def central(self) -> dict[str, Any]:
        if self._central is None:
            try:
                self._central = self.scope_manager.get_central_deployment()
            except ScopeManagerError:
                self._central = self._fallback_central()
        self._assert_selected_environment(self._central["EnvironmentName"])
        return self._central

    def _assert_selected_environment(self, expected: str) -> None:
        if not self.requested_environment_name:
            return
        selected = self.azd.get_environment_value(ENVIRONMENT_NAME_ENV_NAME)
        if not selected or not same_id(selected, expected):
            raise ScopeManagerError(
                f"Selected AZD environment '{selected or '<none>'}' does not "
                f"match requested environment '{expected}'."
            )

    def _require_mutation_environment(self) -> None:
        if not self.requested_environment_name:
            raise ScopeManagerError(
                "Mutating lifecycle commands require an explicit "
                "--environment-name."
            )
        selected = self.azd.get_environment_value(
            ENVIRONMENT_NAME_ENV_NAME
        )
        if not selected or not same_id(
            selected, self.requested_environment_name
        ):
            raise ScopeManagerError(
                f"Selected AZD environment '{selected or '<none>'}' does not "
                "match the required mutation environment "
                f"'{self.requested_environment_name}'."
            )

    def _fallback_central(self) -> dict[str, Any]:
        environment = (
            self.requested_environment_name
            or self.azd.get_environment_value(ENVIRONMENT_NAME_ENV_NAME)
        )
        subscription_id = self.azd.get_environment_value(
            SUBSCRIPTION_ID_ENV_NAME
        )
        tenant_id = self.azd.get_environment_value(TENANT_ID_ENV_NAME)
        if not environment or not subscription_id or not tenant_id:
            raise ScopeManagerError(
                "The selected AZD environment is missing nonsecret target metadata."
            )
        account = self.azure.invoke("account", "show")
        if (
            not same_id(member(account, "id"), subscription_id)
            or not same_id(member(account, "tenantId"), tenant_id)
        ):
            raise ScopeManagerError(
                "Azure CLI and the selected AZD environment target different "
                "subscriptions or tenants."
            )
        resource_group = (
            self.azd.get_environment_value(RESOURCE_GROUP_ENV_NAME)
            or f"rg-{environment}"
        )
        return {
            "EnvironmentName": environment,
            "TenantId": tenant_id,
            "SubscriptionId": subscription_id,
            "ResourceGroup": resource_group,
            "Location": self.azd.get_environment_value("AZURE_LOCATION"),
        }

    def _restart_active_revision(self, central: dict[str, Any]) -> None:
        container_app_id = str(central.get("ContainerAppId") or "")
        if not container_app_id:
            raise ScopeManagerError(
                "Cannot restart the active revision: Container App id is missing."
            )
        details = self.azure.invoke(
            "rest",
            "--method",
            "get",
            "--uri",
            f"https://management.azure.com{container_app_id}"
            f"?api-version={CONTAINER_APP_API_VERSION}",
        )
        revision = str(
            nested(details, "properties", "latestReadyRevisionName") or ""
        )
        if not revision:
            raise ScopeManagerError(
                "Cannot restart the active revision: no latest ready revision."
            )
        subscription_id, resource_group, app_name = resource_coordinates(
            container_app_id
        )
        self.azure.invoke(
            "containerapp",
            "revision",
            "restart",
            "--subscription",
            subscription_id,
            "--resource-group",
            resource_group,
            "--name",
            app_name,
            "--revision",
            revision,
        )

    def resolve_key_vault(self) -> dict[str, Any]:
        central = self.central()
        vaults = as_list(
            self.azure.invoke(
                "resource",
                "list",
                "--subscription",
                central["SubscriptionId"],
                "--resource-group",
                central["ResourceGroup"],
                "--resource-type",
                KEY_VAULT_RESOURCE_TYPE,
            )
        )
        tagged = [item for item in vaults if tag(item, "workload") == WORKLOAD_TAG]
        matches = tagged or vaults
        if len(matches) != 1:
            raise ScopeManagerError(
                "Could not uniquely resolve the central deployment's Key "
                f"Vault in resource group '{central['ResourceGroup']}'."
            )
        vault = matches[0]
        name = str(member(vault, "name", "") or "")
        vault_id = str(member(vault, "id", "") or "")
        if not name or not vault_id:
            raise ScopeManagerError(
                "Key Vault resource is missing a name or resource id."
            )
        details = self.azure.invoke(
            "keyvault",
            "show",
            "--name",
            name,
            "--subscription",
            central["SubscriptionId"],
        )
        uri = str(nested(details, "properties", "vaultUri") or "")
        if not uri:
            raise ScopeManagerError(f"Key Vault '{name}' has no vault URI.")
        return {"Name": name, "Id": vault_id, "Uri": uri}

    def _operation_lock(self) -> OperationLock:
        central = self.central()
        return OperationLock(
            self.azure, central["SubscriptionId"], central["ResourceGroup"]
        )

    def _lock_storage_exists(self) -> bool:
        central = self.central()
        resources = as_list(
            self.azure.invoke(
                "resource",
                "list",
                "--subscription",
                central["SubscriptionId"],
                "--resource-group",
                central["ResourceGroup"],
                "--resource-type",
                "Microsoft.Storage/storageAccounts",
            )
        )
        matches = [
            item
            for item in resources
            if tag(item, "service-health-purpose") == "operation-lock"
        ]
        if len(matches) > 1:
            raise ScopeManagerError(
                "Multiple operation-lock storage accounts were discovered."
            )
        return len(matches) == 1

    def _ensure_lock_infrastructure(self) -> None:
        if self._lock_storage_exists():
            return
        previous = self.azd.get_environment_value(
            DEPLOY_WORKLOAD_ENV_NAME
        )
        self.azd.set_environment_value(
            DEPLOY_WORKLOAD_ENV_NAME, "false"
        )
        try:
            self.azd.provision()
        finally:
            self.azd.set_environment_value(
                DEPLOY_WORKLOAD_ENV_NAME, previous or "true"
            )
        self._central = None
        if not self._lock_storage_exists():
            raise ScopeManagerError(
                "Infrastructure-only provision did not create the atomic "
                "operation-lock storage account."
            )

    def _operation_journal(self) -> OperationJournal:
        central = self.central()
        return OperationJournal(
            self.azure, central["SubscriptionId"], central["ResourceGroup"]
        )

    def _caller_identity(self) -> str:
        caller = self.azure.invoke("ad", "signed-in-user", "show")
        caller_id = str(member(caller, "id", "") or "")
        if not caller_id:
            raise ScopeManagerError(
                "Could not resolve the signed-in caller object id for the "
                "distributed operation lock."
            )
        return caller_id

    def _local_dotenv_path_optional(self) -> Path | None:
        central = self.central()
        return resolve_local_dotenv_path(self.azd, central["EnvironmentName"])

    def _local_dotenv_path(self) -> Path:
        path = self._local_dotenv_path_optional()
        if path is None:
            raise ScopeManagerError(
                "Could not resolve the local AZD dotenv file path for "
                f"environment '{self.central()['EnvironmentName']}'."
            )
        return path

    @staticmethod
    def _validate_token_format(token: str) -> None:
        if not TOKEN_FORMAT_PATTERN.match(token):
            raise ScopeManagerError(
                "The provided token does not match the expected 'xoxb-' bot "
                "token format."
            )

    def _validate_slack_identity(self, token: str) -> dict[str, str]:
        client = self.slack_client_factory(token)
        response = client.auth_test()
        team_id = _slack_field(response, "team_id")
        bot_user_id = _slack_field(response, "user_id")
        if not team_id or not bot_user_id:
            raise ScopeManagerError(
                "Slack authentication test did not return a team and bot "
                "user identity."
            )
        expected_team_id = self.azd.get_environment_value(EXPECTED_TEAM_ID_ENV_NAME)
        expected_bot_user_id = self.azd.get_environment_value(
            EXPECTED_BOT_USER_ID_ENV_NAME
        )
        if expected_team_id and expected_team_id != team_id:
            raise ScopeManagerError(
                "The provided token authenticates to an unexpected Slack team."
            )
        if expected_bot_user_id and expected_bot_user_id != bot_user_id:
            raise ScopeManagerError(
                "The provided token authenticates as an unexpected Slack bot user."
            )
        return {"team_id": team_id, "bot_user_id": bot_user_id}

    def _persist_identity_defaults(self, identity: dict[str, str]) -> None:
        if not self.azd.get_environment_value(EXPECTED_TEAM_ID_ENV_NAME):
            self.azd.set_environment_value(
                EXPECTED_TEAM_ID_ENV_NAME, identity["team_id"]
            )
        if not self.azd.get_environment_value(EXPECTED_BOT_USER_ID_ENV_NAME):
            self.azd.set_environment_value(
                EXPECTED_BOT_USER_ID_ENV_NAME, identity["bot_user_id"]
            )

    def assert_production_readiness(self) -> None:
        central = self.central()
        enforce_production_readiness(
            self.azd,
            central["EnvironmentName"],
            anchor_action_group_id=central.get("AnchorActionGroupId"),
            clock=self.clock,
        )

    @staticmethod
    def _secret_version(secret: Any) -> str:
        return str(getattr(getattr(secret, "properties", secret), "version", "") or "")

    def _current_secret_version(self, secret_client: Any) -> str:
        """Returns the currently-set secret's version, or "" if none
        exists yet. Only catches the injectable 'not found' exception type;
        any other error propagates rather than being silently swallowed."""
        not_found_type = self.resource_not_found_exception_factory()
        try:
            secret = secret_client.get_secret(SECRET_NAME)
        except not_found_type:
            return ""
        return self._secret_version(secret)

    def _verify_secret_metadata(self, secret_client: Any, expected_version: str) -> None:
        """Confirms the just-written secret's version and enabled state
        via `get_secret` metadata only; never reads or logs `.value`."""
        secret = secret_client.get_secret(SECRET_NAME, version=expected_version)
        properties = getattr(secret, "properties", secret)
        version = str(getattr(properties, "version", "") or "")
        enabled = getattr(properties, "enabled", True)
        if version != expected_version:
            raise ScopeManagerError(
                "Key Vault secret metadata verification failed: version "
                f"mismatch (expected '{expected_version}', got '{version}')."
            )
        if enabled is False:
            raise ScopeManagerError(
                "Key Vault secret metadata verification failed: the "
                "written secret version is disabled."
            )

    @staticmethod
    def _is_rbac_propagation_error(error: Exception) -> bool:
        status_code = getattr(error, "status_code", None)
        error_code = getattr(error, "error_code", None)
        return status_code == 403 or error_code in {
            "AuthorizationFailed",
            "Forbidden",
        }

    def _wait_for_secret_client(
        self,
        vault_uri: str,
        renew_lock: Callable[[], None],
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(len(RBAC_PROPAGATION_DELAYS) + 1):
            renew_lock()
            credential = self.credential_factory()
            secret_client = self.secret_client_factory(
                vault_uri, credential
            )
            try:
                iterator = iter(
                    secret_client.list_properties_of_secrets()
                )
                next(iterator, None)
                return secret_client
            except Exception as exc:
                if not self._is_rbac_propagation_error(exc):
                    raise
                last_error = exc
                if attempt == len(RBAC_PROPAGATION_DELAYS):
                    break
                self.sleep(RBAC_PROPAGATION_DELAYS[attempt])
        raise ScopeManagerError(
            "Temporary Key Vault Secrets Officer access did not propagate "
            "within the bounded 10-minute readiness window."
        ) from last_error

    def _rollback_after_acceptance_failure(
        self,
        operation: str,
        previous_version: str,
        original_error: Exception,
        renew_lock: Callable[[], None],
    ) -> None:
        if not previous_version:
            raise LifecycleStateError(
                "ROLLBACK_NOT_POSSIBLE",
                f"{operation} acceptance failed and no prior enabled secret "
                "version exists to pin. The new version was not reported as "
                f"accepted. {_redact(str(original_error))}",
            ) from None
        try:
            renew_lock()
            self.azd.set_environment_value(
                SECRET_VERSION_ENV_NAME, previous_version
            )
            self.azd.provision()
        except Exception as rollback_exc:
            raise LifecycleStateError(
                "ROLLBACK_FAILED",
                f"{operation} acceptance failed and the prior version could "
                "not be provisioned as an emergency pin. "
                f"{_redact(str(rollback_exc))}",
            ) from None
        try:
            self._central = None
            rollback_central = self.central()
            self._restart_active_revision(rollback_central)
            self.acceptance_checker(self.azure, rollback_central)
        except Exception as rollback_exc:
            raise LifecycleStateError(
                "INDETERMINATE",
                f"{operation} acceptance failed. The prior version pin was "
                "provisioned, but rollback verification failed. "
                f"{_redact(str(rollback_exc))}",
            ) from None
        raise LifecycleStateError(
            "ROLLBACK_PINNED",
            f"{operation} acceptance failed; the prior secret version was "
            "pinned and verified. The operation remains failed and requires "
            f"operator reconciliation. {_redact(str(original_error))}",
        ) from None

    def status(self) -> dict[str, Any]:
        """Entirely token-free: never calls `azd.get_environment_value`
        for the legacy token name, and touches neither the distributed lock
        nor the journal."""
        central = self.central()
        dotenv_path = self._local_dotenv_path_optional()
        legacy_present = local_dotenv_value_present(dotenv_path, LEGACY_TOKEN_ENV_NAME)
        migration_marker_set = (
            self.azd.get_environment_value(TOKEN_MIGRATION_MARKER_ENV_NAME) == "true"
        )
        secret_version = self.azd.get_environment_value(SECRET_VERSION_ENV_NAME)
        latest_secret_version = self.azd.get_environment_value(
            SECRET_LATEST_VERSION_ENV_NAME
        )
        previous_secret_version = self.azd.get_environment_value(
            PREVIOUS_SECRET_VERSION_ENV_NAME
        )
        try:
            vault_name = self.resolve_key_vault()["Name"]
        except ScopeManagerError:
            vault_name = None
        return {
            "Environment": central["EnvironmentName"],
            "KeyVaultName": vault_name,
            "SecretVersion": secret_version,
            "LatestSecretVersion": latest_secret_version,
            "PreviousSecretVersion": previous_secret_version,
            "LegacyTokenPresent": legacy_present,
            "MigrationMarkerSet": migration_marker_set,
            "Bootstrapped": bool(latest_secret_version) and not legacy_present,
        }

    def _mutate(
        self,
        command: str,
        target: str,
        action: Callable[[Callable[[], None]], dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_mutation_environment()
        central = self.central()
        lock = self._operation_lock()
        journal = self._operation_journal()
        operation_id = f"slack-token-{command}"
        handle = lock.acquire(
            environment=central["EnvironmentName"],
            command=command,
            target=target,
            caller=self._caller_identity(),
        )
        _update_operation_journal(
            journal,
            operation_id,
            {
                "Command": command,
                "Target": target,
                "StartedAt": handle.metadata["startedAt"],
                "LockNonce": handle.nonce,
                "State": "Started",
            },
        )
        try:
            lock.renew(handle)
            self._active_journal = journal
            self._active_operation_id = operation_id
            try:
                result = action(lambda: lock.renew(handle))
            finally:
                self._active_journal = None
                self._active_operation_id = None
        except Exception as exc:
            current_state = journal.read(operation_id) or {}
            cleanup_incomplete = (
                current_state.get("State") == "CLEANUP_INCOMPLETE"
            )
            _update_operation_journal(
                journal,
                operation_id,
                {
                    "Command": command,
                    "Target": target,
                    "StartedAt": handle.metadata["startedAt"],
                    "State": (
                        "CLEANUP_INCOMPLETE"
                        if cleanup_incomplete
                        else getattr(exc, "lifecycle_state", "Failed")
                    ),
                    "Error": _redact(str(exc)),
                },
            )
            # A lock-release failure here must never mask the original
            # mutation error; it is journaled separately and the ORIGINAL
            # exception is always what propagates via the bare `raise`.
            try:
                lock.release(handle)
            except Exception as release_exc:
                _update_operation_journal(
                    journal,
                    operation_id,
                    {
                        "Command": command,
                        "Target": target,
                        "StartedAt": handle.metadata["startedAt"],
                        "State": (
                            "CLEANUP_INCOMPLETE"
                            if cleanup_incomplete
                            else "FailedLockReleaseFailed"
                        ),
                        "LockReleaseState": "Failed",
                        "Error": _redact(
                            f"{exc}; lock release also failed: {release_exc}"
                        ),
                    },
                )
            raise
        _update_operation_journal(
            journal,
            operation_id,
            {
                "Command": command,
                "Target": target,
                "StartedAt": handle.metadata["startedAt"],
                "State": "Completed",
                "Result": result,
            },
        )
        # The journal entry is only cleared once the lock has genuinely
        # been released; a release failure surfaces explicitly (nonzero)
        # and the "Completed" journal record is left in place rather than
        # discarded.
        try:
            lock.release(handle)
        except Exception as release_exc:
            _update_operation_journal(
                journal,
                operation_id,
                {
                    "Command": command,
                    "Target": target,
                    "StartedAt": handle.metadata["startedAt"],
                    "State": "CompletedLockReleaseFailed",
                    "Error": _redact(str(release_exc)),
                },
            )
            raise
        self._clear_completed_journal(
            lock,
            journal,
            operation_id,
            handle,
            command,
            target,
        )
        return result

    def _clear_completed_journal(
        self,
        lock: OperationLock,
        journal: OperationJournal,
        operation_id: str,
        completed_handle: Any,
        command: str,
        target: str,
    ) -> None:
        try:
            cleanup_handle = lock.acquire(
                environment=self.central()["EnvironmentName"],
                command=f"{command}-journal-cleanup",
                target=target,
                caller=self._caller_identity(),
            )
        except OperationLockError:
            # A successor already owns the lock and may have replaced the
            # deterministic journal. Never race its state.
            return
        try:
            state = journal.read(operation_id)
            if (
                state
                and state.get("LockNonce") == completed_handle.nonce
                and state.get("State") == "Completed"
            ):
                journal.clear(operation_id)
        finally:
            try:
                lock.release(cleanup_handle)
            except Exception as release_exc:
                _update_operation_journal(
                    journal,
                    operation_id,
                    {
                        "Command": command,
                        "Target": target,
                        "LockNonce": cleanup_handle.nonce,
                        "State": "CompletedJournalCleanupLockReleaseFailed",
                        "Error": _redact(str(release_exc)),
                    },
                )
                raise

    def bootstrap(self) -> dict[str, Any]:
        """Genuinely two-phase, disabled-first bootstrap for a brand-new
        environment:

        Phase 1 (unlocked -- no central resource group/lock exists yet):
        force the workload and baseline alert off, then run a token-free
        `azd provision` so the core landing-zone infra (resource group, Key
        Vault) exists to resolve against.

        Phase 2 (lock+journal wrapped, via `_mutate`): prompt for and
        validate the token, write it to Key Vault, verify its metadata,
        enable the workload (baseline alert stays disabled -- that remains
        an explicit, separate operator decision), clear the emergency
        version pin, provision again, and run the acceptance check.
        """
        self._require_mutation_environment()
        current_latest = self.azd.get_environment_value(SECRET_LATEST_VERSION_ENV_NAME)
        if current_latest:
            return {
                "Status": "AlreadyBootstrapped",
                "SecretVersion": current_latest,
            }

        self.azd.set_environment_value(DEPLOY_WORKLOAD_ENV_NAME, "false")
        self.azd.set_environment_value(BASELINE_ALERT_ENV_NAME, "false")
        self.azd.provision()
        self.central()
        self.assert_production_readiness()
        sensitive = [self.prompt_token()]

        def action(
            renew_lock: Callable[[], None],
            public_ip: str,
        ) -> dict[str, Any]:
            central = self.central()
            try:
                identity = self._validate_slack_identity(sensitive[0])
                renew_lock()
                vault = self.resolve_key_vault()
                with _TemporaryVaultNetworkAccess(
                    self.azure,
                    vault["Name"],
                    central["SubscriptionId"],
                    lambda: public_ip,
                    self._active_journal,
                    self._active_operation_id,
                ):
                    with _TemporaryRoleAssignment(
                        self.azure,
                        vault["Id"],
                        central["SubscriptionId"],
                        self._active_journal,
                        self._active_operation_id,
                    ):
                        secret_client = self._wait_for_secret_client(
                            vault["Uri"], renew_lock
                        )
                        secret = secret_client.set_secret(
                            SECRET_NAME, sensitive[0]
                        )
                        version = self._secret_version(secret)
                        self._verify_secret_metadata(secret_client, version)
            except Exception as exc:
                raise ScopeManagerError(_redact(str(exc))) from None
            if not version:
                raise ScopeManagerError("Key Vault did not return a secret version.")
            renew_lock()
            self.assert_production_readiness()
            self._persist_identity_defaults(identity)
            self.azd.set_environment_value(SECRET_LATEST_VERSION_ENV_NAME, version)
            self.azd.set_environment_value(SECRET_VERSION_ENV_NAME, "")
            self.azd.set_environment_value(TOKEN_MIGRATION_MARKER_ENV_NAME, "true")
            self.azd.set_environment_value(DEPLOY_WORKLOAD_ENV_NAME, "true")
            self.azd.provision()
            self._central = None
            provisioned_central = self.central()
            self.provisioning_checker(self.azure, provisioned_central)
            return {
                "Status": "Bootstrapped",
                "SecretVersion": version,
                "KeyVaultName": vault["Name"],
            }

        try:
            self._validate_token_format(sensitive[0])
            public_ip = self.public_ip_resolver()
            return self._mutate(
                "bootstrap",
                f"Key Vault secret '{SECRET_NAME}'",
                lambda renew: action(renew, public_ip),
            )
        finally:
            sensitive.clear()

    def migrate(self, dotenv_path: Path | None = None) -> dict[str, Any]:
        """Migrates an existing plaintext local token to Key Vault. The
        migration marker is set only *after* the Key Vault write is
        verified, the temporary network/RBAC access is fully and verifiably
        restored, the local token line is removed, and the removal is
        re-verified -- never before, so a failed/interrupted migration
        never bypasses the fail-closed legacy-token provisioning check
        while plaintext still exists somewhere."""

        self._require_mutation_environment()
        self.central()
        self._ensure_lock_infrastructure()
        path = dotenv_path or self._local_dotenv_path()
        if not local_dotenv_value_present(path, LEGACY_TOKEN_ENV_NAME):
            raise ScopeManagerError(
                f"No local {LEGACY_TOKEN_ENV_NAME} value exists to migrate."
            )
        self.assert_production_readiness()
        public_ip = self.public_ip_resolver()

        def action(renew_lock: Callable[[], None]) -> dict[str, Any]:
            central = self.central()
            lock_path = path.with_name(f"{path.name}.migrate.lock")
            with _LocalFileLock(lock_path):
                token = read_local_token(path)
                try:
                    try:
                        self._validate_token_format(token)
                        identity = self._validate_slack_identity(token)
                        renew_lock()
                        vault = self.resolve_key_vault()
                        with _TemporaryVaultNetworkAccess(
                            self.azure,
                            vault["Name"],
                            central["SubscriptionId"],
                            lambda: public_ip,
                            self._active_journal,
                            self._active_operation_id,
                        ):
                            with _TemporaryRoleAssignment(
                                self.azure,
                                vault["Id"],
                                central["SubscriptionId"],
                                self._active_journal,
                                self._active_operation_id,
                            ):
                                secret_client = self._wait_for_secret_client(
                                    vault["Uri"], renew_lock
                                )
                                previous_version = self._current_secret_version(
                                    secret_client
                                )
                                secret = secret_client.set_secret(SECRET_NAME, token)
                                version = self._secret_version(secret)
                                self._verify_secret_metadata(secret_client, version)
                        if not version:
                            raise ScopeManagerError(
                                "Key Vault did not return a secret version."
                            )
                        removed = remove_local_token_line(path)
                        if not removed:
                            raise ScopeManagerError(
                                "Local token removal did not find the "
                                "expected line to remove."
                            )
                        if local_dotenv_value_present(path, LEGACY_TOKEN_ENV_NAME):
                            raise ScopeManagerError(
                                "Local legacy token still present after "
                                "removal; refusing to mark migration "
                                "complete."
                            )
                    except Exception as exc:
                        raise ScopeManagerError(_redact(str(exc))) from None
                finally:
                    del token
            # Only now -- after the temporary access is verifiably closed,
            # the local token line is removed, and its absence is
            # re-verified -- is any persisted state changed.
            renew_lock()
            self.assert_production_readiness()
            self._persist_identity_defaults(identity)
            if previous_version:
                self.azd.set_environment_value(
                    PREVIOUS_SECRET_VERSION_ENV_NAME, previous_version
                )
            self.azd.set_environment_value(SECRET_LATEST_VERSION_ENV_NAME, version)
            self.azd.set_environment_value(SECRET_VERSION_ENV_NAME, "")
            self.azd.set_environment_value(TOKEN_MIGRATION_MARKER_ENV_NAME, "true")
            if not self.azd.get_environment_value(DEPLOY_WORKLOAD_ENV_NAME):
                self.azd.set_environment_value(DEPLOY_WORKLOAD_ENV_NAME, "true")
            if not self.azd.get_environment_value(BASELINE_ALERT_ENV_NAME):
                self.azd.set_environment_value(
                    BASELINE_ALERT_ENV_NAME,
                    "true" if central.get("ProtectedAlertEnabled") else "false",
                )
            self.azd.provision()
            try:
                self._central = None
                accepted_central = self.central()
                self._restart_active_revision(accepted_central)
                self.acceptance_checker(self.azure, accepted_central)
            except Exception as exc:
                self._rollback_after_acceptance_failure(
                    "Migration", previous_version, exc, renew_lock
                )
            return {
                "Status": "Migrated",
                "SecretVersion": version,
                "KeyVaultName": vault["Name"],
                "LocalTokenRemoved": removed,
            }

        return self._mutate("migrate", f"Key Vault secret '{SECRET_NAME}'", action)

    def rotate(self) -> dict[str, Any]:
        """Validates before writing, retains the prior version for
        rollback, provisions token-free, and runs the acceptance check --
        rolling back to the prior version (via the emergency pin) and
        re-provisioning if acceptance fails, rather than ever falsely
        reporting a completed rotation."""

        self._require_mutation_environment()
        self.central()
        self.assert_production_readiness()
        sensitive = [self.prompt_token()]

        def action(
            renew_lock: Callable[[], None],
            public_ip: str,
        ) -> dict[str, Any]:
            central = self.central()
            try:
                identity = self._validate_slack_identity(sensitive[0])
                renew_lock()
                vault = self.resolve_key_vault()
                with _TemporaryVaultNetworkAccess(
                    self.azure,
                    vault["Name"],
                    central["SubscriptionId"],
                    lambda: public_ip,
                    self._active_journal,
                    self._active_operation_id,
                ):
                    with _TemporaryRoleAssignment(
                        self.azure,
                        vault["Id"],
                        central["SubscriptionId"],
                        self._active_journal,
                        self._active_operation_id,
                    ):
                        secret_client = self._wait_for_secret_client(
                            vault["Uri"], renew_lock
                        )
                        previous_version = self._current_secret_version(secret_client)
                        secret = secret_client.set_secret(
                            SECRET_NAME, sensitive[0]
                        )
                        version = self._secret_version(secret)
                        self._verify_secret_metadata(secret_client, version)
            except Exception as exc:
                raise ScopeManagerError(_redact(str(exc))) from None
            if not version:
                raise ScopeManagerError("Key Vault did not return a secret version.")
            renew_lock()
            self._persist_identity_defaults(identity)
            if previous_version:
                self.azd.set_environment_value(
                    PREVIOUS_SECRET_VERSION_ENV_NAME, previous_version
                )
            self.azd.set_environment_value(SECRET_LATEST_VERSION_ENV_NAME, version)
            self.azd.set_environment_value(SECRET_VERSION_ENV_NAME, "")
            self.azd.provision()
            try:
                self._central = None
                accepted_central = self.central()
                self._restart_active_revision(accepted_central)
                self.acceptance_checker(self.azure, accepted_central)
            except Exception as exc:
                self._rollback_after_acceptance_failure(
                    "Rotation", previous_version, exc, renew_lock
                )
            return {
                "Status": "Rotated",
                "SecretVersion": version,
                "PreviousSecretVersion": previous_version,
                "KeyVaultName": vault["Name"],
            }

        try:
            self._validate_token_format(sensitive[0])
            public_ip = self.public_ip_resolver()
            return self._mutate(
                "rotate",
                f"Key Vault secret '{SECRET_NAME}'",
                lambda renew: action(renew, public_ip),
            )
        finally:
            sensitive.clear()

    def rollback(self) -> dict[str, Any]:
        """Sets only the emergency version pin, then runs a token-free
        provision and the acceptance check -- never touches Key Vault,
        network, or RBAC state directly."""

        def action(renew_lock: Callable[[], None]) -> dict[str, Any]:
            self.central()
            previous_version = self.azd.get_environment_value(
                PREVIOUS_SECRET_VERSION_ENV_NAME
            )
            if not previous_version:
                raise LifecycleStateError(
                    "ROLLBACK_NOT_POSSIBLE",
                    "No prior secret version is recorded; there is nothing "
                    "to pin.",
                )
            try:
                renew_lock()
                self.azd.set_environment_value(
                    SECRET_VERSION_ENV_NAME, previous_version
                )
                self.azd.provision()
            except Exception as exc:
                raise LifecycleStateError(
                    "ROLLBACK_FAILED",
                    "The prior version could not be provisioned as an "
                    f"emergency pin. {_redact(str(exc))}",
                ) from None
            try:
                self._central = None
                accepted_central = self.central()
                self._restart_active_revision(accepted_central)
                self.acceptance_checker(self.azure, accepted_central)
            except Exception as exc:
                raise LifecycleStateError(
                    "INDETERMINATE",
                    "The prior version is pinned, but rollback verification "
                    f"failed. {_redact(str(exc))}",
                ) from None
            return {"Status": "ROLLED_BACK", "SecretVersion": previous_version}

        return self._mutate("rollback", f"Key Vault secret '{SECRET_NAME}'", action)

    def recover_lock(self, force: bool) -> dict[str, Any]:
        self._require_mutation_environment()
        central = self.central()
        status = self._operation_lock().status(
            expected_environment=central["EnvironmentName"]
        )
        if status["Status"] == "Active":
            raise ScopeManagerError(
                "Refusing to recover the operation lock: the read-only status "
                "check reports an active lease."
            )
        if self.active_deployment_checker(self.azure, central):
            raise ScopeManagerError(
                "Refusing to recover the operation lock: an ARM deployment "
                "in the central resource group is still actively running."
            )
        return self._operation_lock().recover(
            force=force, expected_environment=central["EnvironmentName"]
        )

    def lock_status(self) -> dict[str, Any]:
        central = self.central()
        return self._operation_lock().status(
            expected_environment=central["EnvironmentName"]
        )


def format_text(value: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value[key]}" for key in sorted(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage the Slack bot token lifecycle in Azure Key Vault without "
            "ever exposing the token value at a process boundary."
        )
    )
    parser.add_argument(
        "command",
        choices=(
            "status",
            "bootstrap",
            "migrate",
            "rotate",
            "rollback",
            "lock-status",
            "recover-lock",
        ),
    )
    parser.add_argument("--environment-name")
    parser.add_argument(
        "--dotenv-path",
        help="Override the local AZD dotenv file path used by 'migrate'.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Approve explicit, operator-confirmed lock recovery.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        azure = AzureCli()
        azd = AzdCli()
        manager = SlackTokenManager(
            azure, azd, environment_name=args.environment_name
        )
        if args.command == "status":
            result = manager.status()
        elif args.command == "bootstrap":
            result = manager.bootstrap()
        elif args.command == "migrate":
            dotenv_path = Path(args.dotenv_path) if args.dotenv_path else None
            result = manager.migrate(dotenv_path)
        elif args.command == "rotate":
            result = manager.rotate()
        elif args.command == "rollback":
            result = manager.rollback()
        elif args.command == "lock-status":
            result = manager.lock_status()
        else:
            result = manager.recover_lock(force=args.force)
    except (ScopeManagerError, OperationLockError, KeyboardInterrupt) as exc:
        print(f"ERROR: {_redact(str(exc))}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
