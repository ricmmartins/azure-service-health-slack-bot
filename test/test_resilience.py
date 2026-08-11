"""Deterministic resilience, fault-injection, and state-model coverage."""

import copy
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
)
from slack_sdk.errors import SlackApiError

from service_health.models import LifecycleStatus
from service_health.parser import (
    InvalidServiceHealthPayload,
    parse_service_health_alert,
)
from service_health.routing import RoutingConfig
from service_health.service import (
    PermanentProcessingError,
    ServiceHealthProcessor,
    TransientProcessingError,
)
from service_health.slack import (
    PermanentSlackError,
    SlackIncidentNotifier,
    TransientSlackError,
    render_incident_message,
    render_incident_update,
)
from service_health.storage import (
    AzureTableIncidentStore,
    IncidentWorkItem,
    InvalidStoreStateError,
    StoreConsistencyError,
    StoreDecision,
    TransientStoreError,
)


SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def common_alert():
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertId": (
                    f"/subscriptions/{SUBSCRIPTION_ID}/providers/"
                    "Microsoft.Insights/activityLogAlerts/service-health"
                ),
                "alertTargetIDs": [f"/subscriptions/{SUBSCRIPTION_ID}"],
            },
            "alertContext": {
                "eventSource": "ServiceHealth",
                "level": "Warning",
                "status": "Active",
                "submissionTimestamp": "2025-06-01T12:00:00Z",
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
                    "stage": "Active",
                    "communicationId": "comm-1",
                    "incidentType": "Service issue",
                },
            },
        },
    }


class FakeEntity(dict):
    def __init__(self, value, etag):
        super().__init__(value)
        self.metadata = {"etag": etag} if etag else {}


class ConcurrentTableClient:
    def __init__(self):
        self.entity = None
        self.version = 0
        self.lock = threading.RLock()
        self.create_error = None
        self.read_error = None
        self.forced_conflicts = 0
        self.include_etag = True

    def create_entity(self, entity):
        with self.lock:
            if self.create_error:
                raise self.create_error
            if self.entity is not None:
                raise ResourceExistsError("exists")
            self.version += 1
            self.entity = copy.deepcopy(entity)
            return FakeEntity({}, str(self.version))

    def get_entity(self, partition_key, row_key):
        with self.lock:
            if self.read_error:
                raise self.read_error
            assert self.entity["PartitionKey"] == partition_key
            assert self.entity["RowKey"] == row_key
            etag = str(self.version) if self.include_etag else ""
            return FakeEntity(copy.deepcopy(self.entity), etag)

    def update_entity(self, entity, mode, etag, match_condition):
        del mode, match_condition
        with self.lock:
            if self.forced_conflicts:
                self.forced_conflicts -= 1
                self.version += 1
                raise ResourceModifiedError("changed")
            if etag != str(self.version):
                raise ResourceModifiedError("changed")
            self.version += 1
            self.entity = copy.deepcopy(entity)
            return FakeEntity({}, str(self.version))


class RecordingNotifier:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.replied = 0

    def create(self, event, channel_id, lifecycle_status):
        del event, channel_id, lifecycle_status
        self.created += 1
        return "100.000"

    def update(self, event, channel_id, message_ts, lifecycle_status):
        del event, channel_id, lifecycle_status
        self.updated += 1
        return message_ts

    def reply(self, event, channel_id, message_ts, lifecycle_status):
        del event, channel_id, message_ts, lifecycle_status
        self.replied += 1
        return f"100.{self.replied:03d}"


def default_routing():
    return RoutingConfig.from_dict({
        "default_channel_id": "CDEFAULT",
        "rules": [],
    })


def completed_incident():
    now = datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
    table = ConcurrentTableClient()
    store = AzureTableIncidentStore(table, now=lambda: now)
    event = parse_service_health_alert(common_alert())
    work = store.begin(event, "CDEFAULT")
    store.finalize(work, "100.000", LifecycleStatus.ACTIVE)
    return now, table, store, event


