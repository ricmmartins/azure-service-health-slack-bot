targetScope = 'subscription'

@minLength(1)
param environmentName string
param location string

param serviceHealthRoutesJsonB64 string
param secureWebhookClientId string
param secureWebhookObjectId string
param secureWebhookIdentifierUri string
param tenantId string = tenant().tenantId

@description('Deploy the Container App and Service Health alert path. Set false only for phase-one bootstrap.')
param deployWorkload bool = true

@description('Set automatically by azd so reprovisioning preserves the latest deployed application image.')
param appResourceExists bool = false

@description('Enabled state for the central baseline alert. New environments keep this false until acceptance.')
param baselineAlertEnabled bool = false

@description('Optional Key Vault secret version used only for emergency rollback. Empty selects the latest version.')
param slackBotTokenSecretVersion string = ''

@description('Existing independent operations Action Group resource ID. Empty skips production alert rules.')
param operationsActionGroupId string = ''

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param acrSkuName string = 'Basic'

@description('''
Deprecated. Management Group coverage is configured after the central
deployment with scripts/manage_alert_scopes.py.
''')
@allowed([
  ''
])
param managementGroupId string = ''
var resourceToken = toLower(uniqueString(subscription().id, environmentName))
var resourceGroupName = 'rg-${environmentName}'
var serviceHealthRoutesJson = base64ToString(serviceHealthRoutesJsonB64)
var centralAlertSubscriptionId = empty(managementGroupId)
  ? subscription().subscriptionId
  : ''
var tags = {
  'azd-env-name': environmentName
  workload: 'azure-service-health-slack-bot'
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module observability 'modules/observability.bicep' = {
  scope: resourceGroup
  name: 'observability'
  params: {
    environmentName: environmentName
    location: location
    tags: tags
  }
}

module registry 'modules/registry.bicep' = {
  scope: resourceGroup
  name: 'registry'
  params: {
    location: location
    resourceToken: resourceToken
    skuName: acrSkuName
    tags: tags
  }
}

module network 'modules/network.bicep' = {
  scope: resourceGroup
  name: 'network'
  params: {
    environmentName: environmentName
    location: location
    tags: tags
  }
}

module security 'modules/security.bicep' = {
  scope: resourceGroup
  name: 'security'
  params: {
    environmentName: environmentName
    location: location
    resourceToken: resourceToken
    slackBotTokenSecretVersion: slackBotTokenSecretVersion
    peSubnetId: network.outputs.peSubnetId
    keyVaultPrivateDnsZoneId: network.outputs.keyVaultPrivateDnsZoneId
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  scope: resourceGroup
  name: 'storage'
  params: {
    location: location
    resourceToken: resourceToken
    managedIdentityPrincipalId: security.outputs.managedIdentityPrincipalId
    peSubnetId: network.outputs.peSubnetId
    tablePrivateDnsZoneId: network.outputs.tablePrivateDnsZoneId
    tags: tags
  }
}

module operationsLock 'modules/operations-lock.bicep' = {
  scope: resourceGroup
  name: 'operations-lock'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module app 'modules/container-app.bicep' = if (deployWorkload) {
  scope: resourceGroup
  name: 'container-app'
  params: {
    environmentName: environmentName
    location: location
    containerRegistryName: registry.outputs.name
    containerRegistryLoginServer: registry.outputs.loginServer
    logAnalyticsCustomerId: observability.outputs.logAnalyticsCustomerId
    logAnalyticsSharedKey: observability.outputs.logAnalyticsSharedKey
    applicationInsightsConnectionString: observability.outputs.connectionString
    managedIdentityId: security.outputs.managedIdentityId
    managedIdentityPrincipalId: security.outputs.managedIdentityPrincipalId
    keyVaultName: security.outputs.keyVaultName
    slackBotTokenSecretUri: security.outputs.slackBotTokenSecretUri
    tableEndpoint: storage.outputs.tableEndpoint
    serviceHealthRoutesJson: serviceHealthRoutesJson
    secureWebhookClientId: secureWebhookClientId
    secureWebhookIdentifierUri: secureWebhookIdentifierUri
    tenantId: tenantId
    infraSubnetId: network.outputs.infraSubnetId
    containerAppExists: appResourceExists
    tags: tags
  }
}

module serviceHealthAlert 'modules/service-health-alert.bicep' = if (deployWorkload) {
  scope: resourceGroup
  name: 'service-health-alert'
  params: {
    environmentName: environmentName
    webhookUri: deployWorkload ? 'https://${app!.outputs.fqdn}/api/service-health' : ''
    secureWebhookObjectId: secureWebhookObjectId
    secureWebhookIdentifierUri: secureWebhookIdentifierUri
    tenantId: tenantId
    targetSubscriptionId: centralAlertSubscriptionId
    alertEnabled: baselineAlertEnabled
    tags: tags
  }
}

module operationsMonitoring 'modules/operations-monitoring.bicep' = {
  scope: resourceGroup
  name: 'operations-monitoring'
  params: {
    environmentName: environmentName
    location: location
    keyVaultName: security.outputs.keyVaultName
    storageAccountName: storage.outputs.name
    logAnalyticsId: observability.outputs.logAnalyticsId
    applicationInsightsId: observability.outputs.applicationInsightsId
    containerAppName: deployWorkload ? app!.outputs.name : ''
    containerAppFqdn: deployWorkload ? app!.outputs.fqdn : ''
    deployWorkload: deployWorkload
    operationsActionGroupId: operationsActionGroupId
    tags: tags
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroupName
output SERVICE_APP_NAME string = deployWorkload ? app!.outputs.name : ''
output SERVICE_APP_URI string = deployWorkload ? 'https://${app!.outputs.fqdn}' : ''
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output SERVICE_HEALTH_KEY_VAULT_NAME string = security.outputs.keyVaultName
output SERVICE_HEALTH_SLACK_SECRET_URI string = security.outputs.slackBotTokenSecretUri
output SERVICE_HEALTH_MONITORING_ENABLED bool = deployWorkload && !empty(operationsActionGroupId)
output SERVICE_HEALTH_LOCK_STORAGE_ACCOUNT_NAME string = operationsLock.outputs.storageAccountName
