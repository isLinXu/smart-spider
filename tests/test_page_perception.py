# coding=utf-8
"""PagePerception 的 HTTP response/bytes 边界测试。"""
import io

from PIL import Image

from smart_spider.perception.page_perception import PagePerception


def _jpeg_bytes():
    image = Image.new("RGB", (32, 32), (20, 80, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class _BytesClient:
    def get_bytes(self, url):
        return _jpeg_bytes()


class _ResponseClient:
    class Response:
        content = _jpeg_bytes()

    def get(self, url):
        return self.Response()


def test_download_and_open_image_uses_get_bytes():
    perception = PagePerception(enable_clip=False)
    image = perception._download_and_open_image(
        "https://img.example.com/a.jpg",
        http_client=_BytesClient(),
    )
    assert image is not None
    assert image.size == (32, 32)


def test_download_and_open_image_accepts_response_fallback():
    perception = PagePerception(enable_clip=False)
    image = perception._download_and_open_image(
        "https://img.example.com/a.jpg",
        http_client=_ResponseClient(),
    )
    assert image is not None
    assert image.mode == "RGB"


def test_download_and_open_image_rejects_unsafe_urllib_fallback_url():
    perception = PagePerception(enable_clip=False)
    assert perception._download_and_open_image("file:///tmp/image.jpg") is None
