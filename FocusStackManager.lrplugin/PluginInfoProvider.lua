local LrView = import 'LrView'
local LrPrefs = import 'LrPrefs'
local LrDialogs = import 'LrDialogs'

local function sectionsForTopOfDialog(f, properties)
    local prefs = LrPrefs.prefsForPlugin()
    local bind = LrView.bind

    -- Set defaults if not yet configured
    if not prefs.python_path then prefs.python_path = "/usr/bin/python3" end
    if not prefs.exiftool_path then prefs.exiftool_path = "" end
    if not prefs.helicon_path then prefs.helicon_path = "" end
    if not prefs.helicon_timeout_seconds then prefs.helicon_timeout_seconds = 600 end
    if not prefs.helicon_method then prefs.helicon_method = 1 end
    if not prefs.helicon_radius then prefs.helicon_radius = 11 end
    if not prefs.helicon_smoothing then prefs.helicon_smoothing = 5 end
    if not prefs.min_stack_size then prefs.min_stack_size = 4 end
    if not prefs.max_gap_seconds then prefs.max_gap_seconds = 1.5 end
    if prefs.manual_mode_only == nil then prefs.manual_mode_only = true end
    if not prefs.collection_set_prefix then prefs.collection_set_prefix = "Focus Stacks" end
    if not prefs.result_color_label then prefs.result_color_label = "green" end
    if not prefs.source_color_label then prefs.source_color_label = "red" end
    if prefs.result_keyword == nil then prefs.result_keyword = "" end
    if prefs.source_keyword == nil then prefs.source_keyword = "" end
    if prefs.metadata_provider == nil then prefs.metadata_provider = "lr_catalog" end
    if prefs.validate_focus_distance == nil then prefs.validate_focus_distance = false end

    return {
        {
            title = "Focus Stack Manager Settings",
            synopsis = "Configure detection and processing parameters",

            f:row {
                f:static_text {
                    title = "Python 3 Path:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'python_path', object = prefs },
                    width_in_chars = 40,
                    tooltip = "Path to python3 executable",
                },
            },

            f:row {
                f:static_text {
                    title = "Helicon Focus Path:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'helicon_path', object = prefs },
                    width_in_chars = 40,
                    tooltip = "Optional: full path to the Helicon Focus executable. " ..
                        "Leave blank to auto-discover in /Applications.",
                },
            },

            f:row {
                f:static_text {
                    title = "ExifTool Path:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'exiftool_path', object = prefs },
                    width_in_chars = 40,
                    tooltip = "Optional: full path to exiftool. " ..
                        "Leave blank to auto-discover via PATH or common Homebrew locations.",
                },
            },

            f:row {
                f:static_text {
                    title = "Helicon Timeout:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'helicon_timeout_seconds', object = prefs },
                    width_in_chars = 8,
                    precision = 0,
                    min = 30,
                    max = 7200,
                    increment = 60,
                    tooltip = "Per-stack Helicon Focus timeout in seconds (30 – 7200).",
                },
                f:static_text { title = "seconds" },
            },

            f:separator { fill_horizontal = 1 },

            f:static_text {
                title = "Detection Parameters",
                font = "<system/bold>",
            },

            f:row {
                f:static_text {
                    title = "Min Stack Size:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'min_stack_size', object = prefs },
                    width_in_chars = 6,
                    precision = 0,
                    min = 2,
                    max = 500,
                    increment = 1,
                    tooltip = "Minimum number of images to consider a focus stack",
                },
                f:static_text { title = "frames" },
            },

            f:row {
                f:static_text {
                    title = "Max Gap Between Frames:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'max_gap_seconds', object = prefs },
                    width_in_chars = 6,
                    precision = 2,
                    min = 0.1,
                    max = 60,
                    increment = 0.1,
                    tooltip = "Maximum time gap between consecutive frames in seconds",
                },
                f:static_text { title = "seconds" },
            },

            f:row {
                f:static_text {
                    title = "Manual Exposure Only:",
                    alignment = 'right',
                    width = 160,
                },
                f:checkbox {
                    value = bind { key = 'manual_mode_only', object = prefs },
                    title = "Only detect stacks shot in manual exposure mode",
                },
            },

            f:row {
                f:static_text {
                    title = "Read Metadata From:",
                    alignment = 'right',
                    width = 160,
                },
                f:popup_menu {
                    value = bind { key = 'metadata_provider', object = prefs },
                    items = {
                        { title = "Lightroom catalog (fast)", value = "lr_catalog" },
                        { title = "ExifTool (slow, requires file access)", value = "exiftool" },
                    },
                    width = 320,
                    tooltip = "Where to read camera metadata from when detecting stacks. " ..
                        "The Lightroom catalog is much faster because the data is already in memory. " ..
                        "ExifTool re-reads each source file from disk; pick this if you suspect the " ..
                        "catalog metadata is stale (e.g. EXIF was edited externally after import).",
                },
            },

            f:row {
                f:static_text {
                    title = "Validate Focus Distance:",
                    alignment = 'right',
                    width = 160,
                },
                f:checkbox {
                    value = bind { key = 'validate_focus_distance', object = prefs },
                    title = "Validate focus distance (requires exiftool, slower)",
                    tooltip = "When enabled, exiftool reads ApproximateFocusDistance for each candidate stack " ..
                        "and rejects sequences where the focus distance never changes — these are typically " ..
                        "wind-timing or burst shots, not real focus stacks. Adds a small per-stack disk read.",
                },
            },

            f:separator { fill_horizontal = 1 },

            f:static_text {
                title = "Helicon Focus Defaults",
                font = "<system/bold>",
            },

            f:row {
                f:static_text {
                    title = "Method:",
                    alignment = 'right',
                    width = 160,
                },
                f:popup_menu {
                    value = bind { key = 'helicon_method', object = prefs },
                    items = {
                        { title = "A (Weighted Average)", value = 0 },
                        { title = "B (Depth Map)", value = 1 },
                        { title = "C (Pyramid)", value = 2 },
                    },
                    width = 200,
                },
            },

            f:row {
                f:static_text {
                    title = "Radius:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'helicon_radius', object = prefs },
                    width_in_chars = 6,
                    precision = 0,
                    min = 1,
                    max = 256,
                    increment = 1,
                },
            },

            f:row {
                f:static_text {
                    title = "Smoothing:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'helicon_smoothing', object = prefs },
                    width_in_chars = 6,
                    precision = 0,
                    min = 0,
                    max = 20,
                    increment = 1,
                },
            },

            f:separator { fill_horizontal = 1 },

            f:static_text {
                title = "Labeling Defaults",
                font = "<system/bold>",
            },

            f:row {
                f:static_text {
                    title = "Result Color Label:",
                    alignment = 'right',
                    width = 160,
                },
                f:popup_menu {
                    value = bind { key = 'result_color_label', object = prefs },
                    items = {
                        { title = "None", value = "none" },
                        { title = "Red", value = "red" },
                        { title = "Yellow", value = "yellow" },
                        { title = "Green", value = "green" },
                        { title = "Blue", value = "blue" },
                        { title = "Purple", value = "purple" },
                    },
                    width = 200,
                    tooltip = "Color label applied to merged result photos",
                },
            },

            f:row {
                f:static_text {
                    title = "Source Color Label:",
                    alignment = 'right',
                    width = 160,
                },
                f:popup_menu {
                    value = bind { key = 'source_color_label', object = prefs },
                    items = {
                        { title = "None", value = "none" },
                        { title = "Red", value = "red" },
                        { title = "Yellow", value = "yellow" },
                        { title = "Green", value = "green" },
                        { title = "Blue", value = "blue" },
                        { title = "Purple", value = "purple" },
                    },
                    width = 200,
                    tooltip = "Color label applied to source photos in detected stacks",
                },
            },

            f:row {
                f:static_text {
                    title = "Result Keyword:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'result_keyword', object = prefs },
                    width_in_chars = 30,
                    tooltip = "Optional user-facing keyword applied to merged result photos. Leave blank to disable.",
                },
            },

            f:row {
                f:static_text {
                    title = "Source Keyword:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'source_keyword', object = prefs },
                    width_in_chars = 30,
                    tooltip = "Optional user-facing keyword applied to source photos. Leave blank to disable.",
                },
            },

            f:separator { fill_horizontal = 1 },

            f:static_text {
                title = "Collection Organization",
                font = "<system/bold>",
            },

            f:row {
                f:static_text {
                    title = "Root Collection Set:",
                    alignment = 'right',
                    width = 160,
                },
                f:edit_field {
                    value = bind { key = 'collection_set_prefix', object = prefs },
                    width_in_chars = 30,
                    tooltip = "Name of the top-level collection set for all focus stacks",
                },
            },

            f:static_text {
                title = "Collection hierarchy is derived automatically from the catalog root folder path.",
                font = "<system/small>",
            },
        },
    }
end

return {
    sectionsForTopOfDialog = sectionsForTopOfDialog,
}
