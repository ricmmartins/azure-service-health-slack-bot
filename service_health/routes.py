import logging
import uuid

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from service_health.auth import (
    InvalidWebhookIdentity,
    MissingWebhookIdentity,
    authorize_easy_auth,
)
from service_health.config import InvalidServiceHealthConfiguration
from service_health.parser import (
    InvalidServiceHealthPayload,
    parse_service_health_alert,
)
from service_health.service import (
    PermanentProcessingError,
    TransientProcessingError,
)


logger = logging.getLogger(__name__)


def create_service_health_blueprint(get_runtime):
    blueprint = Blueprint("service_health", __name__)

    @blueprint.get("/healthz")
    def health():
        return jsonify({"status": "healthy"}), 200

    @blueprint.get("/readyz")
    def readiness():
        try:
            get_runtime()
        except InvalidServiceHealthConfiguration:
            return jsonify({"status": "not_ready"}), 503
        return jsonify({"status": "ready"}), 200

    @blueprint.post("/api/service-health")
    def receive_service_health():
        correlation_id = request.headers.get(
            "X-Correlation-ID", str(uuid.uuid4()))[:128]
        response_headers = {"X-Correlation-ID": correlation_id}
        try:
            runtime = get_runtime()
            settings = runtime.settings
            request.max_content_length = settings.max_payload_bytes + 1
            if request.content_length is not None and (
                    request.content_length > settings.max_payload_bytes):
                raise RequestEntityTooLarge()
            if len(request.get_data(cache=True)) > settings.max_payload_bytes:
                raise RequestEntityTooLarge()
            if request.mimetype != "application/json":
                return (
                    jsonify({
                        "error": "content_type_not_supported",
                        "correlationId": correlation_id,
                    }),
                    415,
                    response_headers,
                )
            if settings.require_easy_auth:
                authorize_easy_auth(
                    request.headers,
                    settings.expected_client_app_id,
                    settings.expected_app_role,
                    settings.expected_audience,
                )
            payload = request.get_json(force=False, silent=False)
            event = parse_service_health_alert(payload)
            outcome = runtime.processor.process(event)
            return (
                jsonify({
                    "result": outcome.result.value,
                    "correlationId": correlation_id,
                }),
                200,
                response_headers,
            )
        except MissingWebhookIdentity:
            return (
                jsonify({
                    "error": "authentication_required",
                    "correlationId": correlation_id,
                }),
                401,
                response_headers,
            )
        except InvalidWebhookIdentity as exc:
            logger.warning(
                "Service Health webhook authorization rejected: %s",
                exc,
                extra={"correlation_id": correlation_id},
            )
            return (
                jsonify({
                    "error": "forbidden",
                    "correlationId": correlation_id,
                }),
                403,
                response_headers,
            )
        except (BadRequest, InvalidServiceHealthPayload):
            return (
                jsonify({
                    "error": "invalid_payload",
                    "correlationId": correlation_id,
                }),
                422,
                response_headers,
            )
        except RequestEntityTooLarge:
            return (
                jsonify({
                    "error": "payload_too_large",
                    "correlationId": correlation_id,
                }),
                413,
                response_headers,
            )
        except PermanentProcessingError:
            logger.exception(
                "Permanent Service Health processing failure",
                extra={"correlation_id": correlation_id},
            )
            return (
                jsonify({
                    "error": "permanent_processing_failure",
                    "correlationId": correlation_id,
                }),
                422,
                response_headers,
            )
        except (TransientProcessingError,
                InvalidServiceHealthConfiguration):
            logger.exception(
                "Transient Service Health processing failure",
                extra={"correlation_id": correlation_id},
            )
            return (
                jsonify({
                    "error": "service_unavailable",
                    "correlationId": correlation_id,
                }),
                503,
                response_headers,
            )

    return blueprint
