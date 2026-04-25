-- ProcessingQueue.lua
-- Manages a persistent queue of focus stacks to process through Helicon Focus.
--
-- The queue is stored in plugin prefs as a serializable table. A single background
-- worker task pulls items one at a time and processes them. If the worker is already
-- running, new items are simply appended — the worker picks them up automatically.
--
-- This ensures:
--   - Only one Helicon Focus instance runs at a time (it's single-instance)
--   - Lightroom stays responsive (processing is async)
--   - Multiple "Process Focus Stacks" invocations append to the same queue
--   - The queue survives across dialog closings within the current Lightroom session
--
-- Queue entry format (stored in prefs.processing_queue):
--   {
--     stackName = "_ON_1234-_ON_1248",
--     files = {"/path/to/file1.dng", ...},   -- source file paths
--     resultsDir = "/path/to/source/folder",  -- result goes in same folder as sources
--     method = 1,
--     radius = 11,
--     smoothing = 5,
--   }
--
-- The worker reads actual source files from the collection at processing time
-- (supporting partial stacks), falling back to the queued file list if needed.

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'
local LrProgressScope = import 'LrProgressScope'
local LrTasks = import 'LrTasks'

local ScriptBridge = require 'ScriptBridge'
local CollectionManager = require 'CollectionManager'
local logger = require 'Log'

local function formatDuration(seconds)
    seconds = math.max(0, math.floor((seconds or 0) + 0.5))
    local hours = math.floor(seconds / 3600)
    local minutes = math.floor((seconds % 3600) / 60)
    local secs = seconds % 60

    if hours > 0 then
        return string.format("%dh %dm", hours, minutes)
    end
    if minutes > 0 then
        return string.format("%dm %ds", minutes, secs)
    end
    return string.format("%ds", secs)
end

local function isWriteBlocked(err)
    return type(err) == "string" and
        string.find(err, "blocked by another write access call", 1, true) ~= nil
end

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

local ProcessingQueue = {}

-- Module-level flag: is the worker currently running?
-- This is not persisted across Lightroom restarts.
local workerRunning = false

-- Read-modify-write the queue. Lightroom's Lua uses cooperative coroutines
-- that only yield at documented suspension points (LrTasks.sleep, catalog
-- write access, LrTasks.execute, etc.). The modifier below contains none of
-- those, so this block is atomic with respect to other tasks by construction
-- — no lock needed.
local function modifyQueue(modifier)
    local prefs = LrPrefs.prefsForPlugin()
    local queue = prefs.processing_queue or {}
    local result = modifier(queue)
    prefs.processing_queue = queue
    return queue, result
end

-- Get the current queue from prefs (read-only snapshot).
function ProcessingQueue.getQueue()
    local prefs = LrPrefs.prefsForPlugin()
    return prefs.processing_queue or {}
end

-- Get the number of items in the queue.
function ProcessingQueue.count()
    return #ProcessingQueue.getQueue()
end

-- Add items to the queue and start the worker if needed.
-- entries: list of queue entry tables (see format above)
function ProcessingQueue.enqueue(entries)
    modifyQueue(function(queue)
        for _, entry in ipairs(entries) do
            table.insert(queue, entry)
            logger:info("Enqueued: " .. entry.stackName)
        end
    end)
    logger:info("Queue size: " .. ProcessingQueue.count())
    ProcessingQueue.ensureWorkerRunning()
end

-- Clear the entire queue (e.g., user cancellation).
function ProcessingQueue.clear()
    modifyQueue(function(queue)
        -- Clear in-place so the reference held by modifyQueue is updated
        for i = #queue, 1, -1 do
            table.remove(queue, i)
        end
    end)
    logger:info("Queue cleared")
end

-- Check if the worker is running.
function ProcessingQueue.isWorkerRunning()
    return workerRunning
end

-- Start the worker if it isn't already running.
function ProcessingQueue.ensureWorkerRunning()
    if workerRunning then
        logger:info("Worker already running, new items will be picked up")
        return
    end
    workerRunning = true
    logger:info("Starting processing worker")

    LrTasks.startAsyncTask(function()
        local catalog = LrApplication.activeCatalog()
        local prefs = LrPrefs.prefsForPlugin()
        local resultColorLabel = prefs.result_color_label or "green"
        local workerStartTime = LrDate.currentTime()

        local progress = LrProgressScope {
            title = "Focus Stack Processing",
            functionContext = nil,
        }

        local totalCompleted = 0
        local totalErrors = 0
        local failures = {} -- list of { name = "...", err = "..." }

        while true do
            -- Check for cancellation
            if progress:isCanceled() then
                logger:info("Worker cancelled by user")
                ProcessingQueue.clear()
                break
            end

            -- Pull next item from queue (atomic read-modify-write)
            local entry
            modifyQueue(function(queue)
                if #queue == 0 then return end
                entry = table.remove(queue, 1)
            end)

            if not entry then
                logger:info("Queue empty, worker stopping")
                break
            end

            local doneCount = totalCompleted + totalErrors
            local queuedBehindCurrent = ProcessingQueue.count()
            local remaining = queuedBehindCurrent + 1
            local total = doneCount + remaining
            local percentDone = 0
            if total > 0 then
                percentDone = math.floor((doneCount / total) * 100 + 0.5)
            end

            local caption = string.format(
                "%s (%d/%d, %d%%)",
                entry.stackName, doneCount, total, percentDone
            )

            if doneCount >= 2 then
                local elapsed = math.max(0, LrDate.currentTime() - workerStartTime)
                local averagePerStack = elapsed / doneCount
                local etaSeconds = averagePerStack * remaining
                caption = string.format("%s, ~%s left", caption, formatDuration(etaSeconds))
            end

            progress:setCaption(
                caption
            )

            logger:info("Processing: " .. entry.stackName ..
                         " (" .. doneCount .. " done, " ..
                         remaining .. " remaining including current, " ..
                         percentDone .. "% complete)")

            -- Get actual source files from the collection (supports partial stacks)
            local actualFiles = ProcessingQueue.resolveSourceFiles(catalog, entry)

            if #actualFiles == 0 then
                logger:info("No source files found for " .. entry.stackName .. ", skipping")
                totalErrors = totalErrors + 1
                table.insert(failures, {
                    name = entry.stackName,
                    err = "No source files found (collection may be empty or all rejected)",
                })
            else
                -- Process through Helicon Focus (blocking call)
                local result, procErr = ScriptBridge.processStack(
                    actualFiles,
                    entry.resultsDir,
                    entry.method,
                    entry.radius,
                    entry.smoothing
                )

                if result and result.success then
                    local resultFile = result.result_file
                    logger:info("Completed: " .. entry.stackName .. " -> " .. resultFile)

                    -- Import result into catalog
                    ProcessingQueue.importResult(catalog, entry, resultFile, resultColorLabel)
                    totalCompleted = totalCompleted + 1
                else
                    local errMsg = (result and result.error) or procErr or "Unknown error"
                    logger:info("Failed: " .. entry.stackName .. " - " .. errMsg)
                    totalErrors = totalErrors + 1
                    table.insert(failures, {
                        name = entry.stackName,
                        err = errMsg,
                    })
                end
            end

            -- Update progress (approximate — queue size can change)
            local currentQueue = ProcessingQueue.getQueue()
            local totalWithQueue = totalCompleted + totalErrors + #currentQueue
            if totalWithQueue > 0 then
                progress:setPortionComplete(totalCompleted + totalErrors, totalWithQueue)
            end
        end

        progress:done()
        workerRunning = false

        if totalErrors > 0 then
            local lines = {
                "Focus stack processing finished with errors.",
                "",
                string.format("Completed: %d", totalCompleted),
                string.format("Errors:    %d", totalErrors),
            }

            if #failures > 0 then
                table.insert(lines, "")
                table.insert(lines, "Failed stacks:")
                local showCount = math.min(#failures, 10)
                for i = 1, showCount do
                    -- Truncate each error to keep the dialog readable.
                    local err = failures[i].err or ""
                    if #err > 140 then
                        err = string.sub(err, 1, 137) .. "..."
                    end
                    table.insert(lines, string.format("  • %s — %s",
                        failures[i].name, err))
                end
                if #failures > showCount then
                    table.insert(lines,
                        string.format("  (... and %d more; see plugin log for details)",
                            #failures - showCount))
                end
            end

            LrDialogs.message("Focus Stack Manager",
                table.concat(lines, "\n"),
                "warning")
        end

        logger:info("Worker finished. Completed: " .. totalCompleted ..
                     ", Errors: " .. totalErrors)
    end)
end

-- Look up the collection for a queue entry.
-- Uses the stored collectionId for O(1) lookup, falling back to a name-based
-- tree walk for queue entries created before collectionId was stored.
local function findCollectionForEntry(catalog, entry)
    if entry.collectionId then
        local collection = catalog:getCollectionByLocalIdentifier(entry.collectionId)
        if collection then
            return collection
        end
        logger:warn("Collection ID " .. tostring(entry.collectionId) ..
            " not found, falling back to name search for " .. entry.stackName)
    end

    -- Fallback: walk the tree and match by name prefix
    local allCollections = CollectionManager.findAllFocusStackCollections(catalog)
    for _, collEntry in ipairs(allCollections) do
        local collName = collEntry.collection:getName()
        if string.sub(collName, 1, #entry.stackName + 2) == entry.stackName .. " (" then
            return collEntry.collection
        end
    end
    return nil
end

-- Resolve the actual source file paths for a queue entry.
-- Checks the collection first (supports partial stacks where user removed or
-- rejected frames), falls back to the file list stored in the queue entry.
-- Rejected source photos (pickStatus == -1) are excluded.
function ProcessingQueue.resolveSourceFiles(catalog, entry)
    local collection = findCollectionForEntry(catalog, entry)
    if collection then
        local sourcePhotos = CollectionManager.getActiveSourcePhotos(collection)
        if #sourcePhotos > 0 then
            local files = {}
            for _, sp in ipairs(sourcePhotos) do
                local path = sp:getRawMetadata('path')
                if path then
                    table.insert(files, path)
                end
            end
            if #files > 0 then
                table.sort(files)
                return files
            end
        end
    end

    -- Fall back to the queued file list
    return entry.files or {}
end

-- Import a processed result file into the catalog and add it to the collection.
function ProcessingQueue.importResult(catalog, entry, resultFile, colorLabel)
    if not resultFile or not LrFileUtils.exists(resultFile) then
        logger:info("Result file not found: " .. tostring(resultFile))
        return
    end

    withCatalogWriteRetry(catalog, "Import focus stack result: " .. entry.stackName, function()
        local prefs = LrPrefs.prefsForPlugin()
        local userResultKeyword = CollectionManager.findOrCreateOptionalKeyword(catalog, prefs.result_keyword)

        -- Import or find the result photo
        local resultPhoto = catalog:findPhotoByPath(resultFile, false)
        if not resultPhoto then
            resultPhoto = catalog:addPhoto(resultFile)
        end

        if not resultPhoto then
            logger:info("Failed to import result photo: " .. resultFile)
            return
        end

        -- Tag and label
        CollectionManager.setPhotoState(
            resultPhoto,
            CollectionManager.getResultRoleValue(),
            entry.stackId,
            entry.stackName,
            CollectionManager.buildResultParams(
                entry.method,
                entry.smoothing,
                entry.radius,
                #entry.files
            )
        )
        CollectionManager.addResolvedKeyword(resultPhoto, userResultKeyword)
        if colorLabel and colorLabel ~= "none" then
            resultPhoto:setRawMetadata('colorNameForLabel', colorLabel)
        end

        -- Add to collection
        local collection = findCollectionForEntry(catalog, entry)
        if collection then
            collection:addPhotos({ resultPhoto })
            logger:info("Result added to collection: " .. collection:getName())
        else
            logger:warn("Collection not found for " .. entry.stackName .. ", result imported but not added to collection")
        end
    end)
end

return ProcessingQueue
