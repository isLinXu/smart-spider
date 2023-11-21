
# coding: utf-8
import re
import requests
import time
import os
import threading
from fake_useragent import UserAgent
from queue import Queue
import hashlib


class BaiduSpiderPhotos(threading.Thread):
    def __init__(self, queue, keywords):
        threading.Thread.__init__(self)
        self._queue = queue
        self._keywords = keywords

    def run(self):
        while not self._queue.empty():
            url = self._queue.get()
            try:
                self.spider(url, self._keywords)
            except Exception as e:
                pass

    def spider(self, url, keywords):
        def ua():
            UA = UserAgent()
            return UA.random

        r_photos = requests.get(url=url, headers={'User-Agent': ua()}, timeout=2)
        r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
        for r_url in r_urls:
            try:
                r_url_get = requests.get(url=r_url, headers={'User-Agent': ua()}, timeout=2)
                if r_url_get.status_code == 200:
                    print('[INFO]当前正在下载的url链接为：', r_url)
                    m = hashlib.md5()
                    m.update(r_url.encode())
                    name = m.hexdigest()
                    print('[INFO]正在保存图片')
                    res = requests.get(url=r_url, headers={'User-Agent': ua()}, timeout=3)
                    image_content = res.content
                    filename = keywords + '/' + name + '.jpg'
                    with open(filename, 'wb') as f:
                        f.write(image_content)
                    print('[INFO]保存成功，图片名为：{}.jpg'.format(name))
            except Exception as e:
                pass


def check_pics_number(keywords):
    def ua():
        UA = UserAgent()
        return UA.random

    page_num = 1
    check_page = 0
    while True:
        check_url = 'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word=' + str(keywords) + '&pn=' + str(check_page)
        check_content = requests.get(url=check_url, headers={'User-Agnet': ua()})
        print('[INFO]当前第{}页存在'.format(page_num))
        if '抱歉，没有找到与' in check_content.text:
            return page_num
            break
        page_num += 1
        check_page = ((page_num) * 20) - 20


def deal_url(keywords, maxpage):
    queue = Queue()
    for j in range(0, maxpage, 20):
        spider_url = 'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word=' + str(keywords) + '&pn=' + str(j)
        print('当前要访问的url是：', spider_url)
        queue.put(spider_url)
    threads = []
    thread_count = 30
    for i in range(thread_count):
        threads.append(BaiduSpiderPhotos(queue, keywords))
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def create_file(keywords):
    create_path = keywords
    if not os.path.exists(create_path):
        os.mkdir(create_path)
    else:
        print('[INFO]已存在以{}关键字命名的文件夹'.format(keywords))


def read_file():
    read_list = []
    read_txt = 'spider.txt'
    with open(read_txt, 'r') as f:
        for i in f.readlines():
            read_list.append(i[:-1])
        return read_list


def main():
    read_list = ['crow', 'person', 'nomotor']
    for read in read_list:
        keywords = str(read)
        create_file(keywords)
        print('[INFO]正在搜索关键字：{}一共有多少张图，请稍等。。。'.format(keywords))
        search_time_start = time.time()
        page_num = check_pics_number(keywords)
        print('[INFO]关键字{}大约有{}张图左右'.format(keywords, (page_num * 20)))
        print('[INFO]搜索本关键字一共花费{:.3f} s'.format(time.time() - search_time_start))
        print('[INFO]正在下载...')
        download_time = time.time()
        deal_url(keywords, page_num)
        print('下载这些图片一共用时:{:.3f} s'.format(time.time() - download_time))


if __name__ == '__main__':
    main()