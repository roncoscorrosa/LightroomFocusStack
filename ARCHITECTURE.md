# Architecture & Technical Details

This plugin is intentionally opinionated. It is designed around a Lightroom-first workflow that uses collections and catalog metadata for organization, rather than moving files into staging directories on disk.

## Design Constraints

Several parts of the implementation are driven by external limitations rather than preference:

1. **Detection is heuristic because the metadata is incomplete.**
   Camera files do not provide a reliable, portable "focus stack sequence id". As a result, the plugin has to infer focus stack sequences from timing, exposure settings, and focus distance progression rather than reading a single canonical field.

2. **Collapse uses UI automation because Lightroom does not expose a supported stack-creation API.**
   Lightroom Classic can create photo stacks interactively, but the SDK does not provide a supported way to create Lightroom photo stacks directly. That is why `CollapseStacks.lua` has to drive `Cmd+G` through macOS automation.

3. **Organization stays inside Lightroom on purpose.**
   The plugin is designed so that review, filtering, and organization happen in Lightroom collections and metadata. It does not move source files into staging directories or require a filesystem-based workflow.

4. **The collection hierarchy mirrors the full path for predictability.**
   A shorter hierarchy would be more compact, but full path mirroring is deterministic and avoids collisions when different parts of the library reuse similar folder names or dates.

## How Detection Works

The detection algorithm identifies focus stacks through six steps applied in sequence:

1. **Exposure mode filter** — only considers Manual mode by default (configurable). This avoids false positives from aperture-priority burst shooting (e.g., birds/wildlife).

2. **Camera settings grouping** — groups images with identical focal length, ISO, aperture, and shutter speed. A focus stack is always shot with the same settings.

3. **Temporal proximity** — within each settings group, finds sequences where the gap between end of one exposure and start of the next is within an allowed threshold. The threshold scales with exposure time to handle long exposures — cameras need more buffer time to write a 10-second exposure (~1.07s gap) than a 1/250s exposure (~0.3s gap). Formula: `effective_gap = max_gap + (exposure_time * gap_exposure_scale)`.

4. **Minimum sequence length** — requires at least 4 frames (configurable). Sequences shorter than this are not considered stacks.

5. **Adjacent stack merging** — after initial detection, checks if consecutive stacks (same settings, close in time) have continuous focus distance progression. The merge window is intentionally tight: stack B must begin within a small multiple of stack A's last exposure time, and the focus distance gap between stacks must be small relative to the total combined range. This catches genuine extended sweeps while avoiding false positives from separate stacks shot later in the sequence.

6. **Focus distance validation** — uses `exiftool` to read `ApproximateFocusDistance` from XMP metadata. If all frames in a candidate sequence have the same focus distance, it's rejected as a false positive (e.g., wind-timing shots where you're re-shooting the same composition waiting for wind to stop). If focus distance varies across frames, it's confirmed as a real focus stack. If focus distance data is unavailable, the candidate is kept (fail-open).

All five heuristics run in a single `exiftool -json` batch call per directory — no per-file overhead.

Detection also makes a best-effort attempt to correlate pre-existing merged result files back to the detected source stack. Exact filename matches are preferred, and a conservative heuristic fallback can match renamed DNG/TIFF/PSD results by exposure fingerprint and capture timestamp. This only works when the result file lives in the same physical folder as the source files being scanned.

## Plugin Architecture

The plugin is split into distinct responsibilities:

```
Init.lua              -- runs on plugin load; platform guard + queue cleanup
Log.lua               -- singleton logger (LrLogger, logfile-enabled)
DetectStacks.lua      -- scans folder recursively, calls detect_stacks.py, creates collections
CreateManualStack.lua -- creates a focus stack collection from manually selected photos
ProcessStacks.lua     -- reads collections, builds queue entries, shows params dialog
ProcessingQueue.lua   -- background worker, pulls from queue, calls process_stack.py
CollectionManager.lua -- creates/queries nested collection hierarchy
ScriptBridge.lua      -- shells out to Python scripts, captures JSON output
CollapseStacks.lua    -- UI automation for Cmd+G stacking (macOS only)
```

### Python Scripts

The Lua plugin delegates heavy work to two Python scripts:

**`detect_stacks.py`** — recursively scans directories for raw files, reads EXIF via `exiftool -json` (one call per directory), runs the detection algorithm, and writes JSON grouped by directory. No Python dependencies beyond the standard library.

