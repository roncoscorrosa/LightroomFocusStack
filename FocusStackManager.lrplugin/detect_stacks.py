#!/usr/bin/env python3
"""
Focus Stack Detector — JSON output for Lightroom plugin integration.

Detects focus-bracketed image sequences using EXIF heuristics and outputs
structured JSON for consumption by the Lightroom FocusStackManager plugin.

Detection Heuristics:
    1. Exposure mode filter: Only considers Manual exposure mode (configurable)
    2. Camera settings: Groups images with identical focal length, ISO, aperture,
       and shutter speed
    3. Temporal proximity: Sequences where images are taken within max_gap seconds
       after the previous exposure ends (accounts for long exposure times)
    4. Minimum sequence length: Requires at least min_sequence_length images
    5. Focus distance validation: Reads ApproximateFocusDistance (XMP-aux).
       If all frames have the same focus distance, the sequence is rejected
       as a false positive (e.g., wind-timing shots).

Requirements:
    exiftool (brew install exiftool)

Usage:
    # Detect stacks in a directory, output JSON
    python detect_stacks.py <directory>

    # With custom parameters
    python detect_stacks.py <directory> --min-images 5 --max-gap 2.0

    # Include all exposure modes (not just manual)
    python detect_stacks.py <directory> --all-modes
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, asdict
from collections import defaultdict


class StageTimer:
    """Accumulates wall-clock time per named stage using time.perf_counter."""

    def __init__(self):
        self.stages: Dict[str, float] = defaultdict(float)
        self.counts: Dict[str, int] = defaultdict(int)

    @contextmanager
    def track(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] += time.perf_counter() - start
            self.counts[name] += 1

    def add(self, name: str, seconds: float):
        self.stages[name] += seconds
        self.counts[name] += 1

    def merge(self, other: "StageTimer", prefix: str = ""):
        for name, seconds in other.stages.items():
            key = prefix + name if prefix else name
            self.stages[key] += seconds
            self.counts[key] += other.counts[name]

    def to_dict(self) -> Dict[str, Dict[str, float]]:
        return {
            name: {
                "seconds": round(self.stages[name], 6),
                "count": self.counts[name],
            }
            for name in sorted(self.stages.keys())
        }


DEFAULTS = {
    'max_gap_seconds': 1.5,
    'min_sequence_length': 4,
    'manual_mode_only': True,
    # For long exposures, the camera needs more buffer time between frames.
    # The allowed gap scales: max_gap + (exposure_time * gap_exposure_scale).
    # With defaults: 1.5 + (10s * 0.05) = 2.0s for a 10s exposure.
    'gap_exposure_scale': 0.05,
    # Maximum allowed gap between adjacent stacks, expressed as a multiple of
    # the last exposure time in the earlier stack. This keeps stack merging
    # tight for short exposures while still allowing longer exposures a bit of
    # breathing room.
    'max_merge_gap_exposure_multiplier': 3.0,
    # Maximum allowed gap between the last focus distance in stack A and the
    # first focus distance in stack B, relative to the total focus range.
    # E.g., if stack A spans 2.0-4.0m and stack B starts at 4.5m, the gap
    # is 0.5m and the combined range is 2.0-8.0m = 6.0m. Ratio = 0.5/6.0 = 0.08.
    'merge_focus_distance_gap_ratio': 0.25,
}

RAW_EXTENSIONS = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.raw'}
HEURISTIC_RESULT_EXTENSIONS = {'.dng', '.tif', '.tiff', '.psd'}
RENDERED_RESULT_EXTENSIONS = {'.tif', '.tiff', '.psd'}
COMMON_EXIFTOOL_PATHS = (
    "/opt/homebrew/bin/exiftool",
    "/usr/local/bin/exiftool",
)
# Keep this isolated behind a constant so it is easy to disable later if
# real-world false positives show up.
ENABLE_HEURISTIC_RESULT_CORRELATION = True
RESULT_FILENAME_PATTERNS = (
    re.compile(r"_stacked\.[^.]+$", re.IGNORECASE),
    re.compile(r"\(\d+\s+files\)\.[^.]+$", re.IGNORECASE),
)
PLUGIN_RESULT_PATTERN = re.compile(
    r"^-(?P<first>.+)-(?P<last>.+?)_(?P<count>\d+)f_m(?P<method>[A-Z0-9]+)_s(?P<smoothing>\d+)_r(?P<radius>\d+)_stacked$",
    re.IGNORECASE,
)
EXTERNAL_RESULT_PATTERN = re.compile(
    r"^(?P<last>.+)\s+\((?P<count>\d+)\s+files\)$",
    re.IGNORECASE,
)


@dataclass
class ImageMetadata:
    filepath: str
    filename: str
    timestamp: Optional[datetime]
    focal_length: Optional[float]
    iso: Optional[int]
    aperture: Optional[float]
    shutter_speed: Optional[str]
    exposure_time_seconds: Optional[float]
    exposure_mode: Optional[str]

    def get_signature(self) -> Tuple:
        return (self.focal_length, self.iso, self.aperture, self.shutter_speed)


@dataclass
class FocusStack:
    name: str
    count: int
    files: List[str]
    focal_length: Optional[float]
    iso: Optional[int]
    aperture: Optional[float]
    shutter_speed: Optional[str]
    time_span_seconds: float
    first_image_time: Optional[str]
    last_exposure_time_seconds: Optional[float]


def parse_entry_timestamp(entry: dict) -> Optional[datetime]:
    """Parse the best available timestamp from an exiftool entry."""
    dt_str = entry.get("DateTimeOriginal") or entry.get("CreateDate")
    subsec_str = entry.get("SubSecTimeOriginal")
    if not dt_str:
        return None

    if isinstance(dt_str, str) and '.' in dt_str:
        parts = dt_str.split('.')
        return parse_exif_timestamp(parts[0], parts[1] if len(parts) > 1 else None)

    return parse_exif_timestamp(str(dt_str), str(subsec_str) if subsec_str is not None else None)


def parse_exif_timestamp(timestamp_str: str, subsec_str: Optional[str] = None) -> Optional[datetime]:
    """Parse EXIF timestamp with optional subsecond precision."""
    try:
        dt = datetime.strptime(str(timestamp_str), "%Y:%m:%d %H:%M:%S")
        if subsec_str:
            try:
                subsec_value = int(subsec_str)
                divisor = 10 ** len(subsec_str)
                subsec = subsec_value / divisor
                dt = dt.replace(microsecond=int(subsec * 1000000))
            except (ValueError, TypeError):
                pass
        return dt
    except (ValueError, TypeError):
        return None


def parse_fraction(value_str) -> Optional[float]:
    """Parse a value that may be a fraction (e.g., '1/4'), decimal, or integer."""
    if value_str is None:
        return None
    try:
        s = str(value_str)
        if '/' in s:
            num, denom = s.split('/')
            return float(num) / float(denom)
        return float(s)
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def is_known_result_filename(path: Path) -> bool:
    name = path.name
    return any(pattern.search(name) for pattern in RESULT_FILENAME_PATTERNS)


def is_likely_result_file(path: Path, entry: Optional[dict]) -> bool:
    if is_known_result_filename(path):
        return True

    if not entry or path.suffix.lower() != ".dng":
        return False

    preview_app = str(entry.get("PreviewApplicationName") or "").strip().lower()
    if preview_app == "helicon focus":
        return True

    photometric = str(entry.get("PhotometricInterpretation") or "").strip().lower()
    samples = entry.get("SamplesPerPixel")
    try:
        samples_per_pixel = int(samples) if samples is not None else None
    except (ValueError, TypeError):
        samples_per_pixel = None

    return photometric == "linear raw" and samples_per_pixel == 3


def is_candidate_result_file(path: Path, entry: Optional[dict]) -> bool:
    """Return True when a file is worth considering as a merged-result candidate."""
    suffix = path.suffix.lower()
    if suffix in RENDERED_RESULT_EXTENSIONS:
        return True
    return is_likely_result_file(path, entry)


def parse_result_file_info(path: Path) -> Optional[Dict]:
    stem = path.stem

    match = PLUGIN_RESULT_PATTERN.match(stem)
    if match:
        return {
            "path": str(path),
            "filename": path.name,
            "kind": "plugin",
            "first_stem": match.group("first"),
            "last_stem": match.group("last"),
            "count": int(match.group("count")),
            "method": match.group("method"),
            "smoothing": int(match.group("smoothing")),
            "radius": int(match.group("radius")),
        }

    match = EXTERNAL_RESULT_PATTERN.match(stem)
    if match:
        return {
            "path": str(path),
            "filename": path.name,
            "kind": "external",
            "first_stem": None,
            "last_stem": match.group("last"),
            "count": int(match.group("count")),
            "method": None,
            "smoothing": None,
            "radius": None,
        }

    return {
        "path": str(path),
        "filename": path.name,
        "kind": "unknown",
        "first_stem": None,
        "last_stem": None,
        "count": None,
        "method": None,
        "smoothing": None,
        "radius": None,
    }


def build_result_file_info(path: Path, entry: Optional[dict]) -> Dict:
    info = parse_result_file_info(path)
    metadata = exiftool_entry_to_metadata(str(path), entry or {})
    timestamp = metadata.timestamp if metadata else parse_entry_timestamp(entry or {})
    preview_app = str((entry or {}).get("PreviewApplicationName") or "").strip()
    software = str((entry or {}).get("Software") or "").strip()
    creator_tool = str((entry or {}).get("CreatorTool") or "").strip()

    info.update({
        "timestamp": timestamp.isoformat(sep=' ') if timestamp else None,
        "focal_length": metadata.focal_length if metadata else None,
        "iso": metadata.iso if metadata else None,
        "aperture": metadata.aperture if metadata else None,
        "shutter_speed": metadata.shutter_speed if metadata else None,
        "extension": path.suffix.lower(),
        "preview_application_name": preview_app,
        "software": software,
        "creator_tool": creator_tool,
    })
    return info


def _stack_time_bounds(stack: FocusStack,
                       metadata_by_path: Dict[str, ImageMetadata]) -> Tuple[Optional[datetime], Optional[datetime]]:
    timestamps = [
        metadata_by_path[filepath].timestamp
        for filepath in stack.files
        if filepath in metadata_by_path and metadata_by_path[filepath].timestamp is not None
    ]
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def _matches_stack_signature(stack: FocusStack, result: Dict) -> bool:
    return (
        result.get("focal_length") is not None and
        result.get("iso") is not None and
        result.get("aperture") is not None and
        result.get("shutter_speed") is not None and
        stack.focal_length == result.get("focal_length") and
        stack.iso == result.get("iso") and
        stack.aperture == result.get("aperture") and
        stack.shutter_speed == result.get("shutter_speed")
    )


def find_heuristic_result_match(stacks: List[FocusStack],
                                result: Dict,
                                metadata_by_path: Dict[str, ImageMetadata]) -> Optional[str]:
    """Return a stack name for a unique, high-confidence heuristic result match."""
    result_timestamp = result.get("timestamp")
    if isinstance(result_timestamp, str):
        try:
            result_timestamp = datetime.fromisoformat(result_timestamp)
        except ValueError:
            result_timestamp = None
    if result_timestamp is None:
        return None

    matches = []
    for stack in stacks:
        if not _matches_stack_signature(stack, result):
            continue

        if result.get("count") is not None and result["count"] != stack.count:
            continue

        start_time, end_time = _stack_time_bounds(stack, metadata_by_path)
        if start_time is None or end_time is None:
            continue

        time_slack_seconds = max(1.0, float(stack.last_exposure_time_seconds or 0.0))
        lower_bound = start_time - timedelta(seconds=1.0)
        upper_bound = end_time + timedelta(seconds=time_slack_seconds)
        if not (lower_bound <= result_timestamp <= upper_bound):
            continue

        matches.append(stack.name)

    if len(matches) == 1:
        return matches[0]
    return None


def attach_result_files_to_stacks(stacks: List[FocusStack],
                                  result_files: List[Dict],
                                  metadata_by_path: Optional[Dict[str, ImageMetadata]] = None,
                                  enable_heuristic_result_correlation: bool = ENABLE_HEURISTIC_RESULT_CORRELATION) -> List[Dict]:
    attached = []
    matched_paths = set()

    for stack in stacks:
        first_stem = Path(stack.files[0]).stem if stack.files else None
        last_stem = Path(stack.files[-1]).stem if stack.files else None
        stack_results = []

        for result in result_files:
            if result.get("kind") == "unknown":
                continue

            count = result.get("count")
            if count is None or count != stack.count:
                continue

            result_last = result.get("last_stem")
            if result_last is not None and result_last != last_stem:
                continue

            result_first = result.get("first_stem")
            if result_first is not None and result_first != first_stem:
                continue

            stack_results.append(result)
            matched_paths.add(result["path"])

        stack_dict = asdict(stack)
        stack_dict["result_files"] = stack_results
        attached.append(stack_dict)

    if not enable_heuristic_result_correlation or not metadata_by_path:
        return attached

    stack_by_name = {stack.name: stack for stack in stacks}
    attached_by_name = {stack["name"]: stack for stack in attached}
    unmatched = [result for result in result_files if result["path"] not in matched_paths]

    for result in unmatched:
        matched_stack_name = find_heuristic_result_match(stacks, result, metadata_by_path)
        if not matched_stack_name:
            continue
        attached_by_name[matched_stack_name]["result_files"].append(result)
        matched_paths.add(result["path"])

    return attached


EXIFTOOL_DEFAULT_TAGS = (
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
)


def batch_extract_metadata(file_paths: List[str],
                           exiftool_path: Optional[str] = None,
                           timer: Optional[StageTimer] = None,
                           tags: Optional[List[str]] = None) -> Dict[str, dict]:
    """Read EXIF/XMP fields for multiple files in one exiftool call.

    Returns a dict mapping file path -> raw exiftool entry dict.

    Args:
        file_paths: Files to inspect.
        exiftool_path: Optional explicit exiftool binary path.
        timer: Optional StageTimer for instrumentation.
        tags: Optional list of exiftool tag flags (e.g. ["-ApproximateFocusDistance"]).
              Defaults to EXIFTOOL_DEFAULT_TAGS — the full set used by the detection
              algorithm. Pass a narrower list when only one tag is needed (e.g. the
              focus-distance validation pass).
    """
    if not file_paths:
        return {}

    effective_exiftool = exiftool_path
    if effective_exiftool:
        if not Path(effective_exiftool).exists():
            print(f"Error: exiftool not found at: {effective_exiftool}",
                  file=sys.stderr)
            sys.exit(1)
    else:
        effective_exiftool = shutil.which("exiftool")
        if not effective_exiftool:
            for candidate in COMMON_EXIFTOOL_PATHS:
                if Path(candidate).exists():
                    effective_exiftool = candidate
                    break

    if not effective_exiftool:
        print("Error: exiftool not found. Install with: brew install exiftool, "
              "or set ExifTool Path in the plugin settings.",
              file=sys.stderr)
        sys.exit(1)

    try:
        effective_tags = list(tags) if tags is not None else list(EXIFTOOL_DEFAULT_TAGS)
        cmd = [
            str(effective_exiftool),
            # -fast2 skips reading past the metadata block (and avoids
            # PreviewImage extraction). Benchmarked safe for our tag set on
            # cold cache; -fast3 was ~3× faster but dropped all our tags.
            "-fast2",
            "-json",
        ] + effective_tags + list(file_paths)
        if timer is not None:
            with timer.track("exiftool_subprocess"):
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
        if result.returncode != 0:
            print(f"Error: exiftool failed: {result.stderr}", file=sys.stderr)
            return {}

        if timer is not None:
            with timer.track("exiftool_json_parse"):
                data = json.loads(result.stdout)
        else:
            data = json.loads(result.stdout)
        return {entry["SourceFile"]: entry for entry in data}
    except subprocess.TimeoutExpired:
        print("Error: exiftool timed out", file=sys.stderr)
        return {}
    except (json.JSONDecodeError, Exception) as e:
        print(f"Error: failed to parse exiftool output: {e}", file=sys.stderr)
        return {}


def exiftool_entry_to_metadata(filepath: str, entry: dict) -> Optional[ImageMetadata]:
    """Convert a single exiftool JSON entry to an ImageMetadata object."""
    # Timestamp
    timestamp = None
    timestamp = parse_entry_timestamp(entry)

    # Focal length — exiftool returns "200.0 mm" or just a number
    focal_length = None
    fl_raw = entry.get("FocalLength")
    if fl_raw is not None:
        if isinstance(fl_raw, (int, float)):
            focal_length = float(fl_raw)
        else:
            # Strip " mm" suffix if present
            focal_length = parse_fraction(str(fl_raw).replace(" mm", "").strip())

    # ISO
    iso = None
    iso_raw = entry.get("ISO")
    if iso_raw is not None:
        try:
            iso = int(iso_raw)
        except (ValueError, TypeError):
            pass

    # Aperture
    aperture = parse_fraction(entry.get("FNumber"))

    # Exposure time — exiftool returns either a decimal or fraction string
    shutter_speed = None
    exposure_time_seconds = None
    et_raw = entry.get("ExposureTime")
    if et_raw is not None:
        shutter_speed = str(et_raw)
        exposure_time_seconds = parse_fraction(et_raw)

    # Exposure mode
    exposure_mode = None
    em_raw = entry.get("ExposureMode")
    if em_raw is not None:
        exposure_mode = str(em_raw)

    return ImageMetadata(
        filepath=filepath,
        filename=Path(filepath).name,
        timestamp=timestamp,
        focal_length=focal_length,
        iso=iso,
        aperture=aperture,
        shutter_speed=shutter_speed,
        exposure_time_seconds=exposure_time_seconds,
        exposure_mode=exposure_mode,
    )


# Keep for compatibility with tests that mock this function
def extract_metadata(filepath: Path) -> Optional[ImageMetadata]:
    """Extract metadata for a single file via exiftool. Prefer batch_extract_metadata."""
    entries = batch_extract_metadata([str(filepath)])
    entry = entries.get(str(filepath))
    if not entry:
        return None
    return exiftool_entry_to_metadata(str(filepath), entry)


def find_raw_files(directory: Path) -> List[Path]:
    files = []
    for ext in RAW_EXTENSIONS:
        files.extend(directory.glob(f'*{ext}'))
        files.extend(directory.glob(f'*{ext.upper()}'))
    return sorted(files)


def find_result_candidate_files(directory: Path) -> List[Path]:
    files = set()
    for ext in HEURISTIC_RESULT_EXTENSIONS:
        files.update(directory.glob(f'*{ext}'))
        files.update(directory.glob(f'*{ext.upper()}'))
    return sorted(files)


def find_temporal_sequences(images: List[ImageMetadata],
                            max_gap_seconds: float,
                            min_sequence_length: int,
                            gap_exposure_scale: float = DEFAULTS['gap_exposure_scale'],
                            ) -> List[List[ImageMetadata]]:
    if not images:
        return []

    sorted_images = sorted(
        [img for img in images if img.timestamp is not None],
        key=lambda x: x.timestamp,
    )

    if len(sorted_images) < min_sequence_length:
        return []

    sequences = []
    current_sequence = [sorted_images[0]]

    for i in range(1, len(sorted_images)):
        prev_img = sorted_images[i - 1]
        curr_img = sorted_images[i]

        if prev_img.exposure_time_seconds is not None:
            prev_end_time = prev_img.timestamp.timestamp() + prev_img.exposure_time_seconds
            curr_start_time = curr_img.timestamp.timestamp()
            time_since_prev_ended = curr_start_time - prev_end_time
            # Scale the allowed gap for long exposures — cameras need more
            # buffer time to write longer exposures before the next frame.
            effective_max_gap = max_gap_seconds + (prev_img.exposure_time_seconds * gap_exposure_scale)
        else:
            time_since_prev_ended = (curr_img.timestamp - prev_img.timestamp).total_seconds()
            effective_max_gap = max_gap_seconds

        if 0 <= time_since_prev_ended <= effective_max_gap:
            current_sequence.append(curr_img)
        else:
            if len(current_sequence) >= min_sequence_length:
                sequences.append(current_sequence)
            current_sequence = [curr_img]

    if len(current_sequence) >= min_sequence_length:
        sequences.append(current_sequence)

    return sequences


def generate_stack_name(images: List[ImageMetadata]) -> str:
    first_stem = Path(images[0].filepath).stem
    last_stem = Path(images[-1].filepath).stem
    return f"{first_stem}-{last_stem}"


def has_varying_focus_distance(file_paths: List[str],
                               focus_distances: Dict[str, Optional[float]],
                               tolerance: float = 0.01) -> Optional[bool]:
    """Check whether focus distance varies across a set of files.

    Returns:
        True  - focus distance varies (confirmed focus stack)
        False - all frames have the same focus distance (likely NOT a focus stack)
        None  - insufficient data to determine (tag missing for most/all frames)
    """
    distances = []
    for fp in file_paths:
        d = focus_distances.get(fp)
        if d is not None:
            distances.append(d)

    # If we have focus distance for fewer than half the frames, can't decide
    if len(distances) < len(file_paths) // 2:
        return None

    if not distances:
        return None

    # Check if all distances are the same within tolerance
    min_d = min(distances)
    max_d = max(distances)
    if (max_d - min_d) <= tolerance:
        return False

    return True


def merge_adjacent_stacks(stacks: List[FocusStack],
                          focus_distances: Dict[str, Optional[float]],
                          max_merge_gap_exposure_multiplier: float = DEFAULTS['max_merge_gap_exposure_multiplier'],
                          merge_focus_gap_ratio: float = DEFAULTS['merge_focus_distance_gap_ratio'],
                          ) -> List[FocusStack]:
    """Merge adjacent stacks that form a continuous focus distance progression.

    Sometimes a photographer takes a focus stack, reviews the LCD, and decides
    to extend the stack with more frames. The auto-detector sees two separate
    temporal sequences, but the focus distances show they're one continuous sweep.

    Merge criteria (all must be met):
        1. Same camera settings (already guaranteed — stacks come from same settings group)
        2. Sequential in time (stack B starts after stack A ends)
        3. Time gap between them is less than a small multiple of the last
           exposure time in stack A
        4. Focus distances are progressive — stack A's last distance is close to
           stack B's first distance, relative to the total range
    """
    if len(stacks) <= 1 or not focus_distances:
        return stacks

    def get_focus_range(stack):
        """Get (first_distance, last_distance) for a stack's files, in file order."""
        distances = []
        for fp in stack.files:
            d = focus_distances.get(fp)
            if d is not None:
                distances.append(d)
        if not distances:
            return None, None
        return distances[0], distances[-1]

    def get_focus_sequence(stack):
        """Get all known focus distances for a stack, in file order."""
        distances = []
        for fp in stack.files:
            d = focus_distances.get(fp)
            if d is not None:
                distances.append(d)
        return distances

    def get_direction_and_step(distances):
        """Return (direction, median_abs_step) for a focus distance sequence.

        direction is +1 for near-to-far, -1 for far-to-near, or None if the
        sequence is not reliably monotonic.
        """
        if len(distances) < 2:
            return None, None

        deltas = []
        for i in range(1, len(distances)):
            delta = distances[i] - distances[i - 1]
            if abs(delta) > 0.01:
                deltas.append(delta)

        if not deltas:
            return None, None

        positive = [d for d in deltas if d > 0]
        negative = [d for d in deltas if d < 0]

        if positive and negative:
            return None, None

        abs_steps = sorted(abs(d) for d in deltas)
        median_step = abs_steps[len(abs_steps) // 2]
        direction = 1 if positive else -1
        return direction, median_step

    def get_stack_end_time(stack):
        """Get the timestamp of the last frame (approximate end of stack)."""
        # Parse from first_image_time + time_span
        if stack.first_image_time and stack.time_span_seconds is not None:
            try:
                first_t = datetime.strptime(stack.first_image_time, "%Y-%m-%d %H:%M:%S")
                return first_t.timestamp() + stack.time_span_seconds
            except (ValueError, TypeError):
                pass
        return None

    def get_stack_start_time(stack):
        if stack.first_image_time:
            try:
                return datetime.strptime(stack.first_image_time, "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError):
                pass
        return None

    # Sort stacks by start time
    sorted_stacks = sorted(stacks, key=lambda s: s.first_image_time or "")

    merged = []
    current = sorted_stacks[0]

    for i in range(1, len(sorted_stacks)):
        next_stack = sorted_stacks[i]

        # Check time gap
        current_end = get_stack_end_time(current)
        next_start = get_stack_start_time(next_stack)

        if current_end is None or next_start is None:
            merged.append(current)
            current = next_stack
            continue

        time_gap = next_start - current_end
        merge_gap_limit = None
        if current.last_exposure_time_seconds is not None:
            merge_gap_limit = current.last_exposure_time_seconds * max_merge_gap_exposure_multiplier

        if merge_gap_limit is None or time_gap < 0 or time_gap > merge_gap_limit:
            merged.append(current)
            current = next_stack
            continue

        # Check focus distance progression
        current_first_d, current_last_d = get_focus_range(current)
        next_first_d, next_last_d = get_focus_range(next_stack)
        current_distances = get_focus_sequence(current)
        next_distances = get_focus_sequence(next_stack)

        if any(d is None for d in [current_first_d, current_last_d, next_first_d, next_last_d]):
            merged.append(current)
            current = next_stack
            continue

        current_direction, current_step = get_direction_and_step(current_distances)
        next_direction, next_step = get_direction_and_step(next_distances)
        if current_direction is None or next_direction is None or current_direction != next_direction:
            merged.append(current)
            current = next_stack
            continue

        # The focus distance gap between stacks
        focus_gap = abs(next_first_d - current_last_d)

        # Total range if merged
        all_distances = [current_first_d, current_last_d, next_first_d, next_last_d]
        total_range = max(all_distances) - min(all_distances)

        if total_range <= 0:
            # Both stacks at same distance — don't merge (not progressive)
            merged.append(current)
            current = next_stack
            continue

        gap_ratio = focus_gap / total_range
        step_tolerance = max(current_step or 0.0, next_step or 0.0)
        overlap_tolerance = step_tolerance * 0.35
        forward_gap_tolerance = step_tolerance * 2.0

        if current_direction > 0:
            continues_direction = next_last_d > current_last_d + 0.01
            boundary_is_close = (
                next_first_d >= current_last_d - overlap_tolerance and
                next_first_d <= current_last_d + forward_gap_tolerance
            )
        else:
            continues_direction = next_last_d < current_last_d - 0.01
            boundary_is_close = (
                next_first_d <= current_last_d + overlap_tolerance and
                next_first_d >= current_last_d - forward_gap_tolerance
            )

        if gap_ratio <= merge_focus_gap_ratio and boundary_is_close and continues_direction:
            # Merge: combine files and update metadata
            combined_files = current.files + next_stack.files
            first_stem = Path(combined_files[0]).stem
            last_stem = Path(combined_files[-1]).stem

            time_span = 0.0
            if current.first_image_time:
                next_end = get_stack_end_time(next_stack)
                if next_end and current_end:
                    time_span = next_end - get_stack_start_time(current)

            current = FocusStack(
                name=f"{first_stem}-{last_stem}",
                count=len(combined_files),
                files=combined_files,
                focal_length=current.focal_length,
                iso=current.iso,
                aperture=current.aperture,
                shutter_speed=current.shutter_speed,
                time_span_seconds=time_span,
                first_image_time=current.first_image_time,
                last_exposure_time_seconds=next_stack.last_exposure_time_seconds,
            )
            print(f"Merged stacks: {current.name} ({current.count} frames, "
                  f"focus gap ratio {gap_ratio:.2f})", file=sys.stderr)
        else:
            merged.append(current)
            current = next_stack

    merged.append(current)
    return merged


def detect_stacks(directory: Path,
                  max_gap_seconds: float = DEFAULTS['max_gap_seconds'],
                  min_sequence_length: int = DEFAULTS['min_sequence_length'],
                  manual_mode_only: bool = DEFAULTS['manual_mode_only'],
                  skip_focus_distance_check: bool = False,
                  exiftool_path: Optional[str] = None,
                  enable_heuristic_result_correlation: bool = ENABLE_HEURISTIC_RESULT_CORRELATION,
                  timer: Optional[StageTimer] = None,
                  metadata: Optional[Dict[str, dict]] = None,
                  validate_focus_distance: bool = False) -> Dict:
    """Run detection on a directory.

    Args:
        metadata: Optional pre-fetched metadata dict (path -> exiftool-shaped entry).
            When provided, skip the bulk exiftool call entirely. Typically supplied
            by a Lua-side provider (Lightroom catalog or exiftool wrapper).
        validate_focus_distance: When True, after candidate stacks are detected,
            run a small targeted exiftool call to fetch ApproximateFocusDistance
            for the candidate-stack files only, then merge/validate against it.
            Has no effect when metadata is None and the bulk exiftool call already
            includes the focus distance field.
    """
    owns_timer = timer is None
    if owns_timer:
        timer = StageTimer()

    with timer.track("filesystem_walk"):
        raw_files = find_raw_files(directory)
        result_candidate_files = find_result_candidate_files(directory)

    if not raw_files and not result_candidate_files:
        return {
            "directory": str(directory),
            "total_raw_files": 0,
            "stacks": [],
            "rejected_stacks": [],
            "non_stack_files": [],
            "_timing": timer.to_dict() if owns_timer else None,
        }

    all_paths = sorted({str(f) for f in raw_files + result_candidate_files})
    if metadata is not None:
        with timer.track("metadata_external"):
            raw_entries = {p: metadata[p] for p in all_paths if p in metadata}
    else:
        raw_entries = batch_extract_metadata(
            all_paths,
            exiftool_path=exiftool_path,
            timer=timer,
        )

    with timer.track("classify_results"):
        # Iterate the union: raw sources (CR3/NEF/etc.) only appear in raw_files,
        # rendered results (TIF/PSD) only in result_candidate_files, and DNG can
        # be either a source or a Helicon-rendered result so it's in both.
        source_raw_files = []
        excluded_result_files = []
        excluded_result_infos = []
        raw_file_set = {str(filepath) for filepath in raw_files}
        result_candidate_set = {str(filepath) for filepath in result_candidate_files}
        for fp_str in sorted(raw_file_set | result_candidate_set):
            path = Path(fp_str)
            entry = raw_entries.get(fp_str)
            is_raw = fp_str in raw_file_set

            if is_raw and not is_likely_result_file(path, entry):
                source_raw_files.append(path)
                continue

            if is_candidate_result_file(path, entry):
                excluded_result_files.append(fp_str)
                excluded_result_infos.append(build_result_file_info(path, entry))

        raw_files = source_raw_files

    if not raw_files:
        return {
            "directory": str(directory),
            "total_raw_files": 0,
            "excluded_result_files": excluded_result_files,
            "unmatched_result_files": excluded_result_infos,
            "stacks": [],
            "rejected_stacks": [],
            "non_stack_files": [],
            "_timing": timer.to_dict() if owns_timer else None,
        }

    with timer.track("metadata_parse"):
        metadata_list = []
        metadata_by_path = {}
        focus_distances = {}  # Collected from the same exiftool call

        for filepath in raw_files:
            fp = str(filepath)
            entry = raw_entries.get(fp)
            if not entry:
                continue
            image_metadata = exiftool_entry_to_metadata(fp, entry)
            if image_metadata:
                metadata_list.append(image_metadata)
                metadata_by_path[fp] = image_metadata

            # Extract focus distance from the same data
            dist = entry.get("ApproximateFocusDistance")
            if dist is not None:
                try:
                    focus_distances[fp] = float(dist)
                except (ValueError, TypeError):
                    focus_distances[fp] = None
            else:
                focus_distances[fp] = None

        # Filter to manual mode if specified.
        # Match "Manual" (exiftool ExposureMode) and "Manual exposure" (LR
        # exposureProgram) — both mean the same thing.
        if manual_mode_only:
            metadata_list = [
                m for m in metadata_list
                if m.exposure_mode and m.exposure_mode.strip().lower().startswith('manual')
            ]

    with timer.track("group_and_sequence"):
        # Group by camera settings
        setting_groups = defaultdict(list)
        for img in metadata_list:
            setting_groups[img.get_signature()].append(img)

        # Find temporal sequences within each group
        candidate_stacks = []
        candidate_stack_files = set()

        for signature, images in setting_groups.items():
            sequences = find_temporal_sequences(images, max_gap_seconds, min_sequence_length)
            for sequence in sequences:
                name = generate_stack_name(sequence)
                first_img = sequence[0]
                last_img = sequence[-1]

                time_span = 0.0
                if first_img.timestamp and last_img.timestamp:
                    time_span = (last_img.timestamp - first_img.timestamp).total_seconds()

                first_time_str = None
                if first_img.timestamp:
                    first_time_str = first_img.timestamp.strftime("%Y-%m-%d %H:%M:%S")

                stack = FocusStack(
                    name=name,
                    count=len(sequence),
                    files=[img.filepath for img in sequence],
                    focal_length=first_img.focal_length,
                    iso=first_img.iso,
                    aperture=first_img.aperture,
                    shutter_speed=first_img.shutter_speed,
                    time_span_seconds=time_span,
                    first_image_time=first_time_str,
                    last_exposure_time_seconds=last_img.exposure_time_seconds,
                )
                candidate_stacks.append(stack)
                for img in sequence:
                    candidate_stack_files.add(img.filepath)

    # Targeted focus-distance pass: when the bulk metadata came from a non-exiftool
    # source (e.g. Lightroom catalog), ApproximateFocusDistance is not available.
    # Fetch it via exiftool but only for files in candidate stacks — typically tens
    # of files instead of thousands.
    if (validate_focus_distance
            and not skip_focus_distance_check
            and candidate_stack_files
            and metadata is not None):
        with timer.track("focus_distance_validation_fetch"):
            focus_entries = batch_extract_metadata(
                sorted(candidate_stack_files),
                exiftool_path=exiftool_path,
                timer=timer,
                tags=["-ApproximateFocusDistance"],
            )
            for fp, entry in focus_entries.items():
                dist = entry.get("ApproximateFocusDistance")
                if dist is not None:
                    try:
                        focus_distances[fp] = float(dist)
                    except (ValueError, TypeError):
                        focus_distances[fp] = None

    with timer.track("merge_and_validate"):
        # Merge adjacent stacks that form a continuous focus distance progression.
        # This handles the case where a photographer extends a stack after reviewing.
        if not skip_focus_distance_check and focus_distances:
            candidate_stacks = merge_adjacent_stacks(
                candidate_stacks, focus_distances,
            )
            # Rebuild the candidate_stack_files set after merging
            candidate_stack_files = set()
            for stack in candidate_stacks:
                for fp in stack.files:
                    candidate_stack_files.add(fp)

        # Focus distance validation: reject candidates where all frames
        # have the same focus distance (likely wind-timing or burst shots,
        # not actual focus stacks).
        all_stacks = []
        rejected_stacks = []
        stack_files = set()

        if skip_focus_distance_check or not candidate_stacks:
            all_stacks = candidate_stacks
            stack_files = candidate_stack_files
        else:
            for stack in candidate_stacks:
                if not focus_distances:
                    # No focus distance data at all — keep all candidates
                    all_stacks.append(stack)
                    for fp in stack.files:
                        stack_files.add(fp)
                    continue

                varies = has_varying_focus_distance(stack.files, focus_distances)
                if varies is False:
                    # All frames have the same focus distance — not a focus stack
                    rejected_stacks.append(stack)
                    print(f"Rejected {stack.name}: constant focus distance "
                          f"(likely wind-timing shots, not a focus stack)",
                          file=sys.stderr)
                else:
                    # varies is True (confirmed) or None (insufficient data — keep it)
                    all_stacks.append(stack)
                    for fp in stack.files:
                        stack_files.add(fp)

        # Identify non-stack files
        non_stack_files = [str(f) for f in raw_files if str(f) not in stack_files]

        # Sort stacks by first image time
        all_stacks.sort(key=lambda s: s.first_image_time or "")

    with timer.track("attach_result_files"):
        stacks_with_results = attach_result_files_to_stacks(
            all_stacks,
            excluded_result_infos,
            metadata_by_path=metadata_by_path,
            enable_heuristic_result_correlation=enable_heuristic_result_correlation,
        )
        matched_result_paths = {
            result["path"]
            for stack in stacks_with_results
            for result in stack.get("result_files", [])
        }
        unmatched_result_files = [
            result for result in excluded_result_infos
            if result["path"] not in matched_result_paths
        ]

    return {
        "directory": str(directory),
        "total_raw_files": len(raw_files),
        "excluded_result_files": excluded_result_files,
        "manual_mode_count": len(metadata_list),
        "stacks": stacks_with_results,
        "rejected_stacks": [asdict(s) for s in rejected_stacks],
        "unmatched_result_files": unmatched_result_files,
        "non_stack_files": non_stack_files,
        "_timing": timer.to_dict() if owns_timer else None,
    }


def find_directories_with_raw_files(root: Path,
                                    timer: Optional[StageTimer] = None) -> List[Path]:
    """Recursively find all directories containing raw files.

    Skips 'results' directories from older/generated-output workflows so they
    are not re-scanned as candidate source folders.
    """
    skip_names = {'results'}
    directories = []

    track = timer.track("directory_walk") if timer is not None else None
    if track is not None:
        track.__enter__()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skipped directory names
            dirnames[:] = [d for d in dirnames if d not in skip_names]

            # Check if this directory has any raw files
            has_raw = any(
                Path(f).suffix.lower() in RAW_EXTENSIONS
                for f in filenames
            )
            if has_raw:
                directories.append(Path(dirpath))
    finally:
        if track is not None:
            track.__exit__(None, None, None)

    return sorted(directories)


def list_directories_with_raw_files(root: Path) -> Dict:
    """Return a lightweight directory listing for progress reporting."""
    timer = StageTimer()
    directories = find_directories_with_raw_files(root, timer=timer)
    return {
        "root": str(root),
        "directories_scanned": len(directories),
        "directories": [str(directory) for directory in directories],
        "_timing": timer.to_dict(),
    }


def detect_stacks_recursive(root: Path,
                            max_gap_seconds: float = DEFAULTS['max_gap_seconds'],
                            min_sequence_length: int = DEFAULTS['min_sequence_length'],
                            manual_mode_only: bool = DEFAULTS['manual_mode_only'],
                            skip_focus_distance_check: bool = False,
                            exiftool_path: Optional[str] = None,
                            enable_heuristic_result_correlation: bool = ENABLE_HEURISTIC_RESULT_CORRELATION,
                            metadata: Optional[Dict[str, dict]] = None,
                            validate_focus_distance: bool = False) -> Dict:
    """Recursively detect focus stacks in all subdirectories.

    Returns a combined result with per-directory breakdowns.
    """
    timer = StageTimer()
    directories = find_directories_with_raw_files(root, timer=timer)

    if not directories:
        return {
            "root": str(root),
            "directories_scanned": 0,
            "total_raw_files": 0,
            "total_stacks": 0,
            "total_rejected": 0,
            "results": [],
            "_timing": timer.to_dict(),
        }

    results = []
    total_raw = 0
    total_stacks = 0
    total_rejected = 0

    for directory in directories:
        result = detect_stacks(
            directory,
            max_gap_seconds=max_gap_seconds,
            min_sequence_length=min_sequence_length,
            manual_mode_only=manual_mode_only,
            skip_focus_distance_check=skip_focus_distance_check,
            exiftool_path=exiftool_path,
            enable_heuristic_result_correlation=enable_heuristic_result_correlation,
            timer=timer,
            metadata=metadata,
            validate_focus_distance=validate_focus_distance,
        )
        # Strip nested per-call timing — we track aggregate via the shared timer.
        result.pop("_timing", None)
        results.append(result)
        total_raw += result.get("total_raw_files", 0)
        total_stacks += len(result.get("stacks", []))
        total_rejected += len(result.get("rejected_stacks", []))

    return {
        "root": str(root),
        "directories_scanned": len(directories),
        "total_raw_files": total_raw,
        "total_stacks": total_stacks,
        "total_rejected": total_rejected,
        "results": results,
        "_timing": timer.to_dict(),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Detect focus-bracketed image sequences and output JSON"
    )
    parser.add_argument("directory", type=Path, help="Directory containing raw image files")
    parser.add_argument("--max-gap", type=float, default=DEFAULTS['max_gap_seconds'],
                        help=f"Max time gap between images in seconds (default: {DEFAULTS['max_gap_seconds']})")
    parser.add_argument("--min-images", type=int, default=DEFAULTS['min_sequence_length'],
                        help=f"Min images to consider a stack (default: {DEFAULTS['min_sequence_length']})")
    parser.add_argument("--all-modes", action="store_true",
                        help="Include all exposure modes, not just manual")
    parser.add_argument("--no-focus-check", action="store_true",
                        help="Skip focus distance validation")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Only scan the specified directory, not subdirectories")
    parser.add_argument("--list-directories", action="store_true",
                        help="List directories containing raw files and exit")
    parser.add_argument("--json-out",
                        help="Write JSON result to this file instead of stdout")
    parser.add_argument("--exiftool-path",
                        help="Full path to the exiftool executable")
    parser.add_argument("--metadata-json",
                        help="Path to a JSON file containing pre-fetched metadata "
                             "(in exiftool -json format). When set, the bulk "
                             "exiftool call is skipped and this is used instead.")
    parser.add_argument("--validate-focus-distance", action="store_true",
                        help="After detection, run a small targeted exiftool call "
                             "to fetch ApproximateFocusDistance for files in candidate "
                             "stacks. Only relevant alongside --metadata-json.")
    parser.add_argument("--heuristic-result-correlation", dest="heuristic_result_correlation",
                        action="store_true",
                        help="Try to match renamed DNG/TIFF/PSD results back to detected source stacks")
    parser.add_argument("--no-heuristic-result-correlation", dest="heuristic_result_correlation",
                        action="store_false",
                        help="Disable heuristic matching of renamed DNG/TIFF/PSD results")
    parser.set_defaults(
        heuristic_result_correlation=ENABLE_HEURISTIC_RESULT_CORRELATION
    )

    args = parser.parse_args()

    script_start = time.perf_counter()

    if not args.directory.exists() or not args.directory.is_dir():
        error_payload = json.dumps(
            {"error": f"Directory not found: {args.directory}"},
            ensure_ascii=False
        )
        if args.json_out:
            args.json_out = Path(args.json_out)
            args.json_out.write_text(error_payload, encoding="utf-8")
        else:
            print(error_payload)
        sys.exit(1)

    if args.list_directories:
        result = list_directories_with_raw_files(args.directory)
    else:
        external_metadata = None
        if args.metadata_json:
            with open(args.metadata_json, "r", encoding="utf-8") as f:
                metadata_payload = json.load(f)
            # Accept both list-of-entries (exiftool format) and dict (path -> entry).
            if isinstance(metadata_payload, list):
                external_metadata = {
                    entry["SourceFile"]: entry
                    for entry in metadata_payload
                    if "SourceFile" in entry
                }
            elif isinstance(metadata_payload, dict):
                external_metadata = metadata_payload
            else:
                print(
                    f"Error: --metadata-json must be a list or dict, got "
                    f"{type(metadata_payload).__name__}",
                    file=sys.stderr,
                )
                sys.exit(1)

        common_args = dict(
            max_gap_seconds=args.max_gap,
            min_sequence_length=args.min_images,
            manual_mode_only=not args.all_modes,
            skip_focus_distance_check=args.no_focus_check,
            exiftool_path=args.exiftool_path,
            enable_heuristic_result_correlation=args.heuristic_result_correlation,
            metadata=external_metadata,
            validate_focus_distance=args.validate_focus_distance,
        )

        if args.no_recursive:
            result = detect_stacks(args.directory, **common_args)
        else:
            result = detect_stacks_recursive(args.directory, **common_args)

    if isinstance(result, dict):
        timing = result.get("_timing")
        if not isinstance(timing, dict):
            timing = {}
        dump_start = time.perf_counter()
        payload = json.dumps(result, indent=2, ensure_ascii=False)
        dump_seconds = time.perf_counter() - dump_start
        timing["json_dump"] = {"seconds": round(dump_seconds, 6), "count": 1}
        timing["script_total"] = {
            "seconds": round(time.perf_counter() - script_start, 6),
            "count": 1,
        }
        result["_timing"] = timing
        payload = json.dumps(result, indent=2, ensure_ascii=False)
    else:
        payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
