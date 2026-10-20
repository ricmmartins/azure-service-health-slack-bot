### Stage 3: install dependencies and validate the routing file

Prerequisites: stages 0 through 2 are complete.

Run:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-ops.txt
python -c \
  'from azure.keyvault.secrets import SecretClient; from azure.storage.blob import BlobServiceClient; print("operations-sdk-ready")'

test ! -e config/service_health_routes.json
cp config/service_health_routes.example.json \
  config/service_health_routes.json
vi config/service_health_routes.json
}
```

Replace every synthetic channel and subscription ID. Keep
`default_channel_id`; it is required. Then validate both JSON syntax and the
application routing contract:

```bash
{
set -eEuo pipefail
shopt -s inherit_errexit
runbook_require_repo
python -m json.tool config/service_health_routes.json >/dev/null &&
python -c 'import json; from pathlib import Path; from service_health.routing import RoutingConfig; RoutingConfig.from_dict(json.loads(Path("config/service_health_routes.json").read_text())); print("routing-valid")' &&
! grep -Eq \
  'C0123456789|C1111111111|C2222222222|00000000-0000-0000-0000-000000000000' \
  config/service_health_routes.json &&
echo "routing-checkpoint-passed" || { echo "Routing validation failed." >&2; exit 1; }
}
```

Expected state: the parser prints `routing-valid`, the placeholder scan is
silent, the SDK import prints `operations-sdk-ready`, and the final line is
`routing-checkpoint-passed`.

Checkpoint: inspect the file once more and confirm that its subscription IDs
belong to the intended tenant and its channel IDs belong to the intended Slack
workspace.

Recovery:

- A `JSONDecodeError` identifies malformed JSON. Correct the indicated line and
  rerun both validators.
- `InvalidRoutingConfiguration` identifies a missing channel, invalid rule, or
  invalid priority.
- If the placeholder scan stops the command before the final line, replace the
  remaining example value.
