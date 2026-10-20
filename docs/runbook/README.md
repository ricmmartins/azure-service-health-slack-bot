# Validated deployment command reference

Copy/paste command blocks for the [operator guide](../deployment-and-operations.md), one page per step. Each page has the exact commands, the expected result, and recovery guidance. Production safeguards, day-2 scope management, and decommissioning are retained.

This Flask service receives Azure Service Health notifications from an Azure
Monitor Secure Webhook Action Group and posts them to Slack. It keeps one root
message per subscription and tracking ID. Newer notifications update that root
and add a broadcast reply to its thread.

This is a community reference implementation. Azure Service Health remains the
source of truth. The service does not acknowledge incidents, open Azure support
requests, receive Slack events, or replace an incident management system.

## Steps

| Step | Page |
|---|---|
| Open the shell, clone or reuse the checkout | [Setup and checkout](00-setup.md) |
| 0. Verify the workstation | [Stage 0](stage-0.md) |
| 1. Pin the Azure and AZD target | [Stage 1](stage-1.md) |
| 2. Create the Slack app | [Stage 2](stage-2.md) |
| 3. Install dependencies | [Stage 3](stage-3.md) |
| 4. Load nonsecret inputs | [Stage 4](stage-4.md) |
| 5. Reconcile Entra and preview Azure | [Stage 5](stage-5.md) |
| 6. Provision and transfer the Slack token | [Stage 6](stage-6.md) |
| 7. Build and deploy | [Stage 7](stage-7.md) |
| 8. Verify probes and authentication | [Stage 8](stage-8.md) |
| 9. Signed test and baseline cutover | [Stage 9](stage-9.md) |
| Add subscriptions or a Management Group | [Manage alert scopes](scopes.md) |
| Day-2 operations and troubleshooting | [Operations](operations.md) |
| Remove the solution | [Decommission](decommission.md) |
| Acceptance record and tests | [Acceptance and tests](99-acceptance-and-tests.md) |

Run stages in order and stop at the first failed checkpoint.
This guide is written for Bash, Azure CLI, and Azure Developer CLI (`azd`).
You do not need to author or understand Bicep to deploy the solution. Bicep is
the repository's internal infrastructure format; Stage 0 only asks Azure CLI to
compile the checked-in templates before any deployment.

Copy commands from the fenced blocks on these pages. Do not copy rendered command
text from chat, email, or another rich-text editor: it may add escapes such as
`runbook\_require\_repo`, replace quotes, or truncate a pasted block.
