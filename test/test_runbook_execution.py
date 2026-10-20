"""Execute the published Bash, not a separately maintained walkthrough.

Linux and macOS tests use a private checkout and fail-closed CLI doubles. No
real Azure, Graph, Slack, Docker, or AZD operation is reachable through them.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "runbook"


def doc_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(DOC.glob("*.md"))
    )


def supported_bash():
    candidates = (
        os.environ.get("RUNBOOK_BASH"),
        "/opt/homebrew/bin/bash",
        "/usr/local/bin/bash",
        shutil.which("bash"),
    )
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run(
            [candidate, "-c", 'test "${BASH_VERSINFO[0]}" -ge 4'],
            capture_output=True,
        )
        if result.returncode == 0:
            return candidate
    return None


BASH = supported_bash()
SUPPORTED_POSIX = pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"} or not BASH,
    reason="The operational walkthrough requires Bash 4.4+ on Linux or macOS",
)


def blocks(section=None):
    text = doc_text()
    if section:
        text = text.split(f"### {section}\n", 1)[1]
        text = re.split(r"^###? ", text, maxsplit=1, flags=re.M)[0]
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def run_bash(body, *, cwd=ROOT, env=None, answers=None):
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", body],
        input=(answers or "").encode(),
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=120,
    )


@pytest.mark.skipif(not BASH, reason="Bash is not installed")
def test_every_paste_block_is_a_complete_strict_compound_command():
    for index, block in enumerate(blocks()):
        assert block.startswith(
            "{\nset -eEuo pipefail\nshopt -s inherit_errexit\n"
        ), index
        assert block.endswith("}\n"), index
        result = subprocess.run(
            [BASH, "-n"], input=block.encode(), capture_output=True
        )
        assert result.returncode == 0, (index, result.stderr.decode())


def test_repository_setup_precedes_first_deployment_template_command():
    text = doc_text().split(
        "## Deploy with Azure Developer CLI\n", 1
    )[1]
    assert text.index("git clone ") < text.index("az bicep build ")
    stage1 = "\n".join(blocks("Stage 1: pin the Azure and AZD deployment target"))
    assert "git clone" not in stage1
    assert "runbook_require_profiles" in stage1
    assert "get-access-token --resource-type ms-graph" in stage1


def test_every_arm_preview_opts_out_of_mutating_hooks():
    for block in blocks():
        shell = re.sub(r"\\\n\s*", " ", block)
        for match in re.finditer(r"azd provision\s+--preview", shell):
            prefix = shell[:match.start()]
            assert re.search(
                r"SERVICE_HEALTH_READ_ONLY_PREVIEW=true\s+$", prefix
            )
            assert "SERVICE_HEALTH_READ_ONLY_PREVIEW=true" in prefix
            assert "azd hooks run preprovision" in prefix


def test_documented_repository_scripts_and_templates_exist():
    text = doc_text()
    references = re.findall(
        r"\b(?:scripts/[\w.-]+\.(?:py|sh)|infra/[\w./-]+\.(?:bicep|json))",
        text,
    )
    assert references
    for relative in references:
        assert (ROOT / relative).is_file(), relative


def test_cutover_is_explicit_after_signed_acceptance_and_persists_enabled_state():
    text = doc_text()
    stage9 = blocks("Stage 9: send the signed Service Health test")[0]
    cutover = blocks("Authorize and enable baseline coverage")[0]
    assert 'SIGNED_TEST_AT="$(date -u +%s)"' in stage9
    assert 'SIGNED_TEST_RESULT="$SIGNED_TEST_RESULT" python' in stage9
    assert 'SIGNED_TEST_AT:?' in cutover
    assert cutover.index('test "$ACCEPTANCE" = ACCEPTED') < cutover.index(
        "azd env set SERVICE_HEALTH_BASELINE_ALERT_ENABLED true"
    )
    assert cutover.index('test "$CUTOVER" = ENABLE') < cutover.index(
        'azd provision -e "$AZURE_ENV_NAME"'
    )
    assert "baseline-coverage-enabled" in cutover
    assert text.index("### Stage 9:") < text.index(
        "### Authorize and enable baseline coverage"
    )


@pytest.fixture
def workstation(tmp_path):
    if sys.platform not in {"linux", "darwin"} or not BASH:
        pytest.skip("Linux/macOS execution fixture")
    home = tmp_path / "operator home with spaces"
    home.mkdir()
    checkout = home / "src" / "azure-service-health-slack-bot"
    checkout.mkdir(parents=True)
    for relative in (
        "azure.yaml",
        "infra/main.bicep",
        "infra/day2/service-health-alert-scope.bicep",
        "scripts/configure_secure_webhook.py",
        "scripts/manage_slack_token.py",
        "scripts/manage_alert_scopes.py",
        "scripts/runbook_guard.sh",
        "requirements-ops.txt",
        "requirements.txt",
        "config/service_health_routes.example.json",
    ):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            (ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8"
        )
    real_git = shutil.which("git")
    subprocess.run([real_git, "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [real_git, "-C", str(checkout), "remote", "add", "origin",
         "https://github.com/ricmmartins/azure-service-health-slack-bot.git"],
        check=True,
    )
    shutil.copytree(ROOT / "service_health", checkout / "service_health",
                    ignore=shutil.ignore_patterns("__pycache__"))
    # A tempting unrelated infra tree must never reach the compiler.
    unrelated = home / "infra"
    unrelated.mkdir()
    (unrelated / "main.bicep").write_text(
        "module unrelated 'fdcdn.bicep' = {}\n", encoding="utf-8"
    )
    bindir = tmp_path / "offline bin"
    bindir.mkdir()
    stub = bindir / "stub"
    stub.write_text(
        f"#!{sys.executable}\n" + STUB, encoding="utf-8"
    )
    stub.chmod(0o700)
    for name in ("az", "azd", "docker", "git", "python", "python3", "vi", "curl"):
        (bindir / name).symlink_to(stub)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "STUB_CHECKOUT": str(checkout),
        "STUB_LOG": str(tmp_path / "cli.jsonl"),
        "STUB_STATE": str(tmp_path / "nonsecret-fixture.json"),
        "REAL_GIT": real_git,
        "REAL_PYTHON": sys.executable,
    }
    # Do not pass inherited Azure/Graph/Slack credentials to a child test.
    for key in list(env):
        if (key.startswith(("AZURE_", "AZD_", "SLACK_"))
                or key in {"GH_TOKEN", "GITHUB_TOKEN"}):
            del env[key]
    return home, checkout, env


STUB = r'''
import json
import os
import subprocess
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([name, args, str(Path.cwd())]) + "\n")
failure = os.environ.get("FAIL_COMMAND")
command = " ".join([name, *args])
if failure and (command == failure or command.startswith(failure + " ")):
    raise SystemExit(17)
if name == "git":
    if args[0] == "clone":
        assert args[1].startswith("https://github.com/ricmmartins/")
        assert Path(args[2]) == Path(os.environ["STUB_CHECKOUT"])
        raise SystemExit(0)
    raise SystemExit(subprocess.call([os.environ["REAL_GIT"], *args]))
state_path = Path(os.environ["STUB_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
def argument(flag):
    return args[args.index(flag) + 1]

if name in {"python", "python3"}:
    if args[:3] == ["-m", "venv", ".venv"]:
        directory = Path(".venv/bin")
        directory.mkdir(parents=True)
        (directory / "activate").write_text(
            'export VIRTUAL_ENV="$PWD/.venv"\n'
        )
    elif args[:2] == ["-m", "pip"]:
        pass
    elif args and args[0] == "scripts/manage_slack_token.py":
        if args[1] == "bootstrap":
            state["SERVICE_HEALTH_DEPLOY_WORKLOAD"] = "true"
            state["SERVICE_APP_NAME"] = "app-lab"
            state["SERVICE_APP_URI"] = "https://fixture.invalid"
        elif args[1] == "lock-status":
            print('{"Status":"Unlocked"}')
        elif args[1] == "status":
            print("{}")
        elif args[1] in {"rotate", "rollback", "migrate", "recover-lock"}:
            print("{}")
        else:
            raise SystemExit("Unexpected lifecycle command")
    elif args and args[0] == "scripts/manage_alert_scopes.py":
        assert args[1] == "list"
        print("[]")
    else:
        os.execv(os.environ["REAL_PYTHON"],
                 [os.environ["REAL_PYTHON"], *args])
elif name == "vi":
    Path(args[0]).write_text(
        '{"default_channel_id":"C3333333333","rules":[]}'
    )
elif name == "curl":
    if "-o" in args:
        Path(argument("-o")).write_text('{"error":"unauthorized"}')
        print("401", end="")
    elif args[-1].endswith("/healthz"):
        print('{"status":"healthy"}')
    elif args[-1].endswith("/readyz"):
        print('{"status":"ready"}')
    else:
        raise SystemExit("Unexpected HTTP request")
elif name == "az":
    if args[0] == "version":
        print("{}")
    elif args[:2] == ["cloud", "show"]:
        print("AzureCloud")
    elif args[:2] == ["config", "set"]:
        pass
    elif args[:2] == ["config", "get"]:
        print("false")
    elif args[:2] == ["bicep", "version"]:
        if (os.environ.get("BICEP_MISSING") == "1"
                and not state.get("bicep_installed")):
            raise SystemExit(1)
        print("Bicep CLI version " + os.environ.get("BICEP_VERSION", "0.46.1"))
    elif args[:2] == ["bicep", "install"]:
        assert argument("--version") == "v0.46.1"
        state["bicep_installed"] = True
    elif args[0] == "bicep" and args[1] in {"build", "lint"}:
        assert Path.cwd() == Path(os.environ["STUB_CHECKOUT"])
        assert Path(args[args.index("--file") + 1]).is_file()
    elif args[0] == "extension":
        print("{}")
    elif args[0] == "login" or args[:2] == ["account", "set"]:
        pass
    elif args[:2] == ["account", "get-access-token"]:
        assert args[args.index("--query") + 1] == "tenant"
        print(os.environ.get("GRAPH_TENANT", os.environ["TARGET_TENANT_ID"]))
    elif args[:2] == ["account", "show"]:
        query = args[args.index("--query") + 1]
        print({"tenantId": os.environ["TARGET_TENANT_ID"],
               "id": os.environ["TARGET_SUBSCRIPTION_ID"]}.get(query, "{}"))
    elif args[:3] == ["ad", "signed-in-user", "show"]:
        print("fixture-operator")
    elif args[:3] == ["role", "assignment", "list"]:
        print("0" if "--query" in args and "length(" in argument("--query")
              else "[]")
    elif args[0] == "provider":
        if args[1] == "show":
            print("Registered")
    elif args[:2] == ["policy", "assignment"]:
        print("[]")
    elif args[:2] == ["account", "list"]:
        print(os.environ["TARGET_SUBSCRIPTION_ID"])
    elif args[:2] == ["group", "exists"]:
        print("true" if argument("--name") == "rg-lab"
              and not state.get("resources_deleted") else "false")
    elif args[:2] == ["group", "create"]:
        pass
    elif args[:2] == ["group", "show"]:
        print("Succeeded")
    elif args[:2] == ["group", "list"]:
        print(json.dumps([{"name": "rg-lab", "tags": {
            "workload": "azure-service-health-slack-bot", "azd-env-name": "lab"
        }}]))
    elif args[:3] == ["monitor", "action-group", "list"]:
        print("[]")
    elif args[:3] == ["monitor", "action-group", "create"]:
        group_name = argument("--name")
        state["operations"] = {
            "id": "/subscriptions/" + os.environ["TARGET_SUBSCRIPTION_ID"]
                  + "/resourceGroups/" + argument("--resource-group")
                  + "/providers/Microsoft.Insights/actionGroups/" + group_name,
            "location": "Global",
            "enabled": True,
            "emailReceivers": [{
                "name": args[args.index("--action") + 2],
                "emailAddress": args[args.index("--action") + 3],
                "useCommonAlertSchema": True,
            }],
        }
    elif args[:3] == ["monitor", "action-group", "show"]:
        if argument("--name") == "ag-lab-service-health":
            group = {
                "location": "global", "enabled": True,
                "tags": {"azd-env-name": "lab",
                         "workload": "azure-service-health-slack-bot"},
                "webhookReceivers": [{
                    "objectId": "fixture-api-object",
                    "identifierUri": "api://fixture-api",
                    "useAadAuth": True,
                }],
            }
            values = {
                "webhookReceivers[0].useAadAuth": "true",
                "webhookReceivers[0].useCommonAlertSchema": "true",
                "webhookReceivers[0].serviceUri":
                    "https://fixture.invalid/api/service-health",
                "webhookReceivers[0].objectId": "fixture-api-object",
                "webhookReceivers[0].identifierUri": "api://fixture-api",
            }
        else:
            group = state["operations"]
            values = {"id": group["id"]}
        query = argument("--query") if "--query" in args else ""
        print(values.get(query, str(group.get(query, "")).lower())
              if query in values or query in group else json.dumps(group))
    elif args[:4] == ["monitor", "action-group", "test-notifications", "create"]:
        print(json.dumps({"state": "Complete", "actionDetails": [{
            "Name": "slack-service-health", "MechanismType": "SecureWebhook",
            "Status": "Succeeded",
        }]}))
    elif args[:4] == ["monitor", "activity-log", "alert", "show"]:
        print(state.get("SERVICE_HEALTH_BASELINE_ALERT_ENABLED", "false"))
    elif args[:2] == ["containerapp", "show"]:
        query = argument("--query")
        if query in {"properties.latestRevisionName", "properties.latestReadyRevisionName"}:
            print("fixture-ready-revision")
        else:
            print("true" if query.endswith(".external") else "Succeeded")
    elif args[:3] == ["containerapp", "revision", "list"]:
        print("1" if argument("--query").startswith("length") else "[]")
    elif args[:3] == ["containerapp", "revision", "show"]:
        print("Healthy")
    elif args[:3] == ["containerapp", "logs", "show"]:
        print("Service Health incident processed; fixture HTTP 200")
    elif args[:2] == ["keyvault", "show"]:
        print("0" if argument("--query").startswith("length")
              else "/subscriptions/fixture/vault")
    elif args[:3] == ["deployment", "group", "list"]:
        print("0")
    elif args[:3] == ["monitor", "log-analytics", "workspace"]:
        print("fixture-workspace")
    elif args[:3] == ["monitor", "log-analytics", "query"]:
        print("fixture dependencies succeeded")
    elif args[:3] == ["ad", "app", "delete"]:
        assert argument("--id") == "fixture-api-object"
        state["application_deleted"] = True
    elif args[:3] == ["ad", "app", "list"]:
        assert state["application_deleted"]
        print("0")
    elif args[:2] == ["rest", "--method"] and "graph.microsoft.com" in argument("--url"):
        assert "applications/fixture-api-object?" in argument("--url")
        assert not state.get("application_deleted")
        print(json.dumps({
            "id": "fixture-api-object", "appId": "fixture-api",
            "displayName": "Azure Service Health Slack Bot - lab",
            "identifierUris": ["api://fixture-api"],
        }))
    elif args[:2] == ["rest", "--method"] and "usages?" in argument("--url"):
        print('{"current":0,"limit":100}')
    else:
        raise SystemExit("Unexpected Azure command: " + repr(args))
elif name == "azd":
    if args[0] == "version":
        print("azd version 1.31.0 (commit fixture) (stable)")
    elif args[0] in {"config", "auth"}:
        pass
    elif args[:2] == ["env", "new"]:
        state = {
            "AZURE_ENV_NAME": args[2],
            "AZURE_SUBSCRIPTION_ID": args[args.index("--subscription") + 1],
            "AZURE_LOCATION": args[args.index("--location") + 1],
        }
        directory = Path(".azure") / args[2]
        directory.mkdir(parents=True, mode=0o700)
        Path(".azure").chmod(0o700)
        (directory / ".env").touch(mode=0o600)
    elif args[:2] == ["env", "set"]:
        state[args[2]] = args[3]
    elif args[:2] == ["env", "get-value"]:
        print(state[args[2]])
    elif args[:2] in (["env", "select"], ["env", "list"]):
        pass
    elif args[:2] == ["env", "remove"]:
        directory = Path(".azure") / args[2]
        assert args[2] == "lab"
        (directory / ".env").unlink()
        directory.rmdir()
    elif args[0] == "provision":
        if "--preview" in args:
            assert os.environ.get("SERVICE_HEALTH_READ_ONLY_PREVIEW") == "true"
        else:
            state["AZURE_RESOURCE_GROUP"] = "rg-lab"
            state["SERVICE_HEALTH_KEY_VAULT_NAME"] = "fixture-vault"
    elif args[0] == "hooks":
        if os.environ.get("SERVICE_HEALTH_READ_ONLY_PREVIEW") != "true":
            state["SERVICE_HEALTH_API_CLIENT_ID"] = "fixture-api"
            state["SERVICE_HEALTH_API_OBJECT_ID"] = "fixture-api-object"
            state["SERVICE_HEALTH_API_IDENTIFIER_URI"] = "api://fixture-api"
    elif args[0] == "deploy":
        pass
    elif args[0] == "down":
        state["resources_deleted"] = True
    else:
        raise SystemExit("Unexpected AZD command: " + repr(args))
elif name == "docker":
    print("linux" if args[0] == "info" else "client=fixture server=fixture")
else:
    raise SystemExit("Unexpected executable")
temporary_state = state_path.with_name(
    f".{state_path.name}.{os.getpid()}.tmp"
)
temporary_state.write_text(json.dumps(state))
os.replace(temporary_state, state_path)
'''


def initial_setup():
    return blocks("Safe shell and repository setup")[0]


def existing_checkout_setup():
    return blocks("Safe shell and repository setup")[1]


def stage0():
    return blocks("Stage 0: verify the workstation")[0]


def stage1():
    return blocks("Stage 1: pin the Azure and AZD deployment target")[0].replace(
        "<tenant-id>", "11111111-1111-4111-8111-111111111111"
    ).replace(
        "<subscription-id>", "22222222-2222-4222-8222-222222222222"
    ).replace("<azure-region>", "eastus2").replace("<environment-name>", "lab")


def commands(env):
    path = Path(env["STUB_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()]


@SUPPORTED_POSIX
def test_first_run_from_home_clones_before_compiling_and_retains_state(workstation):
    home, checkout, env = workstation
    body = initial_setup() + stage0() + stage1() + """
{
runbook_require_target
test "$AZURE_ENV_NAME" = lab
test "$BICEP_VERSION" = 0.46.1
test "$OPERATOR_OBJECT_ID" = fixture-operator
bash -c 'test "$TARGET_TENANT_ID" = 11111111-1111-4111-8111-111111111111'
printf 'state-survived\\n'
}
"""
    result = run_bash(body, cwd=home, env=env)
    assert result.returncode == 0, result.stderr.decode()
    assert b"state-survived" in result.stdout
    log = commands(env)
    builds = [row for row in log if row[1][:2] == ["bicep", "build"]]
    assert len(builds) == 2
    assert all(row[2] == str(checkout) for row in builds)
    assert log.index(next(row for row in log if row[1][0] == "clone")) < (
        log.index(builds[0])
    )


@SUPPORTED_POSIX
def test_existing_checkout_setup_reuses_profiles_without_clone(workstation):
    home, checkout, env = workstation
    azure_profile = home / ".config" / "azure-service-health-slack-bot" / "azure"
    azd_profile = home / ".config" / "azure-service-health-slack-bot" / "azd"
    azure_profile.mkdir(parents=True, mode=0o700)
    azd_profile.mkdir(parents=True, mode=0o700)

    result = run_bash(existing_checkout_setup(), cwd=home, env=env)

    assert result.returncode == 0, result.stderr.decode()
    assert b"existing-checkout-ready" in result.stdout
    assert not any(row[1] and row[1][0] == "clone" for row in commands(env))
    assert checkout == home / "src" / "azure-service-health-slack-bot"


@SUPPORTED_POSIX
@pytest.mark.parametrize(
    "failure",
    ["az bicep build", "az config set", "az extension add",
     "azd version", "docker version"],
)
def test_stage0_failure_stops_paste_but_parent_shell_survives(workstation, failure):
    home, _, env = workstation
    env["FAIL_COMMAND"] = failure
    child = initial_setup() + stage0() + "echo UNSAFE_CONTINUATION\n"
    parent = (
        "bash --noprofile --norc <<'CHILD'\n" + child
        + "CHILD\nstatus=$?\nprintf 'parent-alive:%s\\n' \"$status\"\n"
    )
    result = run_bash(parent, cwd=home, env=env)
    assert result.returncode == 0
    assert b"parent-alive:0" not in result.stdout
    assert b"parent-alive:" in result.stdout
    assert b"UNSAFE_CONTINUATION" not in result.stdout
    assert b"deployment-target-pinned" not in result.stdout


@SUPPORTED_POSIX
def test_stage0_installs_tested_bicep_in_empty_isolated_profile(workstation):
    home, _, env = workstation
    env["BICEP_MISSING"] = "1"

    result = run_bash(initial_setup() + stage0(), cwd=home, env=env)

    assert result.returncode == 0, result.stderr.decode()
    assert b"Installing Azure CLI-managed Bicep 0.46.1" in result.stdout
    assert any(
        row[0] == "az"
        and row[1][:2] == ["bicep", "install"]
        and row[1][-1] == "v0.46.1"
        for row in commands(env)
    )


@SUPPORTED_POSIX
def test_starting_outside_checkout_never_compiles_unrelated_infra(workstation):
    home, checkout, env = workstation
    body = (
        "{\nset -eEuo pipefail\nsource "
        + shlex.quote(str(checkout / "scripts" / "runbook_guard.sh"))
        + "\n}\n" + stage0()
    )
    result = run_bash(body, cwd=home, env=env)
    assert result.returncode != 0
    assert not any(row[0] == "az" for row in commands(env))


@SUPPORTED_POSIX
@pytest.mark.parametrize("problem", ["missing-template", "wrong-origin"])
def test_bad_checkout_is_rejected_before_first_azure_command(workstation, problem):
    home, checkout, env = workstation
    if problem == "missing-template":
        (checkout / "infra/day2/service-health-alert-scope.bicep").unlink()
    else:
        subprocess.run(
            [env["REAL_GIT"], "-C", str(checkout), "remote", "set-url",
             "origin", "https://github.com/example/unrelated.git"],
            check=True,
        )
    result = run_bash(initial_setup() + stage0(), cwd=home, env=env)
    assert result.returncode != 0
    assert not any(row[0] in {"az", "azd"} for row in commands(env))


@SUPPORTED_POSIX
def test_arm_match_does_not_authorize_wrong_graph_tenant(workstation):
    home, _, env = workstation
    env["GRAPH_TENANT"] = "33333333-3333-4333-8333-333333333333"
    result = run_bash(
        initial_setup() + stage0() + stage1() + "echo UNSAFE_CONTINUATION\n",
        cwd=home, env=env,
    )
    assert result.returncode != 0
    assert b"UNSAFE_CONTINUATION" not in result.stdout
    assert not any(row[1][:2] == ["env", "new"] for row in commands(env))


@SUPPORTED_POSIX
@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_target_guard_rejects_nonprivate_env_before_any_cloud_call(workstation, mode):
    home, checkout, env = workstation
    setup = run_bash(initial_setup() + stage0() + stage1(), cwd=home, env=env)
    assert setup.returncode == 0, setup.stderr.decode()
    (checkout / ".azure" / "lab" / ".env").chmod(mode)
    Path(env["STUB_LOG"]).write_text("")
    exports = """
