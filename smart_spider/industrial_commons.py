"""Collect unlabelled industrial candidates with per-file Commons license evidence.

License metadata is a source declaration, not a completed legal or visual review.
Original bytes are retained; the existing local-image review CLI can score them.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

from .dataset_contracts import QualityMetrics, SampleRecord, utc_now
from .image_safety import decode_image_bytes
from .industrial_review import CLASS_NAMES, file_sha256
from .url_policy import URLPolicy, UnsafeURLError

API = "https://commons.wikimedia.org/w/api.php"
MAX_BYTES = 12 * 1024 * 1024
USER_AGENT = "smart-spider-bot/1.0 (https://github.com/isLinXu/smart-spider)"


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(str(value))
    return " ".join(" ".join(parser.parts).split())


def creator_text(value):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(str(value), "html.parser")
    for card in soup.select(".commons-creator"):
        name = card.select_one(".fn")
        if name:
            card.replace_with(name.get_text(" ", strip=True))
    for tag in soup.select("[style]"):
        if "display:none" in tag.get("style", "").replace(" ", "").lower():
            tag.decompose()
    return plain(str(soup))


class CommonsURLPolicy(URLPolicy):
    def _parse(self, url):
        value, parsed, host = super()._parse(url)
        if parsed.scheme != "https" or host not in {"commons.wikimedia.org", "upload.wikimedia.org"} or parsed.port not in (None, 443):
            raise UnsafeURLError("Commons collection only permits HTTPS Commons and upload hosts")
        return value, parsed, host


def license_evidence(info: dict) -> dict:
    metadata = info.get("extmetadata", {})
    value = lambda key: plain(metadata.get(key, {}).get("value", ""))
    url = value("LicenseUrl")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.hostname != "creativecommons.org" or parts.username or parts.password or parts.query or parts.fragment or parts.port not in (None, 80, 443):
        raise ValueError("missing or unsupported license URL")
    match = re.fullmatch(r"/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)/?", parts.path)
    if match:
        identifier = f"CC-{match[1].upper()}-{match[2]}"
        expected = identifier.replace("-", " ")
        if value("LicenseShortName").replace("-", " ").upper() != expected:
            raise ValueError("license name and URL disagree")
    elif parts.path.rstrip("/") == "/publicdomain/zero/1.0" and value("LicenseShortName").upper() == "CC0":
        identifier = "CC0-1.0"
    else:
        raise ValueError("license outside reviewed CC BY / CC BY-SA / CC0 policy")
    creator = creator_text(metadata.get("Artist", {}).get("value", ""))
    if not creator or not value("UsageTerms"):
        raise ValueError("missing creator or license terms")
    return {"status": "source_license_declared_review_pending", "license_id": identifier,
            "license_url": url, "creator": creator, "credit": value("Credit"),
            "usage_terms": value("UsageTerms"), "attribution_required": identifier != "CC0-1.0",
            "share_alike": identifier.startswith("CC-BY-SA"), "restrictions": value("Restrictions"),
            "source_page": info["descriptionurl"], "checked_at_utc": utc_now(),
            "raw_extmetadata": metadata, "training_ready": False}


def candidate_info(page: dict) -> tuple[dict, dict]:
    info = page["imageinfo"][0]
    if info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("unsupported raster format")
    html_source = info.get("metadata_origin") == "commons_canonical_html" and bool(re.fullmatch(r"[0-9a-f]{64}", info.get("source_html_sha256", "")))
    if (not html_source and not 1024 <= info["size"] <= MAX_BYTES) or info["width"] < 640 or info["height"] < 360 or info["width"] * info["height"] > 50_000_000:
        raise ValueError("metadata size or dimension budget")
    syntax = CommonsURLPolicy(resolve_dns=False)
    syntax.validate(info["url"])
    syntax.validate(info["descriptionurl"])
    if urlsplit(info["url"]).hostname != "upload.wikimedia.org" or (not html_source and not re.fullmatch(r"[0-9a-f]{40}", info.get("sha1", ""))):
        raise ValueError("missing original media hash or upload URL")
    return info, license_evidence(info)


def canonical_page(title):
    if not title.startswith(("Category:", "File:")):
        raise ValueError("expected a Commons category or file title")
    return "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":")


def parse_file_page(content, url):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")
    original = soup.select_one(".fullMedia a.internal")
    dimensions = soup.select_one(".fullMedia .fileInfo")
    author = soup.select_one("#fileinfotpl_aut")
    mime = soup.select_one(".fullMedia .mime-type")
    if not all((original, dimensions, author, mime)):
        raise ValueError("missing original file, dimensions, author or MIME")
    match = re.search(r"([\d,]+)\s*×\s*([\d,]+)\s*pixels", dimensions.get_text())
    if not match:
        raise ValueError("cannot parse original dimensions")
    credit = soup.select_one("#fileinfotpl_src")
    for license_block in soup.select(".licensetpl"):
        metadata = {}
        for field, selector in (("LicenseUrl", ".licensetpl_link"), ("LicenseShortName", ".licensetpl_short"), ("UsageTerms", ".licensetpl_long")):
            tag = license_block.select_one(selector)
            metadata[field] = {"value": tag.get_text(" ", strip=True) if tag else ""}
        metadata["Artist"] = {"value": str(author.find_next_sibling("td") or "")}
        metadata["Credit"] = {"value": str(credit.find_next_sibling("td") or "") if credit else ""}
        metadata["Restrictions"] = {"value": "See saved source page for non-copyright restrictions; not reviewed"}
        info = {"url": urljoin(url, original["href"]), "descriptionurl": url,
                "width": int(match[1].replace(",", "")), "height": int(match[2].replace(",", "")),
                "size": None, "sha1": None, "mime": mime.get_text(strip=True), "extmetadata": metadata,
                "metadata_origin": "commons_canonical_html", "source_html_sha256": hashlib.sha256(content.encode()).hexdigest()}
        try:
            license_evidence(info)
        except ValueError:
            continue
        return {"title": unquote(urlsplit(url).path.removeprefix("/wiki/")).replace("_", " "), "pageid": url, "imageinfo": [info]}
    raise ValueError("no supported per-file license declaration")


def cached_page(root, title, http, robots):
    url = canonical_page(title)
    snapshot = root / "source_evidence" / (hashlib.sha256(url.encode()).hexdigest() + ".html")
    if snapshot.exists():
        return snapshot.read_text(encoding="utf-8"), url
    if not robots.allowed(url):
        raise ValueError("canonical page robots policy denied request")
    content = http.get_text(url, max_bytes=4 * 1024 * 1024)
    snapshot.write_text(content, encoding="utf-8")
    return content, url


def discover_canonical_pages(root, title, http, robots, limit):
    if title.startswith("File:"):
        return [{"title": title, "pageid": canonical_page(title)}]
    from bs4 import BeautifulSoup
    content, _ = cached_page(root, title, http, robots)
    soup = BeautifulSoup(content, "html.parser")
    titles = []
    for tag in soup.select(".gallerytext a[href]"):
        path = unquote(urlsplit(tag["href"]).path)
        if path.startswith("/wiki/File:"):
            name = path.removeprefix("/wiki/").replace("_", " ")
            if name not in titles:
                titles.append(name)
    return [{"title": name, "pageid": canonical_page(name)} for name in titles[:limit]]


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_manifest(path, records):
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text("".join(row.to_json() + "\n" for row in records), encoding="utf-8")
    temporary.replace(path)


def load_existing(root):
    records, cache = {}, {}
    for name in CLASS_NAMES:
        directory = (root / name).resolve()
        directory.relative_to(root.resolve())
        path = directory / "manifest.jsonl"
        records[name] = []
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = SampleRecord.from_dict(json.loads(line))
            relative = Path(row.file)
            if relative.is_absolute():
                raise ValueError("absolute manifest asset path")
            asset = (directory / relative).resolve()
            asset.relative_to(directory.resolve())
            if "sha256:" + file_sha256(asset) != row.sample_id:
                raise ValueError("local asset hash mismatch; refusing resume")
            records[name].append(row)
            cache[row.provenance["source_sha1"]] = asset
            cache[row.provenance["url"]] = asset
    return records, cache


def materialize(root, name, page, query, content):
    info, evidence = candidate_info(page)
    if not 1024 <= len(content) <= MAX_BYTES or (info["size"] is not None and len(content) != info["size"]) or (info["sha1"] is not None and hashlib.sha1(content).hexdigest() != info["sha1"]):
        raise ValueError("original bytes do not match API size and SHA-1")
    image = decode_image_bytes(content, max_pixels=50_000_000)
    try:
        if image.format != {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[info["mime"]]:
            raise ValueError("decoded format does not match source MIME")
        if (image.width, image.height) != (info["width"], info["height"]):
            raise ValueError("decoded dimensions do not match metadata")
        quality = QualityMetrics(width=image.width, height=image.height, file_size=len(content),
                                 format=info["mime"].split("/")[1], validated=True)
    finally:
        image.close()
    digest = hashlib.sha256(content).hexdigest()
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[info["mime"]]
    relative = "images/" + digest + suffix
    path = root / name / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    return SampleRecord("sha256:" + digest, file=relative, task_type="object_detection", quality=quality,
        provenance={"source": "wikimedia_commons", "url": info["url"], "source_page": info["descriptionurl"],
                    "query": query, "title": page["title"], "page_id": page["pageid"],
                    "source_sha1": hashlib.sha1(content).hexdigest(), "source_sha1_verified_against_api": info["sha1"] is not None,
                    "source_html_sha256": info.get("source_html_sha256"), "license": evidence["license_id"],
                    "license_evidence": evidence, "respect_robots": True},
        pipeline={"status": "unlabelled_candidate", "training_ready": False,
                  "retrieval_class": name, "source_bytes_unchanged": True})


def collect(root, queries, http, robots, *, target=30, results_per_query=30):
    records, cache = load_existing(root)
    rejects = []
    for name, phrases in queries.items():
        seen_ids = {row.sample_id for row in records[name]}
        seen_pages = {row.provenance["page_id"] for row in records[name]}
        for query in phrases:
            if len(seen_ids) >= target:
                break
            url = API + "?" + urlencode({"action": "query", "generator": "search", "gsrsearch": query,
                "gsrnamespace": 6, "gsrlimit": results_per_query, "prop": "imageinfo", "format": "json",
                "formatversion": 2, "iiprop": "url|size|mime|sha1|extmetadata", "maxlag": 5})
            if query.startswith(("File:", "Category:")):
                data = {"query": {"pages": discover_canonical_pages(root, query, http, robots, results_per_query)}}
            elif not robots.allowed(url):
                raise RuntimeError("Commons API robots policy did not allow this request")
            else:
                data = None
            snapshot = root / "source_evidence" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            if data is None and snapshot.exists():
                data = json.loads(snapshot.read_text(encoding="utf-8"))["response"]
            elif data is None:
                data = json.loads(http.get_text(url, max_bytes=4 * 1024 * 1024))
                if "error" in data:
                    raise RuntimeError(f"Commons API error: {data['error']}")
                write_json(snapshot, {"request_url": url, "retrieved_at_utc": utc_now(), "response": data})
            for page in sorted(data.get("query", {}).get("pages", []), key=lambda row: row.get("index", 0)):
                if len(seen_ids) >= target:
                    break
                if page["pageid"] in seen_pages:
                    continue
                seen_pages.add(page["pageid"])
                try:
                    if "imageinfo" not in page:
                        content, page_url = cached_page(root, page["title"], http, robots)
                        page = parse_file_page(content, page_url)
                    info, _ = candidate_info(page)
                    cached = cache.get(info["sha1"]) or cache.get(info["url"])
                    if cached:
                        content = cached.read_bytes()
                    else:
                        if not robots.allowed(info["url"]):
                            raise ValueError("media robots policy denied download")
                        started = time.monotonic()
                        content = http.get_bytes(info["url"], max_bytes=MAX_BYTES)
                        time.sleep(max(0, len(content) / 2_000_000 - (time.monotonic() - started)))
                    row = materialize(root, name, page, query, content)
                    cache[row.provenance["source_sha1"]] = root / name / row.file
                    cache[info["url"]] = root / name / row.file
                    if row.sample_id not in seen_ids:
                        records[name].append(row)
                        seen_ids.add(row.sample_id)
                        write_manifest(root / name / "manifest.jsonl", records[name])
                        print(f"{name}: {len(seen_ids)}/{target}", flush=True)
                except (ValueError, KeyError, OSError, RuntimeError) as exc:
                    rejects.append({"class": name, "page_id": page["pageid"], "title": page["title"], "reason": str(exc)})
            print(f"query completed: {name} {query!r}; candidates={len(seen_ids)}", flush=True)
    all_records = [row for rows in records.values() for row in rows]
    summary = {"training_ready": False, "counts_are": "unreviewed retrieval candidates with source license declarations",
               "per_class": {name: len(records[name]) for name in queries},
               "target_per_class": target, "unique_images": len({row.sample_id for row in all_records}),
               "license_counts_by_membership": dict(Counter(row.provenance["license"] for row in all_records)),
               "rejected_candidates_this_run": len(rejects), "formal_training_labels": 0}
    write_json(root / "rejected_candidates.json", rejects)
    write_json(root / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=Path(__file__).resolve().parents[1] / "examples/industrial_commons_queries.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-per-class", type=int, default=30)
    parser.add_argument("--results-per-query", type=int, default=30)
    parser.add_argument("--proxy")
    args = parser.parse_args(argv)
    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    if not isinstance(queries, dict) or not queries or set(queries) - set(CLASS_NAMES) or any(not isinstance(v, list) or not v or any(not isinstance(q, str) or not q.strip() for q in v) for v in queries.values()):
        parser.error("queries must map known classes to non-empty string lists")
    if not 1 <= args.results_per_query <= 50 or args.target_per_class <= 0:
        parser.error("positive target and 1..50 results per query required")
    if args.proxy:
        proxy = urlsplit(args.proxy)
        if proxy.scheme not in {"http", "https"} or not proxy.hostname or proxy.username or proxy.password or proxy.path not in ("", "/") or proxy.query or proxy.fragment:
            parser.error("proxy must be HTTP(S) without credentials, path or query")
    from .http_client import SmartHttpClient
    from .site_policy import RobotsGate
    class CommonsHTTP(SmartHttpClient):
        def _make_headers(self, engine=None):
            return {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate", "Accept": "*/*"}
    policy = CommonsURLPolicy()
    http = CommonsHTTP(proxies=[args.proxy] if args.proxy else None, rate=1, max_retries=1,
                       timeout=15, connect_timeout=5, read_timeout=15, use_curl_cffi=False, url_policy=policy)
    def robots_fetch(url):
        try:
            response = http.get(url, stream=True)
            if 400 <= response.status_code < 500 and response.status_code != 429:
                response.close()
                return ""
            return http.read_bounded_bytes(response, max_bytes=512 * 1024).decode("utf-8")
        except Exception:
            return None
    robots = RobotsGate(user_agent="smart-spider", fetcher=robots_fetch, url_policy=policy)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = {"queries": queries, "results_per_query": args.results_per_query, "source": API}
        plan_path = root / "collection_plan.json"
        if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
            parser.error("existing collection plan differs; choose another output directory")
        write_json(plan_path, plan)
        (root / "source_evidence").mkdir(exist_ok=True)
        summary = collect(root, queries, http, robots, target=args.target_per_class, results_per_query=args.results_per_query)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(count >= args.target_per_class for count in summary["per_class"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
