# Stage 7: build and deploy the application

Prerequisites:

- the stage 6 resource checkpoint passed;
- Docker still reports a running Linux server;
- the current directory is the repository root.

Run:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
docker info --format 'os={{.OSType}}'
test "$(docker info --format '{{.OSType}}')" = linux
azd deploy -e "$AZURE_ENV_NAME" --no-prompt
}
```

On WSL with Docker Desktop, do not replace `PATH` with a minimal Linux-only
value before `azd deploy`. Docker may need the Windows credential helper under
`/mnt/c/Program Files/Docker/Docker/resources/bin`. If packaging fails with
`docker-credential-desktop.exe: executable file not found`, restore the normal
WSL `PATH`, confirm `docker info`, and rerun the idempotent `azd deploy`.

Expected state: AZD builds the Docker image, pushes it to the deployed registry,
updates the Container App, and reports a successful service deployment.

Inspect active revisions:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query '[?properties.active].{name:name,active:properties.active,created:properties.createdTime}' \
  -o table

test "$(az containerapp revision list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query 'length([?properties.active])' -o tsv)" = "1" &&
echo "application-revision-count-ready" || { echo "Revision checkpoint failed." >&2; exit 1; }
LATEST_REVISION="$(az containerapp show --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" --query properties.latestRevisionName -o tsv)"
READY_REVISION="$(az containerapp show --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" --query properties.latestReadyRevisionName -o tsv)"
test -n "$LATEST_REVISION"
test "$LATEST_REVISION" = "$READY_REVISION"
test "$(az containerapp revision show --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" --revision "$READY_REVISION" \
  --query properties.healthState -o tsv)" = Healthy
echo "application-revision-ready"
}
```

Checkpoint: single revision mode must show exactly one active revision and its
`active` value must be `True`. The latest revision must be the latest ready
revision and report `Healthy`, so a still-running older revision cannot hide
a failed rollout. The final line must be
`application-revision-ready`.

Recovery:

- If Docker cannot connect, fix the engine before rerunning
  `azd deploy -e "$AZURE_ENV_NAME" --no-prompt`.
- If image push or pull fails, inspect the managed identity's `AcrPull` role and
  allow for role-assignment propagation.
- If the new revision does not become active, inspect
  `az containerapp logs show --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" --type system --tail 100`
  before retrying or activating a known-good revision.

---

[Previous: Stage 6](stage-6.md) | [Next: Stage 8](stage-8.md)
