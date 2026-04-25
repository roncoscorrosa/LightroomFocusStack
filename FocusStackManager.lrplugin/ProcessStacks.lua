-- ProcessStacks.lua
-- Process focus stacks through Helicon Focus.
--
-- Operates on the currently selected collection or collection set in the
-- Lightroom sidebar. Finds all focus stack collections (with source photos),
-- checks for existing results, and enqueues unprocessed stacks.
--
-- The user selects one or more collections/collection sets, then runs this.
-- A dialog prompts for Helicon Focus parameters (with defaults from prefs).
--
-- Rejected source photos (pickStatus == -1) are excluded from processing.
-- If the active source count differs from a previous result, reprocessing
-- is triggered even with the same Helicon parameters.

local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'
local LrFunctionContext = import 'LrFunctionContext'
local LrPathUtils = import 'LrPathUtils'
local LrView = import 'LrView'
local LrBinding = import 'LrBinding'

local CollectionManager = require 'CollectionManager'
local ProcessingQueue = require 'ProcessingQueue'
local logger = require 'Log'

-- Gather all focus stack collections from the user's current selection.
-- Works with:
--   - A single collection (process just that stack)
--   - A collection set (recursively find all child collections)
--   - Multiple selected sources
local function getSelectedStackCollections()
    local catalog = LrApplication.activeCatalog()
    local activeSources = catalog:getActiveSources()

    if not activeSources or #activeSources == 0 then
        return nil, "No collection or collection set selected.\n\n" ..
                    "Select a Focus Stacks collection or collection set in the sidebar,\n" ..
                    "then run this action."
    end

    local collections = {}
    local seenIds = {} -- dedup by localIdentifier — names are not unique across folders
    local selectedCollectionCount = 0
    local selectedCollectionSetCount = 0
    local otherSourceCount = 0

    local function hasMethod(obj, methodName)
        return obj and type(obj[methodName]) == 'function'
    end

    local function addCollection(collection)
        local id = collection.localIdentifier
        if id and not seenIds[id] then
            -- Verify this is a focus stack collection (has source-tagged photos)
            local sourcePhotos = CollectionManager.getSourcePhotos(collection)
            if #sourcePhotos > 0 then
                table.insert(collections, collection)
                seenIds[id] = true
            end
        end
    end

    local function walkCollectionSet(collectionSet)
        local childCollections = collectionSet:getChildCollections()
        for _, c in ipairs(childCollections) do
            addCollection(c)
        end
        local childSets = collectionSet:getChildCollectionSets()
        for _, cs in ipairs(childSets) do
            walkCollectionSet(cs)
        end
    end

    for _, source in ipairs(activeSources) do
        local isCollection = hasMethod(source, 'getPhotos')
        local isCollectionSet = hasMethod(source, 'getChildCollections')

        if isCollection then
            selectedCollectionCount = selectedCollectionCount + 1
            addCollection(source)
        end

        if isCollectionSet then
            selectedCollectionSetCount = selectedCollectionSetCount + 1
            walkCollectionSet(source)
        end

        if not isCollection and not isCollectionSet then
            otherSourceCount = otherSourceCount + 1
        end
    end

    if #collections == 0 then
        if otherSourceCount > 0 and selectedCollectionCount == 0 and selectedCollectionSetCount == 0 then
            return nil, "Process Focus Stacks works on Focus Stacks collections,\n" ..
                        "not folders or other Library sources.\n\n" ..
                        "Select a detected Focus Stacks collection or collection set\n" ..
                        "in the Collections panel, then run this action again."
        end

        if selectedCollectionCount > 0 and selectedCollectionSetCount == 0 and otherSourceCount == 0 then
            return nil, "The selected collection is not a Focus Stack collection.\n\n" ..
                        "Select a collection created by 'Detect Focus Stacks' or\n" ..
                        "'Create Focus Stack from Selection', then run this action again."
        end

        if selectedCollectionSetCount > 0 and selectedCollectionCount == 0 and otherSourceCount == 0 then
            return nil, "The selected collection set contains no Focus Stack collections.\n\n" ..
                        "Select the Focus Stacks hierarchy or a child set that contains\n" ..
                        "detected stack collections, then run this action again."
        end

        return nil, "The current selection contains no Focus Stack collections.\n\n" ..
                    "Select a detected Focus Stacks collection or collection set\n" ..
                    "in the Collections panel, then run this action again."
    end

    return collections, nil
end

