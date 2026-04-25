#!/usr/bin/env python3
"""Tests for detect_stacks.py focus stack detection logic."""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "FocusStackManager.lrplugin"))

from detect_stacks import (
    ImageMetadata,
    FocusStack,
    detect_stacks,
    find_raw_files,
    find_result_candidate_files,
    find_temporal_sequences,
    generate_stack_name,
    extract_metadata,
    batch_extract_metadata,
    exiftool_entry_to_metadata,
    parse_fraction,
    parse_exif_timestamp,
    has_varying_focus_distance,
    find_directories_with_raw_files,
    is_known_result_filename,
    is_likely_result_file,
    is_candidate_result_file,
    attach_result_files_to_stacks,
    list_directories_with_raw_files,
    merge_adjacent_stacks,
    detect_stacks_recursive,
)


def make_metadata(filename, timestamp, focal_length=100.0, iso=200,
                  aperture=11.0, shutter_speed="1/4",
                  exposure_time_seconds=0.25, exposure_mode="Manual",
                  directory="/fake/path"):
    """Helper to create ImageMetadata with sensible defaults."""
    return ImageMetadata(
        filepath=os.path.join(directory, filename),
        filename=filename,
        timestamp=timestamp,
        focal_length=focal_length,
        iso=iso,
        aperture=aperture,
        shutter_speed=shutter_speed,
        exposure_time_seconds=exposure_time_seconds,
        exposure_mode=exposure_mode,
    )


def make_stack_sequence(start_num, count, base_time=None, interval_seconds=0.5,
                        exposure_time=0.25, **kwargs):
    """Create a sequence of ImageMetadata simulating a focus stack."""
    if base_time is None:
        base_time = datetime(2026, 1, 15, 8, 30, 0)

    images = []
    for i in range(count):
        t = base_time + timedelta(seconds=i * (exposure_time + interval_seconds))
        filename = f"_ON_{start_num + i}.dng"
        images.append(make_metadata(
            filename=filename,
            timestamp=t,
            exposure_time_seconds=exposure_time,
            **kwargs,
        ))
    return images


def metadata_to_exiftool_entry(img, focus_distance=None):
    """Convert ImageMetadata to a fake exiftool JSON entry for mocking."""
    entry = {"SourceFile": img.filepath}
    if img.timestamp:
        entry["DateTimeOriginal"] = img.timestamp.strftime("%Y:%m:%d %H:%M:%S")
        if img.timestamp.microsecond:
            subsec = str(int(img.timestamp.microsecond / 10000)).zfill(2)
            entry["SubSecTimeOriginal"] = subsec
    if img.focal_length is not None:
        entry["FocalLength"] = img.focal_length
    if img.iso is not None:
        entry["ISO"] = img.iso
    if img.aperture is not None:
        entry["FNumber"] = img.aperture
    if img.shutter_speed is not None:
        entry["ExposureTime"] = img.shutter_speed
    if img.exposure_mode is not None:
        entry["ExposureMode"] = img.exposure_mode
    if focus_distance is not None:
        entry["ApproximateFocusDistance"] = focus_distance
    return entry


def make_exiftool_batch(images, focus_distances=None):
    """Build a mock return value for batch_extract_metadata."""
    result = {}
    for img in images:
        fd = focus_distances.get(img.filepath) if focus_distances else None
        result[img.filepath] = metadata_to_exiftool_entry(img, focus_distance=fd)
    return result


class TestParseFraction(TestCase):
    def test_simple_fraction(self):
        self.assertAlmostEqual(parse_fraction("1/4"), 0.25)

    def test_whole_number(self):
        self.assertAlmostEqual(parse_fraction("100"), 100.0)

    def test_decimal(self):
        self.assertAlmostEqual(parse_fraction("2.8"), 2.8)

    def test_zero_denominator(self):
        self.assertIsNone(parse_fraction("1/0"))

    def test_invalid(self):
        self.assertIsNone(parse_fraction("abc"))

    def test_none(self):
        self.assertIsNone(parse_fraction(None))


class TestParseExifTimestamp(TestCase):
    def test_valid(self):
        result = parse_exif_timestamp("2026:01:15 08:30:00")
        self.assertEqual(result, datetime(2026, 1, 15, 8, 30, 0))

    def test_with_subseconds(self):
        result = parse_exif_timestamp("2026:01:15 08:30:00", "44")
        self.assertEqual(result.microsecond, 440000)

    def test_invalid(self):
        self.assertIsNone(parse_exif_timestamp("not a date"))

    def test_none(self):
        self.assertIsNone(parse_exif_timestamp(None))


class TestImageMetadataSignature(TestCase):
    def test_same_settings_same_signature(self):
        a = make_metadata("a.dng", datetime.now(), focal_length=100, iso=200, aperture=11, shutter_speed="1/4")
        b = make_metadata("b.dng", datetime.now(), focal_length=100, iso=200, aperture=11, shutter_speed="1/4")
        self.assertEqual(a.get_signature(), b.get_signature())

    def test_different_iso_different_signature(self):
        a = make_metadata("a.dng", datetime.now(), iso=200)
        b = make_metadata("b.dng", datetime.now(), iso=400)
        self.assertNotEqual(a.get_signature(), b.get_signature())

    def test_different_aperture_different_signature(self):
        a = make_metadata("a.dng", datetime.now(), aperture=11.0)
        b = make_metadata("b.dng", datetime.now(), aperture=8.0)
        self.assertNotEqual(a.get_signature(), b.get_signature())


