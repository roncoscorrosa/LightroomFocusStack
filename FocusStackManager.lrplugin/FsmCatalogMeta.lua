-- FsmCatalogMeta.lua
-- Pulls per-file metadata directly from the Lightroom catalog.
-- All fields come from photo:getRawMetadata(); no filesystem or exiftool I/O.
-- Files that are not imported into the catalog are omitted from the result.

local LrDate = import 'LrDate'

local FsmCatalogMeta = {}

local function formatDateTime(rawSeconds)
    -- LR returns seconds since the Cocoa epoch (2001-01-01 UTC) as a float
    -- with millisecond precision. Format as "YYYY:MM:DD HH:MM:SS.SSS" so the
    -- Python parser picks up sub-seconds without a separate field.
    if rawSeconds == nil then
        return nil
    end
    local intTime = math.floor(rawSeconds)
    local sub = rawSeconds - intTime
    local dateStr = LrDate.timeToUserFormat(intTime, "%Y:%m:%d %H:%M:%S")
    if sub > 0.0005 then
        local ms = math.floor(sub * 1000 + 0.5)
        if ms >= 1000 then ms = 999 end
        dateStr = string.format("%s.%03d", dateStr, ms)
    end
    return dateStr
end

local function formatShutterSpeed(seconds)
    -- exiftool returns "1/250" for sub-second exposures and a decimal for
    -- 1 second or longer. Mirror that so the camera-settings group key is
    -- stable across providers (Python parses both forms via parse_fraction).
    if seconds == nil then
        return nil
    end
    if seconds >= 1 then
        return string.format("%g", seconds)
    end
    if seconds <= 0 then
        return "0"
    end
    local denom = math.floor(1 / seconds + 0.5)
    if denom <= 0 then denom = 1 end
    return "1/" .. tostring(denom)
end

local function buildEntry(photo, filePath)
    local entry = { SourceFile = filePath }

    local dt = photo:getRawMetadata("dateTimeOriginal")
    local formatted = formatDateTime(dt)
    if formatted then
        entry.DateTimeOriginal = formatted
        entry.CreateDate = formatted
    end

    local fl = photo:getRawMetadata("focalLength")
    if fl then entry.FocalLength = fl end

    local iso = photo:getRawMetadata("isoSpeedRating")
    if iso then entry.ISO = iso end

    local ap = photo:getRawMetadata("aperture")
    if ap then entry.FNumber = ap end

    local ss = formatShutterSpeed(photo:getRawMetadata("shutterSpeed"))
    if ss then entry.ExposureTime = ss end

    -- exposureProgram is only exposed via getFormattedMetadata, not getRawMetadata.
    -- Returns strings like "Manual", "Aperture priority", "Shutter priority".
    -- Python does case-insensitive startswith("manual"), so localized variants
    -- of "Manual" still match as long as they share the prefix.
    local em = photo:getFormattedMetadata("exposureProgram")
    if em and em ~= "" then
        entry.ExposureMode = tostring(em)
    end

    return entry
end

function FsmCatalogMeta.getMetadataForFiles(catalog, filePaths)
    local result = {}
    if not catalog or not filePaths then
        return result
    end
    for _, filePath in ipairs(filePaths) do
        local photo = catalog:findPhotoByPath(filePath, false)
        if photo then
            result[filePath] = buildEntry(photo, filePath)
        end
    end
    return result
end

return FsmCatalogMeta
