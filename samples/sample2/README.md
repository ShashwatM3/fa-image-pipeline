# Sample 2: baseline results for FA pipeline v1.0.0

This sample contains 12 patients, 44 visit ZIPs, and 1,691 original JPEGs from the ALL FA dataset. Its purpose is to show how the unchanged pipeline behaves on a second group of patients before anyone adjusts the labeling rules. It is a baseline for review and regression comparisons. It is not a clinically annotated accuracy benchmark.

## What was selected and run

The selection used `random.Random(20261024).sample(...)` on the sorted pool of patients 10 through 99 after excluding the 12 patients in Sample 1. The selected patients are **13, 18, 21, 23, 34, 37, 57, 63, 86, 87, 89, and 97**. Every visit ZIP in those patient folders was included. `provenance/sample2_selection.json` records the exact population, selection, and source visits. This is a patient-level random sample, so patients with more visits contribute more images.

The baseline ran on October 23, 2026 with pipeline version **1.0.0**, the same source and settings as Sample 1. The SHA-256 of `fa_pipeline.py` is `f57c243822fdae9692f8db1f77bffc3521bf3a5fc99aa969b3b033f4b8531e7f`. The run used Python 3.14.7, NumPy 2.4.2, OpenCV 4.13.0, Pillow 12.1.1, and local Tesseract. The effective settings and runtime are recorded in `ALL_FA_REVISED_sample2/run_summary.json`.

## Where the sample files are