class TestFindTemporalSequences(TestCase):
    def test_basic_stack(self):
        images = make_stack_sequence(1000, 8, interval_seconds=0.3)
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)
        self.assertEqual(len(sequences[0]), 8)

    def test_two_separate_stacks(self):
        base = datetime(2026, 1, 15, 8, 30, 0)
        group1 = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3)
        group2 = make_stack_sequence(1006, 5, base_time=base + timedelta(seconds=30), interval_seconds=0.3)
        all_images = group1 + group2
        sequences = find_temporal_sequences(all_images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 2)

    def test_too_few_images(self):
        images = make_stack_sequence(1000, 3, interval_seconds=0.3)
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 0)

    def test_exact_min_length(self):
        images = make_stack_sequence(1000, 4, interval_seconds=0.3)
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)

    def test_gap_too_large(self):
        base = datetime(2026, 1, 15, 8, 30, 0)
        images = []
        for i in range(8):
            if i < 4:
                t = base + timedelta(seconds=i * 0.5)
            else:
                t = base + timedelta(seconds=10 + (i - 4) * 0.5)
            images.append(make_metadata(f"_ON_{1000+i}.dng", t, exposure_time_seconds=0.25))
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 2)

    def test_long_exposure_10s_real_world(self):
        """Reproduce the real-world bug: 10s exposures with ~1.07s gaps."""
        base = datetime(2026, 1, 15, 19, 27, 27, 440000)
        intervals = [0, 11.04, 11.08, 11.07, 11.08, 11.07, 11.08, 11.08, 11.07]
        cumulative = 0
        images = []
        for i, interval in enumerate(intervals):
            cumulative += interval
            t = base + timedelta(seconds=cumulative) if i > 0 else base
            images.append(make_metadata(
                f"_ON_{3150+i}.dng", t,
                focal_length=200.0, iso=800, aperture=14.0,
                shutter_speed="10", exposure_time_seconds=10.0,
            ))
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)
        self.assertEqual(len(sequences[0]), 9)

    def test_long_exposure_would_fail_with_tight_gap(self):
        base = datetime(2026, 1, 15, 19, 27, 27, 440000)
        intervals = [0, 11.04, 11.08, 11.07, 11.08, 11.07, 11.08, 11.08, 11.07]
        cumulative = 0
        images = []
        for i, interval in enumerate(intervals):
            cumulative += interval
            t = base + timedelta(seconds=cumulative) if i > 0 else base
            images.append(make_metadata(
                f"_ON_{3150+i}.dng", t,
                focal_length=200.0, iso=800, aperture=14.0,
                shutter_speed="10", exposure_time_seconds=10.0,
            ))
        sequences = find_temporal_sequences(images, max_gap_seconds=0.5, min_sequence_length=4, gap_exposure_scale=0.0)
        self.assertEqual(len(sequences), 0)

    def test_long_exposure_gap_scales_with_exposure_time(self):
        base = datetime(2026, 1, 15, 8, 0, 0)
        images = []
        for i in range(6):
            t = base + timedelta(seconds=i * 32.5)
            images.append(make_metadata(f"_ON_{1000+i}.dng", t, exposure_time_seconds=30.0, shutter_speed="30"))
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4, gap_exposure_scale=0.05)
        self.assertEqual(len(sequences), 1)
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4, gap_exposure_scale=0.0)
        self.assertEqual(len(sequences), 0)

    def test_short_exposure_gap_not_inflated(self):
        base = datetime(2026, 1, 15, 8, 0, 0)
        group1 = [make_metadata(f"_ON_{1000+i}.dng", base + timedelta(seconds=i * 0.304),
                                exposure_time_seconds=0.004, shutter_speed="1/250") for i in range(5)]
        group2 = [make_metadata(f"_ON_{1010+i}.dng", base + timedelta(seconds=10 + i * 0.304),
                                exposure_time_seconds=0.004, shutter_speed="1/250") for i in range(5)]
        sequences = find_temporal_sequences(group1 + group2, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 2)

    def test_long_exposures_accounted_for(self):
        base = datetime(2026, 1, 15, 8, 30, 0)
        images = [make_metadata(f"_ON_{1000+i}.dng", base + timedelta(seconds=i * 2.5),
                                exposure_time_seconds=2.0, shutter_speed="2") for i in range(6)]
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)

    def test_unsorted_input(self):
        images = list(reversed(make_stack_sequence(1000, 6, interval_seconds=0.3)))
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)

    def test_all_images_no_timestamps(self):
        """All images with None timestamps should return empty."""
        images = [make_metadata(f"_ON_{1000+i}.dng", None) for i in range(6)]
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 0)

    def test_exact_boundary_gap_included(self):
        """Gap exactly at max_gap should be included (<=, not <)."""
        base = datetime(2026, 1, 15, 8, 0, 0)
        # exposure=0.25s, interval=1.75s -> gap = 1.75 - 0.25 = 1.5s exactly
        images = [make_metadata(f"_ON_{1000+i}.dng", base + timedelta(seconds=i * 1.75),
                                exposure_time_seconds=0.25) for i in range(5)]
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)

    def test_zero_exposure_time(self):
        """Zero exposure time should not cause issues."""
        base = datetime(2026, 1, 15, 8, 0, 0)
        images = [make_metadata(f"_ON_{1000+i}.dng", base + timedelta(seconds=i * 0.5),
                                exposure_time_seconds=0.0) for i in range(5)]
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        self.assertEqual(len(sequences), 1)

    def test_images_without_timestamps_ignored(self):
        """Images with no timestamp should be silently skipped."""
        images = make_stack_sequence(1000, 8, interval_seconds=0.3)
        # Remove timestamps from frames 2-5, leaving a large gap
        # between frame 1 and frame 6 (~2.2s gap after exposure)
        for i in range(2, 6):
            images[i] = make_metadata(f"_ON_{1000+i}.dng", None)
        sequences = find_temporal_sequences(images, max_gap_seconds=1.5, min_sequence_length=4)
        # Only 4 frames have timestamps: 0, 1, 6, 7
        # Gap between 1 and 6 is too large, so no sequence of 4+
        self.assertEqual(len(sequences), 0)


class TestGenerateStackName(TestCase):
    def test_basic(self):
        images = [make_metadata("_ON_1234.dng", datetime.now()), make_metadata("_ON_1238.dng", datetime.now())]
        self.assertEqual(generate_stack_name(images), "_ON_1234-_ON_1238")

    def test_single_image(self):
        images = [make_metadata("_ON_5000.dng", datetime.now())]
        self.assertEqual(generate_stack_name(images), "_ON_5000-_ON_5000")


class TestHasVaryingFocusDistance(TestCase):
    def test_varying_distances(self):
        files = ["/a.dng", "/b.dng", "/c.dng", "/d.dng"]
        distances = {"/a.dng": 4.01, "/b.dng": 4.01, "/c.dng": 4.61, "/d.dng": 5.32}
        self.assertTrue(has_varying_focus_distance(files, distances))

    def test_constant_distances(self):
        files = ["/a.dng", "/b.dng", "/c.dng", "/d.dng"]
        distances = {"/a.dng": 4.01, "/b.dng": 4.01, "/c.dng": 4.01, "/d.dng": 4.01}
        self.assertFalse(has_varying_focus_distance(files, distances))

    def test_within_tolerance(self):
        files = ["/a.dng", "/b.dng", "/c.dng", "/d.dng"]
        distances = {"/a.dng": 4.01, "/b.dng": 4.01, "/c.dng": 4.015, "/d.dng": 4.01}
        self.assertFalse(has_varying_focus_distance(files, distances, tolerance=0.01))

    def test_just_beyond_tolerance(self):
        files = ["/a.dng", "/b.dng", "/c.dng", "/d.dng"]
        distances = {"/a.dng": 4.01, "/b.dng": 4.01, "/c.dng": 4.03, "/d.dng": 4.01}
        self.assertTrue(has_varying_focus_distance(files, distances, tolerance=0.01))

    def test_no_data_returns_none(self):
        self.assertIsNone(has_varying_focus_distance(["/a.dng", "/b.dng"], {}))

    def test_insufficient_data_returns_none(self):
        files = ["/a.dng", "/b.dng", "/c.dng", "/d.dng", "/e.dng", "/f.dng"]
        distances = {"/a.dng": 4.01, "/b.dng": 4.61}
        self.assertIsNone(has_varying_focus_distance(files, distances))

    def test_sufficient_partial_data(self):
        files = ["/a.dng", "/b.dng", "/c.dng", "/d.dng"]
        distances = {"/a.dng": 4.01, "/b.dng": 4.61, "/c.dng": 5.32}
        self.assertTrue(has_varying_focus_distance(files, distances))

    def test_real_world_focus_stack_distances(self):
        files = [f"/p/_ON_{i}.dng" for i in range(3150, 3159)]
        distances = {f"/p/_ON_{3150+i}.dng": d for i, d in enumerate(
            [4.01, 4.01, 4.01, 4.61, 4.61, 5.32, 5.32, 6.12, 6.12])}
        self.assertTrue(has_varying_focus_distance(files, distances))

    def test_wind_timing_constant_distance(self):
        files = [f"/p/img_{i}.dng" for i in range(6)]
        distances = {f"/p/img_{i}.dng": 10.5 for i in range(6)}
        self.assertFalse(has_varying_focus_distance(files, distances))


