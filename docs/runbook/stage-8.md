### Stage 8: verify probes and the authentication boundary

Prerequisites: stage 7 shows one active revision.

Run:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_target
RESPONSE_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_FILE"' EXIT
test "$(curl --connect-timeout 10 --max-time 60 -fsS "$APP_URI/healthz")" = '{"status":"healthy"}' &&
test "$(curl --connect-timeout 10 --max-time 60 -fsS "$APP_URI/readyz")" = '{"status":"ready"}' &&
HTTP_CODE="$(curl --connect-timeout 10 --max-time 60 -sS -o "$RESPONSE_FILE" -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  "$APP_URI/api/service-health")" &&
test "$HTTP_CODE" = "401" &&
python3 -m json.tool "$RESPONSE_FILE" &&
echo "ingress-auth-checkpoint-passed" || { echo "Ingress checkpoint failed." >&2; exit 1; }
rm -f "$RESPONSE_FILE"
trap - EXIT
}
```

Expected state:

- `/healthz` returns exactly `{"status":"healthy"}`;
- `/readyz` returns exactly `{"status":"ready"}`;
- an unauthenticated webhook request returns HTTP `401`;
- the final line is `ingress-auth-checkpoint-passed`.

Checkpoint: all three responses must match. A public `200` from the webhook is a
security failure and must block the signed test.

Recovery:

- A probe timeout points to ingress, revision, or startup failure. Inspect
  system and console logs.
- A readiness `503` points to missing required environment values or client
  construction failure.
- A webhook response other than `401` requires inspection of Container Apps
  authentication and Flask authorization before proceeding.
