# Setup and checkout

## Prerequisites

See [Prerequisites](../deployment-and-operations.md#prerequisites) in the operator guide: supported workstation, required tools, permissions, and the information to collect first. Do not install or run Bicep yourself; Stage 0 prepares it.

Supported: Linux with Bash 4.4 or later; macOS with Bash 4.4 or later installed through Homebrew; Windows through Ubuntu on WSL, with the checkout in the Linux filesystem (not `/mnt/c`). Native Windows shells and Git Bash are not supported.

The operational CLIs need `requirements.txt` and `requirements-ops.txt` in the same virtual environment; Stage 3 installs them.

## Deploy with Azure Developer CLI

Use the stages in order. Each checkpoint is a stop/go decision. Do not continue
when a checkpoint fails, even if a later command appears able to run.
Hold one approved exclusive change window for the environment through all
mutations. Raw AZD hooks, provision, deploy, rollback, and down do not acquire
the lifecycle/day-2 Blob mutex. A successful `lock-status` is only a point-in-time
check, not protection against a concurrent raw AZD command. Freeze other
operators and automation; never delete or bypass an active lock.

### First deployment path

Use this path only for a new installation. An existing deployment uses
[existing-environment setup](operations.md#existing-environment-setup), not Stage 1 or
`azd env new`.

Before opening the deployment shell, collect:

| Input or access | Why it is needed |
|---|---|
| Tenant ID, subscription ID, Azure region | Pins every Azure and Graph operation to the intended target. |
| New AZD environment name | Separates local state and Azure resource names from previous installations. |
| Azure `Owner`, or `Contributor` plus `User Access Administrator` | Creates resources and role assignments. |
| Temporary Microsoft Entra `Application Administrator` | Creates and authorizes the Secure Webhook application. |
| Slack workspace ID, channel ID, and dedicated `xoxb-` bot token | Configures and validates the Slack destination. |
| Independent operations Action Group receiver | Receives availability and dependency alerts outside this bot. |
| Primary owner, backup owner, on-call destination, and HTTPS runbook URL | Satisfies the production-readiness gate. |
| Operator public IPv4, unless using an approved private path | Allows the one-time, default-deny Key Vault token transfer. |

The deployment sequence is:

1. Complete [safe shell and repository setup](#safe-shell-and-repository-setup).
2. Run Stages 0 through 9 in order.
3. Stop at the first failed checkpoint and use that stage's recovery guidance.
4. Enable baseline coverage only after the signed test, telemetry checks, Slack
   receipt, and owner approval.

### Safe shell and repository setup

From an existing interactive terminal, start a **child Bash** with this command
alone. Do not use `exec`, source these commands in your login shell, or paste
multiple blocks at once:

```console
bash --noprofile --norc
```

All Bash blocks below are complete brace groups. They enable strict error
handling, including command substitutions. A failed command or explicit `exit`
ends only this child shell and discards its state; your original interactive
terminal stays open. The braces prevent the remaining lines of a failed paste
from running in that original terminal. Successful blocks keep their working
directory, functions, ordinary variables, exports, and virtual environment in
the same child shell. Keep it open through Stage 9. Never enable `set -x`.
Use `exit` alone to leave it after finishing.

On failure, stop. Start a new child shell, enter the existing checkout, source
`scripts/runbook_guard.sh`, restore the dedicated profile exports below,
activate `.venv` if it exists, and reload only the approved nonsecret target
values. Follow the failed stage's recovery procedure; do not rerun clone,
`azd env new`, or the Stage 4 disabled-first initialization on an existing
deployment. Operations use the [existing-environment setup](operations.md#existing-environment-setup).

The repository checkout may use another local path. On WSL, it must remain in
the Linux filesystem. The two dedicated profile
paths must remain exactly as documented because the guard verifies them.

#### Fresh checkout

Use this block for a new checkout. If the destination already exists, `git
clone` stops safely. Do not delete or overwrite it: use the
[existing checkout](#existing-checkout) block, or change only
`REPOSITORY_PATH` below to a new local destination.

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
test "${BASH_VERSINFO[0]}" -gt 4 ||
  { test "${BASH_VERSINFO[0]}" -eq 4 && test "${BASH_VERSINFO[1]}" -ge 4; } ||
  { echo "Use Bash 4.4 or later." >&2; exit 1; }
case "$(uname -s)" in
  Linux|Darwin) ;;
  *) echo "Use Linux, macOS, or Ubuntu on WSL." >&2; exit 1 ;;
esac
umask 077
mkdir -p "$HOME/src"
REPOSITORY_PATH="$HOME/src/azure-service-health-slack-bot"
git clone https://github.com/ricmmartins/azure-service-health-slack-bot.git \
  "$REPOSITORY_PATH"
cd "$REPOSITORY_PATH"
source scripts/runbook_guard.sh
runbook_require_repo

export AZURE_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azure"
export AZD_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azd"
mkdir -p "$AZURE_CONFIG_DIR" "$AZD_CONFIG_DIR"
chmod 700 "$AZURE_CONFIG_DIR" "$AZD_CONFIG_DIR"
runbook_require_profiles
echo "repository-and-isolated-profiles-ready"
}
```

#### Existing checkout

Use this block when the repository already exists, including after a stopped
paste. Change only `REPOSITORY_PATH` when the checkout is elsewhere. This block
does not pull, switch branches, create an AZD environment, or mutate Azure.

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
test "${BASH_VERSINFO[0]}" -gt 4 ||
  { test "${BASH_VERSINFO[0]}" -eq 4 && test "${BASH_VERSINFO[1]}" -ge 4; } ||
  { echo "Use Bash 4.4 or later." >&2; exit 1; }
case "$(uname -s)" in
  Linux|Darwin) ;;
  *) echo "Use Linux, macOS, or Ubuntu on WSL." >&2; exit 1 ;;
esac
umask 077
REPOSITORY_PATH="$HOME/src/azure-service-health-slack-bot"
test -e "$REPOSITORY_PATH/.git" || {
  echo "Stop: REPOSITORY_PATH is not a Git checkout." >&2
  exit 1
}
cd "$REPOSITORY_PATH"
source scripts/runbook_guard.sh
runbook_require_repo

export AZURE_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azure"
export AZD_CONFIG_DIR="$HOME/.config/azure-service-health-slack-bot/azd"
test -d "$AZURE_CONFIG_DIR"
test -d "$AZD_CONFIG_DIR"
chmod 700 "$AZURE_CONFIG_DIR" "$AZD_CONFIG_DIR"
runbook_require_profiles
echo "existing-checkout-ready"
}
```

These dedicated profiles isolate this walkthrough from another corporate CLI
session. Do not copy token caches into them or sign them into
another tenant. They contain authentication data: never commit, print, or share
them. `AZURE_CONFIG_DIR` also isolates Bicep, extensions, and Azure CLI settings;
the first use needs its own Bicep installation. `AZD_CONFIG_DIR` isolates AZD
settings. See Microsoft's [Azure CLI configuration](https://learn.microsoft.com/cli/azure/azure-cli-configuration)
and [AZD configuration](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-config).

If the target guard rejects permissions on an existing checkout, inspect only
ownership and mode with `stat`, not file contents. With owner approval, use
`chmod 700 .azure ".azure/$AZURE_ENV_NAME"` and
`chmod 600 ".azure/$AZURE_ENV_NAME/.env"` on those exact paths, then repeat the
guard. Do not weaken the guard for a shared or Windows-mounted environment.

The examples use placeholders. Set them only in your local shell and AZD
environment. Do not add real tenant IDs, subscription IDs, user names, Slack
tokens, or channel IDs to tracked files.

### Resume after a stopped stage

Do not skip forward. Start a new child Bash and run
[existing checkout](#existing-checkout). If the AZD environment or Azure
resources already exist, continue with
[existing-environment setup](operations.md#existing-environment-setup) and repeat only the
failed stage using its **Recovery** guidance. Do not repeat `git clone`,
`azd env new`, or token bootstrap for an existing environment.

---

[Operator guide](../deployment-and-operations.md#first-deployment-path) | [Next: Stage 0](stage-0.md)
