import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class LifecycleStatus(str, Enum):
    ACTIVE = "Active"
    UPDATED = "Updated"
    RESOLVED = "Resolved"


class AlertLevel(str, Enum):
    INFORMATIONAL = "Informational"
    WARNING = "Warning"
    ERROR = "Error"
    CRITICAL = "Critical"
    VERBOSE = "Verbose"


@dataclass(frozen=True)
class ImpactedService:
    name: str
    regions: tuple[str, ...]

    def as_dict(self):
        return {"service": self.name, "regions": list(self.regions)}


@dataclass(frozen=True)
class ServiceHealthEvent:
    tracking_id: str
    subscription_id: str
    lifecycle_status: LifecycleStatus
    level: AlertLevel
    title: str
    impact_start_time: datetime
    communication: str
    impacted_services: tuple[ImpactedService, ...]
    submission_time: datetime
    incident_type: str = ""
    communication_id: str = ""
    event_data_id: str = ""

    @property
    def partition_key(self):
        return self.subscription_id.lower()

    @property
    def row_key(self):
        return hashlib.sha256(self.tracking_id.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self):
        canonical = {
            "trackingId": self.tracking_id,
            "subscriptionId": self.subscription_id.lower(),
            "status": self.lifecycle_status.value,
            "level": self.level.value,
            "title": self.title,
            "impactStartTime": self.impact_start_time.isoformat(),
            "communication": self.communication,
            "impactedServices": [item.as_dict() for item in self.impacted_services],
            "submissionTime": self.submission_time.isoformat(),
            "incidentType": self.incident_type,
            "communicationId": self.communication_id,
            "eventDataId": self.event_data_id,
        }
        encoded = json.dumps(
            canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def service_names(self):
        return frozenset(item.name.casefold() for item in self.impacted_services)

    @property
    def region_names(self):
        return frozenset(
            region.casefold()
            for item in self.impacted_services
            for region in item.regions
        )
