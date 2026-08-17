import logging
from dataclasses import dataclass
from enum import Enum

from service_health.models import LifecycleStatus
from service_health.slack import PermanentSlackError, TransientSlackError
from service_health.storage import (
    InvalidStoreStateError,
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
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"


class TransientProcessingError(RuntimeError):
    pass


class PermanentProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessingOutcome:
    result: ProcessingResult
    channel_id: str
    message_ts: str = ""
    thread_reply_ts: str = ""


class ServiceHealthProcessor:
    def __init__(self, routing, store, notifier):
        self.routing = routing
        self.store = store
        self.notifier = notifier

    def process(self, event):
        try:
            return self._process(event)
        except TransientProcessingError:
            service_health_metrics.record(
                event, ProcessingResult.TRANSIENT_FAILURE)
            raise
        except PermanentProcessingError:
            service_health_metrics.record(
                event, ProcessingResult.PERMANENT_FAILURE)
            raise

    def _process(self, event):
        channel_id = self.routing.channel_for(event)
        try:
            work_item = self.store.begin(event, channel_id)
        except InvalidStoreStateError as exc:
            raise PermanentProcessingError(
                "Incident state is invalid and requires operator repair"
            ) from exc
        except (TransientStoreError, StoreConsistencyError) as exc:
            raise TransientProcessingError(
                "Unable to coordinate incident state") from exc

        if work_item.decision == StoreDecision.DUPLICATE:
            service_health_metrics.record(event, ProcessingResult.DUPLICATE)
            return ProcessingOutcome(
                ProcessingResult.DUPLICATE,
                work_item.channel_id,
                work_item.message_ts,
                work_item.thread_reply_ts,
            )
        if work_item.decision == StoreDecision.STALE:
            service_health_metrics.record(event, ProcessingResult.STALE)
            return ProcessingOutcome(
                ProcessingResult.STALE,
                work_item.channel_id,
                work_item.message_ts,
                work_item.thread_reply_ts,
            )
        if work_item.decision == StoreDecision.BUSY:
            raise TransientProcessingError(
                "Another replica is processing this incident")

        try:
            work_item = self.store.renew(work_item)
        except (TransientStoreError, StoreConsistencyError) as exc:
            raise TransientProcessingError(
                "Unable to renew incident processing lease") from exc

        lifecycle_status = event.lifecycle_status
        if (
            work_item.decision in {
                StoreDecision.UPDATE,
                StoreDecision.REPLY,
            }
            and lifecycle_status == LifecycleStatus.ACTIVE
        ):
            lifecycle_status = LifecycleStatus.UPDATED

        try:
            thread_reply_ts = ""
            if work_item.decision == StoreDecision.CREATE:
                message_ts = self.notifier.create(
                    event, work_item.channel_id, lifecycle_status)
                result = ProcessingResult.CREATED
            else:
                message_ts = work_item.message_ts
                if work_item.decision == StoreDecision.UPDATE:
                    message_ts = self.notifier.update(
                        event,
                        work_item.channel_id,
                        work_item.message_ts,
                        lifecycle_status,
                    )
                    try:
                        work_item = self.store.checkpoint_root(
                            work_item,
                            message_ts,
                            lifecycle_status,
                        )
                    except (
                        TransientStoreError,
                        StoreConsistencyError,
                    ) as exc:
                        raise TransientProcessingError(
                            "Root message was updated but its state "
                            "could not be checkpointed"
                        ) from exc
                thread_reply_ts = self.notifier.reply(
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
            self.store.finalize(
                work_item,
                message_ts,
                lifecycle_status,
                thread_reply_ts,
            )
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
                "message_ts": message_ts,
                "thread_reply_ts": thread_reply_ts,
            },
        )
        service_health_metrics.record(event, result)
        return ProcessingOutcome(
            result,
            work_item.channel_id,
            message_ts,
            thread_reply_ts,
        )

    def _mark_failed(self, work_item, error_code):
        try:
            self.store.mark_failed(work_item, error_code)
        except (TransientStoreError, StoreConsistencyError) as exc:
            raise TransientProcessingError(
                "Unable to record downstream failure") from exc