class TestFindRawFiles(TestCase):
    def test_finds_dng_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "photo1.dng").touch()
            (Path(tmpdir) / "photo2.dng").touch()
            (Path(tmpdir) / "photo3.jpg").touch()
            (Path(tmpdir) / "photo4.DNG").touch()
            (Path(tmpdir) / "photo5.cr3").touch()
            files = find_raw_files(Path(tmpdir))
            names = sorted([f.name for f in files])
            self.assertIn("photo1.dng", names)
            self.assertIn("photo2.dng", names)
            self.assertIn("photo4.DNG", names)
            self.assertIn("photo5.cr3", names)
            self.assertNotIn("photo3.jpg", names)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(len(find_raw_files(Path(tmpdir))), 0)


class TestFindResultCandidateFiles(TestCase):
    def test_finds_rendered_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "merged.dng").touch()
            (Path(tmpdir) / "merged.tif").touch()
            (Path(tmpdir) / "merged.TIFF").touch()
            (Path(tmpdir) / "merged.psd").touch()
            (Path(tmpdir) / "photo.jpg").touch()
            names = sorted(path.name for path in find_result_candidate_files(Path(tmpdir)))
            self.assertEqual(names, ["merged.TIFF", "merged.dng", "merged.psd", "merged.tif"])


class TestExposureModeFiltering(TestCase):
    """Test that exposure mode filtering uses exact match, not substring."""

    @patch('detect_stacks.batch_extract_metadata')
    def test_manual_mode_exact_match(self, mock_batch):
        """Only 'Manual' should match, not 'ManualSomething'."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3,
                                          exposure_mode="Manual", directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), manual_mode_only=True, skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_auto_bracket_manual_not_matched(self, mock_batch):
        """Mode like 'Auto Bracket (Manual)' should NOT match manual-only filter."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3,
                                          exposure_mode="Auto Bracket (Manual)", directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), manual_mode_only=True, skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 0)

    @patch('detect_stacks.batch_extract_metadata')
    def test_manual_with_whitespace(self, mock_batch):
        """'Manual ' (with trailing space) should still match."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3,
                                          exposure_mode="Manual ", directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), manual_mode_only=True, skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_none_exposure_mode_filtered(self, mock_batch):
        """Photos with None exposure mode should be filtered out in manual-only mode."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3,
                                          exposure_mode=None, directory=tmpdir)
            # Remove ExposureMode from the exiftool entries
            batch = make_exiftool_batch(images)
            for entry in batch.values():
                entry.pop("ExposureMode", None)
            mock_batch.return_value = batch
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), manual_mode_only=True, skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 0)


class TestExiftoolEntryToMetadata(TestCase):
    def test_basic_conversion(self):
        entry = {
            "SourceFile": "/path/test.dng",
            "DateTimeOriginal": "2026:01:15 08:30:00",
            "SubSecTimeOriginal": "44",
            "FocalLength": 200.0,
            "ISO": 800,
            "FNumber": 14.0,
            "ExposureTime": "1/4",
            "ExposureMode": "Manual",
            "ApproximateFocusDistance": 4.01,
        }
        meta = exiftool_entry_to_metadata("/path/test.dng", entry)
        self.assertEqual(meta.focal_length, 200.0)
        self.assertEqual(meta.iso, 800)
        self.assertEqual(meta.aperture, 14.0)
        self.assertAlmostEqual(meta.exposure_time_seconds, 0.25)
        self.assertEqual(meta.exposure_mode, "Manual")
        self.assertEqual(meta.timestamp.year, 2026)
        self.assertEqual(meta.timestamp.microsecond, 440000)

    def test_focal_length_with_mm_suffix(self):
        entry = {"SourceFile": "/p/t.dng", "FocalLength": "200.0 mm"}
        meta = exiftool_entry_to_metadata("/p/t.dng", entry)
        self.assertEqual(meta.focal_length, 200.0)

    def test_missing_fields(self):
        entry = {"SourceFile": "/p/t.dng"}
        meta = exiftool_entry_to_metadata("/p/t.dng", entry)
        self.assertIsNone(meta.timestamp)
        self.assertIsNone(meta.focal_length)
        self.assertIsNone(meta.iso)

    def test_datetime_with_embedded_subsec(self):
        """exiftool sometimes returns subsec embedded in the timestamp."""
        entry = {"SourceFile": "/p/t.dng", "DateTimeOriginal": "2026:01:15 08:30:00.44"}
        meta = exiftool_entry_to_metadata("/p/t.dng", entry)
        self.assertEqual(meta.timestamp.microsecond, 440000)


class TestDetectStacks(TestCase):
    """Integration tests for detect_stacks() using mocked exiftool data.

    These tests use skip_focus_distance_check=True to isolate the core
    detection logic. Focus distance validation is tested separately.
    """

    @patch('detect_stacks.batch_extract_metadata')
    def test_single_stack_detected(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 8, base_time=base, interval_seconds=0.3, directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(result["stacks"][0]["count"], 8)
        self.assertEqual(result["stacks"][0]["name"], "_ON_1000-_ON_1007")

    @patch('detect_stacks.batch_extract_metadata')
    def test_mixed_stacks_and_singles(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            stack_images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            singles = [
                make_metadata("_ON_1050.dng", base + timedelta(minutes=10), directory=tmpdir),
                make_metadata("_ON_1051.dng", base + timedelta(minutes=15), directory=tmpdir),
            ]
            all_images = stack_images + singles
            mock_batch.return_value = make_exiftool_batch(all_images)
            for img in all_images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(len(result["non_stack_files"]), 2)

    @patch('detect_stacks.batch_extract_metadata')
    def test_non_manual_mode_filtered(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 8, base_time=base, interval_seconds=0.3,
                                          exposure_mode="Auto", directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), manual_mode_only=True, skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 0)

    @patch('detect_stacks.batch_extract_metadata')
    def test_all_modes_includes_auto(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3,
                                          exposure_mode="Auto", directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), manual_mode_only=False, skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_different_settings_separate_stacks(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            stack_a = make_stack_sequence(1000, 5, base_time=base, interval_seconds=0.3,
                                          aperture=11.0, directory=tmpdir)
            stack_b = make_stack_sequence(1005, 5, base_time=base + timedelta(seconds=10),
                                          interval_seconds=0.3, aperture=8.0, directory=tmpdir)
            all_images = stack_a + stack_b
            mock_batch.return_value = make_exiftool_batch(all_images)
            for img in all_images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)
        self.assertEqual(len(result["stacks"]), 2)

    @patch('detect_stacks.batch_extract_metadata')
    def test_custom_min_sequence_length(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 5, base_time=base, interval_seconds=0.3, directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), min_sequence_length=6, skip_focus_distance_check=True)
            self.assertEqual(len(result["stacks"]), 0)
            result = detect_stacks(Path(tmpdir), min_sequence_length=5, skip_focus_distance_check=True)
            self.assertEqual(len(result["stacks"]), 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = detect_stacks(Path(tmpdir))
            self.assertEqual(result["total_raw_files"], 0)
            self.assertEqual(len(result["stacks"]), 0)

    @patch('detect_stacks.batch_extract_metadata')
    def test_output_structure(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 5, base_time=base, interval_seconds=0.3, directory=tmpdir)
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)
        self.assertIn("directory", result)
        self.assertIn("total_raw_files", result)
        self.assertIn("stacks", result)
        self.assertIn("rejected_stacks", result)
        self.assertIn("non_stack_files", result)
        stack = result["stacks"][0]
        for key in ["name", "count", "files", "focal_length", "iso", "aperture",
                     "shutter_speed", "time_span_seconds", "first_image_time"]:
            self.assertIn(key, stack)
        self.assertEqual(stack["count"], len(stack["files"]))


