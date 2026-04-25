-- CollapseStacks.lua
-- Groups focus-stack photos in the current source context into Lightroom stacks
-- (Cmd+G), with the newest non-rejected result on top.
--
-- The Lightroom SDK cannot create stacks programmatically, so this action:
--   1. Reads the photos visible in the currently selected folder/collection context
--   2. Groups photos by private focus_stack_id metadata
--   3. Picks the newest non-rejected result for each group
--   4. Selects result + source photos with the result as the active photo
--   5. Sends Cmd+G via System Events (AppleScript) to create the stack
--
-- Requires: macOS Accessibility permissions for System Events.

local LrApplication = import 'LrApplication'
local LrApplicationView = import 'LrApplicationView'
local LrDialogs = import 'LrDialogs'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'
local LrProgressScope = import 'LrProgressScope'

local CollectionManager = require 'CollectionManager'
local logger = require 'Log'

local function getPhotoPath(photo)
    return photo and photo:getRawMetadata('path') or nil
end

local function shellEscape(s)
    return "'" .. string.gsub(s or "", "'", "'\\''") .. "'"
end

local function getPhotoExtension(photo)
    local path = getPhotoPath(photo)
    if not path then
        return ""
    end
    return string.lower(LrPathUtils.extension(path) or "")
end

local function isProcessedResult(photo)
    local ext = getPhotoExtension(photo)
    return ext == "tif" or ext == "tiff" or ext == "psd"
end

local function getPhotoFileCreationTime(photo)
    local path = getPhotoPath(photo)
    if not path then
        return nil
    end

    local cmd = "stat -f %B " .. shellEscape(path) .. " 2>/dev/null"
    local handle = io.popen(cmd, "r")
    if not handle then
        return nil
    end

    local output = handle:read("*a") or ""
    handle:close()

    local value = tonumber(string.match(output, "%d+"))
    if value and value > 0 then
        return value
    end

    local fallbackCmd = "stat -f %m " .. shellEscape(path) .. " 2>/dev/null"
    local fallbackHandle = io.popen(fallbackCmd, "r")
    if not fallbackHandle then
        return nil
    end

    local fallbackOutput = fallbackHandle:read("*a") or ""
    fallbackHandle:close()

    local fallbackValue = tonumber(string.match(fallbackOutput, "%d+"))
    if fallbackValue and fallbackValue > 0 then
        return fallbackValue
    end

    return nil
end

local function getPhotoFolder(photo)
    local path = getPhotoPath(photo)
    if not path then
        return nil
    end
    return LrPathUtils.parent(path)
end

local function getFolderKey(folder)
    if not folder then return nil end
    return tostring(folder)
end

local function photoSetContains(photoSet, photo)
    local wantedPath = getPhotoPath(photo)
    if not wantedPath then return false end
    for _, candidate in ipairs(photoSet or {}) do
        if getPhotoPath(candidate) == wantedPath then
            return true
        end
    end
    return false
end

local function selectedPhotosMatch(catalog, expectedPhotos, expectedActive)
    local targetPhotos = catalog:getTargetPhotos() or {}
    if #targetPhotos ~= #expectedPhotos then
        return false, string.format(
            "Selection count mismatch (expected %d, got %d)",
            #expectedPhotos, #targetPhotos
        )
    end

    for _, photo in ipairs(expectedPhotos) do
        if not photoSetContains(targetPhotos, photo) then
            return false, "Selection did not contain all expected photos"
        end
    end

    local ok, activePhoto = pcall(function()
        return catalog:getTargetPhoto()
    end)
    if ok and activePhoto and expectedActive then
        if getPhotoPath(activePhoto) ~= getPhotoPath(expectedActive) then
            return false, "Active photo was not the expected result photo"
        end
    end

    return true, nil
end

local function activateLightroom()
    local cmd = "osascript -e 'tell application \"Lightroom Classic\" to activate'"
    local exitCode = LrTasks.execute(cmd)
    if exitCode ~= 0 then
        return false, "Could not bring Lightroom Classic to the foreground"
    end
    return true, nil
end

local function sendGroupKeystroke()
    local script = [[
        tell application "System Events"
            tell process "Lightroom Classic"
                keystroke "g" using command down
            end tell
        end tell
    ]]
    local cmd = "osascript -e " .. "'" .. script .. "'"
    local exitCode = LrTasks.execute(cmd)
    if exitCode ~= 0 then
        return false, "osascript failed (exit " .. tostring(exitCode) ..
            "). Check Accessibility permissions for System Events."
    end
    return true, nil