def http_error(status_code):
    response = Mock(status_code=status_code, reason="injected", headers={})
    return HttpResponseError(message="injected", response=response)


def slack_error(status_code, error_code):
    response = Mock(status_code=status_code)
    response.get.side_effect = lambda key, default=None: (
        error_code if key == "error" else default
    )
    return SlackApiError("injected", response)


def test_seeded_common_schema_variants_are_deterministic():
    rng = random.Random(0x0200C0DE)
    lifecycle_variants = [
        ("Active", "Active", LifecycleStatus.ACTIVE),
        ("Updated", "Active", LifecycleStatus.UPDATED),
        ("Resolved", "Resolved", LifecycleStatus.RESOLVED),
    ]
    levels = [
        ("Informational", "Informational"),
        ("warning", "Warning"),
        (" ERROR ", "Error"),
        (0, "Critical"),
        (4, "Verbose"),
    ]

    for index in range(200):
        payload = common_alert()
        context = payload["data"]["alertContext"]
        properties = context["properties"]
        stage, status, expected_status = rng.choice(lifecycle_variants)
        raw_level, expected_level = rng.choice(levels)
        context["eventSource"] = rng.choice([
            "ServiceHealth",
            " servicehealth ",
            "SERVICEHEALTH",
            2,
        ])
        context["level"] = raw_level
        context["status"] = status
        properties["stage"] = stage
        properties["trackingId"] = f"TRACK-{index}-\\u03a9"
        properties["title"] = f"Title &amp; {index}"
        properties["communication"] = f"Update &lt;{index}&gt;"
        impacted = [
            {
                "ServiceName": f"Service {index}",
                "ImpactedRegions": [
                    {"RegionName": rng.choice(["East US", "Global", "\\u6771\\u4eac"])}
                    for _ in range(rng.randrange(3))
                ],
            }
        ]
        properties["impactedServices"] = (
            impacted if index % 2 else json.dumps(impacted)
        )
        context["extraField"] = {"ignored": True}
        payload["data"]["essentials"]["extraField"] = ["ignored"]

        event = parse_service_health_alert(payload)
        again = parse_service_health_alert(copy.deepcopy(payload))

        assert event.lifecycle_status == expected_status
        assert event.level.value == expected_level
        assert event.title == f"Title & {index}"
        assert event.communication == f"Update <{index}>"
        assert event.row_key == again.row_key
        assert event.fingerprint == again.fingerprint
        assert len(event.row_key) == 64
        assert set(event.row_key) <= set("0123456789abcdef")


def test_seeded_invalid_common_schema_corpus_fails_closed():
    rng = random.Random(0xBADCA5)
    invalid_levels = [True, None, -1, 5, "Sev3", {}, []]
    invalid_sources = ["Administrative", "", "Health", 1, 3, None]

    for index in range(120):
        payload = common_alert()
        context = payload["data"]["alertContext"]
        properties = context["properties"]
        mutation = rng.randrange(6)
        if mutation == 0:
            context["eventSource"] = rng.choice(invalid_sources)
        elif mutation == 1:
            context["level"] = rng.choice(invalid_levels)
        elif mutation == 2:
            properties["impactedServices"] = rng.choice([
                "not-json",
                "{}",
                "[]",
                [],
                None,
            ])
        elif mutation == 3:
            context["submissionTimestamp"] = f"invalid-{index}"
        elif mutation == 4:
            context["status"] = "Unknown"
            properties["stage"] = "Unknown"
        else:
            properties.pop(rng.choice([
                "trackingId",
                "title",
                "communication",
                "impactStartTime",
            ]))

        with pytest.raises(InvalidServiceHealthPayload):
            parse_service_health_alert(payload)


