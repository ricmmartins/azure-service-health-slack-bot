param location string
@minLength(8)
param resourceToken string
param tags object

resource lockStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stlock${resourceToken}'
  location: location
  tags: union(tags, {
    'service-health-purpose': 'operation-lock'
  })
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: lockStorage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: false
    }
  }
}

resource lockContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'operation-locks'
  properties: {
    publicAccess: 'None'
  }
}

output storageAccountName string = lockStorage.name
output containerName string = lockContainer.name
