Describe "manage-alert-scopes.ps1" {
BeforeAll {
    . (Join-Path $PSScriptRoot "..\scripts\manage-alert-scopes.ps1") -Command list

    function New-TestCentral {
        param([string[]] $SubscriptionIds = @("sub-central", "sub-a", "sub-b"))

        $accounts = @($SubscriptionIds | ForEach-Object {
            [pscustomobject]@{
                id = $_
                tenantId = "tenant-a"
                state = "Enabled"
            }
        })
        return [pscustomobject]@{
            EnvironmentName = "test"
            TenantId = "tenant-a"
            SubscriptionId = "sub-central"
            ResourceGroup = "rg-test"
            Location = "eastus2"
            WebhookUri = "https://ca-test.example/api/service-health"
            SecureWebhookClientId = "client-id"
            SecureWebhookObjectId = "object-id"
            SecureWebhookIdentifierUri = "api://client-id"
            AnchorActionGroupId = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/actionGroups/ag-test-service-health"
            ProtectedAlertId = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/activityLogAlerts/ala-test-service-health"
            ProtectedScopeKind = "subscription"
            ProtectedScopeId = "sub-central"
            ProtectedScopeResourceId = "/subscriptions/sub-central"
            Accounts = $accounts
        }
    }

    function New-TestScope {
        param(
            [string] $Kind,
            [string] $Id,
            [bool] $Enabled = $true,
            [string] $SubscriptionId = "sub-central"
        )

        $suffix = "$Kind-$Id"
        if ($Kind -eq "managementGroup") {
            $members = @(@("sub-a", "sub-b") | ForEach-Object {
                $memberId = $_
                [pscustomobject]@{
                    ScopeKind = $Kind
                    ScopeId = $Id
                    ScopeResourceId = "/subscriptions/$memberId"
                    AlertId = "/subscriptions/$memberId/resourceGroups/rg-alerts/providers/Microsoft.Insights/activityLogAlerts/ala-$suffix-$memberId"
                    ActionGroupId = "/subscriptions/$memberId/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-$suffix-$memberId"
                    Enabled = $Enabled
                    ActionGroupEnabled = $true
                    TenantId = "tenant-a"
                    ManagedBy = "manage-alert-scopes"
                    MemberSubscriptionId = $memberId
                }
            })
            return [pscustomobject]@{
                ScopeKind = $Kind
                ScopeId = $Id
                ScopeResourceId = "/providers/Microsoft.Management/managementGroups/$Id"
                AlertId = @($members.AlertId)
                ActionGroupId = @($members.ActionGroupId)
                Enabled = $Enabled
                ActionGroupEnabled = $true
                TenantId = "tenant-a"
                ManagedBy = "manage-alert-scopes"
                MemberSubscriptionIds = @($members.MemberSubscriptionId)
                Members = $members
            }
        }
        return [pscustomobject]@{
            ScopeKind = $Kind
            ScopeId = $Id
            ScopeResourceId = if ($Kind -eq "subscription") {
                "/subscriptions/$Id"
            } else {
                "/providers/Microsoft.Management/managementGroups/$Id"
            }
            AlertId = "/subscriptions/$SubscriptionId/resourceGroups/rg-alerts/providers/Microsoft.Insights/activityLogAlerts/ala-$suffix"
            ActionGroupId = "/subscriptions/$SubscriptionId/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-$suffix"
            Enabled = $Enabled
            ActionGroupEnabled = $true
            TenantId = "tenant-a"
            ManagedBy = "manage-alert-scopes"
        }
    }

    function New-TestContext {
        param([object[]] $Scopes = @())

        return [pscustomobject]@{
            Central = New-TestCentral
            Scopes = @($Scopes)
            ManagementGroupCache = @{}
        }
    }
}

BeforeEach {
    $script:AzCalls = @()
    $script:MockTenant = "tenant-a"
    $script:MockDescendants = @("sub-a", "sub-b")
    $script:MockDescendantManagementGroups = @()
    $script:TestState = "Complete"
    $script:TestReceiverStatus = "Succeeded"
    $script:IncludeTestActionDetail = $true
    $script:MockAlertTenantScope = "tenant-a"
    $script:PermissionActions = @("*")
    $script:LastScopeKind = $null
    $script:LastScopeId = $null
    $script:LastTargetSubscriptionId = $null
    $script:LastActionGroupId = $null

    Mock Refresh-ScopeContext {
        param($Context)
        $Context.ManagementGroupCache = @{}
    }
    Mock Get-CurrentAlertEnabled { return $true }

    Mock Invoke-AzCommand {
        param([string[]] $Arguments)

        $script:AzCalls += , @($Arguments)
        $commandLine = $Arguments -join " "

        if ($commandLine -match "account show --subscription") {
            return [pscustomobject]@{
                id = $Arguments[-1]
                tenantId = $script:MockTenant
                state = "Enabled"
            }
        }
        if ($commandLine -match "Microsoft.Authorization/permissions") {
            return [pscustomobject]@{
                value = @(
                    [pscustomobject]@{
                        actions = $script:PermissionActions
                        notActions = @()
                    }
                )
            }
        }
        if ($commandLine -match "/descendants[?]api-version") {
            return [pscustomobject]@{
                value = @($script:MockDescendants | ForEach-Object {
                    [pscustomobject]@{
                        type = "/subscriptions"
                        name = $_
                    }
                }) + @($script:MockDescendantManagementGroups | ForEach-Object {
                    [pscustomobject]@{
                        type = "/providers/Microsoft.Management/managementGroups"
                        name = $_
                    }
                })
            }
        }
        if ($commandLine -match "/managementGroups/[^?]+[?]api-version") {
            return [pscustomobject]@{
                properties = [pscustomobject]@{ tenantId = $script:MockTenant }
            }
        }
        if ($commandLine -match "^deployment sub create") {
            $scopeKindArgument = @($Arguments | Where-Object { $_ -like "scopeKind=*" })[0]
            $scopeIdArgument = @($Arguments | Where-Object { $_ -like "scopeId=*" })[0]
            $targetSubscriptionArgument = @($Arguments | Where-Object {
                $_ -like "targetSubscriptionId=*"
            })[0]
            $scopeKind = $scopeKindArgument.Split("=", 2)[1]
            $scopeId = $scopeIdArgument.Split("=", 2)[1]
            $targetSubscriptionId = $targetSubscriptionArgument.Split("=", 2)[1]
            $deploymentSubscription = $Arguments[
                [array]::IndexOf($Arguments, "--subscription") + 1
            ]
            $script:LastScopeKind = $scopeKind
            $script:LastScopeId = $scopeId
            $script:LastTargetSubscriptionId = $targetSubscriptionId
            $script:LastActionGroupId = "/subscriptions/$deploymentSubscription/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-$scopeKind-$scopeId"
            return [pscustomobject]@{
                properties = [pscustomobject]@{
                    outputs = [pscustomobject]@{
                        activityLogAlertId = [pscustomobject]@{
                            value = "/subscriptions/$deploymentSubscription/resourceGroups/rg-alerts/providers/Microsoft.Insights/activityLogAlerts/ala-$scopeKind-$scopeId"
                        }
                        actionGroupId = [pscustomobject]@{
                            value = "/subscriptions/$deploymentSubscription/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-$scopeKind-$scopeId"
                        }
                    }
                }
            }
        }
        if ($commandLine -match "^resource show --ids .*activityLogAlerts") {
            $scopeResourceId = "/subscriptions/$($script:LastTargetSubscriptionId)"
            return [pscustomobject]@{
                properties = [pscustomobject]@{
                    enabled = $false
                    scopes = @($scopeResourceId)
                    tenantScope = $script:MockAlertTenantScope
                    condition = [pscustomobject]@{
                        allOf = @(
                            [pscustomobject]@{
                                field = "category"
                                equals = "ServiceHealth"
                            }
                        )
                    }
                    actions = [pscustomobject]@{
                        actionGroups = @(
                            [pscustomobject]@{
                                actionGroupId = $script:LastActionGroupId
                            }
                        )
                    }
                }
            }
        }
        if ($commandLine -match "^resource show --ids .*actionGroups") {
            return [pscustomobject]@{
                properties = [pscustomobject]@{ enabled = $true }
            }
        }
        if ($commandLine -match "^monitor action-group test-notifications create") {
            $details = if ($script:IncludeTestActionDetail) {
                @(
                    [pscustomobject]@{
                        MechanismType = "SecureWebhook"
                        Name = "slack-service-health"
                        Status = $script:TestReceiverStatus
                        Detail = if ($script:TestReceiverStatus -eq "Succeeded") {
                            $null
                        } else {
                            "Receiver failed"
                        }
                    }
                )
            } else {
                @()
            }
            return [pscustomobject]@{
                state = $script:TestState
                actionDetails = $details
            }
        }
        if ($commandLine -match "^resource update") {
            $enabled = $commandLine -match "properties.enabled=true"
            return [pscustomobject]@{
                properties = [pscustomobject]@{ enabled = $enabled }
            }
        }
        if ($commandLine -match "^resource delete") {
            return $null
        }
        throw "Unexpected mocked Azure CLI call: $commandLine"
    }
}

Describe "central deployment discovery" {
    It "discovers webhook identity metadata without reading secrets" {
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $script:AzCalls += , @($Arguments)
            $commandLine = $Arguments -join " "
            if ($commandLine -eq "account show") {
                return [pscustomobject]@{
                    id = "sub-central"
                    tenantId = "tenant-a"
                    state = "Enabled"
                }
            }
            if ($commandLine -eq "account list") {
                return @(
                    [pscustomobject]@{
                        id = "sub-central"
                        tenantId = "tenant-a"
                        state = "Enabled"
                    },
                    [pscustomobject]@{
                        id = "sub-other-tenant"
                        tenantId = "tenant-b"
                        state = "Enabled"
                    }
                )
            }
            if ($commandLine -match "^group list") {
                return [pscustomobject]@{
                    name = "rg-test"
                    location = "eastus2"
                    tags = @{
                        workload = "azure-service-health-slack-bot"
                        "azd-env-name" = "test"
                    }
                }
            }
            if ($commandLine -match "^resource list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.App/containerApps/ca-test"
                    name = "ca-test"
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/actionGroups/ag-test-service-health"
                    name = "ag-test-service-health"
                    webhookReceivers = @(
                        [pscustomobject]@{
                            serviceUri = "https://ca-test.example/api/service-health"
                            useAadAuth = $true
                            useCommonAlertSchema = $true
                            tenantId = "tenant-a"
                            objectId = "object-id"
                            identifierUri = "api://client-id"
                        }
                    )
                }
            }
            if ($commandLine -match "^monitor activity-log alert list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/activityLogAlerts/ala-test-service-health"
                    name = "ala-test-service-health"
                    scopes = @("/subscriptions/sub-central")
                    actions = [pscustomobject]@{
                        actionGroups = @(
                            [pscustomobject]@{
                                actionGroupId = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/actionGroups/ag-test-service-health"
                            }
                        )
                    }
                }
            }
            if ($commandLine -match "/authConfigs/current") {
                return [pscustomobject]@{
                    properties = [pscustomobject]@{
                        identityProviders = [pscustomobject]@{
                            azureActiveDirectory = [pscustomobject]@{
                                registration = [pscustomobject]@{
                                    clientId = "client-id"
                                }
                            }
                        }
                    }
                }
            }
            if ($commandLine -match "containerApps/ca-test[?]api-version") {
                return [pscustomobject]@{
                    properties = [pscustomobject]@{
                        configuration = [pscustomobject]@{
                            ingress = [pscustomobject]@{
                                fqdn = "ca-test.example"
                            }
                        }
                    }
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return @()
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        $central = Get-CentralDeployment -RequestedEnvironmentName test

        $central.TenantId | Should -Be "tenant-a"
        $central.SecureWebhookClientId | Should -Be "client-id"
        $central.SecureWebhookObjectId | Should -Be "object-id"
        $central.WebhookUri | Should -Be "https://ca-test.example/api/service-health"
        ($script:AzCalls -join " ") | Should -Not -Match "secret|keyvault|slack.bot.token"
        ($script:AzCalls -join " ") | Should -Not -Match "group list --subscription sub-other-tenant"
    }

    It "ignores cached subscriptions outside the active tenant" {
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $script:AzCalls += , @($Arguments)
            $commandLine = $Arguments -join " "
            if ($commandLine -eq "account show") {
                return [pscustomobject]@{
                    id = "sub-central"
                    tenantId = "tenant-a"
                    state = "Enabled"
                }
            }
            if ($commandLine -eq "account list") {
                return @(
                    [pscustomobject]@{
                        id = "sub-foreign"
                        tenantId = "tenant-foreign"
                        state = "Enabled"
                    },
                    [pscustomobject]@{
                        id = "sub-central"
                        tenantId = "tenant-a"
                        state = "Enabled"
                    }
                )
            }
            if ($commandLine -match "^group list") {
                return [pscustomobject]@{
                    name = "rg-test"
                    location = "eastus2"
                    tags = @{
                        workload = "azure-service-health-slack-bot"
                        "azd-env-name" = "test"
                    }
                }
            }
            if ($commandLine -match "^resource list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.App/containerApps/ca-test"
                    name = "ca-test"
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/actionGroups/ag-test-service-health"
                    name = "ag-test-service-health"
                    webhookReceivers = @(
                        [pscustomobject]@{
                            serviceUri = "https://ca-test.example/api/service-health"
                            useAadAuth = $true
                            useCommonAlertSchema = $true
                            tenantId = "tenant-a"
                            objectId = "object-id"
                            identifierUri = "api://client-id"
                        }
                    )
                }
            }
            if ($commandLine -match "^monitor activity-log alert list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/activityLogAlerts/ala-test-service-health"
                    name = "ala-test-service-health"
                    scopes = @("/subscriptions/sub-central")
                    actions = [pscustomobject]@{
                        actionGroups = @(
                            [pscustomobject]@{
                                actionGroupId = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/actionGroups/ag-test-service-health"
                            }
                        )
                    }
                }
            }
            if ($commandLine -match "/authConfigs/current") {
                return [pscustomobject]@{
                    properties = [pscustomobject]@{
                        identityProviders = [pscustomobject]@{
                            azureActiveDirectory = [pscustomobject]@{
                                registration = [pscustomobject]@{
                                    clientId = "client-id"
                                }
                            }
                        }
                    }
                }
            }
            if ($commandLine -match "containerApps/ca-test[?]api-version") {
                return [pscustomobject]@{
                    properties = [pscustomobject]@{
                        configuration = [pscustomobject]@{
                            ingress = [pscustomobject]@{
                                fqdn = "ca-test.example"
                            }
                        }
                    }
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return @()
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        $central = Get-CentralDeployment -RequestedEnvironmentName test 3>$null

        $central.EnvironmentName | Should -Be "test"
        $central.SubscriptionId | Should -Be "sub-central"
        $central.TenantId | Should -Be "tenant-a"
        # The foreign subscription is excluded before any resource query.
        @($script:AzCalls | Where-Object { ($_ -join " ") -match "--subscription sub-foreign" }).Count |
            Should -Be 0
    }

    It "propagates an unexpected initial resource-group listing failure" {
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $script:AzCalls += , @($Arguments)
            $commandLine = $Arguments -join " "
            if ($commandLine -eq "account show") {
                return [pscustomobject]@{
                    id = "sub-central"
                    tenantId = "tenant-a"
                    state = "Enabled"
                }
            }
            if ($commandLine -eq "account list") {
                return [pscustomobject]@{
                    id = "sub-central"
                    tenantId = "tenant-a"
                    state = "Enabled"
                }
            }
            if ($commandLine -match "^group list") {
                throw "Azure CLI returned invalid JSON for: az group list --subscription sub-central --tag workload=azure-service-health-slack-bot"
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        { Get-CentralDeployment -RequestedEnvironmentName test } |
            Should -Throw "*invalid JSON*"
    }
}

Describe "day-2 add operations" {
    It "adds a subscription idempotently without redeploying central resources" {
        $context = New-TestContext

        $result = Add-Scope `
            -ScopeKind subscription `
            -ScopeId sub-a `
            -Context $context `
            -ShouldProcess { $true }

        $result.Status | Should -Be "Added"
        $result.TestStatus | Should -Be "Complete"
        $result.Scope.Enabled | Should -BeTrue
        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Match "(?m)^deployment sub create"
        ($script:AzCalls -join " ") | Should -Not -Match "azd|containerapp|container registry|keyvault|storage"
    }

    It "does nothing when the requested subscription scope is already enabled" {
        $existing = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $context = New-TestContext -Scopes @($existing)

        $result = Add-Scope `
            -ScopeKind subscription `
            -ScopeId sub-a `
            -Context $context `
            -ShouldProcess { $true }

        $result.Status | Should -Be "AlreadyPresent"
        ($script:AzCalls -join " ") |
            Should -Not -Match "deployment sub create|resource update|resource delete|test-notifications"
    }

    It "recreates a missing alert from a discovered orphan Action Group" {
        $orphan = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $orphan.AlertId = $null
        $orphan.Enabled = $false
        $orphan | Add-Member -NotePropertyName OrphanedActionGroup -NotePropertyValue $true
        $context = New-TestContext -Scopes @($orphan)

        $result = Add-Scope `
            -ScopeKind subscription `
            -ScopeId sub-a `
            -Context $context `
            -ShouldProcess { $true }

        $result.Status | Should -Be "Added"
        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Match "deployment sub create --subscription sub-a"
    }

    It "rejects a subscription from another tenant" {
        $script:MockTenant = "tenant-b"
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*Multi-tenant scope management is not supported*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create"
    }

    It "rejects a day-2 alert that would duplicate immutable baseline coverage" {
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-central `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*immutable azd-owned baseline alert*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create"
    }

    It "rejects an individual subscription already covered by a Management Group" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($managementGroup)

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*duplicate delivery*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create"
    }

    It "rejects a Management Group that overlaps individual subscriptions" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $context = New-TestContext -Scopes @($individual)

        {
            Add-Scope `
                -ScopeKind managementGroup `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*overlaps existing managed scopes*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create"
    }

    It "reports missing permissions before mutation" {
        $script:PermissionActions = @("Microsoft.Resources/subscriptions/read")
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*Missing Azure permissions*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create"
    }

    It "requires official test-notification permissions before mutation" {
        $script:PermissionActions = @(
            "Microsoft.Resources/subscriptions/resourceGroups/write",
            "Microsoft.Resources/deployments/write",
            "Microsoft.Insights/actionGroups/write",
            "Microsoft.Insights/activityLogAlerts/write"
        )
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*CreateNotifications/Write*NotificationStatus/Read*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create"
    }

    It "leaves a newly created alert disabled when the official test fails" {
        $script:TestState = "Failed"
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*remains disabled*"

        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Not -Match "resource update.*properties.enabled=true"
    }

    It "rejects an unexpected overall test success variant" {
        $script:TestState = "Completed"
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*did not complete successfully*"
    }

    It "leaves a newly created alert disabled when the Secure Webhook receiver fails" {
        $script:TestReceiverStatus = "Failed"
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*receiver test failed*"

        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Not -Match "resource update.*properties.enabled=true"
    }

    It "fails closed when the official test omits receiver details" {
        $script:IncludeTestActionDetail = $false
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*exactly one result*"
    }

    It "rejects an unexpected Secure Webhook receiver success variant" {
        $script:TestReceiverStatus = "Completed"
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind subscription `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*receiver test failed*"
    }

    It "rejects a Management Group with no descendant subscriptions" {
        $script:MockDescendants = @()
        $context = New-TestContext

        {
            Add-Scope `
                -ScopeKind managementGroup `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*has no descendant subscriptions to cover*"

        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Not -Match "resource update.*properties.enabled=true"
    }

    It "fans a Management Group out to one tested alert per descendant subscription" {
        $context = New-TestContext

        $result = Add-Scope `
            -ScopeKind managementGroup `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $true }

        $result.Status | Should -Be "Added"
        $result.Scope.MemberSubscriptionIds | Should -Be @("sub-a", "sub-b")
        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        @($calls | Where-Object { $_ -match "^deployment sub create" }).Count |
            Should -Be 2
        ($calls -join "`n") | Should -Match (
            "deployment sub create --subscription sub-a.*" +
            "targetSubscriptionId=sub-a"
        )
        ($calls -join "`n") | Should -Match (
            "deployment sub create --subscription sub-b.*" +
            "targetSubscriptionId=sub-b"
        )
        @($calls | Where-Object {
            $_ -match "^monitor action-group test-notifications create"
        }).Count | Should -Be 2
        @($calls | Where-Object {
            $_ -match "^resource update.*activityLogAlerts.*properties.enabled=true"
        }).Count | Should -Be 2
    }

    It "rolls back every attempted member when Management Group activation is uncertain" {
        $context = New-TestContext
        Mock Invoke-AzCommand {
            throw "simulated enable failure"
        } -ParameterFilter {
            ($Arguments -join " ") -match (
                "^resource update --ids /subscriptions/sub-b/.+" +
                "activityLogAlerts/ala-managementGroup-mg-root.*" +
                "properties.enabled=true"
            )
        }

        {
            Add-Scope `
                -ScopeKind managementGroup `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*previously updated members were rolled back*"

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-a/.+" +
            "activityLogAlerts/ala-managementGroup-mg-root.*" +
            "properties.enabled=true"
        )
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-a/.+" +
            "activityLogAlerts/ala-managementGroup-mg-root.*" +
            "properties.enabled=false"
        )
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-b/.+" +
            "activityLogAlerts/ala-managementGroup-mg-root.*" +
            "properties.enabled=false"
        )
        ($calls -join "`n") | Should -Not -Match "resource delete"
    }

    It "repairs only missing Management Group members without disabling healthy members" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $managementGroup.Members = @($managementGroup.Members | Where-Object {
            $_.MemberSubscriptionId -eq "sub-a"
        })
        $managementGroup.MemberSubscriptionIds = @("sub-a")
        $managementGroup.AlertId = @($managementGroup.Members.AlertId)
        $managementGroup.ActionGroupId = @($managementGroup.Members.ActionGroupId)
        $context = New-TestContext -Scopes @($managementGroup)

        $result = Add-Scope `
            -ScopeKind managementGroup `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $true }

        $result.Status | Should -Be "Added"
        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        @($calls | Where-Object { $_ -match "^deployment sub create" }).Count |
            Should -Be 1
        ($calls -join "`n") | Should -Match (
            "deployment sub create --subscription sub-b.*" +
            "targetSubscriptionId=sub-b"
        )
        ($calls -join "`n") | Should -Not -Match (
            "deployment sub create --subscription sub-a|" +
            "activityLogAlerts/ala-managementGroup-mg-root-sub-a.*" +
            "properties.enabled=false"
        )
    }

    It "preserves healthy existing members when Management Group repair validation fails" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $managementGroup.Members = @($managementGroup.Members | Where-Object {
            $_.MemberSubscriptionId -eq "sub-a"
        })
        $managementGroup.MemberSubscriptionIds = @("sub-a")
        $managementGroup.AlertId = @($managementGroup.Members.AlertId)
        $managementGroup.ActionGroupId = @($managementGroup.Members.ActionGroupId)
        $context = New-TestContext -Scopes @($managementGroup)
        $script:TestState = "Failed"

        {
            Add-Scope `
                -ScopeKind managementGroup `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*remains disabled*"

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        ($calls -join "`n") | Should -Match (
            "deployment sub create --subscription sub-b.*" +
            "targetSubscriptionId=sub-b"
        )
        ($calls -join "`n") | Should -Not -Match (
            "activityLogAlerts/ala-managementGroup-mg-root-sub-a.*" +
            "properties.enabled=false"
        )
    }

    It "aborts activation when Management Group descendants change during validation" {
        $context = New-TestContext
        $script:CoverageCallCount = 0
        Mock Get-ManagementGroupCoverage {
            param([string] $ManagementGroupId, $Context)

            $script:CoverageCallCount++
            return [pscustomobject]@{
                ManagementGroupId = $ManagementGroupId
                TenantId = "tenant-a"
                SubscriptionIds = if ($script:CoverageCallCount -le 2) {
                    @("sub-a", "sub-b")
                } else {
                    @("sub-a", "sub-b", "sub-c")
                }
                DescendantManagementGroupIds = @()
            }
        }

        {
            Add-Scope `
                -ScopeKind managementGroup `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true }
        } | Should -Throw "*does not have an exact alert member*missing: sub-c*"

        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Not -Match "activityLogAlerts.*properties.enabled=true"
    }
}

