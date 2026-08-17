"""Tests for service_health.runtime bootstrap wiring.

Verifies credential selection (Managed Identity in production/staging vs
DefaultAzureCredential for local/dev/test), required configuration, and that
the runtime assembles the processor with the Table store and Slack notifier.
"""

from unittest.mock import MagicMock, patch

import pytest

from service_health.config import InvalidServiceHealthConfiguration
from service_health.runtime import create_service_health_runtime
from service_health.service import ServiceHealthProcessor
from service_health.slack import SlackIncidentNotifier
from service_health.storage import AzureTableIncidentStore


BASE_ENV = {
    "AZURE_TABLE_ENDPOINT": "https://example.table.core.windows.net",
    "SERVICE_HEALTH_ROUTES_JSON": (
        '{"default_channel_id": "C0123456789", "rules": []}'
    ),
}


def _table_service_client_double():
    table_service = MagicMock()
    table_service.get_table_client.return_value = MagicMock()
    return table_service


@patch("service_health.runtime.TableServiceClient")
@patch("service_health.runtime.DefaultAzureCredential")
def test_runtime_uses_default_credential_outside_production(
        default_credential, table_service_client):
    table_service_client.return_value = _table_service_client_double()
    environ = {**BASE_ENV, "APP_ENV": "development"}

    runtime = create_service_health_runtime(MagicMock(), environ=environ)

    default_credential.assert_called_once_with()
    table_service_client.assert_called_once()
    assert isinstance(runtime.processor, ServiceHealthProcessor)
    assert isinstance(runtime.processor.store, AzureTableIncidentStore)
    assert isinstance(runtime.processor.notifier, SlackIncidentNotifier)


@patch("service_health.runtime.TableServiceClient")
@patch("service_health.runtime.ManagedIdentityCredential")
def test_runtime_uses_managed_identity_in_production(
        managed_identity_credential, table_service_client):
    table_service_client.return_value = _table_service_client_double()
    environ = {
        **BASE_ENV,
        "APP_ENV": "production",
        "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
        "SERVICE_HEALTH_EXPECTED_AUDIENCE": "api://service-health",
    }

    create_service_health_runtime(MagicMock(), environ=environ)

    managed_identity_credential.assert_called_once_with(
        client_id="11111111-1111-1111-1111-111111111111")


def test_runtime_requires_client_id_for_managed_identity_in_production():
    environ = {
        **BASE_ENV,
        "APP_ENV": "production",
        "SERVICE_HEALTH_EXPECTED_AUDIENCE": "api://service-health",
    }
    with pytest.raises(InvalidServiceHealthConfiguration):
        create_service_health_runtime(MagicMock(), environ=environ)


def test_runtime_requires_table_endpoint():
    environ = {
        "SERVICE_HEALTH_ROUTES_JSON": BASE_ENV["SERVICE_HEALTH_ROUTES_JSON"],
        "APP_ENV": "development",
    }
    with pytest.raises(InvalidServiceHealthConfiguration):
        create_service_health_runtime(MagicMock(), environ=environ)


@pytest.mark.parametrize(
    "endpoint",
    [
        "not-a-url",
        "http://example.table.core.windows.net",
        "https://table.core.windows.net",
        "https://example.table.core.windows.net/path",
        "https://example.table.core.windows.net?query=value",
        "https://user@example.table.core.windows.net",
    ],
)
def test_runtime_rejects_malformed_table_endpoint(endpoint):
    environ = {
        **BASE_ENV,
        "AZURE_TABLE_ENDPOINT": endpoint,
        "APP_ENV": "development",
    }

    with pytest.raises(
        InvalidServiceHealthConfiguration,
        match="Azure public cloud Table endpoint",
    ):
        create_service_health_runtime(MagicMock(), environ=environ)
