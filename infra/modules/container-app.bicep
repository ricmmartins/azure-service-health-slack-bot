param environmentName string
param location string
param containerRegistryName string
param containerRegistryLoginServer string
param logAnalyticsCustomerId string

@secure()
param logAnalyticsSharedKey string

param applicationInsightsConnectionString string
param managedIdentityId string
param managedIdentityPrincipalId string
param keyVaultName string
param slackBotTokenSecretUri string
param tableEndpoint string
param serviceHealthRoutesJson string
param secureWebhookClientId string
param secureWebhookIdentifierUri string
param tenantId string
param tags object

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: containerRegistryName
}

var acrPullRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, managedIdentityPrincipalId, acrPullRole)
  scope: registry
  properties: {
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${environmentName}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'app'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 5000
        transport: 'auto'
      }
      registries: [
        {
          server: containerRegistryLoginServer
          identity: managedIdentityId
        }
      ]
      secrets: [
        {
          name: 'slack-bot-token'
          keyVaultUrl: slackBotTokenSecretUri
          identity: managedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'APP_ENV'
              value: 'production'
            }
            {
              name: 'PORT'
              value: '5000'
            }
            {
              name: 'SLACK_BOT_TOKEN'
              secretRef: 'slack-bot-token'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: reference(managedIdentityId, '2023-01-31').clientId
            }
            {
              name: 'AZURE_TABLE_ENDPOINT'
              value: tableEndpoint
            }
            {
              name: 'SERVICE_HEALTH_TABLE_NAME'
              value: 'ServiceHealthIncidents'
            }
            {
              name: 'SERVICE_HEALTH_ROUTES_JSON'
              value: serviceHealthRoutesJson
            }
            {
              name: 'SERVICE_HEALTH_EXPECTED_AUDIENCE'
              value: secureWebhookIdentifierUri
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsightsConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'azure-service-health-slack-bot'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 5000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: 5000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 15
              periodSeconds: 15
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '25'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
    keyVault
  ]
}

resource authConfig 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: containerApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: secureWebhookClientId
          openIdIssuer: '${az.environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            secureWebhookIdentifierUri
          ]
          defaultAuthorizationPolicy: {
            allowedApplications: [
              '461e8683-5575-4561-ac7f-899cc907d62a'
            ]
          }
        }
      }
    }
    httpSettings: {
      requireHttps: true
      routes: {
        apiPrefix: '/.auth'
      }
    }
  }
}

output name string = containerApp.name
output fqdn string = containerApp.properties.configuration.ingress.fqdn
