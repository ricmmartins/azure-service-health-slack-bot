#!/usr/bin/env python3
"""Configure the Entra application used by Azure Monitor Secure Webhooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

try:
    from scripts.manage_alert_scopes import (
        AzureCli,
        SLACK_TOKEN_ENV_ALIASES,
        ScopeManagerError,
        assert_no_slack_secret_material,
        as_list,
        member,
        sanitized_child_environment,
        same_id,
    )
except ModuleNotFoundError:
    from manage_alert_scopes import (  # type: ignore[no-redef]
        AzureCli,
        SLACK_TOKEN_ENV_ALIASES,
        ScopeManagerError,
        assert_no_slack_secret_material,
        as_list,
        member,
        sanitized_child_environment,
        same_id,
    )


DEFAULT_AZNS_APPLICATION_ID = "461e8683-5575-4561-ac7f-899cc907d62a"
DEFAULT_ROLE_NAME = "ActionGroupsSecureWebhook"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MAX_GRAPH_PAGES = 100
READ_ONLY_PREVIEW_ENV_NAME = "SERVICE_HEALTH_READ_ONLY_PREVIEW"

# Nonsecret AZD defaults this hook must initialize exactly once (never
# overwriting an operator- or tool-persisted value). Disabled-first: a
# brand-new environment must not deploy the workload or the baseline
# alert until an operator has explicitly bootstrapped or migrated a
# Slack token.
NONSECRET_AZD_DEFAULTS = {
    "SERVICE_HEALTH_DEPLOY_WORKLOAD": "false",
    "SERVICE_HEALTH_BASELINE_ALERT_ENABLED": "false",
    "SERVICE_HEALTH_SECRET_VERSION": "",
    "SERVICE_HEALTH_SECRET_LATEST_VERSION": "",
    "SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID": "",
}
LEGACY_TOKEN_ENV_NAME = "SLACK_BOT_TOKEN"
TOKEN_MIGRATION_MARKER_ENV_NAME = "SERVICE_HEALTH_TOKEN_MIGRATION_MARKER"
ENVIRONMENT_CLASS_ENV_NAME = "SERVICE_HEALTH_ENVIRONMENT_CLASS"
OPERATIONS_ACTION_GROUP_ENV_NAME = (
    "SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID"
)
OPERATIONS_PRIMARY_OWNER_ENV_NAME = (
    "SERVICE_HEALTH_OPERATIONS_PRIMARY_OWNER"
)
OPERATIONS_BACKUP_OWNER_ENV_NAME = (
    "SERVICE_HEALTH_OPERATIONS_BACKUP_OWNER"
)
OPERATIONS_ON_CALL_ENV_NAME = (
    "SERVICE_HEALTH_OPERATIONS_ON_CALL_DESTINATION"
)
OPERATIONS_RUNBOOK_ENV_NAME = "SERVICE_HEALTH_OPERATIONS_RUNBOOK_URI"
OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME = (
    "SERVICE_HEALTH_OPERATIONS_RECEIVER_TEST_EVIDENCE"
)
MAX_RECEIVER_EVIDENCE_AGE_SECONDS = 90 * 24 * 60 * 60
MAX_RECEIVER_CLOCK_SKEW_SECONDS = 5 * 60
PREVIEW_REQUIRED_NONEMPTY_VALUES = frozenset(
    {
        "AZURE_ENV_NAME",
        "AZURE_LOCATION",
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_TENANT_ID",
        "SERVICE_HEALTH_API_CLIENT_ID",
        "SERVICE_HEALTH_API_IDENTIFIER_URI",
        "SERVICE_HEALTH_API_OBJECT_ID",
        "SERVICE_HEALTH_BASELINE_ALERT_ENABLED",
        "SERVICE_HEALTH_DEPLOY_WORKLOAD",
        "SERVICE_HEALTH_ROUTES_JSON_B64",
    }
)
PREVIEW_REQUIRED_PRESENT_VALUES = frozenset(
    {
        "SERVICE_HEALTH_OPERATIONS_ACTION_GROUP_ID",
        "SERVICE_HEALTH_SECRET_VERSION",
    }
)
# SERVICE_APP_RESOURCE_EXISTS is intentionally absent: AZD computes that
# service-state signal for each provision instead of persisting it in dotenv.
PREVIEW_LOCAL_VALUES = (
    PREVIEW_REQUIRED_NONEMPTY_VALUES
    | PREVIEW_REQUIRED_PRESENT_VALUES
    | frozenset(
        {
            ENVIRONMENT_CLASS_ENV_NAME,
            "AZURE_RESOURCE_GROUP",
            OPERATIONS_PRIMARY_OWNER_ENV_NAME,
            OPERATIONS_BACKUP_OWNER_ENV_NAME,
            OPERATIONS_ON_CALL_ENV_NAME,
            OPERATIONS_RUNBOOK_ENV_NAME,
            OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME,
        }
    )
)


class AzdCli:
    """Fail-closed Azure Developer CLI boundary."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        environment_name: str | None = None,
    ) -> None:
        self.runner = runner
        self.environment_name = environment_name
        self.executable = (
            shutil.which("azd") if runner is subprocess.run else "azd"
        )

    def set_environment_value(self, name: str, value: str) -> None:
        if self.executable is None:
            raise ScopeManagerError(
                "Azure Developer CLI is required. Install it, run 'azd auth login', and retry."
            )
        command = [self.executable, "env", "set", name, value]
        if self.environment_name:
            command.extend(
                ["--environment", self.environment_name, "--no-prompt"]
            )
        if name.upper() in SLACK_TOKEN_ENV_ALIASES:
            raise ScopeManagerError(
                "Slack credential variables cannot be written through AZD."
            )
        assert_no_slack_secret_material(command)
        result = self.runner(
            command,
            env=sanitized_child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = "\n".join(
                text.strip()
                for text in (result.stdout, result.stderr)
                if text.strip()
            )
            if value:
                detail = detail.replace(value, "<value>")
            raise ScopeManagerError(
                f"Azure Developer CLI command failed: azd env set {name} <value>\n{detail}"
            )

    def get_environment_value(self, name: str) -> str:
        if self.executable is None:
            raise ScopeManagerError(
                "Azure Developer CLI is required. Install it, run "
                "'azd auth login', and retry."
            )
        if name.upper() in SLACK_TOKEN_ENV_ALIASES:
            raise ScopeManagerError(
                "Slack credential variables cannot be read through AZD."
            )
        command = [
            self.executable,
            "env",
            "get-value",
            name,
            "--no-prompt",
        ]
        if self.environment_name:
            command.extend(["--environment", self.environment_name])
        assert_no_slack_secret_material(command)
        result = self.runner(
            command,
            env=sanitized_child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        detail = "\n".join(
            text.strip()
            for text in (result.stdout, result.stderr)
            if text.strip()
        )
        if "key not found in environment values" in detail.casefold():
            return ""
        raise ScopeManagerError(
            "Azure Developer CLI command failed: "
            f"azd env get-value {name} --no-prompt\n{detail}"
        )

    def list_environments(self) -> list[dict[str, Any]]:
        """Nonsecret AZD environment metadata only (names and local
        dotenv file paths); never used to read or transport secret
        values."""
        if self.executable is None:
            raise ScopeManagerError(
                "Azure Developer CLI is required. Install it, run "
                "'azd auth login', and retry."
            )
        command = [self.executable, "env", "list", "--output", "json"]
        result = self.runner(
            command,
            env=sanitized_child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = "\n".join(
                text.strip()
                for text in (result.stdout, result.stderr)
                if text.strip()
            )
            raise ScopeManagerError(
                "Azure Developer CLI command failed: azd env list --output json\n"
                f"{detail}"
            )
        text_value = result.stdout.strip()
        if not text_value:
            return []
        try:
            parsed = json.loads(text_value)
        except json.JSONDecodeError as exc:
            raise ScopeManagerError(
                "Azure Developer CLI returned invalid JSON for: "
                "azd env list --output json"
            ) from exc
        return as_list(parsed)

    def provision(self) -> None:
        """Trigger a token-free provision. Never receives a secret
        value: Container Apps resolve the Slack token from Key Vault at
        runtime, not from an AZD-managed environment variable."""
        if self.executable is None:
            raise ScopeManagerError(
                "Azure Developer CLI is required. Install it, run "
                "'azd auth login', and retry."
            )
        command = [self.executable, "provision", "--no-prompt"]
        if not self.environment_name:
            raise ScopeManagerError(
                "AZD provision requires an explicit environment name."
            )
        command.extend(["--environment", self.environment_name])
        result = self.runner(
            command,
            env=sanitized_child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = "\n".join(
                text.strip()
                for text in (result.stdout, result.stderr)
                if text.strip()
            )
            raise ScopeManagerError(
                f"Azure Developer CLI command failed: azd provision --no-prompt\n{detail}"
            )


class EnvironmentValues(Protocol):
    def get_environment_value(self, name: str) -> str:
        ...


def enforce_production_readiness(
    azd: EnvironmentValues,
    environment_name: str,
    *,
    anchor_action_group_id: str | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    environment_class = azd.get_environment_value(
        ENVIRONMENT_CLASS_ENV_NAME
    ).strip()
    is_production = (
        environment_class.casefold() == "production"
        if environment_class
        else bool(
            re.search(
                r"(^|[-_])prod(?:uction)?($|[-_])",
                environment_name.casefold(),
            )
        )
    )
    if not is_production:
        return
    names = (
        OPERATIONS_ACTION_GROUP_ENV_NAME,
        OPERATIONS_PRIMARY_OWNER_ENV_NAME,
        OPERATIONS_BACKUP_OWNER_ENV_NAME,
        OPERATIONS_ON_CALL_ENV_NAME,
        OPERATIONS_RUNBOOK_ENV_NAME,
        OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME,
    )
    values = {
        name: azd.get_environment_value(name).strip()
        for name in names
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ScopeManagerError(
            "Production monitoring readiness is incomplete; configure "
            f"the following nonsecret values: {', '.join(missing)}."
        )
    action_group_id = values[OPERATIONS_ACTION_GROUP_ENV_NAME]
    if (
        "/providers/microsoft.insights/actiongroups/"
        not in action_group_id.casefold()
        or (
            anchor_action_group_id
            and same_id(action_group_id, anchor_action_group_id)
        )
    ):
        raise ScopeManagerError(
            "Production monitoring requires an independent operations "
            "Action Group resource id outside the bot's delivery path."
        )
    if same_id(
        values[OPERATIONS_PRIMARY_OWNER_ENV_NAME],
        values[OPERATIONS_BACKUP_OWNER_ENV_NAME],
    ):
        raise ScopeManagerError(
            "Production monitoring primary and backup owners must be "
            "different."
        )
    runbook = urlparse(values[OPERATIONS_RUNBOOK_ENV_NAME])
    if runbook.scheme.casefold() != "https" or not runbook.netloc:
        raise ScopeManagerError(
            "Production monitoring runbook metadata must be an HTTPS URI."
        )
    try:
        evidence = json.loads(
            values[OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME]
        )
        tested_at = datetime.fromisoformat(
            str(evidence["testedAt"]).replace("Z", "+00:00")
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ScopeManagerError(
            "Production monitoring receiver-test evidence must be JSON "
            "with a valid testedAt timestamp."
        ) from exc
    if (
        not isinstance(evidence, dict)
        or str(evidence.get("status", "")).casefold() != "succeeded"
        or not same_id(evidence.get("actionGroupId"), action_group_id)
        or tested_at.tzinfo is None
    ):
        raise ScopeManagerError(
            "Production monitoring receiver-test evidence must record a "
            "successful test for the configured Action Group."
        )
    age = clock() - tested_at.astimezone(timezone.utc).timestamp()
    if (
        age < -MAX_RECEIVER_CLOCK_SKEW_SECONDS
        or age > MAX_RECEIVER_EVIDENCE_AGE_SECONDS
    ):
        raise ScopeManagerError(
            "Production monitoring receiver-test evidence is expired or "
            "future-dated; record a successful test from the last 90 days."
        )


def expected_anchor_action_group_id(
    azd: EnvironmentValues, environment_name: str
) -> str:
    subscription_id = azd.get_environment_value(
        "AZURE_SUBSCRIPTION_ID"
    ).strip()
    resource_group = azd.get_environment_value(
        "AZURE_RESOURCE_GROUP"
    ).strip()
    if not subscription_id or not resource_group:
        raise ScopeManagerError(
            "Production workload enablement requires the exact AZD "
            "subscription and resource-group outputs before readiness can "
            "prove the operations Action Group is independent."
        )
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        "/providers/Microsoft.Insights/actionGroups/"
        f"ag-{environment_name}-service-health"
    )


def resolve_local_dotenv_path(
    azd: AzdCli, environment_name: str
) -> Path | None:
    """Nonsecret AZD environment metadata lookup only (environment
    name and local dotenv file path); never reads or transports a
    secret value. Returns None if the environment cannot be resolved,
    which callers must treat as 'unknown', not 'absent'."""
    if not environment_name:
        return None
    for entry in azd.list_environments():
        name = member(entry, "Name") or member(entry, "name")
        if name and same_id(name, environment_name):
            raw = member(entry, "DotEnvPath") or member(entry, "dotEnvPath")
            if raw:
                return Path(str(raw))
    return None


def read_only_preview_enabled(
    environment: dict[str, str] | None = None,
) -> bool:
    source = os.environ if environment is None else environment
    if READ_ONLY_PREVIEW_ENV_NAME not in source:
        return False
    if source[READ_ONLY_PREVIEW_ENV_NAME].strip().casefold() == "true":
        return True
    raise ScopeManagerError(
        f"{READ_ONLY_PREVIEW_ENV_NAME} must be omitted or set exactly to true."
    )


def resolve_preview_dotenv_path(
    environment_name: str,
    project_root: Path,
) -> Path:
    if (
        not environment_name
        or Path(environment_name).name != environment_name
        or environment_name in {".", ".."}
    ):
        raise ScopeManagerError(
            "Read-only preview requires a valid explicit AZD environment name."
        )
    azure_directory = project_root / ".azure"
    if azure_directory.is_symlink() or not azure_directory.is_dir():
        raise ScopeManagerError(
            "The local .azure directory is missing; cannot resolve the selected "
            "AZD environment dotenv file."
        )
    matches = [
        path
        for path in azure_directory.iterdir()
        if path.is_dir() and same_id(path.name, environment_name)
    ]
    if len(matches) != 1:
        raise ScopeManagerError(
            "Read-only preview must resolve exactly one local dotenv directory "
            "for the selected AZD environment."
        )
    environment_directory = matches[0]
    dotenv_path = environment_directory / ".env"
    if (
        environment_directory.is_symlink()
        or dotenv_path.is_symlink()
        or not dotenv_path.is_file()
    ):
        raise ScopeManagerError(
            "The selected AZD environment must have one regular local .env file."
        )
    return dotenv_path


def read_preview_dotenv_values(dotenv_path: Path) -> dict[str, str]:
    """Read only approved nonsecret values, rejecting Slack keys before values."""
    values: dict[str, str] = {}
    with dotenv_path.open("r", encoding="utf-8", newline="") as handle:
        while True:
            key_characters: list[str] = []
            delimiter = ""
            while True:
                character = handle.read(1)
                if not character:
                    delimiter = ""
                    break
                if character in {"=", "\n"}:
                    delimiter = character
                    break
                key_characters.append(character)
            if not key_characters and not delimiter:
                break
            raw_key = "".join(key_characters).strip()
            if raw_key.startswith("export "):
                raw_key = raw_key[7:].strip()
            if delimiter != "=":
                if not delimiter:
                    break
                continue
            if raw_key.upper() in SLACK_TOKEN_ENV_ALIASES:
                raise ScopeManagerError(
                    "A Slack credential entry is present in the selected local "
                    "AZD dotenv file; read-only preview refuses to read its value."
                )
            value_characters: list[str] = []
            while True:
                character = handle.read(1)
                if not character or character == "\n":
                    break
                value_characters.append(character)
            if raw_key in PREVIEW_LOCAL_VALUES:
                if raw_key in values:
                    raise ScopeManagerError(
                        "The selected AZD dotenv file contains duplicate required "
                        "nonsecret entries."
                    )
                values[raw_key] = parse_dotenv_value(
                    "".join(value_characters).rstrip("\r")
                )
            if not character:
                break
    return values


class LocalPreviewValues:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get_environment_value(self, name: str) -> str:
        return self.values.get(name, "")


def validate_read_only_preview(
    environment_name: str,
    *,
    project_root: Path,
    azure: AzureCli,
) -> None:
    dotenv_path = resolve_preview_dotenv_path(
        environment_name,
        project_root,
    )
    values = read_preview_dotenv_values(dotenv_path)
    missing = sorted(
        name
        for name in PREVIEW_REQUIRED_NONEMPTY_VALUES
        if not values.get(name, "").strip()
    )
    missing.extend(
        sorted(
            name
            for name in PREVIEW_REQUIRED_PRESENT_VALUES
            if name not in values
        )
    )
    if missing:
        raise ScopeManagerError(
            "Read-only preview is missing required nonsecret deployment "
            f"values: {', '.join(missing)}."
        )
    if values["AZURE_ENV_NAME"] != environment_name:
        raise ScopeManagerError(
            "The selected local dotenv AZURE_ENV_NAME does not exactly match "
            "the requested environment."
        )
    for name in (
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_TENANT_ID",
        "SERVICE_HEALTH_API_CLIENT_ID",
        "SERVICE_HEALTH_API_OBJECT_ID",
    ):
        try:
            parsed = uuid.UUID(values[name])
        except ValueError as exc:
            raise ScopeManagerError(
                f"Read-only preview requires {name} to be a UUID."
            ) from exc
        if str(parsed) != values[name].casefold():
            raise ScopeManagerError(
                f"Read-only preview requires {name} to use canonical UUID "
                "format."
            )
    expected_identifier = (
        f"api://{values['SERVICE_HEALTH_API_CLIENT_ID']}"
    )
    if values["SERVICE_HEALTH_API_IDENTIFIER_URI"] != expected_identifier:
        raise ScopeManagerError(
            "The persisted Secure Webhook identifier URI must exactly match "
            "the persisted client id."
        )
    for name in (
        "SERVICE_HEALTH_DEPLOY_WORKLOAD",
        "SERVICE_HEALTH_BASELINE_ALERT_ENABLED",
    ):
        if values[name].casefold() not in {"true", "false"}:
            raise ScopeManagerError(
                f"Read-only preview requires {name} to be true or false."
            )

    local_values = LocalPreviewValues(values)
    if values["SERVICE_HEALTH_DEPLOY_WORKLOAD"].casefold() == "true":
        enforce_production_readiness(
            local_values,
            environment_name,
            anchor_action_group_id=expected_anchor_action_group_id(
                local_values,
                environment_name,
            ),
        )

    account = azure.invoke("account", "show")
    if not isinstance(account, dict):
        raise ScopeManagerError(
            "No active Azure CLI account is available for read-only target "
            "validation."
        )
    if not same_id(account.get("tenantId"), values["AZURE_TENANT_ID"]):
        raise ScopeManagerError(
            "The active Azure CLI tenant does not match the selected local "
            "AZD environment."
        )
    if not same_id(account.get("id"), values["AZURE_SUBSCRIPTION_ID"]):
        raise ScopeManagerError(
            "The active Azure CLI subscription does not match the selected "
            "local AZD environment."
        )


def parse_dotenv_value(raw: str) -> str:
    """Minimal, fail-closed dotenv value parsing: trims surrounding
    whitespace and, if present, exactly matching single/double quotes.
    Never silently accepts a malformed (unterminated) quoted value."""
    value = raw.strip()
    if not value:
        return value
    if value[0] in ("'", '"'):
        if len(value) < 2 or value[-1] != value[0]:
            raise ScopeManagerError(
                "Malformed local dotenv value: unterminated quote."
            )
        return value[1:-1]
    return value


def local_dotenv_value_present(
    dotenv_path: Path | None, name: str
) -> bool:
    """Direct local file scan for a nonempty `name=...` line. Returns
    a boolean only; never returns, logs, or raises with the value
    itself, and never invokes a subprocess or reads a process
    environment variable to make this determination."""
    if dotenv_path is None or not dotenv_path.is_file():
        return False
    prefix = f"{name}="
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return bool(parse_dotenv_value(line[len(prefix):]))
    return False


class GraphClient:
    """Microsoft Graph requests through the authenticated Azure CLI session."""

    def __init__(self, azure: AzureCli) -> None:
        self.azure = azure

    def request(
        self,
        method: str,
        uri: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        arguments = ["rest", "--method", method.lower(), "--uri", uri]
        body_path: Path | None = None
        try:
            if body is not None:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".json",
                    delete=False,
                ) as handle:
                    json.dump(body, handle, separators=(",", ":"))
                    body_path = Path(handle.name)
                arguments.extend(
                    [
                        "--headers",
                        "Content-Type=application/json",
                        "--body",
                        f"@{body_path}",
                    ]
                )
            return self.azure.invoke(*arguments)
        finally:
            if body_path is not None:
                body_path.unlink(missing_ok=True)


def graph_values(response: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in as_list(member(response, "value"))
        if isinstance(item, dict)
    ]


def graph_collection(
    graph: GraphClient,
    uri: str,
    *,
    max_pages: int = MAX_GRAPH_PAGES,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    next_uri: str | None = uri
    seen: set[str] = set()
    for _page in range(max_pages):
        if next_uri is None:
            return values
        if next_uri in seen:
            raise ScopeManagerError(
                f"Microsoft Graph returned a repeated pagination link for {uri}."
            )
        seen.add(next_uri)
        response = graph.request("GET", next_uri)
        if not isinstance(response, dict) or not isinstance(
            member(response, "value"), list
        ):
            raise ScopeManagerError(
                f"Microsoft Graph returned an invalid collection response for {uri}."
            )
        values.extend(graph_values(response))
        next_link = member(response, "@odata.nextLink")
        if next_link is None:
            return values
        if not isinstance(next_link, str) or not next_link.strip():
            raise ScopeManagerError(
                f"Microsoft Graph returned an invalid pagination link for {uri}."
            )
        next_uri = next_link
    raise ScopeManagerError(
        f"Microsoft Graph pagination exceeded {max_pages} pages for {uri}."
    )


def single_graph_value(
    values: list[dict[str, Any]],
    description: str,
    *,
    allow_absent: bool = True,
) -> dict[str, Any] | None:
    if len(values) > 1:
        raise ScopeManagerError(
            f"Microsoft Graph returned multiple {description} records; refusing to choose one."
        )
    if not values:
        if allow_absent:
            return None
        raise ScopeManagerError(
            f"Microsoft Graph returned no {description} record."
        )
    return values[0]


def resolve_caller_owner_object_id(
    account: dict[str, Any],
    graph: GraphClient,
) -> str:
    user = member(account, "user", {})
    user_type = str(member(user, "type", "") or "").strip().casefold()
    if user_type == "user":
        current_user = graph.request("GET", f"{GRAPH_ROOT}/me?$select=id")
        object_id = str(member(current_user, "id", "") or "").strip()
        if not object_id:
            raise ScopeManagerError(
                "Cannot resolve deployment caller: Microsoft Graph /me returned no object id "
                "for the signed-in user."
            )
        return object_id
    if user_type == "serviceprincipal":
        client_id = str(member(user, "name", "") or "").strip()
        if not client_id:
            raise ScopeManagerError(
                "Cannot resolve deployment caller: 'az account show' reported a service "
                "principal but account.user.name (client id) is empty."
            )
        escaped = client_id.replace("'", "''")
        principals = graph_collection(
            graph,
            f"{GRAPH_ROOT}/servicePrincipals"
            f"?$filter=appId eq '{escaped}'&$select=id",
        )
        principal = single_graph_value(
            principals,
            f"service principal for client id '{client_id}'",
            allow_absent=True,
        )
        if principal is None:
            raise ScopeManagerError(
                "Cannot add deployment caller as application owner: no service principal "
                f"found in the directory for client id '{client_id}'."
            )
        object_id = str(member(principal, "id", "") or "").strip()
        if not object_id:
            raise ScopeManagerError(
                "Cannot add deployment caller as application owner: the resolved service "
                f"principal for client id '{client_id}' has no object id."
            )
        return object_id
    raise ScopeManagerError(
        "Cannot resolve deployment caller: unsupported Azure CLI account user type "
        f"'{member(user, 'type', None)}'. Expected 'user' or 'servicePrincipal'."
    )


class SecureWebhookConfigurator:
    """Idempotent Entra and AZD configuration workflow."""

    def __init__(
        self,
        azure: AzureCli,
        graph: GraphClient,
        azd: AzdCli,
    ) -> None:
        self.azure = azure
        self.graph = graph
        self.azd = azd

    def enforce_token_migration_precondition(
        self, environment_name: str = ""
    ) -> None:
        """Fail closed if a legacy plaintext SLACK_BOT_TOKEN is still
        present in the operator's local AZD dotenv file. Presence is
        detected by a direct local file scan (never by asking AZD for
        the value itself, which would transport the secret through a
        child process's stdout). Ordinary provisioning must not silently
        keep deploying a legacy plaintext token; the operator must run
        'manage_slack_token.py migrate' (or 'bootstrap'), which only
        sets the migration marker after the token has actually been
        removed locally.

        If a named environment cannot be resolved to its local dotenv file,
        fail closed rather than treating an unknown file as token-free."""
        if (
            self.azd.get_environment_value(
                "SERVICE_HEALTH_DEPLOY_WORKLOAD"
            ).casefold()
            == "false"
        ):
            return
        if not environment_name:
            environments = self.azd.list_environments()
            names = [
                str(member(entry, "Name") or member(entry, "name") or "")
                for entry in environments
            ]
            names = [name for name in names if name]
            if len(names) != 1:
                raise ScopeManagerError(
                    "Could not resolve exactly one selected AZD environment; "
                    "refusing to assume the legacy token is absent."
                )
            environment_name = names[0]
        dotenv_path = resolve_local_dotenv_path(self.azd, environment_name)
        if dotenv_path is None or not dotenv_path.is_file():
            raise ScopeManagerError(
                "Could not resolve the selected AZD environment's local "
                "dotenv file; refusing to assume the legacy token is absent."
            )
        if not local_dotenv_value_present(dotenv_path, LEGACY_TOKEN_ENV_NAME):
            return
        raise ScopeManagerError(
            "A legacy SLACK_BOT_TOKEN value is still present in the local "
            "AZD environment file. Ordinary provisioning is refused until "
            "it is migrated: run 'scripts/manage_slack_token.py migrate' "
            "(or 'bootstrap' for a first-time, token-free setup)."
        )

    def ensure_operational_defaults(self) -> dict[str, str]:
        """Initialize missing nonsecret AZD defaults without overwriting
        any value an operator or another tool already persisted."""
        resolved: dict[str, str] = {}
        for name, default in NONSECRET_AZD_DEFAULTS.items():
            current = self.azd.get_environment_value(name)
            if current:
                resolved[name] = current
                continue
            self.azd.set_environment_value(name, default)
            resolved[name] = default
        return resolved

    def _find_service_principal(
        self,
        application_id: str,
        description: str,
    ) -> dict[str, Any] | None:
        escaped = application_id.replace("'", "''")
        return single_graph_value(
            graph_collection(
                self.graph,
                f"{GRAPH_ROOT}/servicePrincipals"
                f"?$filter=appId eq '{escaped}'&$select=id,appId",
            ),
            description,
        )

    def _service_principal(
        self,
        application_id: str,
        description: str,
    ) -> dict[str, Any]:
        existing = self._find_service_principal(
            application_id,
            description,
        )
        if existing is not None:
            return existing
        created = self.graph.request(
            "POST",
            f"{GRAPH_ROOT}/servicePrincipals",
            {"appId": application_id},
        )
        if not member(created, "id"):
            raise ScopeManagerError(
                f"Microsoft Graph did not return an object id after creating {description}."
            )
        return created

    def _application(
        self,
        display_name: str,
        application_object_id: str,
        application_client_id: str,
    ) -> tuple[dict[str, Any], bool]:
        select = "id,appId,displayName,api,appRoles,identifierUris"
        if application_object_id:
            try:
                uuid.UUID(application_object_id)
            except ValueError as exc:
                raise ScopeManagerError(
                    "Secure Webhook application object id must be a UUID."
                ) from exc
            if application_client_id:
                try:
                    uuid.UUID(application_client_id)
                except ValueError as exc:
                    raise ScopeManagerError(
                        "Secure Webhook application client id must be a UUID."
                    ) from exc
            application = self.graph.request(
                "GET",
                f"{GRAPH_ROOT}/applications/{application_object_id}"
                f"?$select={select}",
            )
            if not isinstance(application, dict):
                raise ScopeManagerError(
                    "Microsoft Graph returned an invalid Secure Webhook "
                    "application response."
                )
            if (
                not same_id(member(application, "id"), application_object_id)
                or (
                    application_client_id
                    and not same_id(
                        member(application, "appId"),
                        application_client_id,
                    )
                )
                or member(application, "displayName") != display_name
            ):
                raise ScopeManagerError(
                    "The persisted Secure Webhook application identity does "
                    "not match the requested environment."
                )
            return application, False

        if application_client_id:
            try:
                uuid.UUID(application_client_id)
            except ValueError as exc:
                raise ScopeManagerError(
                    "Secure Webhook application client id must be a UUID."
                ) from exc
            escaped_id = application_client_id.replace("'", "''")
            application = single_graph_value(
                graph_collection(
                    self.graph,
                    f"{GRAPH_ROOT}/applications"
                    f"?$filter=appId eq '{escaped_id}'&$select={select}",
                ),
                "Secure Webhook application for the persisted client id",
                allow_absent=False,
            )
            if (
                not same_id(
                    member(application, "appId"),
                    application_client_id,
                )
                or member(application, "displayName") != display_name
            ):
                raise ScopeManagerError(
                    "The persisted Secure Webhook application identity does "
                    "not match the requested environment."
                )
            return application, False

        escaped_name = display_name.replace("'", "''")
        collisions = graph_collection(
            self.graph,
            f"{GRAPH_ROOT}/applications"
            f"?$filter=displayName eq '{escaped_name}'&$select=id,appId",
        )
        if collisions:
            raise ScopeManagerError(
                f"Application name '{display_name}' already exists, but no "
                "persisted object and client ids were provided. Refusing to "
                "adopt an application by display name."
            )
        application = self.graph.request(
            "POST",
            f"{GRAPH_ROOT}/applications",
            {
                "displayName": display_name,
                "api": {"requestedAccessTokenVersion": 2},
            },
        )
        if not isinstance(application, dict):
            raise ScopeManagerError(
                "Microsoft Graph returned an invalid response after creating "
                "the Secure Webhook application."
            )
        return application, True

    def configure(
        self,
        display_name: str,
        azns_application_id: str = DEFAULT_AZNS_APPLICATION_ID,
        role_name: str = DEFAULT_ROLE_NAME,
        application_object_id: str = "",
        application_client_id: str = "",
        expected_tenant_id: str = "",
        expected_owner_ids: str = "",
        adopt_existing_owner_baseline: bool = False,
        environment_name: str = "",
    ) -> dict[str, str]:
        account = self.azure.invoke("account", "show")
        if not isinstance(account, dict):
            raise ScopeManagerError(
                "No active Azure CLI session. Run 'az login'."
            )
        tenant_id = str(member(account, "tenantId", "") or "").strip()
        if not tenant_id:
            raise ScopeManagerError(
                "The active Azure CLI account has no tenant id."
            )
        if expected_tenant_id and not same_id(tenant_id, expected_tenant_id):
            raise ScopeManagerError(
                "The active Azure CLI tenant does not match the persisted "
                "AZD environment tenant."
            )

        self.enforce_token_migration_precondition(environment_name)

        has_persisted_identity = bool(
            application_object_id or application_client_id
        )
        if adopt_existing_owner_baseline and not has_persisted_identity:
            raise ScopeManagerError(
                "Owner-baseline adoption requires a persisted Secure Webhook "
                "application object or client id."
            )

        application, application_created = self._application(
            display_name,
            application_object_id,
            application_client_id,
        )
        application_id = str(member(application, "appId", "") or "").strip()
        application_object_id = str(
            member(application, "id", "") or ""
        ).strip()
        if not application_id or not application_object_id:
            raise ScopeManagerError(
                "Microsoft Graph did not return both appId and id for the Secure Webhook application."
            )
        identifier_uri = f"api://{application_id}"
        application_object_persisted = False
        if application_created:
            try:
                self.azd.set_environment_value(
                    "SERVICE_HEALTH_API_OBJECT_ID",
                    application_object_id,
                )
            except ScopeManagerError:
                self.graph.request(
                    "DELETE",
                    f"{GRAPH_ROOT}/applications/{application_object_id}",
                )
                raise
            application_object_persisted = True

        caller_object_id = resolve_caller_owner_object_id(account, self.graph)
        existing_azns_principal = self._find_service_principal(
            azns_application_id,
            "Azure Monitor AzNS service principal",
        )
        owners = graph_collection(
            self.graph,
            f"{GRAPH_ROOT}/applications/{application_object_id}/owners?$select=id",
        )
        owner_ids = {
            str(member(owner, "id", "") or "").strip().casefold()
            for owner in owners
        }
        if "" in owner_ids:
            raise ScopeManagerError(
                "The Secure Webhook application has an owner without an "
                "object id."
            )

        persisted_owner_ids = {
            owner_id.strip().casefold()
            for owner_id in expected_owner_ids.split(",")
            if owner_id.strip()
        }
        official_owner_ids = {caller_object_id.casefold()}
        azns_owner_id = ""
        if existing_azns_principal is not None:
            azns_owner_id = str(
                member(existing_azns_principal, "id", "") or ""
            ).strip()
            if not azns_owner_id:
                raise ScopeManagerError(
                    "The Azure Monitor AzNS service principal has no object id."
                )
            official_owner_ids.add(azns_owner_id.casefold())

        if persisted_owner_ids:
            permitted_owner_ids = (
                persisted_owner_ids | official_owner_ids
            )
        elif adopt_existing_owner_baseline and not application_created:
            permitted_owner_ids = owner_ids | official_owner_ids
        elif owner_ids <= official_owner_ids:
            permitted_owner_ids = official_owner_ids
        else:
            permitted_owner_ids = official_owner_ids

        unexpected_owner_ids = sorted(owner_ids - permitted_owner_ids)
        if unexpected_owner_ids:
            raise ScopeManagerError(
                "The Secure Webhook application has unexpected owners; "
                "refusing to modify an application with unverified provenance."
            )
        values = {
            "AZURE_TENANT_ID": tenant_id,
            "SERVICE_HEALTH_API_CLIENT_ID": application_id,
            "SERVICE_HEALTH_API_OBJECT_ID": application_object_id,
            "SERVICE_HEALTH_API_IDENTIFIER_URI": identifier_uri,
        }
        # Nonsecret defaults are only ever initialized once every risky
        # Graph mutation above has already succeeded, so a rejected or
        # rolled-back configure() call never persists any AZD value.
        values.update(self.ensure_operational_defaults())
        if not application_object_persisted:
            self.azd.set_environment_value(
                "SERVICE_HEALTH_API_OBJECT_ID",
                application_object_id,
            )
        for name in (
            "SERVICE_HEALTH_API_CLIENT_ID",
            "AZURE_TENANT_ID",
            "SERVICE_HEALTH_API_IDENTIFIER_URI",
        ):
            self.azd.set_environment_value(name, values[name])

        expected_owner_ids = ",".join(sorted(permitted_owner_ids))
        self.azd.set_environment_value(
            "SERVICE_HEALTH_API_OWNER_IDS",
            expected_owner_ids,
        )
        values["SERVICE_HEALTH_API_OWNER_IDS"] = expected_owner_ids

        api = member(application, "api", {}) or {}
        if member(api, "requestedAccessTokenVersion") != 2:
            self.graph.request(
                "PATCH",
                f"{GRAPH_ROOT}/applications/{application_object_id}",
                {
                    "api": {
                        **api,
                        "requestedAccessTokenVersion": 2,
                    }
                },
            )

        roles = as_list(member(application, "appRoles"))
        matching_roles = [
            role for role in roles if member(role, "value") == role_name
        ]
        if len(matching_roles) > 1:
            raise ScopeManagerError(
                f"Application '{display_name}' has multiple app roles with value '{role_name}'."
            )
        if matching_roles:
            role = matching_roles[0]
            if not member(role, "id"):
                raise ScopeManagerError(
                    f"Existing app role '{role_name}' has no object id."
                )
            if member(role, "isEnabled") is not True:
                raise ScopeManagerError(
                    f"Existing app role '{role_name}' is not enabled."
                )
            allowed_member_types = as_list(
                member(role, "allowedMemberTypes")
            )
            if not contains_case_insensitive(
                allowed_member_types,
                "Application",
            ):
                raise ScopeManagerError(
                    f"Existing app role '{role_name}' does not allow application principals."
                )
            if not contains_case_insensitive(
                as_list(member(application, "identifierUris")),
                identifier_uri,
            ):
                self.graph.request(
                    "PATCH",
                    f"{GRAPH_ROOT}/applications/{application_object_id}",
                    {"identifierUris": [identifier_uri]},
                )
        else:
            role = {
                "id": str(uuid.uuid4()),
                "allowedMemberTypes": ["Application"],
                "description": (
                    "Allows Azure Monitor Action Groups to invoke the secure webhook."
                ),
                "displayName": role_name,
                "isEnabled": True,
                "value": role_name,
            }
            self.graph.request(
                "PATCH",
                f"{GRAPH_ROOT}/applications/{application_object_id}",
                {
                    "identifierUris": [identifier_uri],
                    "appRoles": [*roles, role],
                },
            )

        api_service_principal = self._service_principal(
            application_id,
            "Secure Webhook API service principal",
        )
        azns_principal = self._service_principal(
            azns_application_id,
            "Azure Monitor AzNS service principal",
        )

        for owner_id in (
            caller_object_id,
            str(member(azns_principal, "id", "") or ""),
        ):
            if not owner_id:
                raise ScopeManagerError(
                    "Microsoft Graph returned an owner candidate without an object id."
                )
            if not any(same_id(member(owner, "id"), owner_id) for owner in owners):
                self.graph.request(
                    "POST",
                    f"{GRAPH_ROOT}/applications/{application_object_id}/owners/$ref",
                    {
                        "@odata.id": (
                            f"{GRAPH_ROOT}/directoryObjects/{owner_id}"
                        )
                    },
                )
                owners.append({"id": owner_id})
                permitted_owner_ids.add(owner_id.casefold())

        expected_owner_ids = ",".join(sorted(permitted_owner_ids))
        self.azd.set_environment_value(
            "SERVICE_HEALTH_API_OWNER_IDS",
            expected_owner_ids,
        )
        values["SERVICE_HEALTH_API_OWNER_IDS"] = expected_owner_ids

        azns_object_id = str(member(azns_principal, "id", "") or "")
        api_sp_object_id = str(
            member(api_service_principal, "id", "") or ""
        )
        role_id = str(member(role, "id", "") or "")
        assignments = graph_collection(
            self.graph,
            f"{GRAPH_ROOT}/servicePrincipals/{azns_object_id}"
            "/appRoleAssignments?$select=resourceId,appRoleId",
        )
        assignment_exists = any(
            same_id(member(item, "resourceId"), api_sp_object_id)
            and same_id(member(item, "appRoleId"), role_id)
            for item in assignments
        )
        if not assignment_exists:
            self.graph.request(
                "POST",
                f"{GRAPH_ROOT}/servicePrincipals/{azns_object_id}/appRoleAssignments",
                {
                    "principalId": azns_object_id,
                    "resourceId": api_sp_object_id,
                    "appRoleId": role_id,
                },
            )

        return values


def contains_case_insensitive(values: list[Any], target: str) -> bool:
    return any(str(value).casefold() == target.casefold() for value in values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently configure the Entra application, ownership, and app-role "
            "assignment required by Azure Monitor Secure Webhooks."
        )
    )
    parser.add_argument("--display-name")
    parser.add_argument("--application-object-id")
    parser.add_argument("--application-client-id")
    parser.add_argument(
        "--adopt-existing-owner-baseline",
        action="store_true",
        help=(
            "Persist the current owners of an application resolved by an "
            "immutable object/client id. Use only for a reviewed legacy "
            "environment with no owner baseline."
        ),
    )
    parser.add_argument(
        "--azns-application-id",
        default=DEFAULT_AZNS_APPLICATION_ID,
    )
    parser.add_argument("--role-name", default=DEFAULT_ROLE_NAME)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved_environment_name = os.environ.get("AZURE_ENV_NAME", "").strip()
    if not resolved_environment_name:
        print(
            "ERROR: AZURE_ENV_NAME is required for fail-closed local "
            "secret-state verification.",
            file=sys.stderr,
        )
        return 1
    try:
        if read_only_preview_enabled():
            validate_read_only_preview(
                resolved_environment_name,
                project_root=Path(__file__).resolve().parent.parent,
                azure=AzureCli(),
            )
            print(
                "Read-only preview hook validation passed; no state was "
                "changed."
            )
            return 0
    except ScopeManagerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    display_name = args.display_name
    if not display_name:
        display_name = (
            f"Azure Service Health Slack Bot - {resolved_environment_name}"
        )
    try:
        azure = AzureCli()
        azd = AzdCli(environment_name=resolved_environment_name)
        if (
            azd.get_environment_value(
                "SERVICE_HEALTH_DEPLOY_WORKLOAD"
            ).casefold()
            == "true"
        ):
            enforce_production_readiness(
                azd,
                resolved_environment_name,
                anchor_action_group_id=expected_anchor_action_group_id(
                    azd, resolved_environment_name
                ),
            )

        def persisted_value(argument_value, environment_name):
            return (
                argument_value
                or os.environ.get(environment_name, "")
                or azd.get_environment_value(environment_name)
            ).strip()

        result = SecureWebhookConfigurator(
            azure,
            GraphClient(azure),
            azd,
        ).configure(
            display_name,
            azns_application_id=args.azns_application_id,
            role_name=args.role_name,
            application_object_id=persisted_value(
                args.application_object_id,
                "SERVICE_HEALTH_API_OBJECT_ID",
            ),
            application_client_id=persisted_value(
                args.application_client_id,
                "SERVICE_HEALTH_API_CLIENT_ID",
            ),
            expected_tenant_id=persisted_value(
                "",
                "AZURE_TENANT_ID",
            ),
            expected_owner_ids=persisted_value(
                "",
                "SERVICE_HEALTH_API_OWNER_IDS",
            ),
            adopt_existing_owner_baseline=(
                args.adopt_existing_owner_baseline
            ),
            environment_name=resolved_environment_name,
        )
    except ScopeManagerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Secure webhook application is configured for {display_name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