class TestDetectStacksFocusDistanceIntegration(TestCase):
    """Test that focus distance validation is wired into detect_stacks."""

    @patch('detect_stacks.batch_extract_metadata')
    def test_constant_focus_distance_rejects_stack(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            focus_distances = {img.filepath: 10.5 for img in images}
            mock_batch.return_value = make_exiftool_batch(images, focus_distances)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir))
        self.assertEqual(len(result["stacks"]), 0)
        self.assertEqual(len(result["rejected_stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_varying_focus_distance_keeps_stack(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            focus_distances = {images[i].filepath: 4.0 + i * 0.5 for i in range(6)}
            mock_batch.return_value = make_exiftool_batch(images, focus_distances)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir))
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(len(result["rejected_stacks"]), 0)

    @patch('detect_stacks.batch_extract_metadata')
    def test_missing_focus_data_keeps_stack(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            # No focus distance data at all
            mock_batch.return_value = make_exiftool_batch(images)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir))
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(len(result["rejected_stacks"]), 0)

    @patch('detect_stacks.batch_extract_metadata')
    def test_skip_focus_check_flag(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            focus_distances = {img.filepath: 10.5 for img in images}
            mock_batch.return_value = make_exiftool_batch(images, focus_distances)
            for img in images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)
        # Would be rejected by focus distance, but flag skips the check
        self.assertEqual(len(result["stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_mixed_stacks_some_rejected(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            real_stack = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            wind_shots = make_stack_sequence(1050, 5, base_time=base + timedelta(seconds=60),
                                             interval_seconds=0.3, directory=tmpdir)
            all_images = real_stack + wind_shots
            focus_distances = {}
            for i, img in enumerate(real_stack):
                focus_distances[img.filepath] = 4.0 + i * 0.5
            for img in wind_shots:
                focus_distances[img.filepath] = 10.5
            mock_batch.return_value = make_exiftool_batch(all_images, focus_distances)
            for img in all_images:
                Path(img.filepath).touch()
            result = detect_stacks(Path(tmpdir))
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(result["stacks"][0]["name"], "_ON_1000-_ON_1005")
        self.assertEqual(len(result["rejected_stacks"]), 1)
        self.assertEqual(result["rejected_stacks"][0]["name"], "_ON_1050-_ON_1054")


class TestMergeAdjacentStacks(TestCase):
    """Test merging of adjacent stacks with continuous focus distance progression."""

    def _make_stack(self, start_num, count, base_time, focus_start, focus_step,
                    shutter_speed="1/4", exposure_time_seconds=0.25):
        """Helper to create a FocusStack with associated focus distances."""
        files = [f"/p/_ON_{start_num + i}.dng" for i in range(count)]
        focus_distances = {}
        for i, fp in enumerate(files):
            focus_distances[fp] = focus_start + i * focus_step

        stack = FocusStack(
            name=f"_ON_{start_num}-_ON_{start_num + count - 1}",
            count=count,
            files=files,
            focal_length=100.0,
            iso=200,
            aperture=11.0,
            shutter_speed=shutter_speed,
            time_span_seconds=count * 0.5,
            first_image_time=base_time.strftime("%Y-%m-%d %H:%M:%S"),
            last_exposure_time_seconds=exposure_time_seconds,
        )
        return stack, focus_distances

    def test_merges_continuous_stacks(self):
        """Two long-exposure stacks with a short relative pause should merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(
            1000, 5, base, focus_start=2.0, focus_step=0.5,
            shutter_speed="10", exposure_time_seconds=10.0
        )
        # Stack B starts 30 seconds later, which is within 3x the 10s exposure.
        stack_b, fd_b = self._make_stack(1005, 5,
                                          base + timedelta(seconds=30),
                                          focus_start=4.5, focus_step=0.5,
                                          shutter_speed="10", exposure_time_seconds=10.0)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].count, 10)
        self.assertEqual(result[0].name, "_ON_1000-_ON_1009")

    def test_does_not_merge_large_time_gap(self):
        """Stacks separated by more than max_merge_gap should not merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(1000, 5, base, focus_start=2.0, focus_step=0.5)
        # 10 minutes later — too far apart
        stack_b, fd_b = self._make_stack(1005, 5,
                                          base + timedelta(minutes=10),
                                          focus_start=4.5, focus_step=0.5)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd,
                                        max_merge_gap_exposure_multiplier=120)
        self.assertEqual(len(result), 2)

    def test_does_not_merge_large_focus_gap(self):
        """Stacks with a large focus distance gap should not merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(1000, 5, base, focus_start=2.0, focus_step=0.5)
        # Focus jumps from 4.0 to 20.0 — clearly different subjects
        stack_b, fd_b = self._make_stack(1005, 5,
                                          base + timedelta(seconds=30),
                                          focus_start=20.0, focus_step=0.5)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd)
        self.assertEqual(len(result), 2)

    def test_merges_three_stacks(self):
        """Three long-exposure stacks with short relative pauses should merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(
            1000, 4, base, focus_start=2.0, focus_step=0.5,
            shutter_speed="10", exposure_time_seconds=10.0
        )
        stack_b, fd_b = self._make_stack(1004, 4,
                                          base + timedelta(seconds=20),
                                          focus_start=4.0, focus_step=0.5,
                                          shutter_speed="10", exposure_time_seconds=10.0)
        stack_c, fd_c = self._make_stack(1008, 4,
                                          base + timedelta(seconds=40),
                                          focus_start=6.0, focus_step=0.5,
                                          shutter_speed="10", exposure_time_seconds=10.0)
        all_fd = {**fd_a, **fd_b, **fd_c}

        result = merge_adjacent_stacks([stack_a, stack_b, stack_c], all_fd)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].count, 12)

    def test_partial_merge(self):
        """Only adjacent stacks that meet the exposure-scaled gap criteria should merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        # A and B are continuous
        stack_a, fd_a = self._make_stack(
            1000, 5, base, focus_start=2.0, focus_step=0.5,
            shutter_speed="10", exposure_time_seconds=10.0
        )
        stack_b, fd_b = self._make_stack(1005, 5,
                                          base + timedelta(seconds=20),
                                          focus_start=4.5, focus_step=0.5,
                                          shutter_speed="10", exposure_time_seconds=10.0)
        # C is a completely different stack (big time gap)
        stack_c, fd_c = self._make_stack(1050, 5,
                                          base + timedelta(minutes=10),
                                          focus_start=1.0, focus_step=0.3)
        all_fd = {**fd_a, **fd_b, **fd_c}

        result = merge_adjacent_stacks([stack_a, stack_b, stack_c], all_fd)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].count, 10)  # A+B merged
        self.assertEqual(result[1].count, 5)   # C separate

    def test_single_stack_unchanged(self):
        """A single stack should pass through unchanged."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack, fd = self._make_stack(1000, 5, base, focus_start=2.0, focus_step=0.5)

        result = merge_adjacent_stacks([stack], fd)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].count, 5)

    def test_empty_list(self):
        result = merge_adjacent_stacks([], {})
        self.assertEqual(len(result), 0)

    def test_no_focus_data_no_merge(self):
        """Without focus distance data, stacks should not be merged."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, _ = self._make_stack(1000, 5, base, focus_start=2.0, focus_step=0.5)
        stack_b, _ = self._make_stack(1005, 5,
                                       base + timedelta(seconds=20),
                                       focus_start=4.5, focus_step=0.5)

        # Pass empty focus distances
        result = merge_adjacent_stacks([stack_a, stack_b], {})
        self.assertEqual(len(result), 2)

    def test_same_focus_distance_no_merge(self):
        """Stacks at the same focus distance should not merge (not progressive)."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(1000, 5, base, focus_start=5.0, focus_step=0.0)
        stack_b, fd_b = self._make_stack(1005, 5,
                                          base + timedelta(seconds=20),
                                          focus_start=5.0, focus_step=0.0)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd)
        self.assertEqual(len(result), 2)

    def test_far_to_near_merge(self):
        """Far-to-near long-exposure stacks should also merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(
            1000, 5, base, focus_start=8.0, focus_step=-0.5,
            shutter_speed="10", exposure_time_seconds=10.0
        )
        stack_b, fd_b = self._make_stack(1005, 5,
                                          base + timedelta(seconds=20),
                                          focus_start=5.5, focus_step=-0.5,
                                          shutter_speed="10", exposure_time_seconds=10.0)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].count, 10)

    def test_does_not_merge_second_sweep_over_same_range(self):
        """A second close-to-far sweep over nearly the same range should not merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(1000, 9, base, focus_start=2.0, focus_step=0.5)
        stack_b, fd_b = self._make_stack(1009, 9,
                                          base + timedelta(seconds=20),
                                          focus_start=2.5, focus_step=0.5)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd)
        self.assertEqual(len(result), 2)

    def test_does_not_merge_when_second_stack_resets_near_start(self):
        """A later stack that resets backward before extending farther should not merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        stack_a, fd_a = self._make_stack(1308, 9, base, focus_start=3.01, focus_step=0.125)
        stack_b, fd_b = self._make_stack(1317, 9,
                                          base + timedelta(seconds=49),
                                          focus_start=3.01, focus_step=0.82)
        all_fd = {**fd_a, **fd_b}

        result = merge_adjacent_stacks([stack_a, stack_b], all_fd)
        self.assertEqual(len(result), 2)

    def test_does_not_merge_real_world_three_stack_case(self):
        """Real EXIF case: three 9-frame stacks with long pauses must stay separate."""
        def make_real_stack(start_num, subsec_list, focus_values, base_time):
            files = [f"/p/_ON_{start_num + i}.dng" for i in range(len(subsec_list))]
            fd = {}
            for fp, focus in zip(files, focus_values):
                fd[fp] = focus
            stack = FocusStack(
                name=f"_ON_{start_num}-_ON_{start_num + len(files) - 1}",
                count=len(files),
                files=files,
                focal_length=100.0,
                iso=200,
                aperture=11.0,
                shutter_speed="1/500",
                time_span_seconds=(subsec_list[-1] - subsec_list[0]) / 100.0,
                first_image_time=base_time.strftime("%Y-%m-%d %H:%M:%S"),
                last_exposure_time_seconds=0.002,
            )
            return stack, fd

        base = datetime(2026, 1, 15, 17, 16, 21)
        stack_a, fd_a = make_real_stack(
            1507,
            [0, 10, 20, 29, 39, 48, 58, 67, 77],
            [2.58, 2.58, 2.58, 3.01, 3.01, 3.01, 3.01, 3.01, 3.01],
            base,
        )
        stack_b, fd_b = make_real_stack(
            1516,
            [51, 61, 71, 80, 90, 99, 109, 118, 128],
            [3.01, 3.01, 3.01, 3.01, 3.01, 3.49, 3.49, 3.49, 3.49],
            datetime(2026, 1, 15, 17, 16, 38),
        )
        stack_c, fd_c = make_real_stack(
            1525,
            [92, 102, 112, 121, 131, 140, 150, 159, 169],
            [3.49, 3.49, 4.01, 4.01, 4.01, 4.01, 4.61, 4.61, 4.61],
            datetime(2026, 1, 15, 17, 17, 5),
        )
        all_fd = {**fd_a, **fd_b, **fd_c}

        result = merge_adjacent_stacks([stack_a, stack_b, stack_c], all_fd)
        self.assertEqual(len(result), 3)


class TestDetectStacksMergeIntegration(TestCase):
    """Test that merge is wired into detect_stacks correctly."""

    @patch('detect_stacks.batch_extract_metadata')
    def test_adjacent_stacks_merged_in_detection(self, mock_batch):
        """Two long-exposure temporal sequences with continuous focus should merge."""
        base = datetime(2026, 1, 15, 8, 30, 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Stack A: 5 frames
            imgs_a = make_stack_sequence(
                1000, 5, base_time=base, interval_seconds=0.3,
                exposure_time=10.0, shutter_speed="10", directory=tmpdir
            )
            # Start stack B after stack A has actually finished, but still within
            # the 3x-exposure merge window.
            imgs_b = make_stack_sequence(1005, 5,
                                          base_time=base + timedelta(seconds=60),
                                          interval_seconds=0.3,
                                          exposure_time=10.0, shutter_speed="10",
                                          directory=tmpdir)

            all_imgs = imgs_a + imgs_b
            # Progressive focus distances across both stacks
            focus_distances = {}
            for i, img in enumerate(all_imgs):
                focus_distances[img.filepath] = 2.0 + i * 0.5

            mock_batch.return_value = make_exiftool_batch(all_imgs, focus_distances)
            for img in all_imgs:
                Path(img.filepath).touch()

            result = detect_stacks(Path(tmpdir))

        # Should be 1 merged stack, not 2
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(result["stacks"][0]["count"], 10)

    @patch('detect_stacks.batch_extract_metadata')
    def test_non_adjacent_stacks_not_merged(self, mock_batch):
        """Two stacks with a large gap should remain separate."""
        base = datetime(2026, 1, 15, 8, 30, 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            imgs_a = make_stack_sequence(1000, 5, base_time=base, interval_seconds=0.3, directory=tmpdir)
            # 10 minute gap
            imgs_b = make_stack_sequence(1050, 5,
                                          base_time=base + timedelta(minutes=10),
                                          interval_seconds=0.3, directory=tmpdir)

            all_imgs = imgs_a + imgs_b
            focus_distances = {}
            for i, img in enumerate(imgs_a):
                focus_distances[img.filepath] = 2.0 + i * 0.5
            for i, img in enumerate(imgs_b):
                focus_distances[img.filepath] = 4.5 + i * 0.5

            mock_batch.return_value = make_exiftool_batch(all_imgs, focus_distances)
            for img in all_imgs:
                Path(img.filepath).touch()

            result = detect_stacks(Path(tmpdir))

        self.assertEqual(len(result["stacks"]), 2)


class TestFindDirectoriesWithRawFiles(TestCase):
    def test_finds_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a hierarchy with raw files at different levels
            d1 = Path(tmpdir) / "Location" / "2026" / "2026-01-15"
            d2 = Path(tmpdir) / "Location" / "2026" / "2026-01-16"
            d3 = Path(tmpdir) / "Location" / "2026"  # has no raw files directly
            d1.mkdir(parents=True)
            d2.mkdir(parents=True)

            (d1 / "photo1.dng").touch()
            (d2 / "photo2.dng").touch()

            dirs = find_directories_with_raw_files(Path(tmpdir))
            dir_strs = [str(d) for d in dirs]
            self.assertIn(str(d1), dir_strs)
            self.assertIn(str(d2), dir_strs)
            self.assertNotIn(str(d3), dir_strs)  # no raw files directly

    def test_skips_results_dirs_but_includes_source_files_children(self):
        """'results' should be skipped, but source stack folders under source_files should be scanned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            main_dir = Path(tmpdir) / "photos"
            source_dir = main_dir / "stacks" / "source_files" / "stack1"
            results_dir = main_dir / "stacks" / "results"
            main_dir.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            results_dir.mkdir(parents=True)

            (main_dir / "photo.dng").touch()
            (source_dir / "source.dng").touch()
            (results_dir / "result.dng").touch()

            dirs = find_directories_with_raw_files(Path(tmpdir))
            dir_strs = [str(d) for d in dirs]
            self.assertIn(str(main_dir), dir_strs)
            self.assertIn(str(source_dir), dir_strs)
            self.assertNotIn(str(results_dir), dir_strs)

    def test_does_not_skip_non_stacks_dir(self):
        """'non_stacks' directory should be scanned (not an artifact to skip)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            non_stacks = Path(tmpdir) / "non_stacks"
            non_stacks.mkdir()
            (non_stacks / "photo.dng").touch()

            dirs = find_directories_with_raw_files(Path(tmpdir))
            dir_strs = [str(d) for d in dirs]
            self.assertIn(str(non_stacks), dir_strs)

    def test_empty_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirs = find_directories_with_raw_files(Path(tmpdir))
            self.assertEqual(len(dirs), 0)

    def test_only_non_raw_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "photo.jpg").touch()
            (Path(tmpdir) / "document.txt").touch()
            dirs = find_directories_with_raw_files(Path(tmpdir))
            self.assertEqual(len(dirs), 0)

    def test_list_directories_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d1 = Path(tmpdir) / "plants"
            d2 = d1 / "wildflowers"
            d1.mkdir(parents=True)
            d2.mkdir(parents=True)
            (d1 / "root.dng").touch()
            (d2 / "lupine.dng").touch()

            result = list_directories_with_raw_files(Path(tmpdir))

        self.assertEqual(result["root"], tmpdir)
        self.assertEqual(result["directories_scanned"], 2)
        self.assertEqual(result["directories"], [str(d1), str(d2)])


class TestResultFileDetection(TestCase):
    def test_known_plugin_result_filename(self):
        self.assertTrue(is_known_result_filename(Path("_ON_1000-_ON_1009_10f_mB_s5_r11_stacked.dng")))

    def test_known_external_result_filename(self):
        self.assertTrue(is_known_result_filename(Path("_ON_4708 (6 files).dng")))

    def test_rendered_linear_raw_dng_is_result(self):
        entry = {
            "PhotometricInterpretation": "Linear Raw",
            "SamplesPerPixel": 3,
        }
        self.assertTrue(is_likely_result_file(Path("merged.dng"), entry))

    def test_helicon_preview_is_result(self):
        entry = {
            "PreviewApplicationName": "Helicon Focus",
        }
        self.assertTrue(is_likely_result_file(Path("merged.dng"), entry))

    def test_mosaic_raw_dng_is_not_result(self):
        entry = {
            "PhotometricInterpretation": "Color Filter Array",
            "SamplesPerPixel": 1,
        }
        self.assertFalse(is_likely_result_file(Path("source.dng"), entry))

    def test_rendered_tiff_is_result_candidate(self):
        self.assertTrue(is_candidate_result_file(Path("edit.tif"), {}))

    def test_plugin_result_filename_is_parsed_and_attached(self):
        stack = FocusStack(
            name="_ON_8495-_ON_8501",
            count=7,
            files=["/tmp/_ON_8495.dng", "/tmp/_ON_8501.dng"],
            focal_length=None,
            iso=None,
            aperture=None,
            shutter_speed=None,
            time_span_seconds=0.0,
            first_image_time=None,
            last_exposure_time_seconds=None,
        )
        from detect_stacks import parse_result_file_info, attach_result_files_to_stacks
        result_info = parse_result_file_info(Path("-_ON_8495-_ON_8501_7f_mB_s5_r11_stacked.dng"))
        attached = attach_result_files_to_stacks([stack], [result_info])
        self.assertEqual(len(attached[0]["result_files"]), 1)

    def test_unknown_result_file_is_not_attached_to_every_stack(self):
        stack_a = FocusStack(
            name="_ON_1000-_ON_1005",
            count=6,
            files=["/tmp/_ON_1000.dng", "/tmp/_ON_1005.dng"],
            focal_length=None,
            iso=None,
            aperture=None,
            shutter_speed=None,
            time_span_seconds=0.0,
            first_image_time=None,
            last_exposure_time_seconds=None,
        )
        stack_b = FocusStack(
            name="_ON_2000-_ON_2005",
            count=6,
            files=["/tmp/_ON_2000.dng", "/tmp/_ON_2005.dng"],
            focal_length=None,
            iso=None,
            aperture=None,
            shutter_speed=None,
            time_span_seconds=0.0,
            first_image_time=None,
            last_exposure_time_seconds=None,
        )
        from detect_stacks import attach_result_files_to_stacks
        unknown = {
            "path": "/tmp/merged.dng",
            "filename": "merged.dng",
            "kind": "unknown",
            "first_stem": None,
            "last_stem": None,
            "count": None,
            "method": None,
            "smoothing": None,
            "radius": None,
        }
        attached = attach_result_files_to_stacks([stack_a, stack_b], [unknown])
        self.assertEqual(attached[0]["result_files"], [])
        self.assertEqual(attached[1]["result_files"], [])


class TestDetectStacksRecursive(TestCase):
    @patch('detect_stacks.batch_extract_metadata')
    def test_recursive_finds_stacks_in_subdirs(self, mock_batch):
        """Recursive detection should find stacks in nested directories."""
        base = datetime(2026, 1, 15, 8, 30, 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two subdirectories with stacks
            d1 = Path(tmpdir) / "location" / "2026-01-15"
            d2 = Path(tmpdir) / "location" / "2026-01-16"
            d1.mkdir(parents=True)
            d2.mkdir(parents=True)

            imgs1 = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=str(d1))
            imgs2 = make_stack_sequence(2000, 5,
                                         base_time=base + timedelta(hours=24),
                                         interval_seconds=0.3, directory=str(d2))

            # Create the files and set up mock
            all_entries = {}
            for img in imgs1 + imgs2:
                Path(img.filepath).touch()
                entry = metadata_to_exiftool_entry(img, focus_distance=4.0 + float(hash(img.filepath) % 10) / 10)
                all_entries[img.filepath] = entry

            mock_batch.return_value = all_entries

            # But batch_extract_metadata is called per-directory, so we need
            # to return the right subset each time
            def batch_side_effect(file_paths, **_kwargs):
                return {fp: all_entries[fp] for fp in file_paths if fp in all_entries}

            mock_batch.side_effect = batch_side_effect

            result = detect_stacks_recursive(Path(tmpdir), skip_focus_distance_check=True)

        self.assertEqual(result["directories_scanned"], 2)
        self.assertEqual(result["total_stacks"], 2)
        self.assertEqual(len(result["results"]), 2)

    def test_recursive_empty_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = detect_stacks_recursive(Path(tmpdir))
            self.assertEqual(result["directories_scanned"], 0)


class TestDetectStacksExcludesResultFiles(TestCase):
    @patch('detect_stacks.batch_extract_metadata')
    def test_excludes_rendered_result_dng_from_candidates(self, mock_batch):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "_ON_4708.dng"
            merged = Path(tmpdir) / "_ON_4708 (6 files).dng"
            source.touch()
            merged.touch()

            source_img = make_metadata("_ON_4708.dng", datetime(2026, 1, 15, 8, 30, 0), directory=tmpdir)
            source_entry = metadata_to_exiftool_entry(source_img, focus_distance=1.0)
            source_entry["PhotometricInterpretation"] = "Color Filter Array"
            source_entry["SamplesPerPixel"] = 1

            merged_entry = {
                "SourceFile": str(merged),
                "PhotometricInterpretation": "Linear Raw",
                "SamplesPerPixel": 3,
            }

            mock_batch.return_value = {
                str(source): source_entry,
                str(merged): merged_entry,
            }

            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)

        self.assertEqual(result["total_raw_files"], 1)
        self.assertEqual(result["excluded_result_files"], [str(merged)])
        self.assertEqual(result["non_stack_files"], [str(source)])
        self.assertEqual(len(result["stacks"]), 0)

    @patch('detect_stacks.batch_extract_metadata')
    def test_recursive_returns_per_directory_results(self, mock_batch):
        """Each subdirectory should have its own entry in results."""
        base = datetime(2026, 1, 15, 8, 30, 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            d1 = Path(tmpdir) / "day1"
            d2 = Path(tmpdir) / "day2"
            d1.mkdir()
            d2.mkdir()

            # Stack in d1 only
            imgs = make_stack_sequence(1000, 5, base_time=base, interval_seconds=0.3, directory=str(d1))
            for img in imgs:
                Path(img.filepath).touch()

            # Non-stack files in d2
            for i in range(3):
                (d2 / f"single_{i}.dng").touch()

            def batch_side_effect(file_paths, **_kwargs):
                result = {}
                for fp in file_paths:
                    for img in imgs:
                        if img.filepath == fp:
                            result[fp] = metadata_to_exiftool_entry(img)
                            break
                    else:
                        # Non-stack files — return minimal entry
                        result[fp] = {
                            "SourceFile": fp,
                            "DateTimeOriginal": "2026:01:15 12:00:00",
                            "FocalLength": 50.0,
                            "ISO": 100,
                            "FNumber": 8.0,
                            "ExposureTime": "1/125",
                            "ExposureMode": "Manual",
                        }
                return result

            mock_batch.side_effect = batch_side_effect

            result = detect_stacks_recursive(Path(tmpdir), skip_focus_distance_check=True)

        self.assertEqual(result["directories_scanned"], 2)
        # d1 has a stack, d2 does not
        dir_results = {r["directory"]: r for r in result["results"]}
        self.assertEqual(len(dir_results[str(d1)]["stacks"]), 1)
        self.assertEqual(len(dir_results[str(d2)]["stacks"]), 0)


class TestDetectStacksNonDngRaw(TestCase):
    """Regression tests: non-DNG raws (CR3/NEF/CR2/etc.) must be detected.

    These extensions only appear in find_raw_files (not find_result_candidate_files),
    so the classification step has to iterate the union of both, not just result
    candidates. A prior version dropped them and returned zero stacks.
    """

    @patch('detect_stacks.batch_extract_metadata')
    def test_cr3_files_are_classified_as_sources(self, mock_batch):
        base = datetime(2023, 11, 6, 10, 46, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            files = [Path(tmpdir) / f"_ON_{7765 + i}.CR3" for i in range(6)]
            for f in files:
                f.touch()

            images = []
            for i, f in enumerate(files):
                images.append(make_metadata(
                    filename=f.name,
                    timestamp=base + timedelta(seconds=i * 0.6),
                    exposure_time_seconds=0.0666,
                    shutter_speed="1/15",
                    focal_length=300.0,
                    iso=400,
                    aperture=13.0,
                    directory=tmpdir,
                ))
            mock_batch.return_value = make_exiftool_batch(images)

            result = detect_stacks(Path(tmpdir), skip_focus_distance_check=True)

        self.assertEqual(result["total_raw_files"], 6)
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(result["stacks"][0]["count"], 6)

    @patch('detect_stacks.batch_extract_metadata')
    def test_cr3_stack_kept_when_focus_distance_missing(self, mock_batch):
        """R5 CR3 has no ApproximateFocusDistance; stacks must still survive validation."""
        base = datetime(2023, 11, 6, 10, 46, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            files = [Path(tmpdir) / f"_ON_{7765 + i}.CR3" for i in range(6)]
            for f in files:
                f.touch()

            images = []
            for i, f in enumerate(files):
                images.append(make_metadata(
                    filename=f.name,
                    timestamp=base + timedelta(seconds=i * 0.6),
                    exposure_time_seconds=0.0666,
                    shutter_speed="1/15",
                    directory=tmpdir,
                ))
            # No focus_distances passed -> tag missing for all frames.
            mock_batch.return_value = make_exiftool_batch(images)

            # skip_focus_distance_check=False (the default in production).
            result = detect_stacks(Path(tmpdir))

        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(len(result["rejected_stacks"]), 0)


class TestDetectStacksExternalMetadata(TestCase):
    """When metadata is provided externally (Lua-side provider), no bulk exiftool."""

    @patch('detect_stacks.batch_extract_metadata')
    def test_external_metadata_skips_bulk_exiftool(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            for img in images:
                Path(img.filepath).touch()

            external = make_exiftool_batch(images)

            result = detect_stacks(
                Path(tmpdir),
                skip_focus_distance_check=True,
                metadata=external,
            )

        # Bulk exiftool path must not have been invoked.
        mock_batch.assert_not_called()
        self.assertEqual(len(result["stacks"]), 1)
        self.assertEqual(result["stacks"][0]["count"], 6)

    @patch('detect_stacks.batch_extract_metadata')
    def test_validate_focus_distance_runs_targeted_call(self, mock_batch):
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            for img in images:
                Path(img.filepath).touch()

            # Catalog metadata: no focus distance.
            external = make_exiftool_batch(images)

            # Targeted exiftool call returns varying focus distances per file —
            # confirms the validation pass uses this data.
            distances = {
                img.filepath: 1.0 + 0.5 * i
                for i, img in enumerate(images)
            }
            mock_batch.return_value = {
                fp: {"SourceFile": fp, "ApproximateFocusDistance": d}
                for fp, d in distances.items()
            }

            result = detect_stacks(
                Path(tmpdir),
                metadata=external,
                validate_focus_distance=True,
            )

        # The bulk call was not invoked, but the targeted focus-distance call was.
        self.assertEqual(mock_batch.call_count, 1)
        call_kwargs = mock_batch.call_args.kwargs
        self.assertEqual(call_kwargs.get("tags"), ["-ApproximateFocusDistance"])
        self.assertEqual(len(result["stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_no_focus_distance_pass_without_flag(self, mock_batch):
        """validate_focus_distance=False (default) skips the targeted call entirely."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(1000, 6, base_time=base, interval_seconds=0.3, directory=tmpdir)
            for img in images:
                Path(img.filepath).touch()

            external = make_exiftool_batch(images)
            result = detect_stacks(Path(tmpdir), metadata=external)

        mock_batch.assert_not_called()
        self.assertEqual(len(result["stacks"]), 1)

    @patch('detect_stacks.batch_extract_metadata')
    def test_manual_exposure_program_string_is_accepted(self, mock_batch):
        """LR's exposureProgram returns 'Manual exposure'; must still pass the manual filter."""
        base = datetime(2026, 1, 15, 8, 30, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            images = make_stack_sequence(
                1000, 6, base_time=base, interval_seconds=0.3,
                exposure_mode="Manual exposure", directory=tmpdir,
            )
            for img in images:
                Path(img.filepath).touch()

            external = make_exiftool_batch(images)
            result = detect_stacks(
                Path(tmpdir),
                manual_mode_only=True,
                skip_focus_distance_check=True,
                metadata=external,
            )

        mock_batch.assert_not_called()
        self.assertEqual(len(result["stacks"]), 1)


class TestCLIOutput(TestCase):
    def test_empty_dir_produces_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import subprocess
            script = str(Path(__file__).parent.parent / "FocusStackManager.lrplugin" / "detect_stacks.py")
            result = subprocess.run(
                [sys.executable, script, tmpdir],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["total_raw_files"], 0)

    def test_nonexistent_dir_returns_error(self):
        import subprocess
        script = str(Path(__file__).parent.parent / "FocusStackManager.lrplugin" / "detect_stacks.py")
        result = subprocess.run(
            [sys.executable, script, "/nonexistent/path"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIn("error", data)

    def test_json_out_writes_utf8_payload_to_file(self):
        with tempfile.TemporaryDirectory(prefix="cafe_") as tmpdir:
            import subprocess
            script = str(Path(__file__).parent.parent / "FocusStackManager.lrplugin" / "detect_stacks.py")
            json_out = Path(tmpdir) / "résultat.json"
            result = subprocess.run(
                [sys.executable, script, tmpdir, "--json-out", str(json_out)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            data = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(data["total_raw_files"], 0)


class TestExiftoolPathOverride(TestCase):
    @patch('detect_stacks.subprocess.run')
    def test_batch_extract_metadata_uses_explicit_exiftool_path(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            exiftool = Path(tmpdir) / "exiftool"
            exiftool.write_text("")
            source = Path(tmpdir) / "photo.dng"
            source.write_text("")

            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"SourceFile": str(source)}]),
                stderr="",
            )

            batch_extract_metadata([str(source)], exiftool_path=str(exiftool))

            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], str(exiftool))

    @patch('detect_stacks.subprocess.run')
    def test_batch_extract_metadata_rejects_missing_explicit_path(self, mock_run):
        with self.assertRaises(SystemExit):
            batch_extract_metadata(
                ["/tmp/photo.dng"],
                exiftool_path="/nonexistent/exiftool"
            )
        mock_run.assert_not_called()

    @patch('detect_stacks.COMMON_EXIFTOOL_PATHS', ("/opt/homebrew/bin/exiftool",))
    @patch('detect_stacks.Path.exists')
    @patch('detect_stacks.shutil.which')
    @patch('detect_stacks.subprocess.run')
    def test_batch_extract_metadata_falls_back_to_homebrew_path(self, mock_run, mock_which, mock_exists):
        mock_which.return_value = None
        mock_exists.return_value = True
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"SourceFile": "/tmp/photo.dng"}]),
            stderr="",
        )

        batch_extract_metadata(["/tmp/photo.dng"])

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "/opt/homebrew/bin/exiftool")


