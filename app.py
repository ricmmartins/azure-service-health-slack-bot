"""Standalone Azure Service Health to Slack webhook service.

Exposes exactly three HTTP routes:

* ``POST /api/service-health`` - Azure Monitor Common Alert Schema webhook.
* ``GET /healthz`` - process liveness probe.
* ``GET /readyz`` - Service Health configuration readiness probe.

This app intentionally has no Slack Bolt app, no inbound Slack events, and no
Azure support-ticket workflow. It only initializes a Slack ``WebClient`` for
outbound messages sent by :mod:`service_health.slack`.
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask
from slack_sdk import WebClient

from service_health.routes import create_service_health_blueprint
from service_health.runtime import create_service_health_runtime
from service_health.telemetry import configure_telemetry

load_dotenv(dotenv_path=".env")

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
logging.getLogger("azure").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

configure_telemetry()

slack_bot_token = os.environ["SLACK_BOT_TOKEN"]
slack_client = WebClient(slack_bot_token, timeout=10)

web_app = Flask(__name__)
_service_health_runtime = None


def get_service_health_runtime():
    global _service_health_runtime
    if _service_health_runtime is None:
        _service_health_runtime = create_service_health_runtime(slack_client)
    return _service_health_runtime


web_app.register_blueprint(
    create_service_health_blueprint(get_service_health_runtime))


if __name__ == "__main__":
    web_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
