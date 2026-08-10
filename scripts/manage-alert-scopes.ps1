<#
.SYNOPSIS
Manages Azure Service Health alert scopes without redeploying the central runtime.

.DESCRIPTION
Discovers one existing Azure Service Health Slack Bot deployment from Azure
resource tags and Secure Webhook metadata, then manages only its peripheral
Activity Log Alert and Action Group resources. All target scopes must belong to
the central Microsoft Entra tenant. The script never reads the Slack token.

.EXAMPLE
./scripts/manage-alert-scopes.ps1 list -EnvironmentName production

.EXAMPLE
./scripts/manage-alert-scopes.ps1 add-subscription -SubscriptionId 00000000-0000-0000-0000-000000000000

.EXAMPLE
./scripts/manage-alert-scopes.ps1 migrate-to-management-group -ManagementGroupId platform -WhatIf
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
$script:AzCommandHandler = $null

$WorkloadTag = "azure-service-health-slack-bot"
$ManagerTag = "manage-alert-scopes"
$AlertTemplatePath = Join-Path $PSScriptRoot "..\infra\day2\service-health-alert-scope.bicep"

function Invoke-AzCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments
    )

    if ($script:AzCommandHandler) {
        return & $script:AzCommandHandler $Arguments
    }

    $azArguments = @($Arguments) + @("--only-show-errors", "--output", "json")
    $raw = & az @azArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Arguments -join ' ')`n$($raw -join "`n")"
    }

    $text = ($raw -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }

    try {
        return $text | ConvertFrom-Json -Depth 100
    }
    catch {
        throw "Azure CLI returned invalid JSON for: az $($Arguments -join ' ')"
    }
}

function Get-MemberValue {
    param(
        [AllowNull()] $Object,
        [Parameter(Mandatory = $true)][string] $Name
    )

    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary]) {
        return $Object[$Name]
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }
    return $null
}

function Get-NestedValue {
    param(
        [AllowNull()] $Object,
        [Parameter(Mandatory = $true)][string[]] $Path
    )

    $value = $Object
    foreach ($segment in $Path) {
        $value = Get-MemberValue -Object $value -Name $segment
        if ($null -eq $value) {
            return $null
        }
    }
    return $value
}

function Get-AzureProperty {
    param(
        [AllowNull()] $Object,
        [Parameter(Mandatory = $true)][string] $Name
    )

    $value = Get-MemberValue -Object $Object -Name $Name
    if ($null -ne $value) {
        return $value
    }
    return Get-MemberValue -Object (Get-MemberValue -Object $Object -Name "properties") -Name $Name
}

function Get-TagValue {
    param(
        [AllowNull()] $Resource,
        [Parameter(Mandatory = $true)][string] $Name
    )

    return Get-MemberValue -Object (Get-MemberValue -Object $Resource -Name "tags") -Name $Name
}

function Assert-AzureCli {
    if ($script:AzCommandHandler) {
        return
    }
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "Azure CLI is required. Install it, run 'az login', and retry."
    }
}

function Get-ResourceCoordinates {
    param([Parameter(Mandatory = $true)][string] $ResourceId)

    if ($ResourceId -notmatch "(?i)^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/.+/([^/]+)$") {
        throw "Unsupported Azure resource ID: $ResourceId"
    }
    return [pscustomobject]@{
        SubscriptionId = $Matches[1]
        ResourceGroup = $Matches[2]
        Name = $Matches[3]
    }
}

function Get-ResourceSuffix {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeKind,
        [Parameter(Mandatory = $true)][string] $ScopeId
    )

    $bytes = [System.Text.Encoding]::UTF8.GetBytes("$ScopeKind|$($ScopeId.ToLowerInvariant())")
    $hash = [System.Security.Cryptography.SHA256]::HashData($bytes)
    $shortHash = ([Convert]::ToHexString($hash)).Substring(0, 12).ToLowerInvariant()
    $prefix = if ($ScopeKind -eq "subscription") { "sub" } else { "mg" }
    return "$prefix-$shortHash"
}

function Test-UnreadableSubscriptionError {
    # Narrowly recognizes the well-defined Azure conditions where a stale or
    # foreign cached subscription (from 'az account list') cannot have its
    # resource groups listed by the caller. Only these expected access/not-found
    # signals are matched. Arbitrary Azure CLI failures, invalid JSON, throttling,
    # and network errors deliberately fall through so discovery still fails closed.
    param([Parameter(Mandatory = $true)] $ErrorRecord)

    $message = if ($ErrorRecord -is [System.Management.Automation.ErrorRecord]) {
        [string]$ErrorRecord.Exception.Message
    }
    else {
        [string]$ErrorRecord
    }
    if ([string]::IsNullOrWhiteSpace($message)) {
        return $false
    }

    return ($message -match "(?i)\bAuthorizationFailed\b") -or
        ($message -match "(?i)\bSubscriptionNotFound\b") -or
        ($message -match "(?i)does not have authorization to perform action '[^']*resourceGroups/read'") -or
        ($message -match "(?i)The subscription '[^']*' could not be found")
}

function Get-CentralDeployment {
    param([string] $RequestedEnvironmentName)

    $currentAccount = Invoke-AzCommand -Arguments @("account", "show")
    $currentTenantId = [string](Get-MemberValue $currentAccount "tenantId")
    if ([string]::IsNullOrWhiteSpace($currentTenantId)) {
        throw "The active Azure CLI context has no tenant. Run 'az login' for the central deployment tenant."
    }
    $accounts = @(Invoke-AzCommand -Arguments @("account", "list"))
    $accounts = @($accounts | Where-Object {
        (Get-MemberValue $_ "state") -eq "Enabled" -and
        (Get-MemberValue $_ "tenantId") -eq $currentTenantId
    })
    if ($accounts.Count -eq 0) {
        throw "No enabled Azure subscriptions are available. Run 'az login' with access to the central deployment."
    }

    $candidates = @()
    foreach ($account in $accounts) {
        $accountId = [string](Get-MemberValue $account "id")
        # The initial resource-group listing is the only place a stale or foreign
        # cached subscription may be skipped. A well-defined unreadable-subscription
        # signal here must not abort discovery of an explicitly requested,
        # accessible central environment; anything else still propagates.
        try {
            $groups = @(Invoke-AzCommand -Arguments @(
                "group", "list", "--subscription", $accountId, "--tag", "workload=$WorkloadTag"
            ))
        }
        catch {
            if (Test-UnreadableSubscriptionError -ErrorRecord $_) {
                Write-Warning "Skipping subscription '$accountId' during central deployment discovery: resource groups are not readable (stale or inaccessible cached subscription)."
                continue
            }
            throw
        }
        foreach ($group in $groups) {
            $environment = [string](Get-TagValue $group "azd-env-name")
            if ([string]::IsNullOrWhiteSpace($environment)) {
                continue
            }
            if ($RequestedEnvironmentName -and $environment -ne $RequestedEnvironmentName) {
                continue
            }

            $groupName = [string](Get-MemberValue $group "name")
            $apps = @(Invoke-AzCommand -Arguments @(
                "resource", "list",
                "--subscription", $accountId,
                "--resource-group", $groupName,
                "--resource-type", "Microsoft.App/containerApps"
            ))
            $app = @($apps | Where-Object {
                (Get-MemberValue $_ "name") -eq "ca-$environment"
            })
            if ($app.Count -ne 1) {
                continue
            }

            $actionGroups = @(Invoke-AzCommand -Arguments @(
                "monitor", "action-group", "list",
                "--subscription", $accountId,
                "--resource-group", $groupName
            ))
            $anchor = @($actionGroups | Where-Object {
                (Get-MemberValue $_ "name") -eq "ag-$environment-service-health"
            })
            if ($anchor.Count -ne 1) {
                continue
            }

            $baselineAlerts = @(Invoke-AzCommand -Arguments @(
                "monitor", "activity-log", "alert", "list",
                "--subscription", $accountId,
                "--resource-group", $groupName
            ))
            $anchorId = [string](Get-MemberValue $anchor[0] "id")
            # Identify the azd-owned baseline alert by its anchor Action Group relationship,
            # not by a guessed name, so protection survives a renamed baseline alert.
            $baseline = @($baselineAlerts | Where-Object {
                $candidateActions = @(Get-NestedValue $_ @("actions", "actionGroups"))
                if ($candidateActions.Count -eq 0) {
                    $candidateActions = @(Get-NestedValue $_ @("properties", "actions", "actionGroups"))
                }
                @($candidateActions | Where-Object {
                    [string](Get-MemberValue $_ "actionGroupId") -eq $anchorId
                }).Count -gt 0
            })
            if ($baseline.Count -eq 0) {
                continue
            }
            if ($baseline.Count -ne 1) {
                throw "Central deployment '$environment' has multiple baseline alerts bound to its anchor Action Group '$anchorId'."
            }
            $baselineActions = @(Get-NestedValue $baseline[0] @("actions", "actionGroups"))
            if ($baselineActions.Count -eq 0) {
                $baselineActions = @(Get-NestedValue $baseline[0] @("properties", "actions", "actionGroups"))
            }
            $baselineScopes = @(Get-AzureProperty -Object $baseline[0] -Name "scopes")
            if ($baselineActions.Count -ne 1 -or
                [string](Get-MemberValue $baselineActions[0] "actionGroupId") -ne $anchorId -or
                $baselineScopes.Count -ne 1) {
                throw "Central deployment '$environment' has inconsistent baseline alert metadata."
            }
            $baselineScopeResourceId = [string]$baselineScopes[0]
            if ($baselineScopeResourceId -match "(?i)^/subscriptions/([^/]+)$") {
                $baselineScopeKind = "subscription"
                $baselineScopeId = $Matches[1]
            }
            elseif ($baselineScopeResourceId -match "(?i)^/providers/Microsoft.Management/managementGroups/([^/]+)$") {
                $baselineScopeKind = "managementGroup"
                $baselineScopeId = $Matches[1]
            }
            else {
                throw "Central deployment '$environment' has unsupported baseline alert scope '$baselineScopeResourceId'."
            }

            $receivers = @(Get-AzureProperty -Object $anchor[0] -Name "webhookReceivers")
            $receiver = @($receivers | Where-Object {
                (Get-MemberValue $_ "useAadAuth") -eq $true -and
                ([string](Get-MemberValue $_ "serviceUri")).TrimEnd("/") -match "/api/service-health$"
            })
            if ($receiver.Count -ne 1) {
                continue
            }

            $appId = [string](Get-MemberValue $app[0] "id")
            $appDetails = Invoke-AzCommand -Arguments @(
                "rest", "--method", "get",
                "--url", "${appId}?api-version=2024-03-01",
                "--subscription", $accountId
            )
            $fqdn = [string](Get-NestedValue $appDetails @("properties", "configuration", "ingress", "fqdn"))
            $webhookUri = ([string](Get-MemberValue $receiver[0] "serviceUri")).TrimEnd("/")
            if ([string]::IsNullOrWhiteSpace($fqdn) -or
                $webhookUri -ne "https://$fqdn/api/service-health") {
                throw "Central deployment '$environment' has inconsistent Container App and Action Group webhook metadata."
            }

            $auth = Invoke-AzCommand -Arguments @(
                "rest", "--method", "get",
                "--url", "$appId/authConfigs/current?api-version=2024-03-01",
                "--subscription", $accountId
            )
            $clientId = [string](Get-NestedValue $auth @(
                "properties", "identityProviders", "azureActiveDirectory", "registration", "clientId"
            ))
            $tenantId = [string](Get-MemberValue $receiver[0] "tenantId")
            $accountTenantId = [string](Get-MemberValue $account "tenantId")
            if ([string]::IsNullOrWhiteSpace($clientId) -or
                [string]::IsNullOrWhiteSpace($tenantId) -or
                $tenantId -ne $accountTenantId) {
                throw "Central deployment '$environment' has incomplete or cross-tenant Secure Webhook metadata."
            }

            $candidates += [pscustomobject]@{
                EnvironmentName = $environment
                TenantId = $tenantId
                SubscriptionId = $accountId
                ResourceGroup = $groupName
                Location = [string](Get-MemberValue $group "location")
                ContainerAppId = $appId
                WebhookUri = $webhookUri
                SecureWebhookClientId = $clientId
                SecureWebhookObjectId = [string](Get-MemberValue $receiver[0] "objectId")
                SecureWebhookIdentifierUri = [string](Get-MemberValue $receiver[0] "identifierUri")
                AnchorActionGroupId = [string](Get-MemberValue $anchor[0] "id")
                ProtectedAlertId = [string](Get-MemberValue $baseline[0] "id")
                ProtectedScopeKind = $baselineScopeKind
                ProtectedScopeId = $baselineScopeId
                ProtectedScopeResourceId = $baselineScopeResourceId
                Accounts = $accounts
            }
        }
    }

    if ($candidates.Count -eq 0) {
        $suffix = if ($RequestedEnvironmentName) { " for environment '$RequestedEnvironmentName'" } else { "" }
        throw "No central Azure Service Health Slack Bot deployment was discovered$suffix. Verify Reader access and deployment tags."
    }
    if ($candidates.Count -gt 1) {
        $names = ($candidates | ForEach-Object {
            "$($_.EnvironmentName) [$($_.SubscriptionId)]"
        }) -join ", "
        throw "Multiple central deployments were discovered ($names). Specify -EnvironmentName."
    }

    $central = $candidates[0]
    foreach ($required in @(
        "SecureWebhookObjectId",
        "SecureWebhookIdentifierUri",
        "SecureWebhookClientId",
        "WebhookUri",
        "ProtectedAlertId",
        "ProtectedScopeKind",
        "ProtectedScopeId"
    )) {
        if ([string]::IsNullOrWhiteSpace([string](Get-MemberValue $central $required))) {
            throw "Central deployment discovery could not prove '$required'. No changes were made."
        }
    }
    return $central
}