end

local function sendUnstackKeystroke()
    local script = [[
        tell application "System Events"
            tell process "Lightroom Classic"
                keystroke "g" using {command down, shift down}
            end tell
        end tell
    ]]
    local cmd = "osascript -e " .. "'" .. script .. "'"
    local exitCode = LrTasks.execute(cmd)
    if exitCode ~= 0 then
        return false, "osascript failed (exit " .. tostring(exitCode) ..
            ") while unstacking. Check Accessibility permissions for System Events."
    end
    return true, nil
end

local function isPhotoInFolderStack(photo)
    return photo and photo:getRawMetadata('isInStackInFolder') or false
end

local function anyPhotoInFolderStack(photos)
    for _, photo in ipairs(photos or {}) do
        if isPhotoInFolderStack(photo) then
            return true
        end
    end
    return false
end

local function allPhotosInFolderStack(photos)
    for _, photo in ipairs(photos or {}) do
        if not isPhotoInFolderStack(photo) then
            return false
        end
    end
    return #photos > 0
end

local function waitForStackCreation(resultPhoto, sourcePhotos, attempts, delaySeconds)
    attempts = attempts or 10
    delaySeconds = delaySeconds or 0.5

    for _ = 1, attempts do
        if isPhotoInFolderStack(resultPhoto) then
            return true
        end
        for _, photo in ipairs(sourcePhotos or {}) do
            if isPhotoInFolderStack(photo) then
                return true
            end
        end
        LrTasks.sleep(delaySeconds)
    end
    return false
end

local function waitForUnstack(photos, attempts, delaySeconds)
    attempts = attempts or 10
    delaySeconds = delaySeconds or 0.3

    for _ = 1, attempts do
        if not anyPhotoInFolderStack(photos) then
            return true
        end
        LrTasks.sleep(delaySeconds)
    end
    return false
end

local function waitForPhotosToBeSelectable(catalog, expectedPhotos, attempts, delaySeconds)
    attempts = attempts or 12
    delaySeconds = delaySeconds or 0.4

    for _ = 1, attempts do
        local activeSources = catalog:getActiveSources() or {}
        if #activeSources > 0 and type(activeSources[1].getPhotos) == 'function' then
            local visibleCount = 0
            local visiblePhotos = activeSources[1]:getPhotos() or {}
            local visiblePaths = {}
            for _, photo in ipairs(visiblePhotos) do
                visiblePaths[getPhotoPath(photo)] = true
            end
            for _, photo in ipairs(expectedPhotos or {}) do
                if visiblePaths[getPhotoPath(photo)] then
                    visibleCount = visibleCount + 1
                end
            end
            if visibleCount == #expectedPhotos then
                return true
            end
        end
        LrTasks.sleep(delaySeconds)
    end
    return false
end

local function collectPhotosFromSource(source, outPhotos, seenIds)
    if type(source.getChildCollections) == 'function' then
        for _, collection in ipairs(source:getChildCollections() or {}) do
            collectPhotosFromSource(collection, outPhotos, seenIds)
        end
        if type(source.getChildCollectionSets) == 'function' then
            for _, collectionSet in ipairs(source:getChildCollectionSets() or {}) do
                collectPhotosFromSource(collectionSet, outPhotos, seenIds)
            end
        end
        return
    end

    if type(source.getPhotos) ~= 'function' then
        return
    end

    for _, photo in ipairs(source:getPhotos() or {}) do
        local id = photo.localIdentifier or getPhotoPath(photo)
        if id and not seenIds[id] then
            seenIds[id] = true
            table.insert(outPhotos, photo)
        end
    end
end

local function getCurrentContextPhotos(catalog)
    local activeSources = catalog:getActiveSources()
    if not activeSources or #activeSources == 0 then
        return nil, "No folder or collection selected.\n\nSelect a folder, collection, or collection set and run this action again."
    end

    for _, source in ipairs(activeSources) do
        local isCollectionSet = type(source.getChildCollections) == 'function'
            and type(source.getPhotos) ~= 'function'
        if isCollectionSet then
            return nil,
                "Collapse Focus Stacks is not supported from a collection set.\n\n" ..
                "Select a single collection or a folder in the Library module,\n" ..
                "then run this action again."
        end
    end

    local photos = {}
    local seenIds = {}
    for _, source in ipairs(activeSources) do
        collectPhotosFromSource(source, photos, seenIds)
    end

    if #photos == 0 then
        return nil, "The current source contains no photos."
    end

    return photos, nil
