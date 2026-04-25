local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'
local LrProgressScope = import 'LrProgressScope'
local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'
local LrDate = import 'LrDate'

local ScriptBridge = require 'ScriptBridge'
local CollectionManager = require 'CollectionManager'
local FsmCatalogMeta = require 'FsmCatalogMeta'
local FsmExifToolMeta = require 'FsmExifToolMeta'
local Timing = require 'Timing'
local logger = require 'Log'

local RAW_EXTENSIONS = {
    cr2 = true, cr3 = true, nef = true, arw = true, dng = true,
    raf = true, orf = true, rw2 = true, raw = true,
}

local function fileExtension(name)
    local ext = name:match("%.([^.]+)$")
    return ext and ext:lower() or nil
end

local function enumerateRawFiles(dirPath)
    local files = {}
    if not dirPath or not LrFileUtils.exists(dirPath) then
        return files
    end
    for entry in LrFileUtils.directoryEntries(dirPath) do
        if LrFileUtils.exists(entry) == "file" then
            local ext = fileExtension(entry)
            if ext and RAW_EXTENSIONS[ext] then
                table.insert(files, entry)
            end
        end
    end
    table.sort(files)
    return files
end

local function buildProvider(prefs, catalog)
    local kind = prefs.metadata_provider or "lr_catalog"
    if kind == "exiftool" then
        local path = prefs.exiftool_path
        if path == "" then path = nil end
        return function(filePaths)
            return FsmExifToolMeta.getMetadataForFiles(path, filePaths)
        end, "exiftool"
    end
    return function(filePaths)
        return FsmCatalogMeta.getMetadataForFiles(catalog, filePaths)
    end, "lr_catalog"
end

local function writeMetadataJson(metadata)
    -- Serialize the metadata dict as a list of entries (matches exiftool's
    -- JSON output shape, which detect_stacks.py also accepts as a dict).
    local entries = {}
    for _, entry in pairs(metadata) do
        table.insert(entries, entry)
    end
    if #entries == 0 then
        return nil
    end

    local function escapeString(s)
        s = s:gsub('\\', '\\\\'):gsub('"', '\\"')
        s = s:gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
        return s
    end

    local function encodeValue(v)
        local t = type(v)
        if t == "string" then
            return '"' .. escapeString(v) .. '"'
        elseif t == "number" then
            if v ~= v then return "null" end -- NaN
            return string.format("%.10g", v)
        elseif t == "boolean" then
            return v and "true" or "false"
        elseif v == nil then
            return "null"
        end
        return "null"
    end

    local parts = { "[" }
    for i, entry in ipairs(entries) do
        if i > 1 then table.insert(parts, ",") end
        table.insert(parts, "{")
        local first = true
        for k, v in pairs(entry) do
            if not first then table.insert(parts, ",") end
            first = false
            table.insert(parts, '"' .. escapeString(tostring(k)) .. '":' .. encodeValue(v))
        end
        table.insert(parts, "}")
    end
    table.insert(parts, "]")

    local tempDir = LrPathUtils.getStandardFilePath('temp')
    local name = string.format("fsm_metadata_%d_%d.json", os.time(), math.random(0, 0xfffff))
    local path = LrPathUtils.child(tempDir, name)
    local f = io.open(path, "w")
    if not f then
        return nil
    end
    f:write(table.concat(parts))
    f:close()
    return path
end

local DetectionService = {}

local function withCatalogWriteRetry(catalog, actionName, fn, opts)
    opts = opts or {}
    local maxAttempts = opts.maxAttempts or 6
    local sleepSeconds = opts.sleepSeconds or 0.5
    local timeoutSeconds = opts.timeoutSeconds or 5

    for attempt = 1, maxAttempts do
        local status = catalog:withWriteAccessDo(actionName, fn, {
            timeout = timeoutSeconds,
        })

        if status == nil or status == "executed" then
            return status
        end

        if status ~= "aborted" then
            error("Unexpected write access status for '" .. actionName ..
                "': " .. tostring(status))
        end

        if attempt == maxAttempts then
            error("Timed out waiting for catalog write access for '" ..
                actionName .. "' after " .. tostring(maxAttempts) ..
                " attempts (" .. tostring(timeoutSeconds) .. "s each)")
        end

        logger:info("Write timed out for '" .. actionName ..
            "' (attempt " .. tostring(attempt) .. "/" ..
            tostring(maxAttempts) .. " after " ..
            tostring(timeoutSeconds) .. "s), retrying in " ..
            tostring(sleepSeconds) .. "s")
        LrTasks.sleep(sleepSeconds)
    end
