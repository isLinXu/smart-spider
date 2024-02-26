import hashlib
import imghdr
import os
from io import BytesIO

import numpy as np
import torch
from PIL import Image
from loguru import logger
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize

import clip
from smart_spider import logo_str


def print_logo_str():
    print(f"{logo_str}")


def create_file(keywords):
    '''
    创建文件夹
    :param keywords:
    :return:
    '''
    if not os.path.exists(keywords):
        os.mkdir(keywords)
    else:
        logger.info(f'已存在以{keywords}关键字命名的文件夹')


def is_image_downloaded(url, downloaded_images=None):
    '''
    判断图片是否已经下载
    :param url:
    :return:
    '''
    url_hash = hashlib.md5(url.encode()).hexdigest()
    if url_hash in downloaded_images:
        return True
    downloaded_images.add(url_hash)
    return False


def is_image_relevant(image_content, similarity_threshold, keyword):
    '''
    判断图片是否与关键字相关
    :param image_content:
    :param keyword:
    :return:
    '''
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, preprocess = clip.load("ViT-B/32", device=device)
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

    image_tensor = image_transform(image).unsqueeze(0).to(device)
    keyword_tensor = clip.tokenize([keyword]).to(device)

    with torch.no_grad():
        image_features = model.encode_image(image_tensor)
        keyword_features = model.encode_text(keyword_tensor)
        similarity = torch.nn.functional.cosine_similarity(image_features, keyword_features)
        logger.info(f"Similarity: {similarity.item()}")
    return similarity.item() > similarity_threshold


def delete_error_image(father_path):
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
