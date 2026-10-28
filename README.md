# ALL FA processing pipeline

`fa_pipeline.py` is a standalone Python entry point. It reads local visit ZIPs, labels every JPEG, applies reviewed corrections, and builds `ALL_FA_REVISED`. It does not download from Box, upload to Box, attach diagnoses, choose training splits, remove frames, or prepare images for model training. The earlier labeling prototype is separate and is not included in this repository or imported by this pipeline.

## Included sample

[Sample 2](samples/sample2/README.md) contains browsable baseline tables, correction files, and an explanation of pipeline performance. Its full raw images and processed output are downloadable from the [Sample 2 baseline release](https://github.com/ShashwatM3/fa-image-pipeline/releases/tag/sample2-baseline-v1.0.0). The sample README explains how to download and unpack them.

Version 1.0.1 also accepts explicit month.day.four-digit-year visit names, such as `12.3.2024.zip`. This format appeared in the added Patient 300 data. The image-labeling rules and settings remain the same as version 1.0.0; the original sample release preserves that baseline version.

## Running the pipeline

The supported input is a directory containing `Patient N/<visit date>.zip`, including the extra directory around Patient 11's September 2021 ZIP. Patient directory notes such as `Patient 12 No Timestamp` are retained in the table, while output directories use `Patient 12`. The input ZIPs stay zipped and remain unchanged.

The script requires Python 3.10 or newer. The Python packages are installed with `python3 -m pip install -r requirements.txt`, preferably in a virtual environment. Tesseract must also be installed, for example with `brew install tesseract` on macOS. This implementation was developed with Python 3.14.7, NumPy 2.4.2, OpenCV 4.13.0, and Pillow 12.1.1. The dependencies are already available on the development Mac.

```bash
python3 fa_pipeline.py /path/to/ALL_FA_sample1 \
  --output /path/to/ALL_FA_REVISED_sample1 \
  --settings fa_pipeline_settings.json
```

The same script can be copied to a lab computer. Its built-in settings match the supplied JSON, so `--settings` is optional. A settings file can contain only the fields being changed; unknown fields and invalid boundaries cause an error. Tesseract is required by default. The explicit `--allow-no-ocr` option allows a run without it, records the missing OCR, and leaves timing-dependent labels estimated or unresolved.

```bash
# This mode analyzes all images and writes tables and review sheets without JPEG copies.
python3 fa_pipeline.py /path/to/sample --output /path/to/FA_review --tables-only

# This partial run is for checking setup, not a complete dataset.
python3 fa_pipeline.py /path/to/sample --output /path/to/FA_smoke --max-visits 1

# The implementation tests use Python's built-in test runner.
python3 -m unittest discover -s tests -v
```

## Outputs and file names

The full output contains `fa_images.csv`, `fa_visits.csv`, `run_summary.json`, a short dataset README, `review/index.html`, `review/review_queue.csv`, and one annotated contact sheet per visit under `review/sheets/`. The review page works locally without external services. `--no-sheets` omits the JPEG contact sheets. A tables-only run explicitly records that image copies are absent; its `output_relpath` values describe planned destinations.

```text
ALL_FA_REVISED/
  fa_images.csv
  fa_visits.csv
  run_summary.json
  review/
    index.html
    review_queue.csv
    sheets/<visit key>.jpg
  Patient 13/
    2021-05-04/
      OD/
        1_color/
        2_autofluorescence/
        3_FA_pre-dye/
        4_FA_early/
        5_FA_mid/
        6_FA_late/
      OS/
      eye_uncertain/
```

The same categories appear under each eye when images belong there; empty folders are not created. Estimated phases use `4_FA_early_estimated`, `5_FA_mid_estimated`, and `6_FA_late_estimated`. An FA image that cannot be staged goes to `7_FA_unstaged`, and an undecodable or unknown-modality image goes to `7_modality_uncertain`. These extra review categories preserve every frame without inventing a label.

A name such as `07_t0028s_3041205.jpg` contains the one-based image position within the visit, whole elapsed seconds when a clock is readable, and the original image ID. Whole seconds are truncated for the filename; the CSV preserves the milliseconds. An image without a readable clock has no `t...s` token. Files from two ZIPs with the same normalized patient/date remain separate under date directories with a stable `__v<key>` suffix. Unparsed dates use `date_unknown__v<key>` until confirmed.

## Working with the generated dataset

`fa_images.csv` defines the generated layout, but users make corrections in the separate correction files and rerun the script. Files inside `ALL_FA_REVISED` must not be moved, renamed, deleted, or edited by hand. Every run reads and labels every input image again, applies corrections, and builds a complete replacement tree. There is no labeling cache or incremental mode.

JPEG copies retain the original byte sequence, including any printed timer or image comments. Each row records SHA-256, original ZIP name and relative path, ZIP SHA-256, archive member path and index, original image ID, and output path. Each copied JPEG is read back and checked against its source checksum. Byte preservation does not establish that the inferred eye, modality, or phase label is correct.

## Corrections that survive reruns

The default correction directory is the output path with `.corrections` appended, such as `ALL_FA_REVISED_sample1.corrections/`. `--corrections /path/to/directory` selects another location. It must be outside both the raw input and the rebuilt output. The files are regenerated with suggestions while preserving confirmed values and rows for visits outside the current sample.

| File | Editable confirmation fields | Meaning |
| --- | --- | --- |
| `date_corrections.csv` | `confirmed` | A reviewed ISO date, or `yes` to approve the row's `suggested_date`. A blank keeps the supplied date. |
| `eye_corrections.csv` | `confirmed` | `OD` for right eye or `OS` for left eye. A blank leaves an unresolved image in `eye_uncertain`. |
| `label_corrections.csv` | `confirmed_modality`, `confirmed_phase`, `confirmed_timer_s` | Optional overrides for modality, phase, or clock seconds. A blank retains the automatic result. |

The five suspect date suggestions are built in for Patients 16, 30, 54, 55, and 76. They are candidates from the prior investigation, not verified clinic dates. The Patient 30 and Patient 54 suggestions are approximate. A different weekday alone does not trigger a date change. An unparsed date also creates a correction row, with no invented date.

The image correction key is in `fa_images.csv`, the review queue, and the review page. A reviewer can add a row for a confident but wrong prediction by copying its key, source information, and SHA-256 from the table. Modality values are `color`, `autofluorescence`, `FA`, `FA-dark`, or `unknown`; FA phase values are `pre-dye`, `early`, `mid`, `late`, or `unstaged`. Manual eye and modality corrections are applied before sequence-based phase estimation. A manual phase without a readable clock still uses an estimated stage folder for early, mid, and late.

Fingerprint fields must remain intact. A confirmed correction is rejected if its source bytes have changed, so an old decision cannot silently apply to a replacement image or ZIP. A reviewer must check that replacement and update the fingerprint when deliberately reconfirming it. Correction rows for images outside the current input are retained for later use.

## Labeling rules and limits

The date parser accepts year-first dotted, dashed, and mixed names, month.day.year names with two- or four-digit years, and trailing annotations. The original spelling always remains available. Numeric image IDs define the processing order within a visit, but they are database identifiers, not elapsed time measurements. The script does not extrapolate capture dates or elapsed seconds from them.

The modality heuristic uses color-channel differences and the polarity of vessel contrast. A recognizable dye clock takes precedence over vessel polarity, because early FA vessels may still be dark. A dark frame after an established FA sequence does not become pre-dye just because its clock is unreadable. The laterality heuristic uses the horizontal position of a vessel-density peak near the optic disc. Ambiguous disc localization remains unresolved; neighboring confident eyes can supply a review suggestion but do not automatically turn that suggestion into a confirmed eye. These are image-based heuristics developed on a small initial sample, and they require human review.

The clock reader uses Tesseract on the lower-right image corner at two rendering scales. It accepts a time only when both readings agree, validates the seconds field and maximum duration, and records raw OCR text and status. A disagreement is marked ambiguous and sent to the label correction queue; `confirmed_timer_s` accepts reviewed seconds with decimals. A missing clock, an unreadable clock, an ambiguous clock, an OCR timeout, and unavailable Tesseract are distinct states. OCR agreement is not independent proof of accuracy, and readings must still be checked against the contact sheets.

| Elapsed time read from the clock | Assigned FA phase |
| --- | --- |
| `0 <= t < 5` seconds | `pre-dye` |
| `5 <= t < 180` seconds | `early` |
| `180 <= t < 480` seconds | `mid` |
| `t >= 480` seconds | `late` |

These are the provisional boundaries selected during planning. They are not a documented clinic protocol or an established clinical grading standard. Their suitability still needs to be checked on the random sample. Near-black non-color frames with no clock or a clock under five seconds use the separate darkness heuristic for the pre-dye category. A dark late frame remains a late frame and also receives the near-black flag.

For an FA frame without readable time, the script estimates phase from successive confident-eye runs. With at least four runs, the first two are early, the last two are late, and intervening runs are mid. Six runs are the original working assumption; a different count is flagged. Fewer than four runs remain unstaged. Mixed visits can contain both timed and estimated frames, so failed OCR does not leave a frame without a destination.

Suspected re-imports for Patients 26, 29, 36, 40, 52, 54, 86, and 87 are flagged. Missing-clock frames in those visits remain unstaged because ID order may differ from capture order. A very large ID gap or decreasing readable timers also disables order-based inference for that visit. Timed frames can still receive clock-based phases. A reviewer can resolve individual frames through `label_corrections.csv`.

## Quality flags and review

The script records near-black frames and a coverage score. The score is explicitly a peripheral brightness/texture proxy that can respond to eyelids or eyelashes; it is not a calibrated percentage of retina hidden. Exposure, gaze, disease, and image modality can also affect it. The adjustable threshold produces `coverage_proxy_high`, which requests review and never removes an image. The proxy has not been validated against clinician annotations.

Blur detection and near-duplicate detection remain deferred. Every frame, including an undecodable JPEG payload, receives a table row and an output copy in a full run. An unreadable ZIP or failed archive CRC stops the run rather than publishing a dataset with missing frames. The contact sheets are review derivatives; the actual dataset images are never resized or re-encoded.

## Failure handling and disk space

The script validates the input layout, rejects unsafe archive paths and encrypted entries, and checks free space before analysis. It never extracts ZIP paths directly. A full run needs room for the uncompressed JPEG payloads, review files, and the configured reserve. Sample 1 has 1.85 GB of ZIPs but 2.49 GB of uncompressed JPEG payloads, so output size must not be estimated by assuming ZIP compression is negligible.

An earlier completed output stays in place while a sibling staging directory is built. The new tree replaces it only after counts and checksums pass; a failed run cleans its staging directory. Replacing an existing full output therefore needs space for a second output during the rebuild. The script refuses to overwrite a nonempty directory without its ownership marker, and exclusive locks protect both the output and the shared corrections directory. If a person edits corrections during a run, the run stops before replacing those files or the completed output, preserving the person's changes. An abruptly terminated run may leave a lock or `.building-...` directory; these can be removed only after confirming that the process has stopped.

The agreed trajectory is to run sample 1, run a disjoint random sample 2 with the same pipeline version and settings, review failures from both and iterate, then process all of ALL FA. The existing three-visit prototype accuracy claims do not establish accuracy on the new samples or the full dataset.

## First sample validation, October 23, 2026

All 26 implementation tests passed. A full run on Sample 1 processed 12 patients, 57 ZIPs, and 2,050 images in about 8.2 minutes. Every image received a CSV row and a byte-identical output copy, every source ZIP fingerprint matched the download manifest, and all 57 contact sheets were generated. `validation_results.json` records these checks and the Python source fingerprint. Sample 1 is now archived in [Box](https://ucdavis.app.box.com/folder/430492831040). Its local image data was removed after remote verification, and small CSV/correction snapshots remain at `/Users/gobus/Downloads/_fa_analysis/sample1_records`.

The run read 1,216 clocks, left 962 eye assignments unresolved, and left 207 FA frames unstaged. These results show that laterality and order-based phase heuristics still need iteration; the prototype's earlier uncertainty estimate does not generalize. The coverage proxy and modality predictions also require review. On the original 33-frame timed visit, 27 clock readings matched the stored reference exactly and two OCR disagreements were left for review. The sample also contained 90 rectangular JPEGs, which were retained and flagged, and no images with EXIF data.

## Second sample baseline, October 23, 2026

Sample 2 used 12 different patients and the same Python source and settings as sample 1. Its full run processed 44 ZIPs and 1,691 images in about 4.8 minutes, retained every frame, verified each copied JPEG, and generated 44 contact sheets. It read 615 clocks, left 612 eye assignments unresolved, and left 90 FA frames unstaged. These are review counts, and label accuracy still needs human assessment. Sample 2's complete archive is [Box SAMPLE 2](https://ucdavis.app.box.com/folder/430492686598). Its local raw ZIPs, processed output, and original corrections directory were removed at the user's request after archive verification; small CSV and correction snapshots remain in `/Users/gobus/Downloads/_fa_analysis/sample2_records`.

Both baseline runs are complete. The next agreed stage is to review failures from both samples and iterate on the pipeline, rerunning the samples before attempting the full ALL FA dataset. `sample_comparison.csv` in the sample 2 archive records the baseline counts, and `sample2_validation_results.json` records its integrity checks and source fingerprint.

## Full ALL FA run, October 27, 2026

The full v1.0.1 run processed 101 patients, 285 visit ZIPs and 9,992 images, including the newly added patients 300–310. All original JPEG bytes were preserved, all 285 contact sheets were generated, and there were zero decode errors. A separate pass reread every source and output JPEG to verify SHA-256 hashes. All 3,741 image predictions from the original two samples matched their baselines exactly.

The [complete Box archive](https://ucdavis.app.box.com/folder/431763249246) contains the [revised dataset](https://ucdavis.app.box.com/folder/431763330844), external correction files, exact pipeline snapshot, execution plan, source/download inventories, baseline comparison and validation results. The [run report](https://ucdavis.app.box.com/file/2554312693352) explains the results and review workflow. All 10,301 files were independently listed and verified in Box by name, size and SHA-1; the archive manifest is also verified. Box access is controlled separately from this public code repository.

The original visit ZIPs total 7.19 GB, and the uploaded outputs and supporting records total 10.53 GB before the archive manifest. Downloading and checks took 4.9 minutes, pipeline processing took 25.2 minutes, and Box folder preparation, upload and independent verification took 70.8 minutes on the development laptop and network. These are measurements of this run rather than guaranteed runtimes elsewhere.

The full run left 4,190 eye assignments unresolved, 1,143 FA frames unstaged and 3,603 phases estimated from image order. These overlapping review counts do not measure clinical accuracy. The next stage is reviewing uncertain cases and confident predictions, recording confirmed corrections, and rebuilding the dataset.
