# # # # # import re
# # # # # import requests
# # # # # import time
# # # # # import os
# # # # # import threading
# # # # # from fake_useragent import UserAgent
# # # # # from queue import Queue
# # # # # import hashlib
# # # # #
# # # # #
# # # # # class BaiduSpiderPhotos(threading.Thread):
# # # # #     def __init__(self, queue, keywords):
# # # # #         threading.Thread.__init__(self)
# # # # #         self._queue = queue
# # # # #         self._keywords = keywords
# # # # #         self._ua = UserAgent()
# # # # #
# # # # #     def run(self):
# # # # #         while not self._queue.empty():
# # # # #             url = self._queue.get()
# # # # #             try:
# # # # #                 self.spider(url, self._keywords)
# # # # #             except Exception as e:
# # # # #                 pass
# # # # #
# # # # #     def spider(self, url, keywords):
# # # # #         headers = {'User-Agent': self._ua.random}
# # # # #         r_photos = requests.get(url=url, headers=headers, timeout=2)
# # # # #         r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
# # # # #         for r_url in r_urls:
# # # # #             try:
# # # # #                 r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
# # # # #                 if r_url_get.status_code == 200:
# # # # #                     print(f'[INFO]当前正在下载的url链接为：{r_url}')
# # # # #                     m = hashlib.md5()
# # # # #                     m.update(r_url.encode())
# # # # #                     name = m.hexdigest()
# # # # #                     print('[INFO]正在保存图片')
# # # # #                     res = requests.get(url=r_url, headers=headers, timeout=3)
# # # # #                     image_content = res.content
# # # # #                     filename = os.path.join(keywords, f'{name}.jpg')
# # # # #                     with open(filename, 'wb') as f:
# # # # #                         f.write(image_content)
# # # # #                     print(f'[INFO]保存成功，图片名为：{name}.jpg')
# # # # #             except Exception as e:
# # # # #                 pass
# # # # #
# # # # #
# # # # # def check_pics_number(keywords, ua):
# # # # #     headers = {'User-Agent': ua.random}
# # # # #     page_num = 1
# # # # #     check_page = 0
# # # # #     while True:
# # # # #         check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
# # # # #         check_content = requests.get(url=check_url, headers=headers)
# # # # #         print(f'[INFO]当前第{page_num}页存在')
# # # # #         if '抱歉，没有找到与' in check_content.text:
# # # # #             return page_num
# # # # #             break
# # # # #         page_num += 1
# # # # #         check_page = ((page_num) * 20) - 20
# # # # #
# # # # #
# # # # # def deal_url(keywords, maxpage):
# # # # #     queue = Queue()
# # # # #     for j in range(0, maxpage, 20):
# # # # #         spider_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}'
# # # # #         print(f'当前要访问的url是：{spider_url}')
# # # # #         queue.put(spider_url)
# # # # #     threads = []
# # # # #     thread_count = 30
# # # # #     for i in range(thread_count):
# # # # #         threads.append(BaiduSpiderPhotos(queue, keywords))
# # # # #     for t in threads:
# # # # #         t.start()
# # # # #     for t in threads:
# # # # #         t.join()
# # # # #
# # # # #
# # # # # def create_file(keywords):
# # # # #     if not os.path.exists(keywords):
# # # # #         os.mkdir(keywords)
# # # # #     else:
# # # # #         print(f'[INFO]已存在以{keywords}关键字命名的文件夹')
# # # # #
# # # # #
# # # # # def read_file():
# # # # #     read_list = []
# # # # #     read_txt = 'spider.txt'
# # # # #     with open(read_txt, 'r') as f:
# # # # #         for i in f.readlines():
# # # # #             read_list.append(i[:-1])
# # # # #         return read_list
# # # # #
# # # # #
# # # # # def main():
# # # # #     read_list = ['室内行人','电动车']
# # # # #     ua = UserAgent()
# # # # #     for read in read_list:
# # # # #         keywords = str(read)
# # # # #         create_file(keywords)
# # # # #         print(f'[INFO]正在搜索关键字：{keywords}一共有多少张图，请稍等。。。')
# # # # #         search_time_start = time.time()
# # # # #         page_num = check_pics_number(keywords, ua)
# # # # #         print(f'[INFO]关键字{keywords}大约有{page_num * 20}张图左右')
# # # # #         print(f'[INFO]搜索本关键字一共花费{time.time() - search_time_start:.3f} s')
# # # # #         print('[INFO]正在下载...')
# # # # #         download_time = time.time()
# # # # #         deal_url(keywords, page_num)
# # # # #         print(f'下载这些图片一共用时:{time.time() - download_time:.3f} s')
# # # # #
# # # # # if __name__ == '__main__':
# # # # #     main()
# # # #
# # # # import re
# # # # import requests
# # # # import time
# # # # import os
# # # # import threading
# # # # from fake_useragent import UserAgent
# # # # from queue import Queue
# # # # import hashlib
# # # # from loguru import logger
# # # #
# # # #
# # # # class BaiduSpiderPhotos(threading.Thread):
# # # #     def __init__(self, queue, keywords):
# # # #         threading.Thread.__init__(self)
# # # #         self._queue = queue
# # # #         self._keywords = keywords
# # # #         self._ua = UserAgent()
# # # #
# # # #     def run(self):
# # # #         while not self._queue.empty():
# # # #             url = self._queue.get()
# # # #             try:
# # # #                 self.spider(url, self._keywords)
# # # #             except Exception as e:
# # # #                 pass
# # # #
# # # #     def spider(self, url, keywords):
# # # #         headers = {'User-Agent': self._ua.random}
# # # #         r_photos = requests.get(url=url, headers=headers, timeout=2)
# # # #         r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
# # # #         for r_url in r_urls:
# # # #             try:
# # # #                 r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
# # # #                 if r_url_get.status_code == 200:
# # # #                     logger.info(f'当前正在下载的url链接为：{r_url}')
# # # #                     m = hashlib.md5()
# # # #                     m.update(r_url.encode())
# # # #                     name = m.hexdigest()
# # # #                     logger.info('正在保存图片')
# # # #                     res = requests.get(url=r_url, headers=headers, timeout=3)
# # # #                     image_content = res.content
# # # #                     filename = os.path.join(keywords, f'{name}.jpg')
# # # #                     with open(filename, 'wb') as f:
# # # #                         f.write(image_content)
# # # #                     logger.info(f'保存成功，图片名为：{name}.jpg')
# # # #             except Exception as e:
# # # #                 pass
# # # #
# # # #
# # # # def check_pics_number(keywords, ua):
# # # #     headers = {'User-Agent': ua.random}
# # # #     page_num = 1
# # # #     check_page = 0
# # # #     while True:
# # # #         check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
# # # #         check_content = requests.get(url=check_url, headers=headers)
# # # #         logger.info(f'当前第{page_num}页存在')
# # # #         if '抱歉，没有找到与' in check_content.text:
# # # #             return page_num
# # # #             break
# # # #         page_num += 1
# # # #         check_page = ((page_num) * 20) - 20
# # # #
# # # #
# # # # def deal_url(keywords, maxpage):
# # # #     queue = Queue()
# # # #     for j in range(0, maxpage, 20):
# # # #         spider_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}'
# # # #         logger.info(f'当前要访问的url是：{spider_url}')
# # # #         queue.put(spider_url)
# # # #     threads = []
# # # #     thread_count = 30
# # # #     for i in range(thread_count):
# # # #         threads.append(BaiduSpiderPhotos(queue, keywords))
# # # #     for t in threads:
# # # #         t.start()
# # # #     for t in threads:
# # # #         t.join()
# # # #
# # # #
# # # # def create_file(keywords):
# # # #     if not os.path.exists(keywords):
# # # #         os.mkdir(keywords)
# # # #     else:
# # # #         logger.info(f'已存在以{keywords}关键字命名的文件夹')
# # # #
# # # #
# # # # def read_file():
# # # #     read_list = []
# # # #     read_txt = 'spider.txt'
# # # #     with open(read_txt, 'r') as f:
# # # #         for i in f.readlines():
# # # #             read_list.append(i[:-1])
# # # #         return read_list
# # # #
# # # #
# # # # def main():
# # # #     read_list = ['crow', 'person', 'nomotor']
# # # #     ua = UserAgent()
# # # #     for read in read_list:
# # # #         keywords = str(read)
# # # #         create_file(keywords)
# # # #         logger.info(f'正在搜索关键字：{keywords}一共有多少张图，请稍等...')
# # # #         search_time_start = time.time()
# # # #         page_num = check_pics_number(keywords, ua)
# # # #         logger.info(f'关键字{keywords}大约有{page_num * 20}张图左右')
# # # #         logger.info(f'搜索本关键字一共花费{time.time() - search_time_start:.3f} s')
# # # #         logger.info('正在下载...')
# # # #         download_time = time.time()
# # # #         deal_url(keywords, page_num)
# # # #         logger.info(f'下载这些图片一共用时:{time.time() - download_time:.3f} s')
# # # #
# # # #
# # # # if __name__ == '__main__':
# # # #     main()
# # #
# # #
# # import re
# # import requests
# # import time
# # import os
# # import threading
# # from fake_useragent import UserAgent
# # from queue import Queue
# # import hashlib
# # from loguru import logger
# # #
# # #
# # # class BaiduSpiderPhotos(threading.Thread):
# # #     def __init__(self, queue, keywords):
# # #         threading.Thread.__init__(self)
# # #         self._queue = queue
# # #         self._keywords = keywords
# # #         self._ua = UserAgent()
# # #
# # #     def run(self):
# # #         while not self._queue.empty():
# # #             url = self._queue.get()
# # #             try:
# # #                 self.spider(url, self._keywords)
# # #             except Exception as e:
# # #                 pass
# # #
# # #     def spider(self, url, keywords):
# # #         headers = {'User-Agent': self._ua.random}
# # #         r_photos = requests.get(url=url, headers=headers, timeout=2)
# # #         r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
# # #         for r_url in r_urls:
# # #             try:
# # #                 r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
# # #                 if r_url_get.status_code == 200:
# # #                     logger.info(f'当前正在下载的url链接为：{r_url}')
# # #                     m = hashlib.md5()
# # #                     m.update(r_url.encode())
# # #                     name = m.hexdigest()
# # #                     logger.info('正在保存图片')
# # #                     res = requests.get(url=r_url, headers=headers, timeout=3)
# # #                     image_content = res.content
# # #                     filename = os.path.join(keywords, f'{name}.jpg')
# # #                     with open(filename, 'wb') as f:
# # #                         f.write(image_content)
# # #                     logger.info(f'保存成功，图片名为：{name}.jpg')
# # #             except Exception as e:
# # #                 pass
# # #
# # #
# # # def check_pics_number(keywords, ua, max_pics):
# # #     headers = {'User-Agent': ua.random}
# # #     page_num = 1
# # #     check_page = 0
# # #     total_pics = 0
# # #     while total_pics < max_pics:
# # #         check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
# # #         check_content = requests.get(url=check_url, headers=headers)
# # #         logger.info(f'当前第{page_num}页存在')
# # #         if '抱歉，没有找到与' in check_content.text:
# # #             return page_num
# # #             break
# # #         r_urls = re.findall(r'"objURL":"(.*?)"', check_content.text)
# # #         total_pics += len(r_urls)
# # #         page_num += 1
# # #         check_page = ((page_num) * 20) - 20
# # #     return page_num
# # #
# # #
# # # def deal_url(keywords, maxpage):
# # #     queue = Queue()
# # #     for j in range(0, maxpage, 20):
# # #         spider_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}'
# # #         logger.info(f'当前要访问的url是：{spider_url}')
# # #         queue.put(spider_url)
# # #     threads = []
# # #     thread_count = 30
# # #     for i in range(thread_count):
# # #         threads.append(BaiduSpiderPhotos(queue, keywords))
# # #     for t in threads:
# # #         t.start()
# # #     for t in threads:
# # #         t.join()
# # #
# # #
# # # def create_file(keywords):
# # #     if not os.path.exists(keywords):
# # #         os.mkdir(keywords)
# # #     else:
# # #         logger.info(f'已存在以{keywords}关键字命名的文件夹')
# # #
# # #
# # # def read_file():
# # #     read_list = []
# # #     read_txt = 'spider.txt'
# # #     with open(read_txt, 'r') as f:
# # #         for i in f.readlines():
# # #             read_list.append(i[:-1])
# # #         return read_list
# # #
# # #
# # # def main():
# # #     read_list = ['猫', '狗']
# # #     max_pics = 12000  # 设置下载图片的最大数量
# # #     ua = UserAgent()
# # #     for read in read_list:
# # #         keywords = str(read)
# # #         create_file(keywords)
# # #         logger.info(f'正在搜索关键字：{keywords}一共有多少张图，请稍等。。。')
# # #         search_time_start = time.time()
# # #         page_num = check_pics_number(keywords, ua, max_pics)
# # #         logger.info(f'关键字{keywords}大约有{page_num * 20}张图左右')
# # #         logger.info(f'搜索本关键字一共花费{time.time() - search_time_start:.3f} s')
# # #
# # # if __name__ == '__main__':
# # #     main()
# #
# #
# # class BaiduSpiderPhotos(threading.Thread):
# #     def __init__(self, queue, keywords, max_pics):
# #         threading.Thread.__init__(self)
# #         self._queue = queue
# #         self._keywords = keywords
# #         self._ua = UserAgent()
# #         self._max_pics = max_pics
# #         self._downloaded_pics = 0
# #
# #     def run(self):
# #         while not self._queue.empty() and self._downloaded_pics < self._max_pics:
# #             url = self._queue.get()
# #             try:
# #                 self.spider(url, self._keywords)
# #             except Exception as e:
# #                 pass
# #
# #     def spider(self, url, keywords):
# #         headers = {'User-Agent': self._ua.random}
# #         r_photos = requests.get(url=url, headers=headers, timeout=2)
# #         r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
# #         for r_url in r_urls:
# #             if self._downloaded_pics >= self._max_pics:
# #                 break
# #             try:
# #                 r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
# #                 if r_url_get.status_code == 200:
# #                     logger.info(f'当前正在下载的url链接为：{r_url}')
# #                     m = hashlib.md5()
# #                     m.update(r_url.encode())
# #                     name = m.hexdigest()
# #                     logger.info('正在保存图片')
# #                     res = requests.get(url=r_url, headers=headers, timeout=3)
# #                     image_content = res.content
# #                     filename = os.path.join(keywords, f'{name}.jpg')
# #                     with open(filename, 'wb') as f:
# #                         f.write(image_content)
# #                     logger.info(f'保存成功，图片名为：{name}.jpg')
# #                     self._downloaded_pics += 1
# #             except Exception as e:
# #                 pass
# #
# #
#
# #
# #
# # def read_file():
# #     read_list = []
# #     read_txt = 'spider.txt'
# #     with open(read_txt, 'r') as f:
# #         for i in f.readlines():
# #             read_list.append(i[:-1])
# #         return read_list
# #
# # def main():
# #     read_list = ['猫', '狗', '皮卡丘']
# #     max_pics = 100  # 设置下载图片的最大数量
# #     ua = UserAgent()
# #     for read in read_list:
# #         keywords = str(read)
# #         create_file(keywords)
# #         logger.info(f'正在搜索关键字：{keywords}一共有多少张图，请稍等。。。')
# #         search_time_start = time.time()
# #         page_num = check_pics_number(keywords, ua, max_pics)
# #         total_pics_to_download = min(max_pics, (page_num * 20))
# #         logger.info(f'关键字{keywords}将下载{total_pics_to_download}张图')
# #         logger.info(f'搜索本关键字一共花费{time.time() - search_time_start:.3f} s')
# #         logger.info('正在下载...')
# #         download_time = time.time()
# #         deal_url(keywords, page_num, max_pics)
# #         logger.info(f'下载这些图片一共用时:{time.time() - download_time:.3f} s')
# #
# #
# # if __name__ == '__main__':
# #     main()
#
# import re
# import requests
# import time
# import os
# import threading
# from fake_useragent import UserAgent
# from queue import Queue
# import hashlib
# from loguru import logger
# from tqdm import tqdm
#
# logger.add("output.log", format="{time} {level} {message}", level="INFO")
#
# class BaiduSpiderPhotos(threading.Thread):
#     def __init__(self, queue, keywords, max_pics, pbar):
#         threading.Thread.__init__(self)
#         self._queue = queue
#         self._keywords = keywords
#         self._ua = UserAgent()
#         self._max_pics = max_pics
#         self._downloaded_pics = 0
#         self._pbar = pbar
#
#     def run(self):
#         while not self._queue.empty() and self._downloaded_pics < self._max_pics:
#             url = self._queue.get()
#             try:
#                 self.spider(url, self._keywords)
#             except Exception as e:
#                 pass
#
#     def spider(self, url, keywords):
#         headers = {'User-Agent': self._ua.random}
#         r_photos = requests.get(url=url, headers=headers, timeout=2)
#         r_urls = re.findall(r'"objURL":"(.*?)"', r_photos.text)
#         for r_url in r_urls:
#             if self._downloaded_pics >= self._max_pics:
#                 break
#             try:
#                 r_url_get = requests.get(url=r_url, headers=headers, timeout=2)
#                 if r_url_get.status_code == 200:
#                     logger.info(f'当前正在下载的url链接为：{r_url}')
#                     m = hashlib.md5()
#                     m.update(r_url.encode())
#                     name = m.hexdigest()
#                     logger.info('正在保存图片')
#                     res = requests.get(url=r_url, headers=headers, timeout=3)
#                     image_content = res.content
#                     filename = os.path.join(keywords, f'{name}.jpg')
#                     with open(filename, 'wb') as f:
#                         f.write(image_content)
#                     logger.info(f'保存成功，图片名为：{name}.jpg')
#                     self._downloaded_pics += 1
#                     self._pbar.update(1)
#             except Exception as e:
#                 pass
#
#
# def deal_url(keywords, maxpage, max_pics):
#     queue = Queue()
#     for j in range(0, maxpage, 20):
#         spider_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}'
#         logger.info(f'当前要访问的url是：{spider_url}')
#         queue.put(spider_url)
#     threads = []
#     thread_count = 30
#     for i in range(thread_count):
#         threads.append(BaiduSpiderPhotos(queue, keywords, max_pics))
#     for t in threads:
#         t.start()
#     for t in threads:
#         t.join()
#
#
# def check_pics_number(keywords, ua, max_pics):
#     headers = {'User-Agent': ua.random}
#     page_num = 1
#     check_page = 0
#     total_pics = 0
#     while total_pics < max_pics:
#         check_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={check_page}'
#         check_content = requests.get(url=check_url, headers=headers)
#         logger.info(f'当前第{page_num}页存在')
#         if '抱歉，没有找到与' in check_content.text:
#             return page_num
#             break
#         r_urls = re.findall(r'"objURL":"(.*?)"', check_content.text)
#         total_pics += len(r_urls)
#         page_num += 1
#         check_page = ((page_num) * 20) - 20
#     return page_num
#
#
# def create_file(keywords):
#     if not os.path.exists(keywords):
#         os.mkdir(keywords)
#     else:
#         logger.info(f'已存在以{keywords}关键字命名的文件夹')
#
#
# def deal_url(keywords, maxpage, max_pics, pbar):
#     queue = Queue()
#     for j in range(0, maxpage, 20):
#         spider_url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={keywords}&pn={j}'
#         logger.info(f'当前要访问的url是：{spider_url}')
#         queue.put(spider_url)
#     threads = []
#     thread_count = 30
#     for i in range(thread_count):
#         threads.append(BaiduSpiderPhotos(queue, keywords, max_pics, pbar))
#     for t in threads:
#         t.start()
#     for t in threads:
#         t.join()
#
#
# def main():
#     # read_list = ['crow', 'person', 'nomotor']
#     read_list = ['猫', '狗', '皮卡丘']
#     max_pics = 100  # 设置下载图片的最大数量
#     ua = UserAgent()
#     for read in read_list:
#         keywords = str(read)
#         create_file(keywords)
#         logger.info(f'正在搜索关键字：{keywords}一共有多少张图，请稍等。。。')
#         search_time_start = time.time()
#         page_num = check_pics_number(keywords, ua, max_pics)
#         total_pics_to_download = min(max_pics, (page_num * 20))
#         logger.info(f'关键字{keywords}将下载{total_pics_to_download}张图')
#         logger.info(f'搜索本关键字一共花费{time.time() - search_time_start:.3f} s')
#         logger.info('正在下载...')
#         download_time = time.time()
#         with tqdm(total=total_pics_to_download, desc=f'Downloading {keywords}') as pbar:
#             deal_url(keywords, page_num, max_pics, pbar)
#         logger.info(f'下载这些图片一共用时:{time.time() - download_time:.3f} s')
#
#
# if __name__ == '__main__':
#     main()


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
                    logger.info('正在保存图片')
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


if __name__ == '__main__':
    main()
