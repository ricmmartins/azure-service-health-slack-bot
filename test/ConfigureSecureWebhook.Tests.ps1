Describe "configure-secure-webhook.ps1 caller owner resolution" {
    BeforeAll {
        # Dot-sourcing must expose the helper functions without executing the
        # imperative provisioning body (which would call az / Microsoft Graph).
        . (Join-Path $PSScriptRoot "..\scripts\configure-secure-webhook.ps1")

        function New-UserAccount {
            param([string] $Type = "user", [string] $Name = "alice@contoso.com")
            return [pscustomobject]@{
                tenantId = "tenant-a"
                user = [pscustomobject]@{ type = $Type; name = $Name }
            }
        }

        function New-ServicePrincipalAccount {
            param([string] $Type = "servicePrincipal", [string] $Name = "11111111-1111-1111-1111-111111111111")
            return [pscustomobject]@{
                tenantId = "tenant-a"
                user = [pscustomobject]@{ type = $Type; name = $Name }
            }
        }

        # Builds a Graph invoker scriptblock that records every call and returns
        # canned responses, so no real Graph traffic occurs.
        function New-RecordingInvoker {
            param(
                [string] $MeId = "user-object-id",
                [string[]] $ServicePrincipalIds = @("sp-object-id")
            )

            $calls = [System.Collections.Generic.List[object]]::new()
            $invoker = {
                param([string] $Method, [string] $Uri)
                $calls.Add([pscustomobject]@{ Method = $Method; Uri = $Uri })
                if ($Uri -like "*/me*") {
                    return [pscustomobject]@{ id = $MeId }
                }
                if ($Uri -like "*servicePrincipals*") {
                    return [pscustomobject]@{
                        value = @($ServicePrincipalIds | ForEach-Object {
                            [pscustomobject]@{ id = $_ }
                        })
                    }
                }
                return $null
            }.GetNewClosure()

            return [pscustomobject]@{ Invoker = $invoker; Calls = $calls }
        }
    }

    Context "delegated user context" {
        It "resolves the caller via Graph /me" {
            $rec = New-RecordingInvoker -MeId "delegated-user-id"
            $id = Resolve-CallerOwnerObjectId -Account (New-UserAccount) -GraphInvoker $rec.Invoker

            $id | Should -Be "delegated-user-id"
            $rec.Calls.Count | Should -Be 1
            $rec.Calls[0].Uri | Should -BeLike "*graph.microsoft.com/v1.0/me*"
            # /me must never be used to look up service principals in this path.
            ($rec.Calls | Where-Object { $_.Uri -like "*servicePrincipals*" }) | Should -BeNullOrEmpty
        }

        It "produces the expected owner directoryObjects reference" {
            $rec = New-RecordingInvoker -MeId "delegated-user-id"
            $id = Resolve-CallerOwnerObjectId -Account (New-UserAccount) -GraphInvoker $rec.Invoker
            $ownerRef = "https://graph.microsoft.com/v1.0/directoryObjects/$id"
            $ownerRef | Should -Be "https://graph.microsoft.com/v1.0/directoryObjects/delegated-user-id"
        }

        It "accepts alternate casing for the user type" {
            $rec = New-RecordingInvoker -MeId "delegated-user-id"
            $id = Resolve-CallerOwnerObjectId -Account (New-UserAccount -Type "USER") -GraphInvoker $rec.Invoker
            $id | Should -Be "delegated-user-id"
        }

        It "fails clearly when /me returns no id" {
            $invoker = { param([string] $Method, [string] $Uri) return [pscustomobject]@{ id = $null } }
            { Resolve-CallerOwnerObjectId -Account (New-UserAccount) -GraphInvoker $invoker } |
                Should -Throw "*Graph /me returned no object id*"
        }
    }

    Context "service principal / app-only context" {
        It "resolves the caller by client id via servicePrincipals filter (never /me)" {
            $rec = New-RecordingInvoker -ServicePrincipalIds @("app-only-sp-id")
            $account = New-ServicePrincipalAccount -Name "22222222-2222-2222-2222-222222222222"
            $id = Resolve-CallerOwnerObjectId -Account $account -GraphInvoker $rec.Invoker

            $id | Should -Be "app-only-sp-id"
            ($rec.Calls | Where-Object { $_.Uri -like "*/me*" }) | Should -BeNullOrEmpty
            $spCall = $rec.Calls | Where-Object { $_.Uri -like "*servicePrincipals*" } | Select-Object -First 1
            $spCall | Should -Not -BeNullOrEmpty
            $spCall.Uri | Should -BeLike "*appId eq '22222222-2222-2222-2222-222222222222'*"
        }

        It "produces the expected owner directoryObjects reference" {
            $rec = New-RecordingInvoker -ServicePrincipalIds @("app-only-sp-id")
            $id = Resolve-CallerOwnerObjectId -Account (New-ServicePrincipalAccount) -GraphInvoker $rec.Invoker
            $ownerRef = "https://graph.microsoft.com/v1.0/directoryObjects/$id"
            $ownerRef | Should -Be "https://graph.microsoft.com/v1.0/directoryObjects/app-only-sp-id"
        }

        It "accepts alternate casing for the service principal type" {
            $rec = New-RecordingInvoker -ServicePrincipalIds @("app-only-sp-id")
            $id = Resolve-CallerOwnerObjectId -Account (New-ServicePrincipalAccount -Type "ServicePrincipal") -GraphInvoker $rec.Invoker
            $id | Should -Be "app-only-sp-id"
        }

        It "fails clearly when no matching service principal exists" {
            $rec = New-RecordingInvoker -ServicePrincipalIds @()
            { Resolve-CallerOwnerObjectId -Account (New-ServicePrincipalAccount) -GraphInvoker $rec.Invoker } |
                Should -Throw "*no service principal found*"
        }

        It "fails clearly when resolution is ambiguous" {
            $rec = New-RecordingInvoker -ServicePrincipalIds @("sp-1", "sp-2")
            { Resolve-CallerOwnerObjectId -Account (New-ServicePrincipalAccount) -GraphInvoker $rec.Invoker } |
                Should -Throw "*ambiguous service principal resolution*"
        }

        It "fails clearly when the client id is missing" {
            $account = New-ServicePrincipalAccount -Name ""
            $rec = New-RecordingInvoker
            { Resolve-CallerOwnerObjectId -Account $account -GraphInvoker $rec.Invoker } |
                Should -Throw "*client id* is empty*"
        }
    }

    Context "unknown context" {
        It "refuses to guess for an unsupported user type" {
            $account = [pscustomobject]@{ user = [pscustomobject]@{ type = "managedIdentity"; name = "x" } }
            $rec = New-RecordingInvoker
            { Resolve-CallerOwnerObjectId -Account $account -GraphInvoker $rec.Invoker } |
                Should -Throw "*unsupported Azure CLI account user type*"
        }
    }
}