export AZURE_ENV_NAME=lab TARGET_TENANT_ID=11111111-1111-4111-8111-111111111111
export TARGET_SUBSCRIPTION_ID=22222222-2222-4222-8222-222222222222 AZURE_LOCATION=eastus2
export AZURE_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azure"
export AZD_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azd"
"""
    result = run_bash(
        "{\nset -eEuo pipefail\nsource scripts/runbook_guard.sh\n"
        + exports + "\nrunbook_require_target\n}\n",
        cwd=checkout, env=env,
    )
    assert result.returncode != 0
    assert not any(row[0] == "az" for row in commands(env))


@SUPPORTED_POSIX
def test_failed_paste_returns_to_the_original_interactive_terminal():
    import pty
    import select
    import termios

    master, slave = pty.openpty()
    settings = termios.tcgetattr(slave)
    settings[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, settings)
    process = subprocess.Popen(
        [BASH, "--noprofile", "--norc", "-i"],
        stdin=slave, stdout=slave, stderr=slave,
    )
    os.close(slave)
    output = bytearray()

    def wait_for(marker):
        deadline = time.monotonic() + 10
        while marker not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, bytes(output)
            ready, _, _ = select.select([master], [], [], remaining)
            assert ready, bytes(output)
            output.extend(os.read(master, 65536))

    try:
        os.write(master, b"PS1='PARENT> '; echo PARENT_READY\n")
        wait_for(b"PARENT_READY")
        os.write(master, b"bash --noprofile --norc\n")
        os.write(master, b"PS1='CHILD> '; echo CHILD_READY\n")
        wait_for(b"CHILD_READY")
        os.write(master, (
            b"{\nset -eEuo pipefail\nshopt -s inherit_errexit\n"
            b"false\nprintf 'UNSAFE_CONTINUATION\\n'\n}\n"
        ))
        # Use a distinct marker after failure so an earlier prompt cannot pass.
        os.write(master, b"printf 'parent-survived:%s\\n' \"$?\"\n")
        wait_for(b"parent-survived:1")
        assert b"UNSAFE_CONTINUATION" not in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        os.close(master)


def deployment_walkthrough():
    stages = [initial_setup(), stage0(), stage1()]
    stages.extend(blocks("Stage 1: pin the Azure and AZD deployment target")[1:])
    stages.extend(blocks("Stage 3: install dependencies and validate the routing file"))
    stages.extend(blocks("Stage 4: load nonsecret inputs"))
    stages.extend(blocks("Stage 5: reconcile Microsoft Entra and preview Azure changes"))
    stages.extend(blocks("Stage 6: provision infrastructure and transfer the Slack token"))
    stages.extend(blocks("Stage 7: build and deploy the application"))
    stages.extend(blocks("Stage 8: verify probes and the authentication boundary"))
    stages.extend(blocks("Stage 9: send the signed Service Health test"))
    stages.extend(blocks("Application Insights queries"))
    stages.extend(blocks("Authorize and enable baseline coverage"))
    body = "\n".join(stages)
    for placeholder, value in {
        "<separate-operations-resource-group>": "ops-fixture",
        "<azure-region>": "eastus2",
        "<operations-action-group-name>": "ops-ag",
        "<short-display-name>": "ops",
        "<approved-on-call-email-address>": "oncall@example.invalid",
        "<primary-owner-role-or-alias>": "primary",
        "<different-backup-owner-role-or-alias>": "backup",
        "<approved-runbook>": "example.invalid/runbook",
    }.items():
        body = body.replace(placeholder, value)
    return body


@SUPPORTED_POSIX
def test_published_stages_through_signed_acceptance_and_authorized_cutover(workstation):
    home, _, env = workstation
    result = run_bash(
        deployment_walkthrough()
        + '\ntest "$VIRTUAL_ENV" = "$PWD/.venv"\n',
        cwd=home, env=env, answers="RECEIVED\nACCEPTED\nENABLE\n"
    )
    assert result.returncode == 0, (
        result.stdout.decode(), result.stderr.decode()
    )
    assert b"baseline-coverage-enabled" in result.stdout
    state = json.loads(Path(env["STUB_STATE"]).read_text())
    assert state["SERVICE_HEALTH_DEPLOY_WORKLOAD"] == "true"
    assert state["SERVICE_HEALTH_BASELINE_ALERT_ENABLED"] == "true"
    assert state["SERVICE_HEALTH_ENVIRONMENT_CLASS"] == "production"
    assert state["SERVICE_HEALTH_SECRET_VERSION"] == ""
    assert "SLACK_BOT_TOKEN" not in state
    log = commands(env)
    enabled = next(i for i, row in enumerate(log) if row[1][:4] == [
        "env", "set", "SERVICE_HEALTH_BASELINE_ALERT_ENABLED", "true"
    ])
    signed = next(i for i, row in enumerate(log) if (
        row[1][:4] == ["monitor", "action-group", "test-notifications", "create"]
        and "ag-lab-service-health" in row[1]
    ))
    assert signed < enabled


@SUPPORTED_POSIX
@pytest.mark.parametrize(
    "failure",
    [
        "az provider register",
        "azd provision",
        "curl --connect-timeout",
        "az monitor action-group test-notifications create "
        "--resource-group rg-lab",
    ],
)
def test_failed_walkthrough_cannot_reach_baseline_enablement(workstation, failure):
    home, _, env = workstation
    env["FAIL_COMMAND"] = failure
    result = run_bash(
        deployment_walkthrough(), cwd=home, env=env,
        answers="RECEIVED\nACCEPTED\nENABLE\n",
    )
    assert result.returncode != 0
    assert b"baseline-coverage-enabled" not in result.stdout
    assert not any(row[1][:4] == [
        "env", "set", "SERVICE_HEALTH_BASELINE_ALERT_ENABLED", "true"
    ] for row in commands(env))


@SUPPORTED_POSIX
def test_operations_and_decommission_use_retained_target_not_shell_defaults(workstation):
    home, checkout, env = workstation
    existing = blocks("Existing-environment setup")[0]
    for placeholder, value in {
        "<environment-name>": "lab",
        "<approved-tenant-id>": "11111111-1111-4111-8111-111111111111",
        "<approved-subscription-id>": "22222222-2222-4222-8222-222222222222",
        "<approved-region>": "eastus2",
    }.items():
        existing = existing.replace(placeholder, value)
    body = deployment_walkthrough() + existing
    for section in (
        "Update routing", "Rotate the Slack token",
        "Migrate an existing deployment", "Decommission",
    ):
        body += "\n".join(blocks(section))
    result = run_bash(
        body, cwd=home, env=env,
        answers="RECEIVED\nACCEPTED\nENABLE\nfixture-api-object\nDELETE\n",
    )
    assert result.returncode == 0, (
        result.stdout.decode(), result.stderr.decode()
    )
    assert b"decommission-cloud-resources-removed" in result.stdout
    assert b"decommission-local-credentials-removed" in result.stdout
    assert not (checkout / ".azure" / "lab").exists()
    record = json.loads(
        (home / ".local/state/azure-service-health-slack-bot/"
         "decommission-lab.json").read_text()
    )
    assert record["API_OBJECT_ID"] == "fixture-api-object"
    assert record["REPOSITORY_PATH"] == str(checkout)
    state = json.loads(Path(env["STUB_STATE"]).read_text())
    assert state["resources_deleted"]
    assert state["application_deleted"]
    assert state["operations"]["enabled"]
