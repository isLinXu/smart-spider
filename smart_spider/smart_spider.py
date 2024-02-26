# coding=utf-8
import argparse
import hashlib
import imghdr
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import clip
import numpy as np
import requests
import torch
from fake_useragent import UserAgent
from loguru import logger

from PIL import Image
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
from tqdm import tqdm

from smart_spider import logo_str
from .downloader import Downloader
from .utils import create_file
# from .downloader import check_pics_number

logger.add("../output.log", format="{time} {level} {message}", level="INFO")


class SmartSpider:
    def __init__(self, keywords, max_pics, similarity_threshold=0.20, timeout=5, max_workers=30, search_engines = []):
        self.keywords = keywords
        self.max_pics = max_pics
        self.similarity_threshold = similarity_threshold
        self.timeout = timeout
        self.max_workers = max_workers
        self.search_engines = search_engines

    def run(self):
        downloader = Downloader()
        downloader.download_images(self.search_engines, self.keywords,
                                   self.max_pics, self.max_workers,
                                   self.similarity_threshold, self.timeout)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", nargs="+", default=['皮卡丘', '小火龙', '杰尼龟', '妙蛙种子'], help="关键词列表")
    parser.add_argument("--max_pics", type=int, default=1000, help="每个关键词的最大图片数量")
    parser.add_argument("--similarity_threshold", type=float, default=0.20, help="相似度阈值")
    parser.add_argument("--timeout", type=int, default=5, help="请求超时时间")
    parser.add_argument("--max_workers", type=int, default=30, help="线程池最大工作线程数")
    parser.add_argument("--search_engines", nargs="+", default=["baidu", "bing", "sogou"], help="搜索引擎列表")
    args = parser.parse_args()
    image_downloader = SmartSpider(args.keywords, args.max_pics,
                                   args.similarity_threshold, args.timeout,
                                   args.max_workers, args.search_engines)
    # image_downloader.download_images()
    image_downloader.run()