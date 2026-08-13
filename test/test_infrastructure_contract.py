from pathlib import Path

from scripts.manage_alert_scopes import ScopeManager


ROOT = Path(__file__).resolve().parent.parent


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_arm_contract_never_accepts_or_writes_slack_token_value():
    parameters = read("infra/main.parameters.json")
    main = read("infra/main.bicep")
    security = read("infra/modules/security.bicep")

    assert "SLACK_BOT_TOKEN" not in parameters
    assert "param slackBotToken string" not in main
    assert "param slackBotToken string" not in security
    assert "vaults/secrets@" not in security
    assert "value: slackBotToken" not in security


def test_secret_reference_is_versionless_by_default_and_pin_is_metadata_only():
    security = read("infra/modules/security.bicep")
    container_app = read("infra/modules/container-app.bicep")

    assert "slackBotTokenSecretVersion string = ''" in security
    assert "/secrets/slack-bot-token" in security
    assert "empty(slackBotTokenSecretVersion)" in security
    assert "keyVaultUrl: slackBotTokenSecretUri" in container_app


def test_two_phase_workload_and_disabled_first_alert_controls_are_explicit():
    main = read("infra/main.bicep")
    parameters = read("infra/main.parameters.json")

    assert "param deployWorkload bool = true" in main
    assert "if (deployWorkload)" in main
    assert "param baselineAlertEnabled bool = false" in main
    assert "alertEnabled: baselineAlertEnabled" in main
    assert "SERVICE_HEALTH_DEPLOY_WORKLOAD" in parameters
    assert "SERVICE_APP_RESOURCE_EXISTS" in parameters
    assert "SERVICE_HEALTH_BASELINE_ALERT_ENABLED" in parameters


def test_reprovision_preserves_the_deployed_image_and_bootstrap_is_pinned():
    container_app = read("infra/modules/container-app.bicep")

    assert "param containerAppExists bool" in container_app
    assert (
        "existingContainerApp!.properties.template.containers[0].image"
        in container_app
    )
    assert "containerapps-helloworld@sha256:" in container_app
    assert "containerapps-helloworld:latest" not in container_app


def test_production_monitoring_uses_an_independent_action_group():
    main = read("infra/main.bicep")
    monitoring = read("infra/modules/operations-monitoring.bicep")

    assert "operationsActionGroupId" in main
    assert (
        "module operationsMonitoring "
        "'modules/operations-monitoring.bicep' = {"
        in main
    )
    assert (
        "module operationsMonitoring "
        "'modules/operations-monitoring.bicep' = if (deployWorkload)"
        not in main
    )
    assert "keyVaultDiagnostics" in monitoring
    assert "category: 'AuditEvent'" in monitoring
    assert "StorageRead" in monitoring
    assert "StorageWrite" in monitoring
    assert "StorageDelete" in monitoring
    assert "AppRequests" in monitoring
    assert "AppDependencies" in monitoring
    assert "AppAvailabilityResults" in monitoring
    assert "operationsActionGroupId" in monitoring
    assert "param deployWorkload bool" in monitoring
    assert "= if (deployWorkload)" in monitoring


def test_review_fingerprint_hashes_transitive_day2_bicep_graph():
    graph = ScopeManager._artifact_identity()["templateGraph"]

    assert (
        "infra/day2/service-health-alert-scope.bicep" in graph
    )
    assert "infra/modules/service-health-alert.bicep" in graph


def test_key_vault_sdk_is_not_shipped_in_the_runtime_image():
    runtime = read("requirements.txt")
    operations = read("requirements-ops.txt")

    assert "azure-keyvault-secrets" not in runtime
    assert "azure-keyvault-secrets" in operations
    assert "azure-storage-blob" not in runtime
    assert "azure-storage-blob" in operations


def test_atomic_lock_storage_is_isolated_from_application_data():
    main = read("infra/main.bicep")
    lock = read("infra/modules/operations-lock.bicep")

    assert "module operationsLock" in main
    assert "'service-health-purpose': 'operation-lock'" in lock
    assert "allowBlobPublicAccess: false" in lock
    assert "publicAccess: 'None'" in lock
    assert "name: 'operation-locks'" in lock
