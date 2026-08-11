#!/usr/bin/env python3
"""Configure the Entra application used by Azure Monitor Secure Webhooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.manage_alert_scopes import (
        AzureCli,
        ScopeManagerError,
        as_list,
        member,
        same_id,
    )
except ModuleNotFoundError:
    from manage_alert_scopes import (  # type: ignore[no-redef]
        AzureCli,
        ScopeManagerError,
        as_list,
        member,
        same_id,
    )


DEFAULT_AZNS_APPLICATION_ID = "461e8683-5575-4561-ac7f-899cc907d62a"
DEFAULT_ROLE_NAME = "ActionGroupsSecureWebhook"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MAX_GRAPH_PAGES = 100


class AzdCli:
    """Fail-closed Azure Developer CLI boundary."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runner = runner
        self.executable = (
            shutil.which("azd") if runner is subprocess.run else "azd"
        )

    def set_environment_value(self, name: str, value: str) -> None:
        if self.executable is None:
            raise ScopeManagerError(
                "Azure Developer CLI is required. Install it, run 'azd auth login', and retry."
            )
        result = self.runner(
            [self.executable, "env", "set", name, value],
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
        result = self.runner(
            [
                self.executable,
                "env",
                "get-value",
                name,
                "--no-prompt",
            ],
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
    display_name = args.display_name
    if not display_name:
        environment_name = os.environ.get("AZURE_ENV_NAME", "").strip()
        if not environment_name:
            print(
                "ERROR: AZURE_ENV_NAME is required unless --display-name is provided.",
                file=sys.stderr,
            )
            return 1
        display_name = (
            f"Azure Service Health Slack Bot - {environment_name}"
        )
    try:
        azure = AzureCli()
        azd = AzdCli()

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
