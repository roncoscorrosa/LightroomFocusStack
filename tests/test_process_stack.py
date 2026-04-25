#!/usr/bin/env python3
"""Tests for process_stack.py focus stack processing logic."""

import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "FocusStackManager.lrplugin"))

from process_stack import (
    generate_result_filename,
    create_symlink_dir,
    cleanup_symlink_dir,
    get_helicon_focus_path,
    process_stack,
)


class TestGenerateResultFilename(TestCase):
    def test_default_params(self):
        files = ["/path/_ON_1234.dng", "/path/_ON_1248.dng"]
        result = generate_result_filename(files, method=1, smoothing=5, radius=11)
        self.assertEqual(result, "-_ON_1234-_ON_1248_2f_mB_s5_r11_stacked")

    def test_different_params(self):
        files = ["/path/_ON_1000.dng", "/path/_ON_1010.dng"]
        result = generate_result_filename(files, method=2, smoothing=3, radius=8)
        self.assertEqual(result, "-_ON_1000-_ON_1010_2f_mC_s3_r8_stacked")

    def test_method_a(self):
        files = ["/path/IMG_001.cr3", "/path/IMG_005.cr3"]
        result = generate_result_filename(files, method=0, smoothing=5, radius=11)
        self.assertEqual(result, "-IMG_001-IMG_005_2f_mA_s5_r11_stacked")

    def test_single_file(self):
        files = ["/path/_ON_5000.dng"]
        result = generate_result_filename(files, method=1, smoothing=5, radius=11)
        self.assertEqual(result, "-_ON_5000-_ON_5000_1f_mB_s5_r11_stacked")

    def test_sorts_before_source_files(self):
        """Result filename should sort before source filenames."""
        files = ["/path/_ON_1234.dng", "/path/_ON_1248.dng"]
        result_name = generate_result_filename(files, method=1, smoothing=5, radius=11) + ".dng"
        source_name = "_ON_1234.dng"
        self.assertLess(result_name, source_name)

    def test_different_frame_count_different_name(self):
        """Different number of source files should produce different filenames."""
        files_5 = [f"/path/_ON_{1000+i}.dng" for i in range(5)]
        files_3 = [f"/path/_ON_{1000+i}.dng" for i in range(3)]
        r5 = generate_result_filename(files_5, method=1, smoothing=5, radius=11)
        r3 = generate_result_filename(files_3, method=1, smoothing=5, radius=11)
        self.assertNotEqual(r5, r3)
        self.assertIn("5f", r5)
        self.assertIn("3f", r3)

    def test_params_encoded_deterministically(self):
        """Same inputs always produce same output."""
        files = ["/path/a.dng", "/path/z.dng"]
        r1 = generate_result_filename(files, method=1, smoothing=5, radius=11)
        r2 = generate_result_filename(files, method=1, smoothing=5, radius=11)
        self.assertEqual(r1, r2)

    def test_different_params_produce_different_names(self):
        """Different Helicon params produce different filenames."""
        files = ["/path/a.dng", "/path/z.dng"]
        r1 = generate_result_filename(files, method=1, smoothing=5, radius=11)
        r2 = generate_result_filename(files, method=2, smoothing=5, radius=11)
        r3 = generate_result_filename(files, method=1, smoothing=3, radius=11)
        r4 = generate_result_filename(files, method=1, smoothing=5, radius=8)
        self.assertNotEqual(r1, r2)
        self.assertNotEqual(r1, r3)
        self.assertNotEqual(r1, r4)


class TestSymlinkDir(TestCase):
    def test_creates_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create source files
            files = []
            for i in range(5):
                f = Path(tmpdir) / f"_ON_{1000+i}.dng"
                f.write_text("fake image data")
                files.append(str(f))

            symdir = create_symlink_dir(files)
            try:
                # Verify symlinks exist
                symlinks = sorted(os.listdir(symdir))
                self.assertEqual(len(symlinks), 5)
                for name in symlinks:
                    link = Path(symdir) / name
                    self.assertTrue(link.is_symlink())
                    self.assertTrue(link.resolve().exists())
            finally:
                cleanup_symlink_dir(symdir)

            # Verify cleanup worked
            self.assertFalse(symdir.exists())

    def test_cleanup_nonexistent_dir(self):
        """Cleaning up a nonexistent dir should not raise."""
        cleanup_symlink_dir(Path("/nonexistent/path"))

    def test_symlinks_point_to_originals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "original.dng"
            source.write_text("original content")

            symdir = create_symlink_dir([str(source)])
            try:
                link = Path(symdir) / "original.dng"
                self.assertEqual(link.read_text(), "original content")
                # Verify it's actually a symlink, not a copy
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), source.resolve())
            finally:
                cleanup_symlink_dir(symdir)