class TestHeuristicResultCorrelation(TestCase):
    @patch('detect_stacks.batch_extract_metadata')
    def test_matches_renamed_dng_and_tiff_to_single_stack(self, mock_batch):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_images = make_stack_sequence(
                2946, 7,
                base_time=datetime(2021, 12, 21, 17, 5, 1, 930000),
                interval_seconds=0.08,
                exposure_time=0.05,
                focal_length=167.0,
                iso=100,
                aperture=16.0,
                shutter_speed="1/13",
                directory=tmpdir,
            )
            for img in source_images:
                Path(img.filepath).touch()

            merged_dng = Path(tmpdir) / "2022-02-27 11-19-53 (A,Radius8,Smoothing4).dng"
            merged_tif = Path(tmpdir) / "2022-02-27 11-19-53 (A,Radius8,Smoothing4)-Edit.tif"
            merged_dng.touch()
            merged_tif.touch()

            batch = make_exiftool_batch(source_images, {
                img.filepath: 4.0 + (idx * 0.2)
                for idx, img in enumerate(source_images)
            })
            batch[str(merged_dng)] = {
                "SourceFile": str(merged_dng),
                "DateTimeOriginal": "2021:12:21 17:05:02",
                "SubSecTimeOriginal": "57",
                "FocalLength": "167.0 mm",
                "ISO": 100,
                "FNumber": 16.0,
                "ExposureTime": "1/13",
                "ExposureMode": "Manual",
                "PreviewApplicationName": "Helicon Focus",
                "PhotometricInterpretation": "Linear Raw",
                "SamplesPerPixel": 3,
            }
            batch[str(merged_tif)] = {
                "SourceFile": str(merged_tif),
                "DateTimeOriginal": "2021:12:21 17:05:02",
                "SubSecTimeOriginal": "57",
                "FocalLength": "167.0 mm",
                "ISO": 100,
                "FNumber": 16.0,
                "ExposureTime": "1/13",
                "ExposureMode": "Manual",
                "Software": "Adobe Photoshop Lightroom Classic",
            }
            mock_batch.return_value = batch

            result = detect_stacks(
                Path(tmpdir),
                skip_focus_distance_check=True,
                enable_heuristic_result_correlation=True,
            )

        self.assertEqual(len(result["stacks"]), 1)
        attached = sorted(Path(item["path"]).name for item in result["stacks"][0]["result_files"])
        self.assertEqual(attached, [
            "2022-02-27 11-19-53 (A,Radius8,Smoothing4)-Edit.tif",
            "2022-02-27 11-19-53 (A,Radius8,Smoothing4).dng",
        ])
        self.assertEqual(result["unmatched_result_files"], [])

    def test_requires_unique_match(self):
        stack_a = FocusStack(
            name="_ON_1000-_ON_1006",
            count=7,
            files=[f"/tmp/_ON_{1000+i}.dng" for i in range(7)],
            focal_length=167.0,
            iso=100,
            aperture=16.0,
            shutter_speed="1/13",
            time_span_seconds=0.0,
            first_image_time="2021-12-21 17:05:01",
            last_exposure_time_seconds=0.08,
        )
        stack_b = FocusStack(
            name="_ON_2000-_ON_2006",
            count=7,
            files=[f"/tmp/_ON_{2000+i}.dng" for i in range(7)],
            focal_length=167.0,
            iso=100,
            aperture=16.0,
            shutter_speed="1/13",
            time_span_seconds=0.0,
            first_image_time="2021-12-21 17:05:01",
            last_exposure_time_seconds=0.08,
        )
        metadata_by_path = {}
        for filepath in stack_a.files + stack_b.files:
            metadata_by_path[filepath] = ImageMetadata(
                filepath=filepath,
                filename=Path(filepath).name,
                timestamp=datetime(2021, 12, 21, 17, 5, 2),
                focal_length=167.0,
                iso=100,
                aperture=16.0,
                shutter_speed="1/13",
                exposure_time_seconds=0.08,
                exposure_mode="Manual",
            )

        result_info = {
            "path": "/tmp/ambiguous-edit.tif",
            "filename": "ambiguous-edit.tif",
            "kind": "unknown",
            "first_stem": None,
            "last_stem": None,
            "count": None,
            "method": None,
            "smoothing": None,
            "radius": None,
            "timestamp": datetime(2021, 12, 21, 17, 5, 2),
            "focal_length": 167.0,
            "iso": 100,
            "aperture": 16.0,
            "shutter_speed": "1/13",
            "extension": ".tif",
            "preview_application_name": "",
            "software": "",
            "creator_tool": "",
        }

        attached = attach_result_files_to_stacks(
            [stack_a, stack_b],
            [result_info],
            metadata_by_path=metadata_by_path,
            enable_heuristic_result_correlation=True,
        )

        self.assertEqual(attached[0]["result_files"], [])
        self.assertEqual(attached[1]["result_files"], [])


if __name__ == '__main__':
    main()
