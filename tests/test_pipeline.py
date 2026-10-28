import csv
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import subprocess

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fa_pipeline as fa


def jpeg(color=(40, 100, 140)):
    out = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(out, "JPEG")
    return out.getvalue()


def archive(root, patient="Patient 55", name="2021.8.13.zip", entries=None):
    target = root / patient / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as z:
        for member, payload in entries or [("123.jpg", jpeg())]:
            z.writestr(member, payload)
    return target


def csv_rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def fake_fa(key, eye="OD", timer=None):
    return {"image_key": key, "modality": "FA", "eye": eye, "timer_s": timer,
            "id_gap": 1, "flags": set()}


class RulesTests(unittest.TestCase):
    def test_date_variants_and_invalid_dates(self):
        cases = {"2024.10.8.zip": "2024-10-08", "2024-11.12.zip": "2024-11-12",
                 "12.3.2024.zip": "2024-12-03", "12.3.2024 note.zip": "2024-12-03",
                 "12.3.1999.zip": "1999-12-03",
                 "12.3.20245.zip": "", "12.32.2024.zip": "",
                 "5.4.21.zip": "2021-05-04", "3.29.22 I changed the date.zip": "2022-03-29",
                 "2024.6.4 (2).zip": "2024-06-04", "2024.2.31.zip": "", "2024.2.311.zip": ""}
        for value, expected in cases.items():
            self.assertEqual(fa.parse_date(value)[0], expected)

    def test_phase_boundaries_include_exact_endpoints(self):
        s = fa.Settings()
        expected = [(0, "pre-dye"), (4.999, "pre-dye"), (5, "early"), (179.999, "early"),
                    (180, "mid"), (479.999, "mid"), (480, "late")]
        for time, phase in expected:
            self.assertEqual(fa.phase_from_timer(time, s), phase)

    def test_timer_parser_rejects_invalid_seconds_and_duration(self):
        self.assertEqual(fa.parse_timer("00:28:434", fa.Settings()), 28.434)
        for text in ["00:60:000", "99:00:000", "not a timer", "00:10:12", "00:12:1234"]:
            self.assertIsNone(fa.parse_timer(text, fa.Settings()))

    def test_invalid_settings_fail(self):
        for changes in [{"early_end_seconds": 4}, {"coverage_flag_threshold": 2},
                        {"disc_x_min": .6}, {"max_timer_seconds": float("nan")},
                        {"reserve_disk_bytes": -1}, {"near_black_mean": True}]:
            with self.assertRaises(fa.PipelineError):
                dataclasses.replace(fa.Settings(), **changes).validate()

    def visit(self, patient=13, date="2021-05-04"):
        return fa.Visit(Path("unused.zip"), "Patient 13/5.4.21.zip", patient, f"Patient {patient}", date, date, "", [])

    def test_mixed_clock_visit_falls_back_per_frame(self):
        rows = [fake_fa(str(i), eye, 28.434 if i == 0 else None)
                for i, eye in enumerate(["OD", "OS", "OD", "OS", "OD", "OS"])]
        fa.assign_phases(rows, self.visit(), fa.Settings(), {})
        self.assertEqual(rows[0]["phase_source"], "timer")
        self.assertEqual([r["phase"] for r in rows], ["early", "early", "mid", "mid", "late", "late"])
        self.assertTrue(rows[1]["phase_estimated"])

    def test_reimport_does_not_invent_order_based_phases(self):
        visit = self.visit()
        visit.flags.add("suspected_reimport")
        rows = [fake_fa(str(i), eye) for i, eye in enumerate(["OD", "OS"] * 3)]
        fa.assign_phases(rows, visit, fa.Settings(), {})
        self.assertTrue(all(r["phase"] == "unstaged" for r in rows))

    def test_too_few_eye_runs_stay_unstaged(self):
        rows = [fake_fa("a"), fake_fa("b")]
        fa.assign_phases(rows, self.visit(), fa.Settings(), {})
        self.assertTrue(all(r["phase"] == "unstaged" for r in rows))

    def test_timer_regression_prevents_missing_clock_inference(self):
        rows = [fake_fa(str(i), eye, t) for i, (eye, t) in
                enumerate(zip(["OD", "OS"] * 3, [300, 10, None, None, None, None]))]
        fa.assign_phases(rows, self.visit(), fa.Settings(), {})
        self.assertEqual(rows[0]["phase"], "mid")
        self.assertEqual(rows[2]["phase"], "unstaged")
        self.assertFalse(rows[0]["order_reliable"])

    def test_clock_identifies_early_fa_despite_dark_vessel_polarity(self):
        with patch.object(fa, "read_timer", return_value=(17.102, "00:17:102", "read")):
            row, _ = fa.analyze_image(jpeg((90, 90, 90)), fa.Settings(), "tesseract")
        self.assertEqual(row["modality"], "FA")
        self.assertEqual(row["modality_source"], "timer-overlay")

    def test_ambiguous_clock_does_not_turn_dark_fa_into_pre_dye(self):
        with patch.object(fa, "read_timer", return_value=(None, "00:50:931 | 00:50:951", "ambiguous")):
            row, _ = fa.analyze_image(jpeg((2, 2, 2)), fa.Settings(), "tesseract")
        self.assertEqual(row["modality"], "FA")
        self.assertTrue(row["near_black"])

    def test_dark_frame_inside_fa_sequence_is_not_assumed_pre_dye(self):
        rows = [dict(fake_fa("a"), modality_source="heuristic"),
                dict(fake_fa("b"), modality="FA-dark", modality_source="heuristic")]
        fa.resolve_dark_frames(rows)
        self.assertEqual(rows[1]["modality"], "FA")
        self.assertIn("dark_frame_after_fa", rows[1]["flags"])

    def test_reviewed_timer_overrides_darkness_phase(self):
        visit = self.visit()
        visit.output_dir = "Patient 13/2021-05-04"
        row = dict(fake_fa("a", timer=600.123), modality="FA-dark", timer_status="manual", seq=1, image_id=123)
        fa.assign_phases([row], visit, fa.Settings(), {})
        self.assertEqual(row["phase"], "late")
        self.assertIn("/6_FA_late/", fa.output_relative(row, visit, 1))

    def test_ocr_disagreement_is_not_accepted_as_measured_time(self):
        import cv2
        import numpy as np
        im = np.zeros((1000, 1000, 3), dtype=np.uint8)
        im[920:970, 650:900] = 255
        _, encoded = cv2.imencode(".jpg", im)
        results = [subprocess.CompletedProcess([], 0, b"11:34:138", b""),
                   subprocess.CompletedProcess([], 0, b"11:54:138", b"")]
        with patch.object(fa.subprocess, "run", side_effect=results):
            value, raw, status = fa.read_timer(encoded, im, fa.Settings(), "tesseract")
        self.assertIsNone(value)
        self.assertEqual(status, "ambiguous")
        self.assertIn("11:54:138", raw)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "raw"
        self.source.mkdir()
        self.output = self.base / "revised"
        self.corrections = self.base / "corrections"
        self.settings = dataclasses.replace(fa.Settings(), reserve_disk_bytes=0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, **kwargs):
        return fa.run_pipeline(self.source, self.output, self.corrections, self.settings,
                               sheets=False, allow_no_ocr=True, **kwargs)

    def test_full_output_preserves_payload_and_applies_confirmed_corrections(self):
        payload = jpeg()
        original = archive(self.source, entries=[("123.jpg", payload), ("124.jpg", payload)])
        zip_hash = fa.hash_file(original)
        summary = self.run_pipeline()
        rows = csv_rows(self.output / "fa_images.csv")
        self.assertEqual(summary["images"], 2)
        self.assertEqual(rows[0]["date_status"], "suspect")
        self.assertEqual(len(list(self.output.glob("Patient */**/*.jpg"))), 2)
        self.assertEqual((self.output / rows[0]["output_relpath"]).read_bytes(), payload)
        dates = csv_rows(self.corrections / "date_corrections.csv")
        dates[0]["confirmed"] = "yes"
        fa.write_csv(self.corrections / "date_corrections.csv", fa.DATE_COLS, dates)
        eyes = csv_rows(self.corrections / "eye_corrections.csv")
        eyes[0]["confirmed"] = "OS"
        fa.write_csv(self.corrections / "eye_corrections.csv", fa.EYE_COLS, eyes)
        old_paths = {r["output_relpath"] for r in rows}
        self.run_pipeline()
        rows = csv_rows(self.output / "fa_images.csv")
        self.assertTrue(all(r["visit_date"] == "2021-08-31" for r in rows))
        self.assertTrue(all(not (self.output / p).exists() for p in old_paths))
        confirmed = next(r for r in rows if r["image_key"] == eyes[0]["image_key"])
        self.assertEqual(confirmed["eye_source"], "manual")
        self.assertIn("/OS/", confirmed["output_relpath"])
        self.assertEqual(fa.hash_file(original), zip_hash)
        self.assertFalse(list(self.base.glob(".revised.building-*")))

    def test_failed_run_retains_completed_output_and_corrections(self):
        archive(self.source)
        self.run_pipeline()
        before = (self.output / "fa_images.csv").read_bytes()
        queue = (self.corrections / "eye_corrections.csv").read_bytes()
        with patch.object(fa, "analyze_image", side_effect=fa.PipelineError("simulated failure")):
            with self.assertRaises(fa.PipelineError):
                self.run_pipeline()
        self.assertEqual((self.output / "fa_images.csv").read_bytes(), before)
        self.assertEqual((self.corrections / "eye_corrections.csv").read_bytes(), queue)
        self.assertFalse(list(self.base.glob(".revised.building-*")))
        self.assertFalse(self.output.with_name("revised.pipeline.lock").exists())

    def test_duplicate_member_names_and_same_day_archives_keep_every_frame(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive(self.source, name="2024.6.4.zip", entries=[("123.jpg", jpeg()), ("123.jpg", jpeg((20, 50, 120)))])
        archive(self.source, name="2024.6.4 (2).zip")
        self.run_pipeline()
        rows = csv_rows(self.output / "fa_images.csv")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["image_key"] for r in rows}), 3)
        self.assertEqual(len({r["output_relpath"] for r in rows}), 3)
        self.assertTrue(all("__v" in r["output_relpath"] for r in rows))

    def test_undecodable_jpeg_payload_is_kept(self):
        payload = b"this is not a valid JPEG"
        archive(self.source, entries=[("123.jpg", payload)])
        self.run_pipeline()
        row = csv_rows(self.output / "fa_images.csv")[0]
        self.assertEqual(row["modality"], "unknown")
        self.assertIn("decode_error", row["flags"])
        self.assertEqual((self.output / row["output_relpath"]).read_bytes(), payload)

    def test_tables_only_has_rows_without_image_copies(self):
        archive(self.source)
        summary = self.run_pipeline(tables_only=True)
        self.assertFalse(summary["images_materialized"])
        self.assertEqual(csv_rows(self.output / "fa_images.csv")[0]["images_materialized"], "false")
        self.assertFalse(list(self.output.glob("Patient */**/*.jpg")))

    def test_source_and_output_overlap_is_rejected(self):
        with self.assertRaises(fa.PipelineError):
            fa.validate_paths(self.source, self.source / "output", self.corrections)

    def test_unowned_output_is_never_replaced(self):
        archive(self.source)
        self.output.mkdir()
        valuable = self.output / "human.txt"
        valuable.write_text("keep")
        with self.assertRaises(fa.PipelineError):
            self.run_pipeline()
        self.assertEqual(valuable.read_text(), "keep")

    def test_unsafe_zip_member_is_rejected(self):
        archive(self.source, entries=[("../123.jpg", jpeg())])
        with self.assertRaises(fa.PipelineError):
            self.run_pipeline()
        self.assertFalse((self.base / "123.jpg").exists())

    def test_unparsed_date_can_be_reviewed_without_guessing(self):
        archive(self.source, name="unknown.zip")
        self.run_pipeline()
        row = csv_rows(self.output / "fa_images.csv")[0]
        self.assertEqual(row["visit_date"], "")
        self.assertIn("date_unknown", row["output_relpath"])
        self.assertEqual(csv_rows(self.corrections / "date_corrections.csv")[0]["suggested_date"], "")

    def test_stale_confirmed_date_is_rejected_after_source_changes(self):
        archive(self.source)
        self.run_pipeline()
        dates = csv_rows(self.corrections / "date_corrections.csv")
        dates[0]["confirmed"] = "yes"
        fa.write_csv(self.corrections / "date_corrections.csv", fa.DATE_COLS, dates)
        archive(self.source, entries=[("123.jpg", jpeg((10, 90, 100)))])
        with self.assertRaisesRegex(fa.PipelineError, "fingerprint is stale"):
            self.run_pipeline()

    def test_missing_tesseract_requires_explicit_opt_in(self):
        archive(self.source)
        with patch.object(fa.shutil, "which", return_value=None):
            with self.assertRaisesRegex(fa.PipelineError, "Tesseract is missing"):
                fa.run_pipeline(self.source, self.output, self.corrections, self.settings, sheets=False)

    def test_human_edits_during_run_are_not_overwritten(self):
        archive(self.source)
        self.run_pipeline()
        prior_table = (self.output / "fa_images.csv").read_bytes()
        original_analyze = fa.analyze_image
        def edit_during_analysis(*args):
            result = original_analyze(*args)
            path = self.corrections / "date_corrections.csv"
            rows = csv_rows(path)
            rows[0]["confirmed"] = "2021-08-31"
            fa.write_csv(path, fa.DATE_COLS, rows)
            return result
        with patch.object(fa, "analyze_image", side_effect=edit_during_analysis):
            with self.assertRaisesRegex(fa.PipelineError, "Corrections changed during the run"):
                self.run_pipeline()
        self.assertEqual(csv_rows(self.corrections / "date_corrections.csv")[0]["confirmed"], "2021-08-31")
        self.assertEqual((self.output / "fa_images.csv").read_bytes(), prior_table)

    def test_shared_corrections_lock_blocks_a_second_writer(self):
        archive(self.source)
        self.corrections.mkdir()
        lock = self.corrections / ".pipeline.lock"
        lock.write_text("another process")
        with self.assertRaisesRegex(fa.PipelineError, "Pipeline is locked"):
            self.run_pipeline()
        self.assertEqual(lock.read_text(), "another process")
        self.assertFalse(self.output.with_name("revised.pipeline.lock").exists())


if __name__ == "__main__":
    unittest.main()
