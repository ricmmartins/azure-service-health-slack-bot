[CmdletBinding()]
param(
    [string] $DisplayName = "Azure Service Health Slack Bot - $env:AZURE_ENV_NAME",
    [string] $AznsApplicationId = "461e8683-5575-4561-ac7f-899cc907d62a",
    [string] $RoleName = "ActionGroupsSecureWebhook"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Run 'azd auth login' and install Azure CLI."
}

$account = az account show --output json | ConvertFrom-Json
if (-not $account) {
    throw "No active Azure CLI session. Run 'az login'."
}

$tenantId = $account.tenantId
$apps = az rest --method GET `
    --uri "https://graph.microsoft.com/v1.0/applications?`$filter=displayName eq '$DisplayName'" `
    | ConvertFrom-Json
$application = $apps.value | Select-Object -First 1

if (-not $application) {
    $body = @{
        displayName = $DisplayName
        api = @{
            requestedAccessTokenVersion = 2
        }
    } | ConvertTo-Json -Depth 5
    $application = az rest --method POST `
        --uri "https://graph.microsoft.com/v1.0/applications" `
        --headers "Content-Type=application/json" `
        --body $body | ConvertFrom-Json
}

if ($application.api.requestedAccessTokenVersion -ne 2) {
    $patch = @{
        api = @{
            requestedAccessTokenVersion = 2
        }
    } | ConvertTo-Json -Depth 5
    az rest --method PATCH `
        --uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" `
        --headers "Content-Type=application/json" `
        --body $patch | Out-Null
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
    } | ConvertTo-Json -Depth 10
    az rest --method PATCH `
        --uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" `
        --headers "Content-Type=application/json" `
        --body $patch | Out-Null
} elseif ($application.identifierUris -notcontains $identifierUri) {
    $patch = @{ identifierUris = @($identifierUri) } | ConvertTo-Json
    az rest --method PATCH `
        --uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" `
        --headers "Content-Type=application/json" `
        --body $patch | Out-Null
}

$servicePrincipals = az rest --method GET `
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$($application.appId)'" `
    | ConvertFrom-Json
$apiServicePrincipal = $servicePrincipals.value | Select-Object -First 1
if (-not $apiServicePrincipal) {
    $body = @{ appId = $application.appId } | ConvertTo-Json
    $apiServicePrincipal = az rest --method POST `
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals" `
        --headers "Content-Type=application/json" `
        --body $body | ConvertFrom-Json
}

$aznsPrincipals = az rest --method GET `
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$AznsApplicationId'" `
    | ConvertFrom-Json
$aznsPrincipal = $aznsPrincipals.value | Select-Object -First 1
if (-not $aznsPrincipal) {
    $body = @{ appId = $AznsApplicationId } | ConvertTo-Json
    $aznsPrincipal = az rest --method POST `
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals" `
        --headers "Content-Type=application/json" `
        --body $body | ConvertFrom-Json
}

$assignments = az rest --method GET `
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($aznsPrincipal.id)/appRoleAssignments" `
    | ConvertFrom-Json
$assignment = $assignments.value | Where-Object {
    $_.resourceId -eq $apiServicePrincipal.id -and $_.appRoleId -eq $role.id
}
if (-not $assignment) {
    $body = @{
        principalId = $aznsPrincipal.id
        resourceId = $apiServicePrincipal.id
        appRoleId = $role.id
    } | ConvertTo-Json
    az rest --method POST `
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($aznsPrincipal.id)/appRoleAssignments" `
        --headers "Content-Type=application/json" `
        --body $body | Out-Null
}

azd env set AZURE_TENANT_ID $tenantId | Out-Null
azd env set SERVICE_HEALTH_API_CLIENT_ID $application.appId | Out-Null
azd env set SERVICE_HEALTH_API_OBJECT_ID $application.id | Out-Null
azd env set SERVICE_HEALTH_API_IDENTIFIER_URI $identifierUri | Out-Null

Write-Host "Secure webhook application is configured for $DisplayName."
