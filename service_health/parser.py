import html
import json
import re
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
    r"/subscriptions/([0-9a-fA-F-]{36})(?:/|$)", re.IGNORECASE)
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
    candidates = [
        properties.get("stage"),
        context.get("status"),
    ]
    normalized = [
        item.strip().casefold()
        for item in candidates
        if isinstance(item, str) and item.strip()
    ]
    if "resolved" in normalized:
        return LifecycleStatus.RESOLVED
    if "updated" in normalized:
        return LifecycleStatus.UPDATED
    if "active" in normalized:
        return LifecycleStatus.ACTIVE
    raise InvalidServiceHealthPayload(
        "Service Health status must be Active, Updated, or Resolved")


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
        return value.strip()

    candidates = [essentials.get("alertId")]
    candidates.extend(essentials.get("alertTargetIDs") or [])
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        match = _SUBSCRIPTION_PATTERN.search(candidate)
        if match:
            return match.group(1)
    raise InvalidServiceHealthPayload(
        "Missing subscriptionId in alert context and essentials")


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
