"""Standalone Xiaohongshu search-cover collector with a private login profile."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from PIL import Image

CARD_SCRIPT = """() => Array.from(document.querySelectorAll('section.note-item')).map(e => ({
 title: (e.querySelector('.title')?.innerText || '').trim(),
 source: e.querySelector('a.cover')?.href,
 image: e.querySelector('a.cover img')?.currentSrc || e.querySelector('a.cover img')?.src
})).filter(e => e.source && e.image)"""


def image_record(data, output, note_id):
    """Decode before writing, deduplicate by content, and use the actual format."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        width, height = im.size
        fmt = im.format
    if width < 200 or height < 200:
        raise ValueError('image is too small for a search cover')
    suffix = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}.get(fmt)
    if not suffix:
        raise ValueError('unsupported image format')
    digest = hashlib.sha256(data).hexdigest()
    path = output / f'{digest}.{suffix}'
    if not path.exists():
        temp = path.with_suffix('.part')
        temp.write_bytes(data)
        temp.replace(path)
    return dict(file=path.name, note_id=note_id, sha256=digest,
                width=width, height=height, bytes=len(data), format=fmt)


def main(argv=None):
    parser = argparse.ArgumentParser(description='小红书关键词搜索封面采集（不包含笔记完整相册）')
    parser.add_argument('--keyword', required=True)
    parser.add_argument('--max-items', type=int, default=50)
    parser.add_argument('--max-scrolls', type=int, default=60)
    parser.add_argument('--output', default='output_xiaohongshu')
    parser.add_argument('--profile', default='.artifacts/xiaohongshu-profile')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--wait-for-login', action='store_true')
    parser.add_argument('--browser-channel', default='chrome', choices=['chrome', 'msedge', 'chromium'])
    args = parser.parse_args(argv)
    if args.max_items < 1 or args.max_scrolls < 1:
        parser.error('max-items 和 max-scrolls 必须大于 0')
    if args.headless and args.wait_for_login:
        parser.error('人工登录需要显示浏览器')
    from playwright.sync_api import sync_playwright
    from .douyin_cli import _confirm_login

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    report = dict(keyword=args.keyword, scope='search_covers', requested=args.max_items,
                  downloaded=0, failures=[], items=[], status='running')
    seen_notes, hashes = set(), set()

    def save():
        report['downloaded'] = len(report['items'])
        tmp = output / 'results.json.tmp'
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(output / 'results.json')

    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(profile), headless=args.headless,
                channel=None if args.browser_channel == 'chromium' else args.browser_channel,
                viewport={'width': 1440, 'height': 1000})
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                if args.wait_for_login:
                    _confirm_login(page)
                url = 'https://www.xiaohongshu.com/search_result?' + urlencode(
                    {'keyword': args.keyword, 'source': 'web_explore_feed'})
                page.goto(url, wait_until='domcontentloaded')
                try:
                    page.wait_for_selector('section.note-item a.cover img', timeout=30000)
                except Exception:
                    raise RuntimeError('未看到搜索结果，请使用 --wait-for-login 完成专用浏览器登录后重试')
                stale = 0
                for _ in range(args.max_scrolls):
                    cards = page.evaluate(CARD_SCRIPT)
                    before = len(seen_notes)
                    for card in cards:
                        source = urlsplit(card['source'])
                        note_id = source.path.rsplit('/', 1)[-1]
                        if not note_id.isalnum() or note_id in seen_notes:
                            continue
                        image = urlsplit(card['image'])
                        if image.scheme != 'https' or not (image.hostname or '').endswith('.xhscdn.com'):
                            continue
                        seen_notes.add(note_id)
                        try:
                            response = context.request.get(card['image'], headers={'Referer': page.url}, timeout=30000)
                            try:
                                if not response.ok:
                                    raise ValueError(f'HTTP {response.status}')
                                data = response.body()
                                if len(data) > 20 * 1024 * 1024:
                                    raise ValueError('image exceeds 20 MB')
                                record = image_record(data, output, note_id)
                            finally:
                                response.dispose()
                            if record['sha256'] not in hashes:
                                hashes.add(record['sha256'])
                                record.update(title=card['title'], source_url=f'https://www.xiaohongshu.com/explore/{note_id}')
                                report['items'].append(record)
                                save()
                                print(f"已保存 {len(hashes)}/{args.max_items}", flush=True)
                        except Exception as exc:
                            report['failures'].append({'note_id': note_id, 'error': type(exc).__name__})
                        if len(hashes) >= args.max_items:
                            break
                    if len(hashes) >= args.max_items:
                        break
                    stale = stale + 1 if len(seen_notes) == before else 0
                    if stale >= 5:
                        break
                    page.mouse.wheel(0, 850)
                    page.wait_for_timeout(1800)
                report['status'] = 'complete' if len(hashes) >= args.max_items else 'partial'
            finally:
                context.close()
    except Exception as exc:
        report['status'] = 'error'
        report['error'] = str(exc)
    finally:
        save()
    print(json.dumps({k: v for k, v in report.items() if k not in ('items', 'failures')}, ensure_ascii=False))
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
