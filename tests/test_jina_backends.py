"""Jina adapters use injected models; tests never download model weights."""
import io
import json

import numpy as np
import pytest
from PIL import Image

from smart_spider.dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelMode,
    LabelPolicy,
    Modality,
    ModalityAsset,
    SampleRecord,
)
from smart_spider.jina_backends import (
    RERANKER_V2,
    RERANKER_V35,
    JinaOmniAnnotationBackend,
    JinaOmniRetrievalEncoder,
    JinaTextRerankBackend,
)
from smart_spider.multimodal_job import (
    MultimodalDatasetOrchestrator,
    MultimodalJobConfig,
)
from smart_spider.multimodal_pipeline import (
    AnnotationRouter,
    DiscoveryTask,
    SourceResponse,
)


class FakeOmni:
    def encode_document(self, value):
        if isinstance(value, Image.Image) or "cat" in str(value).lower():
            return np.array([1.0, 0.0])
        return np.array([0.0, 1.0])


def _sample(name, text, query="cats"):
    return SampleRecord(
        sample_id=name,
        provenance={"query": query},
        modalities=[ModalityAsset(Modality.TEXT, text=text)],
    )


def test_omni_suggestions_remain_candidates_even_for_fixed_labels():
    sample = _sample("one", "a cat")
    backend = JinaOmniAnnotationBackend(["cat", "dog"], model=FakeOmni(), top_k=1)
    router = AnnotationRouter(
        LabelPolicy(mode=LabelMode.FIXED, fixed_labels=["cat"]),
        {"jina_omni": backend},
    )
    result = router.annotate(sample)
    assert result.resolution.labels == []
    assert len(result.resolution.candidates) == 1
    suggestion = result.resolution.candidates[0]
    assert suggestion.name == "cat" and suggestion.advisory
    assert suggestion.evidence["task"] == "classification"


def test_omni_uses_staged_image_evidence_before_conflicting_page_text(tmp_path):
    class ConflictingOmni:
        def encode_document(self, value):
            if isinstance(value, Image.Image):
                return [1.0, 0.0]
            return [1.0, 0.0] if str(value) == "cat" else [0.0, 1.0]

    path = tmp_path / "cat.png"
    Image.new("RGB", (16, 16), "red").save(path)
    sample = SampleRecord(
        sample_id="mixed",
        modalities=[
            ModalityAsset(Modality.TEXT, text="dog"),
            ModalityAsset(Modality.IMAGE, asset_id="image-1", uri=str(path)),
        ],
        _annotation_image_paths={"image-1": str(path)},
    )
    decisions = JinaOmniAnnotationBackend(
        ["cat", "dog"], model=ConflictingOmni(), top_k=1
    ).annotate_batch([sample])["mixed"]
    assert decisions[0].name == "cat"
    assert decisions[0].evidence["modality"] == "image"
    assert decisions[0].evidence["asset_id"] == "image-1"


def test_advisory_evidence_does_not_replace_an_existing_formal_label():
    policy = LabelPolicy(mode=LabelMode.FIXED, fixed_labels=["cat"])
    result = policy.resolve([
        LabelDecision("cat", 0.8, "reviewed"),
        LabelDecision("cat", 0.95, "jina_omni", advisory=True),
    ])
    assert [item.source for item in result.labels] == ["reviewed"]
    assert [item.source for item in result.candidates] == ["jina_omni"]


def test_retrieval_encoder_uses_document_and_query_sides():
    class FakeRetrieval:
        def __init__(self):
            self.calls = []

        def encode_document(self, value):
            self.calls.append("document")
            return [1.0, 0.0]

        def encode_query(self, value):
            self.calls.append("query")
            return [1.0, 0.0]

    model = FakeRetrieval()
    encoder = JinaOmniRetrievalEncoder(model=model)
    assert encoder.encode_images([Image.new("RGB", (2, 2))]).shape == (1, 2)
    assert encoder.encode_query_image(Image.new("RGB", (2, 2))).shape == (2,)
    assert encoder.encode_query_text("cat").shape == (2,)
    assert model.calls == ["document", "query", "query"]


