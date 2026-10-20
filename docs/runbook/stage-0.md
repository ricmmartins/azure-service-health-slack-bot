### Stage 0: verify the workstation

Prerequisites:

- the safe child shell and repository setup above, with Bash 4.4+ on Linux,
  macOS, or Ubuntu on WSL
- Python 3.12 or 3.13
- current stable Azure CLI with its managed Bicep CLI `0.46.1` or later
- Azure CLI `log-analytics` extension
- current stable Azure Developer CLI
- Docker with a running Linux container engine
- Git

The dedicated Azure CLI profile starts empty. `azd` uses the repository's
infrastructure templates internally; you do not need to install or run Bicep
yourself. On the first run, this stage prepares and validates that internal
tooling automatically. This does not deploy or change Azure resources.

Run:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
for tool in python3 az azd; do
  TOOL_PATH="$(command -v "$tool")"
  test -x "$TOOL_PATH"
  case "$TOOL_PATH" in
    *.exe|*.cmd|*.bat)
      echo "Stop: $tool must be native to this Bash environment." >&2; exit 1 ;;
  esac
  if [[ "$(uname -s)" = Linux && "$TOOL_PATH" = /mnt/* ]]; then
    echo "Stop: $tool must be Linux-native when running on Linux/WSL." >&2
    exit 1
  fi
done
runbook_require_profiles
test -r scripts/configure_secure_webhook.py
test -r scripts/manage_slack_token.py
test -r scripts/manage_alert_scopes.py
BICEP_MIN_VERSION="0.46.1"
BICEP_TESTED_VERSION="0.46.1"

python3 --version
python3 -c 'import sys; assert sys.version_info[:2] in ((3, 12), (3, 13)), "Use Python 3.12 or 3.13"'
az version
az config set bicep.use_binary_from_path=false
BICEP_FROM_PATH="$(az config get bicep.use_binary_from_path \
  --query value -o tsv 2>/dev/null)" || {
  echo "Could not verify the Azure CLI Bicep binary source; stop." >&2
  exit 1
}
test "$(printf '%s\n' "$BICEP_FROM_PATH" | \
  tr '[:upper:]' '[:lower:]')" = "false" || {
  echo "Azure CLI is configured to run bicep from PATH; stop." >&2
  exit 1
}
BICEP_VERSION_OUTPUT="$(az bicep version 2>/dev/null)" || {
  echo "Installing Azure CLI-managed Bicep $BICEP_TESTED_VERSION..."
  az bicep install --version "v$BICEP_TESTED_VERSION" || {
    echo "Could not install Azure CLI-managed Bicep; stop." >&2
    exit 1
  }
  BICEP_VERSION_OUTPUT="$(az bicep version)" || {
    echo "Azure CLI-managed Bicep is still unavailable; stop." >&2
    exit 1
  }
}
BICEP_VERSION="$(printf '%s\n' "$BICEP_VERSION_OUTPUT" | \
  sed -nE 's/^Bicep CLI version ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')"
test -n "$BICEP_VERSION" || {
  echo "Could not parse the Azure CLI-managed Bicep version; stop." >&2
  exit 1
}
python3 -c \
  'import sys; p=lambda value: tuple(map(int, value.split("."))); raise SystemExit(p(sys.argv[1]) < p(sys.argv[2]))' \
  "$BICEP_VERSION" "$BICEP_MIN_VERSION" || {
  echo "Bicep $BICEP_VERSION is below required $BICEP_MIN_VERSION; stop." >&2
  exit 1
}
printf 'Azure CLI-managed Bicep %s satisfies minimum %s (tested: %s).\n' \
  "$BICEP_VERSION" "$BICEP_MIN_VERSION" "$BICEP_TESTED_VERSION"
az bicep build --file infra/main.bicep --stdout > /dev/null || {
  echo "Central Bicep build failed; stop." >&2
  exit 1
}
az bicep lint --file infra/main.bicep || {
  echo "Central Bicep lint failed; stop." >&2
  exit 1
}
az bicep build \
  --file infra/day2/service-health-alert-scope.bicep --stdout > /dev/null || {
  echo "Day-2 Bicep build failed; stop." >&2
  exit 1
}
az bicep lint --file infra/day2/service-health-alert-scope.bicep || {
  echo "Day-2 Bicep lint failed; stop." >&2
  exit 1
}
az extension add --name log-analytics --upgrade --yes
az extension add --name containerapp --upgrade --yes
az extension show --name log-analytics \
  --query '{name:name,version:version}' -o table
command -v azd
type -a azd
azd version
AZD_VERSION_OUTPUT="$(azd version)"
[[ "$AZD_VERSION_OUTPUT" == *"(stable)"* ]]
AZD_VERSION="$(printf '%s\n' "$AZD_VERSION_OUTPUT" | sed -nE 's/^azd version ([0-9]+\.[0-9]+\.[0-9]+) .*/\1/p')"
python3 -c 'import sys; assert tuple(map(int, sys.argv[1].split("."))) >= (1, 31, 0), "Use stable AZD 1.31.0 or later"' "$AZD_VERSION"
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker info --format 'os={{.OSType}}'
test "$(docker info --format '{{.OSType}}')" = linux
git --version
}
```

Expected state:

- Python reports `3.12.x` or `3.13.x`. The operator hook and CLI were verified
  with Ubuntu Python 3.12, while CI validates 3.12 and 3.13 and the deployed
  container image is pinned to Python 3.14.
- `az bicep version` reports the Azure CLI-managed Bicep CLI at `0.46.1` or
  later, `bicep.use_binary_from_path` is exactly `false`, and both repository
  template graphs build and lint with that exact installed version. `0.46.1` is
  the current repository-tested version. Passing the minimum check does not
  claim that every later Bicep release is compatible; the four build/lint
  commands are the compatibility gate for the installed version.
- Azure CLI reports the installed `log-analytics` extension and its version.
- `command -v azd` identifies the binary that the shell will execute. If
  `type -a azd` lists more than one installation, the first one must be the
  current stable installation.
- AZD reports `1.31.0` or a later stable version. The complete clean deployment,
  bootstrap, image deployment, signed Action Group test, and day-2 previews in
  this guide were exercised with AZD `1.31.0`.
- Every command exits with status `0`.
- Docker reports both a client and server version.
- Docker reports `os=linux`.

Checkpoint: stop until all six tools respond successfully. A Docker client
version without a server version is not sufficient.

Recovery:

- Install or update the tool from the links in [Prerequisites](00-setup.md#prerequisites).
- The repository templates use the Bicep null-forgiving operator in
  `infra/modules/container-app.bicep`. Azure CLI-managed Bicep `0.41.2`
  reproduced `BCP129` against the current graph; `0.46.1` built and linted both
  graphs. Treat `0.46.1` as the supported minimum, not merely the last version
  found on a workstation.
- If the binary-source check fails or returns anything except `false`, run
  `az config set bicep.use_binary_from_path=false`, recheck the setting, and
  rerun the complete Stage 0 block. Do not run a Bicep command before this gate
  passes.
- If the automatic Bicep installation fails, correct network or Azure CLI
  policy access, then rerun the complete Stage 0 block.
- If `az bicep version` reports less than `0.46.1`, run the idempotent
  `az bicep upgrade`, recheck `az bicep version`, and rerun all four build/lint
  commands in the Stage 0 block. Stop if the upgrade or any recheck fails.
- `az bicep` uses Azure CLI's self-contained Bicep installation. A standalone
  `bicep --version`, a VS Code extension version, or another deployment host's
  Bicep installation does not satisfy this checkpoint.
- `az bicep install` and `az bicep upgrade` require network access. For an
  offline or air-gapped workstation, follow Microsoft's
  [air-gapped installation procedure](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install#install-on-air-gapped-cloud)
  to obtain the exact platform asset from the official `0.46.1` release, verify
  its SHA-256 digest against the digest published with that release asset, and
  place it in Azure CLI's `.azure/bin` location. Then rerun the complete Stage 0
  block. If the reviewed binary or its published digest cannot be obtained,
  remain stopped; do not substitute an unrelated standalone executable.
- If an older AZD installation shadows the current one, put the current
  installation directory before the older directory in `PATH`, start a new
  shell, and repeat `command -v azd`, `type -a azd`, and `azd version`.
- On WSL, enable the distribution in Docker Desktop under **WSL integration**,
  then rerun `docker info`.
- If the Log Analytics extension cannot be installed, correct Azure CLI
  extension policy or network access before deployment; do not rely on an
  interactive dynamic-install prompt during incident troubleshooting.
