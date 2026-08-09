targetScope = 'subscription'

@minLength(1)
param environmentName string

param location string
param webhookUri string
param secureWebhookObjectId string
param secureWebhookIdentifierUri string
param tenantId string

@allowed([
  'subscription'
  'managementGroup'
])
param scopeKind string

param scopeId string
param targetSubscriptionId string
param centralSubscriptionId string
param resourceSuffix string
param alertEnabled bool = false

var resourceGroupName = 'rg-${take(environmentName, 40)}-alerts-${resourceSuffix}'
var tags = {
  'azd-env-name': environmentName
  workload: 'azure-service-health-slack-bot'
  'service-health-managed-by': 'manage-alert-scopes'
  'service-health-central-subscription': centralSubscriptionId
  'service-health-scope-kind': scopeKind
  'service-health-scope-id': scopeId
  'service-health-member-subscription': targetSubscriptionId
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module serviceHealthAlert '../modules/service-health-alert.bicep' = {
  scope: resourceGroup
  name: 'service-health-alert-${resourceSuffix}'
  params: {
    environmentName: environmentName
    webhookUri: webhookUri
    secureWebhookObjectId: secureWebhookObjectId
    secureWebhookIdentifierUri: secureWebhookIdentifierUri
    tenantId: tenantId
    targetSubscriptionId: targetSubscriptionId
    resourceSuffix: resourceSuffix
    alertEnabled: alertEnabled
    tags: tags
  }
}

output resourceGroupName string = resourceGroup.name
output actionGroupId string = serviceHealthAlert.outputs.actionGroupId
output actionGroupName string = serviceHealthAlert.outputs.actionGroupName
output activityLogAlertId string = serviceHealthAlert.outputs.activityLogAlertId
output activityLogAlertName string = serviceHealthAlert.outputs.activityLogAlertName
