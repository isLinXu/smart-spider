---
name: smart-spider-data-enrichment
description: Use smart-spider to collect, annotate, rerank, and retrieve multimodal data with traceable Jina model evidence.
---

# Smart-spider data enrichment

Use this skill when a task asks to operate or extend smart-spider crawling, multimodal annotation, text reranking, or local image search.

## Procedure

1. Read `AGENTS.md`, `README.md`, the selected CLI, and `smart_spider/dataset_contracts.py`. Check `git status` before editing.
2. Choose the input: `--queries` for search, `--urls` for pages, or `--site` for a configured site. Limit a first run with `--max-samples` and `--pages`.
3. Install the needed extra in an isolated environment: `pip install -e '.[jina]'` for omni and v3.5, or `pip install -e '.[jina-v2]'` for v2. Their Transformers major versions conflict, so use separate environments. Add browser or text extras only when needed.
4. Run the chosen workflow. Keep the output directory outside source files.

```bash
smart-spider-multimodal --queries '猫,狗' --labels '猫,狗' \
  --jina-omni --jina-omni-revision 009e0d09a98a82227526e8c5aa7b0aa282e36163 \
  --jina-reranker v3.5 --jina-reranker-revision e8a93f33f0b22108f8c2364f8484ce3422552fbc \
  --max-samples 10 \
  --output ./runtime/jina-smoke

smart-spider-image-search --build-from ./images \
  --model jinaai/jina-embeddings-v5-omni-nano \
  --model-revision 009e0d09a98a82227526e8c5aa7b0aa282e36163 \
  --index ./runtime/jina-images.npz
smart-spider-image-search --query-text '夜间道路上的行人' \
  --index ./runtime/jina-images.npz --top-k 5
```

5. Inspect `manifest.jsonl`, `quality_report.json`, and retrieval JSON. Candidate labels belong in `pipeline.annotation.candidates`; text ranks in `pipeline.jina_rerank`. Check that model suggestions remain advisory and that image references resolve to staged or materialized local files.
6. If extending code, add focused regression tests and run the commands in `AGENTS.md`. Record model ID, exact revision, input count, output paths, and any failures.

Text reranking needs a nonempty search query and multiple candidates in the same annotation batch. `--urls` alone has no query, so it produces no `pipeline.jina_rerank` ranking. For image samples with staged bytes, omni candidate labels use image evidence first; text-only samples use text evidence.

## Model roles

- `jinaai/jina-embeddings-v5-omni-nano`: classification adapter for candidate labels; retrieval adapter for image documents and text or image queries.
- `jinaai/jina-reranker-v2-base-multilingual`: text pair scoring within a query group.
- `jinaai/jina-reranker-v3.5`: listwise text reranking within a query group.

The model scores are not calibrated probabilities. Do not infer image content from a text-only reranker. Pin `--jina-omni-revision` and `--jina-reranker-revision` for repeatable runs. Review each model's Hugging Face license before use; the public weights are marked CC BY-NC 4.0.