end

local function createCollectionsForDirectory(catalog, directoryResult, rootSet, timing)
    local prefs = LrPrefs.prefsForPlugin()
    local sourceDir = directoryResult.directory
    local stacks = directoryResult.stacks or {}
    local newCount = 0
    local existingCount = 0
    local adoptedResults = 0
    local skippedEmpty = 0
    local skippedIncomplete = 0

    if #stacks == 0 then
        return 0, 0, 0, 0, 0
    end

    timing:measure("ensureLeafSet", function()
        CollectionManager.ensureLeafSet(catalog, sourceDir, rootSet)
    end)

    local collectionSet = CollectionManager.findLeafSet(catalog, sourceDir, rootSet)
    if not collectionSet then
        error("Collection set was not found after creation for: " .. tostring(sourceDir))
    end

    local preparedStacks = {}
    for _, stack in ipairs(stacks) do
        local collName = CollectionManager.collectionName(stack.name, stack.count)
        local existing = CollectionManager.findCollection(collectionSet, collName)
        local resultFiles = stack.result_files or {}

        local photosToAdd = {}
        local missingCount = 0
        local lookupStart = LrDate.currentTime()
        for _, filePath in ipairs(stack.files) do
            local photo = catalog:findPhotoByPath(filePath, false)
            if photo then
                table.insert(photosToAdd, photo)
            else
                missingCount = missingCount + 1
                logger:info("Source photo not found in catalog: " .. filePath)
            end
        end
        timing:add("findPhotoByPath_per_stack", LrDate.currentTime() - lookupStart)

        if existing then
            existingCount = existingCount + 1
            if #photosToAdd > 0 and missingCount == 0 then
                table.insert(preparedStacks, {
                    name = collName,
                    stackId = CollectionManager.getPhotoStackId(photosToAdd[1]) or CollectionManager.generateStackId(),
                    stackName = stack.name,
                    photos = photosToAdd,
                    collectionAlreadyExists = true,
                    resultFiles = resultFiles,
                })
            end
        else
            if #photosToAdd == 0 then
                skippedEmpty = skippedEmpty + 1
            elseif missingCount > 0 then
                skippedIncomplete = skippedIncomplete + 1
            else
                table.insert(preparedStacks, {
                    name = collName,
                    stackId = CollectionManager.generateStackId(),
                    stackName = stack.name,
                    photos = photosToAdd,
                    collectionAlreadyExists = false,
                    resultFiles = resultFiles,
                })
            end
        end
    end

    -- Batch multiple stacks into a single write transaction. Two reasons:
    --   (1) Each withWriteAccessDo has fixed overhead and locks the catalog,
    --       so 666 small transactions (3 per stack) made LR feel laggy.
    --   (2) Within one write block we can use the LrCollection returned by
    --       createCollection directly, instead of round-tripping through
    --       findCollection (which is unreliable in the same block right after
    --       a creation). That collapses create+tag+populate into one pass.
    -- LrTasks.yield between batches lets the UI thread repaint.
    --
    -- Batch size is bounded by total photos rather than stack count: long
    -- stacks should not produce disproportionately long freezes. We always
    -- include at least one stack, and never split a stack across batches
    -- (so a batch boundary is also a collection boundary, no half-populated
    -- collections visible during a repaint).
    local TARGET_PHOTOS_PER_BATCH = 300

    local function applyBatch(batch)
        withCatalogWriteRetry(catalog, "Apply focus stack collections", function()
            local srcColorLabel = prefs.source_color_label or "red"
            local resultColorLabel = prefs.result_color_label or "green"
            local userSourceKeyword = CollectionManager.findOrCreateOptionalKeyword(catalog, prefs.source_keyword)
            local userResultKeyword = CollectionManager.findOrCreateOptionalKeyword(catalog, prefs.result_keyword)
            local leafSet = CollectionManager.findLeafSet(catalog, sourceDir, rootSet)
            if not leafSet then
                error("Collection set missing for: " .. tostring(sourceDir))
            end

            for _, prepared in ipairs(batch) do
                local collection
                if prepared.collectionAlreadyExists then
                    collection = CollectionManager.findCollection(leafSet, prepared.name)
                    if not collection then
                        error("Existing collection vanished: " .. prepared.name)
                    end
                else
                    collection = CollectionManager.createCollection(catalog, leafSet, prepared.name)
                    if not collection then
                        error("Failed to create collection: " .. prepared.name)
                    end
                end

                for _, photo in ipairs(prepared.photos) do
                    CollectionManager.setPhotoState(
                        photo,
                        CollectionManager.getSourceRoleValue(),
                        prepared.stackId,
                        prepared.stackName,
                        nil
                    )
                    CollectionManager.addResolvedKeyword(photo, userSourceKeyword)
                    if srcColorLabel ~= "none" then
                        photo:setRawMetadata('colorNameForLabel', srcColorLabel)
                    end
                end
                collection:addPhotos(prepared.photos)

                for _, resultInfo in ipairs(prepared.resultFiles or {}) do
                    local resultPhoto = catalog:findPhotoByPath(resultInfo.path, false)
                    if not resultPhoto then
                        resultPhoto = catalog:addPhoto(resultInfo.path)
                    end
                    if resultPhoto then
                        CollectionManager.setPhotoState(
                            resultPhoto,
                            CollectionManager.getResultRoleValue(),
                            prepared.stackId,
                            prepared.stackName,
                            nil
                        )
                        CollectionManager.addResolvedKeyword(resultPhoto, userResultKeyword)
                        if resultColorLabel ~= "none" then
                            resultPhoto:setRawMetadata('colorNameForLabel', resultColorLabel)
                        end
                        collection:addPhotos({ resultPhoto })
                        adoptedResults = adoptedResults + 1
                    end
                end
            end
        end)
    end

    local function stackPhotoCount(prepared)
        return #(prepared.photos or {}) + #(prepared.resultFiles or {})
    end

    local i = 1
    while i <= #preparedStacks do
        local batch = { preparedStacks[i] }
        local photoCount = stackPhotoCount(preparedStacks[i])
        i = i + 1
        while i <= #preparedStacks do
            local nextCount = stackPhotoCount(preparedStacks[i])
            if photoCount + nextCount > TARGET_PHOTOS_PER_BATCH then
                break
            end
            table.insert(batch, preparedStacks[i])
            photoCount = photoCount + nextCount
            i = i + 1
        end

        local batchStart = LrDate.currentTime()
        applyBatch(batch)
        timing:add("write_apply_batch", LrDate.currentTime() - batchStart)

        for _, prepared in ipairs(batch) do
            if not prepared.collectionAlreadyExists then
                newCount = newCount + 1
            end
        end

        LrTasks.yield()
    end

    return newCount, existingCount, adoptedResults, skippedEmpty, skippedIncomplete
