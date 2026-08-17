import html
import json
import re
import uuid
from datetime import datetime, timezone

from service_health.models import (
    AlertLevel,
    ImpactedService,
    LifecycleStatus,
    ServiceHealthEvent,
)


class InvalidServiceHealthPayload(ValueError):
    pass


_SUBSCRIPTION_PATTERN = re.compile(
    r"/subscriptions/([^/]+)(?:/|$)", re.IGNORECASE)
_LEVEL_BY_NUMBER = {
    0: AlertLevel.CRITICAL,
    1: AlertLevel.ERROR,
    2: AlertLevel.WARNING,
    3: AlertLevel.INFORMATIONAL,
    4: AlertLevel.VERBOSE,
}


def _required_string(mapping, key):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidServiceHealthPayload(f"Missing or invalid '{key}'")
    return value.strip()


def _optional_string(mapping, key):
    value = mapping.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidServiceHealthPayload(f"Invalid '{key}'")
    return value.strip()


def _parse_datetime(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise InvalidServiceHealthPayload(
            f"Missing or invalid '{field_name}'")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise InvalidServiceHealthPayload(
            f"Invalid '{field_name}'") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_level(value):
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return _LEVEL_BY_NUMBER[value]
        except KeyError as exc:
            raise InvalidServiceHealthPayload(
                f"Unsupported numeric level '{value}'") from exc
    if isinstance(value, str):
        normalized = value.strip().casefold()
        for level in AlertLevel:
            if level.value.casefold() == normalized:
                return level
    raise InvalidServiceHealthPayload(f"Unsupported level '{value}'")


def _is_service_health(value):
    return value == 2 or (
        isinstance(value, str) and value.strip().casefold() == "servicehealth")


def _parse_lifecycle(context, properties):
    stage = properties.get("stage")
    status = context.get("status")
    normalized_stage = (
        stage.strip().casefold()
        if isinstance(stage, str)
        else ""
    )
    normalized_status = (
        status.strip().casefold()
        if isinstance(status, str)
        else ""
    )
    if (
        normalized_status == "resolved"
        or normalized_stage in {
            "canceled",
            "cancelled",
            "complete",
            "resolved",
            "rca",
        }
    ):
        return LifecycleStatus.RESOLVED
    if normalized_stage in {"inprogress", "rescheduled", "updated"}:
        return LifecycleStatus.UPDATED
    if (
        normalized_status == "active"
        or normalized_stage in {"active", "planned"}
    ):
        return LifecycleStatus.ACTIVE
    raise InvalidServiceHealthPayload(
        "Service Health status or stage is unsupported")


def _parse_impacted_services(raw_value):
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise InvalidServiceHealthPayload(
                "Invalid escaped JSON in 'impactedServices'") from exc
    elif isinstance(raw_value, list):
        decoded = raw_value
    else:
        raise InvalidServiceHealthPayload(
            "Missing or invalid 'impactedServices'")
    if not isinstance(decoded, list) or not decoded:
        raise InvalidServiceHealthPayload(
            "'impactedServices' must be a non-empty list")

    result = []
    for item in decoded:
        if not isinstance(item, dict):
            raise InvalidServiceHealthPayload(
                "Invalid service in 'impactedServices'")
        name = item.get("ServiceName")
        regions = item.get("ImpactedRegions")
        if not isinstance(name, str) or not name.strip():
            raise InvalidServiceHealthPayload(
                "Missing ServiceName in 'impactedServices'")
        if not isinstance(regions, list):
            raise InvalidServiceHealthPayload(
                "Missing ImpactedRegions in 'impactedServices'")
        region_names = []
        for region in regions:
            if not isinstance(region, dict):
                raise InvalidServiceHealthPayload(
                    "Invalid impacted region")
            region_name = region.get("RegionName")
            if not isinstance(region_name, str) or not region_name.strip():
                raise InvalidServiceHealthPayload(
                    "Missing RegionName in 'impactedServices'")
            region_names.append(region_name.strip())
        result.append(ImpactedService(name.strip(), tuple(region_names)))
    return tuple(result)


def _find_subscription_id(context, essentials):
    value = context.get("subscriptionId")
    if isinstance(value, str) and value.strip():
        return _canonical_subscription_id(value, "alertContext.subscriptionId")
    if value is not None:
        raise InvalidServiceHealthPayload(
            "Invalid 'alertContext.subscriptionId'")

    candidates = [essentials.get("alertId")]
    candidates.extend(essentials.get("alertTargetIDs") or [])
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        match = _SUBSCRIPTION_PATTERN.search(candidate)
        if match:
            return _canonical_subscription_id(
                match.group(1), "subscription id in alert essentials")
    raise InvalidServiceHealthPayload(
        "Missing subscriptionId in alert context and essentials")


def _canonical_subscription_id(value, field_name):
    try:
        return str(uuid.UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise InvalidServiceHealthPayload(
            f"Invalid '{field_name}'") from exc


def parse_service_health_alert(payload):
    if not isinstance(payload, dict):
        raise InvalidServiceHealthPayload("Request body must be a JSON object")
    if payload.get("schemaId") != "azureMonitorCommonAlertSchema":
        raise InvalidServiceHealthPayload(
            "schemaId must be 'azureMonitorCommonAlertSchema'")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidServiceHealthPayload("Missing or invalid 'data'")
    essentials = data.get("essentials")
    context = data.get("alertContext")
    if not isinstance(essentials, dict):
        raise InvalidServiceHealthPayload(
            "Missing or invalid 'data.essentials'")
    if not isinstance(context, dict):
        raise InvalidServiceHealthPayload(
            "Missing or invalid 'data.alertContext'")
    if not _is_service_health(context.get("eventSource")):
        raise InvalidServiceHealthPayload(
            "alertContext.eventSource is not ServiceHealth")

    properties = context.get("properties")
    if not isinstance(properties, dict):
        raise InvalidServiceHealthPayload(
            "Missing or invalid 'alertContext.properties'")

    return ServiceHealthEvent(
        tracking_id=_required_string(properties, "trackingId"),
        subscription_id=_find_subscription_id(context, essentials),
        lifecycle_status=_parse_lifecycle(context, properties),
        level=_parse_level(context.get("level")),
        title=html.unescape(_required_string(properties, "title")),
        impact_start_time=_parse_datetime(
            properties.get("impactStartTime"), "impactStartTime"),
        communication=html.unescape(
            _required_string(properties, "communication")),
        impacted_services=_parse_impacted_services(
            properties.get("impactedServices")),
        submission_time=_parse_datetime(
            context.get("submissionTimestamp")
            or context.get("eventTimestamp"),
            "submissionTimestamp",
        ),
        incident_type=_optional_string(properties, "incidentType"),
        communication_id=_optional_string(properties, "communicationId"),
        event_data_id=_optional_string(context, "eventDataId"),
    )