**`process_stack.py`** — processes one focus stack through Helicon Focus. Creates a temp directory of symlinks to the source files (so originals don't move), runs Helicon Focus, and writes JSON with the DNG result path. The plugin standardizes merged output on DNG.

### State Management

There is no external state file. State is encoded in:

- the Lightroom collection hierarchy, which groups focus stack members for review
- private plugin photo metadata:
  - `focus_stack_role` (`source` or `result`)
  - `focus_stack_id` (stable group identifier shared by related source/result photos)
  - `focus_stack_stack_name` (canonical stack name)
  - `focus_stack_result_params` (encoded result parameter tuple)

The result filename also encodes processing parameters: `-_ON_1234-_ON_1248_15f_mB_s5_r11_stacked.dng` means 15 frames, Method B, Smoothing 5, Radius 11.

### Processing Queue

The queue is stored in Lightroom plugin preferences as a serializable table. A single background worker task processes items one at a time (Helicon Focus is single-instance). Key design:

- `ensureWorkerRunning()` is idempotent — calling it when the worker is active is a no-op
- New items appended via `enqueue()` are picked up by the running worker
- Queue survives across dialog closings within the current Lightroom session
- On Lightroom relaunch, `Init.lua` clears any stale persisted queue state
- Worker shows progress via `LrProgressScope` (Lightroom's progress bar)
- Per-stack failures (name + error) are collected and shown in an error-only completion dialog

### Collection Hierarchy

Collections mirror the filesystem path starting at the matching Lightroom catalog root folder:

```
/Volumes/Ron/Photos/Death Valley/Dunes/2026/2026-01-07
→ Focus Stacks > Photos > Death Valley > Dunes > 2026 > 2026-01-07
```

Intermediate collection sets are created on demand. This is intentionally less compact than a shortened hierarchy, but it is deterministic and avoids collisions when different parts of the library reuse similar folder names or dates. These collections are working organization inside Lightroom and can be deleted without affecting files on disk.

### Collapse Into Lightroom Photo Stacks

The Lightroom SDK cannot create Lightroom photo stacks programmatically. `CollapseStacks.lua` works around this with UI automation:

1. Read the currently visible photos in the selected folder or collection context
2. Group related photos by `focus_stack_id`
3. Choose the newest result photo, if one exists
4. Select the photos in the right order so the desired image ends up on top
5. Send `Cmd+G` via macOS System Events (`osascript`)

The same action can also refresh an existing Lightroom photo stack after a newer merged result has been created. This requires the Lightroom window to be in the foreground and macOS Accessibility permissions.

## Result Filename Convention

```
-_ON_1234-_ON_1248_15f_mB_s5_r11_stacked.dng
│ │          │      │   │  │  │    │
│ │          │      │   │  │  │    └── suffix identifying this as a merge result
│ │          │      │   │  │  └─────── Helicon radius parameter
│ │          │      │   │  └────────── Helicon smoothing parameter
│ │          │      │   └───────────── Helicon method letter (A, B, or C)
│ │          │      └────────────────── number of source frames merged
│ │          └───────────────────────── last source file stem
│ └──────────────────────────────────── first source file stem
└────────────────────────────────────── '-' prefix: sorts before source files (ASCII 45 < 95)
```

Changing any parameter — method, radius, smoothing, or the number of source files — produces a different filename. This means:
- Multiple results from different runs coexist on disk
- The plugin can distinguish results from different runs at a glance

## File Structure

The plugin is fully self-contained in the `.lrplugin` folder. Python scripts are bundled inside so there's nothing extra to install or configure beyond exiftool.

```
LightroomFocusStack/
  FocusStackManager.lrplugin/      <-- this is the installable plugin
    Info.lua                        -- plugin registration (4 menu items)
    PluginInfoProvider.lua          -- settings UI
    detect_stacks.py                -- EXIF-based focus stack detection (JSON output)
    process_stack.py                -- single-stack Helicon Focus processing
    DetectStacks.lua                -- scan folder recursively, detect stacks, create collections
    CreateManualStack.lua           -- create stack collection from selected photos
    ProcessStacks.lua               -- process collections through Helicon Focus
    ProcessingQueue.lua             -- background worker, queue management
    CollapseStacks.lua              -- group into LR stacks via Cmd+G automation
    CollectionManager.lua           -- nested collection hierarchy management
    ScriptBridge.lua                -- Python script invocation + JSON parsing
  tests/
    test_detect_stacks.py           -- detection logic tests
    test_process_stack.py           -- processing logic tests
```

## Testing

The Python test suite covers the detection and processing logic:

```bash
cd LightroomFocusStack
python3 -m unittest discover -s tests -v
```

Tests mock `exiftool` and Helicon Focus — no external tools are called during testing.
