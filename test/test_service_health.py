import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError
from flask import Flask

from service_health.auth import (
    InvalidWebhookIdentity,
    MissingWebhookIdentity,
    authorize_easy_auth,
    encode_test_principal,
)
from service_health.config import ServiceHealthSettings
from service_health.models import AlertLevel, LifecycleStatus
from service_health.parser import (
    InvalidServiceHealthPayload,
    parse_service_health_alert,
)
from service_health.routes import create_service_health_blueprint
from service_health.routing import RoutingConfig
from service_health.service import (
    ProcessingOutcome,
    ProcessingResult,
    ServiceHealthProcessor,
    TransientProcessingError,
)
from service_health.slack import render_incident_message
from service_health.storage import (
    AzureTableIncidentStore,
    IncidentWorkItem,
    StoreDecision,
)


SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def common_alert(
        stage="Active",
        status="Active",
        level="Warning",
        submission_time="2025-06-01T12:00:00Z"):
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertId": (
                    f"/subscriptions/{SUBSCRIPTION_ID}/providers/"
                    "Microsoft.Insights/activityLogAlerts/service-health"
                ),
                "alertTargetIDs": [
                    f"/subscriptions/{SUBSCRIPTION_ID}"
                ],
            },
            "alertContext": {
                "eventSource": "ServiceHealth",
                "level": level,
                "status": status,
                "submissionTimestamp": submission_time,
                "eventDataId": "event-1",
                "properties": {
                    "title": "Azure Kubernetes Service issue",
                    "impactStartTime": "2025-06-01T11:45:00Z",
                    "communication": "Engineers are investigating.",
                    "impactedServices": (
                        '[{"ServiceName":"Azure Kubernetes Service",'
                        '"ImpactedRegions":[{"RegionName":"East US"}]}]'
                    ),
                    "trackingId": "ABC1-XYZ",
                    "stage": stage,
                    "communicationId": "comm-1",
                    "incidentType": "Service issue",
                },
            },
        },
    }


@pytest.mark.parametrize(
    ("stage", "status", "expected"),
    [
        ("Active", "Active", LifecycleStatus.ACTIVE),
        ("Updated", "Active", LifecycleStatus.UPDATED),
        ("Resolved", "Resolved", LifecycleStatus.RESOLVED),
    ],
)
def test_parser_normalizes_lifecycle(stage, status, expected):
    event = parse_service_health_alert(common_alert(stage, status))
    assert event.lifecycle_status == expected
    assert event.subscription_id == SUBSCRIPTION_ID
    assert event.impacted_services[0].regions == ("East US",)


def test_parser_accepts_numeric_source_and_level():
    payload = common_alert()
    payload["data"]["alertContext"]["eventSource"] = 2
    payload["data"]["alertContext"]["level"] = 0
    event = parse_service_health_alert(payload)
    assert event.level == AlertLevel.CRITICAL


def test_parser_accepts_native_impacted_services_array():
    payload = common_alert()
    escaped = payload["data"]["alertContext"]["properties"][
        "impactedServices"]
    payload["data"]["alertContext"]["properties"][
        "impactedServices"] = json.loads(escaped)
    event = parse_service_health_alert(payload)
    assert event.impacted_services[0].name == "Azure Kubernetes Service"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.update({"schemaId": "other"}),
        lambda payload: payload["data"]["alertContext"].update(
            {"eventSource": "Administrative"}),
        lambda payload: payload["data"]["alertContext"]["properties"].update(
            {"impactedServices": "not-json"}),
        lambda payload: payload["data"]["alertContext"]["properties"].pop(
            "trackingId"),
        lambda payload: (
            payload["data"]["alertContext"].update({"status": "Unknown"}),
            payload["data"]["alertContext"]["properties"].update(
                {"stage": "Unknown"}),
        ),
    ],
)
def test_parser_rejects_invalid_payload(mutator):
    payload = common_alert()
    mutator(payload)
    with pytest.raises(InvalidServiceHealthPayload):
        parse_service_health_alert(payload)


def test_event_keys_and_fingerprint_are_deterministic():
    event = parse_service_health_alert(common_alert())
    again = parse_service_health_alert(copy.deepcopy(common_alert()))
    assert event.partition_key == SUBSCRIPTION_ID
    assert event.row_key == again.row_key
    assert event.fingerprint == again.fingerprint
    assert "/" not in event.row_key


def test_routing_uses_priority_specificity_and_fallback():
    event = parse_service_health_alert(common_alert())
    routing = RoutingConfig.from_dict({
        "default_channel_id": "CDEFAULT",
        "rules": [
            {
                "channel_id": "CSERVICE",
                "priority": 10,
                "services": ["Azure Kubernetes Service"],
            },
            {
                "channel_id": "CSPECIFIC",
                "priority": 10,
                "services": ["Azure Kubernetes Service"],
                "regions": ["East US"],
            },
        ],
    })
    assert routing.channel_for(event) == "CSPECIFIC"
    other = replace(
        event,
        subscription_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        impacted_services=(),
    )
    assert routing.channel_for(other) == "CDEFAULT"


