import os
from dataclasses import dataclass

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from service_health.config import (
    InvalidServiceHealthConfiguration,
    ServiceHealthSettings,
)
from service_health.service import ServiceHealthProcessor
from service_health.slack import SlackIncidentNotifier
from service_health.storage import AzureTableIncidentStore


@dataclass(frozen=True)
class ServiceHealthRuntime:
    settings: ServiceHealthSettings
    processor: ServiceHealthProcessor


def create_service_health_runtime(slack_client, environ=None):
    environ = os.environ if environ is None else environ
    settings = ServiceHealthSettings.from_env(environ)
    if settings.app_environment in {"production", "staging"}:
        client_id = environ.get("AZURE_CLIENT_ID", "").strip()
        if not client_id:
            raise InvalidServiceHealthConfiguration(
                "AZURE_CLIENT_ID is required for the managed identity")
        credential = ManagedIdentityCredential(client_id=client_id)
    else:
        credential = DefaultAzureCredential()
    table_service = TableServiceClient(
        endpoint=settings.table_endpoint,
        credential=credential,
        retry_total=3,
        retry_backoff_factor=0.8,
    )
    table_client = table_service.get_table_client(settings.table_name)
    store = AzureTableIncidentStore(
        table_client, lease_seconds=settings.lease_seconds)
    notifier = SlackIncidentNotifier(slack_client)
    processor = ServiceHealthProcessor(settings.routing, store, notifier)
    return ServiceHealthRuntime(settings, processor)
