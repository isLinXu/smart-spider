import hashlib
from io import BytesIO
import json

from PIL import Image
import pytest

from smart_spider.industrial_commons import (
    CommonsURLPolicy, candidate_info, collect, license_evidence, load_existing,
    materialize, write_manifest,
    parse_file_page, discover_canonical_pages,
)
from smart_spider.url_policy import UnsafeURLError


@pytest.fixture
def source():
    stream = BytesIO()
    Image.new("RGB", (640, 360), "orange").save(stream, format="JPEG")
    content = stream.getvalue()
    info = {"url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/fixture.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Fixture.jpg",
            "sha1": hashlib.sha1(content).hexdigest(), "mime": "image/jpeg",
            "width": 640, "height": 360, "size": len(content),
            "extmetadata": {key: {"value": value} for key, value in {
                "LicenseShortName": "CC BY 4.0", "LicenseUrl": "https://creativecommons.org/licenses/by/4.0/",
                "Artist": "<a href='https://example.com'>Fixture Creator</a>",
                "UsageTerms": "Creative Commons Attribution 4.0", "Credit": "Own work",
            }.items()}}
    return {"pageid": 123, "title": "File:Fixture.jpg", "index": 1, "imageinfo": [info]}, content


@pytest.mark.parametrize("name,url,identifier", [
    ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
    ("CC BY-SA 3.0", "https://creativecommons.org/licenses/by-sa/3.0", "CC-BY-SA-3.0"),
    ("CC0", "http://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
])
def test_records_source_declaration_without_claiming_training_approval(source, name, url, identifier):
    page, _ = source
    info = page["imageinfo"][0]
    info["extmetadata"]["LicenseShortName"]["value"] = name
    info["extmetadata"]["LicenseUrl"]["value"] = url
    evidence = license_evidence(info)
    assert evidence["license_id"] == identifier and evidence["creator"] == "Fixture Creator"
    assert evidence["status"] == "source_license_declared_review_pending"
    assert not evidence["training_ready"] and evidence["raw_extmetadata"] == info["extmetadata"]


@pytest.mark.parametrize("field,value", [
    ("LicenseUrl", "https://creativecommons.org/licenses/by-nc/4.0/"),
    ("LicenseUrl", "https://creativecommons.org.evil.test/licenses/by/4.0/"),
    ("LicenseUrl", "https://user:secret@creativecommons.org/licenses/by/4.0/"),
    ("LicenseShortName", "CC BY-SA 4.0"), ("Artist", ""), ("UsageTerms", ""),
])
def test_missing_conflicting_or_unsupported_license_is_rejected(source, field, value):
    info = source[0]["imageinfo"][0]
    info["extmetadata"][field]["value"] = value
    with pytest.raises(ValueError):
        license_evidence(info)


def test_creator_widget_does_not_pollute_attribution_with_styles_or_biography(source):
    info = source[0]["imageinfo"][0]
    artist = '<style>.creator{color:red}</style><div class="commons-creator"><span class="fn">Fixture Creator</span><p>Biography and navigation</p></div><div style="display: none">creator QS:123</div>'
    info["extmetadata"]["Artist"]["value"] = artist
    result = license_evidence(info)
    assert result["creator"] == "Fixture Creator"
    assert result["raw_extmetadata"]["Artist"]["value"] == artist


def test_styles_alone_are_not_creator_evidence(source):
    info = source[0]["imageinfo"][0]
    info["extmetadata"]["Artist"]["value"] = "<style>.author{color:red}</style>"
    with pytest.raises(ValueError, match="creator"):
        license_evidence(info)


@pytest.mark.parametrize("url", ["http://upload.wikimedia.org/a.jpg", "https://127.0.0.1/a.jpg",
    "https://upload.wikimedia.org.evil.test/a.jpg", "https://commons.wikimedia.org:8080/a.jpg"])
def test_source_and_redirect_policy_remains_restricted(url):
    with pytest.raises(UnsafeURLError):
        CommonsURLPolicy(resolve_dns=False).validate(url)


def test_metadata_and_actual_bytes_must_agree_before_materializing(tmp_path, source):
    page, content = source
    with pytest.raises(ValueError, match="SHA-1"):
        materialize(tmp_path, "forklift", page, "query", content + b"tampered")
    assert not list(tmp_path.rglob("*.jpg"))
    page["imageinfo"][0]["width"] = 900
    with pytest.raises(ValueError, match="dimensions"):
        materialize(tmp_path, "forklift", page, "query", content)


def test_source_mime_cannot_disguise_decoded_format(tmp_path, source):
    page, content = source
    page["imageinfo"][0]["mime"] = "image/png"
    with pytest.raises(ValueError, match="format"):
        materialize(tmp_path, "forklift", page, "query", content)


def test_resume_rejects_class_directory_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "person").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        load_existing(root)


