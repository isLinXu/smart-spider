import io

import pytest
from PIL import Image

from smart_spider.xiaohongshu_cli import image_record, main


def encoded(size=(640, 850)):
    buf = io.BytesIO()
    Image.new('RGB', size, 'pink').save(buf, format='WEBP')
    return buf.getvalue()


def test_download_validation_and_deduplication(tmp_path):
    first = image_record(encoded(), tmp_path, 'note1')
    second = image_record(encoded(), tmp_path, 'note2')
    assert first['sha256'] == second['sha256']
    assert first['width'] == 640 and first['height'] == 850
    assert first['format'] == 'WEBP'
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('data', [b'<html>login required</html>', encoded((80, 80))])
def test_reject_invalid_or_avatar_image(tmp_path, data):
    with pytest.raises((ValueError, OSError)):
        image_record(data, tmp_path, 'note')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('args', [['--max-items', '0'], ['--headless', '--wait-for-login']])
def test_invalid_arguments(args):
    with pytest.raises(SystemExit) as exc:
        main(['--keyword', 'test', *args])
    assert exc.value.code == 2
