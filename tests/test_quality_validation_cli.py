import json

from PIL import Image

from smart_spider.quality_validation_cli import run_validation


class _Detector:
    @property
    def model_versions(self):
        return {"scene_signal_detector": "fixture"}

    def detect(self, image):
        red = image.getpixel((0, 0))[0]
        return {
            "person_detector": 0.9 if red else 0.0,
            "road_vehicle_detector": 0.9,
            "scene_context": 0.9,
            "synthetic_image": 0.01,
            "non_photographic": 0.01,
        }


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_validation_combines_content_and_scene_gates_without_moving_images(tmp_path):
    dataset = tmp_path / "dataset"
    batch = dataset / "batch_0000"
    batch.mkdir(parents=True)
    Image.new("RGB", (32, 32), (255, 0, 0)).save(batch / "person.jpg")
    Image.new("RGB", (32, 32), (0, 0, 0)).save(batch / "vehicle.jpg")
    _write_jsonl(dataset / "metadata.jsonl", [
        {"index": 0, "keyword": "行人", "sim": 0.3, "file_path": "batch_0000/person.jpg"},
        {"index": 1, "keyword": "车辆", "sim": 0.3, "file_path": "batch_0000/vehicle.jpg"},
    ])
    decisions = dataset / "decisions.jsonl"
    _write_jsonl(decisions, [
        {"record_position": 0, "path": "batch_0000/person.jpg", "action": "keep", "reasons": []},
        {"record_position": 1, "path": "batch_0000/vehicle.jpg", "action": "quarantine", "reasons": ["text_heavy"]},
    ])

    report = run_validation(
        dataset,
        decisions,
        _Detector(),
        output_dir=dataset / "validation",
    )

    assert report["final_actions"] == {"accept": 1, "reject": 1}
    assert report["scene_gate_actions"] == {"accept": 2}
    assert (batch / "person.jpg").exists()
    assert (batch / "vehicle.jpg").exists()
    rows = [
        json.loads(line)
        for line in (dataset / "validation" / "decisions.jsonl").read_text().splitlines()
    ]
    assert rows[0]["final_action"] == "accept"
    assert rows[1]["final_action"] == "reject"
    assert rows[1]["content_gate"]["final_effect"] == "reject"


def test_validation_routes_missing_detector_evidence_to_review(tmp_path):
    dataset = tmp_path / "dataset"
    batch = dataset / "batch_0000"
    batch.mkdir(parents=True)
    Image.new("RGB", (32, 32), (0, 0, 0)).save(batch / "person.jpg")
    _write_jsonl(dataset / "metadata.jsonl", [
        {"index": 0, "keyword": "pedestrian", "sim": 0.3, "file_path": "batch_0000/person.jpg"},
    ])
    decisions = dataset / "decisions.jsonl"
    _write_jsonl(decisions, [
        {"record_position": 0, "path": "batch_0000/person.jpg", "action": "keep", "reasons": []},
    ])

    report = run_validation(
        dataset,
        decisions,
        _Detector(),
        output_dir=dataset / "validation",
    )

    assert report["final_actions"] == {"review": 1}
    assert report["scene_reasons"] == {"signal_below_minimum:person_detector": 1}
