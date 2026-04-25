# Focus Stack Manager — Lightroom Plugin

A Lightroom Classic plugin that detects, processes, and organizes focus-stacked image sequences. Helicon Focus is used to do the actual focus stacking.

This is an *opinionated* workflow plugin. It makes a specific set of tradeoffs based on real-world Lightroom use, with the goal of making focus-stack review and processing fast and predictable rather than exposing every possible organizational option.

One of those tradeoffs is that Lightroom itself handles the organization and filtering workflow. The plugin uses collections and metadata inside the catalog rather than moving files into physical directories or requiring a filesystem-based staging layout.

## Why use it

1. Smarter auto-detection than Helicon batch mode. Detection uses matching exposure settings, timing, and progressing focus distance, which cuts down false positives substantially while staying fast.
2. Recursive detection across folder trees. You can scan an entire folder tree or location at once instead of working one folder at a time.
3. Detection and processing are separate. You can detect first, review each sequence in Lightroom, reject bad source frames, and only then send the good stacks to Helicon.
4. Background processing with queueing. Helicon runs one focus stack at a time while you keep using Lightroom, and the task progress includes completion count and ETA.
5. Lightroom-native organization. Detected stacks are organized into collections, and optional labels or keywords can be applied to both sources and results.
6. Optional collapse into Lightroom photo stacks. In folder view or an individual collection, you can collapse source files and place the newest merged result on top.

## Requirements

