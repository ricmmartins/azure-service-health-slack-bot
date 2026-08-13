import ast
import inspect
import json
import re
import textwrap
from pathlib import Path

from scripts.manage_slack_token import SlackTokenManager
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


def documented_json_contract(markdown: str, name: str) -> dict:
    match = re.search(
        rf"<!-- status-contract:{re.escape(name)} -->\s*"
        r"```json\n(?P<payload>.*?)```",
        markdown,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group("payload"))


def status_schema_keys() -> set[str]:
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(SlackTokenManager.status)
    ))
    returns = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert len(returns) == 1
    return {
        key.value
        for key in returns[0].keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


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


def test_stage0_fails_closed_for_stale_azure_cli_managed_bicep():
    readme = read("README.md")
    stage = markdown_section(readme, "Stage 0: verify the workstation")
    shell = normalized_shell(stage)
    versions = {
        name: value
        for name, value in re.findall(
            r'(BICEP_(?:MIN|TESTED)_VERSION)="(\d+\.\d+\.\d+)"',
            fenced_bash(stage),
        )
    }

    assert versions == {
        "BICEP_MIN_VERSION": "0.46.1",
        "BICEP_TESTED_VERSION": "0.46.1",
    }
    minimum = tuple(map(int, versions["BICEP_MIN_VERSION"].split(".")))
    assert tuple(map(int, "0.41.2".split("."))) < minimum
    assert "az bicep version" in shell
    assert (
        "az config get bicep.use_binary_from_path "
        "--query value -o tsv"
        in shell
    )
    assert (
        shell.index("az config get bicep.use_binary_from_path")
        < shell.index("az bicep version")
    )
    assert (
        'test "$(printf \'%s\\n\' "$BICEP_FROM_PATH" | '
        'tr \'[:upper:]\' \'[:lower:]\')" = "false"'
        in shell
    )
    assert "az config set bicep.use_binary_from_path=false" in stage
    assert '"$BICEP_VERSION" "$BICEP_MIN_VERSION"' in shell
    assert "az bicep upgrade" in stage
    assert "az bicep install --version v0.46.1" in stage
    assert "standalone" in stage
    assert "air-gapped" in stage
    assert "SHA-256" in stage
    assert "existingContainerApp!.properties" in read(
        "infra/modules/container-app.bicep"
    )

    expected_commands = {
        "az bicep build --file infra/main.bicep",
        "az bicep lint --file infra/main.bicep",
        (
            "az bicep build --file "
            "infra/day2/service-health-alert-scope.bicep"
        ),
        (
            "az bicep lint --file "
            "infra/day2/service-health-alert-scope.bicep"
        ),
    }
    workflow = re.sub(r"\s+", " ", read(".github/workflows/ci.yml"))
    for command in expected_commands:
        assert command in shell
        assert command in workflow
    fail_closed_commands = {
        (
            "az bicep build --file infra/main.bicep --stdout > /dev/null "
            "|| {"
        ),
        "az bicep lint --file infra/main.bicep || {",
        (
            "az bicep build --file "
            "infra/day2/service-health-alert-scope.bicep --stdout "
            "> /dev/null || {"
        ),
        (
            "az bicep lint --file "
            "infra/day2/service-health-alert-scope.bicep || {"
        ),
    }
    for command in fail_closed_commands:
        assert command in shell


def test_lifecycle_checkpoints_match_status_json_schema_and_invariants():
    readme = read("README.md")
    expected_keys = status_schema_keys()
    pre = documented_json_contract(readme, "pre-infrastructure")
    post = documented_json_contract(readme, "post-bootstrap")
    stage1 = normalized_shell(markdown_section(
        readme,
        "Stage 1: pin the Azure and AZD deployment target",
    ))

    assert set(pre) == expected_keys
    assert set(post) == expected_keys
    assert pre == {
        "Environment": "<selected AZD environment>",
        "KeyVaultName": None,
        "SecretVersion": "",
        "LatestSecretVersion": "",
        "PreviousSecretVersion": "",
        "LegacyTokenPresent": False,
        "MigrationMarkerSet": False,
        "Bootstrapped": False,
    }
    assert post == {
        "Environment": "<selected AZD environment>",
        "KeyVaultName": "<deployed vault name>",
        "SecretVersion": "",
        "LatestSecretVersion": "<enabled version id>",
        "PreviousSecretVersion": "",
        "LegacyTokenPresent": False,
        "MigrationMarkerSet": True,
        "Bootstrapped": True,
    }
    assert "InfrastructureOnly" not in readme
    assert (
        'azd env set AZURE_TENANT_ID "$TARGET_TENANT_ID" '
        '-e "$AZURE_ENV_NAME" --no-prompt'
        in stage1
    )
    assert (
        readme.index('azd env new "$AZURE_ENV_NAME"')
        < readme.index("azd env set AZURE_TENANT_ID")
        < readme.index("status-contract:pre-infrastructure")
    )
    assert "azd env get-value AZURE_TENANT_ID" in stage1


def test_bootstrap_has_separate_target_and_environment_uniqueness_gate():
    stage = markdown_section(
        read("README.md"),
        "Stage 6: provision infrastructure and transfer the Slack token",
    )
    shell = normalized_shell(stage)
    bootstrap = (
        "python scripts/manage_slack_token.py bootstrap "
        '--environment-name "$AZURE_ENV_NAME"'
    )

    assert 'az account show --query tenantId -o tsv' in shell
    assert 'az account show --query id -o tsv' in shell
    assert "azd env get-value AZURE_TENANT_ID" in shell
    assert "azd env get-value AZURE_SUBSCRIPTION_ID" in shell
    assert "azd env get-value AZURE_RESOURCE_GROUP" in shell
    assert "az account list" in shell
    assert "state=='Enabled'" in shell
    assert "tenantId=='$TARGET_TENANT_ID_LC'" in shell
    assert "--tag workload=azure-service-health-slack-bot" in shell
    assert "json.load(sys.stdin)" in shell
    assert 'get("azd-env-name", "")).casefold() == expected' in shell
    assert "tags.\\\"azd-env-name\\\"" not in shell
    assert 'test "$NORMALIZED_MATCHES" = "$EXPECTED_MATCH" || {' in shell
    assert "bootstrap-target-confirmed" in shell
    assert bootstrap in shell
    assert shell.index("bootstrap-target-confirmed") < shell.index(bootstrap)


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
