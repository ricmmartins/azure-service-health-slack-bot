Describe "manage-alert-scopes.ps1 compatibility wrapper" {
    BeforeAll {
        $scriptPath = Join-Path $PSScriptRoot "..\scripts\manage-alert-scopes.ps1"
        $content = Get-Content $scriptPath -Raw
    }

    It "delegates all business logic to the canonical Python CLI" {
        $content | Should -Match "manage_alert_scopes\.py"
        $content | Should -Match '\$raw = & \$python\.Source @arguments'
        $content | Should -Match 'ConvertFrom-Json -Depth 100'
    }

    It "forwards compatibility switches without Azure business logic" {
        $content | Should -Match '"--subscription-id"'
        $content | Should -Match '"--management-group-id"'
        $content | Should -Match '"--environment-name"'
        $content | Should -Match '"--force"'
        $content | Should -Match '"--json"'
        $content | Should -Match '"--what-if"'
        $content | Should -Match '\$PSCmdlet\.ShouldProcess'
        $content | Should -Not -Match "\baz\s+(account|resource|rest|monitor|deployment)"
    }
}
