-- ScriptBridge.lua
-- Handles communication between the Lightroom plugin and external Python scripts.

local LrTasks = import 'LrTasks'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'
local LrDate = import 'LrDate'

local logger = require 'Log'

local ScriptBridge = {}

-- Monotonically increasing counter for unique temp filenames within this session.
-- Combined with os.time() this is sufficient since LrTasks.execute is synchronous
-- from the caller's perspective — no two calls contend for the same name.
local tmpCounter = 0
local function nextTmpPath(suffix)
    tmpCounter = tmpCounter + 1
    local tempDir = LrPathUtils.getStandardFilePath('temp')
    local name = string.format("fsm_%d_%d.%s",
        os.time(), tmpCounter, suffix or "tmp")
    return LrPathUtils.child(tempDir, name)
end

-- Extract the most useful line from stderr for user-facing error messages.
-- Python tracebacks put the actual error on the last non-empty line.
local function lastNonEmptyLine(s)
    if not s or s == "" then return "" end
    local last = ""
    for line in string.gmatch(s, "[^\r\n]+") do
        if line:match("%S") then
            last = line
        end
    end
    return last
end

-- Get the directory containing our Python scripts (inside the .lrplugin bundle)
function ScriptBridge.getScriptsDir()
    return _PLUGIN.path
end

-- Shell-escape a string for use in a command
local function shellEscape(s)
    return "'" .. string.gsub(s, "'", "'\\''") .. "'"
end

-- Read and delete a file, returning its contents (or "" if absent / unreadable).
local function readAndDelete(path)
    local content = ""
    local f = io.open(path, "r")
    if f then
        content = f:read("*a") or ""
        f:close()
    end
    -- Best-effort cleanup; ignore errors (file may not exist).
    pcall(function() LrFileUtils.delete(path) end)
    return content
end