end

local function chooseNewestResult(resultPhotos)
    local chosen = nil

    for _, photo in ipairs(resultPhotos or {}) do
        local pickStatus = photo:getRawMetadata('pickStatus')
        if pickStatus ~= -1 then
            if not chosen or (photo.localIdentifier or 0) > (chosen.localIdentifier or 0) then
                chosen = photo
            end
        end
    end

    if chosen then
        return chosen
    end

    for _, photo in ipairs(resultPhotos or {}) do
        if not chosen or (photo.localIdentifier or 0) > (chosen.localIdentifier or 0) then
            chosen = photo
        end
    end

    return chosen
end

local function choosePreferredResult(resultPhotos)
    local processedKept = {}
    local processedRejected = {}

    for _, photo in ipairs(resultPhotos or {}) do
        if isProcessedResult(photo) then
            local pickStatus = photo:getRawMetadata('pickStatus')
            if pickStatus ~= -1 then
                table.insert(processedKept, photo)
            else
                table.insert(processedRejected, photo)
            end
        end
    end

    local function chooseNewestProcessed(photos)
        local chosen = nil
        local chosenCreated = nil

        for _, photo in ipairs(photos or {}) do
            local created = getPhotoFileCreationTime(photo)
            if not chosen then
                chosen = photo
                chosenCreated = created
            elseif created and (not chosenCreated or created > chosenCreated) then
                chosen = photo
                chosenCreated = created
            elseif created == chosenCreated and
                (photo.localIdentifier or 0) > (chosen.localIdentifier or 0) then
                chosen = photo
                chosenCreated = created
            elseif not created and not chosenCreated and
                (photo.localIdentifier or 0) > (chosen.localIdentifier or 0) then
                chosen = photo
            end
        end

        return chosen
    end

    return chooseNewestProcessed(processedKept)
        or chooseNewestProcessed(processedRejected)
        or chooseNewestResult(resultPhotos)
end

local function sortPhotosForStack(photos)
    local sortable = {}
    for _, photo in ipairs(photos or {}) do
        table.insert(sortable, {
            photo = photo,
            path = getPhotoPath(photo) or "",
        })
    end

    table.sort(sortable, function(a, b)
        return a.path < b.path
    end)

    for i, entry in ipairs(sortable) do
        photos[i] = entry.photo
    end
end

local function allPhotosShareFolder(resultPhoto, sourcePhotos)
    local expectedFolder = getPhotoFolder(resultPhoto) or getPhotoFolder(sourcePhotos[1])
    if not expectedFolder then
        return false
    end

    local expectedKey = getFolderKey(expectedFolder)
    for _, photo in ipairs(sourcePhotos) do
        if getFolderKey(getPhotoFolder(photo)) ~= expectedKey then
            return false
        end
    end
    if getFolderKey(getPhotoFolder(resultPhoto)) ~= expectedKey then
        return false
    end

    return true
end

