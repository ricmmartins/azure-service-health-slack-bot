import json
import os
from dataclasses import dataclass

from service_health.routing import RoutingConfig


AZNS_AAD_WEBHOOK_APP_ID = "461e8683-5575-4561-ac7f-899cc907d62a"


class InvalidServiceHealthConfiguration(ValueError):
    pass


def _read_routing_config(environ):
    inline = environ.get("SERVICE_HEALTH_ROUTES_JSON")
    path = environ.get("SERVICE_HEALTH_ROUTES_FILE")
    if inline and path:
        raise InvalidServiceHealthConfiguration(
            "Configure only one of SERVICE_HEALTH_ROUTES_JSON or "
            "SERVICE_HEALTH_ROUTES_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as config_file:
                return json.load(config_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidServiceHealthConfiguration(
                "Unable to load SERVICE_HEALTH_ROUTES_FILE") from exc
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise InvalidServiceHealthConfiguration(
                "SERVICE_HEALTH_ROUTES_JSON is invalid JSON") from exc
    raise InvalidServiceHealthConfiguration(
        "Service Health routing configuration is required")


@dataclass(frozen=True)
class ServiceHealthSettings:
    table_endpoint: str
    table_name: str
    routing: RoutingConfig
    app_environment: str
    expected_client_app_id: str = AZNS_AAD_WEBHOOK_APP_ID
    expected_app_role: str = "ActionGroupsSecureWebhook"
    expected_audience: str = ""
    max_payload_bytes: int = 262_144
    lease_seconds: int = 30

    @property
    def require_easy_auth(self):
        return self.app_environment not in {"development", "test"}

    @classmethod
    def from_env(cls, environ=None):
        environ = environ or os.environ
        endpoint = environ.get("AZURE_TABLE_ENDPOINT", "").strip()
        if not endpoint:
            raise InvalidServiceHealthConfiguration(
                "AZURE_TABLE_ENDPOINT is required")
        table_name = environ.get(
            "SERVICE_HEALTH_TABLE_NAME", "ServiceHealthIncidents").strip()
        if not table_name:
            raise InvalidServiceHealthConfiguration(
                "SERVICE_HEALTH_TABLE_NAME is required")
        try:
            max_payload_bytes = int(environ.get(
                "SERVICE_HEALTH_MAX_PAYLOAD_BYTES", "262144"))
            lease_seconds = int(environ.get(
                "SERVICE_HEALTH_LEASE_SECONDS", "30"))
        except ValueError as exc:
            raise InvalidServiceHealthConfiguration(
                "Payload and lease limits must be integers") from exc
        if max_payload_bytes <= 0 or lease_seconds <= 0:
            raise InvalidServiceHealthConfiguration(
                "Payload and lease limits must be positive")

        app_environment = environ.get(
            "APP_ENV", "development").strip().lower()
        expected_audience = environ.get(
            "SERVICE_HEALTH_EXPECTED_AUDIENCE", "").strip()
        if app_environment not in {"development", "test"} and not expected_audience:
            raise InvalidServiceHealthConfiguration(
                "SERVICE_HEALTH_EXPECTED_AUDIENCE is required outside local use")

        return cls(
            table_endpoint=endpoint,
            table_name=table_name,
            routing=RoutingConfig.from_dict(_read_routing_config(environ)),
            app_environment=app_environment,
            expected_client_app_id=environ.get(
                "SERVICE_HEALTH_EXPECTED_CLIENT_APP_ID",
                AZNS_AAD_WEBHOOK_APP_ID,
            ).strip(),
            expected_app_role=environ.get(
                "SERVICE_HEALTH_EXPECTED_APP_ROLE",
                "ActionGroupsSecureWebhook",
            ).strip(),
            expected_audience=expected_audience,
            max_payload_bytes=max_payload_bytes,
            lease_seconds=lease_seconds,
        )
