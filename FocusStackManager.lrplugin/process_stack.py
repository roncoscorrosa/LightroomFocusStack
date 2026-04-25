#!/usr/bin/env python3
"""
Focus Stack Processor — processes a single focus stack via Helicon Focus.

Designed to be called by the Lightroom FocusStackManager plugin, one stack
at a time. Takes a JSON file list on stdin or via --files argument, runs
Helicon Focus, and outputs the result path.

Does NOT move source files. Source files stay in their original location.
A temp directory of symlinks is created for Helicon Focus input.

Result filename encodes Helicon parameters:
    <first>-<last>_m<method-letter>_s<smoothing>_r<radius>_stacked.dng

Usage:
    # Process a single stack from a file list
    python process_stack.py --files file1.dng file2.dng ... --output-dir /path/to/results

    # Process from JSON on stdin (from detect_stacks.py output)
    echo '{"files": [...]}' | python process_stack.py --stdin --output-dir /path/to/results

    # With custom Helicon parameters
    python process_stack.py --files *.dng --output-dir ./results --method 2 --radius 8 --smoothing 3

    # Dry run
    python process_stack.py --files *.dng --output-dir ./results --dry-run
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional


DEFAULTS = {
    'helicon_method': 1,    # Method B (depth map)
    'helicon_radius': 11,
    'helicon_smoothing': 5,
}
RESULT_EXTENSION = ".dng"
METHOD_LABELS = {
    0: "A",
    1: "B",
    2: "C",
}


def resolve_helicon_executable(path: Path) -> Optional[Path]:
    """Resolve a Helicon Focus executable from either a binary path or .app bundle."""
    if path.suffix.lower() == ".app":
        candidates = [
            path / "Contents/MacOS/HeliconFocus",
            path / "Contents/MacOS/Helicon Focus",
            path / "Contents/MacOS/Focus",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    if path.exists():
        return path

    return None


def get_helicon_focus_path() -> Optional[Path]:
    """Find Helicon Focus executable path based on platform."""
    system = platform.system()

    if system == "Darwin":
        paths = [
            Path("/Applications/HeliconFocus.app/Contents/MacOS/HeliconFocus"),
            Path("/Applications/Helicon Focus.app/Contents/MacOS/Helicon Focus"),
            Path.home() / "Applications/HeliconFocus.app/Contents/MacOS/HeliconFocus",
            Path.home() / "Applications/Helicon Focus.app/Contents/MacOS/Helicon Focus",
            Path("/Applications/HeliconFocus.app/Contents/MacOS/Focus"),
            Path.home() / "Applications/HeliconFocus.app/Contents/MacOS/Focus",
        ]
    elif system == "Windows":
        paths = [
            Path("C:/Program Files/Helicon Focus/HeliconFocus.exe"),
            Path("C:/Program Files (x86)/Helicon Focus/HeliconFocus.exe"),
        ]
    else:
        paths = [
            Path("/usr/bin/heliconfocus"),
            Path("/usr/local/bin/heliconfocus"),
        ]

    for path in paths:
        if path.exists():
            return path

    return None


def generate_result_filename(files: List[str],
                             method: int, smoothing: int, radius: int) -> str:
    """Generate result filename encoding the Helicon parameters and frame count.

    Prefixed with '-' so the result sorts before source files in directory
    listings (ASCII '-' = 45 sorts before '_' = 95).

    The frame count is encoded so that reprocessing with fewer source files
    (e.g., after rejecting blurry frames) produces a different filename.
    """
    if not files:
        raise ValueError("Cannot generate result filename from empty file list")
    first_stem = Path(files[0]).stem
    last_stem = Path(files[-1]).stem
    count = len(files)
    method_label = METHOD_LABELS.get(method, str(method))
    return f"-{first_stem}-{last_stem}_{count}f_m{method_label}_s{smoothing}_r{radius}_stacked"


def create_symlink_dir(files: List[str]) -> Path:
    """Create a temp directory with symlinks to source files.

    Helicon Focus expects a folder of images. Rather than moving files,
    we create symlinks so the originals stay in place.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="helicon_stack_"))
    for filepath in files:
        src = Path(filepath)
        dst = tmpdir / src.name
        os.symlink(src.resolve(), dst)
    return tmpdir


def cleanup_symlink_dir(tmpdir: Path):
    """Remove the temp symlink directory."""
    if not tmpdir.exists():
        return
    for f in tmpdir.iterdir():
        f.unlink()
    tmpdir.rmdir()


def last_non_empty_line(text: str) -> str:
    """Return the last non-empty line from console output."""
    last = ""
    for line in text.splitlines():
        if line.strip():
            last = line.strip()
    return last


def summarize_helicon_output(stdout: str, stderr: str) -> str:
    """Pick the most useful one-line failure summary from Helicon output."""
    stdout_last = last_non_empty_line(stdout)
    stderr_last = last_non_empty_line(stderr)

    for candidate in (stdout_last, stderr_last):
        lowered = candidate.lower()
        if "failed" in lowered or "error" in lowered or "exception" in lowered:
            return candidate

    return stderr_last or stdout_last or "no console output"


