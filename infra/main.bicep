targetScope = 'subscription'

@minLength(1)
param environmentName string
param location string

@secure()
param slackBotToken string

param serviceHealthRoutesJsonB64 string
param secureWebhookClientId string
param secureWebhookObjectId string
param secureWebhookIdentifierUri string
param tenantId string = tenant().tenantId

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param acrSkuName string = 'Basic'

var resourceToken = toLower(uniqueString(subscription().id, environmentName))
var resourceGroupName = 'rg-${environmentName}'
var serviceHealthRoutesJson = base64ToString(serviceHealthRoutesJsonB64)
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
    slackBotToken: slackBotToken
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

module app 'modules/container-app.bicep' = {
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
    tags: tags
  }
}

module serviceHealthAlert 'modules/service-health-alert.bicep' = {
  scope: resourceGroup
  name: 'service-health-alert'
  params: {
    environmentName: environmentName
    webhookUri: 'https://${app.outputs.fqdn}/api/service-health'
    secureWebhookObjectId: secureWebhookObjectId
    secureWebhookIdentifierUri: secureWebhookIdentifierUri
    tenantId: tenantId
    targetSubscriptionId: subscription().subscriptionId
    tags: tags
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroupName
output SERVICE_APP_NAME string = app.outputs.name
output SERVICE_APP_URI string = 'https://${app.outputs.fqdn}'
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
