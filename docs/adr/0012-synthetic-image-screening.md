# ADR 0012: Synthetic-image screening is an abstention signal

## Status

Accepted, 2026-09-11.

## Context

The frozen pedestrian/road-vehicle validation admitted one AI-generated road
scene because both CLIP relevance and YOLO vehicle evidence were valid.  A
real-photo requirement therefore needs independent forensic evidence.

Whole-image AI detectors remain probabilistic and can fail on future
generators, severe recompression, screenshots, or small generated composites.
Their scores must not be presented as proof of provenance.

## Decision

- Generic pedestrian and road-vehicle profiles require a `synthetic_image`
  score and a topic-free `non_photographic` style score in addition to object
  and scene-context evidence.
- A calibrated score above `0.98` routes a sample to human review.  It does not
  automatically reject the image.
- A CLIP photographic-style score above `0.92` (equivalent to a max-prompt
  cosine margin of about `0.024` at scale 100) also routes to review.  The
  prompts intentionally omit people, vehicles, and scene topics to reduce
  semantic leakage into the style comparison.
- An explicit OCR disclosure such as `AI generated` or `AI生成` is a separate,
  auditable content rule and may quarantine the image automatically.  For
  Chinese watermarks, install `chi_sim.traineddata`; `eng`-only OCR cannot
  reliably recognize the disclosure.
- The default detector is the pinned ONNX artifact
  `Thermostatic/community-forensics-low-quality-detector-2026-08` at revision
  `6fca3e7f4365363ee5c0fdb1a17d73917d54413d`; its config SHA-256 check is
  enforced before inference.  The small runtime files are downloaded on first
  use unless `SMART_SPIDER_SYNTHETIC_MODEL` points at a local artifact.
- If the forensic model or its signal is unavailable, the gate fails closed to
  review rather than accepting the sample.

## Consequences

The gate sacrifices some automatic coverage to protect accepted-set precision.
Validation reports retain the detector revision, model checksum, calibrated
score, and explicit reason so decisions can be replayed and audited.  This is a
screening control, not an authorship determination.
