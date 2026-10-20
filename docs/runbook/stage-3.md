# Stage 3: install dependencies and validate the routing file

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
application routing contract.

How the file works:

- `channel_id` is a Slack channel ID (starts with `C`). In Slack, open the
  channel, select its name, and copy the Channel ID at the bottom of the
  details window. `subscription_ids` are Azure subscription IDs.
- `default_channel_id` receives every alert that matches no rule.
- Each entry in `rules` sends matching alerts to its own channel. The example
  routes Azure Kubernetes Service alerts in East US for one subscription to
  one channel, and all Azure Storage alerts to another. The services are only
  examples. `subscription_ids`, `services`, and `regions` are optional filters
  that must all match.
- If several rules match, the highest `priority` wins, then the most specific
  rule, then the first in the file.
- Simplest setup: set only `default_channel_id` and use `"rules": []`.

Validate both JSON syntax and the application routing contract:

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

- `runbook_require_repo: command not found` means the terminal was restarted
  and lost its state. Start the child shell from
  [safe shell setup](00-setup.md#safe-shell-and-repository-setup), run the
  [existing checkout](00-setup.md#existing-checkout) block, then restore the
  Stage 1 values and the virtual environment (use your own values) and rerun
  the failed block:

  ```bash
  export AZURE_ENV_NAME="<environment-name>"
  export TARGET_TENANT_ID="<tenant-id>"
  export TARGET_SUBSCRIPTION_ID="<subscription-id>"
  export AZURE_LOCATION="<region>"
  source .venv/bin/activate
  runbook_require_target
  ```

- A `JSONDecodeError` identifies malformed JSON. Correct the indicated line and
  rerun both validators.
- `InvalidRoutingConfiguration` identifies a missing channel, invalid rule, or
  invalid priority.
- If the placeholder scan stops the command before the final line, replace the
  remaining example value.

---

[Previous: Stage 2](stage-2.md) | [Next: Stage 4](stage-4.md)