LrTasks.startAsyncTask(function()
    logger:info("=== COLLAPSE STACKS STARTED ===")

    local catalog = LrApplication.activeCatalog()
    local contextPhotos, contextErr = getCurrentContextPhotos(catalog)

    if not contextPhotos then
        LrDialogs.message("Focus Stack Manager", contextErr, "warning")
        return
    end

    local groups = {}
    for _, photo in ipairs(contextPhotos) do
        local stackId = CollectionManager.getPhotoStackId(photo)
        if stackId then
            local group = groups[stackId]
            if not group then
                group = {
                    stackId = stackId,
                    stackName = CollectionManager.getPhotoStackName(photo) or stackId,
                    sourcePhotos = {},
                    resultPhotos = {},
                }
                groups[stackId] = group
            end

            if CollectionManager.getPhotoResultParams(photo) then
                table.insert(group.resultPhotos, photo)
            elseif CollectionManager.getPhotoStackName(photo) then
                table.insert(group.sourcePhotos, photo)
            end
        end
    end

    local toStack = {}
    local skippedInvalid = {}
    local alreadyStacked = 0
    local alreadyComplete = 0

    for _, group in pairs(groups) do
        if #group.sourcePhotos > 0 then
            sortPhotosForStack(group.sourcePhotos)
            local resultPhoto = choosePreferredResult(group.resultPhotos)
            local anchorPhoto = resultPhoto or group.sourcePhotos[1]

            if anchorPhoto then
                if resultPhoto then
                    if allPhotosShareFolder(resultPhoto, group.sourcePhotos) then
                        local expectedPhotos = { resultPhoto }
                        for _, sourcePhoto in ipairs(group.sourcePhotos) do
                            table.insert(expectedPhotos, sourcePhoto)
                        end
                        local sourceStacked = allPhotosInFolderStack(group.sourcePhotos)
                        if allPhotosInFolderStack(expectedPhotos) then
                            alreadyComplete = alreadyComplete + 1
                        elseif sourceStacked and not isPhotoInFolderStack(resultPhoto) then
                            table.insert(toStack, {
                                name = group.stackName,
                                stackId = group.stackId,
                                anchorPhoto = group.sourcePhotos[1],
                                resultPhoto = resultPhoto,
                                sourcePhotos = group.sourcePhotos,
                                mode = "augment",
                            })
                        else
                            table.insert(toStack, {
                                name = group.stackName,
                                stackId = group.stackId,
                                anchorPhoto = anchorPhoto,
                                resultPhoto = resultPhoto,
                                sourcePhotos = group.sourcePhotos,
                                mode = "rebuild",
                            })
                        end
                    else
                        table.insert(skippedInvalid, group.stackName)
                        logger:warn("Skipping non-folder-local stack: " .. group.stackName)
                    end
                else
                    if allPhotosInFolderStack(group.sourcePhotos) then
                        alreadyComplete = alreadyComplete + 1
                    else
                        table.insert(toStack, {
                            name = group.stackName,
                            stackId = group.stackId,
                            anchorPhoto = anchorPhoto,
                            resultPhoto = nil,
                            sourcePhotos = group.sourcePhotos,
                            mode = "rebuild",
                        })
                    end
                end
            end
        end
    end

    table.sort(toStack, function(a, b)
        return a.name < b.name
    end)

    if #toStack == 0 then
        local msg
        if #skippedInvalid > 0 then
            msg = string.format(
                "No eligible stacks to collapse in the current source.\n\n" ..
                "Skipped because photos were not all in one folder: %d\n  %s",
                #skippedInvalid,
                table.concat(skippedInvalid, "\n  ")
            )
        elseif alreadyComplete > 0 then
            msg = "All eligible stacks in the current source are already up to date."
        else
            msg = "No focus-stack photos found in the current source."
        end
        LrDialogs.message("Focus Stack Manager", msg, "info")
        return
    end

    local activated, activateErr = activateLightroom()
    if not activated then
        LrDialogs.message("Focus Stack Manager", activateErr, "critical")
        return
    end
    LrApplicationView.switchToModule('library')
    LrTasks.sleep(0.7)

    local confirmResult = LrDialogs.confirm(
        "Collapse uses keyboard automation in Lightroom.\n\n" ..
        "Do not use the keyboard or change selection until it finishes.",
        nil,
        "Collapse",
        "Cancel"
    )
    if confirmResult ~= "ok" then
        logger:info("User cancelled collapse before execution")
        return
    end

    local progress = LrProgressScope { title = "Collapsing focus stacks..." }
    local collapsed = 0
    local errors = 0
    local errorDetails = {}

    for i, item in ipairs(toStack) do
        if progress:isCanceled() then
            logger:info("User cancelled at stack " .. i)
            break
        end

        progress:setCaption(item.name)
        progress:setPortionComplete(i - 1, #toStack)
        logger:info("Collapsing: " .. item.name)

        local expectedPhotos = {}
        if item.mode == "augment" then
            table.insert(expectedPhotos, item.resultPhoto)
            table.insert(expectedPhotos, item.anchorPhoto)
        else
            if item.resultPhoto then
                table.insert(expectedPhotos, item.resultPhoto)
            end
            for _, photo in ipairs(item.sourcePhotos) do
                table.insert(expectedPhotos, photo)
            end
        end

        local collapseErr = nil
        local activePhoto = item.anchorPhoto

        if item.mode == "rebuild" and anyPhotoInFolderStack(expectedPhotos) then
            catalog:setSelectedPhotos(item.anchorPhoto, {})
            LrTasks.sleep(0.4)

            local activatedForUnstack, activateErrForUnstack = activateLightroom()
            if not activatedForUnstack then
                collapseErr = activateErrForUnstack
            else
                LrTasks.sleep(0.2)
                local unstackOk, unstackErr = sendUnstackKeystroke()
                if not unstackOk then
                    collapseErr = unstackErr
                else
                    waitForUnstack(expectedPhotos)
                    waitForPhotosToBeSelectable(catalog, expectedPhotos)
                    alreadyStacked = alreadyStacked + 1
                    logger:info("  Unstacked existing stack before regrouping")
                end
            end
        end

        if not collapseErr then
            local selectedOthers = {}
            if item.mode == "augment" then
                activePhoto = item.resultPhoto
                table.insert(selectedOthers, item.anchorPhoto)
            elseif item.resultPhoto then
                activePhoto = item.resultPhoto
                for _, photo in ipairs(item.sourcePhotos) do
                    table.insert(selectedOthers, photo)
                end
            else
                for i = 2, #item.sourcePhotos do
                    table.insert(selectedOthers, item.sourcePhotos[i])
                end
            end

            catalog:setSelectedPhotos(activePhoto, selectedOthers)
            LrTasks.sleep(0.5)

            local selectionOk, selectionErr = selectedPhotosMatch(
                catalog, expectedPhotos, activePhoto)
            if not selectionOk then
                collapseErr = "Selection verification failed: " .. tostring(selectionErr)
            else
                local activatedAgain, activateErrAgain = activateLightroom()
                if not activatedAgain then
                    collapseErr = activateErrAgain
                else
                    LrTasks.sleep(0.2)
                    local keystrokeOk, keystrokeErr = sendGroupKeystroke()
                    if not keystrokeOk then
                        collapseErr = keystrokeErr
                    end
                end
            end
        end

        if not collapseErr then
            collapsed = collapsed + 1
            local verifyPhoto = item.resultPhoto or item.anchorPhoto
            local isInStack = waitForStackCreation(verifyPhoto, item.sourcePhotos)
            if isInStack then
                logger:info("  Collapsed successfully (verified)")
            else
                logger:warn("  Collapse could not be verified for: " .. item.name ..
                    " — treating as success because Cmd+G completed without error")
            end
        else
            errors = errors + 1
            logger:info("  Error: " .. tostring(collapseErr))
            table.insert(errorDetails, item.name .. ": " .. tostring(collapseErr))
        end
    end

    progress:done()

    local finalMsg
    if errors == 0 then
        finalMsg = string.format(
            "Collapse complete.\n\nSuccessfully collapsed: %d stacks",
            collapsed
        )
        if alreadyStacked > 0 then
            finalMsg = finalMsg .. string.format(
                "\nRebuilt existing stacks: %d",
                alreadyStacked
            )
        end
        if alreadyComplete > 0 then
            finalMsg = finalMsg .. string.format(
                "\nAlready up to date: %d",
                alreadyComplete
            )
        end
        if #skippedInvalid > 0 then
            finalMsg = finalMsg .. string.format(
                "\nSkipped (photos not all in one folder): %d\n  %s",
                #skippedInvalid,
                table.concat(skippedInvalid, "\n  ")
            )
        end
    else
        local parts = {
            string.format("Collapse complete.\n\nCollapsed: %d", collapsed),
        }
        if alreadyStacked > 0 then
            table.insert(parts, string.format("Rebuilt existing stacks: %d", alreadyStacked))
        end
        if alreadyComplete > 0 then
            table.insert(parts, string.format("Already up to date: %d", alreadyComplete))
        end
        if #skippedInvalid > 0 then
            table.insert(parts, string.format(
                "Skipped (photos not all in one folder): %d\n  %s",
                #skippedInvalid,
                table.concat(skippedInvalid, "\n  ")
            ))
        end
        if errors > 0 then
            local showCount = math.min(#errorDetails, 5)
            table.insert(parts, string.format("Errors: %d", errors))
            if showCount > 0 then
                table.insert(parts, "Error details:")
                for i = 1, showCount do
                    table.insert(parts, "  " .. errorDetails[i])
                end
                if #errorDetails > showCount then
                    table.insert(parts, string.format(
                        "  (... and %d more; see plugin log for details)",
                        #errorDetails - showCount
                    ))
                end
            end
        end
        finalMsg = table.concat(parts, "\n")
    end

    LrDialogs.message("Focus Stack Manager", finalMsg, "info")
    logger:info("=== COLLAPSE STACKS COMPLETE ===")
end)
