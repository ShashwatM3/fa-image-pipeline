import json
import hashlib
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fa_normalize as norm


def geometric_fixture():
    mask = np.zeros((256, 256), np.uint8)
    cv2.ellipse(mask, (128, 130), (94, 80), 0, 0, 360, 1, -1)
    cv2.ellipse(mask, (128, 30), (55, 40), 0, 0, 360, 0, -1)
    cv2.ellipse(mask, (128, 231), (55, 40), 0, 0, 360, 0, -1)
    yy, xx = np.mgrid[:256, :256]
    gray = ((40 + xx//3 + yy//4)*mask).astype(np.uint8)
    rgb = np.repeat(gray[..., None], 3, axis=2)
    return rgb, [dict(name='fixture', mask=mask)]


class FramingTests(unittest.TestCase):
    def test_same_input_and_models_give_identical_estimates(self):
        original, models = geometric_fixture()
        first = norm.estimate_framing(original, models)
        self.assertEqual(first['status'], 'estimated')
        self.assertEqual(first, norm.estimate_framing(original.copy(), models))

    def test_recovers_known_scale_and_translation_without_original_argument(self):
        original, models = geometric_fixture()
        small = cv2.resize(original, (154, 154), interpolation=cv2.INTER_AREA)
        padded = np.zeros((300, 420, 3), np.uint8)
        padded[63:217, 117:271] = small
        result = norm.estimate_framing(padded, models)
        self.assertEqual(result['status'], 'estimated', result)
        x0, y0, x1, y1 = result['crop']
        self.assertLess(abs(x0-117), 4)
        self.assertLess(abs(y0-63), 4)
        self.assertLess(abs((x1-x0)-154), 5)
        self.assertLess(abs((y1-y0)-154), 5)

    def test_uniform_nonblack_padding_is_not_treated_as_retina(self):
        original, models = geometric_fixture()
        padded = np.full((420, 360, 3), 6, np.uint8)
        padded[100:356, 40:296] = original
        result = norm.estimate_framing(padded, models)
        self.assertEqual(result['status'], 'estimated', result)
        self.assertLess(abs(result['crop'][0]-40), 4)
        self.assertLess(abs(result['crop'][1]-100), 4)

    def test_sparse_bright_edge_pixels_do_not_set_padding_noise(self):
        original, models = geometric_fixture()
        original[100:140, :2] = 255
        result = norm.estimate_framing(original, models)
        self.assertEqual(result['status'], 'estimated', result)
        x0, y0, x1, y1 = result['crop']
        self.assertLess(abs((x1-x0)-256), 13)
        self.assertLessEqual(x0, 0)
        self.assertGreaterEqual(y1, 140)

    def test_bright_padding_does_not_hide_a_dimmer_field(self):
        original, models = geometric_fixture()
        for level in (5, 16, 255):
            padded = np.full((420, 360, 3), level, np.uint8)
            padded[100:356, 40:296] = original
            result = norm.estimate_framing(padded, models)
            self.assertEqual(result['status'], 'estimated', (level, result))
            self.assertLess(abs(result['crop'][0]-40), 4)
            self.assertLess(abs(result['crop'][1]-100), 4)

    def test_uninformative_inputs_are_reviewed(self):
        _, models = geometric_fixture()
        for value in (0, 5, 100, 255):
            result = norm.estimate_framing(np.full((256, 256, 3), value, np.uint8), models)
            self.assertEqual(result['status'], 'review')
        self.assertEqual(norm.estimate_framing(np.zeros((32, 32, 3), np.uint8), models)['status'], 'review')

    def test_disconnected_content_cannot_be_silently_cropped(self):
        original, models = geometric_fixture()
        padded = np.zeros((400, 500, 3), np.uint8)
        padded[70:326, 100:356] = original
        padded[10:20, 470:480] = 255
        self.assertEqual(norm.estimate_framing(padded, models)['status'], 'review')

    def test_nearby_faint_signal_is_preserved(self):
        original, models = geometric_fixture()
        padded = np.zeros((400, 500, 3), np.uint8)
        padded[70:326, 100:356] = original
        padded[65, 225] = 2
        result = norm.estimate_framing(padded, models)
        self.assertEqual(result['status'], 'estimated', result)
        self.assertLessEqual(result['crop'][1], 65)

    def test_dim_region_can_agree_in_geometry_without_identical_outline(self):
        original, models = geometric_fixture()
        yy, xx = np.mgrid[:256, :256]
        original[(xx < 128)&(yy < 100)&(original[:, :, 0] > 0)] = 2
        result = norm.estimate_framing(original, models)
        self.assertEqual(result['status'], 'estimated', result)
        self.assertLess(abs(result['crop'][0]), 4)
        self.assertLess(abs(result['crop'][1]), 4)
        self.assertLess(abs(result['crop'][2]-256), 6)

    def test_incompatible_field_cannot_use_dim_field_fallback(self):
        _, models = geometric_fixture()
        rgb = np.zeros((256, 256, 3), np.uint8)
        cv2.circle(rgb, (128, 128), 100, (100, 100, 100), -1)
        result = norm.estimate_framing(rgb, models)
        self.assertEqual(result['status'], 'review', result)
        self.assertIn(result['review_code'], ('shape_overlap', 'no_field_match'))

    def test_render_preserves_aspect_and_cannot_render_review(self):
        rgb = np.zeros((100, 200, 3), np.uint8)
        rgb[20:80, 40:160] = 150
        output, transform = norm.render_framing(rgb, dict(status='estimated', crop=[0, 0, 200, 100], background=0))
        self.assertEqual(output.shape, (512, 1024, 3))
        self.assertEqual(transform[0, 0], transform[1, 1])
        with self.assertRaises(ValueError):
            norm.render_framing(rgb, dict(status='review'))

    def test_bad_image_array_is_rejected(self):
        _, models = geometric_fixture()
        for value in (np.zeros((100, 100), np.uint8), np.zeros((100, 100, 3), float)):
            with self.assertRaises(ValueError):
                norm.estimate_framing(value, models)

    def test_cli_does_not_overwrite_any_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = root/'input.png', root/'out.png', root/'decision.json'
            Image.new('RGB', (100, 100)).save(source)
            payload = source.read_bytes()
            output.write_bytes(b'keep')
            self.assertEqual(norm.main([str(source), '--output', str(output), '--report', str(report)]), 1)
            self.assertEqual(output.read_bytes(), b'keep')
            self.assertEqual(source.read_bytes(), payload)
            self.assertFalse(report.exists())

    def test_invalid_model_is_rejected_without_pickle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'model.npz'
            np.savez(path, metadata=np.array(json.dumps(dict(version=1,models=[dict(name='bad')]))), bad=np.full((32,32), 2))
            with self.assertRaises(ValueError):
                norm.load_models(path)

    def test_cli_review_writes_only_report_and_hashes_analyzed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report = root/'input.png', root/'out.png', root/'decision.json'
            Image.new('RGB', (100, 100)).save(source)
            payload = source.read_bytes()
            def review(rgb, models):
                source.write_bytes(b'changed after reading')
                return dict(status='review', reason='test uncertainty')
            with patch.object(norm, 'estimate_framing', side_effect=review):
                status = norm.main([str(source), '--output', str(output), '--report', str(report)])
            self.assertEqual(status, 2)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(report.read_text())['source_sha256'], hashlib.sha256(payload).hexdigest())

    def test_native_resolution_marker_is_not_lost_in_preview(self):
        original, models = geometric_fixture()
        original = cv2.resize(original, (1024, 1024))
        padded = np.zeros((1600, 2000, 3), np.uint8)
        padded[300:1324, 500:1524] = original
        padded[50, 1900] = 255
        self.assertEqual(norm.estimate_framing(padded, models)['status'], 'review')

    def test_bundled_templates_have_no_patient_identifiers_or_extra_arrays(self):
        with np.load(norm.MODEL_PATH, allow_pickle=False) as saved:
            metadata = json.loads(str(saved['metadata']))
            self.assertNotIn('training_patients', metadata)
            self.assertNotIn('evaluation_patients', metadata)
            self.assertNotRegex(json.dumps(metadata), r'Patient\s+\d+')
            self.assertEqual(set(saved.files), {'metadata'} | {m['name'] for m in metadata['models']})
        self.assertEqual(len(norm.load_models()), 2)

    def test_cli_runs_outside_repository_with_bundled_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (100, 100)).save(root/'input.png')
            result = subprocess.run([sys.executable, str(Path(norm.__file__).resolve()),
                'input.png', '--output', 'output.png', '--report', 'decision.json'],
                cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertFalse((root/'output.png').exists())
            decision = json.loads((root/'decision.json').read_text())
            self.assertEqual(decision['status'], 'review')
            self.assertEqual(decision['model_sha256'], hashlib.sha256(norm.MODEL_PATH.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