Describe "scope listing and overlap detection" {
    It "rejects a receiver that does not use Common Alert Schema" {
        $central = New-TestCentral
        $actionGroup = [pscustomobject]@{
            properties = [pscustomobject]@{
                webhookReceivers = @(
                    [pscustomobject]@{
                        serviceUri = $central.WebhookUri
                        useAadAuth = $true
                        useCommonAlertSchema = $false
                        tenantId = $central.TenantId
                        objectId = $central.SecureWebhookObjectId
                        identifierUri = $central.SecureWebhookIdentifierUri
                    }
                )
            }
        }

        Test-SameWebhookReceiver -ActionGroup $actionGroup -Central $central |
            Should -BeFalse
    }

    It "discovers the Azure CLI flattened Activity Log Alert shape" {
        $central = New-TestCentral -SubscriptionIds @("sub-central")
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $commandLine = $Arguments -join " "
            if ($commandLine -match "^monitor activity-log alert list") {
                return @(
                    [pscustomobject]@{
                        id = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/activityLogAlerts/ala-test-service-health"
                        tags = @{
                            workload = "azure-service-health-slack-bot"
                            "azd-env-name" = "test"
                        }
                        enabled = $true
                        scopes = @("/subscriptions/sub-central")
                        condition = [pscustomobject]@{
                            allOf = @(
                                [pscustomobject]@{
                                    field = "category"
                                    equals = "ServiceHealth"
                                }
                            )
                        }
                        actions = [pscustomobject]@{
                            actionGroups = @(
                                [pscustomobject]@{
                                    actionGroupId = "/subscriptions/sub-central/resourceGroups/rg-test/providers/Microsoft.Insights/actionGroups/ag-test-service-health"
                                }
                            )
                        }
                    },
                    [pscustomobject]@{
                        id = "/subscriptions/sub-a/resourceGroups/rg-alerts/providers/Microsoft.Insights/activityLogAlerts/ala-subscription-sub-a"
                        tags = @{
                            workload = "azure-service-health-slack-bot"
                            "azd-env-name" = "test"
                            "service-health-managed-by" = "manage-alert-scopes"
                            "service-health-scope-kind" = "subscription"
                            "service-health-scope-id" = "sub-a"
                            "service-health-member-subscription" = "sub-a"
                        }
                        enabled = $true
                        scopes = @("/subscriptions/sub-a")
                        condition = [pscustomobject]@{
                            allOf = @(
                                [pscustomobject]@{
                                    field = "category"
                                    equals = "ServiceHealth"
                                }
                            )
                        }
                        actions = [pscustomobject]@{
                            actionGroups = @(
                                [pscustomobject]@{
                                    actionGroupId = "/subscriptions/sub-a/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-subscription-sub-a"
                                }
                            )
                        }
                    }
                )
            }
            if ($commandLine -match "^resource show --ids .*actionGroups") {
                return [pscustomobject]@{
                    properties = [pscustomobject]@{
                        enabled = $true
                        webhookReceivers = @(
                            [pscustomobject]@{
                                serviceUri = "https://ca-test.example/api/service-health"
                                useAadAuth = $true
                                useCommonAlertSchema = $true
                                tenantId = "tenant-a"
                                objectId = "object-id"
                                identifierUri = "api://client-id"
                            }
                        )
                    }
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return @()
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        $scopes = @(Get-ManagedScopes -Central $central)

        $scopes.Count | Should -Be 1
        $scopes[0].ScopeKind | Should -Be "subscription"
        $scopes[0].ScopeId | Should -Be "sub-a"
        $scopes[0].Enabled | Should -BeTrue
        $scopes[0].ActionGroupEnabled | Should -BeTrue
    }

    It "excludes the protected baseline even if its ownership tag is malformed" {
        $central = New-TestCentral -SubscriptionIds @("sub-central")
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $commandLine = $Arguments -join " "
            if ($commandLine -match "^monitor activity-log alert list") {
                return [pscustomobject]@{
                    id = $central.ProtectedAlertId
                    tags = @{
                        workload = "azure-service-health-slack-bot"
                        "azd-env-name" = "test"
                        "service-health-managed-by" = "manage-alert-scopes"
                    }
                    enabled = $true
                    scopes = @("/subscriptions/sub-central")
                    actions = [pscustomobject]@{
                        actionGroups = @(
                            [pscustomobject]@{
                                actionGroupId = $central.AnchorActionGroupId
                            }
                        )
                    }
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return @()
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        @(Get-ManagedScopes -Central $central).Count | Should -Be 0
        ($script:AzCalls -join " ") | Should -Not -Match "resource show"
    }

    It "reports tenant, resource IDs, enabled state, coverage, and overlap" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($individual, $managementGroup)

        $report = @(Get-ScopeReport -Context $context)
        $subscriptionRow = $report | Where-Object ScopeKind -eq subscription

        $subscriptionRow.TenantId | Should -Be "tenant-a"
        $subscriptionRow.AlertId | Should -Match "activityLogAlerts"
        $subscriptionRow.ActionGroupId | Should -Match "actionGroups"
        $subscriptionRow.Enabled | Should -BeTrue
        $subscriptionRow.EffectiveCoverage | Should -Be "Covered"
        $subscriptionRow.CoveredSubscriptionIds | Should -Be @("sub-a")
        $subscriptionRow.Overlap | Should -Be "Duplicate with MG: mg-root"
    }

    It "aggregates descendant member alerts into one logical Management Group scope" {
        $central = New-TestCentral -SubscriptionIds @("sub-a", "sub-b")
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $commandLine = $Arguments -join " "
            if ($commandLine -match "^monitor activity-log alert list") {
                $subscriptionId = $Arguments[
                    [array]::IndexOf($Arguments, "--subscription") + 1
                ]
                return [pscustomobject]@{
                    id = "/subscriptions/$subscriptionId/resourceGroups/rg-alerts/providers/Microsoft.Insights/activityLogAlerts/ala-mg-root"
                    tags = @{
                        workload = "azure-service-health-slack-bot"
                        "azd-env-name" = "test"
                        "service-health-managed-by" = "manage-alert-scopes"
                        "service-health-scope-kind" = "managementGroup"
                        "service-health-scope-id" = "mg-root"
                        "service-health-member-subscription" = $subscriptionId
                    }
                    enabled = $true
                    scopes = @("/subscriptions/$subscriptionId")
                    condition = [pscustomobject]@{
                        allOf = @(
                            [pscustomobject]@{
                                field = "category"
                                equals = "ServiceHealth"
                            }
                        )
                    }
                    actions = [pscustomobject]@{
                        actionGroups = @(
                            [pscustomobject]@{
                                actionGroupId = "/subscriptions/$subscriptionId/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-mg-root"
                            }
                        )
                    }
                }
            }
            if ($commandLine -match "^resource show --ids .*actionGroups") {
                return [pscustomobject]@{
                    properties = [pscustomobject]@{
                        enabled = $true
                        webhookReceivers = @(
                            [pscustomobject]@{
                                serviceUri = $central.WebhookUri
                                useAadAuth = $true
                                useCommonAlertSchema = $true
                                tenantId = $central.TenantId
                                objectId = $central.SecureWebhookObjectId
                                identifierUri = $central.SecureWebhookIdentifierUri
                            }
                        )
                    }
                }
            }
            if ($commandLine -match "^monitor action-group list") {
                return @()
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        $scopes = @(Get-ManagedScopes -Central $central)

        $scopes.Count | Should -Be 1
        $scopes[0].ScopeKind | Should -Be "managementGroup"
        $scopes[0].ScopeId | Should -Be "mg-root"
        $scopes[0].MemberSubscriptionIds | Should -Be @("sub-a", "sub-b")
        $scopes[0].AlertId.Count | Should -Be 2
        $scopes[0].Enabled | Should -BeTrue
        $scopes[0].ActionGroupEnabled | Should -BeTrue
    }

    It "discovers an orphaned manager Action Group for portable cleanup" {
        $central = New-TestCentral -SubscriptionIds @("sub-a")
        Mock Invoke-AzCommand {
            param([string[]] $Arguments)

            $script:AzCalls += , @($Arguments)
            $commandLine = $Arguments -join " "
            if ($commandLine -match "^monitor activity-log alert list") {
                return @()
            }
            if ($commandLine -match "^monitor action-group list") {
                return [pscustomobject]@{
                    id = "/subscriptions/sub-a/resourceGroups/rg-alerts/providers/Microsoft.Insights/actionGroups/ag-sub-a"
                    tags = @{
                        workload = "azure-service-health-slack-bot"
                        "azd-env-name" = "test"
                        "service-health-managed-by" = "manage-alert-scopes"
                        "service-health-scope-kind" = "subscription"
                        "service-health-scope-id" = "sub-a"
                        "service-health-member-subscription" = "sub-a"
                    }
                    enabled = $true
                }
            }
            if ($commandLine -match "^account show --subscription") {
                return [pscustomobject]@{
                    id = "sub-a"
                    tenantId = "tenant-a"
                    state = "Enabled"
                }
            }
            throw "Unexpected mocked Azure CLI call: $commandLine"
        }

        $scopes = @(Get-ManagedScopes -Central $central)
        $context = [pscustomobject]@{
            Central = $central
            Scopes = $scopes
            ManagementGroupCache = @{}
        }
        $row = @(Get-ScopeReport -Context $context)[0]

        $scopes.Count | Should -Be 1
        $scopes[0].OrphanedActionGroup | Should -BeTrue
        $scopes[0].AlertId | Should -BeNullOrEmpty
        $row.EffectiveCoverage | Should -Be "Disabled"
        $row.CoverageDetail | Should -Match "orphaned Action Group"
    }

    It "reports incomplete logical Management Group membership as ineffective coverage" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $managementGroup.Members = @($managementGroup.Members | Where-Object {
            $_.MemberSubscriptionId -eq "sub-a"
        })
        $managementGroup.MemberSubscriptionIds = @("sub-a")
        $managementGroup.AlertId = @($managementGroup.Members.AlertId)
        $managementGroup.ActionGroupId = @($managementGroup.Members.ActionGroupId)
        $context = New-TestContext -Scopes @($managementGroup)

        $row = @(Get-ScopeReport -Context $context)[0]

        $row.EffectiveCoverage | Should -Be "Incomplete"
        $row.CoverageDetail | Should -Match "missing members: sub-b"
        $row.CoveredSubscriptionIds | Should -Be @("sub-a")
    }

    It "reports duplicate Management Group members as incomplete instead of aborting list" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $duplicate = $managementGroup.Members[0].PSObject.Copy()
        $duplicate.AlertId = "$($duplicate.AlertId)-duplicate"
        $managementGroup.Members = @($managementGroup.Members) + @($duplicate)
        $managementGroup.MemberSubscriptionIds = @(
            $managementGroup.Members.MemberSubscriptionId
        )
        $managementGroup.AlertId = @($managementGroup.Members.AlertId)
        $managementGroup.ActionGroupId = @($managementGroup.Members.ActionGroupId)
        $context = New-TestContext -Scopes @($managementGroup)

        $row = @(Get-ScopeReport -Context $context)[0]

        $row.EffectiveCoverage | Should -Be "Incomplete"
        $row.CoverageDetail | Should -Match "duplicate or blank member IDs"
        $row.CoveredSubscriptionIds | Should -Be @("sub-a", "sub-b")
    }

    It "fails closed when Management Group descendants are inaccessible" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($managementGroup)
        $script:MockDescendants = @("sub-a", "sub-hidden")

        { Get-ScopeReport -Context $context } |
            Should -Throw "*coverage*cannot be proven*sub-hidden*"
    }

    It "reports nested Management Group overlap even when the child has no subscriptions" {
        $parent = New-TestScope -Kind managementGroup -Id mg-parent
        $child = New-TestScope -Kind managementGroup -Id mg-child
        $context = New-TestContext -Scopes @($parent, $child)
        $script:MockDescendants = @()
        $script:MockDescendantManagementGroups = @("mg-child")

        $report = @(Get-ScopeReport -Context $context)
        $parentRow = $report | Where-Object ScopeId -eq mg-parent

        $parentRow.Overlap | Should -Be "Management Groups: mg-child"
    }
}

Describe "destructive scope operations" {
    It "removes an individual subscription only when replacement coverage exists" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($individual, $managementGroup)

        $result = Remove-SubscriptionScope `
            -ScopeId sub-a `
            -Context $context `
            -ShouldProcess { $true } `
            -ConfirmDestructive { $true }

        $result.Status | Should -Be "Removed"
        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Match "resource delete --ids .*activityLogAlerts"
    }

    It "rejects removal that would create a coverage gap" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $context = New-TestContext -Scopes @($individual)

        {
            Remove-SubscriptionScope `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*coverage gap*"

        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "rejects incomplete Management Group membership as replacement coverage" {
        $individual = New-TestScope -Kind subscription -Id sub-b -SubscriptionId sub-b
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $managementGroup.Members = @($managementGroup.Members | Where-Object {
            $_.MemberSubscriptionId -eq "sub-a"
        })
        $managementGroup.MemberSubscriptionIds = @("sub-a")
        $managementGroup.AlertId = @($managementGroup.Members.AlertId)
        $managementGroup.ActionGroupId = @($managementGroup.Members.ActionGroupId)
        $context = New-TestContext -Scopes @($individual, $managementGroup)

        {
            Remove-SubscriptionScope `
                -ScopeId sub-b `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*does not have an exact alert member*missing: sub-b*"

        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "honors explicit cancellation" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($individual, $managementGroup)

        $result = Remove-SubscriptionScope `
            -ScopeId sub-a `
            -Context $context `
            -ShouldProcess { $true } `
            -ConfirmDestructive { $false }

        $result.Status | Should -Be "Cancelled"
        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "honors WhatIf without prompting or mutating" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($individual, $managementGroup)
        $script:Prompted = $false

        $result = Remove-SubscriptionScope `
            -ScopeId sub-a `
            -Context $context `
            -ShouldProcess { $false } `
            -ConfirmDestructive { $script:Prompted = $true; $true }

        $result.Status | Should -Be "Planned"
        $script:Prompted | Should -BeFalse
        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "rechecks coverage after confirmation and aborts when it changed" {
        $individual = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $context = New-TestContext -Scopes @($individual, $managementGroup)
        Mock Refresh-ScopeContext {
            param($Context)
            $Context.Scopes = @($individual)
            $Context.ManagementGroupCache = @{}
        }

        {
            Remove-SubscriptionScope `
                -ScopeId sub-a `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*Coverage changed after confirmation*"

        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "removes a Management Group only after every descendant has replacement coverage" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $subB = New-TestScope -Kind subscription -Id sub-b -SubscriptionId sub-b
        $context = New-TestContext -Scopes @($managementGroup, $subA, $subB)

        $result = Remove-ManagementGroupScope `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $true } `
            -ConfirmDestructive { $true }

        $result.Status | Should -Be "Removed"
    }

    It "refuses to delete the baseline alert or anchor Action Group even with forged manager metadata" {
        $central = New-TestCentral
        $context = New-TestContext
        $baseline = New-TestScope -Kind subscription -Id sub-central
        $baseline.AlertId = $central.ProtectedAlertId
        $baseline.ActionGroupId = $central.AnchorActionGroupId

        {
            Remove-ScopeResources -ScopeState $baseline -Context $context
        } | Should -Throw "*azd-owned central baseline*"

        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "refuses to delete a scope without authoritative manager ownership" {
        $scope = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $scope.ManagedBy = "azd"
        $context = New-TestContext

        {
            Remove-ScopeResources -ScopeState $scope -Context $context
        } | Should -Throw "*not owned by the day-2 scope manager*"

        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "deletes a rediscovered orphan Action Group without a null alert delete" {
        $orphan = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $orphan.AlertId = $null
        $orphan.Enabled = $false
        $orphan | Add-Member -NotePropertyName OrphanedActionGroup -NotePropertyValue $true
        $context = New-TestContext -Scopes @($orphan)

        Remove-ScopeResources -ScopeState $orphan -Context $context

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        @($calls | Where-Object { $_ -match "^resource delete" }).Count |
            Should -Be 1
        ($calls -join "`n") | Should -Match "resource delete --ids .*actionGroups"
        ($calls -join "`n") | Should -Not -Match "resource delete --ids\\s*$"
    }
}