def test_easy_auth_requires_app_audience_and_role():
    audience = "api://service-health"
    headers = {
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL": encode_test_principal([
            ("appid", "461e8683-5575-4561-ac7f-899cc907d62a"),
            ("aud", audience),
            ("roles", "ActionGroupsSecureWebhook"),
        ]),
    }
    authorize_easy_auth(
        headers,
        "461e8683-5575-4561-ac7f-899cc907d62a",
        "ActionGroupsSecureWebhook",
        audience,
    )

    v2_headers = {
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL": encode_test_principal([
            ("azp", "461e8683-5575-4561-ac7f-899cc907d62a"),
            ("aud", "service-health"),
            ("roles", "ActionGroupsSecureWebhook"),
        ]),
    }
    authorize_easy_auth(
        v2_headers,
        "461e8683-5575-4561-ac7f-899cc907d62a",
        "ActionGroupsSecureWebhook",
        audience,
    )

    with pytest.raises(MissingWebhookIdentity):
        authorize_easy_auth({}, "client", "role", audience)
    with pytest.raises(InvalidWebhookIdentity):
        authorize_easy_auth(
            headers, "wrong-client", "ActionGroupsSecureWebhook", audience)
    with pytest.raises(InvalidWebhookIdentity):
        authorize_easy_auth(
            headers,
            "461e8683-5575-4561-ac7f-899cc907d62a",
            "ActionGroupsSecureWebhook",
            "api://wrong-audience",
        )


def test_slack_rendering_includes_accessible_fallback_and_incident_details():
    event = parse_service_health_alert(common_alert())
    text, blocks = render_incident_message(event, LifecycleStatus.ACTIVE)
    assert "Azure Service Health Active" in text
    assert "ABC1-XYZ" in text
    serialized = str(blocks)
    assert "Azure Kubernetes Service" in serialized
    assert "East US" in serialized
    assert "Open Azure Service Health" in serialized
    assert len(blocks[0]["text"]["text"]) <= 150


def test_slack_rendering_truncates_complete_resolved_header():
    event = replace(
        parse_service_health_alert(common_alert()),
        title="T" * 300,
    )
    _, blocks = render_incident_message(event, LifecycleStatus.RESOLVED)
    assert len(blocks[0]["text"]["text"]) == 150


class FakeEntity(dict):
    def __init__(self, value, etag):
        super().__init__(value)
        self.metadata = {"etag": etag}


class FakeTableClient:
    def __init__(self):
        self.entity = None
        self.version = 0

    def create_entity(self, entity):
        if self.entity is not None:
            raise ResourceExistsError("exists")
        self.version += 1
        self.entity = FakeEntity(copy.deepcopy(entity), str(self.version))

    def get_entity(self, partition_key, row_key):
        assert self.entity["PartitionKey"] == partition_key
        assert self.entity["RowKey"] == row_key
        return FakeEntity(copy.deepcopy(self.entity), str(self.version))

    def update_entity(self, entity, mode, etag, match_condition):
        assert etag == str(self.version)
        self.version += 1
        self.entity = FakeEntity(copy.deepcopy(entity), str(self.version))


def test_table_store_create_finalize_duplicate_and_update():
    now = datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
    table = FakeTableClient()
    store = AzureTableIncidentStore(table, now=lambda: now)
    event = parse_service_health_alert(common_alert())

    created = store.begin(event, "CDEFAULT")
    assert created.decision == StoreDecision.CREATE
    store.finalize(created, "123.456", LifecycleStatus.ACTIVE)

    duplicate = store.begin(event, "COTHER")
    assert duplicate.decision == StoreDecision.DUPLICATE
    assert duplicate.channel_id == "CDEFAULT"

    updated_event = replace(
        event,
        lifecycle_status=LifecycleStatus.UPDATED,
        communication="Mitigation is progressing.",
        submission_time=event.submission_time + timedelta(minutes=5),
    )
    updated = store.begin(updated_event, "COTHER")
    assert updated.decision == StoreDecision.UPDATE
    assert updated.message_ts == "123.456"


class FakeStore:
    def __init__(self, work_items):
        self.work_items = list(work_items)
        self.finalized = []
        self.failures = []

    def begin(self, event, channel_id):
        return self.work_items.pop(0)

    def finalize(self, work_item, message_ts, lifecycle_status):
        self.finalized.append((message_ts, lifecycle_status))

    def mark_failed(self, work_item, error_code):
        self.failures.append(error_code)


