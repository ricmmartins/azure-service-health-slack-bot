import logging
from dataclasses import dataclass
from enum import Enum

from service_health.models import LifecycleStatus
from service_health.slack import PermanentSlackError, TransientSlackError
from service_health.storage import (
    StoreConsistencyError,
    StoreDecision,
    TransientStoreError,
)
from service_health.telemetry import service_health_metrics


logger = logging.getLogger(__name__)


class ProcessingResult(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DUPLICATE = "duplicate"
    STALE = "stale"


class TransientProcessingError(RuntimeError):
    pass


class PermanentProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessingOutcome:
    result: ProcessingResult
    channel_id: str
    message_ts: str = ""


class ServiceHealthProcessor:
    def __init__(self, routing, store, notifier):
        self.routing = routing
        self.store = store
        self.notifier = notifier

    def process(self, event):
        channel_id = self.routing.channel_for(event)
        try:
            work_item = self.store.begin(event, channel_id)
        except (TransientStoreError, StoreConsistencyError) as exc:
            raise TransientProcessingError(
                "Unable to coordinate incident state") from exc

        if work_item.decision == StoreDecision.DUPLICATE:
            service_health_metrics.record(event, ProcessingResult.DUPLICATE)
            return ProcessingOutcome(
                ProcessingResult.DUPLICATE,
                work_item.channel_id,
                work_item.message_ts,
            )
        if work_item.decision == StoreDecision.STALE:
            service_health_metrics.record(event, ProcessingResult.STALE)
            return ProcessingOutcome(
                ProcessingResult.STALE,
                work_item.channel_id,
                work_item.message_ts,
            )
        if work_item.decision == StoreDecision.BUSY:
            raise TransientProcessingError(
                "Another replica is processing this incident")

        lifecycle_status = event.lifecycle_status
        if (
            work_item.decision == StoreDecision.UPDATE
            and lifecycle_status == LifecycleStatus.ACTIVE
        ):
            lifecycle_status = LifecycleStatus.UPDATED

        try:
            if work_item.decision == StoreDecision.CREATE:
                message_ts = self.notifier.create(
                    event, work_item.channel_id, lifecycle_status)
                result = ProcessingResult.CREATED
            else:
                message_ts = self.notifier.update(
                    event,
                    work_item.channel_id,
                    work_item.message_ts,
                    lifecycle_status,
                )
                result = ProcessingResult.UPDATED
        except TransientSlackError as exc:
            self._mark_failed(work_item, "slack_transient")
            raise TransientProcessingError(
                "Slack is temporarily unavailable") from exc
        except PermanentSlackError as exc:
            self._mark_failed(work_item, "slack_permanent")
            raise PermanentProcessingError(
                "Slack rejected the incident message") from exc

        try:
            self.store.finalize(work_item, message_ts, lifecycle_status)
        except (TransientStoreError, StoreConsistencyError) as exc:
            raise TransientProcessingError(
                "Message was sent but incident state could not be finalized"
            ) from exc

        logger.info(
            "Service Health incident processed",
            extra={
                "service_health_result": result.value,
                "tracking_id": event.tracking_id,
                "channel_id": work_item.channel_id,
            },
        )
        service_health_metrics.record(event, result)
        return ProcessingOutcome(result, work_item.channel_id, message_ts)

    def _mark_failed(self, work_item, error_code):
        try:
            self.store.mark_failed(work_item, error_code)
        except (TransientStoreError, StoreConsistencyError) as exc:
            raise TransientProcessingError(
                "Unable to record downstream failure") from exc