def test_seeded_routing_precedence_matches_priority_specificity_and_order():
    rng = random.Random(0xC0FFEE)
    event = parse_service_health_alert(common_alert())

    for _case in range(100):
        raw_rules = []
        for order in range(20):
            matches = rng.choice([True, True, False])
            filters = {}
            if rng.choice([True, False]):
                filters["services"] = [
                    "Azure Kubernetes Service" if matches else "Other"
                ]
            if rng.choice([True, False]):
                filters["regions"] = ["East US" if matches else "West Europe"]
            if rng.choice([True, False]):
                filters["subscription_ids"] = [
                    SUBSCRIPTION_ID if matches else "00000000-0000-0000-0000-000000000000"
                ]
            raw_rules.append({
                "channel_id": f"C{order:02d}",
                "priority": rng.randrange(-2, 4),
                **filters,
            })

        routing = RoutingConfig.from_dict({
            "default_channel_id": "CDEFAULT",
            "rules": raw_rules,
        })
        matching = [rule for rule in routing.rules if rule.matches(event)]
        expected = (
            min(
                matching,
                key=lambda rule: (
                    -rule.priority,
                    -rule.specificity,
                    rule.order,
                ),
            ).channel_id
            if matching
            else "CDEFAULT"
        )
        assert routing.channel_for(event) == expected


def test_slack_fallbacks_escape_control_sequences_and_enforce_limits():
    event = replace(
        parse_service_health_alert(common_alert()),
        title="<!channel> <script> " + "T" * 500,
        communication="<!here> & <unsafe> " + "C" * 6000,
        tracking_id="<tracking>",
    )

    root_text, root_blocks = render_incident_message(
        event,
        LifecycleStatus.ACTIVE,
    )
    update_text, update_blocks = render_incident_update(
        event,
        LifecycleStatus.UPDATED,
    )

    assert "<!channel>" not in root_text
    assert "<tracking>" not in root_text
    assert "<!here>" not in update_text
    assert "&lt;!channel&gt;" in root_text
    assert "&lt;!here&gt;" in update_text
    assert len(root_text) <= 4000
    assert len(update_text) <= 4000
    assert len(root_blocks[0]["text"]["text"]) <= 150
    assert len(update_blocks[0]["text"]["text"]) <= 3000


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_slack_retryable_status_codes_are_transient(status_code):
    client = Mock()
    client.chat_postMessage.side_effect = slack_error(
        status_code,
        "injected_error",
    )
    notifier = SlackIncidentNotifier(client)

    with pytest.raises(TransientSlackError):
        notifier.create(
            parse_service_health_alert(common_alert()),
            "CDEFAULT",
            LifecycleStatus.ACTIVE,
        )


@pytest.mark.parametrize(
    "error_code",
    [
        "fatal_error",
        "internal_error",
        "ratelimited",
        "rate_limited",
        "request_timeout",
        "service_unavailable",
    ],
)
def test_slack_retryable_error_codes_are_transient(error_code):
    client = Mock()
    client.chat_update.side_effect = slack_error(200, error_code)
    notifier = SlackIncidentNotifier(client)

    with pytest.raises(TransientSlackError):
        notifier.update(
            parse_service_health_alert(common_alert()),
            "CDEFAULT",
            "100.000",
            LifecycleStatus.UPDATED,
        )


def test_slack_nonretryable_error_is_permanent():
    client = Mock()
    client.chat_postMessage.side_effect = slack_error(400, "invalid_auth")

    with pytest.raises(PermanentSlackError, match="invalid_auth"):
        SlackIncidentNotifier(client).create(
            parse_service_health_alert(common_alert()),
            "CDEFAULT",
            LifecycleStatus.ACTIVE,
        )


def test_ambiguous_slack_transport_failure_retains_duplicate_risk():
    remote_writes = []

    def ambiguous_write(**kwargs):
        remote_writes.append(kwargs)
        raise TimeoutError("response lost after remote acceptance")

    client = Mock()
    client.chat_postMessage.side_effect = ambiguous_write

    with pytest.raises(TransientSlackError):
        SlackIncidentNotifier(client).create(
            parse_service_health_alert(common_alert()),
            "CDEFAULT",
            LifecycleStatus.ACTIVE,
        )

    assert len(remote_writes) == 1


