# coding=utf-8
"""SmartSpider 工具函数模块。"""
import hashlib
import os

from loguru import logger

_LOGO = r"""
  ____                       _   ____        _     _
 / ___| _ __ ___   __ _ _ __| |_/ ___| _ __ (_) __| | ___ _ __
 \___ \| '_ ` _ \ / _` | '__| __\___ \| '_ \| |/ _` |/ _ \ '__|
  ___) | | | | | | (_| | |  | |_ ___) | |_) | | (_| |  __/ |
 |____/|_| |_| |_|\__,_|_|   \__|____/| .__/|_|\__,_|\___|_|
                                       |_|   CLIP-powered
"""


def print_logo_str():
    """打印 ASCII logo。"""
    print(_LOGO)


def create_file(path: str):
    """创建目录（如果不存在）。"""
    os.makedirs(path, exist_ok=True)


def is_image_downloaded(url: str, downloaded_images: set) -> bool:
    """判断图片 URL 是否已经处理过（线程安全需由调用方加锁）。

    会修改 downloaded_images 集合（添加新条目），
    在多线程环境下调用方需自行加锁保护。
    """
    url_hash = hashlib.md5(url.encode()).hexdigest()
    if url_hash in downloaded_images:
        return True
    downloaded_images.add(url_hash)
    return False