class TestProcessStack(TestCase):
    def test_missing_source_files(self):
        result = process_stack(
            files=["/nonexistent/file.dng"],
            output_dir="/tmp/output",
        )
        self.assertFalse(result["success"])
        self.assertIn("Missing source files", result["error"])

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fake source files
            files = []
            for i in range(5):
                f = Path(tmpdir) / f"_ON_{1000+i}.dng"
                f.write_text("fake")
                files.append(str(f))

            output_dir = Path(tmpdir) / "results"

            result = process_stack(
                files=files,
                output_dir=str(output_dir),
                dry_run=True,
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["dry_run"])
            self.assertIn("-_ON_1000-_ON_1004_5f_mB_s5_r11_stacked", result["result_file"])
            self.assertTrue(result["result_file"].endswith(".dng"))
            self.assertEqual(result["stack_name"], "_ON_1000-_ON_1004")

    def test_output_is_always_dng_even_for_raw_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files = []
            for i in range(2):
                f = Path(tmpdir) / f"IMG_00{i+1}.cr3"
                f.write_text("fake")
                files.append(str(f))

            result = process_stack(
                files=files,
                output_dir=str(Path(tmpdir) / "results"),
                dry_run=True,
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["result_file"].endswith(".dng"))
            self.assertNotIn(".cr3", result["result_file"].lower())

    def test_existing_result_skipped(self):
        """If result already exists on disk, skip processing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = []
            for i in range(5):
                f = Path(tmpdir) / f"_ON_{1000+i}.dng"
                f.write_text("fake")
                files.append(str(f))

            output_dir = Path(tmpdir) / "results"
            output_dir.mkdir()

            # Pre-create the expected result file
            result_file = output_dir / "-_ON_1000-_ON_1004_5f_mB_s5_r11_stacked.dng"
            result_file.write_text("already done")

            result = process_stack(
                files=files,
                output_dir=str(output_dir),
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["already_existed"])

    def test_different_params_not_skipped(self):
        """Existing result with different params should not cause skip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = []
            for i in range(5):
                f = Path(tmpdir) / f"_ON_{1000+i}.dng"
                f.write_text("fake")
                files.append(str(f))

            output_dir = Path(tmpdir) / "results"
            output_dir.mkdir()

            # Pre-create result with method 1
            old_result = output_dir / "-_ON_1000-_ON_1004_5f_mB_s5_r11_stacked.dng"
            old_result.write_text("old result")

            # Process with method 2 — should NOT skip (different params)
            result = process_stack(
                files=files,
                output_dir=str(output_dir),
                method=2,
                dry_run=True,  # Use dry_run since we don't have Helicon
            )

            self.assertTrue(result["success"])
            self.assertIn("mC_s5_r11", result["result_file"])

    @patch('process_stack.get_helicon_focus_path')
    def test_helicon_not_found(self, mock_hf):
        """Should return error if Helicon Focus is not installed."""
        mock_hf.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "photo.dng"
            f.write_text("fake")

            result = process_stack(
                files=[str(f)],
                output_dir=str(Path(tmpdir) / "results"),
            )

            self.assertFalse(result["success"])
            self.assertIn("not found", result["error"])

    @patch('process_stack.subprocess.run')
    @patch('process_stack.get_helicon_focus_path')
    def test_helicon_success(self, mock_hf, mock_run):
        """Simulate successful Helicon Focus run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = []
            for i in range(5):
                f = Path(tmpdir) / f"_ON_{1000+i}.dng"
                f.write_text("fake")
                files.append(str(f))

            output_dir = Path(tmpdir) / "results"
            expected_result = output_dir / "-_ON_1000-_ON_1004_5f_mB_s5_r11_stacked.dng"

            mock_hf.return_value = Path("/mock/HeliconFocus")

            def fake_helicon(*args, **kwargs):
                # Simulate Helicon creating the output file
                output_dir.mkdir(parents=True, exist_ok=True)
                expected_result.write_text("stacked result")
                return MagicMock(returncode=0, stderr="")

            mock_run.side_effect = fake_helicon

            result = process_stack(
                files=files,
                output_dir=str(output_dir),
            )

            self.assertTrue(result["success"])
            self.assertFalse(result.get("already_existed", False))
            self.assertEqual(result["result_file"], str(expected_result))
            run_args = mock_run.call_args[0][0]
            self.assertEqual(run_args[1], "-silent")
            self.assertNotEqual(run_args[2], "-i")
            self.assertIn("helicon_stack_", run_args[2])

    @patch('process_stack.subprocess.run')
    @patch('process_stack.get_helicon_focus_path')
    def test_helicon_failure(self, mock_hf, mock_run):
        """Helicon Focus returns non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "_ON_1000.dng"
            f.write_text("fake")

            output_dir = Path(tmpdir) / "results"

            mock_hf.return_value = Path("/mock/HeliconFocus")
            mock_run.return_value = MagicMock(returncode=1, stdout="Processing output", stderr="")

            result = process_stack(
                files=[str(f)],
                output_dir=str(output_dir),
            )

            self.assertFalse(result["success"])
            self.assertIn("failed", result["error"].lower())
            self.assertIn("Processing output", result["error"])

    @patch('process_stack.subprocess.run')
    @patch('process_stack.get_helicon_focus_path')
    def test_helicon_failure_prefers_stdout_error_line(self, mock_hf, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "_ON_1000.dng"
            f.write_text("fake")

            output_dir = Path(tmpdir) / "results"

            mock_hf.return_value = Path("/mock/HeliconFocus")
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="info line\nRendering failed (silent mode): unknown exception!\n",
                stderr="*** GPU Warning ***\n",
            )

            result = process_stack(
                files=[str(f)],
                output_dir=str(output_dir),
            )

            self.assertFalse(result["success"])
            self.assertIn("unknown exception", result["error"])

    @patch('process_stack.subprocess.run')
    @patch('process_stack.get_helicon_focus_path')
    def test_symlink_dir_cleaned_up_on_success(self, mock_hf, mock_run):
        """Temp symlink dir should be cleaned up even on success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "_ON_1000.dng"
            f.write_text("fake")
            output_dir = Path(tmpdir) / "results"

            mock_hf.return_value = Path("/mock/HeliconFocus")

            created_symlink_dirs = []
            original_create = create_symlink_dir

            def tracking_create(files):
                d = original_create(files)
                created_symlink_dirs.append(d)
                return d

            with patch('process_stack.create_symlink_dir', side_effect=tracking_create):
                def fake_helicon(*args, **kwargs):
                    output_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "-_ON_1000-_ON_1000_1f_mB_s5_r11_stacked.dng").write_text("done")
                    return MagicMock(returncode=0, stderr="")

                mock_run.side_effect = fake_helicon

                process_stack(files=[str(f)], output_dir=str(output_dir))

            for d in created_symlink_dirs:
                self.assertFalse(d.exists(), f"Symlink dir was not cleaned up: {d}")


class TestGenerateResultFilenameEdgeCases(TestCase):
    def test_empty_file_list_raises(self):
        """Empty file list should raise ValueError."""
        with self.assertRaises(ValueError):
            generate_result_filename([], method=1, smoothing=5, radius=11)

    def test_files_with_spaces_in_path(self):
        """Paths with spaces should work."""
        files = ["/path/with spaces/_ON_1000.dng", "/path/with spaces/_ON_1005.dng"]
        result = generate_result_filename(files, method=1, smoothing=5, radius=11)
        self.assertIn("_ON_1000", result)
        self.assertIn("_ON_1005", result)
        self.assertIn("2f", result)

    def test_files_with_multiple_dots(self):
        """Filenames with multiple dots use the last extension only for stem."""
        files = ["/path/file.backup.dng"]
        result = generate_result_filename(files, method=1, smoothing=5, radius=11)
        # Path.stem of "file.backup.dng" is "file.backup"
        self.assertIn("file.backup", result)


class TestPartialStack(TestCase):
    """Test that processing works with fewer files than originally detected."""

    def test_dry_run_with_subset(self):
        """Processing a subset of a detected stack should work fine."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create 15 files (original detected stack)
            all_files = []
            for i in range(15):
                f = Path(tmpdir) / f"_ON_{1234+i}.dng"
                f.write_text("fake")
                all_files.append(str(f))

            # User removed last 4 files from collection — process only first 11
            subset = all_files[:11]

            result = process_stack(
                files=subset,
                output_dir=str(Path(tmpdir) / "results"),
                dry_run=True,
            )

            self.assertTrue(result["success"])
            # Result name should reflect actual first-last, not original detection
            self.assertEqual(result["stack_name"], "_ON_1234-_ON_1244")
            self.assertIn("-_ON_1234-_ON_1244_11f_mB_s5_r11_stacked", result["result_file"])


class TestCLI(TestCase):
    def test_missing_files_arg(self):
        import subprocess
        script = str(Path(__file__).parent.parent / "FocusStackManager.lrplugin" / "process_stack.py")
        result = subprocess.run(
            [sys.executable, script, "--output-dir", "/tmp"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_dry_run_cli(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.dng"
            f.write_text("fake")

            script = str(Path(__file__).parent.parent / "FocusStackManager.lrplugin" / "process_stack.py")
            result = subprocess.run(
                [sys.executable, script,
                 "--files", str(f),
                 "--output-dir", str(Path(tmpdir) / "results"),
                 "--dry-run"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertTrue(data["success"])
            self.assertTrue(data["dry_run"])

    def test_json_out_cli(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.dng"
            f.write_text("fake")
            json_out = Path(tmpdir) / "result.json"

            script = str(Path(__file__).parent.parent / "FocusStackManager.lrplugin" / "process_stack.py")
            result = subprocess.run(
                [sys.executable, script,
                 "--files", str(f),
                 "--output-dir", str(Path(tmpdir) / "results"),
                 "--dry-run",
                 "--json-out", str(json_out)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            data = json.loads(json_out.read_text())
            self.assertTrue(data["success"])


if __name__ == '__main__':
    main()
