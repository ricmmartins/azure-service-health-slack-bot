<#
.SYNOPSIS
Compatibility wrapper for the canonical Python Secure Webhook configurator.
#>
[CmdletBinding()]
param(
    [string] $DisplayName,
    [string] $AznsApplicationId = "461e8683-5575-4561-ac7f-899cc907d62a",
    [string] $RoleName = "ActionGroupsSecureWebhook"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw "Python 3 is required. Install Python 3.13 or later and retry."
}

$arguments = @(
    (Join-Path $PSScriptRoot "configure_secure_webhook.py"),
    "--azns-application-id",
    $AznsApplicationId,
    "--role-name",
    $RoleName
)
if ($DisplayName) {
    $arguments += @("--display-name", $DisplayName)
}

& $python.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Python Secure Webhook configurator failed with exit code $LASTEXITCODE."
}
