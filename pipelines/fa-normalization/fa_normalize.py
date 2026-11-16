#!/usr/bin/env python3
"""Experimental digital-framing recovery for FA images; originals stay unchanged.

This matches captured-field silhouettes, not gradable retina or physical retinal
scale. Uncertain fits produce a review record without a transformed image.
"""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MODEL_PATH = Path(__file__).with_name('models') / 'field_models.npz'
ANALYSIS_EDGE = 640
OUTPUT_EDGE = 1024


def load_models(path=MODEL_PATH):
    with np.load(path, allow_pickle=False) as saved:
        metadata = json.loads(str(saved['metadata']))
        if metadata.get('version') != 1:
            raise ValueError('Unsupported field model version')
        models = []
        for entry in metadata['models']:
            mask = saved[entry['name']]
            if (mask.ndim != 2 or min(mask.shape) < 16 or not np.isfinite(mask).all()
                    or not np.isin(mask, (0, 1)).all() or not mask.any()):
                raise ValueError('Invalid field model mask')
            models.append(dict(name=entry['name'], mask=mask.astype(np.uint8)))
        if not models:
            raise ValueError('No field models')
    return models


def field_component(gray, threshold):
    n, labels, stats, _ = cv2.connectedComponentsWithStats((gray > threshold).astype(np.uint8), connectivity=8)
    if n == 1:
        return None
    label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = (labels == label).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(mask, contours, -1, 1, cv2.FILLED)
    x, y, w, h = cv2.boundingRect(mask)
    if min(w, h) < 24 or int(mask.sum()) < 200:
        return None
    return mask, (x, y, w, h)


def match_field(mask, box, model):
    template = model['mask']
    mx, my, mw, mh = cv2.boundingRect(template)
    x, y, w, h = box
    sx, sy = w/mw, h/mh
    if abs(math.log(sx/sy)) > .18:
        return None
    initial = math.sqrt(sx*sy)
    best = None
    for scale in initial*np.linspace(.94, 1.06, 9):
        W, H = round(template.shape[1]*scale), round(template.shape[0]*scale)
        if min(W, H) < 24 or max(W, H) > 2.5*max(mask.shape):
            continue
        rendered = cv2.resize(template, (W, H), interpolation=cv2.INTER_NEAREST)
        pad = max(W, H)//2 + 2
        padded = cv2.copyMakeBorder(mask, pad, pad, pad, pad, cv2.BORDER_CONSTANT)
        correlation = cv2.matchTemplate(padded.astype(np.float32), rendered.astype(np.float32), cv2.TM_CCORR)
        _, intersection, _, position = cv2.minMaxLoc(correlation)
        union = float(mask.sum()+rendered.sum())-intersection
        score = min(1., max(0., intersection/max(union, 1)))
        if best is None or score > best['score']:
            best = dict(model=model['name'], score=score,
                        x=float(position[0]-pad), y=float(position[1]-pad),
                        w=float(W), h=float(H))
    return best


