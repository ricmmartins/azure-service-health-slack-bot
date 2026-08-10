<#
.SYNOPSIS
Compatibility wrapper for the canonical cross-platform Python scope manager.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet(
        "list",
        "add-subscription",
        "remove-subscription",
        "add-management-group",
        "remove-management-group",
        "migrate-to-management-group"
    )]
    [string] $Command,
    [string] $SubscriptionId,
    [string] $ManagementGroupId,
    [string] $EnvironmentName,
    [switch] $Force,
    [switch] $Json
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
    (Join-Path $PSScriptRoot "manage_alert_scopes.py"),
    $Command
)
if ($SubscriptionId) {
    $arguments += @("--subscription-id", $SubscriptionId)
}
if ($ManagementGroupId) {
    $arguments += @("--management-group-id", $ManagementGroupId)
}
if ($EnvironmentName) {
    $arguments += @("--environment-name", $EnvironmentName)
}
if ($Force) {
    $arguments += "--force"
}
$arguments += "--json"
if ($WhatIfPreference) {
    $arguments += "--what-if"
}
if (-not $WhatIfPreference -and $PSBoundParameters.ContainsKey("Confirm")) {
    if (-not $PSCmdlet.ShouldProcess(
        "$Command day-2 scope operation",
        "Delegate to the Python scope manager"
    )) {
        return
    }
}

$raw = & $python.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Python scope manager failed with exit code $LASTEXITCODE."
}
$text = ($raw -join [Environment]::NewLine).Trim()
if ($Json) {
    $text
}
elseif ($text) {
    $text | ConvertFrom-Json -Depth 100
}