def test_parallel_duplicate_burst_has_one_creator_then_only_duplicates():
    now = datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
    table = ConcurrentTableClient()
    event = parse_service_health_alert(common_alert())
    barrier = threading.Barrier(16)

    def reserve(_index):
        barrier.wait()
        return AzureTableIncidentStore(
            table,
            now=lambda: now,
        ).begin(event, "CDEFAULT")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(16)))

    creators = [
        item for item in results if item.decision == StoreDecision.CREATE
    ]
    assert len(creators) == 1
    assert sum(
        item.decision == StoreDecision.BUSY for item in results
    ) == 15

    store = AzureTableIncidentStore(table, now=lambda: now)
    store.finalize(creators[0], "100.000", LifecycleStatus.ACTIVE)
    duplicates = [
        store.begin(event, "COTHER").decision for _index in range(16)
    ]
    assert duplicates == [StoreDecision.DUPLICATE] * 16
    assert table.entity["channelId"] == "CDEFAULT"


def test_resolved_is_terminal_even_when_later_update_has_newer_timestamp():
    now, table, store, active = completed_incident()
    resolved = replace(
        active,
        lifecycle_status=LifecycleStatus.RESOLVED,
        communication="Resolved.",
        submission_time=active.submission_time + timedelta(minutes=5),
    )
    resolved_work = store.begin(resolved, "CDEFAULT")
    store.finalize(
        resolved_work,
        "100.000",
        LifecycleStatus.RESOLVED,
        "100.001",
    )

    later_updated = replace(
        active,
        lifecycle_status=LifecycleStatus.UPDATED,
        communication="Late nonterminal update.",
        submission_time=active.submission_time + timedelta(minutes=10),
    )
    assert (
        store.begin(later_updated, "CDEFAULT").decision
        == StoreDecision.STALE
    )
    assert table.entity["lifecycleStatus"] == "Resolved"
    assert table.entity["lastFingerprint"] == resolved.fingerprint
    assert now == datetime(2025, 6, 1, 12, tzinfo=timezone.utc)


def test_expired_replay_cannot_erase_pending_resolution():
    current_time = [
        datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
    ]
    table = ConcurrentTableClient()
    store = AzureTableIncidentStore(
        table,
        lease_seconds=30,
        now=lambda: current_time[0],
    )
    active = parse_service_health_alert(common_alert())
    created = store.begin(active, "CDEFAULT")
    store.finalize(created, "100.000", LifecycleStatus.ACTIVE)

    resolved = replace(
        active,
        lifecycle_status=LifecycleStatus.RESOLVED,
        communication="Resolved.",
        submission_time=active.submission_time + timedelta(minutes=5),
    )
    pending = store.begin(resolved, "CDEFAULT")
    assert pending.decision == StoreDecision.UPDATE
    assert table.entity["pendingLifecycleStatus"] == "Resolved"

    current_time[0] += timedelta(seconds=31)
    assert (
        store.begin(active, "CDEFAULT").decision
        == StoreDecision.STALE
    )
    assert table.entity["pendingLifecycleStatus"] == "Resolved"
    assert table.entity["pendingFingerprint"] == resolved.fingerprint

    later_updated = replace(
        active,
        lifecycle_status=LifecycleStatus.UPDATED,
        communication="Late nonterminal update.",
        submission_time=active.submission_time + timedelta(minutes=10),
    )
    assert (
        store.begin(later_updated, "CDEFAULT").decision
        == StoreDecision.STALE
    )
    assert table.entity["pendingFingerprint"] == resolved.fingerprint


