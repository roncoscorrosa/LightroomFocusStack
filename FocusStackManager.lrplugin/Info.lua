return {
    LrSdkVersion = 6.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = 'com.smallscenes.focusstackmanager',
    LrPluginName = "Focus Stack Manager",
    LrPluginInfoProvider = 'PluginInfoProvider.lua',
    LrMetadataProvider = 'MetadataDefinition.lua',
    LrInitPlugin = 'Init.lua',

    LrLibraryMenuItems = {
        {
            title = "1. Detect Focus Stacks",
            file = "DetectStacks.lua",
        },
        {
            title = "2. Process Focus Stacks",
            file = "ProcessStacks.lua",
        },
        {
            title = "3. Create Focus Stack from Selection",
            file = "CreateManualStack.lua",
        },
        {
            title = "4. Collapse Focus Stacks",
            file = "CollapseStacks.lua",
        },
    },

    VERSION = { major = 1, minor = 0, revision = 0, build = 1 },
}
