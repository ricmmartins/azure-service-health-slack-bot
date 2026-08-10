from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
FAKE_CLI = ROOT / "test" / "fake_operational_cli.py"
SCOPE_CLI = ROOT / "scripts" / "manage_alert_scopes.py"
SETUP_CLI = ROOT / "scripts" / "configure_secure_webhook.py"
SCOPE_WRAPPER = ROOT / "scripts" / "manage-alert-scopes.ps1"
SETUP_WRAPPER = ROOT / "scripts" / "configure-secure-webhook.ps1"
AZURE_YAML = ROOT / "azure.yaml"


def write_shim(directory: Path, tool: str) -> None:
    if os.name == "nt":
        path = directory / f"{tool}.cmd"
        path.write_text(
            f'@echo off\r\n"{sys.executable}" "{FAKE_CLI}" {tool} %*\r\n',
            encoding="utf-8",
        )
        return
    path = directory / tool
    path.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLI))} "
        f"{tool} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def cli_environment(tmp_path):
    shim_directory = tmp_path / "fake cli path with spaces"
    shim_directory.mkdir()
    write_shim(shim_directory, "az")
    write_shim(shim_directory, "azd")
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "commands.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{shim_directory}{os.pathsep}{environment['PATH']}",
            "FAKE_CLI_STATE": str(state_path),
            "FAKE_CLI_LOG": str(log_path),
            "AZURE_ENV_NAME": "hook-contract-env",
        }
    )
    return environment, state_path, log_path


def run_cli(arguments, environment):
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.parametrize("script", [SCOPE_CLI, SETUP_CLI])
def test_python_entry_points_help_is_success(script):
    result = run_cli([sys.executable, str(script), "--help"], os.environ.copy())

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["list"],
        [
            "add-subscription",
            "--subscription-id",
            "00000000-0000-0000-0000-000000000000",
        ],
        ["add-management-group", "--management-group-id", "platform"],
        [
            "migrate-to-management-group",
            "--management-group-id",
            "platform",
            "--what-if",
        ],
        [
            "migrate-to-management-group",
            "--management-group-id",
            "platform",
        ],
    ],
)
def test_readme_scope_invocation_forms_are_accepted(arguments):
    result = run_cli(
        [sys.executable, str(SCOPE_CLI), *arguments, "--help"],
        os.environ.copy(),
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout


def test_scope_cli_list_json_and_invalid_input_subprocess(cli_environment):
    environment, _state_path, log_path = cli_environment
    result = run_cli(
        [
            sys.executable,
            str(SCOPE_CLI),
            "list",
            "--environment-name",
            "contract-env",
            "--json",
        ],
        environment,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert result.stderr == ""
    commands = read_log(log_path)
    assert commands
    assert all(item["tool"] == "az" for item in commands)
    assert any(
        item["arguments"][:2] == ["account", "show"] for item in commands
    )

    invalid = run_cli(
        [sys.executable, str(SCOPE_CLI), "not-a-command"],
        environment,
    )
    assert invalid.returncode == 2
    assert "invalid choice" in invalid.stderr

    missing = run_cli(
        [sys.executable, str(SCOPE_CLI), "add-subscription", "--json"],
        environment,
    )
    assert missing.returncode == 1
    assert "--subscription-id is required" in missing.stderr


def test_setup_cli_create_rerun_json_and_quoting_subprocess(cli_environment):
    environment, state_path, log_path = cli_environment
    display_name = "Contract App With Spaces 'Quoted'"
    command = [
        sys.executable,
        str(SETUP_CLI),
        "--display-name",
        display_name,
        "--json",
    ]

    first = run_cli(command, environment)
    assert first.returncode == 0, first.stderr
    first_output = json.loads(first.stdout)
    assert first_output["AZURE_TENANT_ID"] == (
        "11111111-1111-1111-1111-111111111111"
    )
    state_after_first = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after_first["application"]["displayName"] == display_name
    assert len(state_after_first["owners"]) == 2
    assert len(state_after_first["assignments"]) == 1
    assert state_after_first["azd"] == first_output

    first_log = read_log(log_path)
    mutation_count = sum(
        item["arguments"][:3] == ["rest", "--method", "post"]
        or item["arguments"][:3] == ["rest", "--method", "patch"]
        for item in first_log
    )
    graph_uris = [
        item["arguments"][item["arguments"].index("--uri") + 1]
        for item in first_log
        if "--uri" in item["arguments"]
    ]
    assert any("Contract App With Spaces ''Quoted''" in uri for uri in graph_uris)
    body_paths = [
        Path(item["bodyPath"]) for item in first_log if item["bodyPath"]
    ]
    assert body_paths
    assert not any(path.exists() for path in body_paths)

    second = run_cli(command, environment)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == first_output
    second_log = read_log(log_path)
    second_mutation_count = sum(
        item["arguments"][:3] == ["rest", "--method", "post"]
        or item["arguments"][:3] == ["rest", "--method", "patch"]
        for item in second_log
    )
    assert second_mutation_count == mutation_count


def test_setup_cli_missing_display_name_fails_without_subprocess(
    cli_environment,
):
    environment, _state_path, log_path = cli_environment
    environment.pop("AZURE_ENV_NAME")

    result = run_cli([sys.executable, str(SETUP_CLI)], environment)

    assert result.returncode == 1
    assert "AZURE_ENV_NAME is required" in result.stderr
    assert not log_path.exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh unavailable")
def test_compatibility_wrappers_delegate_through_real_processes(
    cli_environment,
):
    environment, _state_path, _log_path = cli_environment
    setup = run_cli(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SETUP_WRAPPER),
            "-DisplayName",
            "Wrapper Contract With Spaces",
        ],
        environment,
    )
    assert setup.returncode == 0, setup.stderr
    assert "Secure webhook application is configured" in setup.stdout

    scope = run_cli(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCOPE_WRAPPER),
            "list",
            "-EnvironmentName",
            "contract-env",
            "-Json",
        ],
        environment,
    )
    assert scope.returncode == 0, scope.stderr
    assert json.loads(scope.stdout) == []


def test_azure_yaml_hook_command_executes_on_current_os(cli_environment):
    environment, _state_path, _log_path = cli_environment
    azure_yaml = AZURE_YAML.read_text(encoding="utf-8")
    if os.name == "nt":
        assert "shell: pwsh\n      run: python ./scripts/configure_secure_webhook.py" in (
            azure_yaml
        )
        command = [
            "pwsh",
            "-NoProfile",
            "-Command",
            "python ./scripts/configure_secure_webhook.py",
        ]
    else:
        assert "shell: sh\n      run: python3 ./scripts/configure_secure_webhook.py" in (
            azure_yaml
        )
        command = [
            "sh",
            "-c",
            "python3 ./scripts/configure_secure_webhook.py",
        ]

    result = run_cli(command, environment)

    assert result.returncode == 0, result.stderr
    assert "Secure webhook application is configured" in result.stdout
