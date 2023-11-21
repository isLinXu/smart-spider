import os
import re
import time
import uuid
import requests
import numpy as np
import torch
import clip
from PIL import Image
import imghdr
from io import BytesIO
from tqdm import tqdm


class ImageDownloader:
    def __init__(self, keywords, max_sum=1000, save_root='images'):
        self.keywords = keywords
        self.max_sum = max_sum
        self.save_root = save_root

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model, self.preprocess = clip.load("ViT-B/32", device=self.device)

    def is_image_relevant(self, image, keyword):
        text = clip.tokenize([keyword]).to(self.device)
        image = self.preprocess(image).unsqueeze(0).to(self.device)

        with torch.no_grad():
            image_features = self.model.encode_image(image)
            text_features = self.model.encode_text(text)

            logits_per_image, _ = self.model(image, text)
            probs = logits_per_image.softmax(dim=-1).cpu().numpy()

        return probs[0][0] > 0.5

    def download_image(self, key_word, save_name):
        download_sum = 0
        save_path = os.path.join(self.save_root, save_name)
        os.makedirs(save_path, exist_ok=True)

        for i in tqdm(range(0, self.max_sum), desc=f"下载 {key_word} 的图片"):
            download_sum = i
            str_pn = str(download_sum)
            url = f'http://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word={key_word}&pn={str_pn}&gsm=80&ct=&ic=0&lm=-1&width=0&height=0'

            try:
                s = requests.session()
                s.headers[
                    'User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/72.0.3626.119 Safari/537.36'
                result = s.get(url).content.decode('utf-8')
                img_urls = re.findall('"objURL":"(.*?)",', result, re.S)

                if not img_urls:
                    break

                # for img_url in tqdm(img_urls, desc=f"下载 {key_word} 的图片"):
                for img_url in img_urls:
                    for _ in range(3):  # 尝试3次
                        try:
                            img = requests.get(img_url, timeout=30)
                            pil_img = Image.open(BytesIO(img.content))
                            break
                        except requests.exceptions.RequestException as e:
                            print(f"请求异常，重试：{e}")
                            time.sleep(1)
                    else:
                        continue

                    relevance_score = self.is_image_relevant(pil_img, key_word)

                    if relevance_score:
                        file_name = os.path.join(save_path, f'{uuid.uuid1()}_{relevance_score:.2f}.jpg')

                        with open(file_name, 'wb') as f:
                            f.write(img.content)

                        print(f'正在下载 {key_word} 的第 {download_sum} 张图片，相关性分数：{relevance_score:.2f}')
                        download_sum += 1

                        if download_sum >= self.max_sum:
                            break

                        time.sleep(1)  # 在请求之间添加延迟
            except Exception as e:
                print(e)
                continue

        print('下载完成')

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

    def run(self):
        for key_word in self.keywords:
            save_name = self.keywords[key_word]
            self.download_image(key_word, save_name)

        self.delete_error_image(self.save_root)


if __name__ == '__main__':
    # keywords = {'电动车': 'nomotor'}
    keywords = {'行人': 'person'}
    max_sum = 1000

    image_downloader = ImageDownloader(keywords, max_sum)
    image_downloader.run()