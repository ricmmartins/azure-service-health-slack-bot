import logging
import os

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import metrics


logger = logging.getLogger(__name__)
_configured = False


def configure_telemetry(environ=None):
    global _configured
    environ = environ or os.environ
    connection_string = environ.get(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if _configured or not connection_string:
        return False
    configure_azure_monitor(
        connection_string=connection_string,
        enable_live_metrics=False,
    )
    _configured = True
    logger.info("Azure Monitor OpenTelemetry configured")
    return True


class ServiceHealthMetrics:
    def __init__(self):
        meter = metrics.get_meter("azure-service-health-slack-bot.service-health")
        self.requests = meter.create_counter(
            "service_health.requests",
            description="Service Health events by processing result",
        )
        self.lifecycle = meter.create_counter(
            "service_health.lifecycle",
            description="Service Health events by lifecycle state",
        )

    def record(self, event, result):
        attributes = {"result": result.value}
        self.requests.add(1, attributes)
        self.lifecycle.add(
            1, {"lifecycle": event.lifecycle_status.value})


service_health_metrics = ServiceHealthMetrics()