def test_pending_watermark_rejects_older_distinct_resolution():
    current_time = [
        datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
    ]
    table = ConcurrentTableClient()
    store = AzureTableIncidentStore(
        table,
        lease_seconds=30,
        now=lambda: current_time[0],
    )
    active = parse_service_health_alert(common_alert())
    created = store.begin(active, "CDEFAULT")
    store.finalize(created, "100.000", LifecycleStatus.ACTIVE)

    newer_resolved = replace(
        active,
        lifecycle_status=LifecycleStatus.RESOLVED,
        communication="Newest resolution.",
        submission_time=active.submission_time + timedelta(minutes=10),
    )
    pending = store.begin(newer_resolved, "CDEFAULT")
    assert pending.decision == StoreDecision.UPDATE
    current_time[0] += timedelta(seconds=31)

    older_resolved = replace(
        active,
        lifecycle_status=LifecycleStatus.RESOLVED,
        communication="Older resolution.",
        submission_time=active.submission_time + timedelta(minutes=5),
    )
    assert (
        store.begin(older_resolved, "CDEFAULT").decision
        == StoreDecision.STALE
    )
    assert table.entity["pendingFingerprint"] == newer_resolved.fingerprint
    assert (
        table.entity["pendingSubmissionTime"]
        == newer_resolved.submission_time
    )

    recovered = store.begin(newer_resolved, "CDEFAULT")
    assert recovered.decision == StoreDecision.UPDATE
    assert recovered.entity["pendingFingerprint"] == newer_resolved.fingerprint


def test_seeded_processor_state_model_never_regresses_or_duplicates_root():
    rng = random.Random(0x5A7E)
    now = datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
    table = ConcurrentTableClient()
    store = AzureTableIncidentStore(table, now=lambda: now)
    notifier = RecordingNotifier()
    processor = ServiceHealthProcessor(default_routing(), store, notifier)
    active = parse_service_health_alert(common_alert())
    processor.process(active)

    rank = {
        "Active": 0,
        "Updated": 1,
        "Resolved": 2,
    }
    previous_rank = rank[table.entity["lifecycleStatus"]]
    for index in range(250):
        event = replace(
            active,
            lifecycle_status=rng.choice(list(LifecycleStatus)),
            communication=f"seeded update {index}",
            submission_time=active.submission_time
            + timedelta(minutes=rng.randrange(-25, 150)),
        )
        processor.process(event)
        current_rank = rank[table.entity["lifecycleStatus"]]
        assert current_rank >= previous_rank
        previous_rank = current_rank

    assert notifier.created == 1
    assert table.entity["messageTs"] == "100.000"
    assert table.entity["channelId"] == "CDEFAULT"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda entity: entity.update(channelId=""),
        lambda entity: entity.update(processingState="unknown"),
        lambda entity: entity.update(attemptCount="one"),
        lambda entity: entity.update(lastSubmissionTime="not-a-time"),
        lambda entity: entity.update(lastSubmissionTime=123),
        lambda entity: entity.update(rootSubmissionTime=[]),
        lambda entity: entity.update(lifecycleStatus="Reopened"),
        lambda entity: entity.update(lifecycleStatus=""),
        lambda entity: entity.update(pendingLifecycleStatus="Reopened"),
        lambda entity: entity.update(pendingLifecycleStatus=""),
        lambda entity: entity.update(messageTs=123),
        lambda entity: entity.update(threadReplyTs=123),
        lambda entity: entity.update(lastFingerprint=123),
        lambda entity: entity.pop("messageTs"),
    ],
)
def test_malformed_table_entities_fail_closed(mutation):
    _now, table, store, active = completed_incident()
    mutation(table.entity)
    later = replace(
        active,
        communication="later",
        submission_time=active.submission_time + timedelta(minutes=5),
    )

    with pytest.raises(StoreConsistencyError):
        store.begin(later, "CDEFAULT")


