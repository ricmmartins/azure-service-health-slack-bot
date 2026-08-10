# Contributing

Thank you for improving this community reference implementation.

## Local setup

Use Python 3.13, the Azure CLI with Bicep, and Docker with a Linux container
engine. Python is the canonical cross-platform interface for Secure Webhook
setup and day-2 scope management. PowerShell 7+ is optional unless you are
running the AZD hook on Windows or testing the temporary compatibility
wrappers; it hosts the command but contains no operational business logic.
Create and activate a virtual environment, then install the application and
test dependencies:

```sh
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-test.txt
```

Copy `.env-example` to `.env` only when running the application locally. Never
commit `.env` or real credentials.

## Validation

Run the checks used by CI before opening a pull request.

```sh
python -m pytest -q
python -m flake8 .
python -m pytest -q test/test_manage_alert_scopes.py test/test_configure_secure_webhook.py test/test_cli_subprocess.py
pwsh -NoProfile -Command "Invoke-Pester test/ManageAlertScopes.Tests.ps1, test/ConfigureSecureWebhook.Tests.ps1 -CI"
az bicep build --file infra/main.bicep --stdout
az bicep lint --file infra/main.bicep
az bicep build --file infra/day2/service-health-alert-scope.bicep --stdout
az bicep lint --file infra/day2/service-health-alert-scope.bicep
docker build -t azure-service-health-slack-bot:ci .
```

## Pull requests

Keep each pull request focused, explain the motivation and operational impact,
and add or update tests and documentation for changed behavior. Avoid unrelated
formatting or refactoring. Describe any validation that could not be run.

Changes to infrastructure, authentication, secrets, network boundaries, or
deployment scripts must preserve least privilege and deployment safety. Do not
broaden roles, expose protected resources, weaken webhook authentication, or
introduce destructive deployment defaults without an explicit security and
operational justification.

For security vulnerabilities, do not open an issue or pull request. Follow the
private reporting process in [SECURITY.md](SECURITY.md).
