-- Init.lua
-- Runs once when the plugin loads (see LrInitPlugin in Info.lua).
--
-- Responsibilities:
--   1. Platform guard — the plugin is macOS-only (AppleScript for stacking,
--      POSIX shell for Python invocation). Warn loudly on Windows.
--   2. Clear any stale persisted queue state from a previous Lightroom session.
--      The processing queue is treated as session-scoped; if Lightroom exits
--      mid-run, the user should requeue intentionally rather than resuming
--      partial state on next launch.

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'

local logger = require 'Log'

logger:info("=== PLUGIN LOAD ===")

-- Platform guard. WIN_ENV is a Lightroom Lua global.
if WIN_ENV then
    LrTasks.startAsyncTask(function()
        LrDialogs.message(
            "Focus Stack Manager",
            "This plugin is macOS-only. It depends on AppleScript (for " ..
            "stack grouping) and POSIX shell conventions that Windows " ..
            "Lightroom doesn't provide.\n\nThe plugin will load but most " ..
            "actions will fail.",
            "warning"
        )
    end)
    logger:warn("Plugin loaded on non-macOS platform — unsupported")
    return
end

-- Clear stale queue state asynchronously. We can't require ProcessingQueue at
-- the top level here without risking circular loads during plugin init, so
-- defer into a task.
LrTasks.startAsyncTask(function()
    local ProcessingQueue = require 'ProcessingQueue'
    local pending = ProcessingQueue.count()
    if pending > 0 then
        logger:info("Clearing stale persisted processing queue with " .. pending ..
            " items from previous session")
        ProcessingQueue.clear()
    end

    local DetectionQueue = require 'DetectionQueue'
    local detectPending = DetectionQueue.count()
    if detectPending > 0 then
        logger:info("Clearing stale persisted detection queue with " .. detectPending ..
            " items from previous session")
        DetectionQueue.clear()
    end
end)
