param location string
@minLength(8)
param resourceToken string
param managedIdentityPrincipalId string
param peSubnetId string
param tablePrivateDnsZoneId string
param tags object

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

resource tablePrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'pe-table-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'table-connection'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'table'
          ]
        }
      }
    ]
  }
}

resource tablePrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: tablePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-table-core-windows-net'
        properties: {
          privateDnsZoneId: tablePrivateDnsZoneId
        }
      }
    ]
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource incidentTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'ServiceHealthIncidents'
}

var tableDataContributorRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
)

resource tableDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, managedIdentityPrincipalId, tableDataContributorRole)
  scope: storage
  properties: {
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRole
  }
}

output name string = storage.name
output tableEndpoint string = storage.properties.primaryEndpoints.table