-- Show Helicon Focus parameter dialog.
local function showParamsDialog(collectionCount, stackFrameCount)
    local prefs = LrPrefs.prefsForPlugin()
    local f = LrView.osFactory()

    local queueStatus = ""
    local currentCount = ProcessingQueue.count()
    if ProcessingQueue.isWorkerRunning() then
        queueStatus = string.format(
            "\n(Worker active, %d items in queue — new items will be appended)",
            currentCount)
    end

    local result = LrFunctionContext.callWithContext('ProcessParams', function(context)
        local props = LrBinding.makePropertyTable(context)
        props.method = prefs.helicon_method or 1
        props.radius = prefs.helicon_radius or 11
        props.smoothing = prefs.helicon_smoothing or 5

        local dialogResult = LrDialogs.presentModalDialog {
            title = "Process Focus Stacks",
            contents = f:column {
                spacing = f:control_spacing(),

                f:static_text {
                    title = string.format("%d stacks selected (%d total source frames)%s",
                        collectionCount, stackFrameCount, queueStatus),
                    font = "<system/bold>",
                },

                f:separator { fill_horizontal = 1 },

                f:static_text {
                    title = "Helicon Focus Parameters",
                    font = "<system/bold>",
                },

                f:row {
                    f:static_text { title = "Method:", alignment = 'right', width = 100 },
                    f:popup_menu {
                        value = LrView.bind { object = props, key = 'method' },
                        items = {
                            { title = "A (Weighted Average)", value = 0 },
                            { title = "B (Depth Map)", value = 1 },
                            { title = "C (Pyramid)", value = 2 },
                        },
                        width = 200,
                    },
                },

                f:row {
                    f:static_text { title = "Radius:", alignment = 'right', width = 100 },
                    f:edit_field {
                        value = LrView.bind { object = props, key = 'radius' },
                        width_in_chars = 6,
                        precision = 0,
                        min = 1,
                        max = 256,
                        increment = 1,
                    },
                },

                f:row {
                    f:static_text { title = "Smoothing:", alignment = 'right', width = 100 },
                    f:edit_field {
                        value = LrView.bind { object = props, key = 'smoothing' },
                        width_in_chars = 6,
                        precision = 0,
                        min = 0,
                        max = 20,
                        increment = 1,
                    },
                },
            },
            actionVerb = "Process",
        }

        if dialogResult == 'ok' then
            return {
                method = props.method,
                radius = props.radius,
                smoothing = props.smoothing,
            }
        end
        return nil
    end)

    return result
end


-- Main entry point
LrTasks.startAsyncTask(function()
    logger:info("=== PROCESS FOCUS STACKS STARTED ===")

    local catalog = LrApplication.activeCatalog()

    -- Get selected focus stack collections
    local collections, selErr = getSelectedStackCollections()
    if not collections then
        LrDialogs.message("Focus Stack Manager", selErr, "warning")
        return
    end

    -- Count total source frames for the dialog
    local totalFrames = 0
    for _, collection in ipairs(collections) do
        local sources = CollectionManager.getActiveSourcePhotos(collection)
        totalFrames = totalFrames + #sources
    end

    logger:info("Selected " .. #collections .. " collections, " .. totalFrames .. " source frames")

    -- Show params dialog
    local params = showParamsDialog(#collections, totalFrames)
    if not params then
        logger:info("User cancelled")
        return
    end

    logger:info("Params: method=" .. params.method ..
                " radius=" .. params.radius ..
                " smoothing=" .. params.smoothing)

    -- Analyze each collection: check for existing results, build queue entries
    local queueEntries = {}
    local alreadyDone = 0

    for _, collection in ipairs(collections) do
        local activeSources = CollectionManager.getActiveSourcePhotos(collection)
        local activeCount = #activeSources

        if activeCount == 0 then
            logger:info("Collection " .. collection:getName() .. " has no active source photos, skipping")
        else
            -- Check for existing result with matching params + source count
            local resultPhotos = CollectionManager.getResultPhotos(collection)
            local hasMatchingResult = false
            for _, rp in ipairs(resultPhotos) do
                if CollectionManager.resultMatchesParams(rp, params.method, params.smoothing, params.radius, activeCount) then
                    hasMatchingResult = true
                    break
                end
            end

            if hasMatchingResult then
                alreadyDone = alreadyDone + 1
                logger:info("Collection " .. collection:getName() ..
                            " already has result with these params and " .. activeCount .. " sources")
            else
                -- Get file paths for the active sources
                local activeFiles = {}
                for _, photo in ipairs(activeSources) do
                    local path = photo:getRawMetadata('path')
                    if path then
                        table.insert(activeFiles, path)
                    end
                end
                table.sort(activeFiles)

                -- Determine output directory from the source file location
                local resultsDir = nil
                if #activeFiles > 0 then
                    resultsDir = LrPathUtils.parent(activeFiles[1])
                end

                if resultsDir and #activeFiles > 0 then
                    -- Derive stack name from first and last active files
                    local firstStem = LrPathUtils.removeExtension(LrPathUtils.leafName(activeFiles[1]))
                    local lastStem = LrPathUtils.removeExtension(LrPathUtils.leafName(activeFiles[#activeFiles]))
                    local stackName = firstStem .. "-" .. lastStem
                    local stackId = CollectionManager.getPhotoStackId(activeSources[1])

                    table.insert(queueEntries, {
                        stackName = stackName,
                        stackId = stackId,
                        collectionId = collection.localIdentifier,
                        files = activeFiles,
                        resultsDir = resultsDir,
                        method = params.method,
                        radius = params.radius,
                        smoothing = params.smoothing,
                    })
                end
            end
        end
    end

    if #queueEntries == 0 then
        local summaryLines = {}
        table.insert(summaryLines, string.format("Selected: %d collections", #collections))
        if alreadyDone > 0 then
            table.insert(summaryLines, string.format("Already processed (same params + sources): %d", alreadyDone))
        end
        table.insert(summaryLines, "To process: 0 stacks")
        LrDialogs.message("Focus Stack Manager",
            table.concat(summaryLines, "\n") .. "\n\nNothing new to process.", "info")
        return
    end

    ProcessingQueue.enqueue(queueEntries)
    logger:info("Queued " .. tostring(#queueEntries) .. " stacks for processing")
    logger:info("=== PROCESS FOCUS STACKS COMPLETE ===")
end)