function Test-SameWebhookReceiver {
    param(
        [Parameter(Mandatory = $true)] $ActionGroup,
        [Parameter(Mandatory = $true)] $Central
    )

    $receivers = @(Get-AzureProperty -Object $ActionGroup -Name "webhookReceivers")
    foreach ($receiver in $receivers) {
        if (([string](Get-MemberValue $receiver "serviceUri")).TrimEnd("/") -eq $Central.WebhookUri -and
            [string](Get-MemberValue $receiver "tenantId") -eq $Central.TenantId -and
            [string](Get-MemberValue $receiver "objectId") -eq $Central.SecureWebhookObjectId -and
            [string](Get-MemberValue $receiver "identifierUri") -eq $Central.SecureWebhookIdentifierUri -and
            (Get-MemberValue $receiver "useAadAuth") -eq $true -and
            (Get-MemberValue $receiver "useCommonAlertSchema") -eq $true) {
            return $true
        }
    }
    return $false
}

function Get-ManagedScopes {
    param([Parameter(Mandatory = $true)] $Central)

    $scopes = @()
    $referencedActionGroupIds = @()
    $managedActionGroups = @()
    $tenantAccounts = @($Central.Accounts | Where-Object {
        (Get-MemberValue $_ "tenantId") -eq $Central.TenantId -and
        (Get-MemberValue $_ "state") -eq "Enabled"
    })

    foreach ($account in $tenantAccounts) {
        $accountId = [string](Get-MemberValue $account "id")
        $alerts = @(Invoke-AzCommand -Arguments @(
            "monitor", "activity-log", "alert", "list", "--subscription", $accountId
        ))
        foreach ($alert in $alerts) {
            if ((Get-TagValue $alert "workload") -ne $WorkloadTag -or
                (Get-TagValue $alert "azd-env-name") -ne $Central.EnvironmentName -or
                (Get-TagValue $alert "service-health-managed-by") -ne $ManagerTag) {
                continue
            }
            $alertId = [string](Get-MemberValue $alert "id")
            if ($alertId -eq $Central.ProtectedAlertId) {
                continue
            }

            $alertScopes = @(Get-AzureProperty -Object $alert -Name "scopes")
            if ($alertScopes.Count -ne 1) {
                throw "Alert '$([string](Get-MemberValue $alert 'id'))' has an ambiguous scope configuration."
            }

            $actions = @(Get-NestedValue $alert @("actions", "actionGroups"))
            if ($actions.Count -eq 0) {
                $actions = @(Get-NestedValue $alert @("properties", "actions", "actionGroups"))
            }
            if ($actions.Count -ne 1) {
                throw "Alert '$([string](Get-MemberValue $alert 'id'))' does not have exactly one Action Group."
            }
            $actionGroupId = [string](Get-MemberValue $actions[0] "actionGroupId")
            if ($actionGroupId -eq $Central.AnchorActionGroupId) {
                continue
            }
            $actionGroup = Invoke-AzCommand -Arguments @(
                "resource", "show", "--ids", $actionGroupId, "--api-version", "2023-01-01"
            )
            if (-not (Test-SameWebhookReceiver -ActionGroup $actionGroup -Central $Central)) {
                throw "Managed Action Group '$actionGroupId' does not match the central signed Common Alert Schema receiver."
            }
            $condition = Get-AzureProperty -Object $alert -Name "condition"
            $conditionAllOf = @(Get-MemberValue $condition "allOf")
            $serviceHealthConditions = @($conditionAllOf | Where-Object {
                [string](Get-MemberValue $_ "field") -eq "category" -and
                [string](Get-MemberValue $_ "equals") -eq "ServiceHealth"
            })
            if ($conditionAllOf.Count -ne 1 -or $serviceHealthConditions.Count -ne 1) {
                throw "Alert '$([string](Get-MemberValue $alert 'id'))' is not an unrestricted Service Health category rule."
            }

            $scopeResourceId = [string]$alertScopes[0]
            if ($scopeResourceId -notmatch "(?i)^/subscriptions/([^/]+)$") {
                throw "Alert '$([string](Get-MemberValue $alert 'id'))' must use a subscription scope. Azure Activity Log Alerts do not support Management Group descendant fan-out natively."
            }
            $memberSubscriptionId = $Matches[1]
            $kind = [string](Get-TagValue $alert "service-health-scope-kind")
            $scopeId = [string](Get-TagValue $alert "service-health-scope-id")
            $taggedMemberId = [string](Get-TagValue $alert "service-health-member-subscription")
            if ($kind -notin @("subscription", "managementGroup") -or
                [string]::IsNullOrWhiteSpace($scopeId) -or
                ($taggedMemberId -and $taggedMemberId -ne $memberSubscriptionId) -or
                ($kind -eq "subscription" -and $scopeId -ne $memberSubscriptionId)) {
                throw "Alert '$([string](Get-MemberValue $alert 'id'))' has inconsistent day-2 ownership metadata."
            }

            $scopes += [pscustomobject]@{
                ScopeKind = $kind
                ScopeId = $scopeId
                ScopeResourceId = $scopeResourceId
                AlertId = [string](Get-MemberValue $alert "id")
                ActionGroupId = $actionGroupId
                Enabled = [bool](Get-AzureProperty -Object $alert -Name "enabled")
                ActionGroupEnabled = [bool](Get-AzureProperty -Object $actionGroup -Name "enabled")
                TenantId = $Central.TenantId
                ManagedBy = [string](Get-TagValue $alert "service-health-managed-by")
                MemberSubscriptionId = $memberSubscriptionId
                OrphanedActionGroup = $false
            }
            $referencedActionGroupIds += $actionGroupId
        }

        $actionGroups = @(Invoke-AzCommand -Arguments @(
            "monitor", "action-group", "list", "--subscription", $accountId
        ))
        foreach ($actionGroup in $actionGroups) {
            if ((Get-TagValue $actionGroup "workload") -eq $WorkloadTag -and
                (Get-TagValue $actionGroup "azd-env-name") -eq $Central.EnvironmentName -and
                (Get-TagValue $actionGroup "service-health-managed-by") -eq $ManagerTag) {
                $managedActionGroups += $actionGroup
            }
        }
    }
    foreach ($actionGroup in $managedActionGroups) {
        $actionGroupId = [string](Get-MemberValue $actionGroup "id")
        if ($actionGroupId -eq $Central.AnchorActionGroupId -or
            $referencedActionGroupIds -contains $actionGroupId) {
            continue
        }
        $kind = [string](Get-TagValue $actionGroup "service-health-scope-kind")
        $scopeId = [string](Get-TagValue $actionGroup "service-health-scope-id")
        $memberSubscriptionId = [string](Get-TagValue $actionGroup "service-health-member-subscription")
        if ([string]::IsNullOrWhiteSpace($memberSubscriptionId) -and
            $actionGroupId -match "(?i)^/subscriptions/([^/]+)/") {
            $memberSubscriptionId = $Matches[1]
        }
        if ($kind -notin @("subscription", "managementGroup") -or
            [string]::IsNullOrWhiteSpace($scopeId) -or
            ($kind -eq "subscription" -and $scopeId -ne $memberSubscriptionId) -or
            $actionGroupId -notmatch "(?i)^/subscriptions/$([regex]::Escape($memberSubscriptionId))/") {
            throw "Orphaned manager-owned Action Group '$actionGroupId' has inconsistent ownership metadata."
        }
        $scopes += [pscustomobject]@{
            ScopeKind = $kind
            ScopeId = $scopeId
            ScopeResourceId = "/subscriptions/$memberSubscriptionId"
            AlertId = $null
            ActionGroupId = $actionGroupId
            Enabled = $false
            ActionGroupEnabled = [bool](Get-AzureProperty -Object $actionGroup -Name "enabled")
            TenantId = $Central.TenantId
            ManagedBy = $ManagerTag
            MemberSubscriptionId = $memberSubscriptionId
            OrphanedActionGroup = $true
        }
    }
    $result = @($scopes | Where-Object ScopeKind -eq "subscription")
    foreach ($group in @($scopes | Where-Object ScopeKind -eq "managementGroup" |
        Group-Object ScopeId)) {
        $members = @($group.Group)
        $operationalMembers = @($members | Where-Object {
            -not [bool](Get-MemberValue $_ "OrphanedActionGroup")
        })
        $memberIds = @($operationalMembers.MemberSubscriptionId)
        $uniqueMemberIds = @($memberIds | Sort-Object -Unique)
        $result += [pscustomobject]@{
            ScopeKind = "managementGroup"
            ScopeId = $group.Name
            ScopeResourceId = "/providers/Microsoft.Management/managementGroups/$($group.Name)"
            AlertId = @($operationalMembers.AlertId)
            ActionGroupId = @($members.ActionGroupId)
            Enabled = $operationalMembers.Count -gt 0 -and
                @($operationalMembers | Where-Object { -not $_.Enabled }).Count -eq 0
            ActionGroupEnabled = $operationalMembers.Count -gt 0 -and
                @($operationalMembers | Where-Object {
                    -not $_.ActionGroupEnabled
                }).Count -eq 0
            TenantId = $Central.TenantId
            ManagedBy = $ManagerTag
            MemberSubscriptionIds = $uniqueMemberIds
            Members = $members
        }
    }
    return $result
}