-- Run a Python script and return its JSON output as a string.
-- Blocks until the script completes.
function ScriptBridge.runPythonScript(scriptName, args)
    local prefs = LrPrefs.prefsForPlugin()
    local pythonPath = prefs.python_path or "/usr/bin/python3"
    local scriptsDir = ScriptBridge.getScriptsDir()
    local scriptPath = LrPathUtils.child(scriptsDir, scriptName)

    if not LrFileUtils.exists(scriptPath) then
        return nil, "Script not found: " .. scriptPath
    end

    local tmpOut = nextTmpPath("json")
    local tmpErr = nextTmpPath("stderr")

    -- Build command
    local cmd = shellEscape(pythonPath) .. " " .. shellEscape(scriptPath)
        .. " --json-out " .. shellEscape(tmpOut)
    for _, arg in ipairs(args or {}) do
        cmd = cmd .. " " .. shellEscape(arg)
    end

    -- Scripts write JSON to a temp file so we don't have to parse shell stdout and
    -- can preserve UTF-8 paths without ASCII escaping.
    cmd = cmd .. " 2> " .. shellEscape(tmpErr)

    logger:info("Running: " .. cmd)
    local subprocessStart = LrDate.currentTime()
    local exitCode = LrTasks.execute(cmd)
    local subprocessSeconds = LrDate.currentTime() - subprocessStart

    local stdout = readAndDelete(tmpOut)
    local stderr = readAndDelete(tmpErr)

    local timings = { subprocessSeconds = subprocessSeconds }

    if exitCode ~= 0 then
        -- Surface the most informative line first so LrDialogs.message doesn't
        -- show a wall of Python traceback. Full stderr still goes to the log.
        local headline = lastNonEmptyLine(stderr)
        if headline == "" then
            headline = "(no stderr output)"
        end
        logger:info("Script failed (exit " .. tostring(exitCode) .. "). Full stderr:\n" .. stderr)
        return nil, string.format(
            "Script failed (exit %d): %s", exitCode, headline), timings
    end

    if stdout == "" then
        logger:info("Script returned empty output. Stderr was:\n" .. stderr)
        return nil, "Script returned empty output", timings
    end

    logger:info("Script output length: " .. #stdout)
    return stdout, nil, timings
end

local function detectionArgs(directoryPath, minImages, maxGap, allModes)
    local prefs = LrPrefs.prefsForPlugin()
    local args = {
        directoryPath,
        "--min-images", tostring(minImages or prefs.min_stack_size or 4),
        "--max-gap", tostring(maxGap or prefs.max_gap_seconds or 1.0),
    }
    if prefs.exiftool_path and prefs.exiftool_path ~= "" then
        table.insert(args, "--exiftool-path")
        table.insert(args, prefs.exiftool_path)
    end
    if allModes or (prefs.manual_mode_only == false) then
        table.insert(args, "--all-modes")
    end
    return args
end

local function runAndParsePython(scriptName, args, parseErrorPrefix)
    local stdout, err, timings = ScriptBridge.runPythonScript(scriptName, args)
    timings = timings or {}
    if not stdout then
        return nil, err, timings
    end

    local parseStart = LrDate.currentTime()
    local success, result = pcall(function()
        return ScriptBridge.parseJSON(stdout)
    end)
    timings.parseSeconds = LrDate.currentTime() - parseStart

    if not success then
        return nil, parseErrorPrefix .. tostring(result), timings
    end

    return result, nil, timings
end

-- Run detect_stacks.py and return parsed JSON result.
-- Runs recursively by default — scans all subdirectories.
function ScriptBridge.detectStacks(directoryPath, minImages, maxGap, allModes)
    local args = detectionArgs(directoryPath, minImages, maxGap, allModes)
    return runAndParsePython(
        "detect_stacks.py",
        args,
        "Failed to parse detection output: "
    )
end

-- Run detect_stacks.py for a single directory only.
-- opts (optional table):
--   metadataPath: path to a JSON file pre-populated by a metadata provider
--   validateFocusDistance: when true, run the targeted exiftool focus-distance pass
function ScriptBridge.detectStacksInDirectory(directoryPath, minImages, maxGap, allModes, opts)
    local args = detectionArgs(directoryPath, minImages, maxGap, allModes)
    table.insert(args, "--no-recursive")
    if opts then
        if opts.metadataPath and opts.metadataPath ~= "" then
            table.insert(args, "--metadata-json")
            table.insert(args, opts.metadataPath)
        end
        if opts.validateFocusDistance then
            table.insert(args, "--validate-focus-distance")
        end
    end
    return runAndParsePython(
        "detect_stacks.py",
        args,
        "Failed to parse detection output: "
    )
end

-- List all directories beneath the root that directly contain raw files.
function ScriptBridge.listDirectoriesWithRawFiles(directoryPath, minImages, maxGap, allModes)
    local args = detectionArgs(directoryPath, minImages, maxGap, allModes)
    table.insert(args, "--list-directories")
    return runAndParsePython(
        "detect_stacks.py",
        args,
        "Failed to parse directory listing output: "
    )
end

-- Run process_stack.py for a single stack and return parsed JSON result.
-- heliconPath is optional; if nil, prefs.helicon_path is consulted, and if that
-- is also empty, process_stack.py auto-discovers standard install locations.
function ScriptBridge.processStack(files, outputDir, method, radius, smoothing, heliconPath)
    local prefs = LrPrefs.prefsForPlugin()
    local effectiveHelicon = heliconPath
    if not effectiveHelicon and prefs.helicon_path and prefs.helicon_path ~= "" then
        effectiveHelicon = prefs.helicon_path
    end
    local timeoutSeconds = prefs.helicon_timeout_seconds or 600

    local args = {
        "--output-dir", outputDir,
        "--method", tostring(method or prefs.helicon_method or 1),
        "--radius", tostring(radius or prefs.helicon_radius or 11),
        "--smoothing", tostring(smoothing or prefs.helicon_smoothing or 5),
        "--timeout", tostring(timeoutSeconds),
    }

    if effectiveHelicon then
        table.insert(args, "--helicon-path")
        table.insert(args, effectiveHelicon)
    end

    -- --files must be last because nargs='+' is greedy.
    table.insert(args, "--files")
    for _, f in ipairs(files) do
        table.insert(args, f)
    end

    return runAndParsePython(
        "process_stack.py",
        args,
        "Failed to parse process output: "
    )
end

-- Minimal JSON parser for the structured output we expect.
-- Handles objects, arrays, strings, numbers, booleans, null.
-- Not a full JSON parser but sufficient for our script output.
-- Errors loudly on malformed input with position context.
function ScriptBridge.parseJSON(str)
    if type(str) ~= "string" or #str == 0 then
        error("parseJSON: input must be a non-empty string, got " .. type(str))
    end

    local pos = 1

    -- Return a snippet of the input around the current position for error messages.
    local function contextSnippet()
        local start = math.max(1, pos - 20)
        local finish = math.min(#str, pos + 20)
        return string.format("...%s...", string.sub(str, start, finish))
    end

    local function skipWhitespace()
        while pos <= #str and string.find(str, "^%s", pos) do
            pos = pos + 1
        end
    end

    local parseValue -- forward declaration

    local function parseString()
        -- pos should be at opening quote
        pos = pos + 1 -- skip "
        local result = {}
        while pos <= #str do
            local ch = string.sub(str, pos, pos)
            if ch == '"' then
                pos = pos + 1
                return table.concat(result)
            elseif ch == '\\' then
                pos = pos + 1
                if pos > #str then
                    error("parseJSON: unterminated escape at end of input")
                end
                local esc = string.sub(str, pos, pos)
                if esc == '"' then table.insert(result, '"')
                elseif esc == '\\' then table.insert(result, '\\')
                elseif esc == '/' then table.insert(result, '/')
                elseif esc == 'n' then table.insert(result, '\n')
                elseif esc == 'r' then table.insert(result, '\r')
                elseif esc == 't' then table.insert(result, '\t')
                elseif esc == 'b' then table.insert(result, '\b')
                elseif esc == 'f' then table.insert(result, '\f')
                elseif esc == 'u' then
                    if pos + 4 > #str then
                        error("parseJSON: truncated \\u escape at position " .. pos)
                    end
                    local hex = string.sub(str, pos + 1, pos + 4)
                    local codepoint = tonumber(hex, 16)
                    if not codepoint then
                        error("parseJSON: invalid \\u escape at position " .. pos)
                    end

                    local utf8Char
                    if codepoint <= 0x7F then
                        utf8Char = string.char(codepoint)
                    elseif codepoint <= 0x7FF then
                        utf8Char = string.char(
                            0xC0 + math.floor(codepoint / 0x40),
                            0x80 + (codepoint % 0x40)
                        )
                    else
                        utf8Char = string.char(
                            0xE0 + math.floor(codepoint / 0x1000),
                            0x80 + (math.floor(codepoint / 0x40) % 0x40),
                            0x80 + (codepoint % 0x40)
                        )
                    end
                    pos = pos + 4
                    table.insert(result, utf8Char)
                else
                    error(string.format(
                        "parseJSON: invalid escape '\\%s' at position %d near: %s",
                        esc, pos, contextSnippet()))
                end
                pos = pos + 1
            else
                table.insert(result, ch)
                pos = pos + 1
            end
        end
        error("parseJSON: unterminated string starting near position " .. pos)
    end

    local function parseNumber()
        local start = pos
        if string.sub(str, pos, pos) == '-' then pos = pos + 1 end
        while pos <= #str and string.find(string.sub(str, pos, pos), "[%d%.eE%+%-]") do
            pos = pos + 1
        end
        local numStr = string.sub(str, start, pos - 1)
        local num = tonumber(numStr)
        if num == nil then
            error(string.format(
                "parseJSON: invalid number '%s' at position %d", numStr, start))
        end
        return num
    end

    local function expectLiteral(expected, value)
        local actual = string.sub(str, pos, pos + #expected - 1)
        if actual ~= expected then
            error(string.format(
                "parseJSON: expected '%s' at position %d, got '%s' near: %s",
                expected, pos, actual, contextSnippet()))
        end
        pos = pos + #expected
        return value
    end

    local function parseArray()
        pos = pos + 1 -- skip [
        local arr = {}
        skipWhitespace()
        if string.sub(str, pos, pos) == ']' then
            pos = pos + 1
            return arr
        end
        while true do
            skipWhitespace()
            table.insert(arr, parseValue())
            skipWhitespace()
            local ch = string.sub(str, pos, pos)
            if ch == ']' then
                pos = pos + 1
                return arr
            elseif ch == ',' then
                pos = pos + 1
            else
                error(string.format(
                    "parseJSON: expected ',' or ']' in array at position %d near: %s",
                    pos, contextSnippet()))
            end
        end
    end

    local function parseObject()
        pos = pos + 1 -- skip {
        local obj = {}
        skipWhitespace()
        if string.sub(str, pos, pos) == '}' then
            pos = pos + 1
            return obj
        end
        while true do
            skipWhitespace()
            if string.sub(str, pos, pos) ~= '"' then
                error(string.format(
                    "parseJSON: expected string key at position %d near: %s",
                    pos, contextSnippet()))
            end
            local key = parseString()
            skipWhitespace()
            if string.sub(str, pos, pos) ~= ':' then
                error(string.format(
                    "parseJSON: expected ':' after key '%s' at position %d",
                    key, pos))
            end
            pos = pos + 1
            skipWhitespace()
            obj[key] = parseValue()
            skipWhitespace()
            local ch = string.sub(str, pos, pos)
            if ch == '}' then
                pos = pos + 1
                return obj
            elseif ch == ',' then
                pos = pos + 1
            else
                error(string.format(
                    "parseJSON: expected ',' or '}' in object at position %d near: %s",
                    pos, contextSnippet()))
            end
        end
    end

    parseValue = function()
        skipWhitespace()
        if pos > #str then
            error("parseJSON: unexpected end of input")
        end
        local ch = string.sub(str, pos, pos)
        if ch == '"' then return parseString()
        elseif ch == '{' then return parseObject()
        elseif ch == '[' then return parseArray()
        elseif ch == 't' then return expectLiteral("true", true)
        elseif ch == 'f' then return expectLiteral("false", false)
        elseif ch == 'n' then return expectLiteral("null", nil)
        elseif ch == '-' or (ch >= '0' and ch <= '9') then
            return parseNumber()
        else
            error(string.format(
                "parseJSON: unexpected character '%s' at position %d near: %s",
                ch, pos, contextSnippet()))
        end
    end

    skipWhitespace()
    local result = parseValue()
    skipWhitespace()
    if pos <= #str then
        error(string.format(
            "parseJSON: trailing content at position %d near: %s",
            pos, contextSnippet()))
    end
    return result
end

return ScriptBridge
