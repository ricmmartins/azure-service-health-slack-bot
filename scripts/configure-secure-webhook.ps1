[CmdletBinding()]
param(
    [string] $DisplayName = "Azure Service Health Slack Bot - $env:AZURE_ENV_NAME",
    [string] $AznsApplicationId = "461e8683-5575-4561-ac7f-899cc907d62a",
    [string] $RoleName = "ActionGroupsSecureWebhook"
)

$ErrorActionPreference = "Stop"

# az rest --body <jsonString> is unreliable on Windows: PowerShell -> az.cmd -> cmd.exe
# argument passing can corrupt embedded double quotes, causing Graph to reject the
# payload with "Unable to read JSON request payload". Writing the body to a temp file
# and passing --body @file avoids all shell quoting issues and works identically on
# Windows and POSIX shells. This helper also checks the az CLI exit code so Graph
# errors fail the script instead of silently leaving $application/$role as $null.
function Invoke-GraphRest {
    param(
        [Parameter(Mandatory = $true)][string] $Method,
        [Parameter(Mandatory = $true)][string] $Uri,
        [Hashtable] $BodyObject
    )

    $azArgs = @("rest", "--method", $Method, "--uri", $Uri)
    $tempFile = $null
    if ($PSBoundParameters.ContainsKey("BodyObject")) {
        $tempFile = [System.IO.Path]::GetTempFileName()
        ($BodyObject | ConvertTo-Json -Depth 10) | Set-Content -Path $tempFile -Encoding utf8NoBOM
        $azArgs += @("--headers", "Content-Type=application/json", "--body", "@$tempFile")
    }

    try {
        $raw = & az @azArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "az rest $Method $Uri failed (exit $LASTEXITCODE): $raw"
        }
    }
    finally {
        if ($tempFile -and (Test-Path $tempFile)) {
            Remove-Item -Path $tempFile -Force
        }
    }

    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return $raw | ConvertFrom-Json
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Run 'azd auth login' and install Azure CLI."
}

$account = az account show --output json | ConvertFrom-Json
if (-not $account) {
    throw "No active Azure CLI session. Run 'az login'."
}

$tenantId = $account.tenantId
$apps = Invoke-GraphRest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/applications?`$filter=displayName eq '$DisplayName'"
$application = $apps.value | Select-Object -First 1

if (-not $application) {
    $body = @{
        displayName = $DisplayName
        api = @{
            requestedAccessTokenVersion = 2
        }
    }
    $application = Invoke-GraphRest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/applications" `
        -BodyObject $body
}

if ($application.api.requestedAccessTokenVersion -ne 2) {
    $patch = @{
        api = @{
            requestedAccessTokenVersion = 2
        }
    }
    Invoke-GraphRest -Method PATCH `
        -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" `
        -BodyObject $patch | Out-Null
}

$identifierUri = "api://$($application.appId)"
$role = $application.appRoles |
    Where-Object { $_.value -eq $RoleName } |
    Select-Object -First 1

if (-not $role) {
    $role = @{
        id = [guid]::NewGuid().ToString()
        allowedMemberTypes = @("Application")
        description = "Allows Azure Monitor Action Groups to invoke the secure webhook."
        displayName = $RoleName
        isEnabled = $true
        value = $RoleName
    }
    $appRoles = @($application.appRoles) + $role
    $patch = @{
        identifierUris = @($identifierUri)
        appRoles = $appRoles
    }
    Invoke-GraphRest -Method PATCH `
        -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" `
        -BodyObject $patch | Out-Null
} elseif ($application.identifierUris -notcontains $identifierUri) {
    $patch = @{ identifierUris = @($identifierUri) }
    Invoke-GraphRest -Method PATCH `
        -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" `
        -BodyObject $patch | Out-Null
}

$servicePrincipals = Invoke-GraphRest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$($application.appId)'"
$apiServicePrincipal = $servicePrincipals.value | Select-Object -First 1
if (-not $apiServicePrincipal) {
    $body = @{ appId = $application.appId }
    $apiServicePrincipal = Invoke-GraphRest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/servicePrincipals" `
        -BodyObject $body
}

$aznsPrincipals = Invoke-GraphRest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$AznsApplicationId'"
$aznsPrincipal = $aznsPrincipals.value | Select-Object -First 1
if (-not $aznsPrincipal) {
    $body = @{ appId = $AznsApplicationId }
    $aznsPrincipal = Invoke-GraphRest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/servicePrincipals" `
        -BodyObject $body
}

$assignments = Invoke-GraphRest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($aznsPrincipal.id)/appRoleAssignments"
$assignment = $assignments.value | Where-Object {
    $_.resourceId -eq $apiServicePrincipal.id -and $_.appRoleId -eq $role.id
}
if (-not $assignment) {
    $body = @{
        principalId = $aznsPrincipal.id
        resourceId = $apiServicePrincipal.id
        appRoleId = $role.id
    }
    Invoke-GraphRest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($aznsPrincipal.id)/appRoleAssignments" `
        -BodyObject $body | Out-Null
}

azd env set AZURE_TENANT_ID $tenantId | Out-Null
azd env set SERVICE_HEALTH_API_CLIENT_ID $application.appId | Out-Null
azd env set SERVICE_HEALTH_API_OBJECT_ID $application.id | Out-Null
azd env set SERVICE_HEALTH_API_IDENTIFIER_URI $identifierUri | Out-Null

Write-Host "Secure webhook application is configured for $DisplayName."