function Get-ManagementGroupCoverage {
    param(
        [Parameter(Mandatory = $true)][string] $ManagementGroupId,
        [Parameter(Mandatory = $true)] $Context
    )

    $key = $ManagementGroupId.ToLowerInvariant()
    if ($Context.ManagementGroupCache.ContainsKey($key)) {
        return $Context.ManagementGroupCache[$key]
    }

    $encodedId = [uri]::EscapeDataString($ManagementGroupId)
    $group = Invoke-AzCommand -Arguments @(
        "rest", "--method", "get",
        "--url", "https://management.azure.com/providers/Microsoft.Management/managementGroups/${encodedId}?api-version=2021-04-01",
        "--subscription", $Context.Central.SubscriptionId
    )
    $tenantId = [string](Get-AzureProperty -Object $group -Name "tenantId")
    if ([string]::IsNullOrWhiteSpace($tenantId) -or $tenantId -ne $Context.Central.TenantId) {
        throw "Management Group '$ManagementGroupId' is not proven to belong to central tenant '$($Context.Central.TenantId)'."
    }

    $subscriptionIds = @()
    $managementGroupIds = @()
    $nextUrl = "https://management.azure.com/providers/Microsoft.Management/managementGroups/$encodedId/descendants?api-version=2020-05-01"
    while ($nextUrl) {
        $descendants = Invoke-AzCommand -Arguments @(
            "rest", "--method", "get",
            "--url", $nextUrl,
            "--subscription", $Context.Central.SubscriptionId
        )
        foreach ($item in @(Get-MemberValue $descendants "value")) {
            $type = [string](Get-MemberValue $item "type")
            if ($type -match "(?i)/managementGroups$") {
                $descendantGroupId = [string](Get-MemberValue $item "name")
                if (-not [string]::IsNullOrWhiteSpace($descendantGroupId) -and
                    $descendantGroupId -ne $ManagementGroupId) {
                    $managementGroupIds += $descendantGroupId
                }
                continue
            }
            if ($type -notmatch "(?i)/subscriptions$") {
                continue
            }
            $itemId = [string](Get-MemberValue $item "name")
            if ([string]::IsNullOrWhiteSpace($itemId)) {
                $resourceId = [string](Get-MemberValue $item "id")
                if ($resourceId -match "(?i)/subscriptions/([^/]+)$") {
                    $itemId = $Matches[1]
                }
            }
            if (-not [string]::IsNullOrWhiteSpace($itemId)) {
                $subscriptionIds += $itemId
            }
        }
        $nextUrl = [string](Get-MemberValue $descendants "nextLink")
    }
    $subscriptionIds = @($subscriptionIds | Sort-Object -Unique)
    $managementGroupIds = @($managementGroupIds | Sort-Object -Unique)

    $knownAccountIds = @($Context.Central.Accounts | Where-Object {
        (Get-MemberValue $_ "tenantId") -eq $Context.Central.TenantId -and
        (Get-MemberValue $_ "state") -eq "Enabled"
    } | ForEach-Object { [string](Get-MemberValue $_ "id") })
    $inaccessible = @($subscriptionIds | Where-Object { $knownAccountIds -notcontains $_ })
    if ($inaccessible.Count -gt 0) {
        throw "Coverage for Management Group '$ManagementGroupId' cannot be proven. These descendant subscriptions are not accessible: $($inaccessible -join ', ')."
    }

    $coverage = [pscustomobject]@{
        ManagementGroupId = $ManagementGroupId
        TenantId = $tenantId
        SubscriptionIds = $subscriptionIds
        DescendantManagementGroupIds = $managementGroupIds
    }
    $Context.ManagementGroupCache[$key] = $coverage
    return $coverage
}

function Test-PermissionPattern {
    param(
        [Parameter(Mandatory = $true)][string] $Pattern,
        [Parameter(Mandatory = $true)][string] $Operation
    )

    $regex = "^$([regex]::Escape($Pattern).Replace('\*', '.*'))$"
    return $Operation -match $regex
}

function Assert-AzurePermissions {
    param(
        [Parameter(Mandatory = $true)][string] $Scope,
        [Parameter(Mandatory = $true)][string] $SubscriptionId,
        [Parameter(Mandatory = $true)][string[]] $Operations
    )

    $permissions = Invoke-AzCommand -Arguments @(
        "rest", "--method", "get",
        "--url", "https://management.azure.com$Scope/providers/Microsoft.Authorization/permissions?api-version=2022-04-01",
        "--subscription", $SubscriptionId
    )
    $sets = @(Get-MemberValue $permissions "value")
    if ($sets.Count -eq 0) {
        throw "Azure returned no effective permissions for '$Scope'. No changes were made."
    }

    $missing = @()
    foreach ($operation in $Operations) {
        $allowed = $false
        foreach ($permissionSet in $sets) {
            $actions = @(Get-MemberValue $permissionSet "actions")
            $notActions = @(Get-MemberValue $permissionSet "notActions")
            $included = @($actions | Where-Object {
                Test-PermissionPattern -Pattern ([string]$_) -Operation $operation
            }).Count -gt 0
            $excluded = @($notActions | Where-Object {
                Test-PermissionPattern -Pattern ([string]$_) -Operation $operation
            }).Count -gt 0
            if ($included -and -not $excluded) {
                $allowed = $true
                break
            }
        }
        if (-not $allowed) {
            $missing += $operation
        }
    }
    if ($missing.Count -gt 0) {
        throw "Missing Azure permissions at '$Scope': $($missing -join ', '). Assign Contributor for the target subscription resource group and Monitoring Contributor for Management Group alert scope, then retry."
    }
}

function Assert-AddPermissions {
    param(
        [Parameter(Mandatory = $true)][string] $DeploymentSubscriptionId,
        [string] $ManagementGroupId,
        [Parameter(Mandatory = $true)] $Context
    )

    $subscriptionScope = "/subscriptions/$DeploymentSubscriptionId"
    Assert-AzurePermissions -Scope $subscriptionScope -SubscriptionId $DeploymentSubscriptionId -Operations @(
        "Microsoft.Resources/subscriptions/resourceGroups/write",
        "Microsoft.Resources/deployments/write",
        "Microsoft.Insights/actionGroups/write",
        "Microsoft.Insights/activityLogAlerts/write",
        "Microsoft.Insights/CreateNotifications/Write",
        "Microsoft.Insights/NotificationStatus/Read"
    )
    if ($ManagementGroupId) {
        Assert-AzurePermissions `
            -Scope "/providers/Microsoft.Management/managementGroups/$ManagementGroupId" `
            -SubscriptionId $Context.Central.SubscriptionId `
            -Operations @("Microsoft.Insights/activityLogAlerts/write")
    }
}

function Get-ScopeMembers {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [switch] $IncludeOrphans
    )

    $members = @(Get-MemberValue $ScopeState "Members" | Where-Object { $null -ne $_ })
    if ($members.Count -eq 0) {
        $members = @($ScopeState)
    }
    if (-not $IncludeOrphans) {
        $members = @($members | Where-Object {
            -not [bool](Get-MemberValue $_ "OrphanedActionGroup") -and
            -not [string]::IsNullOrWhiteSpace([string](Get-MemberValue $_ "AlertId"))
        })
    }
    return $members
}

function Get-ManagementGroupMembershipState {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)] $Coverage
    )

    $members = @(Get-ScopeMembers -ScopeState $ScopeState -IncludeOrphans)
    $operationalMembers = @(Get-ScopeMembers -ScopeState $ScopeState)
    $memberIds = @($operationalMembers | ForEach-Object {
        [string](Get-MemberValue $_ "MemberSubscriptionId")
    })
    $blankIds = @($memberIds | Where-Object { [string]::IsNullOrWhiteSpace($_) })
    $uniqueMemberIds = @($memberIds | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_)
    } | Sort-Object -Unique)
    $expectedIds = @($Coverage.SubscriptionIds | Sort-Object -Unique)
    $missingIds = @($expectedIds | Where-Object { $uniqueMemberIds -notcontains $_ })
    $unexpectedIds = @($uniqueMemberIds | Where-Object { $expectedIds -notcontains $_ })
    $hasDuplicates = $memberIds.Count -ne $uniqueMemberIds.Count
    $orphanedIds = @($members | Where-Object {
        [bool](Get-MemberValue $_ "OrphanedActionGroup") -or
        [string]::IsNullOrWhiteSpace([string](Get-MemberValue $_ "AlertId"))
    } | ForEach-Object {
        [string](Get-MemberValue $_ "MemberSubscriptionId")
    } | Sort-Object -Unique)

    return [pscustomobject]@{
        Complete = (
            $blankIds.Count -eq 0 -and
            -not $hasDuplicates -and
            $missingIds.Count -eq 0 -and
            $unexpectedIds.Count -eq 0
        )
        MemberIds = $uniqueMemberIds
        ExpectedIds = $expectedIds
        MissingIds = $missingIds
        UnexpectedIds = $unexpectedIds
        HasDuplicates = $hasDuplicates
        OrphanedIds = $orphanedIds
        RepairIds = $missingIds
    }
}

function Assert-ManagementGroupMembershipComplete {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)] $Coverage,
        [string] $Operation = "use Management Group coverage"
    )

    $state = Get-ManagementGroupMembershipState `
        -ScopeState $ScopeState `
        -Coverage $Coverage
    if (-not $state.Complete) {
        $details = @()
        if ($state.MissingIds.Count -gt 0) {
            $details += "missing: $($state.MissingIds -join ', ')"
        }
        if ($state.UnexpectedIds.Count -gt 0) {
            $details += "unexpected: $($state.UnexpectedIds -join ', ')"
        }
        if ($state.HasDuplicates) {
            $details += "duplicate or blank member IDs"
        }
        if ($state.OrphanedIds.Count -gt 0) {
            $details += "orphaned Action Groups: $($state.OrphanedIds -join ', ')"
        }
        throw "Cannot $Operation because Management Group '$($ScopeState.ScopeId)' does not have an exact alert member for every current descendant ($($details -join '; '))."
    }
    return $state
}

function Assert-RemovePermissions {
    param([Parameter(Mandatory = $true)] $ScopeState)

    foreach ($member in @(Get-ScopeMembers -ScopeState $ScopeState -IncludeOrphans)) {
        $resourceId = if ([string]::IsNullOrWhiteSpace([string]$member.AlertId)) {
            $member.ActionGroupId
        } else {
            $member.AlertId
        }
        $coordinates = Get-ResourceCoordinates -ResourceId $resourceId
        $operations = @("Microsoft.Insights/actionGroups/delete")
        if (-not [string]::IsNullOrWhiteSpace([string]$member.AlertId)) {
            $operations += "Microsoft.Insights/activityLogAlerts/delete"
        }
        Assert-AzurePermissions `
            -Scope "/subscriptions/$($coordinates.SubscriptionId)/resourceGroups/$($coordinates.ResourceGroup)" `
            -SubscriptionId $coordinates.SubscriptionId `
            -Operations $operations
    }
}

