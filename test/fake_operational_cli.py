"""Stateful fake az/azd process used by cross-platform subprocess tests."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "22222222-2222-2222-2222-222222222222"
APPLICATION_ID = "33333333-3333-3333-3333-333333333333"
APPLICATION_OBJECT_ID = "44444444-4444-4444-4444-444444444444"
AZNS_APPLICATION_ID = "461e8683-5575-4561-ac7f-899cc907d62a"
ENVIRONMENT_NAME = "contract-env"
RESOURCE_GROUP = f"rg-{ENVIRONMENT_NAME}"
CONTAINER_APP_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    "/providers/Microsoft.App/containerApps/ca-contract-env"
)
ACTION_GROUP_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    "/providers/Microsoft.Insights/actionGroups/ag-contract-env-service-health"
)
ALERT_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    "/providers/Microsoft.Insights/activityLogAlerts/"
    "ala-contract-env-service-health"
)


def load_state() -> dict[str, Any]:
    path = Path(os.environ["FAKE_CLI_STATE"])
    if not path.exists():
        return {
            "application": None,
            "servicePrincipals": {},
            "owners": [],
            "assignments": [],
            "azd": {},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    Path(os.environ["FAKE_CLI_STATE"]).write_text(
        json.dumps(state),
        encoding="utf-8",
    )


def request_body(arguments: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    if "--body" not in arguments:
        return None, None
    value = arguments[arguments.index("--body") + 1]
    if not value.startswith("@"):
        fail("Graph mutation body was not passed through an @file.")
    path = Path(value[1:])
    return json.loads(path.read_text(encoding="utf-8")), str(path)


def record(
    tool: str,
    arguments: list[str],
    body: dict[str, Any] | None,
    body_path: str | None,
) -> None:
    path = Path(os.environ["FAKE_CLI_LOG"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "body": body,
                    "bodyPath": body_path,
                }
            )
            + "\n"
        )


def emit(value: Any) -> None:
    if value is not None:
        print(json.dumps(value, separators=(",", ":")))


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(7)


def graph_request(
    state: dict[str, Any],
    method: str,
    uri: str,
    body: dict[str, Any] | None,
) -> Any:
    unique_name_match = re.search(
        r"/applications\(uniqueName='([^']+)'\)", uri
    )
    if unique_name_match and method == "get":
        application = state["application"]
        if (
            application
            and application.get("uniqueName") == unique_name_match.group(1)
        ):
            return application
        return None
    if unique_name_match and method == "patch":
        if state["application"] is None:
            state["application"] = {
                "id": APPLICATION_OBJECT_ID,
                "appId": APPLICATION_ID,
                "displayName": body["displayName"],
                "uniqueName": unique_name_match.group(1),
                "api": body["api"],
                "identifierUris": [],
                "appRoles": [],
            }
            return state["application"]
        state["application"].update(body)
        return None
    if "/applications?" in uri and method == "get":
        application = state["application"]
        return {"value": [application] if application else []}
    if (
        f"/applications/{APPLICATION_OBJECT_ID}?" in uri
        and method == "get"
    ):
        return state["application"]
    if uri.endswith("/applications") and method == "post":
        state["application"] = {
            "id": APPLICATION_OBJECT_ID,
            "appId": APPLICATION_ID,
            "displayName": body["displayName"],
            "api": body["api"],
            "identifierUris": [],
            "appRoles": [],
        }
        return state["application"]
    if f"/applications/{APPLICATION_OBJECT_ID}" in uri and method == "patch":
        state["application"].update(body)
        return None
    if "/servicePrincipals?" in uri and method == "get":
        match = re.search(r"appId eq '([^']+)'", uri)
        if not match:
            fail(f"Unsupported service principal filter: {uri}")
        principal = state["servicePrincipals"].get(match.group(1))
        return {"value": [principal] if principal else []}
    if uri.endswith("/servicePrincipals") and method == "post":
        app_id = body["appId"]
        object_id = (
            "55555555-5555-5555-5555-555555555555"
            if app_id == APPLICATION_ID
            else "66666666-6666-6666-6666-666666666666"
        )
        principal = {"id": object_id, "appId": app_id}
        state["servicePrincipals"][app_id] = principal
        return principal
    if "/owners?" in uri and method == "get":
        return {"value": [{"id": value} for value in state["owners"]]}
    if uri.endswith("/me?$select=id") and method == "get":
        return {"id": "77777777-7777-7777-7777-777777777777"}
    if uri.endswith("/owners/$ref") and method == "post":
        owner_id = body["@odata.id"].rsplit("/", 1)[-1]
        if owner_id not in state["owners"]:
            state["owners"].append(owner_id)
        return None
    if "/appRoleAssignments?" in uri and method == "get":
        return {"value": state["assignments"]}
    if uri.endswith("/appRoleAssignments") and method == "post":
        state["assignments"].append(body)
        return None
    fail(f"Unsupported Graph request: {method} {uri}")


def azure_response(
    state: dict[str, Any],
    arguments: list[str],
    body: dict[str, Any] | None,
) -> Any:
    if arguments[:2] == ["account", "show"]:
        return {
            "id": SUBSCRIPTION_ID,
            "tenantId": TENANT_ID,
            "state": "Enabled",
            "user": {"type": "user", "name": "operator@example.com"},
        }
    if arguments[:2] == ["account", "list"]:
        return [
            {
                "id": SUBSCRIPTION_ID,
                "tenantId": TENANT_ID,
                "state": "Enabled",
            }
        ]
    if arguments[:2] == ["group", "list"]:
        return [
            {
                "name": RESOURCE_GROUP,
                "location": "eastus2",
                "tags": {
                    "workload": "azure-service-health-slack-bot",
                    "azd-env-name": ENVIRONMENT_NAME,
                },
            }
        ]
    if arguments[:2] == ["resource", "list"]:
        return [{"name": "ca-contract-env", "id": CONTAINER_APP_ID}]
    if arguments[:3] == ["monitor", "action-group", "list"]:
        return [
            {
                "name": "ag-contract-env-service-health",
                "id": ACTION_GROUP_ID,
                "webhookReceivers": [
                    {
                        "serviceUri": (
                            "https://contract.example/api/service-health"
                        ),
                        "useAadAuth": True,
                        "tenantId": TENANT_ID,
                        "objectId": APPLICATION_OBJECT_ID,
                        "identifierUri": f"api://{APPLICATION_ID}",
                    }
                ],
            }
        ]
    if arguments[:4] == ["monitor", "activity-log", "alert", "list"]:
        return [
            {
                "id": ALERT_ID,
                "scopes": [f"/subscriptions/{SUBSCRIPTION_ID}"],
                "actions": {
                    "actionGroups": [{"actionGroupId": ACTION_GROUP_ID}]
                },
            }
        ]
    if arguments[0] == "rest":
        method = arguments[arguments.index("--method") + 1].casefold()
        uri_flag = "--uri" if "--uri" in arguments else "--url"
        uri = arguments[arguments.index(uri_flag) + 1]
        if uri.startswith("https://graph.microsoft.com/"):
            return graph_request(state, method, uri, body)
        if "/authConfigs/current?" in uri:
            return {
                "properties": {
                    "identityProviders": {
                        "azureActiveDirectory": {
                            "registration": {"clientId": APPLICATION_ID}
                        }
                    }
                }
            }
        if uri.startswith(CONTAINER_APP_ID):
            return {
                "properties": {
                    "configuration": {
                        "ingress": {"fqdn": "contract.example"}
                    }
                }
            }
    fail(f"Unsupported az command: {' '.join(arguments)}")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"az", "azd"}:
        fail("Expected az or azd tool selector.")
    tool = sys.argv[1]
    arguments = sys.argv[2:]
    body, body_path = request_body(arguments)
    record(tool, arguments, body, body_path)
    state = load_state()
    if tool == "azd":
        if arguments[:2] == ["env", "set"] and len(arguments) >= 4:
            state["azd"][arguments[2]] = arguments[3]
            save_state(state)
            return 0
        if (
            arguments[:2] == ["env", "get-value"]
            and "--no-prompt" in arguments
        ):
            key = arguments[2]
            if key not in state["azd"]:
                print(
                    f"ERROR: key not found in environment values: '{key}'",
                    file=sys.stderr,
                )
                return 1
            print(state["azd"][key])
            return 0
        if arguments[:2] == ["env", "list"] and "--output" in arguments:
            environment_name = os.environ.get("AZURE_ENV_NAME", ENVIRONMENT_NAME)
            dotenv_path = Path(os.environ["FAKE_CLI_STATE"]).with_name(
                f"{environment_name}.env"
            )
            print(
                json.dumps(
                    [
                        {
                            "Name": environment_name,
                            "DotEnvPath": str(dotenv_path),
                        }
                    ]
                )
            )
            return 0
        if arguments[:1] == ["provision"]:
            return 0
        fail(f"Unsupported azd command: {' '.join(arguments)}")
    response = azure_response(state, arguments, body)
    save_state(state)
    emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
