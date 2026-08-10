Describe "configure-secure-webhook.ps1 compatibility wrapper" {
    BeforeAll {
        $scriptPath = Join-Path $PSScriptRoot "..\scripts\configure-secure-webhook.ps1"
        $content = Get-Content $scriptPath -Raw
    }

    It "delegates all business logic to the canonical Python configurator" {
        $content | Should -Match "configure_secure_webhook\.py"
        $content | Should -Match '& \$python\.Source @arguments'
    }

    It "forwards legacy parameters without Azure or Graph business logic" {
        $content | Should -Match '"--display-name"'
        $content | Should -Match '"--azns-application-id"'
        $content | Should -Match '"--role-name"'
        $content | Should -Not -Match "\baz\s+(account|rest)"
        $content | Should -Not -Match "\bazd\s+env\s+set"
        $content | Should -Not -Match "graph\.microsoft\.com"
    }
}