function Invoke-OfficialWebhookTest {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)] $Context
    )

    foreach ($member in @(Get-ScopeMembers -ScopeState $ScopeState)) {
        $coordinates = Get-ResourceCoordinates -ResourceId $member.ActionGroupId
        $result = Invoke-AzCommand -Arguments @(
            "monitor", "action-group", "test-notifications", "create",
            "--subscription", $coordinates.SubscriptionId,
            "--resource-group", $coordinates.ResourceGroup,
            "--action-group-name", $coordinates.Name,
            "--alert-type", "servicehealth",
            "--add-action", "webhook", "slack-service-health",
            $Context.Central.WebhookUri,
            "useaadauth",
            $Context.Central.SecureWebhookObjectId,
            $Context.Central.SecureWebhookIdentifierUri,
            "usecommonalertschema"
        )
        $state = [string](Get-AzureProperty -Object $result -Name "state")
        if ($state -ne "Complete") {
            throw "Official signed Secure Webhook test did not complete successfully for subscription '$($member.MemberSubscriptionId)' (state: '$state'). The new alert remains disabled."
        }
        $actionDetails = @(Get-AzureProperty -Object $result -Name "actionDetails")
        $secureWebhookDetails = @($actionDetails | Where-Object {
            [string](Get-MemberValue $_ "Name") -eq "slack-service-health" -and
            [string](Get-MemberValue $_ "MechanismType") -eq "SecureWebhook"
        })
        if ($secureWebhookDetails.Count -ne 1) {
            throw "Official signed Secure Webhook test did not return exactly one result for 'slack-service-health'. The new alert remains disabled."
        }
        $receiverStatus = [string](Get-MemberValue $secureWebhookDetails[0] "Status")
        if ($receiverStatus -ne "Succeeded") {
            $detail = [string](Get-MemberValue $secureWebhookDetails[0] "Detail")
            throw "Official signed Secure Webhook receiver test failed for subscription '$($member.MemberSubscriptionId)' (status: '$receiverStatus'; detail: '$detail'). The new alert remains disabled."
        }
    }
    return "Complete"
}

function Assert-DeployedScopeState {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)] $Context
    )

    foreach ($member in @(Get-ScopeMembers -ScopeState $ScopeState)) {
        $alert = Invoke-AzCommand -Arguments @(
            "resource", "show",
            "--ids", $member.AlertId,
            "--api-version", "2020-10-01"
        )
        $scopes = @(Get-AzureProperty -Object $alert -Name "scopes")
        $categoryConditions = @(Get-NestedValue $alert @("properties", "condition", "allOf") |
            Where-Object {
                [string](Get-MemberValue $_ "field") -eq "category" -and
                [string](Get-MemberValue $_ "equals") -eq "ServiceHealth"
            })
        $actionGroups = @(Get-NestedValue $alert @("properties", "actions", "actionGroups"))
        if ([bool](Get-AzureProperty -Object $alert -Name "enabled") -or
            $scopes.Count -ne 1 -or
            [string]$scopes[0] -ne $member.ScopeResourceId -or
            $categoryConditions.Count -ne 1 -or
            $actionGroups.Count -ne 1 -or
            [string](Get-MemberValue $actionGroups[0] "actionGroupId") -ne $member.ActionGroupId) {
            throw "Deployed alert '$($member.AlertId)' does not match the expected disabled Service Health rule. It was not enabled."
        }
    }
}

function Set-AlertEnabled {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)][bool] $Enabled
    )

    $value = if ($Enabled) { "true" } else { "false" }
    $attemptedMembers = @()
    try {
        foreach ($member in @(Get-ScopeMembers -ScopeState $ScopeState)) {
            $originalEnabled = [bool]$member.Enabled
            $attempt = [pscustomobject]@{
                Member = $member
                OriginalEnabled = $originalEnabled
            }
            $attemptedMembers += $attempt
            $updated = Invoke-AzCommand -Arguments @(
                "resource", "update",
                "--ids", $member.AlertId,
                "--api-version", "2020-10-01",
                "--set", "properties.enabled=$value"
            )
            if ([bool](Get-AzureProperty -Object $updated -Name "enabled") -ne $Enabled) {
                throw "Azure did not confirm enabled=$Enabled for '$($member.AlertId)'."
            }
            $member.Enabled = $Enabled
        }
    }
    catch {
        $updateError = $_.Exception.Message
        $rollbackErrors = @()
        $rollbackChanges = @($attemptedMembers)
        [array]::Reverse($rollbackChanges)
        foreach ($change in $rollbackChanges) {
            $rollbackValue = if ($change.OriginalEnabled) { "true" } else { "false" }
            try {
                $rolledBack = Invoke-AzCommand -Arguments @(
                    "resource", "update",
                    "--ids", $change.Member.AlertId,
                    "--api-version", "2020-10-01",
                    "--set", "properties.enabled=$rollbackValue"
                )
                if ([bool](Get-AzureProperty -Object $rolledBack -Name "enabled") -ne
                    $change.OriginalEnabled) {
                    throw "Azure did not confirm the rollback."
                }
                $change.Member.Enabled = $change.OriginalEnabled
            }
            catch {
                $rollbackErrors += "$($change.Member.AlertId): $($_.Exception.Message)"
            }
        }
        if ($rollbackErrors.Count -gt 0) {
            throw "Alert state update failed and rollback was incomplete. Manual intervention is required. Update error: $updateError Rollback errors: $($rollbackErrors -join '; ')"
        }
        throw "Alert state update failed; previously updated members were rolled back. $updateError"
    }
    $ScopeState.Enabled = $Enabled
}

function Get-CurrentAlertEnabled {
    param([Parameter(Mandatory = $true)] $ScopeMember)

    $alert = Invoke-AzCommand -Arguments @(
        "resource", "show",
        "--ids", $ScopeMember.AlertId,
        "--api-version", "2020-10-01"
    )
    $enabled = Get-AzureProperty -Object $alert -Name "enabled"
    if ($null -eq $enabled -or $enabled -isnot [bool]) {
        throw "Azure did not return a boolean enabled state for '$($ScopeMember.AlertId)'."
    }
    $ScopeMember.Enabled = [bool]$enabled
    return [bool]$enabled
}

function Get-CurrentActionGroupEnabled {
    param([Parameter(Mandatory = $true)] $ScopeMember)

    $actionGroup = Invoke-AzCommand -Arguments @(
        "resource", "show",
        "--ids", $ScopeMember.ActionGroupId,
        "--api-version", "2023-01-01"
    )
    $enabled = Get-AzureProperty -Object $actionGroup -Name "enabled"
    if ($null -eq $enabled -or $enabled -isnot [bool]) {
        throw "Azure did not return a boolean enabled state for '$($ScopeMember.ActionGroupId)'."
    }
    $ScopeMember.ActionGroupEnabled = [bool]$enabled
    return [bool]$enabled
}

function Set-ActionGroupEnabled {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)][bool] $Enabled
    )

    $value = if ($Enabled) { "true" } else { "false" }
    $attemptedMembers = @()
    try {
        foreach ($member in @(Get-ScopeMembers -ScopeState $ScopeState)) {
            $originalEnabled = [bool]$member.ActionGroupEnabled
            $attemptedMembers += [pscustomobject]@{
                Member = $member
                OriginalEnabled = $originalEnabled
            }
            $updated = Invoke-AzCommand -Arguments @(
                "resource", "update",
                "--ids", $member.ActionGroupId,
                "--api-version", "2023-01-01",
                "--set", "properties.enabled=$value"
            )
            if ([bool](Get-AzureProperty -Object $updated -Name "enabled") -ne $Enabled) {
                throw "Azure did not confirm enabled=$Enabled for '$($member.ActionGroupId)'."
            }
            $member.ActionGroupEnabled = $Enabled
        }
    }
    catch {
        $updateError = $_.Exception.Message
        $rollbackErrors = @()
        $rollbackChanges = @($attemptedMembers)
        [array]::Reverse($rollbackChanges)
        foreach ($change in $rollbackChanges) {
            $rollbackValue = if ($change.OriginalEnabled) { "true" } else { "false" }
            try {
                $rolledBack = Invoke-AzCommand -Arguments @(
                    "resource", "update",
                    "--ids", $change.Member.ActionGroupId,
                    "--api-version", "2023-01-01",
                    "--set", "properties.enabled=$rollbackValue"
                )
                if ([bool](Get-AzureProperty -Object $rolledBack -Name "enabled") -ne
                    $change.OriginalEnabled) {
                    throw "Azure did not confirm the rollback."
                }
                $change.Member.ActionGroupEnabled = $change.OriginalEnabled
            }
            catch {
                $rollbackErrors += "$($change.Member.ActionGroupId): $($_.Exception.Message)"
            }
        }
        if ($rollbackErrors.Count -gt 0) {
            throw "Action Group state update failed and rollback was incomplete. Manual intervention is required. Update error: $updateError Rollback errors: $($rollbackErrors -join '; ')"
        }
        throw "Action Group state update failed; previously attempted members were restored. $updateError"
    }
    $ScopeState.ActionGroupEnabled = $Enabled
}

function Test-ScopeActive {
    param([Parameter(Mandatory = $true)] $ScopeState)

    return [bool]$ScopeState.Enabled -and [bool]$ScopeState.ActionGroupEnabled
}

