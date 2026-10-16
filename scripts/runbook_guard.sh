#!/usr/bin/env bash
# Source only in the dedicated runbook shell, never in the user's login shell.

runbook_require_repo() {
  local root origin
  root="$(git rev-parse --show-toplevel)" || return 1
  test "$(pwd -P)" = "$(cd "$root" && pwd -P)" || {
    echo "Stop: run this block at the repository root." >&2
    return 1
  }
  origin="$(git remote get-url origin)" || return 1
  case "$origin" in
    https://github.com/ricmmartins/azure-service-health-slack-bot|\
    https://github.com/ricmmartins/azure-service-health-slack-bot.git|\
    git@github.com:ricmmartins/azure-service-health-slack-bot.git) ;;
    *) echo "Stop: origin is not the Service Health bot repository." >&2; return 1 ;;
  esac
  local file
  for file in azure.yaml infra/main.bicep \
      infra/day2/service-health-alert-scope.bicep \
      scripts/configure_secure_webhook.py requirements-ops.txt; do
    test -f "$file" || {
      echo "Stop: required repository file is missing: $file" >&2
      return 1
    }
  done
  case "$(findmnt -T "$(pwd -P)" -n -o FSTYPE)" in
    ext2|ext3|ext4|xfs|btrfs|zfs|overlay|tmpfs) ;;
    *) echo "Stop: use a Linux filesystem, not a Windows/network mount." >&2; return 1 ;;
  esac
}

runbook_require_profiles() {
  test "${AZURE_CONFIG_DIR:-}" = \
    "$HOME/.config/azure-service-health-slack-bot/azure" &&
  test "${AZD_CONFIG_DIR:-}" = \
    "$HOME/.config/azure-service-health-slack-bot/azd" || {
    echo "Stop: use the dedicated CLI profiles from Stage 0." >&2
    return 1
  }
  test -d "$AZURE_CONFIG_DIR" && test -d "$AZD_CONFIG_DIR" || {
    echo "Stop: dedicated CLI profile directories are missing." >&2
    return 1
  }
}

runbook_require_target() {
  runbook_require_repo || return 1
  runbook_require_profiles || return 1
  : "${AZURE_ENV_NAME:?Set the approved AZD environment.}"
  : "${TARGET_TENANT_ID:?Set the approved tenant.}"
  : "${TARGET_SUBSCRIPTION_ID:?Set the approved subscription.}"
  : "${AZURE_LOCATION:?Set the approved region.}"
  [[ "$AZURE_ENV_NAME" =~ ^[a-z0-9][a-z0-9-]*[a-z0-9]$ ]] || {
    echo "Stop: invalid environment name." >&2
    return 1
  }
  python3 - "$AZURE_CONFIG_DIR" "$AZD_CONFIG_DIR" \
    .azure ".azure/$AZURE_ENV_NAME" ".azure/$AZURE_ENV_NAME/.env" <<'PY' || return 1
import os
import stat
import sys
from pathlib import Path

for value in sys.argv[1:]:
    path = Path(value)
    metadata = path.lstat()
    if (path.is_symlink() or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise SystemExit("Stop: CLI/AZD state must be owner-only and not symlinked.")
PY
  local state_mount state_path
  for state_path in "$AZURE_CONFIG_DIR" "$AZD_CONFIG_DIR" ".azure/$AZURE_ENV_NAME"; do
    state_mount="$(findmnt -T "$state_path" -n -o FSTYPE)" || return 1
    case "$state_mount" in
      ext2|ext3|ext4|xfs|btrfs|zfs|overlay|tmpfs) ;;
      *) echo "Stop: CLI/AZD state is not on a Linux filesystem." >&2; return 1 ;;
    esac
  done
  local account_tenant account_subscription graph_tenant name expected actual
  actual="$(az cloud show --query name -o tsv)" || return 1
  test "$actual" = AzureCloud || {
    echo "Stop: this runbook targets Azure public cloud only." >&2
    return 1
  }
  account_tenant="$(az account show --query tenantId -o tsv)" || return 1
  account_subscription="$(az account show --query id -o tsv)" || return 1
  # Query metadata only. Never materialize accessToken in shell output/state.
  graph_tenant="$(az account get-access-token --resource-type ms-graph \
    --query tenant -o tsv)" || return 1
  if [[ "${account_tenant,,}" != "${TARGET_TENANT_ID,,}" ||
        "${account_subscription,,}" != "${TARGET_SUBSCRIPTION_ID,,}" ||
        "${graph_tenant,,}" != "${TARGET_TENANT_ID,,}" ]]; then
    echo "Stop: ARM or default Microsoft Graph token tenant does not match." >&2
    return 1
  fi
  actual="$(azd env get-value AZURE_ENV_NAME --no-prompt)" || return 1
  test "$actual" = "$AZURE_ENV_NAME" || {
    echo "Stop: selected AZD environment differs from the approved environment." >&2
    return 1
  }
  for name in AZURE_ENV_NAME AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID AZURE_LOCATION; do
    case "$name" in
      AZURE_ENV_NAME) expected="$AZURE_ENV_NAME" ;;
      AZURE_TENANT_ID) expected="$TARGET_TENANT_ID" ;;
      AZURE_SUBSCRIPTION_ID) expected="$TARGET_SUBSCRIPTION_ID" ;;
      AZURE_LOCATION) expected="$AZURE_LOCATION" ;;
    esac
    actual="$(azd env get-value "$name" -e "$AZURE_ENV_NAME" --no-prompt)" \
      || return 1
    test -n "$actual" && test "${actual,,}" = "${expected,,}" || {
      echo "Stop: AZD target binding differs for $name." >&2
      return 1
    }
  done
}
