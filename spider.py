
import argparse

from smart_spider.smart_spider import SmartSpider

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # parser.add_argument("--keywords", nargs="+", default=['皮卡丘', '小火龙', '杰尼龟', '妙蛙种子'], help="关键词列表")
    parser.add_argument("--keywords", nargs="+", default=['车'], help="关键词列表")
    parser.add_argument("--max_pics", type=int, default=30, help="每个关键词的最大图片数量")
    parser.add_argument("--similarity_threshold", type=float, default=0.20, help="相似度阈值")
    parser.add_argument("--timeout", type=int, default=5, help="请求超时时间")
    parser.add_argument("--max_workers", type=int, default=30, help="线程池最大工作线程数")
    parser.add_argument("--search_engines", nargs="+", default=["baidu", "bing", "sogou", "360"], help="搜索引擎列表")
    args = parser.parse_args()
    image_downloader = SmartSpider(args.keywords, args.max_pics,
                                   args.similarity_threshold, args.timeout,
                                   args.max_workers, args.search_engines)
    image_downloader.run()