function New-Day2ScopeMember {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeKind,
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)][string] $TargetSubscriptionId,
        [Parameter(Mandatory = $true)] $Context
    )

    $suffix = Get-ResourceSuffix -ScopeKind $ScopeKind -ScopeId $ScopeId
    $deployment = Invoke-AzCommand -Arguments @(
        "deployment", "sub", "create",
        "--subscription", $TargetSubscriptionId,
        "--name", "service-health-$suffix",
        "--location", $Context.Central.Location,
        "--template-file", (Resolve-Path $AlertTemplatePath),
        "--parameters",
        "environmentName=$($Context.Central.EnvironmentName)",
        "location=$($Context.Central.Location)",
        "webhookUri=$($Context.Central.WebhookUri)",
        "secureWebhookObjectId=$($Context.Central.SecureWebhookObjectId)",
        "secureWebhookIdentifierUri=$($Context.Central.SecureWebhookIdentifierUri)",
        "tenantId=$($Context.Central.TenantId)",
        "scopeKind=$ScopeKind",
        "scopeId=$ScopeId",
        "targetSubscriptionId=$TargetSubscriptionId",
        "centralSubscriptionId=$($Context.Central.SubscriptionId)",
        "resourceSuffix=$suffix",
        "alertEnabled=false"
    )
    $outputs = Get-NestedValue $deployment @("properties", "outputs")
    foreach ($required in @("actionGroupId", "activityLogAlertId")) {
        if ([string]::IsNullOrWhiteSpace([string](Get-NestedValue $outputs @($required, "value")))) {
            throw "Day-2 deployment did not return '$required'."
        }
    }

    $scopeState = [pscustomobject]@{
        ScopeKind = $ScopeKind
        ScopeId = $ScopeId
        ScopeResourceId = "/subscriptions/$TargetSubscriptionId"
        AlertId = [string](Get-NestedValue $outputs @("activityLogAlertId", "value"))
        ActionGroupId = [string](Get-NestedValue $outputs @("actionGroupId", "value"))
        Enabled = $false
        ActionGroupEnabled = $true
        TenantId = $Context.Central.TenantId
        ManagedBy = $ManagerTag
        MemberSubscriptionId = $TargetSubscriptionId
    }
    Assert-DeployedScopeState -ScopeState $scopeState -Context $Context
    return $scopeState
}

function New-ManagementGroupScopeState {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)][object[]] $Members,
        [Parameter(Mandatory = $true)] $Context
    )

    $memberIds = @($Members.MemberSubscriptionId)
    if ($memberIds.Count -ne @($memberIds | Sort-Object -Unique).Count) {
        throw "Management Group '$ScopeId' has duplicate alert members."
    }
    return [pscustomobject]@{
        ScopeKind = "managementGroup"
        ScopeId = $ScopeId
        ScopeResourceId = "/providers/Microsoft.Management/managementGroups/$ScopeId"
        AlertId = @($Members.AlertId)
        ActionGroupId = @($Members.ActionGroupId)
        Enabled = @($Members | Where-Object { -not $_.Enabled }).Count -eq 0
        ActionGroupEnabled = @($Members | Where-Object {
            -not $_.ActionGroupEnabled
        }).Count -eq 0
        TenantId = $Context.Central.TenantId
        ManagedBy = $ManagerTag
        MemberSubscriptionIds = @($memberIds | Sort-Object)
        Members = $Members
    }
}

function New-Day2ScopeResources {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeKind,
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)] $Context
    )

    if ($ScopeKind -eq "subscription") {
        return New-Day2ScopeMember `
            -ScopeKind $ScopeKind `
            -ScopeId $ScopeId `
            -TargetSubscriptionId $ScopeId `
            -Context $Context
    }

    $coverage = Get-ManagementGroupCoverage -ManagementGroupId $ScopeId -Context $Context
    if ($coverage.SubscriptionIds.Count -eq 0) {
        throw "Management Group '$ScopeId' has no descendant subscriptions to cover."
    }
    $members = @($coverage.SubscriptionIds | ForEach-Object {
        New-Day2ScopeMember `
            -ScopeKind "managementGroup" `
            -ScopeId $ScopeId `
            -TargetSubscriptionId $_ `
            -Context $Context
    })
    return New-ManagementGroupScopeState `
        -ScopeId $ScopeId `
        -Members $members `
        -Context $Context
}

function Test-SubscriptionTenant {
    param(
        [Parameter(Mandatory = $true)][string] $SubscriptionId,
        [Parameter(Mandatory = $true)] $Context
    )

    $account = Invoke-AzCommand -Arguments @("account", "show", "--subscription", $SubscriptionId)
    if ((Get-MemberValue $account "state") -ne "Enabled") {
        throw "Subscription '$SubscriptionId' is not enabled."
    }
    $tenantId = [string](Get-MemberValue $account "tenantId")
    if ($tenantId -ne $Context.Central.TenantId) {
        throw "Subscription '$SubscriptionId' belongs to tenant '$tenantId', not central tenant '$($Context.Central.TenantId)'. Multi-tenant scope management is not supported."
    }
    return $account
}

function Get-OverlapsForManagementGroup {
    param(
        [Parameter(Mandatory = $true)] $Coverage,
        [Parameter(Mandatory = $true)] $Context,
        [string] $ExcludeManagementGroupId
    )

    $overlaps = @($Context.Scopes | Where-Object {
        (Test-ScopeActive $_) -and $_.ScopeKind -eq "subscription" -and
        $Coverage.SubscriptionIds -contains $_.ScopeId
    })
    foreach ($otherManagementGroup in @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "managementGroup" -and
        $_.ScopeId -ne $ExcludeManagementGroupId
    })) {
        $otherCoverage = Get-ManagementGroupCoverage `
            -ManagementGroupId $otherManagementGroup.ScopeId `
            -Context $Context
        $sharesSubscriptions = @($Coverage.SubscriptionIds | Where-Object {
            $otherCoverage.SubscriptionIds -contains $_
        }).Count -gt 0
        $isNested = $Coverage.DescendantManagementGroupIds -contains $otherManagementGroup.ScopeId -or
            $otherCoverage.DescendantManagementGroupIds -contains $Coverage.ManagementGroupId
        if ($sharesSubscriptions -or $isNested) {
            Assert-ManagementGroupMembershipComplete `
                -ScopeState $otherManagementGroup `
                -Coverage $otherCoverage `
                -Operation "evaluate overlapping scope changes" | Out-Null
            $overlaps += $otherManagementGroup
        }
    }
    return $overlaps
}

