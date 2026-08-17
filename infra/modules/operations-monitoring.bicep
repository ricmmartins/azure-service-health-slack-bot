param environmentName string
param location string
param keyVaultName string
param storageAccountName string
param logAnalyticsId string
param applicationInsightsId string
param containerAppName string
param containerAppFqdn string
param deployWorkload bool
param operationsActionGroupId string
param tags object

var storageDnsSuffix = environment().suffixes.storage

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' existing = if (deployWorkload) {
  name: containerAppName
}

resource keyVaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-audit-to-${environmentName}'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsId
    logs: [
      {
        category: 'AuditEvent'
        enabled: true
      }
    ]
  }
}

resource tableDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-audit-to-${environmentName}'
  scope: tableService
  properties: {
    workspaceId: logAnalyticsId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

resource healthWebTest 'Microsoft.Insights/webTests@2022-06-15' = if (deployWorkload) {
  name: 'webtest-${environmentName}-health'
  location: location
  kind: 'standard'
  tags: union(tags, {
    'hidden-link:${applicationInsightsId}': 'Resource'
  })
  properties: {
    SyntheticMonitorId: 'webtest-${environmentName}-health'
    Name: 'webtest-${environmentName}-health'
    Description: 'Production availability check. Runbook: docs/deployment-and-operations.md#operations'
    Enabled: true
    Frequency: 300
    Timeout: 30
    Kind: 'standard'
    RetryEnabled: true
    Locations: [
      {
        Id: 'us-tx-sn1-azr'
      }
    ]
    Request: {
      RequestUrl: 'https://${containerAppFqdn}/healthz'
      HttpVerb: 'GET'
      ParseDependentRequests: false
      FollowRedirects: false
    }
    ValidationRules: {
      ExpectedHttpStatusCode: 200
      IgnoreHttpStatusCode: false
      SSLCheck: true
      SSLCertRemainingLifetimeCheck: 7
    }
  }
}

resource webhookFailureAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (deployWorkload && !empty(operationsActionGroupId)) {
  name: 'alert-${environmentName}-webhook-5xx'
  location: location
  tags: tags
  properties: {
    displayName: 'Service Health webhook processing failures'
    description: 'Runbook: docs/deployment-and-operations.md#operations'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      applicationInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'union (AppRequests | where Url endswith "/api/service-health" | where Success == false and ResultCode startswith "5" | project TimeGenerated), (AppTraces | where Message == "Permanent Service Health processing failure" | project TimeGenerated)'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        operationsActionGroupId
      ]
    }
  }
}

resource dependencyFailureAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (deployWorkload && !empty(operationsActionGroupId)) {
  name: 'alert-${environmentName}-dependency-failures'
  location: location
  tags: tags
  properties: {
    displayName: 'Sustained Slack or Table dependency failures'
    description: 'Runbook: docs/deployment-and-operations.md#operations'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      applicationInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'AppDependencies | where Success == false | where Target has "slack.com" or Target has "${storageDnsSuffix}"'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 3
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        operationsActionGroupId
      ]
    }
  }
}

resource availabilityFailureAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (deployWorkload && !empty(operationsActionGroupId)) {
  name: 'alert-${environmentName}-availability'
  location: location
  tags: tags
  properties: {
    displayName: 'Service Health bot availability failures'
    description: 'Runbook: docs/deployment-and-operations.md#operations'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [
      applicationInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'AppAvailabilityResults | where Name == "webtest-${environmentName}-health" | where Success == false'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 2
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        operationsActionGroupId
      ]
    }
  }
  dependsOn: [
    healthWebTest
  ]
}

resource replicaAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = if (deployWorkload && !empty(operationsActionGroupId)) {
  name: 'alert-${environmentName}-replicas'
  location: 'Global'
  tags: tags
  properties: {
    description: 'No running Container App replicas. Runbook: docs/deployment-and-operations.md#operations'
    severity: 0
    enabled: true
    scopes: [
      containerApp.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'RunningReplicas'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.App/containerApps'
          metricName: 'Replicas'
          operator: 'LessThan'
          threshold: 1
          timeAggregation: 'Average'
        }
      ]
    }
    actions: [
      {
        actionGroupId: operationsActionGroupId
      }
    ]
    autoMitigate: true
  }
}
