# smart-spider agent guide

This repository collects web content and produces traceable image, text, video, and multimodal datasets. Read `README.md` and the relevant module before changing a pipeline.

## Code map

- `smart_spider/cli.py`, `dataset_cli.py`, `multimodal_cli.py`, `image_search_cli.py`: user entry points.
- `smart_spider/dataset_contracts.py`: persisted sample and label contracts.
- `smart_spider/multimodal_job.py`, `multimodal_pipeline.py`, `multimodal_repository.py`: discovery, staging, annotation, and output.
- `smart_spider/jina_backends.py`, `image_retrieval.py`: optional model enrichment and local retrieval.
- `smart_spider/url_policy.py`, `site_policy.py`, `image_safety.py`: network and content safety boundaries.
- `tests/`: unit and integration tests; add focused tests for changed behavior.

## Working rules

1. Inspect `git status` first. Preserve unrelated edits and generated datasets. Do not commit model weights, cookies, credentials, browser state, or crawled content.
2. Keep optional model imports lazy. Base crawling and CLIP paths must work without the Jina extra.
3. Reuse validated local staged images for model inference; do not fetch source media a second time. Ground image label suggestions in staged image bytes before page text. Keep private and reserved addresses blocked by default.
4. Treat model similarities, ranks, and inferred labels as evidence. Persist model ID, revision, source, and score; require calibration or human review before promotion to training labels.
5. Maintain `SampleRecord` serialization and CLI compatibility. A transient field must not enter the manifest.
6. Use small, licensed, reproducible fixtures for validation. Pin model revisions for production and check the model license before commercial use.

## Verify changes

```bash
python -m pytest tests/test_jina_backends.py tests/test_image_retrieval.py tests/test_multimodal_cli.py
python -m pytest
python -m smart_spider.multimodal_cli --help
python -m smart_spider.image_search_cli --help
```

Report targeted and full-suite results separately, including any known baseline failures. For model changes, also run a tiny real-weight smoke test when weights and hardware are available; record the exact revision and observed output.
