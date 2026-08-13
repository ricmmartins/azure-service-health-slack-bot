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
