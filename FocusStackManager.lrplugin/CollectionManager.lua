-- CollectionManager.lua
-- Manages focus stack collection sets and collections within Lightroom.
--
-- Collection structure mirrors the filesystem hierarchy under a root set:
--
--   Focus Stacks                              (root, configurable)
--     └── Death Valley
--           └── Sand Dunes
--                 └── Mesquite Dunes
--                       └── 2026
--                             └── 2026-01-07
--                                   ├── _ON_1234-_ON_1248 (15)    (collection)
--                                   └── _ON_1260-_ON_1272 (13)    (collection)
--
-- The relative path is derived from the matching catalog root folder when
-- possible, so imported roots like "/Volumes/Ron/Photos" become simply
-- "Photos" in the Focus Stacks hierarchy.
--
-- State is encoded in the collection structure itself plus private photo metadata:
--   - Collection exists, no result-role photo -> detected, not processed
--   - Collection exists, result photo present -> processed, awaiting review
--   - Collection doesn't exist -> never detected or removed by the user
--
-- Result/source identity is tracked using private photo metadata:
--   - focus_stack_role = "source" or "result"
--   - focus_stack_stack_name = canonical stack id (for example "_ON_0001-_ON_0009")
--   - focus_stack_result_params = encoded tuple (for example "m1_s5_r11_c9")
--
-- User-facing keywords remain optional and are not used for internal logic.
-- Result filenames encode Helicon params: <first>-<last>_m<Letter>_s<S>_r<R>_stacked.<ext>

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'

local logger = require 'Log'

local CollectionManager = {}
local ROLE_FIELD = 'focus_stack_role'
local STACK_ID_FIELD = 'focus_stack_id'
local STACK_NAME_FIELD = 'focus_stack_stack_name'
local RESULT_PARAMS_FIELD = 'focus_stack_result_params'
local ROLE_SOURCE = 'source'
local ROLE_RESULT = 'result'
local stackIdCounter = 0

-- Build the expected collection name for a stack.
-- Format: "<first_stem>-<last_stem> (<count>)"
function CollectionManager.collectionName(stackName, count)
    return stackName .. " (" .. tostring(count) .. ")"
end

-- Split a path string into its components.
-- "/a/b/c" -> {"a", "b", "c"}
local function splitPath(path)
    local parts = {}
    for segment in string.gmatch(path, "[^/\\]+") do
        table.insert(parts, segment)
    end
    return parts
end