class FakeNotifier:
    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, event, channel_id, lifecycle_status):
        self.created.append((channel_id, lifecycle_status))
        return "123.456"

    def update(self, event, channel_id, message_ts, lifecycle_status):
        self.updated.append((channel_id, message_ts, lifecycle_status))
        return message_ts


def work_item(decision, message_ts=""):
    return IncidentWorkItem(
        decision,
        {"channelId": "CDEFAULT", "messageTs": message_ts},
        "etag",
    )


def test_processor_creates_then_updates_same_root_message():
    event = parse_service_health_alert(common_alert())
    store = FakeStore([
        work_item(StoreDecision.CREATE),
        work_item(StoreDecision.UPDATE, "123.456"),
    ])
    notifier = FakeNotifier()
    routing = RoutingConfig.from_dict({
        "default_channel_id": "CDEFAULT",
        "rules": [],
    })
    processor = ServiceHealthProcessor(routing, store, notifier)

    created = processor.process(event)
    updated = processor.process(replace(
        event,
        lifecycle_status=LifecycleStatus.RESOLVED,
        submission_time=event.submission_time + timedelta(minutes=10),
    ))
    assert created.result == ProcessingResult.CREATED
    assert updated.result == ProcessingResult.UPDATED
    assert notifier.updated == [
        ("CDEFAULT", "123.456", LifecycleStatus.RESOLVED)
    ]


def test_processor_returns_duplicate_without_calling_slack():
    event = parse_service_health_alert(common_alert())
    store = FakeStore([work_item(StoreDecision.DUPLICATE, "123.456")])
    notifier = FakeNotifier()
    routing = RoutingConfig.from_dict({
        "default_channel_id": "CDEFAULT",
        "rules": [],
    })
    outcome = ServiceHealthProcessor(
        routing, store, notifier).process(event)
    assert outcome.result == ProcessingResult.DUPLICATE
    assert notifier.created == []
    assert notifier.updated == []


def test_processor_returns_transient_error_when_incident_is_busy():
    event = parse_service_health_alert(common_alert())
    store = FakeStore([work_item(StoreDecision.BUSY)])
    routing = RoutingConfig.from_dict({
        "default_channel_id": "CDEFAULT",
        "rules": [],
    })
    with pytest.raises(TransientProcessingError):
        ServiceHealthProcessor(
            routing, store, FakeNotifier()).process(event)


class StubProcessor:
    def __init__(self):
        self.events = []

    def process(self, event):
        self.events.append(event)
        return ProcessingOutcome(ProcessingResult.CREATED, "CDEFAULT", "1.2")


def test_flask_endpoint_parses_and_processes_common_schema():
    settings = ServiceHealthSettings(
        table_endpoint="https://example.table.core.windows.net",
        table_name="ServiceHealthIncidents",
        routing=RoutingConfig.from_dict({
            "default_channel_id": "CDEFAULT",
            "rules": [],
        }),
        app_environment="test",
    )
    processor = StubProcessor()
    runtime = SimpleNamespace(settings=settings, processor=processor)
    flask_app = Flask(__name__)
    flask_app.register_blueprint(
        create_service_health_blueprint(lambda: runtime))

    response = flask_app.test_client().post(
        "/api/service-health",
        json=common_alert(),
        headers={"X-Correlation-ID": "test-correlation"},
    )
    assert response.status_code == 200
    assert response.json["result"] == "created"
    assert response.headers["X-Correlation-ID"] == "test-correlation"
    assert len(processor.events) == 1


def test_flask_endpoint_maps_invalid_payload_to_422():
    settings = ServiceHealthSettings(
        table_endpoint="https://example.table.core.windows.net",
        table_name="ServiceHealthIncidents",
        routing=RoutingConfig.from_dict({
            "default_channel_id": "CDEFAULT",
            "rules": [],
        }),
        app_environment="test",
    )
    runtime = SimpleNamespace(settings=settings, processor=StubProcessor())
    flask_app = Flask(__name__)
    flask_app.register_blueprint(
        create_service_health_blueprint(lambda: runtime))

    response = flask_app.test_client().post(
        "/api/service-health",
        json={"schemaId": "wrong"},
    )
    assert response.status_code == 422


def test_flask_endpoint_requires_easy_auth_in_production():
    settings = ServiceHealthSettings(
        table_endpoint="https://example.table.core.windows.net",
        table_name="ServiceHealthIncidents",
        routing=RoutingConfig.from_dict({
            "default_channel_id": "CDEFAULT",
            "rules": [],
        }),
        app_environment="production",
        expected_audience="api://service-health",
    )
    runtime = SimpleNamespace(settings=settings, processor=StubProcessor())
    flask_app = Flask(__name__)
    flask_app.register_blueprint(
        create_service_health_blueprint(lambda: runtime))

    response = flask_app.test_client().post(
        "/api/service-health",
        json=common_alert(),
    )
    assert response.status_code == 401
