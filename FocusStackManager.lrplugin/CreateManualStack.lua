-- CreateManualStack.lua
-- Create a focus stack collection from manually selected photos.
--
-- Select any set of photos in the Library, run this action, and they
-- become a focus stack collection — identical to what auto-detection
-- produces. All downstream actions (Process, Collapse) work
-- the same way.

local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'
local LrPathUtils = import 'LrPathUtils'

local CollectionManager = require 'CollectionManager'
local logger = require 'Log'

LrTasks.startAsyncTask(function()
    logger:info("=== CREATE MANUAL FOCUS STACK STARTED ===")

    local catalog = LrApplication.activeCatalog()
    local prefs = LrPrefs.prefsForPlugin()
    local targetPhotos = catalog:getTargetPhotos()

    if not targetPhotos or #targetPhotos < 2 then
        LrDialogs.message("Focus Stack Manager",
            "Select at least 2 photos to create a manual focus stack.",
            "warning")
        return
    end

    -- Precompute filenames before sorting. Lightroom metadata access may yield,
    -- which is not allowed inside table.sort's comparison callback.
    local sortablePhotos = {}
    for _, photo in ipairs(targetPhotos) do
        table.insert(sortablePhotos, {
            photo = photo,
            fileName = photo:getFormattedMetadata('fileName') or "",
        })
    end

    table.sort(sortablePhotos, function(a, b)
        return a.fileName < b.fileName
    end)

    targetPhotos = {}
    for _, entry in ipairs(sortablePhotos) do
        table.insert(targetPhotos, entry.photo)
    end

    -- Derive stack name from first and last filenames
    local firstName = sortablePhotos[1].fileName ~= "" and sortablePhotos[1].fileName or "unknown"
    local lastName = sortablePhotos[#sortablePhotos].fileName ~= "" and sortablePhotos[#sortablePhotos].fileName or "unknown"
    local firstStem = LrPathUtils.removeExtension(firstName)
    local lastStem = LrPathUtils.removeExtension(lastName)
    local stackName = firstStem .. "-" .. lastStem
    local stackId = CollectionManager.generateStackId()
    local count = #targetPhotos
    local collName = CollectionManager.collectionName(stackName, count)

    -- Derive source directory from first photo
    local firstPath = targetPhotos[1]:getRawMetadata('path')
    if not firstPath then
        LrDialogs.message("Focus Stack Manager",
            "Cannot determine file path for selected photos.",
            "warning")
        return
    end
    local sourceDir = LrPathUtils.parent(firstPath)

    for i = 2, #targetPhotos do
        local path = targetPhotos[i]:getRawMetadata('path')
        local dir = path and LrPathUtils.parent(path) or nil
        if dir ~= sourceDir then
            LrDialogs.message("Focus Stack Manager",
                "Manual focus stacks must come from a single folder.\n\n" ..
                "The current selection spans multiple directories.",
                "warning")
            return
        end
    end

    -- Confirm with user
    local confirmResult = LrDialogs.confirm(
        string.format(
            "Create manual focus stack:\n\n" ..
            "Name: %s\n" ..
            "Photos: %d\n" ..
            "Folder: %s\n\n" ..
            "These photos will be marked as focus stack sources\n" ..
            "and organized into a collection for processing.",
            stackName, count, LrPathUtils.leafName(sourceDir)
        ),
        nil,
        "Create Stack",
        "Cancel"
    )

    if confirmResult ~= "ok" then
        logger:info("User cancelled")
        return
    end

    CollectionManager.ensureLeafSet(catalog, sourceDir)

    local existingCollection = false
    catalog:withWriteAccessDo("Prepare manual focus stack", function()
        local collectionSet = CollectionManager.findLeafSet(catalog, sourceDir)
        if not collectionSet then
            error("Collection set was not found during manual stack preparation: " .. tostring(sourceDir))
        end
        -- Check if collection already exists
        local existing = CollectionManager.findCollection(collectionSet, collName)
        if existing then
            LrDialogs.message("Focus Stack Manager",
                "A collection named '" .. collName .. "' already exists.",
                "warning")
            existingCollection = true
            return
        end
    end)

    if existingCollection then
        return
    end

    local collectionSet = CollectionManager.findLeafSet(catalog, sourceDir)
    if not collectionSet then
        error("Collection set was not found after creation for: " .. tostring(sourceDir))
    end

    catalog:withWriteAccessDo("Create manual focus stack collection", function()
        local collectionSet = CollectionManager.findLeafSet(catalog, sourceDir)
        if not collectionSet then
            error("Collection set missing before manual stack creation: " .. tostring(sourceDir))
        end
        if not CollectionManager.findCollection(collectionSet, collName) then
            CollectionManager.createCollection(catalog, collectionSet, collName)
        end
    end)

    catalog:withWriteAccessDo("Tag manual focus stack photos", function()
        local srcColorLabel = prefs.source_color_label or "red"
        local userSourceKeyword = CollectionManager.findOrCreateOptionalKeyword(catalog, prefs.source_keyword)
        for _, photo in ipairs(targetPhotos) do
            CollectionManager.setPhotoState(
                photo,
                CollectionManager.getSourceRoleValue(),
                stackId,
                stackName,
                nil
            )
            CollectionManager.addResolvedKeyword(photo, userSourceKeyword)
            if srcColorLabel ~= "none" then
                photo:setRawMetadata('colorNameForLabel', srcColorLabel)
            end
        end
    end)

    catalog:withWriteAccessDo("Populate manual focus stack", function()
        local collectionSet = CollectionManager.findLeafSet(catalog, sourceDir)
        if not collectionSet then
            error("Collection set missing before manual stack population: " .. tostring(sourceDir))
        end
        local collection = CollectionManager.findCollection(collectionSet, collName)
        if not collection then
            error("Collection was not found after creation: " .. collName)
        end
        collection:addPhotos(targetPhotos)
        logger:info("Created manual stack: " .. collName .. " with " .. count .. " photos")
    end)

    LrDialogs.message("Focus Stack Manager",
        string.format("Created focus stack: %s\n\n" ..
            "Next: Select this collection and run 'Process Focus Stacks'.",
            collName),
        "info")

    logger:info("=== CREATE MANUAL FOCUS STACK COMPLETE ===")
end)