function Test-SubscriptionCoveredByManagementGroup {
    param(
        [Parameter(Mandatory = $true)][string] $SubscriptionId,
        [Parameter(Mandatory = $true)] $Context,
        [string] $ExcludeManagementGroupId
    )

    foreach ($scope in @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "managementGroup" -and
        $_.ScopeId -ne $ExcludeManagementGroupId
    })) {
        $coverage = Get-ManagementGroupCoverage -ManagementGroupId $scope.ScopeId -Context $Context
        if ($coverage.SubscriptionIds -contains $SubscriptionId) {
            Assert-ManagementGroupMembershipComplete `
                -ScopeState $scope `
                -Coverage $coverage `
                -Operation "prove replacement coverage for subscription '$SubscriptionId'" |
                Out-Null
            $member = @(Get-ScopeMembers -ScopeState $scope | Where-Object {
                [string](Get-MemberValue $_ "MemberSubscriptionId") -eq $SubscriptionId
            })
            if ($member.Count -ne 1) {
                throw "Management Group '$($scope.ScopeId)' does not have exactly one member for subscription '$SubscriptionId'."
            }
            if ((Test-ScopeActive $member[0])) {
                return $true
            }
        }
    }
    return $false
}

function Test-ProtectedBaselineCoversSubscription {
    param(
        [Parameter(Mandatory = $true)][string] $SubscriptionId,
        [Parameter(Mandatory = $true)] $Context
    )

    if ($Context.Central.ProtectedScopeKind -eq "subscription") {
        return $Context.Central.ProtectedScopeId -eq $SubscriptionId
    }
    $coverage = Get-ManagementGroupCoverage `
        -ManagementGroupId $Context.Central.ProtectedScopeId `
        -Context $Context
    return $coverage.SubscriptionIds -contains $SubscriptionId
}

function Test-ProtectedBaselineOverlapsManagementGroup {
    param(
        [Parameter(Mandatory = $true)] $Coverage,
        [Parameter(Mandatory = $true)] $Context
    )

    if ($Context.Central.ProtectedScopeKind -eq "subscription") {
        return $Coverage.SubscriptionIds -contains $Context.Central.ProtectedScopeId
    }
    if ($Coverage.ManagementGroupId -eq $Context.Central.ProtectedScopeId) {
        return $true
    }
    $baselineCoverage = Get-ManagementGroupCoverage `
        -ManagementGroupId $Context.Central.ProtectedScopeId `
        -Context $Context
    $sharesSubscriptions = @($Coverage.SubscriptionIds | Where-Object {
        $baselineCoverage.SubscriptionIds -contains $_
    }).Count -gt 0
    $isNested = $Coverage.DescendantManagementGroupIds -contains $Context.Central.ProtectedScopeId -or
        $baselineCoverage.DescendantManagementGroupIds -contains $Coverage.ManagementGroupId
    return $sharesSubscriptions -or $isNested
}

function Remove-ScopeResources {
    param(
        [Parameter(Mandatory = $true)] $ScopeState,
        [Parameter(Mandatory = $true)] $Context
    )

    $members = @(Get-ScopeMembers -ScopeState $ScopeState -IncludeOrphans)
    foreach ($member in $members) {
        if ([string](Get-MemberValue $member "ManagedBy") -ne $ManagerTag) {
            throw "Refusing to delete alert '$($member.AlertId)' because it is not owned by the day-2 scope manager."
        }
        if ([string](Get-MemberValue $member "AlertId") -eq $Context.Central.ProtectedAlertId -or
            [string](Get-MemberValue $member "ActionGroupId") -eq $Context.Central.AnchorActionGroupId) {
            throw "Refusing to delete the azd-owned central baseline alert or anchor Action Group."
        }
    }
    $otherMembers = @($Context.Scopes | Where-Object {
        -not ($_.ScopeKind -eq $ScopeState.ScopeKind -and $_.ScopeId -eq $ScopeState.ScopeId)
    } | ForEach-Object { Get-ScopeMembers -ScopeState $_ -IncludeOrphans })
    foreach ($member in $members) {
        if (-not [string]::IsNullOrWhiteSpace([string]$member.AlertId)) {
            Invoke-AzCommand -Arguments @(
                "resource", "delete", "--ids", $member.AlertId
            ) | Out-Null
        }
        $otherReferences = @($otherMembers | Where-Object {
            $_.ActionGroupId -eq $member.ActionGroupId
        })
        if ($member.ActionGroupId -ne $Context.Central.AnchorActionGroupId -and
            $otherReferences.Count -eq 0) {
            Invoke-AzCommand -Arguments @("resource", "delete", "--ids", $member.ActionGroupId) | Out-Null
        }
    }
    $Context.Scopes = @($Context.Scopes | Where-Object {
        -not ($_.ScopeKind -eq $ScopeState.ScopeKind -and $_.ScopeId -eq $ScopeState.ScopeId)
    })
}

function Refresh-ScopeContext {
        param([Parameter(Mandatory = $true)] $Context)

        $Context.Scopes = @(Get-ManagedScopes -Central $Context.Central)
        $Context.ManagementGroupCache = @{}
    }

function Get-UniqueScopeState {
        param(
            [Parameter(Mandatory = $true)][string] $ScopeKind,
            [Parameter(Mandatory = $true)][string] $ScopeId,
            [Parameter(Mandatory = $true)] $Context
        )

        $foundScopes = @($Context.Scopes | Where-Object {
            $_.ScopeKind -eq $ScopeKind -and $_.ScopeId -eq $ScopeId
        })
        if ($foundScopes.Count -gt 1) {
            throw "Multiple managed alerts exist for $ScopeKind '$ScopeId'. Resolve duplicates manually."
        }
        if ($foundScopes.Count -eq 0) {
            return $null
        }
        return $foundScopes[0]
    }

function Assert-ManagementGroupRemovalCoverage {
        param(
            [Parameter(Mandatory = $true)][string] $ScopeId,
            [Parameter(Mandatory = $true)] $Context
        )

        $scopeState = Get-UniqueScopeState `
            -ScopeKind "managementGroup" `
            -ScopeId $ScopeId `
            -Context $Context
        if (-not $scopeState) {
            return $null
        }
        $coverage = Get-ManagementGroupCoverage -ManagementGroupId $ScopeId -Context $Context
        $memberSubscriptionIds = @(
            Get-ScopeMembers -ScopeState $scopeState |
                ForEach-Object {
                    [string](Get-MemberValue $_ "MemberSubscriptionId")
                } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )
        $subscriptionsRequiringReplacement = @(
            @($coverage.SubscriptionIds) + $memberSubscriptionIds |
                Sort-Object -Unique
        )
        $uncovered = @()
        foreach ($descendantId in $subscriptionsRequiringReplacement) {
            $individual = @($Context.Scopes | Where-Object {
                (Test-ScopeActive $_) -and $_.ScopeKind -eq "subscription" -and $_.ScopeId -eq $descendantId
            }).Count -gt 0
            $otherManagementGroup = Test-SubscriptionCoveredByManagementGroup `
                -SubscriptionId $descendantId `
                -Context $Context `
                -ExcludeManagementGroupId $ScopeId
            if (-not $individual -and -not $otherManagementGroup) {
                $uncovered += $descendantId
            }
        }
        if ($uncovered.Count -gt 0) {
            throw "Removing Management Group '$ScopeId' would leave subscriptions uncovered: $($uncovered -join ', '). Add replacement coverage first."
        }
        return $scopeState
    }
function Add-Scope {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeKind,
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)] $Context,
        [Parameter(Mandatory = $true)][scriptblock] $ShouldProcess,
        [switch] $LeaveDisabled,
        [switch] $AllowSubscriptionOverlap
    )

    $existing = @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq $ScopeKind -and $_.ScopeId -eq $ScopeId
    })
    if ($existing.Count -gt 1) {
        throw "Multiple managed alerts already exist for $ScopeKind '$ScopeId'. Resolve the duplicate resources before retrying."
    }
    if ($ScopeKind -eq "subscription") {
        Test-SubscriptionTenant -SubscriptionId $ScopeId -Context $Context | Out-Null
        if (Test-ProtectedBaselineCoversSubscription -SubscriptionId $ScopeId -Context $Context) {
            throw "Subscription '$ScopeId' is already covered by the immutable azd-owned baseline alert. Adding a day-2 alert would duplicate delivery."
        }
        if (Test-SubscriptionCoveredByManagementGroup -SubscriptionId $ScopeId -Context $Context) {
            throw "Subscription '$ScopeId' is already covered by an enabled Management Group alert. Adding an individual alert would duplicate delivery."
        }
        $deploymentSubscriptionIds = @($ScopeId)
        $managementGroupForPermissions = $null
    }
    else {
        $coverage = Get-ManagementGroupCoverage -ManagementGroupId $ScopeId -Context $Context
        if (Test-ProtectedBaselineOverlapsManagementGroup -Coverage $coverage -Context $Context) {
            throw "Management Group '$ScopeId' overlaps the immutable azd-owned baseline alert. Choose a non-overlapping scope; the day-2 manager will not modify the baseline."
        }
        $overlaps = @(Get-OverlapsForManagementGroup `
            -Coverage $coverage -Context $Context -ExcludeManagementGroupId $ScopeId)
        $managementGroupOverlaps = @($overlaps | Where-Object {
            $_.ScopeKind -eq "managementGroup"
        })
        $subscriptionOverlaps = @($overlaps | Where-Object {
            $_.ScopeKind -eq "subscription"
        })
        if ($managementGroupOverlaps.Count -gt 0 -or
            ($subscriptionOverlaps.Count -gt 0 -and -not $AllowSubscriptionOverlap)) {
            throw "Management Group '$ScopeId' overlaps existing managed scopes. Use migrate-to-management-group for individual subscription alerts; nested Management Group overlaps must be removed first."
        }
        $deploymentSubscriptionIds = @($coverage.SubscriptionIds)
        $managementGroupForPermissions = $null
    }

    $existingMembership = $null
    if ($ScopeKind -eq "managementGroup" -and $existing.Count -eq 1) {
        $existingMembership = Get-ManagementGroupMembershipState `
            -ScopeState $existing[0] `
            -Coverage $coverage
        if ($existingMembership.HasDuplicates -or
            $existingMembership.UnexpectedIds.Count -gt 0) {
            Assert-ManagementGroupMembershipComplete `
                -ScopeState $existing[0] `
                -Coverage $coverage `
                -Operation "repair the logical scope automatically" | Out-Null
        }
    }
    $hasCompleteMembership = $ScopeKind -eq "subscription" -or (
        $existing.Count -eq 1 -and $existingMembership.Complete
    )
    if ($existing.Count -eq 1 -and $hasCompleteMembership -and
        (Test-ScopeActive $existing[0]) -and -not $LeaveDisabled) {
        return [pscustomobject]@{
            Status = "AlreadyPresent"
            TestStatus = "NotRun"
            Scope = $existing[0]
        }
    }

    foreach ($deploymentSubscriptionId in $deploymentSubscriptionIds) {
        Assert-AddPermissions `
            -DeploymentSubscriptionId $deploymentSubscriptionId `
            -ManagementGroupId $managementGroupForPermissions `
            -Context $Context
    }

    $operation = if ($existing.Count -eq 1) {
        "Validate and enable existing alert scope"
    } else {
        "Create disabled alert scope, test Secure Webhook, and enable"
    }
    if (-not (& $ShouldProcess "$ScopeKind '$ScopeId'" $operation)) {
        return [pscustomobject]@{
            Status = "Planned"
            TestStatus = "NotRun"
            Scope = if ($existing.Count -eq 1) { $existing[0] } else { $null }
        }
    }

    if ($existing.Count -eq 1 -and $ScopeKind -eq "subscription" -and
        -not [bool](Get-MemberValue $existing[0] "OrphanedActionGroup") -and
        -not [string]::IsNullOrWhiteSpace([string]$existing[0].AlertId)) {
        $scopeState = $existing[0]
    }
    elseif ($ScopeKind -eq "subscription") {
        $scopeState = New-Day2ScopeResources `
            -ScopeKind $ScopeKind `
            -ScopeId $ScopeId `
            -Context $Context
    }
    elseif ($existing.Count -eq 1) {
        $members = @(Get-ScopeMembers -ScopeState $existing[0] | Where-Object {
            $existingMembership.RepairIds -notcontains
                [string](Get-MemberValue $_ "MemberSubscriptionId")
        })
        foreach ($missingSubscriptionId in $existingMembership.RepairIds) {
            $members += New-Day2ScopeMember `
                -ScopeKind "managementGroup" `
                -ScopeId $ScopeId `
                -TargetSubscriptionId $missingSubscriptionId `
                -Context $Context
        }
        $scopeState = New-ManagementGroupScopeState `
            -ScopeId $ScopeId `
            -Members $members `
            -Context $Context
    }
    else {
        $scopeState = New-Day2ScopeResources `
            -ScopeKind $ScopeKind `
            -ScopeId $ScopeId `
            -Context $Context
    }
    $testStatus = Invoke-OfficialWebhookTest -ScopeState $scopeState -Context $Context
    if (-not $scopeState.ActionGroupEnabled) {
        Set-ActionGroupEnabled -ScopeState $scopeState -Enabled $true
    }
    if ($ScopeKind -eq "managementGroup") {
        $Context.ManagementGroupCache = @{}
        $currentCoverage = Get-ManagementGroupCoverage `
            -ManagementGroupId $ScopeId `
            -Context $Context
        Assert-ManagementGroupMembershipComplete `
            -ScopeState $scopeState `
            -Coverage $currentCoverage `
            -Operation "activate the logical scope" | Out-Null
    }
    if (-not $LeaveDisabled) {
        Set-AlertEnabled -ScopeState $scopeState -Enabled $true
    }
    $Context.Scopes = @($Context.Scopes | Where-Object {
        -not ($_.ScopeKind -eq $ScopeKind -and $_.ScopeId -eq $ScopeId)
    }) + @($scopeState)
    return [pscustomobject]@{
        Status = if ($LeaveDisabled -and -not $scopeState.Enabled) {
            "ValidatedDisabled"
        } elseif ($LeaveDisabled) {
            "ValidatedPreserved"
        } else {
            "Added"
        }
        TestStatus = $testStatus
        Scope = $scopeState
    }
}

function Remove-SubscriptionScope {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)] $Context,
        [Parameter(Mandatory = $true)][scriptblock] $ShouldProcess,
        [Parameter(Mandatory = $true)][scriptblock] $ConfirmDestructive
    )

    Test-SubscriptionTenant -SubscriptionId $ScopeId -Context $Context | Out-Null
    $existing = @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "subscription" -and $_.ScopeId -eq $ScopeId
    })
    if ($existing.Count -eq 0) {
        return [pscustomobject]@{ Status = "AlreadyAbsent"; ScopeId = $ScopeId }
    }
    if ($existing.Count -gt 1) {
        throw "Multiple individual alerts exist for subscription '$ScopeId'. Resolve duplicates manually."
    }
    if (-not (Test-SubscriptionCoveredByManagementGroup -SubscriptionId $ScopeId -Context $Context)) {
        throw "Removing subscription '$ScopeId' would leave a coverage gap. Add or migrate to an enabled Management Group alert first."
    }
    Assert-RemovePermissions -ScopeState $existing[0]

    if (-not (& $ShouldProcess "subscription '$ScopeId'" "Delete Activity Log Alert and unshared Action Group")) {
        return [pscustomobject]@{ Status = "Planned"; ScopeId = $ScopeId }
    }
    if (-not (& $ConfirmDestructive "Remove the individual alert for subscription '$ScopeId'? Management Group coverage has been verified.")) {
        return [pscustomobject]@{ Status = "Cancelled"; ScopeId = $ScopeId }
    }
    Refresh-ScopeContext -Context $Context
    Test-SubscriptionTenant -SubscriptionId $ScopeId -Context $Context | Out-Null
    $current = Get-UniqueScopeState `
        -ScopeKind "subscription" `
        -ScopeId $ScopeId `
        -Context $Context
    if (-not $current) {
        return [pscustomobject]@{ Status = "AlreadyAbsent"; ScopeId = $ScopeId }
    }
    if (-not (Test-SubscriptionCoveredByManagementGroup -SubscriptionId $ScopeId -Context $Context)) {
        throw "Coverage changed after confirmation. Subscription '$ScopeId' is no longer proven to be covered; no resources were deleted."
    }
    Assert-RemovePermissions -ScopeState $current
    Remove-ScopeResources -ScopeState $current -Context $Context
    return [pscustomobject]@{ Status = "Removed"; ScopeId = $ScopeId }
}

function Remove-ManagementGroupScope {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)] $Context,
        [Parameter(Mandatory = $true)][scriptblock] $ShouldProcess,
        [Parameter(Mandatory = $true)][scriptblock] $ConfirmDestructive
    )

    $existing = Assert-ManagementGroupRemovalCoverage -ScopeId $ScopeId -Context $Context
    if (-not $existing) {
        return [pscustomobject]@{ Status = "AlreadyAbsent"; ScopeId = $ScopeId }
    }
    Assert-RemovePermissions -ScopeState $existing

    if (-not (& $ShouldProcess "Management Group '$ScopeId'" "Delete Activity Log Alert and unshared Action Group")) {
        return [pscustomobject]@{ Status = "Planned"; ScopeId = $ScopeId }
    }
    if (-not (& $ConfirmDestructive "Remove the Management Group alert '$ScopeId'? Replacement coverage has been verified for every accessible descendant subscription.")) {
        return [pscustomobject]@{ Status = "Cancelled"; ScopeId = $ScopeId }
    }
    Refresh-ScopeContext -Context $Context
    $current = Assert-ManagementGroupRemovalCoverage -ScopeId $ScopeId -Context $Context
    if (-not $current) {
        return [pscustomobject]@{ Status = "AlreadyAbsent"; ScopeId = $ScopeId }
    }
    Assert-RemovePermissions -ScopeState $current
    Remove-ScopeResources -ScopeState $current -Context $Context
    return [pscustomobject]@{ Status = "Removed"; ScopeId = $ScopeId }
}

function Invoke-ManagementGroupMigration {
    param(
        [Parameter(Mandatory = $true)][string] $ScopeId,
        [Parameter(Mandatory = $true)] $Context,
        [Parameter(Mandatory = $true)][scriptblock] $ShouldProcess,
        [Parameter(Mandatory = $true)][scriptblock] $ConfirmDestructive
    )

    $coverage = Get-ManagementGroupCoverage -ManagementGroupId $ScopeId -Context $Context
    $otherManagementGroupOverlaps = @(Get-OverlapsForManagementGroup `
        -Coverage $coverage -Context $Context -ExcludeManagementGroupId $ScopeId |
        Where-Object { $_.ScopeKind -eq "managementGroup" })
    if ($otherManagementGroupOverlaps.Count -gt 0) {
        throw "Management Group '$ScopeId' overlaps another enabled Management Group alert. Nested Management Group migrations are not automatic."
    }
    $individualOverlaps = @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "subscription" -and
        $coverage.SubscriptionIds -contains $_.ScopeId -and (
            (Test-ScopeActive $_) -or
            [bool](Get-MemberValue $_ "OrphanedActionGroup")
        )
    })

    $existingManagementGroup = @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "managementGroup" -and $_.ScopeId -eq $ScopeId
    })
    if ($existingManagementGroup.Count -gt 1) {
        throw "Multiple alerts exist for Management Group '$ScopeId'. Resolve duplicates manually."
    }

    $addResult = $null
    $existingMembershipComplete = $false
    if ($existingManagementGroup.Count -eq 1) {
        $existingMembershipComplete = (
            Get-ManagementGroupMembershipState `
                -ScopeState $existingManagementGroup[0] `
                -Coverage $coverage
        ).Complete
    }
    if ($existingManagementGroup.Count -eq 0 -or
        -not $existingMembershipComplete -or
        -not (Test-ScopeActive $existingManagementGroup[0])) {
        # Validate the receiver while the new alert is disabled, so cancellation cannot create duplicate delivery.
        $addResult = Add-Scope `
            -ScopeKind "managementGroup" `
            -ScopeId $ScopeId `
            -Context $Context `
            -ShouldProcess $ShouldProcess `
            -LeaveDisabled `
            -AllowSubscriptionOverlap
        if ($addResult.Status -eq "Planned") {
            return [pscustomobject]@{
                Status = "Planned"
                ManagementGroupId = $ScopeId
                OverlappingSubscriptions = @($individualOverlaps.ScopeId)
            }
        }
        $managementGroupState = $addResult.Scope
    }
    else {
        $managementGroupState = $existingManagementGroup[0]
    }

    if ($individualOverlaps.Count -eq 0) {
        $Context.ManagementGroupCache = @{}
        $currentCoverage = Get-ManagementGroupCoverage `
            -ManagementGroupId $ScopeId `
            -Context $Context
        Assert-ManagementGroupMembershipComplete `
            -ScopeState $managementGroupState `
            -Coverage $currentCoverage `
            -Operation "complete migration" | Out-Null
        if (-not (Test-ScopeActive $managementGroupState)) {
            if (-not (& $ShouldProcess "Management Group '$ScopeId'" "Enable validated Activity Log Alert")) {
                return [pscustomobject]@{
                    Status = "ValidatedDisabled"
                    ManagementGroupId = $ScopeId
                    RemovedSubscriptions = @()
                }
            }
            Set-AlertEnabled -ScopeState $managementGroupState -Enabled $true
        }
        return [pscustomobject]@{
            Status = "Migrated"
            ManagementGroupId = $ScopeId
            RemovedSubscriptions = @()
        }
    }

    foreach ($overlap in $individualOverlaps) {
        Assert-RemovePermissions -ScopeState $overlap
    }
    $subscriptionList = ($individualOverlaps.ScopeId -join ", ")
    if (-not (& $ShouldProcess "Management Group '$ScopeId'" "Enable Management Group alert and remove overlapping individual alerts: $subscriptionList")) {
        return [pscustomobject]@{
            Status = "Planned"
            ManagementGroupId = $ScopeId
            OverlappingSubscriptions = @($individualOverlaps.ScopeId)
        }
    }
    if (-not (& $ConfirmDestructive "Enable Management Group '$ScopeId', then remove the overlapping individual alerts for: ${subscriptionList}?")) {
        return [pscustomobject]@{
            Status = "Cancelled"
            ManagementGroupId = $ScopeId
            ValidatedAlertId = $managementGroupState.AlertId
        }
    }

    $confirmedSubscriptionIds = @(
        $individualOverlaps.ScopeId | Sort-Object -Unique
    )
    Refresh-ScopeContext -Context $Context
    $coverage = Get-ManagementGroupCoverage -ManagementGroupId $ScopeId -Context $Context
    $otherManagementGroupOverlaps = @(Get-OverlapsForManagementGroup `
        -Coverage $coverage -Context $Context -ExcludeManagementGroupId $ScopeId |
        Where-Object { $_.ScopeKind -eq "managementGroup" })
    if ($otherManagementGroupOverlaps.Count -gt 0) {
        throw "Coverage changed after confirmation. Management Group '$ScopeId' now overlaps another enabled Management Group alert."
    }
    $managementGroupState = Get-UniqueScopeState `
        -ScopeKind "managementGroup" `
        -ScopeId $ScopeId `
        -Context $Context
    if (-not $managementGroupState) {
        throw "The validated Management Group alert disappeared after confirmation. No subscription alerts were removed."
    }
    $managementGroupMembers = @(Get-ScopeMembers -ScopeState $managementGroupState)
    Assert-ManagementGroupMembershipComplete `
        -ScopeState $managementGroupState `
        -Coverage $coverage `
        -Operation "continue migration after confirmation" | Out-Null
    $currentIndividualOverlaps = @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "subscription" -and
        $coverage.SubscriptionIds -contains $_.ScopeId -and (
            (Test-ScopeActive $_) -or
            [bool](Get-MemberValue $_ "OrphanedActionGroup")
        )
    })
    $currentSubscriptionIds = @(
        $currentIndividualOverlaps.ScopeId | Sort-Object -Unique
    )
    if (($confirmedSubscriptionIds -join "|") -ne ($currentSubscriptionIds -join "|")) {
        throw "Coverage changed after confirmation. Overlapping subscriptions are now '$($currentSubscriptionIds -join ', ')'; rerun the migration."
    }
    foreach ($overlap in $currentIndividualOverlaps) {
        Assert-RemovePermissions -ScopeState $overlap
    }

    $overlapBySubscription = @{}
    foreach ($overlap in $currentIndividualOverlaps) {
        $overlapBySubscription[$overlap.ScopeId] = $overlap
    }

    foreach ($member in $managementGroupMembers) {
        $memberSubscriptionId = [string](Get-MemberValue $member "MemberSubscriptionId")
        $overlap = $overlapBySubscription[$memberSubscriptionId]
        if (-not $overlap) {
            if (-not $member.Enabled) {
                Set-AlertEnabled -ScopeState $member -Enabled $true
            }
            continue
        }

        if (-not (Get-CurrentActionGroupEnabled -ScopeMember $member)) {
            throw "Replacement Action Group is disabled for subscription '$memberSubscriptionId'. The original alert remains enabled; no handoff occurred."
        }
        if ($overlap.Enabled) {
            Set-AlertEnabled -ScopeState $overlap -Enabled $false
        }
        try {
            if (-not $member.Enabled) {
                Set-AlertEnabled -ScopeState $member -Enabled $true
            }
        }
        catch {
            $enableError = $_.Exception.Message
            try {
                $replacementEnabled = Get-CurrentAlertEnabled -ScopeMember $member
            }
            catch {
                throw "Replacement alert state is indeterminate for subscription '$memberSubscriptionId' after an enable failure. The original alert remains disabled to avoid duplicate delivery. Immediate manual intervention is required to inspect both alerts and restore exactly one enabled path. Enable error: $enableError State-read error: $($_.Exception.Message)"
            }
            if ($replacementEnabled) {
                throw "Replacement alert is enabled for subscription '$memberSubscriptionId' after an uncertain enable response. The original alert remains disabled, preserving one active path. Manual review is required before retrying migration. $enableError"
            }
            try {
                Set-AlertEnabled -ScopeState $overlap -Enabled $true
            }
            catch {
                throw "Replacement alert failed for subscription '$memberSubscriptionId', and its original alert could not be re-enabled. Immediate manual intervention is required to restore coverage. Replacement error: $enableError Rollback error: $($_.Exception.Message)"
            }
            throw "Replacement alert failed for subscription '$memberSubscriptionId'. Its original alert was re-enabled, so coverage remains intact. $enableError"
        }
    }
    $managementGroupState.Enabled = @($managementGroupMembers | Where-Object {
        -not $_.Enabled
    }).Count -eq 0

    foreach ($subscriptionScopeId in $confirmedSubscriptionIds) {
        $currentSubscription = $overlapBySubscription[$subscriptionScopeId]
        $replacement = @($managementGroupMembers | Where-Object {
            [string](Get-MemberValue $_ "MemberSubscriptionId") -eq
                $subscriptionScopeId
        })[0]
        try {
            $replacementAlertEnabled = Get-CurrentAlertEnabled `
                -ScopeMember $replacement
            $replacementActionGroupEnabled = Get-CurrentActionGroupEnabled `
                -ScopeMember $replacement
        }
        catch {
            throw "Replacement state is indeterminate for subscription '$subscriptionScopeId'. Its disabled original was not deleted. Inspect both paths and restore exactly one active path before retrying. $($_.Exception.Message)"
        }
        if (-not $replacementAlertEnabled -or -not $replacementActionGroupEnabled) {
            if ([bool](Get-MemberValue $currentSubscription "OrphanedActionGroup")) {
                throw "Replacement coverage became inactive for subscription '$subscriptionScopeId'. The orphaned Action Group was retained; no original alert exists to restore. Restore replacement coverage before retrying cleanup."
            }
            try {
                Set-AlertEnabled -ScopeState $currentSubscription -Enabled $true
            }
            catch {
                throw "Replacement coverage became inactive for subscription '$subscriptionScopeId', and its original alert could not be restored. Immediate manual intervention is required. $($_.Exception.Message)"
            }
            throw "Replacement coverage became inactive for subscription '$subscriptionScopeId'. Its original alert was restored and was not deleted."
        }
        try {
            Remove-ScopeResources -ScopeState $currentSubscription -Context $Context
        }
        catch {
            throw "Replacement coverage is enabled and the original alert is disabled, but an overlapping subscription resource could not be deleted. No duplicate delivery is active; rerun migration to finish cleanup. $($_.Exception.Message)"
        }
    }

    return [pscustomobject]@{
        Status = "Migrated"
        ManagementGroupId = $ScopeId
        RemovedSubscriptions = $confirmedSubscriptionIds
        TestStatus = if ($addResult) { $addResult.TestStatus } else { "NotRun" }
    }
}