-- Derive the collection hierarchy path from a source directory.
-- Automatically strips the OS mount prefix to produce a clean hierarchy.
--
-- macOS:  /Volumes/Ron 8TB SSD/Photos/Death Valley/2026/2026-01-07
--      -> {"Ron 8TB SSD", "Photos", "Death Valley", "2026", "2026-01-07"}
--
-- macOS (boot volume): /Users/ron/Pictures/Photos/...
--      -> {"Users", "ron", "Pictures", "Photos", ...}
--
-- No configuration needed — the volume name becomes the first level.
local function getCatalogRootFolderMatch(catalog, sourceDir)
    if not catalog or not sourceDir then
        return nil
    end

    local bestFolder = nil
    local bestPathLen = -1
    local rootFolders = catalog:getFolders() or {}

    for _, folder in ipairs(rootFolders) do
        local folderPath = folder:getPath()
        if folderPath then
            local exactMatch = sourceDir == folderPath
            local prefixMatch = string.sub(sourceDir, 1, #folderPath + 1) == folderPath .. "/"
            if (exactMatch or prefixMatch) and #folderPath > bestPathLen then
                bestFolder = folder
                bestPathLen = #folderPath
            end
        end
    end

    return bestFolder
end

function CollectionManager.getRelativePathComponents(sourceDir, catalog)
    local rootFolder = getCatalogRootFolderMatch(catalog, sourceDir)
    if rootFolder then
        local rootPath = rootFolder:getPath()
        local components = { rootFolder:getName() }

        if sourceDir ~= rootPath then
            local remainder = string.sub(sourceDir, #rootPath + 2)
            for _, segment in ipairs(splitPath(remainder)) do
                table.insert(components, segment)
            end
        end

        return components
    end

    local parts = splitPath(sourceDir)

    -- On macOS, paths under /Volumes/<name>/... — strip the "Volumes" prefix
    -- so the volume name becomes the first collection set level
    if #parts >= 2 and parts[1] == "Volumes" then
        table.remove(parts, 1)
    end

    return parts
end

-- Find a child collection set by name within a parent.
-- parent can be a catalog (top level) or a collection set.
local function findChildSet(parent, name)
    local children = parent:getChildCollectionSets()
    for _, cs in ipairs(children) do
        if cs:getName() == name then
            return cs
        end
    end
    return nil
end

local function findLeafSetFromComponents(rootSet, components)
    local currentParent = rootSet
    for _, component in ipairs(components) do
        currentParent = findChildSet(currentParent, component)
        if not currentParent then
            return nil
        end
    end
    return currentParent
end

-- Find or create the root "Focus Stacks" collection set.
-- Must be called within catalog:withWriteAccessDo().
function CollectionManager.findOrCreateRootSet(catalog)
    local prefs = LrPrefs.prefsForPlugin()
    local rootName = prefs.collection_set_prefix or "Focus Stacks"

    local existing = findChildSet(catalog, rootName)
    if existing then
        logger:info("Found existing root set: " .. rootName)
        return existing
    end

    logger:info("Creating root set: " .. rootName)
    local rootSet = catalog:createCollectionSet(rootName, nil, true)
    logger:info("Created root set: " .. rootName)
    return rootSet
end

function CollectionManager.isStackCollection(collection)
    return #CollectionManager.getSourcePhotos(collection) > 0
end

-- Find or create the nested collection set hierarchy for a source directory.
-- Creates intermediate sets as needed. Returns the leaf collection set.
--
-- Example: sourceDir = "/Volumes/Ron/Photos/Death Valley/Sand Dunes/2026/2026-01-07"
--   creates: Focus Stacks > Ron > Photos > Death Valley > Sand Dunes > 2026 > 2026-01-07
--   (/Volumes/ prefix is stripped automatically)
--
-- Pass an already-resolved rootSet to avoid re-running findOrCreateRootSet
-- per directory in a recursive scan.
--
-- Must be called within catalog:withWriteAccessDo().
function CollectionManager.findOrCreateLeafSet(catalog, sourceDir, rootSet)
    rootSet = rootSet or CollectionManager.findOrCreateRootSet(catalog)
    local components = CollectionManager.getRelativePathComponents(sourceDir, catalog)

    if #components == 0 then
        -- No relative path — just use the root set directly
        return rootSet
    end

    local currentParent = rootSet
    for _, component in ipairs(components) do
        local child = findChildSet(currentParent, component)
        if child then
            currentParent = child
        else
            logger:info("Creating collection set: " .. component ..
                        " under " .. currentParent:getName())
            currentParent = catalog:createCollectionSet(component, currentParent, true)
            logger:info("Created collection set: " .. component)
        end
    end

    return currentParent
end

-- Find the nested collection set hierarchy for a source directory without
-- creating anything. Returns the leaf set or nil if any component is missing.
function CollectionManager.findLeafSet(catalog, sourceDir, rootSet)
    if not rootSet then
        local prefs = LrPrefs.prefsForPlugin()
        local rootName = prefs.collection_set_prefix or "Focus Stacks"
        rootSet = findChildSet(catalog, rootName)
        if not rootSet then
            return nil
        end
    end

    local components = CollectionManager.getRelativePathComponents(sourceDir, catalog)
    return findLeafSetFromComponents(rootSet, components)
end

-- Ensure the nested collection set hierarchy exists, creating at most one
-- level per write transaction. Lightroom SDK objects can become unusable if
-- we continue traversing immediately after creation in the same write block.
function CollectionManager.ensureLeafSet(catalog, sourceDir, rootSet)
    rootSet = rootSet or CollectionManager.findOrCreateRootSet(catalog)
    local components = CollectionManager.getRelativePathComponents(sourceDir, catalog)

    if #components == 0 then
        return rootSet
    end

    local existing = findLeafSetFromComponents(rootSet, components)
    if existing then
        return existing
    end

    local built = {}
    local currentParent = rootSet
    for _, component in ipairs(components) do
        local child = findChildSet(currentParent, component)
        if not child then
            local builtSnapshot = {}
            for i = 1, #built do
                builtSnapshot[i] = built[i]
            end

            catalog:withWriteAccessDo("Create collection set path component", function()
                local parent = rootSet
                if #builtSnapshot > 0 then
                    parent = findLeafSetFromComponents(rootSet, builtSnapshot)
                    if not parent then
                        error("Parent collection set missing while creating path component: " .. component)
                    end
                end
                logger:info("Creating collection set: " .. component ..
                    " under " .. parent:getName())
                catalog:createCollectionSet(component, parent, true)
                logger:info("Created collection set: " .. component)
            end)
            child = findChildSet(currentParent, component)
            if not child then
                child = findLeafSetFromComponents(rootSet, builtSnapshot)
                if child then
                    child = findChildSet(child, component)
                else
                    child = findChildSet(rootSet, component)
                end
            end
            if not child then
                error("Collection set was not found after creation: " .. component)
            end
        end

        table.insert(built, component)
        currentParent = child
    end

    return currentParent
end

-- Find a collection by name within a collection set.
-- Returns the collection or nil.
function CollectionManager.findCollection(collectionSet, name)
    local collections = collectionSet:getChildCollections()
    for _, c in ipairs(collections) do
        if c:getName() == name then
            return c
        end
    end
    return nil
end

-- Create a collection within a collection set.
-- Must be called within catalog:withWriteAccessDo().
function CollectionManager.createCollection(catalog, collectionSet, name)
    logger:info("Creating collection: " .. name)
    local collection = catalog:createCollection(name, collectionSet, true)
    logger:info("Created collection object for: " .. name)
    return collection
end

local function getPhotoPluginValue(photo, fieldId)
    return photo:getPropertyForPlugin(_PLUGIN, fieldId)
end

local function photoHasRole(photo, role)
    return getPhotoPluginValue(photo, ROLE_FIELD) == role
end

function CollectionManager.getSourceRoleValue()
    return ROLE_SOURCE
end

function CollectionManager.getResultRoleValue()
    return ROLE_RESULT
end

function CollectionManager.buildResultParams(method, smoothing, radius, sourceCount)
    return string.format("m%d_s%d_r%d_c%d",
        tonumber(method) or 0,
        tonumber(smoothing) or 0,
        tonumber(radius) or 0,
        tonumber(sourceCount) or 0)
end

function CollectionManager.generateStackId()
    stackIdCounter = stackIdCounter + 1
    local currentTime = LrDate.currentTime()
    local seconds = math.floor(currentTime)
    local micros = math.floor((currentTime - seconds) * 1000000)
    local randomPart = math.random(0, 0xffffff)
    return string.format(
        "fs-%08x-%05x-%04x-%06x",
        seconds,
        micros % 0xfffff,
        stackIdCounter % 0xffff,
        randomPart
    )
end

function CollectionManager.setPhotoState(photo, role, stackId, stackName, resultParams)
    photo:setPropertyForPlugin(_PLUGIN, ROLE_FIELD, role)
    photo:setPropertyForPlugin(_PLUGIN, STACK_ID_FIELD, stackId)
    photo:setPropertyForPlugin(_PLUGIN, STACK_NAME_FIELD, stackName)
    photo:setPropertyForPlugin(_PLUGIN, RESULT_PARAMS_FIELD, resultParams)
end

function CollectionManager.clearPhotoState(photo)
    photo:setPropertyForPlugin(_PLUGIN, ROLE_FIELD, nil)
    photo:setPropertyForPlugin(_PLUGIN, STACK_ID_FIELD, nil)
    photo:setPropertyForPlugin(_PLUGIN, STACK_NAME_FIELD, nil)
    photo:setPropertyForPlugin(_PLUGIN, RESULT_PARAMS_FIELD, nil)
end

function CollectionManager.getPhotoStackId(photo)
    return getPhotoPluginValue(photo, STACK_ID_FIELD)
end

function CollectionManager.getPhotoStackName(photo)
    return getPhotoPluginValue(photo, STACK_NAME_FIELD)
end

function CollectionManager.getPhotoResultParams(photo)
    return getPhotoPluginValue(photo, RESULT_PARAMS_FIELD)
end

-- Check if a collection has a result photo.
function CollectionManager.hasResult(collection)
    local photos = collection:getPhotos()
    for _, photo in ipairs(photos) do
        if photoHasRole(photo, ROLE_RESULT) then
            return true, photo
        end
    end
    return false, nil
end

-- Get all result photos in a collection.
function CollectionManager.getResultPhotos(collection)
    local results = {}
    local photos = collection:getPhotos()
    for _, photo in ipairs(photos) do
        if photoHasRole(photo, ROLE_RESULT) then
            table.insert(results, photo)
        end
    end
    return results
end

-- Get all source photos in a collection.
function CollectionManager.getSourcePhotos(collection)
    local sources = {}
    local photos = collection:getPhotos()
    for _, photo in ipairs(photos) do
        if photoHasRole(photo, ROLE_SOURCE) then
            table.insert(sources, photo)
        end
    end
    return sources
end

-- Get non-rejected source photos in a collection.
-- Filters out photos with pickStatus == -1 (rejected).
function CollectionManager.getActiveSourcePhotos(collection)
    local sources = {}
    local photos = collection:getPhotos()
    for _, photo in ipairs(photos) do
        local pickStatus = photo:getRawMetadata('pickStatus')
        if pickStatus ~= -1 and photoHasRole(photo, ROLE_SOURCE) then
            table.insert(sources, photo)
        end
    end
    return sources
end

-- Find or create a keyword by name.
-- Must be called within catalog:withWriteAccessDo().
function CollectionManager.findOrCreateKeyword(catalog, keywordName)
    local keywords = catalog:getKeywords() or {}
    for _, keyword in ipairs(keywords) do
        if keyword:getName() == keywordName then
            return keyword
        end
    end
    return catalog:createKeyword(keywordName, {}, false, nil, true)
end

local function trimKeywordName(keywordName)
    if type(keywordName) ~= "string" then
        return nil
    end

    local trimmed = keywordName:match("^%s*(.-)%s*$")
    if trimmed == "" then
        return nil
    end

    return trimmed
end

-- Resolve an optional user-facing keyword.
-- Blank or whitespace-only names are treated as disabled.
-- Must be called within catalog:withWriteAccessDo().
function CollectionManager.findOrCreateOptionalKeyword(catalog, keywordName)
    local trimmed = trimKeywordName(keywordName)
    if not trimmed then
        return nil
    end

    return CollectionManager.findOrCreateKeyword(catalog, trimmed)
end

-- Apply an already-resolved optional keyword to a photo.
function CollectionManager.addResolvedKeyword(photo, keyword)
    if keyword then
        photo:addKeyword(keyword)
        return true
    end
    return false
end

-- Recursively find all focus stack collections under the root set.
-- Returns a flat list of {collection=, collectionSet=} tables.
function CollectionManager.findAllFocusStackCollections(catalog)
    local rootSet = nil
    local prefs = LrPrefs.prefsForPlugin()
    local rootName = prefs.collection_set_prefix or "Focus Stacks"

    local topSets = catalog:getChildCollectionSets()
    for _, cs in ipairs(topSets) do
        if cs:getName() == rootName then
            rootSet = cs
            break
        end
    end

    if not rootSet then
        return {}
    end

    local results = {}

    local function walkSets(parentSet)
        -- Collect collections at this level
        local collections = parentSet:getChildCollections()
        for _, c in ipairs(collections) do
            if CollectionManager.isStackCollection(c) then
                table.insert(results, {
                    collection = c,
                    collectionSet = parentSet,
                })
            end
        end
        -- Recurse into child sets
        local childSets = parentSet:getChildCollectionSets()
        for _, cs in ipairs(childSets) do
            walkSets(cs)
        end
    end

    walkSets(rootSet)
    return results
end

-- Recursively check if a collection set is empty (no collections or child sets
-- with collections anywhere beneath it).
function CollectionManager.isCollectionSetEmpty(collectionSet)
    local collections = collectionSet:getChildCollections()
    if #collections > 0 then
        return false
    end
    local childSets = collectionSet:getChildCollectionSets()
    if #childSets == 0 then
        return true
    end
    for _, cs in ipairs(childSets) do
        if not CollectionManager.isCollectionSetEmpty(cs) then
            return false
        end
    end
    return true
end

-- Clean up empty collection sets by walking up from a leaf.
-- Deletes the given set if empty, then checks its parent, etc.
-- Stops at the root set (does not delete the root).
-- Must be called within catalog:withWriteAccessDo().
function CollectionManager.cleanupEmptySets(collectionSet)
    local prefs = LrPrefs.prefsForPlugin()
    local rootName = prefs.collection_set_prefix or "Focus Stacks"

    local current = collectionSet
    while current do
        local name = current:getName()
        -- Don't delete the root set
        if name == rootName then
            break
        end
        if CollectionManager.isCollectionSetEmpty(current) then
            local parent = current:getParent()
            logger:info("Removing empty collection set: " .. name)
            current:delete()
            current = parent
        else
            break
        end
    end
end

-- Parse a result filename into its encoded parameters.
-- Returns a table { count, method, smoothing, radius } or nil if not a result file.
-- Expected format: *_<N>f_m<Letter>_s<S>_r<R>_stacked.<ext>
function CollectionManager.parseResultFilename(filename)
    if not filename then return nil end
    local count, method, smoothing, radius =
        string.match(filename, "_(%d+)f_m([A-Z])_s(%d+)_r(%d+)_stacked%.[^.]+$")
    if not count then return nil end
    return {
        count = tonumber(count),
        method = method,
        smoothing = tonumber(smoothing),
        radius = tonumber(radius),
    }
end

-- Check if a result photo matches specific Helicon params AND frame count.
-- Uses a strict parse rather than substring matching so "15f" doesn't
-- accidentally match "115f" and param digits can't collide across fields.
function CollectionManager.resultMatchesParams(photo, method, smoothing, radius, sourceCount)
    local expected = CollectionManager.buildResultParams(method, smoothing, radius, sourceCount)
    return CollectionManager.getPhotoResultParams(photo) == expected
end

return CollectionManager
