import ast
import json
import re
from pathlib import Path

from scripts.configure_secure_webhook import (
    OPERATIONS_ACTION_GROUP_ENV_NAME,
    OPERATIONS_BACKUP_OWNER_ENV_NAME,
    OPERATIONS_ON_CALL_ENV_NAME,
    OPERATIONS_PRIMARY_OWNER_ENV_NAME,
    OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME,
    OPERATIONS_RUNBOOK_ENV_NAME,
)


ROOT = Path(__file__).resolve().parent.parent


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def markdown_section(markdown: str, heading: str) -> str:
    marker = f"### {heading}\n"
    start = markdown.index(marker) + len(marker)
    match = re.search(r"^###? ", markdown[start:], re.MULTILINE)
    end = start + match.start() if match else len(markdown)
    return markdown[start:end]


def fenced_bash(section: str) -> str:
    blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    return "\n".join(blocks)


def normalized_shell(section: str) -> str:
    shell = fenced_bash(section)
    shell = re.sub(r"\\\n\s*", " ", shell)
    return re.sub(r"\s+", " ", shell)


def fenced_json_after(section: str, marker: str) -> dict[str, object]:
    tail = section[section.index(marker) + len(marker):]
    match = re.search(r"```json\n(.*?)```", tail, re.DOTALL)
    assert match is not None
    return json.loads(match.group(1))


