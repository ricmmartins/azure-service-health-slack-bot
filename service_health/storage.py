import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.data.tables import UpdateMode

from service_health.models import LifecycleStatus, ServiceHealthEvent


class StoreDecision(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DUPLICATE = "duplicate"
    STALE = "stale"
    BUSY = "busy"


class TransientStoreError(RuntimeError):
    pass


class StoreConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class IncidentWorkItem:
    decision: StoreDecision
    entity: dict
    etag: str = ""

    @property
    def channel_id(self):
        return self.entity["channelId"]

    @property
    def message_ts(self):
        return self.entity.get("messageTs", "")


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _etag(entity):
    metadata = getattr(entity, "metadata", None)
    if metadata and metadata.get("etag"):
        return metadata["etag"]
    return entity.get("etag") or entity.get("odata.etag") or ""


def _event_properties(event):
    return {
        "trackingId": event.tracking_id,
        "pendingFingerprint": event.fingerprint,
        "pendingSubmissionTime": event.submission_time,
        "pendingLifecycleStatus": event.lifecycle_status.value,
        "level": event.level.value,
        "title": event.title,
        "impactStartTime": event.impact_start_time,
        "communication": event.communication,
        "impactedServicesJson": json.dumps(
            [item.as_dict() for item in event.impacted_services],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        "incidentType": event.incident_type,
        "communicationId": event.communication_id,
        "eventDataId": event.event_data_id,
    }


class AzureTableIncidentStore:
    def __init__(self, table_client, lease_seconds=30, now=_utcnow):
        self.table_client = table_client
        self.lease_seconds = lease_seconds
        self.now = now

    def begin(self, event: ServiceHealthEvent, channel_id):
        now = self.now()
        entity = {
            "PartitionKey": event.partition_key,
            "RowKey": event.row_key,
            "channelId": channel_id,
            "messageTs": "",
            "processingState": "processing",
            "leaseUntil": now + timedelta(seconds=self.lease_seconds),
            "attemptCount": 1,
            "createdAt": now,
            "updatedAt": now,
            "lastFingerprint": "",
            "lastSubmissionTime": None,
            "lastErrorCode": "",
            **_event_properties(event),
        }
        try:
            self.table_client.create_entity(entity)
            created = self._get(event)
            return IncidentWorkItem(
                StoreDecision.CREATE, created, _etag(created))
        except ResourceExistsError:
            return self._begin_existing(event, now)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to reserve incident state") from exc
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientStoreError(
                    "Unable to reserve incident state") from exc
            raise

    def _begin_existing(self, event, now):
        for _ in range(3):
            current = self._get(event)
            if (
                current.get("lastFingerprint") == event.fingerprint
                and current.get("processingState") == "complete"
            ):
                return IncidentWorkItem(
                    StoreDecision.DUPLICATE, current, _etag(current))

            last_submission = _parse_datetime(
                current.get("lastSubmissionTime"))
            if last_submission and event.submission_time <= last_submission:
                return IncidentWorkItem(
                    StoreDecision.STALE, current, _etag(current))

            lease_until = _parse_datetime(current.get("leaseUntil"))
            if (
                current.get("processingState") == "processing"
                and lease_until
                and lease_until > now
            ):
                return IncidentWorkItem(
                    StoreDecision.BUSY, current, _etag(current))

            current.update({
                "processingState": "processing",
                "leaseUntil": now + timedelta(seconds=self.lease_seconds),
                "attemptCount": int(current.get("attemptCount", 0)) + 1,
                "updatedAt": now,
                "lastErrorCode": "",
                **_event_properties(event),
            })
            try:
                self._replace(current, _etag(current))
                acquired = self._get(event)
                decision = (
                    StoreDecision.UPDATE
                    if acquired.get("messageTs")
                    else StoreDecision.CREATE
                )
                return IncidentWorkItem(
                    decision, acquired, _etag(acquired))
            except ResourceModifiedError:
                continue
        raise TransientStoreError(
            "Incident state changed too often to acquire a lease")

    def finalize(
            self, work_item, message_ts, lifecycle_status: LifecycleStatus):
        entity = dict(work_item.entity)
        entity.update({
            "messageTs": message_ts,
            "lifecycleStatus": lifecycle_status.value,
            "lastFingerprint": entity["pendingFingerprint"],
            "lastSubmissionTime": entity["pendingSubmissionTime"],
            "processingState": "complete",
            "leaseUntil": None,
            "lastErrorCode": "",
            "updatedAt": self.now(),
        })
        try:
            self._replace(entity, work_item.etag)
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident state changed before it could be finalized") from exc

    def mark_failed(self, work_item, error_code):
        entity = dict(work_item.entity)
        entity.update({
            "processingState": "failed",
            "leaseUntil": None,
            "lastErrorCode": str(error_code)[:128],
            "updatedAt": self.now(),
        })
        try:
            self._replace(entity, work_item.etag)
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident state changed before failure could be recorded") from exc

    def _get(self, event):
        try:
            return self.table_client.get_entity(
                partition_key=event.partition_key,
                row_key=event.row_key,
            )
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to read incident state") from exc
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientStoreError(
                    "Unable to read incident state") from exc
            raise

    def _replace(self, entity, etag):
        if not etag:
            raise StoreConsistencyError(
                "Azure Table entity did not include an ETag")
        try:
            return self.table_client.update_entity(
                entity=entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError:
            raise
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to update incident state") from exc
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientStoreError(
                    "Unable to update incident state") from exc
            raise StoreConsistencyError(
                "Azure Table rejected the incident state update") from exc