def test_table_entity_without_etag_fails_closed():
    _now, table, store, active = completed_incident()
    table.include_etag = False
    later = replace(
        active,
        communication="later",
        submission_time=active.submission_time + timedelta(minutes=5),
    )

    with pytest.raises(StoreConsistencyError, match="ETag"):
        store.begin(later, "CDEFAULT")


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (408, TransientStoreError),
        (429, TransientStoreError),
        (500, TransientStoreError),
        (503, TransientStoreError),
        (403, StoreConsistencyError),
        (404, StoreConsistencyError),
    ],
)
def test_table_reservation_error_classification(status_code, error_type):
    table = ConcurrentTableClient()
    table.create_error = http_error(status_code)
    store = AzureTableIncidentStore(table)

    with pytest.raises(error_type):
        store.begin(
            parse_service_health_alert(common_alert()),
            "CDEFAULT",
        )


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (429, TransientStoreError),
        (503, TransientStoreError),
        (403, StoreConsistencyError),
        (404, StoreConsistencyError),
    ],
)
def test_table_read_error_classification(status_code, error_type):
    _now, table, store, active = completed_incident()
    table.read_error = http_error(status_code)

    with pytest.raises(error_type):
        store.begin(
            replace(
                active,
                communication="later",
                submission_time=active.submission_time
                + timedelta(minutes=5),
            ),
            "CDEFAULT",
        )


def test_repeated_etag_conflicts_are_bounded_and_retryable():
    _now, table, store, active = completed_incident()
    table.forced_conflicts = 3
    later = replace(
        active,
        communication="later",
        submission_time=active.submission_time + timedelta(minutes=5),
    )

    with pytest.raises(TransientStoreError, match="changed too often"):
        store.begin(later, "CDEFAULT")
    assert table.forced_conflicts == 0


def work_item(decision):
    return IncidentWorkItem(
        decision,
        {
            "channelId": "CDEFAULT",
            "messageTs": "100.000" if decision != StoreDecision.CREATE else "",
            "threadReplyTs": "",
        },
        "etag",
    )


def test_root_checkpoint_failure_prevents_thread_reply():
    store = Mock()
    item = work_item(StoreDecision.UPDATE)
    store.begin.return_value = item
    store.renew.return_value = item
    store.checkpoint_root.side_effect = TransientStoreError("injected")
    notifier = RecordingNotifier()
    processor = ServiceHealthProcessor(default_routing(), store, notifier)

    with pytest.raises(TransientProcessingError, match="checkpointed"):
        processor.process(parse_service_health_alert(common_alert()))

    assert notifier.updated == 1
    assert notifier.replied == 0
    store.finalize.assert_not_called()


@pytest.mark.parametrize(
    "decision",
    [StoreDecision.CREATE, StoreDecision.REPLY],
)
def test_post_slack_finalization_failure_is_retryable_and_ambiguous(decision):
    store = Mock()
    item = work_item(decision)
    store.begin.return_value = item
    store.renew.return_value = item
    store.finalize.side_effect = TransientStoreError("injected")
    notifier = RecordingNotifier()
    processor = ServiceHealthProcessor(default_routing(), store, notifier)

    with pytest.raises(TransientProcessingError, match="finalized"):
        processor.process(parse_service_health_alert(common_alert()))

    if decision == StoreDecision.CREATE:
        assert notifier.created == 1
    else:
        assert notifier.replied == 1


def test_reply_only_recovery_does_not_update_root_again():
    store = Mock()
    item = work_item(StoreDecision.REPLY)
    store.begin.return_value = item
    store.renew.return_value = item
    notifier = RecordingNotifier()
    processor = ServiceHealthProcessor(default_routing(), store, notifier)

    processor.process(parse_service_health_alert(common_alert()))

    assert notifier.updated == 0
    assert notifier.replied == 1
    store.checkpoint_root.assert_not_called()
    store.finalize.assert_called_once()


def test_malformed_persisted_state_is_nonretryable_processing_failure():
    store = Mock()
    store.begin.side_effect = InvalidStoreStateError("injected")
    processor = ServiceHealthProcessor(
        default_routing(),
        store,
        RecordingNotifier(),
    )

    with pytest.raises(PermanentProcessingError, match="operator repair"):
        processor.process(parse_service_health_alert(common_alert()))
