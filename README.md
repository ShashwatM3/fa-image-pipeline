# ALL FA processing pipeline

`fa_pipeline.py` reads patient visit ZIPs, labels the images, and copies every original JPEG into an unzipped visit folder. Version **1.1.0** produces the simple layout below. The image-analysis rules are unchanged from 1.0.1.

```text
ALL_FA_REVISED/
  Patient 1/
    2021-06-22/
      1234567.jpg
      1234568.jpg
  Patient 12 No Timestamp/
    <original visit name>/
      <original image name>.jpg
  review/
    eye_unknown.csv
    phase_unknown.csv
    phase_estimated.csv
    ...other categories that actually have cases...
```

Patient names, visit names and original image names are preserved. Each source ZIP becomes a folder at the same relative location without `.zip`. The existing extra directory around Patient 11's September 2021 ZIP is therefore also preserved. Images sit directly inside their visit folder. There are no eye/phase subfolders, collages, HTML pages, logs or metadata tables inside the dataset. If a ZIP contains repeated image basenames, a deterministic `__member<archive index>` suffix prevents overwriting either image. The source member path and index remain in the records.

## Running it

Python 3.10+, Tesseract, and the packages in `requirements.txt` are required. On macOS, Tesseract can be installed with `brew install tesseract`. Install Python packages in your environment with `python3 -m pip install -r requirements.txt`.

```bash
python3 fa_pipeline.py /path/to/ALL_FA \
  --output /path/to/ALL_FA_REVISED \
  --records /path/to/PIPELINE/RUN_RECORDS \
  --settings fa_pipeline_settings.json

python3 -m unittest discover -s tests -v
```

`--records` and `--settings` are optional. The default records directory is `<output>.records`; it must be outside the source and output directories. Built-in settings match the supplied JSON. `--max-visits 1` makes a partial smoke-test run. `--tables-only` analyzes images and writes records and review CSVs without copying images; the image paths are then planned paths. `--allow-no-ocr` explicitly permits missing Tesseract and records the resulting uncertainty. The old `--no-sheets` flag is accepted for compatibility; sheets are never generated. `--corrections` is a legacy alias for `--records`.

When upgrading a v1.0 output, reuse its existing correction directory with `--records /path/to/ALL_FA_REVISED.corrections` to preserve earlier confirmed decisions. You can move that directory under `PIPELINE` first and pass its new location. The ownership marker binds an existing output to its input and records, so retain the records directory alongside the dataset.

## What the labeller uses

The labeller opens **`ALL_FA_REVISED/review/`**. Each CSV covers one kind of problem. Each row starts with `image_path`, `problem` and `labeller_task`. `image_path` is relative to `ALL_FA_REVISED`, not relative to the CSV. The actual image stays in its patient/visit folder and is not copied into review. One image can appear in several CSVs because it has several warnings. A warning is not proof that an image or label is wrong.

| CSV | What needs checking |
| --- | --- |
| `eye_unknown.csv` | Left or right eye could not be determined. |
| `modality_uncertain.csv` | The image type is uncertain. |
| `phase_unknown.csv` | The FA stage could not be determined. |
| `phase_estimated.csv` | The stage was estimated from order without an accepted clock. |
| `timer_uncertain.csv` | OCR disagreed, failed, timed out or was unavailable. |
| `timer_missing.csv` | An FA image has no accepted elapsed time. |
| `date_uncertain.csv` | The visit date may be wrong or could not be parsed. |
| `sequence_uncertain.csv` | The visit's order or grouping needs checking. This is visit context, not an accusation against every listed image. |
| `dark_images.csv` | A very dark image needs inspection for usability or interpretation. |
| `possible_obstruction.csv` | A brightness/texture heuristic suggests possible eyelid/eyelash obstruction. |
| `unusual_dimensions.csv` | The image is rectangular; check whether the export is complete. |
| `unreadable_images.csv` | The JPEG decoder could not read the image. |
| `image_name_unusual.csv` | A nonnumeric filename makes ID-based ordering uncertain. |

Only categories with cases are written. The full dataset currently has 11 populated categories, with no unreadable JPEGs or nonnumeric image names.

A labeller can fill the appropriate `confirmed_*` column and rerun the pipeline. Values are: `confirmed_eye` = `OD` or `OS`; `confirmed_modality` = `color`, `autofluorescence`, `FA`, `FA-dark` or `unknown`; `confirmed_phase` = `pre-dye`, `early`, `mid`, `late` or `unstaged`; `confirmed_timer_s` = seconds, including decimals; `confirmed_date` = `YYYY-MM-DD`. A date decision applies to the entire source ZIP. Blank means no decision. Conflicting entries for the same image/visit are rejected. Fingerprint and source columns must remain intact. The pipeline checks those fingerprints before applying decisions.