Describe "Management Group migration" {
    It "tests a disabled Management Group path, enables it, then removes overlaps" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $subB = New-TestScope -Kind subscription -Id sub-b -SubscriptionId sub-b
        $context = New-TestContext -Scopes @($subA, $subB)

        $result = Invoke-ManagementGroupMigration `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $true } `
            -ConfirmDestructive { $true }

        $result.Status | Should -Be "Migrated"
        $result.RemovedSubscriptions | Should -Be @("sub-a", "sub-b")
        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        $enableIndex = [array]::FindIndex(
            [string[]]$calls,
            [Predicate[string]] { param($line) $line -match "resource update.*enabled=true" }
        )
        $deleteIndex = [array]::FindIndex(
            [string[]]$calls,
            [Predicate[string]] { param($line) $line -match "resource delete.*activityLogAlerts" }
        )
        $enableIndex | Should -BeGreaterOrEqual 0
        $deleteIndex | Should -BeGreaterThan $enableIndex
    }

    It "keeps the validated Management Group alert disabled when migration is cancelled" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $context = New-TestContext -Scopes @($subA)

        $result = Invoke-ManagementGroupMigration `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $true } `
            -ConfirmDestructive { $false }

        $result.Status | Should -Be "Cancelled"
        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Not -Match "resource update.*properties.enabled=true"
        ($script:AzCalls -join " ") | Should -Not -Match "resource delete"
    }

    It "reports validated disabled when no-overlap activation is declined" {
        $context = New-TestContext
        $script:ShouldProcessCount = 0

        $result = Invoke-ManagementGroupMigration `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess {
                $script:ShouldProcessCount++
                return $script:ShouldProcessCount -eq 1
            } `
            -ConfirmDestructive { throw "must not prompt" }

        $result.Status | Should -Be "ValidatedDisabled"
        (($script:AzCalls | ForEach-Object { $_ -join " " }) -join "`n") |
            Should -Not -Match "activityLogAlerts.*properties.enabled=true|resource delete"
    }

    It "retries cleanup of a rediscovered orphan during migration" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $orphan = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $orphan.AlertId = $null
        $orphan.Enabled = $false
        $orphan | Add-Member -NotePropertyName OrphanedActionGroup -NotePropertyValue $true
        $context = New-TestContext -Scopes @($managementGroup, $orphan)

        $result = Invoke-ManagementGroupMigration `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $true } `
            -ConfirmDestructive { $true }

        $result.Status | Should -Be "Migrated"
        $result.RemovedSubscriptions | Should -Be @("sub-a")
        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        @($calls | Where-Object { $_ -match "^resource delete" }).Count |
            Should -Be 1
        ($calls -join "`n") | Should -Match "resource delete --ids .*actionGroups"
        ($calls -join "`n") | Should -Not -Match "resource delete --ids\\s*$"
    }

    It "retains an orphan when replacement coverage disables before cleanup" {
        $managementGroup = New-TestScope -Kind managementGroup -Id mg-root
        $orphan = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $orphan.AlertId = $null
        $orphan.Enabled = $false
        $orphan | Add-Member -NotePropertyName OrphanedActionGroup -NotePropertyValue $true
        $context = New-TestContext -Scopes @($managementGroup, $orphan)
        $script:ActionGroupReadCount = 0
        Mock Get-CurrentActionGroupEnabled {
            $script:ActionGroupReadCount++
            return $script:ActionGroupReadCount -lt 2
        }

        {
            Invoke-ManagementGroupMigration `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*orphaned Action Group was retained*no original alert exists*"

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        ($calls -join "`n") | Should -Not -Match "resource delete|--ids\\s+--api-version"
    }

    It "plans migration under WhatIf without creating or deleting resources" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $context = New-TestContext -Scopes @($subA)

        $result = Invoke-ManagementGroupMigration `
            -ScopeId mg-root `
            -Context $context `
            -ShouldProcess { $false } `
            -ConfirmDestructive { throw "must not prompt" }

        $result.Status | Should -Be "Planned"
        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create|resource delete|test-notifications"
    }

    It "rejects migration when the Management Group contains baseline coverage" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $context = New-TestContext -Scopes @($subA)
        $script:MockDescendants = @("sub-central", "sub-a")

        {
            Invoke-ManagementGroupMigration `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*overlaps the immutable azd-owned baseline alert*"

        ($script:AzCalls -join " ") | Should -Not -Match "deployment sub create|resource delete"
    }

    It "restores the original alert when a descendant handoff cannot be enabled" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $subB = New-TestScope -Kind subscription -Id sub-b -SubscriptionId sub-b
        $context = New-TestContext -Scopes @($subA, $subB)
        Mock Invoke-AzCommand {
            throw "simulated enable failure"
        } -ParameterFilter {
            ($Arguments -join " ") -match (
                "^resource update --ids /subscriptions/sub-b/.+" +
                "activityLogAlerts/ala-managementGroup-mg-root.*" +
                "properties.enabled=true"
            )
        }
        Mock Get-CurrentAlertEnabled { return $false }

        {
            Invoke-ManagementGroupMigration `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*original alert was re-enabled*"

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-b/.+" +
            "activityLogAlerts/ala-subscription-sub-b.*" +
            "properties.enabled=false"
        )
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-b/.+" +
            "activityLogAlerts/ala-subscription-sub-b.*" +
            "properties.enabled=true"
        )
        ($calls -join "`n") | Should -Not -Match "resource delete"
    }

    It "keeps the original disabled when an uncertain replacement is proven enabled" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $subB = New-TestScope -Kind subscription -Id sub-b -SubscriptionId sub-b
        $context = New-TestContext -Scopes @($subA, $subB)
        Mock Invoke-AzCommand {
            throw "simulated uncertain enable response"
        } -ParameterFilter {
            ($Arguments -join " ") -match (
                "^resource update --ids /subscriptions/sub-b/.+" +
                "activityLogAlerts/ala-managementGroup-mg-root.*" +
                "properties.enabled=true"
            )
        }
        Mock Get-CurrentAlertEnabled { return $true }

        {
            Invoke-ManagementGroupMigration `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*Replacement alert is enabled*original alert remains disabled*"

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-b/.+" +
            "activityLogAlerts/ala-subscription-sub-b.*" +
            "properties.enabled=false"
        )
        ($calls -join "`n") | Should -Not -Match (
            "activityLogAlerts/ala-subscription-sub-b.*" +
            "properties.enabled=true|resource delete"
        )
    }

    It "restores the original when replacement Action Group disables before deletion" {
        $subA = New-TestScope -Kind subscription -Id sub-a -SubscriptionId sub-a
        $subB = New-TestScope -Kind subscription -Id sub-b -SubscriptionId sub-b
        $context = New-TestContext -Scopes @($subA, $subB)
        $script:ActionGroupReadCount = 0
        Mock Get-CurrentActionGroupEnabled {
            $script:ActionGroupReadCount++
            return $script:ActionGroupReadCount -lt 3
        }

        {
            Invoke-ManagementGroupMigration `
                -ScopeId mg-root `
                -Context $context `
                -ShouldProcess { $true } `
                -ConfirmDestructive { $true }
        } | Should -Throw "*Replacement coverage became inactive*original alert was restored*"

        $calls = @($script:AzCalls | ForEach-Object { $_ -join " " })
        ($calls -join "`n") | Should -Match (
            "resource update --ids /subscriptions/sub-a/.+" +
            "activityLogAlerts/ala-subscription-sub-a.*" +
            "properties.enabled=true"
        )
        ($calls -join "`n") | Should -Not -Match "resource delete"
    }
}
}
