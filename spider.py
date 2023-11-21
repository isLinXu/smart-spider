#coding=utf-8
import hashlib
import re
import requests
import os
from concurrent.futures import ThreadPoolExecutor
from fake_useragent import UserAgent
from loguru import logger
from tqdm import tqdm
import argparse

logger.add("output.log", format="{time} {level} {message}", level="INFO")


def spider(url, keywords, max_pics, pbar):
    ua = UserAgent()
    headers = {'User-Agent': ua.random}
    try:
        r_photos = requests.get(url=url, headers=headers, timeout=2)
        r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
        downloaded_pics = 0
        for r_url in r_urls:
            if downloaded_pics >= max_pics:
                break
            try:
                r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
                if r_url_get.status_code == 200:
                    logger.info(f'当前正在下载的url链接为：{r_url}')
                    name = hashlib.md5(r_url.encode()).hexdigest()
                    logger.info('正在保存图片......')
                    res = requests.get(url=r_url, headers=headers, timeout=3)
                    image_content = res.content
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


def check_pics_number(keywords, ua, max_pics):
    headers = {'User-Agent': ua.random}
    page_num = 1
    check_page = 0
    total_pics = 0
    while total_pics < max_pics:
        check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
        check_content = requests.get(url=check_url, headers=headers)
        logger.info(f'当前第{page_num}页存在')
        if '抱歉，没有找到与' in check_content.text:
            return page_num
        r_urls = re.findall(r'"objURL":"(.*?)"', check_content.text)
        total_pics += len(r_urls)
        page_num += 1
        check_page = ((page_num) * 20) - 20
    return page_num


def create_file(keywords):
    if not os.path.exists(keywords):
        os.mkdir(keywords)
    else:
        logger.info(f'已存在以{keywords}关键字命名的文件夹')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", nargs="+", default=['皮卡丘', '小火龙', '杰尼龟', '妙蛙种子'], help="关键词列表")
    parser.add_argument("--max_pics", type=int, default=100, help="每个关键词的最大图片数量")
    args = parser.parse_args()

    max_pics = args.max_pics
    ua = UserAgent()

    for keywords in args.keywords:
        create_file(keywords)
        logger.info(f'正在搜索关键字：{keywords}一共有多少张图，请稍等。。。')
        page_num = check_pics_number(keywords, ua, max_pics)
        total_pics_to_download = min(max_pics, (page_num * 20))
        logger.info(f'关键字{keywords}将下载{total_pics_to_download}张图')

        urls = [f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}' for j in
                range(0, page_num * 20, 20)]

        with tqdm(total=total_pics_to_download, desc=f'Downloading {keywords}') as pbar:
            with ThreadPoolExecutor(max_workers=30) as executor:
                for url in urls:
                    executor.submit(spider, url, keywords, max_pics, pbar)
    logger.info(f'下载完成！')

if __name__ == '__main__':
    main()