- **macOS** — the plugin uses AppleScript for stack grouping and POSIX shell conventions
- Lightroom Classic (SDK 6.0+)
- Python 3 (no additional libraries needed)
- [exiftool](https://exiftool.org/) (`brew install exiftool`)
- [Helicon Focus](https://www.heliconsoft.com/) 8 (for the actual stacking; 9 beta does not work due to a bug that will hopefully be resolved soon)

## Installation

1. Install exiftool: `brew install exiftool`
2. Add the plugin to Lightroom: `File > Plug-in Manager > Add`, then select the `FocusStackManager.lrplugin` folder.
3. Optionally review settings in Plug-in Manager:
   - Helicon defaults and optional executable path override
   - ExifTool path override if Lightroom cannot auto-detect it
   - Detection defaults such as minimum stack size and frame gap
   - Optional color labels, keywords, and root collection set name
   - Python 3 path override if needed

The collection hierarchy is derived automatically from the photo folder path, mirroring your library structure under the configured root set.

## Workflow

1. **Import** photos into Lightroom and navigate to the folder.
2. **Detect** with `Detect Focus Stacks`. The folder and its subdirectories are scanned recursively; collections are created, but nothing is processed yet.
3. **Refine** by reviewing the detected collections and rejecting any blurry or unwanted source frames. Rejected frames are excluded from processing.
4. **Process** with `Process Focus Stacks`. Choose Helicon parameters and let the selected focus stacks run in the background.
5. **Collapse** optionally with `Collapse Focus Stacks` to group source frames into Lightroom photo stacks in folder view. If you process a merged result later, run collapse again to place the newest result on top.
6. **Review** and delete in Lightroom using your normal workflow.

Steps 3 and 4 can be repeated. Each combination of source count and Helicon parameters produces a distinct result file.

## Menu Items

### Detect Focus Stacks

`Library > Plug-in Extras > Detect Focus Stacks`

Run from: a folder in the Library module.

Scans the folder and all its subdirectories recursively for raw files, identifies focus stack sequences, creates a nested collection hierarchy mirroring your folder structure, and optionally applies your chosen source color label or keyword.

Detection also tries to adopt pre-existing merged result files back into the detected stack collections. This currently works only when the merged result lives in the same physical folder as its source files.

Adjacent stacks with continuous focus distance progression can be merged automatically, which helps when a single focus sweep was captured in multiple bursts.

### Process Focus Stacks

`Library > Plug-in Extras > Process Focus Stacks`

Run from: a focus stack collection or collection set under the Focus Stacks hierarchy.

Prompts for Helicon Focus parameters, then queues the selected focus stacks for background processing.

- Rejected source photos are excluded from the merge
- Stacks already processed with the same parameters and source count are skipped
- Different parameters or source count create a new result
- Multiple results can coexist in the same collection

Each completed result is imported back into Lightroom, added to its focus stack collection, and optionally labeled or keyworded. Result files are written as DNGs in the same folder as the source files.

### Create Focus Stack from Selection

`Library > Plug-in Extras > Create Focus Stack from Selection`

Run from: a photo selection in the Library module.

Creates a focus stack collection from your current selection. Use this when auto-detection misses a stack or when you want to group photos manually. All selected photos must come from the same physical folder.

### Collapse Focus Stacks

`Library > Plug-in Extras > Collapse Focus Stacks`

Run from: a folder or an individual focus stack collection.

Groups each focus stack into a Lightroom photo stack in folder view. Source-only focus stacks can be grouped before processing, and if you later create a merged result, re-running collapse adds the newest result on top. Re-running collapse refreshes only focus stacks whose Lightroom stack membership is incomplete; groups already up to date are left alone.

**macOS only** — `Collapse Focus Stacks` uses AppleScript/System Events. In `System Settings > Privacy & Security > Accessibility`, enable **Adobe Lightroom Classic** to control your computer. If needed, also enable `System Events`. This permission is only required for `Collapse Focus Stacks`. If you just granted permission, reopen Lightroom before retrying collapse.

**While collapse is running** — do not use the keyboard or change selection until it finishes.

**Lightroom limitation** — collapse works from a folder or an individual collection, but not from a collection set.

## Common Scenarios

### Reprocessing with different parameters

1. Select the same collection(s) in the sidebar
2. Run `Process Focus Stacks` and change parameters
3. A new result appears alongside the old one
4. Compare and reject/delete the worse one in Lightroom

### Refining after a first `Process Focus Stacks` run

1. In the collection, reject problematic source frames
2. Run `Process Focus Stacks` with the same parameters
3. A new merge runs with fewer frames
4. Compare old and new results, then reject/delete the worse one

### Monitoring and cancelling

- **Progress:** Lightroom progress bar (upper-left)
- **Cancel:** X on progress bar
- **Resume:** Select collections and run `Process Focus Stacks` again; completed stacks are skipped

## Collection Structure

Detected stacks are organized into collections that mirror your folder hierarchy under the configured root set.

This is intentional. Full path mirroring is less compact, but it is deterministic and avoids collisions when different parts of the library reuse similar folder names or dates. The plugin favors that predictability over a shorter but more ambiguous collection tree.

These collections are working organization inside Lightroom, not permanent filesystem structure. They can be deleted at any time without affecting any files on disk.

On disk, merged results stay next to the source files and sort first:

- Collection name: `<first_file>-<last_file> (<count>)`
- Result filename: `-<first>-<last>_<N>f_m<MethodLetter>_s<S>_r<R>_stacked.dng`
- Multiple results from different parameter runs can coexist in the same collection

## FAQ

### Which Helicon Focus version should I use?

Use Helicon Focus 8.

Helicon Focus 9 beta currently appears to have a CLI bug that breaks this workflow in headless mode, even though interactive use may still work. Until that is fixed upstream, this plugin should be considered a Helicon 8 workflow.

### Why is this macOS-only?

The plugin currently depends on macOS-specific behavior in two places:

- `Collapse Focus Stacks` uses AppleScript/System Events to send `Cmd+G` to Lightroom
- executable discovery is currently written around macOS application and install conventions

Windows support has not been built or tested because the project is developed and used on macOS.

### What would be required to port it to Windows?

At a minimum:

- replace the AppleScript-based collapse automation with a Windows equivalent
- change the stacking shortcut logic from `Cmd+G` to the Windows Lightroom shortcut
- update Helicon Focus executable discovery for Windows install locations
- update ExifTool discovery and shell/path handling for Windows conventions
- test the Lightroom SDK behavior and UI automation on Windows end to end

The core detection and processing logic is mostly portable, but the Lightroom integration and collapse workflow are where the platform-specific work lives.

### What source formats are supported?

Auto-detection currently scans these raw formats:

- `dng`, `cr2`, `cr3`, `nef`, `arw`, `raf`, `orf`, `rw2`, `raw`

The plugin has been used primarily with DNG source files, but detection is not limited to DNG.

### Why is the output always DNG?

The plugin accepts raw source files as input and standardizes on a raw output file. In practice, that means DNG.

Helicon can also produce TIFF, but if you want the merged result to stay in a raw format, DNG is the output format available for that workflow. So the merged result is always written as DNG, regardless of the source raw format.

## Bugs / Support

There are no known critical bugs at the moment, but this project should still be treated as early-stage software.

- The Python detection and processing code has test coverage.
- The Lightroom integration is much more constrained by Lightroom Classic’s plugin APIs and documentation.
- In practice, that means the Lightroom-facing parts are the most likely place for edge cases or unexpected behavior.

If you run into a bug, have workflow feedback, or want to contribute a fix:

- open an issue or pull request on GitHub
- or email `ron@smallscenes.com`

Bug reports are welcome. Feature requests are less likely to be taken on, because the plugin is intentionally opinionated and not trying to become a general-purpose focus stacking toolkit.

Support and turnaround time are not guaranteed to be fast, and not guaranteed at all.

## Maintainer

- Ron Coscorrosa
- Email: `ron@smallscenes.com`
- Website: https://smallscenes.com

## License

MIT — see [LICENSE](LICENSE).
