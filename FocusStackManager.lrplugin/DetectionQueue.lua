local LrDialogs = import 'LrDialogs'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'

local DetectionService = require 'DetectionService'
local logger = require 'Log'

local DetectionQueue = {}
local workerRunning = false

local function addStats(total, stats)
    if not stats then
        return
    end
    total.dirsScanned = (total.dirsScanned or 0) + (stats.dirsScanned or 0)
    total.completedDirs = (total.completedDirs or 0) + (stats.completedDirs or 0)
    total.totalRaw = (total.totalRaw or 0) + (stats.totalRaw or 0)
    total.totalStacks = (total.totalStacks or 0) + (stats.totalStacks or 0)
    total.totalRejected = (total.totalRejected or 0) + (stats.totalRejected or 0)
    total.totalNew = (total.totalNew or 0) + (stats.totalNew or 0)
    total.totalExisting = (total.totalExisting or 0) + (stats.totalExisting or 0)
    total.totalAdoptedResults = (total.totalAdoptedResults or 0) + (stats.totalAdoptedResults or 0)
    total.totalSkippedEmpty = (total.totalSkippedEmpty or 0) + (stats.totalSkippedEmpty or 0)
    total.totalSkippedIncomplete = (total.totalSkippedIncomplete or 0) + (stats.totalSkippedIncomplete or 0)
end

local function modifyQueue(modifier)
    local prefs = LrPrefs.prefsForPlugin()
    local queue = prefs.detection_queue or {}
    local result = modifier(queue)
    prefs.detection_queue = queue
    return queue, result
end

function DetectionQueue.getQueue()
    local prefs = LrPrefs.prefsForPlugin()
    return prefs.detection_queue or {}
end

function DetectionQueue.count()
    return #DetectionQueue.getQueue()
end

function DetectionQueue.clear()
    modifyQueue(function(queue)
        for i = #queue, 1, -1 do
            table.remove(queue, i)
        end
    end)
    logger:info("Detection queue cleared")
end

function DetectionQueue.isWorkerRunning()
    return workerRunning
end

function DetectionQueue.enqueue(entries)
    modifyQueue(function(queue)
        for _, entry in ipairs(entries) do
            table.insert(queue, entry)
            logger:info("Enqueued detection root: " .. tostring(entry.scanRoot))
        end
    end)
    DetectionQueue.ensureWorkerRunning()
end

function DetectionQueue.ensureWorkerRunning()
    if workerRunning then
        logger:info("Detection worker already running, new items will be appended")
        return
    end

    workerRunning = true
    logger:info("Starting detection worker")

    LrTasks.startAsyncTask(function()
        local batchStats = {}
        local ranAny = false
        while true do
            local entry
            local queuedCount = 0
            modifyQueue(function(queue)
                queuedCount = #queue
                if #queue == 0 then return end
                entry = table.remove(queue, 1)
            end)

            if not entry then
                break
            end

            ranAny = true
            local queueTotal = queuedCount
            local status, stats = DetectionService.run(entry.scanRoot, 1, queueTotal, {
                suppressDialogs = true,
            })
            addStats(batchStats, stats)
            if status == "cancelled" then
                DetectionQueue.clear()
                LrDialogs.message(
                    "Focus Stack Detection Cancelled",
                    table.concat(DetectionService.buildSummaryLines(batchStats), "\n"),
                    "warning"
                )
                break
            end
        end

        workerRunning = false
        if ranAny then
            LrDialogs.message(
                "Focus Stack Detection Complete",
                table.concat(DetectionService.buildSummaryLines(batchStats), "\n"),
                "info"
            )
        end
        logger:info("Detection worker stopped")
    end)
end

function DetectionQueue.enqueueScanRoot(scanRoot)
    DetectionQueue.enqueue({
        { scanRoot = scanRoot }
    })
end

return DetectionQueue