The [Sample 2 baseline release](https://github.com/ShashwatM3/fa-image-pipeline/releases/tag/sample2-baseline-v1.0.0) contains two downloadable attachments. `sample2-raw.tar` holds the original 44 visit ZIPs, and `sample2-outputs.tar` holds all 1,691 processed JPEG copies, the 44 contact sheets, the review page, tables, and correction files. Both archives contain this README and unpack into the same `sample2/` folder. The `SHA256SUMS` file in this directory records the archive checksums. The raw ZIPs total 1,187,616,364 bytes, and the original full processed output totals 1,761,743,430 bytes before tar packaging.

The repository keeps the tables, correction files, and provenance here so they can be browsed without downloading gigabytes of images. The complete original archive also remains in [Box SAMPLE 2](https://ucdavis.app.box.com/folder/430492686598). The GitHub repository and these sample downloads are public. Box access is controlled separately.

After both release attachments are downloaded, the following commands unpack them together:

```bash
mkdir -p sample2-download
cd sample2-download
gh release download sample2-baseline-v1.0.0 \
  --repo ShashwatM3/fa-image-pipeline \
  --pattern 'sample2-*.tar' --pattern SHA256SUMS
shasum -a 256 -c SHA256SUMS
tar -xf sample2-raw.tar
tar -xf sample2-outputs.tar
```

The resulting folder contains:

```text
sample2/
  README.md
  ALL_FA_sample2/                       # Original Patient N/<visit>.zip files
  ALL_FA_REVISED_sample2/               # Baseline labels and byte-identical JPEG copies
    fa_images.csv
    fa_visits.csv
    run_summary.json
    review/
      index.html                       # Local review page
      review_queue.csv
      sheets/                          # 44 annotated visit contact sheets
    Patient N/<date>/<eye>/<category>/
  ALL_FA_REVISED_sample2.corrections/   # Editable confirmation tables
  provenance/                         # Selection, integrity checks, and comparison
```

The raw archive supplies `ALL_FA_sample2`; the output archive supplies the other folders. About 3 GB is needed for the unpacked sample, with roughly another 3 GB while keeping both downloaded tar files. Opening `ALL_FA_REVISED_sample2/review/index.html` locally gives the visit contact sheets and review links. `fa_images.csv` is the per-image record linking each original ZIP member and checksum to its output file, automatic label, confidence-related flags, and review key. `fa_visits.csv` summarizes each visit.

## What the measured performance means

The full run finished in **287.18 seconds, about 4.8 minutes**, on the development Mac. That duration covers processing and output generation. It excludes Box download, GitHub upload, and human review. The sample produced the following counts:

| Measure | Count | Interpretation |
| --- | ---: | --- |
| Input images represented and copied | 1,691 / 1,691 | Every input frame has a CSV row and a byte-identical output copy. |
| Visit contact sheets | 44 / 44 | Every visit has a visual review sheet. |
| JPEG decode errors | 0 | Every JPEG decoded; this does not prove image quality or correct labels. |
| Accepted readable timers | 615 / 1,691 (36.4%) | Both OCR scales agreed; agreement still needs visual checking. |
| Unresolved eye assignments | 612 / 1,691 (36.2%) | These remain in `eye_uncertain` rather than receiving an invented OD/OS label. |
| Phases estimated from image order | 727 / 1,691 (43.0%) | These are heuristic phase estimates, not measured elapsed times. |
| Unstaged FA frames | 90 / 1,691 (5.3%) | The pipeline could not assign an FA phase under its rules. |
| Ambiguous timer flags | 47 | OCR readings disagreed and require review. |
| Coverage proxy flags | 1,149 | A peripheral brightness/texture heuristic requested review. This is not a measured eyelid-coverage percentage. |

The percentage denominator in every percentage above is **all 1,691 images**, including color and autofluorescence. These categories overlap, so the counts must not be added to estimate a total number of failures. The modality totals are 124 color, 135 autofluorescence, 30 FA-dark, and 1,402 FA. The phase-source totals are 259 modality-only frames, 30 darkness-heuristic frames, 585 timer-staged frames, 727 order-estimated frames, and 90 unresolved frames. The 615 accepted timers include 30 frames whose phase comes from the FA-dark rule, so the accepted-timer count differs from the timer-staged count.

The integrity checks passed for source archive fingerprints, ZIP CRCs, unique output paths, input/output image counts, and SHA-256 readback of every copied JPEG. These checks establish that the pipeline preserved the data and generated a complete output tree. They do not establish the correctness of an eye, modality, timer reading, or phase. There are no clinician-confirmed reference labels for this baseline, so accuracy, sensitivity, specificity, and a clinical error rate cannot yet be reported.

## How this compares with Sample 1

| Baseline measure | Sample 1 | Sample 2 |
| --- | ---: | ---: |
| Patients / visits / images | 12 / 57 / 2,050 | 12 / 44 / 1,691 |
| Runtime | 493.35 s | 287.18 s |
| Accepted readable timers | 1,216 (59.3%) | 615 (36.4%) |
| Unresolved eyes | 962 (46.9%) | 612 (36.2%) |
| Order-estimated phases | 305 (14.9%) | 727 (43.0%) |
| Unstaged FA | 207 (10.1%) | 90 (5.3%) |
| Decode errors | 0 | 0 |

Every percentage uses that sample's total image count. The same code and settings produced both columns, so these differences describe different sample contents and behavior. They do not demonstrate an improvement from Sample 1 to Sample 2. Sample 2 depends much more on image-order phase estimation and has fewer accepted clocks. Its smaller unresolved-eye count does not tell us whether its confident eye predictions are correct. The patient groups are disjoint, but once both samples guide changes, both become development samples; a separate untouched set would be needed for an independent final accuracy estimate.

## How to read the labels and review the failures

`OD` means right eye and `OS` means left eye. `eye_uncertain` means the laterality heuristic did not meet its own confidence rules. Folder categories separate color, autofluorescence, pre-dye, early, mid, late, and unresolved images. A phase folder ending in `_estimated` indicates an inference without an accepted measured clock; it must not be treated as timing ground truth.

The provisional time boundaries are pre-dye below 5 seconds, early from 5 to below 180 seconds, mid from 180 to below 480 seconds, and late from 480 seconds onward. These thresholds are configurable working rules and need review for the intended study. For missing clocks, the pipeline estimates phases from confident-eye runs when the sequence passes its ordering rules. Suspected re-imports, large image-ID gaps, and timer regressions can disable that inference. Image IDs do not measure elapsed time.

A reviewer should first inspect the 612 unresolved eye assignments, 90 unstaged FA frames, 47 ambiguous timers, and examples from the 727 estimated phases. The reviewer should also inspect confident predictions from different patients, modalities, and phases, because flags do not capture every error. Reviews should record the original prediction, reviewed reference label, and reason for the disagreement. This makes it possible to measure correctness and regression after changing the rules, rather than treating a smaller review queue as success.

The correction files sit outside the generated output. `eye_corrections.csv` accepts confirmed `OD` or `OS`; `label_corrections.csv` accepts reviewed modality, phase, or timer values; and `date_corrections.csv` accepts a confirmed date. Existing source fingerprints must stay intact. Generated JPEGs and folders should not be manually moved or renamed, because the next run replaces the generated tree.

## Reproducing and improving this baseline

A checkout of the baseline release tag contains the original pipeline source. From that checkout, the following command creates a separate output after the sample has been unpacked:

```bash
python3 fa_pipeline.py /path/to/sample2/ALL_FA_sample2 \
  --output /path/to/ALL_FA_REVISED_sample2_rerun \
  --settings fa_pipeline_settings.json \
  --corrections /path/to/sample2/ALL_FA_REVISED_sample2.corrections
```

The supplied correction files preserve the baseline state; adding confirmed corrections changes the expected labeling results. The next agreed step is to review both samples, fix recurring causes, rerun both, and compare against reviewed labels. The full ALL FA dataset should be processed after that iteration. This upload restores the existing baseline and does not rerun or improve its predictions.
