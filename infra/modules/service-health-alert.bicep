param environmentName string
param webhookUri string
param secureWebhookObjectId string
param secureWebhookIdentifierUri string
param tenantId string
param targetSubscriptionId string

param tags object

@description('Optional suffix for day-2 scope resources. Empty preserves the original azd resource names.')
param resourceSuffix string = ''

@description('Whether the Activity Log Alert is enabled when deployed.')
param alertEnabled bool = true

var nameSuffix = empty(resourceSuffix) ? '' : '-${resourceSuffix}'

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${environmentName}-service-health${nameSuffix}'
  location: 'Global'
  tags: tags
  properties: {
    groupShortName: 'SvcHealth'
    enabled: true
    webhookReceivers: [
      {
        name: 'slack-service-health'
        serviceUri: webhookUri
        useCommonAlertSchema: true
        useAadAuth: true
        objectId: secureWebhookObjectId
        identifierUri: secureWebhookIdentifierUri
        tenantId: tenantId
      }
    ]
  }
}

resource activityLogAlert 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {
  name: 'ala-${environmentName}-service-health${nameSuffix}'
  location: 'Global'
  tags: tags
  properties: {
    enabled: alertEnabled
    scopes: [
      '/subscriptions/${targetSubscriptionId}'
    ]
    condition: {
      allOf: [
        {
          field: 'category'
          equals: 'ServiceHealth'
        }
      ]
    }
    actions: {
      actionGroups: [
        {
          actionGroupId: actionGroup.id
        }
      ]
    }
  }
}

output actionGroupId string = actionGroup.id
output activityLogAlertId string = activityLogAlert.id
output actionGroupName string = actionGroup.name
output activityLogAlertName string = activityLogAlert.name
