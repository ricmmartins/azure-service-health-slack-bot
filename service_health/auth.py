import base64
import json


class MissingWebhookIdentity(PermissionError):
    pass


class InvalidWebhookIdentity(PermissionError):
    pass


_APP_ID_CLAIMS = {
    "appid",
    "azp",
    "http://schemas.microsoft.com/identity/claims/applicationid",
}


def authorize_easy_auth(headers, expected_client_app_id, expected_role,
                        expected_audience):
    encoded_principal = headers.get("X-MS-CLIENT-PRINCIPAL")
    if not encoded_principal:
        raise MissingWebhookIdentity(
            "Easy Auth client principal is required")
    if headers.get("X-MS-CLIENT-PRINCIPAL-IDP", "").casefold() not in {
        "aad",
        "azureactivedirectory",
    }:
        raise InvalidWebhookIdentity(
            "Webhook identity provider must be Microsoft Entra ID")

    try:
        padding = "=" * (-len(encoded_principal) % 4)
        decoded = base64.b64decode(
            encoded_principal + padding, validate=True).decode("utf-8")
        principal = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWebhookIdentity(
            "Easy Auth client principal is invalid") from exc

    raw_claims = principal.get("claims")
    if not isinstance(raw_claims, list):
        raise InvalidWebhookIdentity(
            "Easy Auth client principal has no claims")

    claims = {}
    for claim in raw_claims:
        if not isinstance(claim, dict):
            continue
        claim_type = claim.get("typ")
        claim_value = claim.get("val")
        if isinstance(claim_type, str) and isinstance(claim_value, str):
            claims.setdefault(claim_type.casefold(), []).append(claim_value)

    app_ids = {
        value.casefold()
        for claim_type, values in claims.items()
        if claim_type in _APP_ID_CLAIMS
        for value in values
    }
    if expected_client_app_id.casefold() not in app_ids:
        raise InvalidWebhookIdentity(
            "Webhook caller application is not authorized")

    audiences = {
        value.casefold()
        for value in claims.get("aud", [])
    }
    if expected_audience and expected_audience.casefold() not in audiences:
        raise InvalidWebhookIdentity(
            "Webhook token audience is not authorized")

    roles = {
        role.casefold()
        for value in claims.get("roles", [])
        for role in value.split(",")
    }
    role_claim = (
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
    roles.update(value.casefold() for value in claims.get(role_claim, []))
    if expected_role.casefold() not in roles:
        raise InvalidWebhookIdentity(
            "Webhook caller does not have the required app role")


def encode_test_principal(claims):
    value = {
        "auth_typ": "aad",
        "claims": [
            {"typ": claim_type, "val": claim_value}
            for claim_type, claim_value in claims
        ],
    }
    return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")