Confirmed decisions persist in the run records even after a resolved image disappears from its review category. Review CSVs are regenerated, so they are forms for the listed `confirmed_*` fields, not a place for extra notes or new columns. Quality categories request human inspection; the pipeline does not automatically discard images. Reviewers should record any dataset inclusion/exclusion decisions separately for the ML training workflow. A confident but wrong prediction can also be corrected using the internal correction tables and its key/hash from `run/fa_images.csv`.

## What the records are for

`PIPELINE/RUN_RECORDS` in the example command holds the complete label table `run/fa_images.csv`, visit table `run/fa_visits.csv`, summary `run/run_summary.json`, ownership markers and persistent correction state. These are pipeline records outside the image dataset. The model's labels are in `run/fa_images.csv`; its `output_relpath` column points to each image. The image folder alone does not provide training targets.

The three internal correction tables preserve the earlier interface: `date_corrections.csv` (`confirmed` = ISO date, or `yes` to approve the suggestion), `eye_corrections.csv` (`confirmed` = OD/OS), and `label_corrections.csv` (`confirmed_modality`, `confirmed_phase`, `confirmed_timer_s`). A labeller handling the flagged cases can use the review CSVs instead. Do not enter contradictory decisions in different copies of the dataset; download the edited review files before a local rerun.

## How the labels work

The source folder is a visit date, and the source filename is an image ID. The script parses year.month.day, year-month-day, and month.day.year dates, retaining uncertain dates until confirmed. Known suspect dates are suggestions, not verified clinic dates. Image IDs provide ordering evidence, not measured time since dye injection.

Color-channel differences and vessel contrast estimate modality. A recognizable dye timer takes precedence over vessel contrast. A vessel-density peak near the optic disc estimates laterality; ambiguous cases remain unknown. Neighboring eyes can supply a suggestion but do not become a confirmed label automatically.

Tesseract reads the lower-right timer at two scales. A reading is accepted only when both agree and the value passes bounds checks. Agreement is not independent proof of accuracy. Working phase boundaries are below 5 seconds = pre-dye; 5 to below 180 = early; 180 to below 480 = mid; 480 onward = late. These are provisional selected settings, not a validated clinical standard.

Without a clock, a provisional sequence rule uses alternating eye runs. Suspected re-imports, large ID gaps and timer regressions disable that inference. Frames without enough evidence remain unstaged. Darkness can suggest a pre-dye frame but is not reliable on its own. The coverage score is an unvalidated brightness/texture proxy, not a measured percentage of hidden retina. Blur and duplicate detection are not implemented.

## Integrity and limitations

Every source JPEG is retained byte for byte, including its printed text. Images are not cropped, resized, re-encoded or deleted. Source ZIP CRCs, source hashes, output read-back hashes, image counts and unique destinations are checked. Unsafe archive paths, encrypted entries and unexpected non-JPEG files stop the run. A corrupt JPEG payload is preserved and listed for review; an unreadable ZIP stops the run.

The pipeline builds a sibling staging directory before replacing its managed output. It refuses an unowned output, checks available disk space, locks the output and records, and stops if review forms or correction tables change while it is running. Keep both the dataset and its records. An interrupted process may leave a lock or staging directory; confirm the process stopped before removing either. Rebuilding requires space for a second copy of the uncompressed images until promotion completes.

This is a labeling and organization pipeline, not a clinically validated training dataset builder. It does not assign diagnoses, choose train/test splits or remove unusable scans. The ML loader should select the original JPEGs under patient folders and use the label table, while treating unreviewed labels as provisional.

## Existing runs

Sample 1 processed 12 patients, 57 visits and 2,050 images. Sample 2 processed 12 different patients, 44 visits and 1,691 images. [Sample 2's README](samples/sample2/README.md) and [v1.0.0 release](https://github.com/ShashwatM3/fa-image-pipeline/releases/tag/sample2-baseline-v1.0.0) preserve that historical baseline, including its older folder layout. Those archived sample files have not been rewritten to v1.1.0.

The full v1.0.1 run processed **101 patients, 285 visits and 9,992 images**, preserving all original JPEG bytes with zero decode errors. All 3,741 predictions from the two samples matched their baselines. It left 4,190 eyes unresolved, 1,143 FA frames unstaged and 3,603 phases estimated from order. These are overlapping review counts, not accuracy measurements.

The original ZIPs total 7.19 GB and their JPEGs total 10.45 GB. That full run took 4.9 minutes to download/check, 25.2 minutes to process, and 70.8 minutes to upload and verify the previous layout. These measurements depend on the laptop, network and folder layout.

The [Box archive](https://ucdavis.app.box.com/folder/431763249246) holds the [full revised dataset](https://ucdavis.app.box.com/folder/431763330844). The v1.1.0 reorganization retains those predictions, verifies all source ZIP and output JPEG hashes, and changes paths and review reports without rerunning OCR. Original All FA remains unchanged. Box access is separate from this public code repository.
