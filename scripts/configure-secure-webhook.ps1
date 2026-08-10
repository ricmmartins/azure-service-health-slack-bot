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

# The deployment caller must be added as an owner of the secure-webhook
# application. Graph /me only works for delegated (interactive user) tokens; when
# azd provision runs under a service principal or federated CI identity it uses
# app-only Graph auth, and /me fails with
# "/me request is only valid with delegated authentication flow". This helper
# inspects the already-loaded 'az account show' object to pick the correct
# resolution path and returns the caller's directory object id:
#   * user            -> Graph /me (delegated token carries the user identity)
#   * servicePrincipal -> resolve the SP directory object by its client/app id
# It requires exactly one match for the service-principal path and fails loudly
# on zero or ambiguous results rather than silently skipping ownership.
function Resolve-CallerOwnerObjectId {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] $Account,
        [Parameter(Mandatory = $true)][scriptblock] $GraphInvoker
    )

    $userType = $null
    if ($Account.PSObject.Properties['user'] -and $Account.user) {
        $userType = $Account.user.type
    }

    # Azure CLI has emitted this as "user"/"servicePrincipal" historically but the
    # exact casing is not contractually guaranteed, so normalize deliberately
    # instead of comparing against a single literal.
    $normalizedType = if ($null -ne $userType) {
        ([string]$userType).Trim().ToLowerInvariant()
    }
    else {
        ""
    }

    switch ($normalizedType) {
        "serviceprincipal" {
            $clientId = $null
            if ($Account.PSObject.Properties['user'] -and $Account.user) {
                $clientId = $Account.user.name
            }
            if ([string]::IsNullOrWhiteSpace($clientId)) {
                throw "Cannot resolve deployment caller: 'az account show' reported a service principal but account.user.name (client id) is empty."
            }

            $escapedClientId = ([string]$clientId).Replace("'", "''")
            $principals = & $GraphInvoker -Method GET `
                -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$escapedClientId'&`$select=id"

            $ids = @()
            if ($principals -and $principals.PSObject.Properties['value'] -and $principals.value) {
                $ids = @(
                    $principals.value |
                        ForEach-Object { $_.id } |
                        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
                        Select-Object -Unique
                )
            }

            if ($ids.Count -eq 0) {
                throw "Cannot add deployment caller as application owner: no service principal found in the directory for client id '$clientId'."
            }
            if ($ids.Count -gt 1) {
                throw "Cannot add deployment caller as application owner: ambiguous service principal resolution for client id '$clientId' ($($ids.Count) matches)."
            }
            return $ids[0]
        }
        "user" {
            $me = & $GraphInvoker -Method GET `
                -Uri "https://graph.microsoft.com/v1.0/me?`$select=id"
            if (-not $me -or [string]::IsNullOrWhiteSpace($me.id)) {
                throw "Cannot resolve deployment caller: Microsoft Graph /me returned no object id for the signed-in user."
            }
            return $me.id
        }
        default {
            throw "Cannot resolve deployment caller: unsupported Azure CLI account user type '$userType'. Expected 'user' or 'servicePrincipal'."
        }
    }
}

if ($MyInvocation.InvocationName -eq ".") {
    return
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it and run 'az login'."
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

$owners = Invoke-GraphRest -Method GET `
    -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/owners?`$select=id"
$callerObjectId = Resolve-CallerOwnerObjectId -Account $account -GraphInvoker {
    param([string] $Method, [string] $Uri)
    Invoke-GraphRest -Method $Method -Uri $Uri
}
$callerOwner = $owners.value | Where-Object { $_.id -eq $callerObjectId }
if (-not $callerOwner) {
    $body = @{
        '@odata.id' = "https://graph.microsoft.com/v1.0/directoryObjects/$callerObjectId"
    }
    Invoke-GraphRest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/owners/`$ref" `
        -BodyObject $body | Out-Null
}

$aznsOwner = $owners.value | Where-Object { $_.id -eq $aznsPrincipal.id }
if (-not $aznsOwner) {
    $body = @{
        '@odata.id' = "https://graph.microsoft.com/v1.0/directoryObjects/$($aznsPrincipal.id)"
    }
    Invoke-GraphRest -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/owners/`$ref" `
        -BodyObject $body | Out-Null
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
