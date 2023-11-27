
import argparse

from smart_spider.smart_spider import SmartSpider

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", nargs="+", default=['皮卡丘', '小火龙', '杰尼龟', '妙蛙种子'], help="关键词列表")
    parser.add_argument("--max_pics", type=int, default=100000, help="每个关键词的最大图片数量")
    args = parser.parse_args()
    image_downloader = SmartSpider(args.keywords, args.max_pics)
    image_downloader.download_images()