import json
import uuid
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

_PROCESSING_STATES = {
    "processing",
    "reply_pending",
    "complete",
    "failed",
}


class StoreDecision(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    REPLY = "reply"
    DUPLICATE = "duplicate"
    STALE = "stale"
    BUSY = "busy"


class TransientStoreError(RuntimeError):
    pass


class StoreConsistencyError(RuntimeError):
    pass


class InvalidStoreStateError(StoreConsistencyError):
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

    @property
    def thread_reply_ts(self):
        return self.entity.get("threadReplyTs", "")


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
        lease_owner = uuid.uuid4().hex
        entity = {
            "PartitionKey": event.partition_key,
            "RowKey": event.row_key,
            "channelId": channel_id,
            "messageTs": "",
            "threadReplyTs": "",
            "processingState": "processing",
            "leaseUntil": now + timedelta(seconds=self.lease_seconds),
            "leaseOwner": lease_owner,
            "attemptCount": 1,
            "createdAt": now,
            "updatedAt": now,
            "lastFingerprint": "",
            "lastSubmissionTime": None,
            "rootFingerprint": "",
            "rootSubmissionTime": None,
            "lastErrorCode": "",
            **_event_properties(event),
        }
        try:
            response = self.table_client.create_entity(entity)
            return self._owned_work_item(
                StoreDecision.CREATE,
                entity,
                response,
            )
        except ResourceExistsError:
            return self._begin_existing(event, now)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to reserve incident state") from exc
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientStoreError(
                    "Unable to reserve incident state") from exc
            raise StoreConsistencyError(
                "Azure Table rejected the incident reservation") from exc

    def _begin_existing(self, event, now):
        for _ in range(3):
            current = self._get(event)
            self._validate_existing(current, event)
            if (
                current.get("lastFingerprint") == event.fingerprint
                and current.get("processingState") == "complete"
            ):
                return IncidentWorkItem(
                    StoreDecision.DUPLICATE, current, _etag(current))

            if (
                current.get("pendingLifecycleStatus")
                == LifecycleStatus.RESOLVED.value
                and event.lifecycle_status != LifecycleStatus.RESOLVED
            ):
                return IncidentWorkItem(
                    StoreDecision.STALE, current, _etag(current))

            lease_until = _parse_datetime(current.get("leaseUntil"))
            lease_active = (
                current.get("processingState") in {
                    "processing",
                    "reply_pending",
                }
                and lease_until
                and lease_until > now
            )
            if (
                current.get("rootFingerprint") == event.fingerprint
                and current.get("pendingFingerprint") == event.fingerprint
                and current.get("messageTs")
            ):
                if lease_active:
                    return IncidentWorkItem(
                        StoreDecision.BUSY, current, _etag(current))
                lease_owner = uuid.uuid4().hex
                attempt_count = current["attemptCount"]
                current.update({
                    "processingState": "reply_pending",
                    "leaseUntil": now + timedelta(seconds=self.lease_seconds),
                    "leaseOwner": lease_owner,
                    "attemptCount": attempt_count + 1,
                    "updatedAt": now,
                    "lastErrorCode": "",
                    **_event_properties(event),
                })
                try:
                    response = self._replace(current, _etag(current))
                    return self._owned_work_item(
                        StoreDecision.REPLY,
                        current,
                        response,
                    )
                except ResourceModifiedError:
                    continue

            if (
                current.get("lifecycleStatus")
                == LifecycleStatus.RESOLVED.value
                and event.lifecycle_status != LifecycleStatus.RESOLVED
            ):
                return IncidentWorkItem(
                    StoreDecision.STALE, current, _etag(current))

            last_submission = _parse_datetime(
                current.get("lastSubmissionTime"))
            root_submission = _parse_datetime(
                current.get("rootSubmissionTime"))
            pending_submission = _parse_datetime(
                current.get("pendingSubmissionTime"))
            watermarks = [
                value for value in (last_submission, root_submission) if value
            ]
            if (
                current.get("pendingFingerprint") != event.fingerprint
                and pending_submission
            ):
                watermarks.append(pending_submission)
            latest_submission = max(watermarks) if watermarks else None
            if (
                latest_submission
                and event.submission_time <= latest_submission
            ):
                return IncidentWorkItem(
                    StoreDecision.STALE, current, _etag(current))

            if lease_active:
                return IncidentWorkItem(
                    StoreDecision.BUSY, current, _etag(current))

            lease_owner = uuid.uuid4().hex
            attempt_count = current["attemptCount"]
            current.update({
                "processingState": "processing",
                "leaseUntil": now + timedelta(seconds=self.lease_seconds),
                "leaseOwner": lease_owner,
                "attemptCount": attempt_count + 1,
                "updatedAt": now,
                "lastErrorCode": "",
                **_event_properties(event),
            })
            try:
                response = self._replace(current, _etag(current))
                decision = (
                    StoreDecision.UPDATE
                    if current.get("messageTs")
                    else StoreDecision.CREATE
                )
                return self._owned_work_item(
                    decision,
                    current,
                    response,
                )
            except ResourceModifiedError:
                continue
        raise TransientStoreError(
            "Incident state changed too often to acquire a lease")

    def _validate_existing(self, current, event):
        if not hasattr(current, "get"):
            raise InvalidStoreStateError(
                "Incident state is not a valid entity")
        if (
            current.get("PartitionKey") != event.partition_key
            or current.get("RowKey") != event.row_key
        ):
            raise InvalidStoreStateError(
                "Incident state key does not match the requested event")
        if (
            not isinstance(current.get("channelId"), str)
            or not current["channelId"].strip()
        ):
            raise InvalidStoreStateError(
                "Incident state has no valid channel id")
        if current.get("processingState") not in _PROCESSING_STATES:
            raise InvalidStoreStateError(
                "Incident state has an invalid processing state")
        if not _etag(current):
            raise InvalidStoreStateError(
                "Incident state did not include an ETag")
        attempt_count = current.get("attemptCount")
        if (
            not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 1
        ):
            raise InvalidStoreStateError(
                "Incident state contains an invalid attempt count")

        def parsed_timestamp(field_name):
            raw_value = current.get(field_name)
            if raw_value is None:
                return None
            if not isinstance(raw_value, (datetime, str)) or (
                isinstance(raw_value, str) and not raw_value
            ):
                raise InvalidStoreStateError(
                    f"Incident state contains an invalid {field_name}")
            try:
                parsed = _parse_datetime(raw_value)
            except ValueError as exc:
                raise InvalidStoreStateError(
                    f"Incident state contains an invalid {field_name}"
                ) from exc
            if parsed is None:
                raise InvalidStoreStateError(
                    f"Incident state contains an invalid {field_name}")
            return parsed

        lease_until = parsed_timestamp("leaseUntil")
        last_submission = parsed_timestamp("lastSubmissionTime")
        root_submission = parsed_timestamp("rootSubmissionTime")
        pending_submission = parsed_timestamp("pendingSubmissionTime")

        for field_name in (
            "messageTs",
            "threadReplyTs",
            "lastFingerprint",
            "rootFingerprint",
            "pendingFingerprint",
        ):
            value = current.get(field_name)
            if value is not None and not isinstance(value, str):
                raise InvalidStoreStateError(
                    f"Incident state contains an invalid {field_name}")

        state = current["processingState"]
        if current.get("processingState") in {
            "processing",
            "reply_pending",
        } and (
            lease_until is None
            or not isinstance(current.get("leaseOwner"), str)
            or not current["leaseOwner"]
        ):
            raise InvalidStoreStateError(
                "In-progress incident state has no valid lease")

        lifecycle_status = current.get("lifecycleStatus")
        valid_lifecycle = lifecycle_status in {
            status.value for status in LifecycleStatus
        }
        if lifecycle_status is not None and not valid_lifecycle:
            raise InvalidStoreStateError(
                "Incident state has an invalid lifecycle status")
        pending_lifecycle = current.get("pendingLifecycleStatus")
        if pending_lifecycle not in {
            status.value for status in LifecycleStatus
        }:
            raise InvalidStoreStateError(
                "Incident state has an invalid pending lifecycle status")
        if state == "complete":
            if (
                not valid_lifecycle
                or not current.get("messageTs")
                or not current.get("lastFingerprint")
                or not current.get("rootFingerprint")
                or last_submission is None
                or root_submission is None
            ):
                raise InvalidStoreStateError(
                    "Completed incident state is missing required fields")
        if state == "reply_pending":
            if (
                not valid_lifecycle
                or not current.get("messageTs")
                or not current.get("rootFingerprint")
                or not current.get("pendingFingerprint")
                or root_submission is None
                or pending_submission is None
            ):
                raise InvalidStoreStateError(
                    "Reply-pending incident state is missing required fields")
        if state == "processing" and (
            not current.get("pendingFingerprint")
            or pending_submission is None
        ):
            raise InvalidStoreStateError(
                "Processing incident state is missing required fields")
        if state == "failed" and current.get("messageTs") and (
            not valid_lifecycle
            or not current.get("rootFingerprint")
            or root_submission is None
        ):
            raise InvalidStoreStateError(
                "Failed incident state is missing its root checkpoint")

    def renew(self, work_item):
        entity = dict(work_item.entity)
        now = self.now()
        entity.update({
            "leaseUntil": now + timedelta(seconds=self.lease_seconds),
            "updatedAt": now,
        })
        try:
            response = self._replace(entity, work_item.etag)
            return self._owned_work_item(
                work_item.decision,
                entity,
                response,
            )
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident lease changed before it could be renewed") from exc

    def checkpoint_root(
            self, work_item, message_ts,
            lifecycle_status: LifecycleStatus):
        entity = dict(work_item.entity)
        now = self.now()
        entity.update({
            "messageTs": message_ts,
            "lifecycleStatus": lifecycle_status.value,
            "rootFingerprint": entity["pendingFingerprint"],
            "rootSubmissionTime": entity["pendingSubmissionTime"],
            "processingState": "reply_pending",
            "leaseUntil": now + timedelta(seconds=self.lease_seconds),
            "lastErrorCode": "",
            "updatedAt": now,
        })
        try:
            response = self._replace(entity, work_item.etag)
            return self._owned_work_item(
                StoreDecision.REPLY,
                entity,
                response,
            )
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident state changed before the root checkpoint") from exc

    def finalize(
            self, work_item, message_ts, lifecycle_status: LifecycleStatus,
            thread_reply_ts=""):
        entity = dict(work_item.entity)
        entity.update({
            "messageTs": message_ts,
            "threadReplyTs": thread_reply_ts,
            "lifecycleStatus": lifecycle_status.value,
            "lastFingerprint": entity["pendingFingerprint"],
            "lastSubmissionTime": entity["pendingSubmissionTime"],
            "rootFingerprint": entity["pendingFingerprint"],
            "rootSubmissionTime": entity["pendingSubmissionTime"],
            "processingState": "complete",
            "leaseUntil": None,
            "leaseOwner": "",
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
            "leaseOwner": "",
            "lastErrorCode": str(error_code)[:128],
            "updatedAt": self.now(),
        })
        try:
            self._replace(entity, work_item.etag)
        except ResourceModifiedError as exc:
            raise StoreConsistencyError(
                "Incident state changed before failure could be recorded") from exc

    def _get(self, event):
        return self._get_entity(event.partition_key, event.row_key)

    def _get_entity(self, partition_key, row_key):
        try:
            return self.table_client.get_entity(
                partition_key=partition_key,
                row_key=row_key,
            )
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise TransientStoreError(
                "Unable to read incident state") from exc
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientStoreError(
                    "Unable to read incident state") from exc
            raise StoreConsistencyError(
                "Azure Table rejected the incident state read") from exc

    def _owned_work_item(self, decision, entity, response=None):
        lease_until = _parse_datetime(entity.get("leaseUntil"))
        if not lease_until or lease_until <= self.now():
            raise TransientStoreError(
                "Incident lease expired during state coordination")

        response_etag = _etag(response) if response else ""
        if response_etag:
            return IncidentWorkItem(
                decision,
                dict(entity),
                response_etag,
            )

        current = self._get_entity(
            entity["PartitionKey"], entity["RowKey"])
        if (
            current.get("leaseOwner") != entity.get("leaseOwner")
            or current.get("pendingFingerprint")
            != entity.get("pendingFingerprint")
        ):
            raise StoreConsistencyError(
                "Incident lease ownership changed during state coordination")
        current_lease = _parse_datetime(current.get("leaseUntil"))
        if not current_lease or current_lease <= self.now():
            raise TransientStoreError(
                "Incident lease expired during state coordination")
        return IncidentWorkItem(decision, current, _etag(current))

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
