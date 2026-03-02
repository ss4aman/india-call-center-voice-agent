targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources (filtered on available regions for Azure Open AI Service).')
@allowed([
  'centralindia'
  'eastus2'
  'swedencentral'
])
param location string

var abbrs = loadJsonContent('./abbreviations.json')
param useApplicationInsights bool = true
param useContainerRegistry bool = true
param appExists bool
@description('The OpenAI model name')
param modelName string = ' gpt-4o-mini'
@description('Existing Voice Live endpoint to reuse instead of deploying a new AI Services resource. Leave empty to deploy new AI Services.')
param existingVoiceLiveEndpoint string = ''
@description('Optional existing AI Services resource ID used only for role assignment when existingVoiceLiveEndpoint is provided.')
param existingAiServicesId string = ''
@description('Id of the user or app to assign application roles. If ommited will be generated from the user assigned identity.')
param principalId string = ''
@description('Optional Voice Live BYOM profile (e.g., byom-azure-openai-chat-completion).')
param byomProfile string = ''
@description('Optional Voice Live foundry resource override for BYOM routing.')
param foundryResourceOverride string = ''
@secure()
@description('Optional existing ACS connection string. When set, infra will reuse this ACS and skip creating a new Communication Services resource.')
param existingAcsConnectionString string = ''

var uniqueSuffix = substring(uniqueString(subscription().id, environmentName), 0, 5)
var tags = {'azd-env-name': environmentName }
var rgName = 'rg-${environmentName}-${uniqueSuffix}'
var useExistingVoiceLive = !empty(existingVoiceLiveEndpoint)
var useExistingAcsConnectionString = !empty(existingAcsConnectionString)

resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: rgName
  location: location
  tags: tags
}

// [ User Assigned Identity for App to avoid circular dependency ]
module appIdentity './modules/identity.bicep' = {
  name: 'uami'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
  }
}

var sanitizedEnvName = toLower(replace(replace(replace(replace(environmentName, ' ', '-'), '--', '-'), '[^a-zA-Z0-9-]', ''), '_', '-'))
var logAnalyticsName = take('log-${sanitizedEnvName}-${uniqueSuffix}', 63)
var appInsightsName = take('insights-${sanitizedEnvName}-${uniqueSuffix}', 63)
module monitoring 'modules/monitoring/monitor.bicep' = {
  name: 'monitor'
  scope: rg
  params: {
    logAnalyticsName: logAnalyticsName
    appInsightsName: appInsightsName
    tags: tags
  }
}

module registry 'modules/containerregistry.bicep' = {
  name: 'registry'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    identityName: appIdentity.outputs.name
    tags: tags
  }
  dependsOn: [ appIdentity ]
}


module aiServices './modules/aiservices.bicep' = if (!useExistingVoiceLive) {
  name: 'ai-foundry-deployment'
  scope: rg
  params: {
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    identityId: appIdentity.outputs.identityId
    tags: tags
  }
  dependsOn: [ appIdentity ]
}

module acs 'modules/acs.bicep' = if (!useExistingAcsConnectionString) {
  name: 'acs-deployment'
  scope: rg
  params: {
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    tags: tags
  }
}

var sanitizedKeyVaultName = take('kv${uniqueSuffix}${substring(uniqueString(subscription().id, environmentName), 0, 10)}', 24)
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault-deployment'
  scope: rg
  params: {
    location: location
    keyVaultName: sanitizedKeyVaultName
    tags: tags
    acsConnectionString: useExistingAcsConnectionString ? existingAcsConnectionString : acs.outputs.acsConnectionString
  }
  dependsOn: [ appIdentity ]
}

// Add role assignments 
module RoleAssignments 'modules/roleassignments.bicep' = if (!useExistingVoiceLive) {
  scope: rg
  name: 'role-assignments'
  params: {
    identityPrincipalId: appIdentity.outputs.principalId
    aiServicesId: aiServices.outputs.aiServicesId
    keyVaultName: sanitizedKeyVaultName
  }
  dependsOn: [ keyvault, appIdentity ] 
}

module RoleAssignmentsExisting 'modules/roleassignments.bicep' = if (useExistingVoiceLive && !empty(existingAiServicesId)) {
  scope: rg
  name: 'role-assignments-existing'
  params: {
    identityPrincipalId: appIdentity.outputs.principalId
    aiServicesId: existingAiServicesId
    keyVaultName: sanitizedKeyVaultName
  }
  dependsOn: [ keyvault, appIdentity ]
}

module KeyVaultRoleAssignmentsOnly 'modules/keyvault-roleassignments.bicep' = if (useExistingVoiceLive && empty(existingAiServicesId)) {
  scope: rg
  name: 'role-assignments-keyvault-only'
  params: {
    identityPrincipalId: appIdentity.outputs.principalId
    keyVaultName: sanitizedKeyVaultName
  }
  dependsOn: [ keyvault, appIdentity ]
}

module containerapp 'modules/containerapp.bicep' = {
  name: 'containerapp-deployment'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    tags: tags
    exists: appExists
    identityId: appIdentity.outputs.identityId
    identityClientId: appIdentity.outputs.clientId
    containerRegistryName: registry.outputs.name
    aiServicesEndpoint: useExistingVoiceLive ? existingVoiceLiveEndpoint : aiServices.outputs.aiServicesEndpoint
    modelDeploymentName: modelName
    acsConnectionStringSecretUri: keyvault.outputs.acsConnectionStringUri
    logAnalyticsWorkspaceName: logAnalyticsName
    byomProfile: byomProfile
    foundryResourceOverride: foundryResourceOverride
    imageName: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
  }
  dependsOn: [keyvault]
}


// OUTPUTS will be saved in azd env for later use
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_USER_ASSIGNED_IDENTITY_ID string = appIdentity.outputs.identityId
output AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID string = appIdentity.outputs.clientId

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output SERVICE_API_ENDPOINTS array = ['${containerapp.outputs.containerAppFqdn}/acs/incomingcall']
output AZURE_VOICE_LIVE_ENDPOINT string = useExistingVoiceLive ? existingVoiceLiveEndpoint : aiServices.outputs.aiServicesEndpoint
output AZURE_VOICE_LIVE_MODEL string = modelName
