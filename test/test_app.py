"""Tests for the standalone Flask entrypoint (app.py).

These tests exercise only what the minimal service is responsible for:
serving the three public routes and constructing a Slack ``WebClient`` from
the environment. Parsing, routing, auth, storage, processing, and Slack
rendering are covered in ``test_service_health.py``.
"""

import app


def test_only_the_three_expected_routes_are_registered():
    rules = {
        rule.rule
        for rule in app.web_app.url_map.iter_rules()
        if rule.endpoint != "static"
    }
    assert rules == {"/healthz", "/readyz", "/api/service-health"}


def test_healthz_is_public_and_healthy():
    client = app.web_app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "healthy"}


def test_readyz_reports_ready_when_configuration_is_valid():
    client = app.web_app.test_client()
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json == {"status": "ready"}


def test_service_health_route_rejects_non_json_content_type():
    client = app.web_app.test_client()
    response = client.post(
        "/api/service-health",
        data="not-json",
        content_type="text/plain",
    )
    assert response.status_code == 415


def test_slack_client_is_initialized_from_environment_token():
    assert app.slack_client.token == "xoxb-test-token"
    assert app.slack_client.timeout == 10


def test_service_health_runtime_is_created_lazily_and_cached():
    app._service_health_runtime = None
    first = app.get_service_health_runtime()
    second = app.get_service_health_runtime()
    assert first is second
