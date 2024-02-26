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


from .utils import create_file, print_logo_str, is_image_downloaded

# from .downloader import check_pics_number

logger.add("../output.log", format="{time} {level} {message}", level="INFO")


class SmartSpider:
    def __init__(self, keywords, max_pics, similarity_threshold=0.20, timeout=5, max_workers=30, search_engines = []):

        self.logo = print_logo_str()
        self.search_engines = search_engines
        self.keywords = keywords
        self.max_pics = max_pics
        self.similarity_threshold = similarity_threshold
        self.timeout = timeout
        self.max_workers = max_workers
        self.ua = UserAgent()
        self.downloaded_images = set()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model, self.preprocess = clip.load("ViT-B/32", device=self.device)


    def is_image_relevant(self, image_content, keyword):
        '''
        判断图片是否与关键字相关
        :param image_content:
        :param keyword:
        :return:
        '''
        try:
            image = Image.open(BytesIO(image_content))
        except Exception as e:
            logger.error(f"Error opening image: {e}")
            return False

        # 检查图片的尺寸
        if image.width < 100 or image.height < 100:
            logger.info("Image size is too small, skipping")
            return False

        # 检查图片的颜色
        image_array = np.array(image)
        if np.std(image_array) < 10:
            logger.info("Image color is too uniform, skipping")
            return False

        # 以下是原来的代码
        image_transform = Compose([
            Resize(256),
            CenterCrop(224),
            ToTensor(),
            Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))
        ])

        image_tensor = image_transform(image).unsqueeze(0).to(self.device)
        keyword_tensor = clip.tokenize([keyword]).to(self.device)

        with torch.no_grad():
            image_features = self.model.encode_image(image_tensor)
            keyword_features = self.model.encode_text(keyword_tensor)
            similarity = torch.nn.functional.cosine_similarity(image_features, keyword_features)
            logger.info(f"Similarity: {similarity.item()}")
        return similarity.item() > self.similarity_threshold



    def spider(self, search_engine, url, keywords, max_pics, pbar):
        headers = {'User-Agent': self.ua.random}
        try:
            r_photos = requests.get(url=url, headers=headers, timeout=self.timeout)
            if search_engine == "baidu":
                r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
            elif search_engine == "google":
                r_urls = re.findall(r'"ou":"(.*?)"', r_photos.text)
            elif search_engine == "bing":
                r_urls = re.findall(r'src="(.*?)"', r_photos.text)
            elif search_engine == "sogou":
                r_urls = re.findall(r'"thumbUrl":"(.*?)"', r_photos.text)
            else:
                raise ValueError("Unknown search engine")
            try:
                downloaded_pics = 0
                for r_url in r_urls:
                    if downloaded_pics >= max_pics:
                        break
                    try:
                        if is_image_downloaded(r_url, self.downloaded_images):
                            logger.info(f"Image already downloaded: {r_url}")
                            continue
                        r_url_get = requests.get(url=r_url, headers=headers, timeout=self.timeout)
                        if r_url_get.status_code == 200:
                            image_content = r_url_get.content
                            if self.is_image_relevant(image_content, keywords):
                                logger.info(f'当前正在下载的url链接为：{r_url}')
                                name = hashlib.md5(r_url.encode()).hexdigest()
                                logger.info('正在保存图片......')
                                filename = os.path.join(keywords, f'{name}.jpg')
                                with open(filename, 'wb') as f:
                                    f.write(image_content)
                                logger.info(f'保存成功，图片名为：{name}.jpg')
                                downloaded_pics += 1
                                pbar.update(1)
                    except Exception as e:
                        logger.error(f"Error downloading image: {e}")
            except Exception as e:
                logger.error(f"Error processing image URLs: {e}")
        except Exception as e:
            logger.error(f"Error fetching image search results: {e}")

    def check_pics_number(self, search_engine, keywords, max_pics):
        '''
        检查图片数量
        :param search_engine:
        :param keywords:
        :param max_pics:
        :return:
        '''
        headers = {'User-Agent': self.ua.random}
        page_num = 1
        check_page = 0
        total_pics = 0
        while total_pics < max_pics:
            if search_engine == "baidu":
                check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
            elif search_engine == "google":
                check_url = f'https://www.google.com/search?q={keywords}&tbm=isch&start={check_page}'
            elif search_engine == "bing":
                check_url = f'https://www.bing.com/images/search?q={keywords}&first={check_page}'
            elif search_engine == "sogou":
                check_url = f'https://pic.sogou.com/pics?query={keywords}&start={check_page}'
            elif search_engine == "360":
                check_url = f'https://image.so.com/i?q={keywords}&pn={check_page}'
            else:
                raise ValueError("Unknown search engine")

            check_content = requests.get(url=check_url, headers=headers)
            logger.info(f'正在搜索{search_engine}中，当前第{page_num}页存在...')

            if search_engine == "baidu":
                r_urls = re.findall(r'"objURL":"(.*?)"', check_content.text)
            elif search_engine == "google":
                r_urls = re.findall(r'"ou":"(.*?)"', check_content.text)
            elif search_engine == "bing":
                r_urls = re.findall(r'src="(.*?)"', check_content.text)
            elif search_engine == "sogou":
                r_urls = re.findall(r'"thumbUrl":"(.*?)"', check_content.text)
            elif search_engine == "360":
                r_urls = re.findall(r'"img":"(.*?)"', check_content.text)
            else:
                raise ValueError("Unknown search engine")

            total_pics += len(r_urls)
            page_num += 1
            check_page = ((page_num) * 20) - 20
        return page_num

    def download_images(self):
        '''
        下载图片
        :return:
        '''
        # self.search_engines = ["baidu", "bing", "sogou", "google", "360"]
        for keywords in self.keywords:
            create_file(keywords)
            logger.info(f'正在搜索关键字：[{keywords}]一共有多少张图，请稍等。。。')
            urls = []
            for search_engine in self.search_engines:
                page_num = self.check_pics_number(search_engine, keywords, self.max_pics)
                total_pics_to_download = min(self.max_pics, (page_num * 20))
                logger.info(f'关键字[{keywords}]将下载{total_pics_to_download}的图像数量')
                if search_engine == "baidu":
                    urls.extend(
                        [f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}' for j in
                         range(0, page_num * 20, 20)])
                elif search_engine == "google":
                    urls.extend([f'https://www.google.com/search?q={keywords}&tbm=isch&start={j}' for j in
                                 range(0, page_num * 20, 20)])
                elif search_engine == "bing":
                    urls.extend([f'https://www.bing.com/images/search?q={keywords}&first={j}' for j in
                                 range(0, page_num * 20, 20)])
                elif search_engine == "sogou":
                    urls.extend([f'https://pic.sogou.com/pics?query={keywords}&start={j}' for j in
                                 range(0, page_num * 20, 20)])
                elif search_engine == "360":
                    urls.extend([f'https://image.so.com/i?q={keywords}&pn={j}' for j in range(0, page_num * 20, 20)])
                else:
                    raise ValueError("Unknown search engine")

                with tqdm(total=total_pics_to_download, desc=f'Downloading [{keywords}]') as pbar:
                    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                        for url in urls:
                            search_engine = random.choice(self.search_engines)
                            executor.submit(self.spider, search_engine, url, keywords, self.max_pics, pbar)

        logger.info(f'下载完成！')

    def delete_error_image(self, father_path):

        try:
            image_dirs = os.listdir(father_path)
            for image_dir in image_dirs:
                image_dir = os.path.join(father_path, image_dir)

                if os.path.isdir(image_dir):
                    images = os.listdir(image_dir)

                    for image in images:
                        image_path = os.path.join(image_dir, image)
                        try:
                            image_type = imghdr.what(image_path)

                            if image_type not in ('jpeg', 'png'):
                                os.remove(image_path)
                                logger.info(f'已删除：{image_path}')
                                continue

                            img = np.array(Image.open(image_path))

                            if len(img.shape) == 2:
                                os.remove(image_path)
                                logger.info(f'已删除：{image_path}')
                        except Exception as e:
                            os.remove(image_path)
                            logger.error(f"Error processing image: {e}")
                            logger.info(f'已删除：{image_path}')
        except Exception as e:
            logger.error(f"Error processing image directories: {e}")
    # def run(self):
    #     downloader = Downloader()
    #     downloader.download_images(self.search_engines, self.keywords, self.max_pics, self.max_workers)

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
    image_downloader.download_images()
    # image_downloader.run()