end

local function makeDirectoryLabel(scanRoot, directoryPath)
    if directoryPath == scanRoot then
        return LrPathUtils.leafName(scanRoot) or scanRoot
    end

    local prefix = scanRoot
    if string.sub(prefix, -1) ~= "/" then
        prefix = prefix .. "/"
    end

    local relative = directoryPath
    if string.sub(directoryPath, 1, #prefix) == prefix then
        relative = string.sub(directoryPath, #prefix + 1)
    end

    return (LrPathUtils.leafName(scanRoot) or scanRoot) .. "/" .. relative
end

local function setProgressForDirectory(progressScope, scanRoot, directoryPath, index, totalDirs)
    progressScope:setPortionComplete(index - 1, totalDirs)
    progressScope:setCaption(string.format(
        "Scanning %s (%d/%d)",
        makeDirectoryLabel(scanRoot, directoryPath),
        index,
        totalDirs
    ))
end

local function setCreateProgressForDirectory(progressScope, scanRoot, directoryPath, index, totalDirs)
    progressScope:setPortionComplete(index, totalDirs)
    progressScope:setCaption(string.format(
        "Created collections for %s (%d/%d)",
        makeDirectoryLabel(scanRoot, directoryPath),
        index,
        totalDirs
    ))
end

local function buildSummaryLines(stats)
    local summaryLines = {}
    table.insert(summaryLines, string.format("Scanned %d directories", stats.dirsScanned or 0))
    table.insert(summaryLines, string.format("Detected %d focus stacks", stats.totalStacks or 0))
    if (stats.totalRaw or 0) > 0 then
        table.insert(summaryLines, string.format("  Raw files inspected: %d", stats.totalRaw))
    end
    if (stats.totalNew or 0) > 0 then
        table.insert(summaryLines, string.format("  New collections created: %d", stats.totalNew))
    end
    if (stats.totalExisting or 0) > 0 then
        table.insert(summaryLines, string.format("  Already detected: %d", stats.totalExisting))
    end
    if (stats.totalAdoptedResults or 0) > 0 then
        table.insert(summaryLines, string.format("  Existing result files adopted: %d", stats.totalAdoptedResults))
    end
    if (stats.totalSkippedEmpty or 0) > 0 then
        table.insert(summaryLines, string.format(
            "  Skipped (source photos not imported into catalog yet): %d",
            stats.totalSkippedEmpty))
    end
    if (stats.totalSkippedIncomplete or 0) > 0 then
        table.insert(summaryLines, string.format(
            "  Skipped (detected stack only partially imported into catalog): %d",
            stats.totalSkippedIncomplete))
    end
    if (stats.totalRejected or 0) > 0 then
        table.insert(summaryLines, string.format(
            "  Filtered out as non-focus-stacks: %d",
            stats.totalRejected))
    end
    if stats.failedDirectory then
        table.insert(summaryLines, "")
        table.insert(summaryLines, "Stopped while scanning:")
        table.insert(summaryLines, "  " .. tostring(stats.failedDirectory))
        if stats.failureMessage then
            table.insert(summaryLines, "  " .. tostring(stats.failureMessage))
        end
        if (stats.completedDirs or 0) > 0 then
            table.insert(summaryLines, "")
            table.insert(summaryLines, "Collections created before the failure were kept.")
        end
    elseif (stats.totalNew or 0) > 0 then
        table.insert(summaryLines, "")
        table.insert(summaryLines, "Next steps:")
        table.insert(summaryLines, "  1. Review collections in the Focus Stacks hierarchy")
        table.insert(summaryLines, "  2. Reject (X) any blurry source frames")
        table.insert(summaryLines, "  3. Select collection(s) and run 'Process Focus Stacks'")
    end

    return summaryLines
end

DetectionService.buildSummaryLines = buildSummaryLines

local function copyStats(state)
    return {
        dirsScanned = state.completedDirs,
        completedDirs = state.completedDirs,
        totalRaw = state.totalRaw,
        totalStacks = state.totalStacks,
        totalRejected = state.totalRejected,
        totalNew = state.totalNew,
        totalExisting = state.totalExisting,
        totalAdoptedResults = state.totalAdoptedResults,
        totalSkippedEmpty = state.totalSkippedEmpty,
        totalSkippedIncomplete = state.totalSkippedIncomplete,
    }
end

function DetectionService.run(scanRoot, queuePosition, queueTotal, opts)
    opts = opts or {}
    logger:info("=== DETECT FOCUS STACKS STARTED ===")
    logger:info("Scanning recursively: " .. scanRoot)

    local timing = Timing.new()
    local runStart = LrDate.currentTime()

    local state = {
        scanRoot = scanRoot,
        catalog = LrApplication.activeCatalog(),
        totalNew = 0,
        totalExisting = 0,
        totalAdoptedResults = 0,
        totalSkippedEmpty = 0,
        totalSkippedIncomplete = 0,
        totalStacks = 0,
        totalRejected = 0,
        totalRaw = 0,
        completedDirs = 0,
    }

    local title = "Detecting focus stacks..."
    if queueTotal and queueTotal > 1 then
        title = string.format("Detecting focus stacks (%d/%d)", queuePosition or 1, queueTotal)
    end

    local detectProgress = LrProgressScope { title = title }
    detectProgress:setCaption("Enumerating folders...")

    local listing, listErr, listTimings = ScriptBridge.listDirectoriesWithRawFiles(scanRoot)
    if listTimings then
        timing:add("list_dirs_subprocess", listTimings.subprocessSeconds or 0)
        timing:add("list_dirs_parse", listTimings.parseSeconds or 0)
    end
    if listing and listing._timing then
        timing:mergePythonTiming(listing._timing, "py_list_")
    end
    if not listing then
        detectProgress:done()
        LrDialogs.message("Detection Failed", listErr or "Unknown error", "critical")
        return "failed", copyStats(state)
    end

    if listing.error then
        detectProgress:done()
        LrDialogs.message("Detection Failed", listing.error, "critical")
        return "failed", copyStats(state)
    end

    state.directories = listing.directories or {}
    state.totalDirectories = #state.directories

    if state.totalDirectories == 0 then
        detectProgress:done()
        if not opts.suppressDialogs then
            LrDialogs.message("Focus Stack Manager", "No raw files found in this folder tree.", "info")
        end
        return "completed", copyStats(state)
    end

    local prefs = LrPrefs.prefsForPlugin()
    local provider, providerKind = buildProvider(prefs, state.catalog)
    local validateFocusDistance = prefs.validate_focus_distance
    if validateFocusDistance == nil then
        validateFocusDistance = false
    end
    logger:info(string.format(
        "Metadata provider: %s; focus distance validation: %s",
        providerKind, tostring(validateFocusDistance)))

    local rootSet
    timing:measure("write_find_or_create_root_set", function()
        withCatalogWriteRetry(state.catalog, "Find or create focus stack root set", function()
            rootSet = CollectionManager.findOrCreateRootSet(state.catalog)
        end)
    end)

    for index, directoryPath in ipairs(state.directories) do
        if detectProgress:isCanceled() then
            logger:info("User cancelled detection after " .. tostring(index - 1) .. " directories")
            break
        end

        setProgressForDirectory(detectProgress, state.scanRoot, directoryPath, index, state.totalDirectories)

        -- Gather metadata via the configured provider, write to a temp JSON
        -- file, and pass that path to the Python script. If gathering fails
        -- (catalog miss, exiftool error), fall through with no metadata path
        -- and Python's built-in exiftool path takes over for that directory.
        local providerStart = LrDate.currentTime()
        local rawFiles = enumerateRawFiles(directoryPath)
        timing:add("enumerate_raw_files", LrDate.currentTime() - providerStart)

        -- Don't wrap this in pcall: catalog:findPhotoByPath yields internally,
        -- and Lua 5.1's pcall can't span a yield ("Yielding is not allowed
        -- within a C or metamethod call"). If the provider blows up we want
        -- the real stack trace anyway.
        local metadataPath = nil
        if rawFiles and #rawFiles > 0 then
            local providerCallStart = LrDate.currentTime()
            local metadata = provider(rawFiles)
            timing:add("provider_getMetadata", LrDate.currentTime() - providerCallStart)
            if metadata then
                local writeStart = LrDate.currentTime()
                metadataPath = writeMetadataJson(metadata)
                timing:add("metadata_json_write", LrDate.currentTime() - writeStart)
            end
        end

        local detectOpts = {
            metadataPath = metadataPath,
            validateFocusDistance = validateFocusDistance,
        }
        local dirResult, detectErr, detectTimings = ScriptBridge.detectStacksInDirectory(
            directoryPath, nil, nil, nil, detectOpts)

        if metadataPath then
            pcall(function() LrFileUtils.delete(metadataPath) end)
        end
        if detectTimings then
            timing:add("detect_subprocess", detectTimings.subprocessSeconds or 0)
            timing:add("detect_parse", detectTimings.parseSeconds or 0)
        end
        if dirResult and dirResult._timing then
            timing:mergePythonTiming(dirResult._timing, "py_detect_")
        end
        if not dirResult then
            detectProgress:done()
            LrDialogs.message("Detection Failed", table.concat(buildSummaryLines({
                dirsScanned = state.completedDirs,
                completedDirs = state.completedDirs,
                totalRaw = state.totalRaw,
                totalStacks = state.totalStacks,
                totalRejected = state.totalRejected,
                totalNew = state.totalNew,
                totalExisting = state.totalExisting,
                totalAdoptedResults = state.totalAdoptedResults,
                totalSkippedEmpty = state.totalSkippedEmpty,
                totalSkippedIncomplete = state.totalSkippedIncomplete,
                failedDirectory = makeDirectoryLabel(state.scanRoot, directoryPath),
                failureMessage = detectErr or "Unknown error",
            }), "\n"), "critical")
            return "failed", copyStats(state)
        end

        if dirResult.error then
            detectProgress:done()
            LrDialogs.message("Detection Failed", table.concat(buildSummaryLines({
                dirsScanned = state.completedDirs,
                completedDirs = state.completedDirs,
                totalRaw = state.totalRaw,
                totalStacks = state.totalStacks,
                totalRejected = state.totalRejected,
                totalNew = state.totalNew,
                totalExisting = state.totalExisting,
                totalAdoptedResults = state.totalAdoptedResults,
                totalSkippedEmpty = state.totalSkippedEmpty,
                totalSkippedIncomplete = state.totalSkippedIncomplete,
                failedDirectory = makeDirectoryLabel(state.scanRoot, directoryPath),
                failureMessage = dirResult.error,
            }), "\n"), "critical")
            return "failed", copyStats(state)
        end

        detectProgress:setCaption("Creating collections for " ..
            makeDirectoryLabel(state.scanRoot, directoryPath) ..
            string.format(" (%d/%d)", index, state.totalDirectories))

        local createStart = LrDate.currentTime()
        local newCount, existingCount, adoptedResults, skippedEmpty, skippedIncomplete =
            createCollectionsForDirectory(state.catalog, dirResult, rootSet, timing)
        timing:add("create_collections_total", LrDate.currentTime() - createStart)

        state.totalRaw = state.totalRaw + (dirResult.total_raw_files or 0)
        state.totalStacks = state.totalStacks + #(dirResult.stacks or {})
        state.totalRejected = state.totalRejected + #(dirResult.rejected_stacks or {})
        state.totalNew = state.totalNew + newCount
        state.totalExisting = state.totalExisting + existingCount
        state.totalAdoptedResults = state.totalAdoptedResults + adoptedResults
        state.totalSkippedEmpty = state.totalSkippedEmpty + skippedEmpty
        state.totalSkippedIncomplete = state.totalSkippedIncomplete + skippedIncomplete
        state.completedDirs = index

        setCreateProgressForDirectory(detectProgress, state.scanRoot, directoryPath, index, state.totalDirectories)
    end

    detectProgress:done()

    timing:add("run_total", LrDate.currentTime() - runStart)
    logger:info(timing:formatSummary(string.format(
        "=== DETECT FOCUS STACKS TIMING (dirs=%d) ===", state.completedDirs)))

    if state.completedDirs < state.totalDirectories then
        if not opts.suppressDialogs then
            LrDialogs.message("Focus Stack Detection Cancelled", table.concat(buildSummaryLines(copyStats(state)), "\n"), "warning")
        end
        logger:info("=== DETECT FOCUS STACKS CANCELLED ===")
        return "cancelled", copyStats(state)
    end

    if not opts.suppressDialogs then
        LrDialogs.message("Focus Stack Detection Complete", table.concat(buildSummaryLines(copyStats(state)), "\n"), "info")
    end
    logger:info("=== DETECT FOCUS STACKS COMPLETE ===")
    return "completed", copyStats(state)
end

return DetectionService
