# Local development

## Local development

First complete [safe shell and repository setup](runbook/00-setup.md#safe-shell-and-repository-setup),
then create a virtual environment and install dependencies:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
}
```

On macOS, run the commands with the Homebrew Bash rather than the older
`/bin/bash` included with macOS. On Windows, use Ubuntu on WSL. Native Windows
shell commands are outside this repository's supported operational surface.

After activation, `python`, `python -m pip`, and the test commands use the
virtual environment. Activate it again in each new shell.

Copy `.env-example` to `.env`, then set:

- `SLACK_BOT_TOKEN`
- `AZURE_TABLE_ENDPOINT`, using
  `https://<account>.table.core.windows.net` with no path, query, credentials,
  or custom port
- one routing source
- `SERVICE_HEALTH_EXPECTED_AUDIENCE` only when `APP_ENV` is not
  `development` or `test`

The example routing file uses synthetic channel and subscription IDs. For local
Table access, sign in with an identity that has Storage Table data permissions:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
az login
python app.py
}
```

Build the production image with:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
docker build -t azure-service-health-slack-bot .
}
```

The Docker build retries package download through Microsoft's Python package
proxy if the configured package index fails. Override `PIP_INDEX_URL` or
`PIP_FALLBACK_INDEX_URL` with Docker build arguments when required by your
network policy.
