local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'
local LrPathUtils = import 'LrPathUtils'

local DetectionQueue = require 'DetectionQueue'
local logger = require 'Log'

local function getSourceFolder()
    local catalog = LrApplication.activeCatalog()
    local activeSources = catalog:getActiveSources()
    local targetPhotos = catalog:getTargetPhotos()

    if activeSources and #activeSources > 0 then
        for _, source in ipairs(activeSources) do
            if source and type(source.getPath) == 'function' then
                local folderPath = source:getPath()
                if folderPath and folderPath ~= "" then
                    return folderPath, nil
                end
            end
        end
    end

    if not targetPhotos or #targetPhotos == 0 then
        return nil, "No photos selected. Please select photos or navigate to a folder."
    end

    local photoPath = targetPhotos[1]:getRawMetadata('path')
    if not photoPath then
        return nil, "Cannot determine file path for selected photo."
    end

    return LrPathUtils.parent(photoPath), nil
end

local function confirmScanRoot(folder)
    local queueStatus = ""
    local currentCount = DetectionQueue.count()
    local actionLabel = "Start Scan"
    if DetectionQueue.isWorkerRunning() then
        queueStatus = string.format(
            "\n\nAnother folder is already being scanned.\nThis folder will be added next.\nFolders waiting: %d",
            currentCount
        )
        actionLabel = "Add to Queue"
    end

    local confirmResult = LrDialogs.confirm(
        "Detect focus stacks recursively in this folder and its subfolders:\n\n" ..
            folder .. queueStatus,
        nil,
        actionLabel,
        "Cancel"
    )
    if confirmResult == "ok" then
        return folder
    end
    return nil
end

LrTasks.startAsyncTask(function()
    logger:info("=== QUEUE DETECT FOCUS STACKS ===")

    local folder, folderErr = getSourceFolder()
    if not folder then
        LrDialogs.message("Focus Stack Manager", folderErr, "warning")
        return
    end

    local scanRoot = confirmScanRoot(folder)
    if not scanRoot then
        logger:info("User cancelled detection enqueue")
        return
    end

    DetectionQueue.enqueueScanRoot(scanRoot)
end)