def estimate_framing(rgb, models):
    """Return a framing estimate from this image alone, without paired references."""
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('Expected an RGB uint8 image')
    if min(rgb.shape[:2]) < 64:
        return dict(status='review', review_code='too_small', reason='Image is too small to establish framing')
    height, width = rgb.shape[:2]
    factor = min(1., ANALYSIS_EDGE/max(height, width))
    small = cv2.resize(rgb, (round(width*factor), round(height*factor)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    edge = np.concatenate((gray[:3].ravel(), gray[-3:].ravel(), gray[:, :3].ravel(), gray[:, -3:].ravel()))
    p10, p90 = np.percentile(edge, (10, 90))
    uniform_border = p90-p10 <= 6
    background = float(np.median(edge)) if uniform_border else 0.
    # Use the same central border population as the uniformity check; bright
    # anatomy or overlays at a few edge pixels must not set the noise floor.
    noise = max(1., float(p90)-background+1) if uniform_border else 1.
    threshold_base = background
    padding_removed = False
    if uniform_border and background > noise+2:
        # Remove only the flat background connected to the canvas exterior;
        # dim eye signal inside it must not be thresholded against the padding.
        flat = (cv2.absdiff(gray, background) <= noise).astype(np.uint8)
        connected = cv2.copyMakeBorder(flat, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
        cv2.floodFill(connected, None, (0, 0), 2)
        gray = gray.copy()
        gray[connected[1:-1, 1:-1] == 2] = 0
        threshold_base = 0.
        padding_removed = True
    if float(np.percentile(gray, 99))-threshold_base < 8 or float(gray.std()) < 1:
        return dict(status='review', review_code='insufficient_signal', reason='Insufficient signal or contrast for a field estimate')
    candidates = []
    for threshold in (threshold_base+noise, threshold_base+noise+1, threshold_base+noise+3):
        component = field_component(gray, threshold)
        if component is None:
            continue
        mask, box = component
        for model in models:
            result = match_field(mask, box, model)
            if result is not None:
                candidates.append(dict(**result, threshold=float(threshold)))
    strong = sorted([r for r in candidates if r['score'] >= .95], key=lambda r: -r['score'])
    failures = dict(threshold_agreement=0, model_ambiguity=0, no_preservation_signal=0, preservation_expansion=0)
    # Keep the strict path first. A dim-field fallback keeps the 0.95 anchor and
    # allows a partly faded outline to corroborate geometry at another threshold.
    for support_score, scale_tolerance in ((.95, .025), (.85, .05)):
        for candidate in strong:
            agreeing = [r for r in candidates if r['score'] >= support_score and r['model'] == candidate['model']
                and r['threshold'] != candidate['threshold']
                and abs(math.log(r['w']/candidate['w'])) <= scale_tolerance
                and abs((r['x']+r['w']/2)-(candidate['x']+candidate['w']/2))/candidate['w'] <= .02
                and abs((r['y']+r['h']/2)-(candidate['y']+candidate['h']/2))/candidate['h'] <= .02]
            if not agreeing:
                failures['threshold_agreement'] += 1; continue
            alternatives = [r for r in strong if r['model'] != candidate['model']]
            if alternatives and max(r['score'] for r in alternatives) >= candidate['score']-.025:
                failures['model_ambiguity'] += 1; continue
            x, y, w, h = (candidate[k]/factor for k in ('x', 'y', 'w', 'h'))
            # Preserve faint signal as well as bright content and disconnected overlays.
            signal = np.max(cv2.absdiff(rgb, (background, background, background, 0)), axis=2) > noise
            yy, xx = np.nonzero(signal)
            if not len(xx):
                failures['no_preservation_signal'] += 1; continue
            margin = 2/factor
            sx0, sy0 = max(0, xx.min()-margin), max(0, yy.min()-margin)
            sx1, sy1 = min(width, xx.max()+1+margin), min(height, yy.max()+1+margin)
            preserved = (min(x, sx0), min(y, sy0), max(x+w, sx1), max(y+h, sy1))
            cw, ch = preserved[2]-preserved[0], preserved[3]-preserved[1]
            if cw > w*1.05 or ch > h*1.05:
                failures['preservation_expansion'] += 1; continue
            # shortcut: field-shape agreement supports digital reframing only;
            # add reviewed anatomical calibration before claiming cross-camera scale.
            return dict(status='estimated', reason='Consistent captured-field shape at multiple thresholds',
                model=candidate['model'], score=round(candidate['score'], 6),
                crop=[float(v) for v in preserved], background=background,
                input_size=[width, height], threshold_agreement=1+len(agreeing),
                agreement_method='strict' if support_score == .95 else 'dim_field',
                padding_removed=padding_removed, scope='digital framing; no physical retinal calibration')
    code = next((key for key in reversed(failures) if failures[key]), 'shape_overlap' if candidates else 'no_field_match')
    reasons = dict(threshold_agreement='Field geometry is unstable across brightness thresholds',
        model_ambiguity='Different field shapes give comparable fits',
        no_preservation_signal='No reliable signal for the preservation check',
        preservation_expansion='Preserving detected content would change the fitted framing too much',
        shape_overlap='Captured field does not match a reference shape closely enough',
        no_field_match='No compatible captured-field geometry could be fitted')
    return dict(status='review', review_code=code, reason=reasons[code], candidate_failures=failures,
                best_score=round(max((r['score'] for r in candidates), default=0.), 6))


def render_framing(rgb, estimate):
    if estimate.get('status') != 'estimated':
        raise ValueError('A review case must not be transformed automatically')
    x0, y0, x1, y1 = estimate['crop']
    width, height = x1-x0, y1-y0
    scale = OUTPUT_EDGE/max(width, height)
    size = (max(1, round(width*scale)), max(1, round(height*scale)))
    matrix = np.array([[scale, 0, -x0*scale], [0, scale, -y0*scale]], dtype=np.float64)
    background = int(round(estimate['background']))
    result = cv2.warpAffine(rgb, matrix, size, flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(background,)*3)
    return result, matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--output', required=True, type=Path, help='New PNG; existing files are refused')
    parser.add_argument('--report', required=True, type=Path, help='New JSON decision record')
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != '.png':
            raise ValueError('Output must be PNG')
        paths = [args.image.expanduser().resolve(), args.output.expanduser().resolve(), args.report.expanduser().resolve()]
        if len(set(paths)) != 3 or args.output.exists() or args.report.exists():
            raise ValueError('Input, output and report must be distinct; output/report must not exist')
        cv2.setNumThreads(1)
        payload = args.image.read_bytes()
        with Image.open(io.BytesIO(payload)) as im:
            rgb = np.asarray(im.convert('RGB'))
        decision = estimate_framing(rgb, load_models())
        decision['source_sha256'] = hashlib.sha256(payload).hexdigest()
        decision['model_sha256'] = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
        if decision['status'] == 'estimated':
            result, matrix = render_framing(rgb, decision)
            decision['output_transform'] = matrix.tolist()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('xb') as f:
                Image.fromarray(result).save(f, format='PNG')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x') as f:
            json.dump(decision, f, indent=2); f.write('\n')
        print(json.dumps(decision, indent=2))
        return 0 if decision['status'] == 'estimated' else 2
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
