-- Timing.lua
-- Lightweight named-span timing for performance investigation.
-- Uses LrDate.currentTime() (subsecond resolution).

local LrDate = import 'LrDate'

local Timing = {}
Timing.__index = Timing

function Timing.new()
    local self = setmetatable({}, Timing)
    self.totals = {}
    self.counts = {}
    self.starts = {}
    return self
end

function Timing:now()
    return LrDate.currentTime()
end

function Timing:start(name)
    self.starts[name] = self:now()
end

function Timing:stop(name)
    local startTime = self.starts[name]
    if startTime == nil then
        return 0
    end
    self.starts[name] = nil
    local elapsed = self:now() - startTime
    self:add(name, elapsed)
    return elapsed
end

function Timing:add(name, seconds)
    self.totals[name] = (self.totals[name] or 0) + (seconds or 0)
    self.counts[name] = (self.counts[name] or 0) + 1
end

-- Time a function call and accumulate the result.
-- Returns the function's first return value (sufficient for current call sites).
function Timing:measure(name, fn, ...)
    local startTime = self:now()
    local result = fn(...)
    self:add(name, self:now() - startTime)
    return result
end

-- Merge a Python `_timing` dict (as returned by StageTimer.to_dict).
-- Keys are stage names; values are { seconds=number, count=number }.
function Timing:mergePythonTiming(timingDict, prefix)
    if type(timingDict) ~= "table" then
        return
    end
    prefix = prefix or ""
    for name, entry in pairs(timingDict) do
        if type(entry) == "table" then
            local key = prefix .. name
            self.totals[key] = (self.totals[key] or 0) + (entry.seconds or 0)
            self.counts[key] = (self.counts[key] or 0) + (entry.count or 0)
        end
    end
end

local function sortedKeys(t)
    local keys = {}
    for k in pairs(t) do
        table.insert(keys, k)
    end
    table.sort(keys, function(a, b)
        return (t[a] or 0) > (t[b] or 0)
    end)
    return keys
end

function Timing:formatSummary(title)
    local lines = {}
    table.insert(lines, title or "Timing summary (sorted by total seconds desc):")
    local keys = sortedKeys(self.totals)
    for _, name in ipairs(keys) do
        local seconds = self.totals[name] or 0
        local count = self.counts[name] or 0
        local average = count > 0 and (seconds / count) or 0
        table.insert(lines, string.format(
            "  %-32s  total=%8.3fs  count=%6d  avg=%8.4fs",
            name, seconds, count, average
        ))
    end
    return table.concat(lines, "\n")
end

return Timing