def status_json_schema() -> set[str]:
    module = ast.parse(read("scripts/manage_slack_token.py"))
    manager = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "SlackTokenManager"
    )
    status = next(
        node
        for node in manager.body
        if isinstance(node, ast.FunctionDef) and node.name == "status"
    )
    returns = [
        node
        for node in ast.walk(status)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert len(returns) == 1
    return {
        key.value
        for key in returns[0].value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_stage0_requires_and_rechecks_azure_cli_managed_bicep():
    stage = markdown_section(read("README.md"), "Stage 0: verify the workstation")
    shell = normalized_shell(stage)

    assert 'MIN_AZ_BICEP_VERSION="0.46.1"' in shell
    assert "version_at_least()" in shell
    assert (
        "az config get bicep.use_binary_from_path --query value -o tsv"
        in shell
    )
    assert '"${AZ_BICEP_PATH_MODE,,}" != "false"' in shell
    assert shell.count("AZ_BICEP_VERSION_OUTPUT=\"$(az bicep version 2>&1)\"") >= 3
    assert "if ! az bicep install" in shell
    assert "if ! az bicep upgrade" in shell
    assert shell.count(
        'version_at_least "$AZ_BICEP_VERSION" "$MIN_AZ_BICEP_VERSION"'
    ) == 2
    assert shell.index("if ! az bicep upgrade") < shell.rindex(
        "AZ_BICEP_VERSION_OUTPUT=\"$(az bicep version 2>&1)\""
    )
    assert "command -v bicep" in shell
    assert "bicep --version" in shell
    assert (
        "The standalone binary does not satisfy the az bicep requirement."
        in shell
    )
    for failure in (
        "installation failed; stop.",
        "upgrade failed; stop.",
        "could not be rechecked after upgrade.",
        "still below the required version.",
    ):
        assert failure in shell


def test_lifecycle_checkpoint_examples_match_status_json_schema():
    readme = read("README.md")
    stage4 = markdown_section(readme, "Stage 4: load nonsecret inputs")
    stage6 = markdown_section(
        readme,
        "Stage 6: provision infrastructure and transfer the Slack token",
    )
    schema = status_json_schema()
    assert schema == {
        "Environment",
        "KeyVaultName",
        "SecretVersion",
        "LatestSecretVersion",
        "PreviousSecretVersion",
        "LegacyTokenPresent",
        "MigrationMarkerSet",
        "Bootstrapped",
    }

    before = fenced_json_after(
        stage4,
        "For a new environment before infrastructure exists",
    )
    after_infrastructure = fenced_json_after(
        stage6,
        "Immediately after infrastructure provisioning and before token transfer",
    )
    after_bootstrap = fenced_json_after(
        stage6,
        "A first successful bootstrap has this",
    )
    assert set(before) == schema
    assert set(after_infrastructure) == schema
    assert set(after_bootstrap) == schema

    assert before == {
        "Environment": "<environment-name>",
        "KeyVaultName": None,
        "SecretVersion": "",
        "LatestSecretVersion": "",
        "PreviousSecretVersion": "",
        "LegacyTokenPresent": False,
        "MigrationMarkerSet": False,
        "Bootstrapped": False,
    }
    assert after_infrastructure["KeyVaultName"] == "<key-vault-name>"
    assert after_infrastructure["Bootstrapped"] is False
    assert all(
        after_infrastructure[field] == ""
        for field in (
            "SecretVersion",
            "LatestSecretVersion",
            "PreviousSecretVersion",
        )
    )
    assert after_bootstrap["KeyVaultName"] == "<key-vault-name>"
    assert after_bootstrap["SecretVersion"] == ""
    assert (
        after_bootstrap["LatestSecretVersion"]
        == "<recorded-latest-version>"
    )
    assert after_bootstrap["PreviousSecretVersion"] == ""
    assert after_bootstrap["LegacyTokenPresent"] is False
    assert after_bootstrap["MigrationMarkerSet"] is True
    assert after_bootstrap["Bootstrapped"] is True
    assert "`InfrastructureOnly` is the expected" not in stage4


def central_bicep_instances(
    entrypoint: str = "infra/main.bicep",
    ancestors: tuple[str, ...] = (),
) -> list[str]:
    if entrypoint in ancestors:
        raise AssertionError(f"Cyclic Bicep module graph at {entrypoint}")
    source = read(entrypoint)
    parent = Path(entrypoint).parent
    instances = [entrypoint]
    for match in re.finditer(
        r"\bmodule\s+\w+\s+'(?P<path>[^']+\.bicep)'\s*"
        r"=\s*(?P<collection>\[for\s+)?",
        source,
    ):
        if match.group("collection"):
            raise AssertionError(
                "Dynamic Bicep module multiplicity requires an explicit "
                "documentation contract."
            )
        module_path = (parent / match.group("path")).as_posix()
        instances.extend(
            central_bicep_instances(
                module_path,
                ancestors=(*ancestors, entrypoint),
            )
        )
    return instances


def created_resource_count(resource_type: str) -> int:
    declaration = re.compile(
        rf"\bresource\s+\w+\s+'{re.escape(resource_type)}@[^']+'"
        r"\s*(?P<existing>existing\s*)?="
        r"\s*(?P<collection>\[for\s+)?"
    )
    count = 0
    for relative_path in central_bicep_instances():
        for match in declaration.finditer(read(relative_path)):
            if match.group("existing") is not None:
                continue
            if match.group("collection"):
                raise AssertionError(
                    f"Dynamic {resource_type} multiplicity requires an "
                    "explicit documentation contract."
                )
            count += 1
    return count


def test_stage5_preview_uses_only_the_explicit_selected_environment():
    stage = markdown_section(
        read("README.md"),
        "Stage 5: reconcile Microsoft Entra and preview Azure changes",
    )
    shell = normalized_shell(stage)

    assert "service-health-mgmt-test" not in fenced_bash(stage)
    assert (
        "azd env get-value AZURE_ENV_NAME --no-prompt"
        in shell
    )
    assert (
        'test "$SELECTED_AZD_ENV_NAME" = "$AZURE_ENV_NAME"'
        in shell
    )
    assert (
        "SERVICE_HEALTH_READ_ONLY_PREVIEW=true "
        "azd hooks run preprovision"
        in shell
    )
    assert (
        "SERVICE_HEALTH_READ_ONLY_PREVIEW=true azd provision --preview"
        in shell
    )
    assert shell.count('--environment "$AZURE_ENV_NAME"') >= 2
    assert '--subscription "$TARGET_SUBSCRIPTION_ID"' in shell
    assert '--location "$AZURE_LOCATION"' in shell
    assert '"${TARGET_TENANT_ID,,}"' in shell
    assert '"${TARGET_SUBSCRIPTION_ID,,}"' in shell


def test_stage4_keeps_independent_action_group_readiness_contract():
    stage = markdown_section(read("README.md"), "Stage 4: load nonsecret inputs")
    shell = normalized_shell(stage)

    assert "az monitor action-group create" in shell
    assert "--location Global" in shell
    assert (
        '--action email "$OPERATIONS_EMAIL_RECEIVER_NAME" '
        '"$OPERATIONS_EMAIL_ADDRESS" usecommonalertschema'
        in shell
    )
    assert "az monitor action-group test-notifications create" in shell
    assert "--alert-type servicehealth" in shell
    assert "OPERATIONS_RECEIVER_CONFIRMATION" in shell
    assert '"RECEIVED"' in shell
    assert '"testedAt"' in shell
    assert (
        'test "${OPERATIONS_ACTION_GROUP_ID,,}" != '
        '"${BOT_ACTION_GROUP_ID,,}"'
        in shell
    )
    assert (
        'test "${OPERATIONS_RESOURCE_GROUP,,}" != '
        '"${BOT_RESOURCE_GROUP,,}"'
        in shell
    )
    assert '"emailAddress"' in shell
    assert '"webhookReceivers"' in shell
    assert "OPERATIONS_RECEIVER_TEST_SENT_AT" in shell
    assert "elapsed <= 900" in shell
    assert shell.count('--subscription "$TARGET_SUBSCRIPTION_ID"') >= 7

    readiness_names = {
        OPERATIONS_ACTION_GROUP_ENV_NAME,
        OPERATIONS_PRIMARY_OWNER_ENV_NAME,
        OPERATIONS_BACKUP_OWNER_ENV_NAME,
        OPERATIONS_ON_CALL_ENV_NAME,
        OPERATIONS_RUNBOOK_ENV_NAME,
        OPERATIONS_RECEIVER_EVIDENCE_ENV_NAME,
    }
    for name in readiness_names:
        assert f"azd env set {name}" in shell


def test_documented_storage_increment_matches_central_bicep_graph():
    created_storage_accounts = created_resource_count(
        "Microsoft.Storage/storageAccounts"
    )
    stage = markdown_section(
        read("README.md"),
        "Stage 1: pin the Azure and AZD deployment target",
    )
    shell = normalized_shell(stage)
    documented = re.search(
        r"check_capacity Microsoft\.Storage 2024-01-01 "
        r"StorageAccounts (?P<count>\d+)",
        shell,
    )

    assert documented is not None
    assert created_storage_accounts == 2
    assert int(documented.group("count")) == created_storage_accounts
