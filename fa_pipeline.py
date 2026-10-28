#!/usr/bin/env python3
"""Build ALL_FA_REVISED from local Patient N/<visit>.zip exports.

The JPEG payloads are never re-encoded. Labels are provisional image heuristics;
review CSVs identify problems; run records are kept outside the image dataset.
Run `python3 fa_pipeline.py --help` for the command line interface.
"""
from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid
import zipfile

VERSION = "1.1.0"
OWNER = "all-fa-pipeline-v1"
MARKER = ".fa_pipeline.json"


class PipelineError(Exception):
    """An actionable input, configuration, or output error."""


@dataclasses.dataclass(frozen=True)
class Settings:
    # These are user-selected provisional boundaries, not a clinical standard.
    pre_dye_seconds: float = 5.0
    early_end_seconds: float = 180.0
    mid_end_seconds: float = 480.0
    max_timer_seconds: float = 3600.0
    near_black_mean: float = 7.0
    color_chroma_threshold: float = 5.0
    modality_ambiguity_margin: float = 0.5
    disc_min_strength: float = 1.0
    disc_x_min: float = 0.35
    disc_x_max: float = 0.68
    disc_center_margin: float = 0.03
    coverage_flag_threshold: float = 0.25
    # Coverage is a peripheral brightness/texture proxy, not a segmented area.
    coverage_bright_threshold: float = 0.72
    coverage_dark_threshold: float = 0.18
    coverage_texture_threshold: float = 0.10
    unreliable_id_gap: int = 50000
    ocr_timeout_seconds: float = 10.0
    reserve_disk_bytes: int = 200_000_000

    def validate(self) -> None:
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise PipelineError(f"Setting {f.name} must be a finite number")
        if not 0 < self.pre_dye_seconds < self.early_end_seconds < self.mid_end_seconds < self.max_timer_seconds:
            raise PipelineError("Timer boundaries must increase: 0 < pre-dye < early end < mid end < maximum")
        if not 0 <= self.disc_x_min < 0.5 < self.disc_x_max <= 1:
            raise PipelineError("Disc bounds must straddle the image center")
        for name in ("disc_center_margin", "coverage_flag_threshold", "coverage_bright_threshold",
                     "coverage_dark_threshold", "coverage_texture_threshold"):
            if not 0 <= getattr(self, name) <= 1:
                raise PipelineError(f"{name} must be in [0, 1]")
        for name in ("near_black_mean", "color_chroma_threshold", "modality_ambiguity_margin",
                     "disc_min_strength", "unreliable_id_gap", "ocr_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise PipelineError(f"{name} must be positive")
        if self.reserve_disk_bytes < 0:
            raise PipelineError("reserve_disk_bytes cannot be negative")


KNOWN_DATES = {
    (16, "2021-06-14"): ("2022-06-14", "probable year typo; database-ID pattern"),
    (55, "2021-08-13"): ("2021-08-31", "probable day-digit swap; database-ID pattern"),
    (30, "2021-04-27"): ("2021-06-01", "approximate candidate from database-ID pattern; requires lookup"),
    (54, "2020-09-22"): ("2020-10-06", "approximate candidate from database-ID pattern; requires lookup"),
    (76, "2025-01-25"): ("2025-01-21", "suspected date from neighboring IDs; Saturday alone is not proof"),
}
REIMPORT_VISITS = {
    (26, "2022-01-05"), (29, "2022-01-25"), (36, "2022-01-25"),
    (40, "2021-12-21"), (52, "2022-01-18"), (54, "2021-12-21"),
    (87, "2021-12-21"), (86, "2022-03-29"),
}
MODALITIES = {"color", "autofluorescence", "FA", "FA-dark", "unknown"}
PHASES = {"pre-dye", "early", "mid", "late", "unstaged"}
DATE_COLS = ["source_zip", "source_zip_sha256", "original_date", "suggested_date", "reason", "confirmed"]
EYE_COLS = ["image_key", "source_zip", "member_path", "sha256", "suggested_eye", "reason", "confirmed"]
LABEL_COLS = ["image_key", "source_zip", "member_path", "sha256", "predicted_modality",
              "predicted_phase", "confirmed_modality", "confirmed_phase", "confirmed_timer_s"]
IMAGE_COLS = [
    "image_key", "patient", "patient_raw", "visit_key", "visit_raw", "visit_note",
    "visit_date_original", "visit_date", "date_status", "date_suggestion", "date_reason",
    "source_zip", "source_zip_sha256", "member_path", "member_index", "image_id", "seq", "id_gap",
    "bytes", "sha256", "width", "height", "modality_predicted", "modality", "modality_source",
    "eye_predicted", "eye", "eye_source", "disc_x", "disc_strength", "timer_s", "timer_raw",
    "timer_status", "phase_predicted", "phase", "phase_source", "phase_estimated", "order_reliable",
    "mean_brightness", "color_chroma", "vessel_polarity", "near_black", "coverage_score",
    "coverage_flag", "coverage_method", "flags", "error", "output_relpath", "images_materialized",
]
VISIT_COLS = ["patient", "patient_raw", "visit_key", "source_zip", "source_zip_sha256", "zip_bytes",
              "visit_raw", "visit_date_original", "visit_date", "date_status", "date_suggestion",
              "image_count", "readable_timers", "uncertain_eyes", "unstaged_fa", "estimated_phases",
              "eye_runs", "order_reliable", "flags", "output_visit_dir"]


@dataclasses.dataclass
class Visit:
    path: Path
    source: str
    patient_number: int
    patient_raw: str
    raw_date: str
    date_original: str
    note: str
    members: list[tuple[int, zipfile.ZipInfo]]
    sha256: str = ""
    date: str = ""
    date_status: str = "original"
    suggestion: str = ""
    date_reason: str = ""
    output_dir: str = ""
    flags: set[str] = dataclasses.field(default_factory=set)

    @property
    def patient(self) -> str:
        return f"Patient {self.patient_number}"

    @property
    def key(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()[:16]


def parse_date(name: str) -> tuple[str, str]:
    base = re.sub(r"\.zip$", "", name.strip(), flags=re.I)
    m = re.match(r"^(\d{4})[.-](\d{1,2})[.-](\d{1,2})(?!\d)", base)
    if m:
        year, month, day = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[.-](\d{1,2})[.-](\d{4}|\d{2})(?!\d)", base)
        if not m:
            return "", "unparsed date: " + base
        month, day, year = map(int, m.groups())
        if len(m.group(3)) == 2:
            year += 2000
    try:
        return dt.date(year, month, day).isoformat(), base[m.end():].strip()
    except ValueError:
        return "", "invalid date: " + base


def iso_date(value: str) -> str:
    try:
        result = dt.date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return value
    except ValueError as e:
        raise PipelineError(f"Expected an ISO date YYYY-MM-DD, got {value!r}") from e


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_settings(path: Path | None) -> Settings:
    try:
        values = json.loads(path.read_text()) if path else {}
        if not isinstance(values, dict):
            raise PipelineError("Settings JSON must be an object")
        allowed = {f.name for f in dataclasses.fields(Settings)}
        if set(values) - allowed:
            raise PipelineError(f"Unknown settings: {', '.join(sorted(set(values) - allowed))}")
        settings = Settings(**values)
        settings.validate()
        return settings
    except (OSError, json.JSONDecodeError, TypeError) as e:
        raise PipelineError(f"Cannot read settings: {e}") from e


def validate_paths(source: Path, output: Path, corrections: Path) -> None:
    if not source.is_dir():
        raise PipelineError(f"Input is not a directory: {source}")
    for a, b, reason in ((source, output, "Input and output must be separate"),
                         (source, corrections, "Corrections must be outside the input"),
                         (output, corrections, "Corrections must be outside the rebuilt output")):
        if a == b or a in b.parents or b in a.parents:
            raise PipelineError(reason)
    if output.is_symlink() or corrections.is_symlink():
        raise PipelineError("Output and corrections directories cannot be symbolic links")
    if output.exists():
        if not output.is_dir():
            raise PipelineError(f"Output exists and is not a directory: {output}")
        if any(output.iterdir()):
            try:
                marker_path = output / MARKER if (output / MARKER).exists() else corrections / "run" / MARKER
                marker = json.loads(marker_path.read_text())
                if marker_path != output / MARKER and marker.get("output_root") != str(output):
                    raise PipelineError("Run records belong to another output directory")
            except (OSError, ValueError) as e:
                raise PipelineError(f"Refusing to replace an unowned output directory: {output}") from e
            if marker.get("owner") != OWNER or marker.get("input_root") != str(source):
                raise PipelineError("Existing output belongs to another input or another program")


def discover(source: Path) -> list[Visit]:
    visits = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".zip":
            continue
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            raise PipelineError(f"Symbolic links are not accepted in the input: {path}")
        rel = path.relative_to(source)
        if any(p.startswith(".") or p == "__MACOSX" for p in rel.parts):
            continue
        patient_parts = [(part, re.fullmatch(r"Patient\s+(\d+)(?:\s+.*)?", part, re.I))
                         for part in rel.parts[:-1]]
        patients = [(part, m) for part, m in patient_parts if m]
        if len(patients) != 1:
            raise PipelineError(f"Expected one Patient N directory around {rel}")
        patient_raw, match = patients[0]
        date, note = parse_date(path.name)
        try:
            with zipfile.ZipFile(path) as z:
                members = []
                for index, info in enumerate(z.infolist()):
                    member = PurePosixPath(info.filename)
                    if (member.is_absolute() or ".." in member.parts or "\\" in info.filename
                            or (info.external_attr >> 16) & 0o170000 == 0o120000):
                        raise PipelineError(f"Unsafe archive member in {rel}: {info.filename}")
                    if info.is_dir() or any(p == "__MACOSX" or p.startswith(".") for p in member.parts):
                        continue
                    if member.suffix.lower() not in (".jpg", ".jpeg"):
                        raise PipelineError(f"Unexpected non-JPEG file in {rel}: {info.filename}")
                    if info.flag_bits & 1:
                        raise PipelineError(f"Encrypted ZIP member in {rel}: {info.filename}")
                    members.append((index, info))
        except (OSError, zipfile.BadZipFile) as e:
            raise PipelineError(f"Cannot read {rel}: {e}") from e
        if not members:
            raise PipelineError(f"No JPEG images in {rel}")
        def sort_key(entry: tuple[int, zipfile.ZipInfo]) -> tuple:
            name = PurePosixPath(entry[1].filename).stem
            return (0, int(name), entry[1].filename, entry[0]) if name.isdigit() else (1, name, entry[0])
        members.sort(key=sort_key)
        visits.append(Visit(path, rel.as_posix(), int(match.group(1)), patient_raw,
                            path.stem, date, note, members))
    if not visits:
        raise PipelineError(f"No visit ZIPs found under {source}")
    return sorted(visits, key=lambda v: (v.patient_number, v.date_original, v.source))


def read_corrections(path: Path, key: str, columns: list[str]) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or set(columns) - set(reader.fieldnames):
                raise PipelineError(f"Missing columns in {path}; expected: {', '.join(columns)}")
            rows = {}
            for number, row in enumerate(reader, 2):
                if None in row or any(value is None for value in row.values()):
                    raise PipelineError(f"Malformed CSV row at {path}:{number}")
                row = {k: v.strip() for k, v in row.items()}
                if not any(row.values()):
                    continue
                ident = row[key]
                if not ident or ident in rows:
                    raise PipelineError(f"Blank or duplicate {key} at {path}:{number}")
                rows[ident] = row
            return rows
    except (OSError, csv.Error) as e:
        raise PipelineError(f"Cannot read corrections file {path}: {e}") from e


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {k: (";".join(sorted(v)) if isinstance(v, set) else
                         "true" if v is True else "false" if v is False else v)
                     for k, v in row.items()}
            writer.writerow(clean)


def atomic_csv(path: Path, columns: list[str], rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        write_csv(tmp, columns, [rows[k] for k in sorted(rows)])
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def prepare_dates(visits: list[Visit], corrections: dict[str, dict]) -> None:
    for v in visits:
        v.sha256 = hash_file(v.path)
        v.date = v.date_original
        known = KNOWN_DATES.get((v.patient_number, v.date_original))
        if known:
            v.suggestion, v.date_reason = known
            v.date_status = "suspect"
            v.flags.add("date_suspect")
        if not v.date_original:
            v.date_status = "unparsed"
            v.flags.add("date_unparsed")
            v.date_reason = v.note
        if (v.patient_number, v.date_original) in REIMPORT_VISITS:
            v.flags.add("suspected_reimport")
        old = corrections.get(v.source, {})
        confirmed = old.get("confirmed", "")
        if confirmed:
            if old.get("source_zip_sha256") != v.sha256:
                raise PipelineError(f"Date correction fingerprint is stale for {v.source}; review the changed ZIP")
            value = old.get("suggested_date", "") if confirmed.lower() in ("yes", "true") else confirmed
            v.date = iso_date(value)
            v.date_status = "confirmed"
            v.flags.discard("date_suspect")
            v.flags.discard("date_unparsed")
        if known or not v.date_original or old:
            corrections[v.source] = {
                "source_zip": v.source, "source_zip_sha256": v.sha256, "original_date": v.date_original,
                "suggested_date": old.get("suggested_date") or v.suggestion,
                "reason": old.get("reason") or v.date_reason, "confirmed": confirmed,
            }
    groups = collections.Counter((v.patient, v.date) for v in visits)
    for v in visits:
        if v.date and groups[(v.patient, v.date)] > 1:
            v.flags.add("multiple_archives_same_date")
        # Keep the source hierarchy and visit name, replacing only the ZIP suffix.
        v.output_dir = PurePosixPath(v.source).with_suffix("").as_posix()


def parse_timer(text: str, settings: Settings) -> float | None:
    m = re.fullmatch(r"(\d{1,3})[:.](\d{2})[:.](\d{3})", re.sub(r"\s+", "", text))
    if not m:
        return None
    minute, second, millis = map(int, m.groups())
    value = minute * 60 + second + millis / 1000
    if second >= 60 or value > settings.max_timer_seconds:
        return None
    return value


def read_timer(encoded: Any, small: Any, settings: Settings, tesseract: str | None) -> tuple:
    import cv2
    import numpy as np
    h, w = small.shape[:2]
    corner = small[int(h * .91):int(h * .99), int(w * .64):int(w * .93)]
    if not corner.size or corner.max() < 75:
        return None, "", "absent"
    gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None, "", "decode_error"
    h, w = gray.shape
    crop = gray[int(h * .91):int(h * .99), int(w * .64):int(w * .93)]
    if not crop.size or crop.max() < 180:
        return None, "", "absent"
    if not tesseract:
        return None, "", "unavailable"
    ys, xs = np.nonzero(crop > 160)
    tight = crop[max(int(ys.min()) - 15, 0):int(ys.max()) + 16,
                 max(int(xs.min()) - 15, 0):int(xs.max()) + 16]
    _, binary = cv2.threshold(tight, 160, 255, cv2.THRESH_BINARY_INV)
    readings = []
    for scale in (.5, .35):
        rendered = cv2.resize(binary, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        rendered = cv2.copyMakeBorder(rendered, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        ok, png = cv2.imencode(".png", rendered)
        if not ok:
            return None, "", "ocr_error"
        try:
            result = subprocess.run(
                [tesseract, "stdin", "stdout", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789:."],
                input=png.tobytes(), capture_output=True, timeout=settings.ocr_timeout_seconds,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except subprocess.TimeoutExpired:
            return None, " | ".join(readings), "timeout"
        raw = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode:
            return None, raw, "ocr_error"
        readings.append(raw)
    values = [parse_timer(text, settings) for text in readings]
    if values[0] is not None and values[0] == values[1]:
        return values[0], readings[0], "read"
    status = "ambiguous" if any(value is not None for value in values) else "unreadable"
    return None, " | ".join(readings), status


def coverage_proxy(gray: Any, settings: Settings) -> float:
    """Measure suspicious brightness and texture in a fixed retinal ellipse.

    This is deliberately named a proxy. It is not the fraction of retina actually
    hidden by a lid; disease, exposure and gaze can change it too.
    """
    import cv2
    import numpy as np
    g = cv2.resize(gray, (256, 256), interpolation=cv2.INTER_AREA).astype(np.float32)
    yy, xx = np.mgrid[:256, :256]
    ellipse = ((xx - 128) / 94) ** 2 + ((yy - 128) / 85) ** 2 <= 1
    center = ((xx - 128) / 47) ** 2 + ((yy - 128) / 36) ** 2 <= 1
    reference = float(np.percentile(g[center], 75))
    if reference < 5:
        return 0.0
    normalized = np.clip(g / max(reference, 1), 0, 3)
    local = cv2.GaussianBlur(normalized, (0, 0), 3)
    texture = cv2.GaussianBlur(np.abs(normalized - local), (0, 0), 2)
    peripheral = ellipse & ((yy < 104) | (yy > 153))
    bright = peripheral & (normalized > 1 + settings.coverage_bright_threshold)
    dark = peripheral & (normalized < settings.coverage_dark_threshold)
    textured = peripheral & (texture > settings.coverage_texture_threshold)
    return round(float(np.count_nonzero(bright | dark | textured) / np.count_nonzero(ellipse)), 6)


def analyze_image(payload: bytes, settings: Settings, tesseract: str | None) -> tuple[dict, Any]:
    import cv2
    import numpy as np
    from PIL import Image
    encoded = np.frombuffer(payload, dtype=np.uint8)
    small = cv2.imdecode(encoded, cv2.IMREAD_REDUCED_COLOR_4)
    if small is None:
        return {"modality_predicted": "unknown", "modality": "unknown", "modality_source": "decode_error",
                "eye_predicted": "", "eye": "unknown", "eye_source": "uncertain", "timer_s": None,
                "timer_raw": "", "timer_status": "decode_error", "near_black": False,
                "coverage_score": "", "coverage_flag": False, "coverage_method": "not_available",
                "error": "JPEG decoder could not read this image", "flags": {"decode_error"}}, None
    try:
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
    except OSError:
        width, height = 0, 0
    small = cv2.resize(small, (1000, 1000), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    b, g, r = [small[..., i].astype(np.int16) for i in range(3)]
    chroma = float(np.abs(r - g).mean() + np.abs(g - b).mean())
    median = cv2.medianBlur(gray, 31).astype(np.float32)
    contrast = gray.astype(np.float32) - median
    center = contrast[250:750, 250:750]
    polarity = float(np.percentile(center, 99.5) + np.percentile(center, .5))
    mean = float(gray.mean())
    near_black = mean < settings.near_black_mean
    modality = "color" if chroma > settings.color_chroma_threshold else "FA" if polarity > 0 else "autofluorescence"
    flags: set[str] = set()
    timer, raw, timer_status = (None, "", "not_applicable") if modality == "color" else read_timer(encoded, small, settings, tesseract)
    # A recognizable dye clock is stronger modality evidence than vessel polarity,
    # especially in the early transit before vessels become bright.
    source = "heuristic"
    if modality != "color" and timer_status in ("read", "ambiguous"):
        modality, source = "FA", "timer-overlay"
    if (modality != "color" and near_black and timer_status != "ambiguous"
            and (timer is None or timer < settings.pre_dye_seconds)):
        modality = "FA-dark"
    elif source == "heuristic" and modality != "color" and abs(polarity) < settings.modality_ambiguity_margin:
        flags.add("modality_ambiguous")
    density = np.abs(contrast)
    density[density < np.percentile(density, 90)] = 0
    density = cv2.GaussianBlur(density, (0, 0), 25)
    sub = density[300:700, 220:780]
    yy, xx = np.unravel_index(np.argmax(sub), sub.shape)
    dx, strength = float(xx + 220) / 1000, float(sub.max())
    eye_guess = "OD" if dx > .5 else "OS"
    confident = (strength >= settings.disc_min_strength and settings.disc_x_min <= dx <= settings.disc_x_max
                 and abs(dx - .5) >= settings.disc_center_margin and not near_black)
    if strength < settings.disc_min_strength:
        eye_guess = ""
    if not confident:
        flags.add("eye_uncertain")
    if near_black:
        flags.add("near_black")
    if width != height:
        flags.add("non_square_image")
    if timer_status in ("unreadable", "ambiguous", "ocr_error", "timeout", "unavailable"):
        flags.add("timer_" + timer_status)
    score = coverage_proxy(gray, settings)
    covered = score >= settings.coverage_flag_threshold and not near_black
    if covered:
        flags.add("coverage_proxy_high")
    return {
        "width": width, "height": height, "modality_predicted": modality, "modality": modality,
        "modality_source": source, "eye_predicted": eye_guess,
        "eye": eye_guess if confident else "unknown", "eye_source": "disc" if confident else "uncertain",
        "disc_x": round(dx, 6), "disc_strength": round(strength, 6), "timer_s": timer,
        "timer_raw": raw, "timer_status": timer_status, "mean_brightness": round(mean, 6),
        "color_chroma": round(chroma, 6), "vessel_polarity": round(polarity, 6),
        "near_black": near_black, "coverage_score": score, "coverage_flag": covered,
        "coverage_method": "peripheral_brightness_texture_proxy_v1", "error": "", "flags": flags,
    }, None


def validate_correction_values(eyes: dict[str, dict], labels: dict[str, dict], settings: Settings) -> None:
    for key, r in eyes.items():
        if r["confirmed"] and r["confirmed"].upper() not in ("OD", "OS"):
            raise PipelineError(f"Eye correction {key}: confirmed must be OD, OS, or blank")
    for key, r in labels.items():
        for column, allowed in (("confirmed_modality", MODALITIES), ("confirmed_phase", PHASES)):
            if r[column] and r[column] not in allowed:
                raise PipelineError(f"Label correction {key}: invalid {column} {r[column]!r}")
        if r["confirmed_timer_s"]:
            try:
                value = float(r["confirmed_timer_s"])
            except ValueError as e:
                raise PipelineError(f"Timer correction {key}: expected seconds as a number") from e
            if not math.isfinite(value) or not 0 <= value <= settings.max_timer_seconds:
                raise PipelineError(f"Timer correction {key}: value outside the allowed duration")


def apply_image_corrections(row: dict, eyes: dict[str, dict], labels: dict[str, dict]) -> None:
    key = row["image_key"]
    for corrections, confirmed in ((eyes, ("confirmed",)), (labels, ("confirmed_modality", "confirmed_phase", "confirmed_timer_s"))):
        old = corrections.get(key, {})
        if any(old.get(c) for c in confirmed) and old.get("sha256") != row["sha256"]:
            raise PipelineError(f"Image correction fingerprint is stale for {key}; review the changed image")
    eye = eyes.get(key, {}).get("confirmed", "")
    if eye:
        row["eye"] = eye.upper()
        row["eye_source"] = "manual"
        row["flags"].discard("eye_uncertain")
    modality = labels.get(key, {}).get("confirmed_modality", "")
    if modality:
        row["modality"] = modality
        row["modality_source"] = "manual"
        row["flags"].discard("modality_ambiguous")
    timer = labels.get(key, {}).get("confirmed_timer_s", "")
    if timer:
        row["timer_s"] = float(timer)
        row["timer_status"] = "manual"
        for flag in ("timer_unreadable", "timer_ambiguous", "timer_timeout", "timer_unavailable", "timer_ocr_error"):
            row["flags"].discard(flag)


def phase_from_timer(seconds: float, settings: Settings) -> str:
    if seconds < settings.pre_dye_seconds:
        return "pre-dye"
    if seconds < settings.early_end_seconds:
        return "early"
    if seconds < settings.mid_end_seconds:
        return "mid"
    return "late"


def resolve_dark_frames(rows: list[dict]) -> None:
    """Darkness after an established FA sequence cannot establish pre-dye timing."""
    seen_fa = False
    for row in rows:
        if (row["modality"] == "FA-dark" and row["timer_s"] is None
                and seen_fa and row["modality_source"] != "manual"):
            row["modality"] = "FA"
            row["modality_source"] = "sequence"
            row["flags"].add("dark_frame_after_fa")
        if row["modality"] == "FA":
            seen_fa = True


def assign_phases(rows: list[dict], visit: Visit, settings: Settings, labels: dict[str, dict]) -> int:
    fa = [r for r in rows if r["modality"] == "FA"]
    timed = [r for r in fa if r["timer_s"] is not None]
    order_reliable = "suspected_reimport" not in visit.flags
    if any(r["id_gap"] > settings.unreliable_id_gap for r in rows):
        order_reliable = False
        visit.flags.add("large_id_gap_order_unreliable")
    if any(b["timer_s"] < a["timer_s"] for a, b in zip(timed, timed[1:])):
        order_reliable = False
        visit.flags.add("timer_order_conflict")
    runs: list[list[dict]] = []
    current_eye = None
    for row in fa:
        eye = row["eye"] if row["eye"] in ("OD", "OS") else None
        if not runs or (eye and current_eye and eye != current_eye):
            runs.append([])
        runs[-1].append(row)
        if eye:
            current_eye = eye
    # The six-run model is only an estimate; incomplete sequences remain unstaged.
    inferred: dict[str, str] = {}
    if order_reliable and len(runs) >= 4:
        for index, run in enumerate(runs):
            phase = "early" if index < 2 else "late" if index >= len(runs) - 2 else "mid"
            for row in run:
                inferred[row["image_key"]] = phase
    if len(runs) != 6 and fa:
        visit.flags.add("atypical_eye_run_count")
    for row in rows:
        mod, timer = row["modality"], row["timer_s"]
        estimated = False
        if mod in ("color", "autofluorescence"):
            phase, source = "pre-injection", "modality"
        elif (mod == "FA" or row.get("timer_status") == "manual") and timer is not None:
            phase = phase_from_timer(timer, settings)
            source = "manual-timer" if row.get("timer_status") == "manual" else "timer"
        elif mod == "FA-dark":
            phase, source = "pre-dye", "darkness_heuristic"
            estimated = timer is None
        elif mod == "FA" and row["image_key"] in inferred:
            phase, source = inferred[row["image_key"]], "inferred-order"
            estimated = True
            row["flags"].add("phase_estimated")
        else:
            phase, source = "unstaged", "unresolved"
            if mod == "FA":
                row["flags"].add("phase_unresolved")
        row["phase_predicted"] = phase
        manual = labels.get(row["image_key"], {}).get("confirmed_phase", "")
        if manual:
            if mod not in ("FA", "FA-dark"):
                raise PipelineError(f"A phase correction requires FA or FA-dark modality: {row['image_key']}")
            phase, source, estimated = manual, "manual", timer is None and manual in ("early", "mid", "late")
            row["flags"].discard("phase_unresolved")
            row["flags"].discard("phase_estimated")
        row.update(phase=phase, phase_source=source, phase_estimated=estimated, order_reliable=order_reliable)
        row["flags"].update(visit.flags)
    return len(runs)


def output_relative(row: dict, visit: Visit, count: int) -> str:
    member = PurePosixPath(row["member_path"])
    duplicates = sum(PurePosixPath(info.filename).name.casefold() == member.name.casefold()
                     for _, info in visit.members)
    name = member.name
    if duplicates > 1:
        name = f"{member.stem}__member{row['member_index']}{member.suffix}"
    return f"{visit.output_dir}/{name}"


def update_review_queues(rows: list[dict], eyes: dict[str, dict], labels: dict[str, dict]) -> None:
    confident = [i for i, r in enumerate(rows) if r["eye"] in ("OD", "OS")]
    for index, row in enumerate(rows):
        key = row["image_key"]
        if row["eye"] == "unknown":
            before = next((rows[i]["eye"] for i in reversed(confident) if i < index), None)
            after = next((rows[i]["eye"] for i in confident if i > index), None)
            if before and before == after:
                row["eye_predicted"] = before
        if row["eye"] == "unknown" or key in eyes:
            old = eyes.get(key, {})
            eyes[key] = {"image_key": key, "source_zip": row["source_zip"], "member_path": row["member_path"],
                         "sha256": row["sha256"], "suggested_eye": row["eye_predicted"],
                         "reason": "disc localization uncertain; suggestion may use neighboring eyes",
                         "confirmed": old.get("confirmed", "")}
        if (row["phase"] == "unstaged" or "modality_ambiguous" in row["flags"]
                or row["timer_status"] in ("ambiguous", "unreadable", "timeout", "ocr_error")
                or row["modality"] == "unknown" or key in labels):
            old = labels.get(key, {})
            labels[key] = {"image_key": key, "source_zip": row["source_zip"], "member_path": row["member_path"],
                           "sha256": row["sha256"], "predicted_modality": row["modality_predicted"],
                           "predicted_phase": row["phase_predicted"],
                           "confirmed_modality": old.get("confirmed_modality", ""),
                           "confirmed_phase": old.get("confirmed_phase", ""),
                           "confirmed_timer_s": old.get("confirmed_timer_s", "")}


# Each rule is a review category, not a diagnosis or a reason to discard a scan.
REVIEW_RULES = {
    "eye_unknown": ({"eye_uncertain"}, "The pipeline could not determine left or right eye.",
                    "Check the image or source record; enter OD or OS in confirmed_eye if known."),
    "modality_uncertain": ({"modality_ambiguous"}, "The image type is uncertain.",
                           "Check whether this is color, autofluorescence, FA, or FA-dark; fill confirmed_modality if known."),
    "phase_unknown": ({"phase_unresolved"}, "The FA stage could not be determined.",
                      "Check the image, visit sequence and source record; fill confirmed_phase only if supported."),
    "phase_estimated": ({"phase_estimated"}, "The FA stage was estimated without an accepted clock reading.",
                        "Check the estimated stage; fill confirmed_phase if supported, otherwise leave blank."),
    "timer_uncertain": ({"timer_ambiguous", "timer_unreadable", "timer_timeout", "timer_ocr_error", "timer_unavailable"},
                        "The printed timer could not be read reliably, or OCR was unavailable.",
                        "Check the timer on the original image; fill confirmed_timer_s in seconds if readable."),
    "timer_missing": (set(), "No accepted timer value is available for this FA image.",
                      "Check the original image or source record for elapsed seconds; do not invent a time from image order."),
    "date_uncertain": ({"date_suspect", "date_unparsed"}, "The visit date may be wrong or could not be parsed.",
                       "Check the clinic record; enter confirmed_date as YYYY-MM-DD. This applies to the whole visit."),
    "sequence_uncertain": ({"suspected_reimport", "timer_order_conflict", "large_id_gap_order_unreliable", "atypical_eye_run_count", "multiple_archives_same_date"},
                           "The visit sequence has a warning; this does not prove that this individual image is wrong.",
                           "Check the source visit and image order before trusting order-based stage labels."),
    "dark_images": ({"near_black", "dark_frame_after_fa"}, "The image is very dark or occurs after FA has already started.",
                    "Check whether this is a usable scan, an expected dark frame, or an acquisition problem."),
    "possible_obstruction": ({"coverage_proxy_high"}, "A brightness/texture heuristic suggests possible obstruction; it may be a false alarm.",
                             "Inspect for eyelids or eyelashes hiding useful retina; decide whether the image is usable."),
    "unusual_dimensions": ({"non_square_image"}, "The image is rectangular rather than square.",
                           "Check whether the export is complete and suitable for the model; do not assume it is damaged."),
    "unreadable_images": ({"decode_error"}, "The JPEG decoder could not read this image.",
                          "Try the source export and obtain a readable replacement if necessary."),
    "image_name_unusual": ({"non_numeric_image_name"}, "The original filename is not a numeric image ID.",
                           "Check the source record before using filename order as capture order."),
}
SEQUENCE_PROBLEMS = {
    "suspected_reimport": "The visit may contain re-imported images, so numeric IDs may not follow capture order.",
    "timer_order_conflict": "Readable timers decrease when images are sorted by ID, so ID order conflicts with elapsed time.",
    "large_id_gap_order_unreliable": "A very large gap between image IDs makes order-based stage inference unreliable.",
    "atypical_eye_run_count": "The visit does not follow the expected six alternating eye groups used by the stage heuristic.",
    "multiple_archives_same_date": "More than one source ZIP has the same patient and parsed visit date.",
}
REVIEW_COLS = ["image_path", "problem", "labeller_task", "patient", "visit", "eye", "modality", "phase",
               "timer_s", "visit_date", "trigger_flags", "confirmed_eye", "confirmed_modality",
               "confirmed_phase", "confirmed_timer_s", "confirmed_date", "image_key", "source_zip",
               "source_zip_sha256", "member_path", "sha256"]


def write_review_csvs(directory: Path, rows: list[dict]) -> dict[str, int]:
    directory.mkdir(parents=True, exist_ok=True)
    counts = {}
    for category, (flags, problem, task) in REVIEW_RULES.items():
        selected = []
        for row in rows:
            triggers = flags & row["flags"]
            missing_timer = (category == "timer_missing" and row["modality"] in ("FA", "FA-dark")
                             and row["timer_s"] in (None, ""))
            unknown_modality = category == "modality_uncertain" and row["modality"] == "unknown"
            if not (triggers or missing_timer or unknown_modality):
                continue
            selected.append({**{key: row.get(key, "") for key in REVIEW_COLS},
                             "image_path": row["output_relpath"],
                             "problem": " ".join(SEQUENCE_PROBLEMS[f] for f in sorted(triggers)) if category == "sequence_uncertain" else problem,
                             "labeller_task": task,
                             "visit": row["visit_raw"], "trigger_flags": ";".join(sorted(triggers))})
        if selected:
            write_csv(directory / (category + ".csv"), REVIEW_COLS, selected)
            counts[category] = len(selected)
    return counts


def import_review(directory: Path, dates: dict, eyes: dict, labels: dict) -> dict[Path, str]:
    hashes = {p: hash_file(p) for p in directory.glob("*.csv")}
    decisions = {}
    for path in sorted(hashes):
        if path.stem not in REVIEW_RULES:
            if path.name == "review_queue.csv":  # v1.0 report, not an editable form.
                continue
            raise PipelineError(f"Unexpected review CSV: {path}; retain it outside the generated dataset")
        for key, row in read_corrections(path, "image_key", REVIEW_COLS).items():
            for field in ("confirmed_eye", "confirmed_modality", "confirmed_phase", "confirmed_timer_s", "confirmed_date"):
                value = row[field]
                if not value:
                    continue
                ident = row["source_zip"] if field == "confirmed_date" else key
                fingerprint = row["source_zip_sha256"] if field == "confirmed_date" else row["sha256"]
                if not fingerprint:
                    raise PipelineError(f"Missing correction fingerprint in {path}")
                decision = (value, fingerprint)
                if (ident, field) in decisions and decisions[ident, field] != decision:
                    raise PipelineError(f"Conflicting review decisions for {ident}: {field}")
                decisions[ident, field] = decision
                if field == "confirmed_date":
                    iso_date(value)
                    dates[ident] = {**row, "confirmed": value, "original_date": row["visit_date"],
                                    "suggested_date": value, "reason": "Reviewed in review/date_uncertain.csv"}
                elif field == "confirmed_eye":
                    eyes[key] = {**row, "confirmed": value, "suggested_eye": row["eye"], "reason": "Reviewed in review CSV"}
                else:
                    old = labels.get(key, {})
                    # A fingerprint must bind every saved decision, including earlier fields.
                    if old.get("sha256") not in (None, fingerprint) and any(old.get(c) for c in LABEL_COLS if c.startswith("confirmed_")):
                        raise PipelineError(f"Image correction fingerprint is stale for {key}")
                    labels[key] = {**{c: "" for c in LABEL_COLS}, **old,
                                   **{c: row[c] for c in ("image_key", "source_zip", "member_path", "sha256")}, field: value}
    return hashes


def materialize_visit(staging: Path, visit: Visit, rows: list[dict]) -> None:
    with zipfile.ZipFile(visit.path) as z:
        members = z.infolist()
        for row in rows:
            target = staging / row["output_relpath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            sha = hashlib.sha256()
            with z.open(members[row["member_index"]]) as source, target.open("xb") as dest:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    sha.update(chunk)
                    dest.write(chunk)
            if sha.hexdigest() != row["sha256"]:
                raise PipelineError(f"Source image changed during the run: {row['source_zip']}/{row['member_path']}")
            # Read-back checks cover output corruption, not just bytes sent to write().
            if hash_file(target) != row["sha256"]:
                raise PipelineError(f"Output checksum failed: {row['output_relpath']}")


def promote(staging: Path, output: Path, records_staging: Path, records_output: Path) -> None:
    backups, published = [], []
    try:
        for fresh, destination in ((records_staging, records_output), (staging, output)):
            backup = destination.with_name(destination.name + ".previous-" + uuid.uuid4().hex[:8])
            if destination.exists():
                os.replace(destination, backup)
                backups.append((destination, backup))
            os.replace(fresh, destination)
            published.append(destination)
    except OSError:
        # Restore the label table and images together if either promotion fails.
        for destination in reversed(published):
            shutil.rmtree(destination)
        for destination, backup in reversed(backups):
            os.replace(backup, destination)
        raise
    for _, backup in backups:
        try:
            shutil.rmtree(backup)
        except OSError:
            print(f"Output complete; old managed output remains at {backup}", file=sys.stderr)


def run_pipeline(source: Path, output: Path, corrections: Path, settings: Settings,
                 *, tables_only: bool = False, sheets: bool = False, allow_no_ocr: bool = False,
                 max_visits: int | None = None) -> dict:
    start = time.monotonic()
    # Resolve parent paths while retaining the ability to reject a symlink leaf.
    source = source.expanduser().resolve()
    output = output.expanduser().parent.resolve() / output.name
    corrections = corrections.expanduser().parent.resolve() / corrections.name
    validate_paths(source, output, corrections)
    settings.validate()
    tesseract = shutil.which("tesseract")
    if not tesseract and not allow_no_ocr:
        raise PipelineError("Tesseract is missing. Install it, or explicitly use --allow-no-ocr for an estimated-only run")
    visits = discover(source)
    if max_visits is not None:
        if max_visits < 1:
            raise PipelineError("--max-visits must be positive")
        visits = visits[:max_visits]
    correction_paths = [corrections / name for name in
                        ("date_corrections.csv", "eye_corrections.csv", "label_corrections.csv")]
    correction_hashes = {p: hash_file(p) if p.exists() else None for p in correction_paths}
    dates = read_corrections(corrections / "date_corrections.csv", "source_zip", DATE_COLS)
    eyes = read_corrections(corrections / "eye_corrections.csv", "image_key", EYE_COLS)
    labels = read_corrections(corrections / "label_corrections.csv", "image_key", LABEL_COLS)
    review_hashes = import_review(output / "review", dates, eyes, labels)
    validate_correction_values(eyes, labels, settings)
    expected_count = sum(len(v.members) for v in visits)
    image_bytes = sum(info.file_size for v in visits for _, info in v.members)
    output.parent.mkdir(parents=True, exist_ok=True)
    # The space budget includes CSVs; old output is
    # deliberately left in place while a fresh complete output is assembled.
    needed = (0 if tables_only else image_bytes) + settings.reserve_disk_bytes
    free = shutil.disk_usage(output.parent).free
    if free < needed:
        raise PipelineError(f"Need approximately {needed / 1e9:.2f} GB free for this run; have {free / 1e9:.2f} GB. "
                            "Use --tables-only to review labels without copying JPEGs, or choose another output disk")
    print(f"Input: {len(visits)} ZIPs, {expected_count} images, {image_bytes:,} uncompressed bytes", flush=True)
    # A per-output exclusive lock prevents two runs from swapping the same tree.
    corrections.mkdir(parents=True, exist_ok=True)
    locks = []
    try:
        for lock in (output.with_name(output.name + ".pipeline.lock"), corrections / ".pipeline.lock"):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as e:
                raise PipelineError(f"Pipeline is locked: {lock}. If a run crashed, confirm it stopped before removing this lock") from e
            locks.append(lock)
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid(), "input": str(source)}, f)
    except BaseException:
        for lock in locks:
            lock.unlink(missing_ok=True)
        raise
    staging: Path | None = None
    try:
        prepare_dates(visits, dates)
        staging = Path(tempfile.mkdtemp(prefix="." + output.name + ".building-", dir=output.parent))
        all_rows: list[dict] = []
        visit_rows: list[dict] = []
        paths: set[str] = set()
        for number, visit in enumerate(visits, 1):
            rows = []
            previous_id = None
            with zipfile.ZipFile(visit.path) as z:
                for sequence, (member_index, info) in enumerate(visit.members, 1):
                    payload = z.read(info)  # ZipFile also verifies the original CRC.
                    features, _ = analyze_image(payload, settings, tesseract)
                    stem = PurePosixPath(info.filename).stem
                    image_id = int(stem) if stem.isdigit() else ""
                    key = hashlib.sha256(f"{visit.source}\0{member_index}\0{info.filename}".encode()).hexdigest()[:24]
                    row = {
                        "image_key": key, "patient": visit.patient, "patient_raw": visit.patient_raw,
                        "visit_key": visit.key, "visit_raw": visit.raw_date, "visit_note": visit.note,
                        "visit_date_original": visit.date_original, "visit_date": visit.date,
                        "date_status": visit.date_status, "date_suggestion": visit.suggestion,
                        "date_reason": visit.date_reason, "source_zip": visit.source, "source_zip_sha256": visit.sha256,
                        "member_path": info.filename, "member_index": member_index, "image_id": image_id,
                        "seq": sequence, "id_gap": (image_id - previous_id) if image_id != "" and previous_id is not None else 0,
                        "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                        "images_materialized": not tables_only, **features,
                    }
                    if image_id == "":
                        row["flags"].add("non_numeric_image_name")
                    previous_id = image_id if image_id != "" else None
                    apply_image_corrections(row, eyes, labels)
                    rows.append(row)
            resolve_dark_frames(rows)
            run_count = assign_phases(rows, visit, settings, labels)
            update_review_queues(rows, eyes, labels)
            for row in rows:
                row["output_relpath"] = output_relative(row, visit, len(rows))
                if row["output_relpath"].casefold() in paths:
                    raise PipelineError(f"Output name collision: {row['output_relpath']}")
                paths.add(row["output_relpath"].casefold())
            if not tables_only:
                materialize_visit(staging, visit, rows)
            if hash_file(visit.path) != visit.sha256:
                raise PipelineError(f"Input ZIP changed during the run: {visit.source}")
            all_rows.extend(rows)
            visit_rows.append({
                "patient": visit.patient, "patient_raw": visit.patient_raw, "visit_key": visit.key,
                "source_zip": visit.source, "source_zip_sha256": visit.sha256, "zip_bytes": visit.path.stat().st_size,
                "visit_raw": visit.raw_date, "visit_date_original": visit.date_original,
                "visit_date": visit.date, "date_status": visit.date_status, "date_suggestion": visit.suggestion,
                "image_count": len(rows), "readable_timers": sum(r["timer_s"] is not None for r in rows),
                "uncertain_eyes": sum(r["eye"] == "unknown" for r in rows),
                "unstaged_fa": sum(r["modality"] == "FA" and r["phase"] == "unstaged" for r in rows),
                "estimated_phases": sum(r["phase_estimated"] for r in rows), "eye_runs": run_count,
                "order_reliable": rows[0]["order_reliable"], "flags": visit.flags,
                "output_visit_dir": visit.output_dir,
            })
            print(f"[{number}/{len(visits)}] {visit.source}: {len(rows)} images; "
                  f"{visit_rows[-1]['readable_timers']} clocks; {visit_rows[-1]['uncertain_eyes']} uncertain eyes", flush=True)
        if len(all_rows) != expected_count:
            raise PipelineError("Image count mismatch; refusing to publish an incomplete output")
        if not tables_only and sum(p.is_file() for p in staging.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg")) != expected_count:
            raise PipelineError("Output file count mismatch")
        records_staging = staging / "_run_records"
        write_csv(records_staging / "fa_images.csv", IMAGE_COLS, all_rows)
        write_csv(records_staging / "fa_visits.csv", VISIT_COLS, visit_rows)
        review_counts = write_review_csvs(staging / "review", all_rows)
        summary = {
            "pipeline_version": VERSION, "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "input_root": str(source), "output_root": str(output), "records_root": str(corrections), "review_categories": review_counts,
            "mode": "tables-only" if tables_only else "full", "partial_run": max_visits is not None,
            "patients": len({v.patient for v in visits}), "visits": len(visits), "images": len(all_rows),
            "source_image_bytes": image_bytes, "images_materialized": not tables_only,
            "readable_timers": sum(r["timer_s"] is not None for r in all_rows),
            "uncertain_eyes": sum(r["eye"] == "unknown" for r in all_rows),
            "decode_errors": sum(bool(r.get("error")) for r in all_rows),
            "modalities": dict(collections.Counter(r["modality"] for r in all_rows)),
            "phase_sources": dict(collections.Counter(r["phase_source"] for r in all_rows)),
            "flags": dict(collections.Counter(flag for r in all_rows for flag in r["flags"])),
            "settings": dataclasses.asdict(settings), "tesseract": tesseract,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "validation": {"row_count_matches_input": True, "output_paths_unique": True,
                           "source_zip_crc_checked": True, "jpeg_sha256_readback_verified": not tables_only},
            "limitations": ["Image labels are heuristics, not clinical ground truth",
                            "The coverage score is a brightness/texture proxy, not measured eyelid area",
                            "The timing boundaries are provisional user-selected settings"],
        }
        (records_staging / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        (records_staging / MARKER).write_text(json.dumps({
            "owner": OWNER, "input_root": str(source), "output_root": str(output), "version": VERSION}))
        # Preserve all earlier rows, including corrections for archives outside this sample.
        current_review = {p for p in (output / "review").glob("*.csv")}
        if current_review != set(review_hashes):
            raise PipelineError("Review files changed during the run; your edits are preserved")
        for path, original_hash in {**correction_hashes, **review_hashes}.items():
            if (hash_file(path) if path.exists() else None) != original_hash:
                raise PipelineError(f"Corrections changed during the run: {path}. Your edits are preserved; rerun to apply them")
        atomic_csv(corrections / "date_corrections.csv", DATE_COLS, dates)
        atomic_csv(corrections / "eye_corrections.csv", EYE_COLS, eyes)
        atomic_csv(corrections / "label_corrections.csv", LABEL_COLS, labels)
        promote(staging, output, records_staging, corrections / "run")
        staging = None
        print(f"Complete: {len(all_rows)} images; output {output}", flush=True)
        return summary
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        for lock in locks:
            lock.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="Local folder containing Patient N directories and visit ZIPs")
    parser.add_argument("--output", type=Path, required=True, help="Generated ALL_FA_REVISED directory")
    parser.add_argument("--settings", type=Path, help="JSON overrides; built-in defaults work without this file")
    parser.add_argument("--corrections", type=Path, help="Legacy alias for --records", dest="records")
    parser.add_argument("--records", type=Path, help="Run records outside the dataset; default: <output>.records")
    parser.add_argument("--tables-only", action="store_true", help="Analyze every image but do not copy JPEGs")
    parser.add_argument("--no-sheets", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-no-ocr", action="store_true", help="Explicitly allow missing Tesseract")
    parser.add_argument("--max-visits", type=int, help="Smoke-test only the first N sorted visits; recorded as partial")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    try:
        import cv2
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
        cv2.setNumThreads(1)
        settings = load_settings(args.settings)
        corrections = args.records or args.output.with_name(args.output.name + ".records")
        legacy = args.output.with_name(args.output.name + ".corrections")
        if not args.records and not corrections.exists() and legacy.exists():
            raise PipelineError(f"Existing corrections found at {legacy}; pass --records {legacy} to preserve them")
        run_pipeline(args.input, args.output, corrections, settings, tables_only=args.tables_only,
                     allow_no_ocr=args.allow_no_ocr, max_visits=args.max_visits)
        return 0
    except ImportError as e:
        print(f"Missing dependency: {e}. Install requirements.txt with this Python interpreter", file=sys.stderr)
        return 2
    except (PipelineError, OSError, zipfile.BadZipFile, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; any previous completed output has been retained", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
