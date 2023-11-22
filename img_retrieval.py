import os
import torch
import clip
import numpy as np
from torchvision.transforms import Compose, Resize, ToTensor
from PIL import Image

# 加载 CLIP 模型
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)

# 图像数据文件夹
image_folder = "/Users/gatilin/PycharmProjects/smart-spider/coco128/train2017"

# 从文件夹加载图像
image_files = [os.path.join(image_folder, f) for f in os.listdir(image_folder) if f.endswith(('.jpg', '.jpeg', '.png'))]

# 检查找到的图像文件
print(f"Found {len(image_files)} image files:")
print("\n".join(image_files))

# 检查是否找到图像
if not image_files:
    raise ValueError("No images found in the specified folder.")

images = [Image.open(f).convert("RGB") for f in image_files]

# 对图像进行预处理和编码
preprocess = Compose([Resize((224, 224)), ToTensor()])
image_tensors = torch.stack([preprocess(img) for img in images]).to(device)
with torch.no_grad():
    image_embeddings = model.encode_image(image_tensors).cpu().numpy()


def search_image(query):
    # 对查询进行编码
    with torch.no_grad():
        text_features = model.encode_text(clip.tokenize(query).to(device)).cpu().numpy()

    # 计算图像和文本之间的相似性
    similarities = (image_embeddings @ text_features.T).squeeze(1)

    # 获取最相似的图像
    best_index = np.argmax(similarities)
    best_image = images[best_index]

    return best_image


# 搜索包含 "person" 的图像
query = "person"
best_image = search_image(query)

# 显示结果
import matplotlib.pyplot as plt

plt.imshow(best_image)
plt.title(f"Best match for '{query}'")
plt.axis("off")
plt.show()