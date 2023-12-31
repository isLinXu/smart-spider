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

logger.add("../output.log", format="{time} {level} {message}", level="INFO")


class SmartSpider:
    def __init__(self, keywords, max_pics):
        self.print_logo_str()
        self.keywords = keywords
        self.max_pics = max_pics
        self.ua = UserAgent()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model, self.preprocess = clip.load("ViT-B/32", device=self.device)

    def print_logo_str(self):
        print(f"{logo_str}")

    def is_image_relevant(self, image_content, keyword):
        image = Image.open(BytesIO(image_content))
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

        return similarity.item()

    def spider(self, search_engine, url, keywords, max_pics, pbar):
        headers = {'User-Agent': self.ua.random}
        try:
            r_photos = requests.get(url=url, headers=headers, timeout=2)
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
                # r_photos = requests.get(url=url, headers=headers, timeout=2)
                # r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
                downloaded_pics = 0
                for r_url in r_urls:
                    if downloaded_pics >= max_pics:
                        break
                    try:
                        r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
                        if r_url_get.status_code == 200:
                            image_content = r_url_get.content
                            image_similarity = self.is_image_relevant(image_content, keywords)
                            if image_similarity > 0.20:
                                logger.info(f'当前正在下载的url链接为：{r_url}, 相似度为：{image_similarity}')
                                name = hashlib.md5(r_url.encode()).hexdigest()
                                logger.info('正在保存图片......')
                                filename = os.path.join(keywords, f'{name}.jpg')
                                with open(filename, 'wb') as f:
                                    f.write(image_content)
                                logger.info(f'保存成功，图片名为：{name}.jpg')
                                downloaded_pics += 1
                                pbar.update(1)
                    except Exception as e:
                        pass
            except Exception as e:
                pass
        except Exception as e:
            pass

    # def check_pics_number(self, keywords, max_pics):
    #     headers = {'User-Agent': self.ua.random}
    #     page_num = 1
    #     check_page = 0
    #     total_pics = 0
    #     while total_pics < max_pics:
    #         check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
    #         check_content = requests.get(url=check_url, headers=headers)
    #         logger.info(f'当前第{page_num}页存在')
    #         if '抱歉，没有找到与' in check_content.text:
    #             return page_num
    #         r_urls = re.findall(r'"objURL":"(.*?)"', check_content.text)
    #         total_pics += len(r_urls)
    #         page_num += 1
    #         check_page = ((page_num) * 20) - 20
    #     return page_num

    def check_pics_number(self, search_engine, keywords, max_pics):
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
            else:
                raise ValueError("Unknown search engine")

            total_pics += len(r_urls)
            page_num += 1
            check_page = ((page_num) * 20) - 20
        return page_num


    def create_file(self, keywords):
        if not os.path.exists(keywords):
            os.mkdir(keywords)
        else:
            logger.info(f'已存在以{keywords}关键字命名的文件夹')

    def download_images(self):
        search_engines = ["baidu", "google", "bing", "sogou"]
        for keywords in self.keywords:
            self.create_file(keywords)
            logger.info(f'正在搜索关键字：[{keywords}]一共有多少张图，请稍等。。。')
            # page_num = self.check_pics_number(search_engine, keywords, self.max_pics)
            # total_pics_to_download = min(self.max_pics, (page_num * 20))
            # logger.info(f'关键字[{keywords}]将下载{total_pics_to_download}的图像数量')
            urls = []
            for search_engine in search_engines:
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
                else:
                    raise ValueError("Unknown search engine")

                with tqdm(total=total_pics_to_download, desc=f'Downloading [{keywords}]') as pbar:
                    with ThreadPoolExecutor(max_workers=30) as executor:
                        for url in urls:
                            search_engine = random.choice(search_engines)
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
                        image = os.path.join(image_dir, image)

                        try:
                            image_type = imghdr.what(image)

                            if image_type not in ('jpeg', 'png'):
                                os.remove(image)
                                print(f'已删除：{image}')
                                continue

                            img = np.array(Image.open(image))

                            if len(img.shape) == 2:
                                os.remove(image)
                                print(f'已删除：{image}')
                        except:
                            os.remove(image)
                            print(f'已删除：{image}')
        except:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", nargs="+", default=['皮卡丘', '小火龙', '杰尼龟', '妙蛙种子'], help="关键词列表")
    parser.add_argument("--max_pics", type=int, default=1000, help="每个关键词的最大图片数量")
    args = parser.parse_args()
    image_downloader = SmartSpider(args.keywords, args.max_pics)
    image_downloader.download_images()
