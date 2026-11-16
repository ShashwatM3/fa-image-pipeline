# Aggregate captured-field templates

`field_models.npz` contains two binary silhouettes and JSON metadata. These are
captured-field envelopes, not retinal photographs or segmentations of gradable
retina. The estimator considers both shapes for every image:

- `bounded`: a 256 × 256 mask representing the recurring bounded, concave field.
- `window`: a 201 × 256 mask representing an observed rectangular export window.

They were derived from patient-balanced majority outlines in an internal training
cohort of 63 unique patients. The bounded template uses 62 patients and the window
template three; some patients contribute to both. The 38 evaluation patients do
not contribute to either template. They do not establish that the two export
types share an anatomical scale or that an unobserved outer field is complete.

The published masks are numerically identical to the templates used in the
reported experiments. Metadata has been reduced to format version, research
scope, model names, shapes and aggregate patient counts. No patient identifiers,
patient assignments, clinical records or source images are included. Redacting
metadata changes the NPZ file hash; output JSON records the hash of the actual
distributed file.

The private training dataset is not distributed, so it is not possible to rebuild
these exact templates from this repository alone. The included templates allow
the inference pipeline and synthetic geometric tests to run without that dataset.
