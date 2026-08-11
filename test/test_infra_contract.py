import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN_BICEP = ROOT / "infra" / "main.bicep"
HOOK_POPULATED_PARAMETERS = (
    "secureWebhookClientId",
    "secureWebhookObjectId",
    "secureWebhookIdentifierUri",
)


def test_secure_webhook_hook_parameters_have_empty_defaults():
    source = MAIN_BICEP.read_text(encoding="utf-8")

    for parameter in HOOK_POPULATED_PARAMETERS:
        declaration = rf"^param {parameter} string = ''$"
        assert re.search(declaration, source, flags=re.MULTILINE)


def test_secure_webhook_parameters_fail_closed_before_module_use():
    source = MAIN_BICEP.read_text(encoding="utf-8")
    expected_guard = """\
var hasSecureWebhookConfiguration = !empty(secureWebhookClientId) && !empty(secureWebhookObjectId) && !empty(secureWebhookIdentifierUri)
var secureWebhookConfiguration = hasSecureWebhookConfiguration ? {
    clientId: secureWebhookClientId
    objectId: secureWebhookObjectId
    identifierUri: secureWebhookIdentifierUri
  }
  : fail('Secure Webhook configuration is missing. Run the preprovision hook before deploying.')"""

    assert expected_guard in source
    assert (
        "secureWebhookClientId: secureWebhookConfiguration.clientId"
        in source
    )
    assert (
        "secureWebhookObjectId: secureWebhookConfiguration.objectId"
        in source
    )
    assert source.count(
        "secureWebhookIdentifierUri: "
        "secureWebhookConfiguration.identifierUri"
    ) == 2
