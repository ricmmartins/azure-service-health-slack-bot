param environmentName string
param webhookUri string
param secureWebhookObjectId string
param secureWebhookIdentifierUri string
param tenantId string
param targetSubscriptionId string

@description('''
Optional Management Group ID. When set, the Activity Log Alert is scoped to
this management group instead of a single subscription, so Service Health
events from every subscription under it are captured by one deployment.
Deploying this way requires the principal running `azd provision`/`az
deployment` to hold at least Monitoring Contributor (or Contributor) on the
management group, in addition to the usual subscription-level permissions
for the rest of the stack.
''')
param managementGroupId string = ''

param tags object

var alertScopes = empty(managementGroupId)
  ? ['/subscriptions/${targetSubscriptionId}']
  : ['/providers/Microsoft.Management/managementGroups/${managementGroupId}']

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${environmentName}-service-health'
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
  name: 'ala-${environmentName}-service-health'
  location: 'Global'
  tags: tags
  properties: {
    enabled: true
    scopes: alertScopes
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
