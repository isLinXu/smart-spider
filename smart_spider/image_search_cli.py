# coding=utf-8
"""本地图片以图搜图命令行入口。

示例：
    python -m smart_spider.image_search_cli \
        --build-from ./output --index ./output/image_index.npz
    python -m smart_spider.image_search_cli \
        --query-image ./query.jpg --index ./output/image_index.npz --top-k 20
"""
from __future__ import annotations

import argparse
import json
from typing import Optional

from .image_retrieval import ImageSimilarityIndex


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SmartSpider 本地图片以图搜图")
    parser.add_argument("--index", default="./image_index.npz", help="索引文件路径")
    parser.add_argument("--build-from", help="建立索引的图片目录")
    parser.add_argument("--query-image", help="查询图片路径")
    parser.add_argument("--top-k", type=int, default=10, help="返回结果数量")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="最低余弦相似度（-1~1，默认不过滤）",
    )
    parser.add_argument("--model", default="ViT-B/32", help="CLIP 模型名称")
    parser.add_argument("--device", default="cpu", help="推理设备，例如 cpu 或 cuda")
    parser.add_argument("--batch-size", type=int, default=32, help="建立索引时的批大小")
    parser.add_argument("--no-recursive", action="store_true", help="不递归扫描目录")
    parser.add_argument("--include-query", action="store_true", help="结果中允许包含查询图片自身")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.build_from and not args.query_image:
        parser.error("至少需要 --build-from 或 --query-image")
    if args.top_k <= 0:
        parser.error("--top-k must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")

    index = ImageSimilarityIndex(
        model_name=args.model,
        device=args.device,
        batch_size=args.batch_size,
    )
    output: dict[str, object] = {"index": args.index}
    if args.build_from:
        report = index.build_from_directory(
            args.build_from,
            recursive=not args.no_recursive,
        )
        index.save(args.index)
        output["build"] = report.to_dict()

    if args.query_image:
        if not args.build_from:
            index = ImageSimilarityIndex.load(
                args.index,
                encoder=index.encoder,
                device=args.device,
                batch_size=args.batch_size,
            )
        results = index.search(
            args.query_image,
            top_k=args.top_k,
            threshold=args.threshold,
            exclude_query=not args.include_query,
        )
        output["query_image"] = args.query_image
        output["results"] = [item.to_dict(rank=i) for i, item in enumerate(results, start=1)]

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
