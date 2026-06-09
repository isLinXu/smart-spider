#!/usr/bin/env python3
# coding=utf-8
"""CLIP 本地图片检索示例。

从本地文件夹加载图片，使用 CLIP 模型进行文本-图片语义检索，
返回与查询文本最相似的图片。

依赖: pip install torch torchvision openai-clip numpy pillow matplotlib
"""
import argparse
import os

import clip
import numpy as np
import torch
from PIL import Image


def load_images_from_folder(folder: str, device: str, model, preprocess):
    """从文件夹加载图片并计算 CLIP 图像嵌入。

    Args:
        folder:     图片文件夹路径
        device:     推理设备 ("cuda" / "cpu")
        model:      已加载的 CLIP 模型
        preprocess: CLIP 官方预处理 transform

    Returns:
        (images, valid_files, image_embeddings)
    """
    image_files = [
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ]

    if not image_files:
        raise ValueError(f"No images found in {folder}")

    images = []
    valid_files = []
    for f in image_files:
        try:
            img = Image.open(f).convert("RGB")
            images.append(img)
            valid_files.append(f)
        except Exception as e:
            print(f"Error loading image {f}: {e}")

    if not images:
        raise ValueError("No valid images found after filtering")

    # 批量编码（使用传入的 model 和 preprocess）
    image_tensors = torch.stack([preprocess(img) for img in images]).to(device)
    with torch.no_grad():
        image_embeddings = model.encode_image(image_tensors).cpu().numpy()

    return images, valid_files, image_embeddings


def search_images(
    query: str,
    image_embeddings: np.ndarray,
    images: list,
    image_files: list,
    model,
    device: str,
    num_results: int = 5,
):
    """根据文本查询检索最相似的图片。

    Args:
        query:            查询文本
        image_embeddings: 图像嵌入矩阵 (N, D)
        images:           PIL Image 列表
        image_files:      对应文件路径列表
        model:            已加载的 CLIP 模型
        device:           推理设备
        num_results:      返回结果数

    Returns:
        (best_images, best_files)
    """
    with torch.no_grad():
        text_features = model.encode_text(
            clip.tokenize([query]).to(device)
        ).cpu().numpy()

    similarities = (image_embeddings @ text_features.T).squeeze(1)
    best_indices = np.argsort(similarities)[-num_results:][::-1]
    best_images = [images[i] for i in best_indices]
    best_files = [image_files[i] for i in best_indices]

    return best_images, best_files


def main():
    parser = argparse.ArgumentParser(description="CLIP 本地图片检索示例")
    parser.add_argument("--folder", type=str, required=True, help="图片文件夹路径")
    parser.add_argument("--query", type=str, default="a person", help="搜索查询文本")
    parser.add_argument("--num_results", type=int, default=10, help="返回结果数量")
    parser.add_argument("--output", type=str, default="./output_folder", help="输出目录")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, preprocess = clip.load("ViT-B/32", device=device)
    model.eval()

    images, image_files, embeddings = load_images_from_folder(
        args.folder, device, model, preprocess
    )
    print(f"Loaded {len(images)} images from {args.folder}")

    best_images, best_files = search_images(
        args.query, embeddings, images, image_files, model, device, args.num_results
    )

    # 保存结果
    os.makedirs(args.output, exist_ok=True)
    for i, (img, img_file) in enumerate(zip(best_images, best_files)):
        output_file = os.path.join(args.output, f"{args.query}_result_{i + 1}.jpg")
        img.save(output_file)
        print(f"Saved: {output_file}  (source: {img_file})")

    # 可视化（可选依赖）
    try:
        import matplotlib.pyplot as plt

        n = min(args.num_results, len(best_images))
        fig, axes = plt.subplots(1, n, figsize=(3 * n, 3))
        if n == 1:
            axes = [axes]
        for i, (img, ax) in enumerate(zip(best_images, axes)):
            ax.imshow(img)
            ax.set_title(f"Result {i + 1}", fontsize=9)
            ax.axis("off")
        plt.suptitle(f"Best matches for '{args.query}'")
        plt.tight_layout()
        vis_path = os.path.join(args.output, "search_results.png")
        plt.savefig(vis_path, dpi=150)
        print(f"Visualization saved to {vis_path}")
    except ImportError:
        print("matplotlib not installed, skipping visualization")


if __name__ == "__main__":
    main()