def test_original_bytes_contract_and_resume_integrity(tmp_path, source):
    page, content = source
    row = materialize(tmp_path, "forklift", page, "irrelevant page title", content)
    assert row.labels == [] and row.task_type == "object_detection"
    assert not row.pipeline["training_ready"]
    asset = tmp_path / "forklift" / row.file
    assert asset.read_bytes() == content
    write_manifest(tmp_path / "forklift/manifest.jsonl", [row])
    records, cache = load_existing(tmp_path)
    assert records["forklift"][0].sample_id == row.sample_id
    assert set(cache.values()) == {asset}
    asset.write_bytes(b"modified")
    with pytest.raises(ValueError, match="hash"):
        load_existing(tmp_path)


def test_cross_class_reuse_and_resume_do_not_refetch_original_bytes(tmp_path, source):
    page, content = source
    (tmp_path / "source_evidence").mkdir()
    class HTTP:
        image_calls = 0
        api_calls = 0
        def get_text(self, url, **kwargs):
            self.api_calls += 1
            return json.dumps({"query": {"pages": [page]}})
        def get_bytes(self, url, **kwargs):
            self.image_calls += 1
            return content
    class Robots:
        def allowed(self, url):
            return True
    http = HTTP()
    queries = {"person": ["workers"], "head": ["workers"]}
    result = collect(tmp_path, queries, http, Robots(), target=1)
    assert result["unique_images"] == 1 and result["per_class"] == {"person": 1, "head": 1}
    assert http.image_calls == http.api_calls == 1
    collect(tmp_path, queries, http, Robots(), target=1)
    assert http.image_calls == http.api_calls == 1


def test_robots_denial_prevents_requests(tmp_path, source):
    (tmp_path / "source_evidence").mkdir()
    class Robots:
        def allowed(self, url):
            return False
    with pytest.raises(RuntimeError, match="robots"):
        collect(tmp_path, {"phone": ["worker phone"]}, object(), Robots(), target=1)


def file_html():
    return '''<div class="fullMedia"><a class="internal" href="https://upload.wikimedia.org/wikipedia/commons/a/ab/fixture.jpg">Original</a>
    <span class="fileInfo">(640 × 360 pixels, <span class="mime-type">image/jpeg</span>)</span></div>
    <table><tr><td id="fileinfotpl_aut">Author</td><td>Fixture Creator</td></tr></table>
    <div class="licensetpl"><span class="licensetpl_short">CC BY 4.0</span>
    <span class="licensetpl_link">https://creativecommons.org/licenses/by/4.0/</span>
    <span class="licensetpl_long">Creative Commons Attribution 4.0</span></div>'''


def test_canonical_file_license_and_original_bytes_are_traceable(tmp_path, source):
    page = parse_file_page(file_html(), "https://commons.wikimedia.org/wiki/File:Fixture.jpg")
    row = materialize(tmp_path, "phone", page, "File:Fixture.jpg", source[1])
    assert row.provenance["license"] == "CC-BY-4.0"
    assert row.provenance["source_html_sha256"] == hashlib.sha256(file_html().encode()).hexdigest()
    assert row.provenance["source_sha1"] == hashlib.sha1(source[1]).hexdigest()
    assert row.provenance["source_sha1_verified_against_api"] is False
    assert row.labels == [] and not row.pipeline["training_ready"]
    page["imageinfo"][0]["width"] = 800
    with pytest.raises(ValueError, match="dimensions"):
        materialize(tmp_path, "phone", page, "File:Fixture.jpg", source[1])


def test_site_footer_license_is_not_a_file_license():
    with pytest.raises(ValueError, match="per-file license"):
        parse_file_page(file_html().replace('class="licensetpl"', 'class="footer"'),
                        "https://commons.wikimedia.org/wiki/File:Fixture.jpg")


def test_canonical_discovery_is_cached_and_only_uses_file_gallery(tmp_path):
    (tmp_path / "source_evidence").mkdir()
    class HTTP:
        calls = 0
        def get_text(self, url, **kwargs):
            assert "/wiki/Category:" in url and "?" not in url
            self.calls += 1
            return '''<a href="/wiki/File:Outside.jpg">outside</a><div class="gallerytext">
            <a href="/wiki/File:Inside.jpg">image</a><a href="/wiki/User:Someone">author</a>
            <a href="/wiki/File:Inside.jpg">duplicate</a></div>'''
    class Robots:
        def allowed(self, url):
            return True
    http = HTTP()
    pages = discover_canonical_pages(tmp_path, "Category:Forklifts", http, Robots(), 30)
    assert [page["title"] for page in pages] == ["File:Inside.jpg"]
    assert discover_canonical_pages(tmp_path, "Category:Forklifts", http, Robots(), 30) == pages
    assert http.calls == 1