def process_stack(files: List[str],
                  output_dir: str,
                  method: int = DEFAULTS['helicon_method'],
                  radius: int = DEFAULTS['helicon_radius'],
                  smoothing: int = DEFAULTS['helicon_smoothing'],
                  helicon_path: Optional[str] = None,
                  timeout: int = 600,
                  dry_run: bool = False) -> dict:
    """Process a single focus stack through Helicon Focus.

    Returns a dict with result info suitable for JSON output.
    """
    # Validate files exist
    missing = [f for f in files if not Path(f).exists()]
    if missing:
        return {"error": f"Missing source files: {missing}", "success": False}

    # Determine output file. The plugin contract assumes DNG output.
    result_name = generate_result_filename(files, method, smoothing, radius)
    output_path = Path(output_dir) / f"{result_name}{RESULT_EXTENSION}"

    # Check if result already exists (before looking for Helicon — we don't need it)
    if output_path.exists():
        return {
            "success": True,
            "result_file": str(output_path),
            "already_existed": True,
            "stack_name": f"{Path(files[0]).stem}-{Path(files[-1]).stem}",
        }

    # Find Helicon Focus
    if helicon_path:
        hf_path = resolve_helicon_executable(Path(helicon_path))
        if not hf_path:
            return {"error": f"Helicon Focus not found at: {helicon_path}", "success": False}
    else:
        hf_path = get_helicon_focus_path()
        if not hf_path:
            return {"error": "Helicon Focus not found", "success": False}

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "result_file": str(output_path),
            "command": f"{hf_path} -silent <symlink_dir> -save:{output_path} -mp:{method} -rp:{radius} -sp:{smoothing}",
            "stack_name": f"{Path(files[0]).stem}-{Path(files[-1]).stem}",
        }

    # Create output directory
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Helicon Focus v8 expects a stack-only directory. Use symlinks so the
    # originals stay in place while the input folder contains only this stack.
    symlink_dir = create_symlink_dir(files)

    try:
        cmd = [
            str(hf_path),
            "-silent",
            str(symlink_dir),
            f"-save:{output_path}",
            f"-mp:{method}",
            f"-rp:{radius}",
            f"-sp:{smoothing}",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode == 0 and output_path.exists():
            return {
                "success": True,
                "result_file": str(output_path),
                "already_existed": False,
                "stack_name": f"{Path(files[0]).stem}-{Path(files[-1]).stem}",
                "command": " ".join(cmd),
            }
        else:
            details = summarize_helicon_output(stdout, stderr)
            return {
                "success": False,
                "error": f"Helicon Focus failed (exit code {result.returncode}): {details}",
                "stack_name": f"{Path(files[0]).stem}-{Path(files[-1]).stem}",
                "command": " ".join(cmd),
                "stdout": stdout,
                "stderr": stderr,
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"Helicon Focus timed out (>{timeout}s)",
            "stack_name": f"{Path(files[0]).stem}-{Path(files[-1]).stem}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "stack_name": f"{Path(files[0]).stem}-{Path(files[-1]).stem}",
        }
    finally:
        cleanup_symlink_dir(symlink_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Process a single focus stack through Helicon Focus"
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--files", nargs="+", help="Source file paths")
    input_group.add_argument("--stdin", action="store_true",
                             help="Read JSON with 'files' array from stdin")

    parser.add_argument("--output-dir", required=True,
                        help="Directory for the result file")
    parser.add_argument("--method", type=int, choices=[0, 1, 2],
                        default=DEFAULTS['helicon_method'],
                        help=f"Helicon method: 0=A, 1=B (depth map), 2=C (default: {DEFAULTS['helicon_method']})")
    parser.add_argument("--radius", type=int, default=DEFAULTS['helicon_radius'],
                        help=f"Helicon radius (default: {DEFAULTS['helicon_radius']})")
    parser.add_argument("--smoothing", type=int, default=DEFAULTS['helicon_smoothing'],
                        help=f"Helicon smoothing (default: {DEFAULTS['helicon_smoothing']})")
    parser.add_argument("--helicon-path", help="Path to Helicon Focus executable")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Helicon Focus timeout in seconds (default: 600)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without processing")
    parser.add_argument("--json-out", help="Write JSON result to this file instead of stdout")

    args = parser.parse_args()

    if args.stdin:
        data = json.load(sys.stdin)
        files = data.get("files", [])
    else:
        files = args.files

    if not files:
        print(json.dumps({"error": "No files provided", "success": False}))
        sys.exit(1)

    result = process_stack(
        files=files,
        output_dir=args.output_dir,
        method=args.method,
        radius=args.radius,
        smoothing=args.smoothing,
        helicon_path=args.helicon_path,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    if not result.get("success", False):
        sys.exit(1)


if __name__ == "__main__":
    main()
