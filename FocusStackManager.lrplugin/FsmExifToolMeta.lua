-- FsmExifToolMeta.lua
-- Shells out to exiftool and parses the JSON output. Slower than the catalog
-- source (each file is read from disk) but works on files that have not been
-- imported into Lightroom yet.

local LrTasks = import 'LrTasks'
local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'

local ScriptBridge = require 'ScriptBridge'
local logger = require 'Log'

local FsmExifToolMeta = {}

-- Tags read by the detection algorithm. Mirrors EXIFTOOL_DEFAULT_TAGS in
-- detect_stacks.py - keep in sync if either side adds a tag.
local DEFAULT_TAGS = {
    "-DateTimeOriginal",
    "-CreateDate",
    "-SubSecTimeOriginal",
    "-FocalLength",
    "-ISO",
    "-FNumber",
    "-ExposureTime",
    "-ExposureMode",
    "-ApproximateFocusDistance",
    "-PreviewApplicationName",
    "-Software",
    "-CreatorTool",
    "-PhotometricInterpretation",
    "-SamplesPerPixel",
}

local COMMON_EXIFTOOL_PATHS = {
    "/opt/homebrew/bin/exiftool",
    "/usr/local/bin/exiftool",
}

local function shellEscape(s)
    return "'" .. string.gsub(s, "'", "'\\''") .. "'"
end

local function resolveExiftool(explicitPath)
    if explicitPath and explicitPath ~= "" then
        if LrFileUtils.exists(explicitPath) then
            return explicitPath
        end
    end
    for _, candidate in ipairs(COMMON_EXIFTOOL_PATHS) do
        if LrFileUtils.exists(candidate) then
            return candidate
        end
    end
    return nil
end

local function readAndDelete(path)
    local content = ""
    local f = io.open(path, "r")
    if f then
        content = f:read("*a") or ""
        f:close()
    end
    pcall(function() LrFileUtils.delete(path) end)
    return content
end

local function nextTmpPath(suffix)
    local tempDir = LrPathUtils.getStandardFilePath('temp')
    local name = string.format("fsm_meta_%d_%d.%s",
        os.time(), math.random(0, 0xfffff), suffix or "tmp")
    return LrPathUtils.child(tempDir, name)
end

function FsmExifToolMeta.getMetadataForFiles(exiftoolPath, filePaths)
    if not filePaths or #filePaths == 0 then
        return {}
    end

    local exiftool = resolveExiftool(exiftoolPath)
    if not exiftool then
        error("ExifTool not found. Install with: brew install exiftool, or set ExifTool Path in plugin settings.")
    end

    local stdoutPath = nextTmpPath("json")
    local stderrPath = nextTmpPath("stderr")

    local cmd = shellEscape(exiftool) .. " -fast2 -json"
    for _, tag in ipairs(DEFAULT_TAGS) do
        cmd = cmd .. " " .. tag
    end
    for _, fp in ipairs(filePaths) do
        cmd = cmd .. " " .. shellEscape(fp)
    end
    cmd = cmd .. " > " .. shellEscape(stdoutPath) .. " 2> " .. shellEscape(stderrPath)

    local exitCode = LrTasks.execute(cmd)
    local stdout = readAndDelete(stdoutPath)
    local stderr = readAndDelete(stderrPath)

    if exitCode ~= 0 then
        logger:info("exiftool metadata extract failed (exit " .. tostring(exitCode) ..
            "). stderr:\n" .. stderr)
        error(string.format("exiftool failed (exit %d)", exitCode))
    end

    if stdout == "" then
        return {}
    end

    local entries = ScriptBridge.parseJSON(stdout)
    local result = {}
    if type(entries) == "table" then
        for _, entry in ipairs(entries) do
            if type(entry) == "table" and entry.SourceFile then
                result[entry.SourceFile] = entry
            end
        end
    end
    return result
end

return FsmExifToolMeta