function Get-ScopeReport {
    param([Parameter(Mandatory = $true)] $Context)

    $managementGroupCoverage = @{}
    $managementGroupMembership = @{}
    foreach ($scope in @($Context.Scopes | Where-Object {
        $_.ScopeKind -eq "managementGroup"
    })) {
        $managementGroupCoverage[$scope.ScopeId] = Get-ManagementGroupCoverage `
            -ManagementGroupId $scope.ScopeId `
            -Context $Context
        $managementGroupMembership[$scope.ScopeId] = Get-ManagementGroupMembershipState `
            -ScopeState $scope `
            -Coverage $managementGroupCoverage[$scope.ScopeId]
    }

    $report = @()
    foreach ($scope in $Context.Scopes) {
        if ($scope.ScopeKind -eq "subscription") {
            Test-SubscriptionTenant -SubscriptionId $scope.ScopeId -Context $Context | Out-Null
            $coveringGroups = @($Context.Scopes | Where-Object {
                (Test-ScopeActive $_) -and $_.ScopeKind -eq "managementGroup" -and
                $managementGroupMembership[$_.ScopeId].Complete -and
                $managementGroupCoverage[$_.ScopeId].SubscriptionIds -contains $scope.ScopeId
            } | ForEach-Object { $_.ScopeId })
            $protectedOverlap = Test-ProtectedBaselineCoversSubscription `
                -SubscriptionId $scope.ScopeId `
                -Context $Context
            $effective = if ((Test-ScopeActive $scope) -or $coveringGroups.Count -gt 0) {
                "Covered"
            } else {
                "Disabled"
            }
            $overlapParts = @()
            if ((Test-ScopeActive $scope) -and $coveringGroups.Count -gt 0) {
                $overlapParts += "Duplicate with MG: $($coveringGroups -join ', ')"
            }
            if ((Test-ScopeActive $scope) -and $protectedOverlap) {
                $overlapParts += "Duplicate with protected baseline"
            }
            $overlap = $overlapParts -join "; "
            $coverageDetail = if ([bool](Get-MemberValue $scope "OrphanedActionGroup")) {
                "$($scope.ScopeId); orphaned Action Group requires repair or cleanup"
            } else {
                $scope.ScopeId
            }
            $coveredSubscriptionIds = @($scope.ScopeId)
        }
        else {
            $coverage = $managementGroupCoverage[$scope.ScopeId]
            $membership = $managementGroupMembership[$scope.ScopeId]
            $effective = if (-not $membership.Complete) {
                "Incomplete"
            } elseif (Test-ScopeActive $scope) {
                "Covered"
            } else {
                "Disabled"
            }
            $individualOverlaps = @($Context.Scopes | Where-Object {
                (Test-ScopeActive $_) -and $_.ScopeKind -eq "subscription" -and
                $coverage.SubscriptionIds -contains $_.ScopeId
            } | ForEach-Object { $_.ScopeId })
            $managementGroupOverlaps = @($Context.Scopes | Where-Object {
                (Test-ScopeActive $_) -and $_.ScopeKind -eq "managementGroup" -and
                $_.ScopeId -ne $scope.ScopeId -and (
                    $coverage.DescendantManagementGroupIds -contains $_.ScopeId -or
                    $managementGroupCoverage[$_.ScopeId].DescendantManagementGroupIds -contains $scope.ScopeId
                )
            } | ForEach-Object { $_.ScopeId })
            $overlapParts = @()
            if ($individualOverlaps.Count -gt 0) {
                $overlapParts += "Subscriptions: $($individualOverlaps -join ', ')"
            }
            if ($managementGroupOverlaps.Count -gt 0) {
                $overlapParts += "Management Groups: $($managementGroupOverlaps -join ', ')"
            }
            if ((Test-ScopeActive $scope) -and
                (Test-ProtectedBaselineOverlapsManagementGroup -Coverage $coverage -Context $Context)) {
                $overlapParts += "Protected baseline"
            }
            $overlap = $overlapParts -join "; "
            if ($membership.Complete) {
                $coverageDetail = "$($coverage.SubscriptionIds.Count) descendant subscription(s)"
                if ($membership.OrphanedIds.Count -gt 0) {
                    $coverageDetail += "; orphaned Action Groups requiring cleanup: $($membership.OrphanedIds -join ', ')"
                }
            }
            else {
                $membershipIssues = @()
                if ($membership.MissingIds.Count -gt 0) {
                    $membershipIssues += "missing members: $($membership.MissingIds -join ', ')"
                }
                if ($membership.UnexpectedIds.Count -gt 0) {
                    $membershipIssues += "unexpected members: $($membership.UnexpectedIds -join ', ')"
                }
                if ($membership.HasDuplicates) {
                    $membershipIssues += "duplicate or blank member IDs"
                }
                if ($membership.OrphanedIds.Count -gt 0) {
                    $membershipIssues += "orphaned Action Groups: $($membership.OrphanedIds -join ', ')"
                }
                $coverageDetail = "$($coverage.SubscriptionIds.Count) descendant(s); $($membershipIssues -join '; ')"
            }
            $coveredSubscriptionIds = if ($membership.Complete) {
                @($coverage.SubscriptionIds)
            } else {
                @(
                    Get-ScopeMembers -ScopeState $scope |
                        Where-Object { Test-ScopeActive $_ } |
                        ForEach-Object {
                            [string](Get-MemberValue $_ "MemberSubscriptionId")
                        } |
                        Sort-Object -Unique
                )
            }
        }

        $report += [pscustomobject]@{
            Environment = $Context.Central.EnvironmentName
            TenantId = $Context.Central.TenantId
            ScopeKind = $scope.ScopeKind
            ScopeId = $scope.ScopeId
            EffectiveCoverage = $effective
            CoverageDetail = $coverageDetail
            CoveredSubscriptionIds = $coveredSubscriptionIds
            Enabled = $scope.Enabled
            ActionGroupEnabled = $scope.ActionGroupEnabled
            AlertId = $scope.AlertId
            ActionGroupId = $scope.ActionGroupId
            Overlap = $overlap
        }
    }
    return $report
}