def test_reranker_v35_uses_returned_indexes_and_records_batch_scope():
    class FakeReranker:
        def rerank(self, query, documents):
            return [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
            ]

    first = _sample("first", "not relevant")
    second = _sample("second", "highly relevant")
    backend = JinaTextRerankBackend(RERANKER_V35, model=FakeReranker())
    result = backend.annotate_batch([first, second])
    assert result == {"first": [], "second": []}
    assert second.pipeline["jina_rerank"]["rank"] == 1
    assert first.pipeline["jina_rerank"]["rank"] == 2
    assert second.pipeline["jina_rerank"]["candidate_count"] == 2
    assert second.pipeline["jina_rerank"]["scope"] == "annotation_batch"


def test_reranker_v2_scores_query_document_pairs():
    class FakeReranker:
        def compute_score(self, pairs, max_length):
            assert max_length == 1024
            assert all(query == "cats" for query, _ in pairs)
            return [0.1, 0.8]

    first = _sample("first", "bird")
    second = _sample("second", "cat")
    JinaTextRerankBackend(RERANKER_V2, model=FakeReranker()).annotate_batch([first, second])
    assert second.pipeline["jina_rerank"]["rank"] == 1
    assert second.pipeline["jina_rerank"]["score"] == 0.8


def test_reranker_failure_does_not_leave_partial_batch_evidence():
    class FakeReranker:
        def rerank(self, query, documents):
            if query == "dogs":
                return [{"index": 99, "relevance_score": 0.9}]
            return [{"index": 0, "relevance_score": 0.8}]

    first = _sample("first", "cat", query="cats")
    second = _sample("second", "dog", query="dogs")
    backend = JinaTextRerankBackend(RERANKER_V35, model=FakeReranker())
    with pytest.raises(ValueError, match="candidate indexes"):
        backend.annotate_batch([first, second])
    assert "jina_rerank" not in first.pipeline
    assert "jina_rerank" not in second.pipeline


def test_orchestrator_annotates_staged_image_without_refetch_and_persists_evidence(tmp_path):
    image = Image.new("RGB", (32, 32), "red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    calls = []

    def fetch(url):
        calls.append(url)
        return buffer.getvalue()

    sample = SampleRecord(
        sample_id="sample-1",
        file="https://example.com/cat.jpg",
        provenance={"query": "cat"},
        modalities=[ModalityAsset(
            Modality.IMAGE, uri="https://example.com/cat.jpg", asset_id="image-1"
        )],
    )
    policy = LabelPolicy(mode=LabelMode.FIXED, fixed_labels=["cat"])
    router = AnnotationRouter(policy, {
        "jina_omni": JinaOmniAnnotationBackend(["cat"], model=FakeOmni())
    })
    job = MultimodalDatasetOrchestrator(
        MultimodalJobConfig(output_dir=str(tmp_path), max_samples=1, label_policy=policy),
        annotation_router=router,
        image_fetcher=fetch,
    )
    candidate = CandidateResource(
        "https://example.com/cat.jpg", "test", modality=Modality.IMAGE, query="cat"
    )
    report = job.run(
        [DiscoveryTask(query="cat")],
        lambda task: SourceResponse(candidates=[candidate], sample=sample),
    )
    assert report.accepted == 1
    assert calls == ["https://example.com/cat.jpg"]
    manifest = json.loads((tmp_path / "manifest.jsonl").read_text().splitlines()[0])
    assert manifest["labels"] == []
    suggestions = manifest["pipeline"]["annotation"]["candidates"]
    assert suggestions[0]["name"] == "cat"
    assert suggestions[0]["evidence"]["modality"] == "image"
    assert suggestions[0]["evidence"]["asset_id"] == "image-1"
    assert "_annotation_image_paths" not in manifest
    assert sample._annotation_image_paths == {}
