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

The detection algorithm identifies focus stacks through these steps:

1. **Exposure mode filter** — only considers Manual mode by default (configurable). This avoids false positives from aperture-priority burst shooting (e.g., birds/wildlife).

2. **Camera settings grouping** — groups images with identical focal length, ISO, aperture, and shutter speed. A focus stack is always shot with the same settings.

3. **Temporal proximity** — within each settings group, finds sequences where the gap between end of one exposure and start of the next is within an allowed threshold. The threshold scales with exposure time to handle long exposures — cameras need more buffer time to write a 10-second exposure (~1.07s gap) than a 1/250s exposure (~0.3s gap). Formula: `effective_gap = max_gap + (exposure_time * gap_exposure_scale)`.

4. **FocusBracketing-based splitting** — when the camera recorded a `FocusBracketing` tag (Canon writes this in CR3 MakerNotes), a temporal sequence is subdivided wherever consecutive frames have different `FocusBracketing` states, or wherever a run of `FocusBracketing=On` frames reaches `FocusBracketingImageCount`. This cleanly separates back-to-back focus brackets captured within the same temporal grouping. When no frame carries the tag (e.g., DNGs where Adobe's CR3→DNG conversion stripped MakerNotes), this step is a no-op.

5. **Minimum sequence length** — requires at least 4 frames (configurable). Sequences shorter than this are not considered stacks.

6. **Adjacent stack merging** — after initial detection, checks if consecutive stacks (same settings, close in time) have continuous focus distance progression. The merge window is intentionally tight: stack B must begin within a small multiple of stack A's last exposure time, and the focus distance gap between stacks must be small relative to the total combined range. This catches genuine extended sweeps while avoiding false positives from separate stacks shot later in the sequence.

7. **Focus distance validation** — uses focus-distance metadata to reject sequences whose focus didn't actually move (wind/wave/burst false positives). Values are read in preference order: `ApproximateFocusDistance` (XMP-aux, Nikon/Sony) → `FocusDistanceLower` (Canon MakerNotes near-DOF limit) → `FocusDistanceUpper` (Canon far-DOF limit). The decision depends on what other signals are available:

   - **`FocusBracketing=On` for all frames** → accept regardless of focus distance. The camera explicitly recorded that the photographer was bracketing focus; the lens may report a constant value (especially at infinity) but the bracket is real.
   - **Constant focus distance, FB signal present (= `Off` or mixed)** → reject. The camera said no bracketing.
   - **Constant focus distance at a finite value, no FB signal** → reject. Strong wind/wave/burst signal at a known subject distance.
   - **Constant focus distance at infinity, no FB signal** → keep. EXIF "infinity" collapses both legitimate far-focus brackets and constant-focus sky bursts into the same value; without the FB tag we err on keeping (a false-positive collection is easy to delete; a silently-dropped real stack is hard to recover).
   - **Focus distance varies across frames** → accept (real focus pull).
   - **Focus distance missing for more than half the frames** → accept (insufficient data, fail-open).

### Metadata sources

By default the plugin reads camera metadata from the Lightroom catalog (`photo:getRawMetadata`) — it's already in memory, so scanning is fast and doesn't touch the source files on disk. The catalog provides timestamps and camera settings but **not** focus-distance or FocusBracketing tags; those live in EXIF/XMP/MakerNotes.

When the catalog source is in use and focus-distance validation is enabled, a small targeted `exiftool` call is issued **only for the files in candidate stacks** to read the focus tags. This keeps the heavy disk read scoped to the small set of files we actually need to validate, instead of reading every raw in the directory.

An alternative ExifTool metadata provider is also available (configurable in the plugin settings) — slower because it reads every source file from disk, but useful when the catalog metadata is stale.

### Raw-format note: DNG vs. native CR3 (Canon)

For Canon bodies, **keeping CR3 files native (not converting to DNG on import)** gives substantially better detection accuracy. The CR3 MakerNotes section contains `FocusBracketing` (On/Off), `FocusBracketingImageCount`, sub-second timestamps, and other fields that authoritatively distinguish a focus bracket from a wind/wave burst at the same camera settings. **Adobe's CR3→DNG conversion strips MakerNotes**, leaving the plugin to fall back on `ApproximateFocusDistance` alone — which collapses to a sentinel "infinity" value for any focus position past the lens's measurable range, so legitimate infinity-focused focus brackets become indistinguishable from sky/cloud bursts. The plugin handles this by erring on the side of keeping all-infinity candidates when no FB signal is present (see step 7), but you'll get cleaner results out of the box if Canon raws come in as CR3. Nikon, Sony, and Fujifilm raws write focus-distance metadata in tags that survive DNG conversion, so the trade-off is Canon-specific.

Detection also makes a best-effort attempt to correlate pre-existing merged result files back to the detected source stack. Exact filename matches are preferred (covers both the plugin's own `_stacked.<ext>` outputs and Helicon Focus's `<name> (N files).<ext>` and legacy `<name>-<count>(method,Rradius,Ssmoothing).<ext>` naming, including Lightroom `-Edit` derivatives), and a conservative heuristic fallback can correlate renamed DNG/TIFF/PSD results by exposure fingerprint and capture timestamp. This only works when the result file lives in the same physical folder as the source files being scanned.

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
