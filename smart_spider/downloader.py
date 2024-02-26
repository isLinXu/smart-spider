import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from fake_useragent import UserAgent
from loguru import logger
from tqdm import tqdm

from .utils import create_file, is_image_relevant, is_image_downloaded, print_logo_str


class Downloader:
    def __init__(self):
        self.logo = print_logo_str()
        self.ua = UserAgent()

    def get_image_urls(self, search_engine, content):
        if search_engine == "baidu":
            return re.findall(r'"objURL":"(.*?)"', content)
        elif search_engine == "google":
            return re.findall(r'"ou":"(.*?)"', content)
        elif search_engine == "bing":
            return re.findall(r'src="(.*?)"', content)
        elif search_engine == "sogou":
            return re.findall(r'"thumbUrl":"(.*?)"', content)
        elif search_engine == "360":
            return re.findall(r'"img":"(.*?)"', content)
        else:
            raise ValueError("Unknown search engine")

    def spider(self, search_engine, url, keywords, max_pics, pbar, downloaded_pics, similarity_threshold=0.2, timeout=5):
        headers = {'User-Agent': self.ua.random}
        try:
            r_photos = requests.get(url=url, headers=headers, timeout=timeout)
            r_urls = self.get_image_urls(search_engine, r_photos.text)
            for r_url in r_urls:
                if downloaded_pics >= max_pics:
                    break
                try:
                    if is_image_downloaded(r_url, downloaded_images=set()):
                        logger.info(f"Image already downloaded: {r_url}")
                        continue
                    r_url_get = requests.get(url=r_url, headers=headers, timeout=timeout)
                    if r_url_get.status_code == 200:
                        image_content = r_url_get.content
                        if is_image_relevant(image_content, similarity_threshold, keywords):
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
            logger.error(f"Error fetching image search results: {e}")
        return downloaded_pics

    def download_images(self, search_engines, keywords_list, max_pics, max_workers, similarity_threshold, timeout):
        for keywords in keywords_list:
            create_file(keywords)
            logger.info(f'正在搜索关键字：[{keywords}]一共有多少张图，请稍等。。。')
            pics_downloaded = 0
            urls = []
            with tqdm(total=max_pics, desc=f'Downloading [{keywords}]') as pbar:
                for search_engine in search_engines:
                    if pics_downloaded >= max_pics:
                        break
                    page_num = self.check_pics_number(search_engine, keywords, max_pics)
                    for j in range(0, page_num * 20, 20):
                        if search_engine == "baidu":
                            urls.append(
                                f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}')
                        elif search_engine == "google":
                            urls.append(f'https://www.google.com/search?q={keywords}&tbm=isch&start={j}')
                        elif search_engine == "bing":
                            urls.append(f'https://www.bing.com/images/search?q={keywords}&first={j}')
                        elif search_engine == "sogou":
                            urls.append(f'https://pic.sogou.com/pics?query={keywords}&start={j}')
                        elif search_engine == "360":
                            urls.append(f'https://image.so.com/i?q={keywords}&pn={j}')
                        else:
                            raise ValueError("Unknown search engine")

                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = []
                        for url in urls:
                            if pics_downloaded >= max_pics:
                                break
                            futures.append(executor.submit(self.spider, search_engine, url, keywords, max_pics, pbar,
                                                           pics_downloaded, similarity_threshold, timeout))
                        for future in futures:
                            pics_downloaded += future.result()

        logger.info(f'下载完成！')

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
            elif search_engine == "360":
                check_url = f'https://image.so.com/i?q={keywords}&pn={check_page}'
            else:
                raise ValueError("Unknown search engine")

            check_content = requests.get(url=check_url, headers=headers)
            logger.info(f'正在搜索{search_engine}中，当前第{page_num}页存在...')

            r_urls = self.get_image_urls(search_engine, check_content.text)
            total_pics += len(r_urls)
            page_num += 1
            check_page = ((page_num) * 20) - 20
        return page_num