function Invoke-Day2Command {
    param(
        [Parameter(Mandatory = $true)][string] $CommandName,
        [string] $TargetSubscriptionId,
        [string] $TargetManagementGroupId,
        [string] $RequestedEnvironmentName,
        [Parameter(Mandatory = $true)][scriptblock] $ShouldProcess,
        [Parameter(Mandatory = $true)][scriptblock] $ConfirmDestructive
    )

    Assert-AzureCli
    $central = Get-CentralDeployment -RequestedEnvironmentName $RequestedEnvironmentName
    $context = [pscustomobject]@{
        Central = $central
        Scopes = @(Get-ManagedScopes -Central $central)
        ManagementGroupCache = @{}
    }

    switch ($CommandName) {
        "list" {
            return Get-ScopeReport -Context $context
        }
        "add-subscription" {
            if ([string]::IsNullOrWhiteSpace($TargetSubscriptionId)) {
                throw "-SubscriptionId is required for add-subscription."
            }
            return Add-Scope `
                -ScopeKind "subscription" `
                -ScopeId $TargetSubscriptionId `
                -Context $context `
                -ShouldProcess $ShouldProcess
        }
        "remove-subscription" {
            if ([string]::IsNullOrWhiteSpace($TargetSubscriptionId)) {
                throw "-SubscriptionId is required for remove-subscription."
            }
            return Remove-SubscriptionScope `
                -ScopeId $TargetSubscriptionId `
                -Context $context `
                -ShouldProcess $ShouldProcess `
                -ConfirmDestructive $ConfirmDestructive
        }
        "add-management-group" {
            if ([string]::IsNullOrWhiteSpace($TargetManagementGroupId)) {
                throw "-ManagementGroupId is required for add-management-group."
            }
            return Add-Scope `
                -ScopeKind "managementGroup" `
                -ScopeId $TargetManagementGroupId `
                -Context $context `
                -ShouldProcess $ShouldProcess
        }
        "remove-management-group" {
            if ([string]::IsNullOrWhiteSpace($TargetManagementGroupId)) {
                throw "-ManagementGroupId is required for remove-management-group."
            }
            return Remove-ManagementGroupScope `
                -ScopeId $TargetManagementGroupId `
                -Context $context `
                -ShouldProcess $ShouldProcess `
                -ConfirmDestructive $ConfirmDestructive
        }
        "migrate-to-management-group" {
            if ([string]::IsNullOrWhiteSpace($TargetManagementGroupId)) {
                throw "-ManagementGroupId is required for migrate-to-management-group."
            }
            return Invoke-ManagementGroupMigration `
                -ScopeId $TargetManagementGroupId `
                -Context $context `
                -ShouldProcess $ShouldProcess `
                -ConfirmDestructive $ConfirmDestructive
        }
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    $shouldProcessCallback = {
        param($Target, $Operation)
        return $PSCmdlet.ShouldProcess($Target, $Operation)
    }.GetNewClosure()
    $confirmationCallback = {
        param($Question)
        if ($Force) {
            return $true
        }
        return $PSCmdlet.ShouldContinue($Question, "Confirm destructive day-2 scope operation")
    }.GetNewClosure()

    $result = Invoke-Day2Command `
        -CommandName $Command `
        -TargetSubscriptionId $SubscriptionId `
        -TargetManagementGroupId $ManagementGroupId `
        -RequestedEnvironmentName $EnvironmentName `
        -ShouldProcess $shouldProcessCallback `
        -ConfirmDestructive $confirmationCallback

    if ($Json) {
        $result | ConvertTo-Json -Depth 20
    }
    else {
        $result
    }
}
