import logging
from http.client import RemoteDisconnected
from urllib.error import URLError

from slack_sdk.errors import SlackApiError, SlackRequestError

from service_health.models import LifecycleStatus, ServiceHealthEvent


logger = logging.getLogger(__name__)


class TransientSlackError(RuntimeError):
    pass


class PermanentSlackError(RuntimeError):
    pass


_TRANSIENT_SLACK_ERRORS = {
    "fatal_error",
    "internal_error",
    "ratelimited",
    "rate_limited",
    "request_timeout",
    "service_unavailable",
}

_TRANSIENT_TRANSPORT_ERRORS = (
    SlackRequestError,
    TimeoutError,
    URLError,
    ConnectionResetError,
    RemoteDisconnected,
)


def _escape_mrkdwn(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;")


def _escape_code_span(value):
    return _escape_mrkdwn(value).replace("`", "'")


def _truncate(value, limit):
    if len(value) <= limit:
        return value
    return f"{value[:limit - 1].rstrip()}…"


def _service_health_url(event):
    return (
        "https://portal.azure.com/#view/"
        "Microsoft_Azure_Health/AzureHealthBrowseBlade/~/serviceIssues"
    )


def _status_icon(status):
    return {
        LifecycleStatus.ACTIVE: "🔴",
        LifecycleStatus.UPDATED: "🟠",
        LifecycleStatus.RESOLVED: "🟢",
    }[status]


class SlackIncidentNotifier:
    def __init__(self, client):
        self.client = client

    def create(self, event, channel_id, lifecycle_status):
        text, blocks = render_incident_message(event, lifecycle_status)
        try:
            response = self.client.chat_postMessage(
                channel=channel_id,
                text=text,
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
            return self._required_ts(response, "create")
        except (SlackApiError, *_TRANSIENT_TRANSPORT_ERRORS) as exc:
            self._raise_classified(exc)

    def update(self, event, channel_id, message_ts, lifecycle_status):
        text, blocks = render_incident_message(event, lifecycle_status)
        try:
            self.client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=text,
                blocks=blocks,
            )
            return message_ts
        except (SlackApiError, *_TRANSIENT_TRANSPORT_ERRORS) as exc:
            self._raise_classified(exc)

    def reply(self, event, channel_id, message_ts, lifecycle_status):
        text, blocks = render_incident_update(event, lifecycle_status)
        try:
            response = self.client.chat_postMessage(
                channel=channel_id,
                thread_ts=message_ts,
                reply_broadcast=True,
                text=text,
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
            return self._required_ts(response, "thread reply")
        except (SlackApiError, *_TRANSIENT_TRANSPORT_ERRORS) as exc:
            self._raise_classified(exc)

    @staticmethod
    def _required_ts(response, operation):
        message_ts = response.get("ts")
        if not message_ts:
            raise PermanentSlackError(
                f"Slack {operation} response did not include a message timestamp")
        return message_ts

    @staticmethod
    def _raise_classified(exc):
        if isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS):
            raise TransientSlackError(
                "Slack request failed before receiving a response") from exc

        error_code = exc.response.get("error", "unknown_error")
        status_code = getattr(exc.response, "status_code", 0)
        if (
            status_code in {408, 429, 500, 502, 503, 504}
            or error_code in _TRANSIENT_SLACK_ERRORS
        ):
            raise TransientSlackError(
                f"Transient Slack API error: {error_code}") from exc
        raise PermanentSlackError(
            f"Permanent Slack API error: {error_code}") from exc


def render_incident_message(event: ServiceHealthEvent, lifecycle_status):
    icon = _status_icon(lifecycle_status)
    header = _truncate(
        f"{icon} {lifecycle_status.value}: {event.title}", 150)
    services = []
    for impacted in event.impacted_services:
        regions = ", ".join(impacted.regions) if impacted.regions else "Global"
        services.append(f"• *{_escape_mrkdwn(impacted.name)}*: "
                        f"{_escape_mrkdwn(regions)}")
    impacted_text = _truncate("\n".join(services), 2800)
    communication = _truncate(_escape_mrkdwn(event.communication), 2800)
    portal_url = _service_health_url(event)
    fallback = _truncate(
        f"{icon} Azure Service Health {lifecycle_status.value}: "
        f"{_escape_mrkdwn(event.title)} "
        f"({_escape_mrkdwn(event.tracking_id)})",
        4000,
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Level*\n{event.level.value}",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Incident type*\n"
                        f"{_escape_mrkdwn(event.incident_type or 'Service issue')}"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Impact started*\n"
                        f"{event.impact_start_time.strftime('%Y-%m-%d %H:%M UTC')}"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Subscription*\n"
                        f"`{_escape_code_span(event.subscription_id)}`"
                    ),
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Impacted services and regions*\n{impacted_text}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Latest communication*\n{communication}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Tracking ID: `{_escape_code_span(event.tracking_id)}` · "
                        f"Updated "
                        f"{event.submission_time.strftime('%Y-%m-%d %H:%M UTC')} · "
                        f"<{portal_url}|Open Azure Service Health>"
                    ),
                }
            ],
        },
    ]
    return fallback, blocks


def render_incident_update(event: ServiceHealthEvent, lifecycle_status):
    icon = _status_icon(lifecycle_status)
    portal_url = _service_health_url(event)
    title = _truncate(
        f"{icon} *{lifecycle_status.value}:* "
        f"{_escape_mrkdwn(event.title)}",
        150,
    )
    communication = _escape_mrkdwn(event.communication)
    update_text = _truncate(f"{title}\n\n{communication}", 3000)
    fallback = _truncate(
        f"{icon} Azure Service Health {lifecycle_status.value}: "
        f"{_escape_mrkdwn(event.title)} — "
        f"{_escape_mrkdwn(event.communication)}",
        4000,
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": update_text,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Tracking ID: `{_escape_code_span(event.tracking_id)}` · "
                        f"Updated "
                        f"{event.submission_time.strftime('%Y-%m-%d %H:%M UTC')} · "
                        f"<{portal_url}|Open Azure Service Health>"
                    ),
                }
            ],
        },
    ]
    return fallback, blocks
