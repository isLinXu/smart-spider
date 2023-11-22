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

# 保存匹配到的图像的文件夹
output_folder = "./output_folder1"
os.makedirs(output_folder, exist_ok=True)

# 从文件夹加载图像
image_files = [os.path.join(image_folder, f) for f in os.listdir(image_folder) if f.endswith(('.jpg', '.jpeg', '.png'))]

# 检查找到的图像文件
print(f"Found {len(image_files)} image files:")
print("\n".join(image_files))

# 检查是否找到图像
if not image_files:
    raise ValueError("No images found in the specified folder.")

# images = [Image.open(f).convert("RGB") for f in image_files]

# 尝试打开图像，跳过无法识别的文件
images = []
valid_image_files = []
for f in image_files:
    try:
        img = Image.open(f).convert("RGB")
        images.append(img)
        valid_image_files.append(f)
    except Exception as e:
        print(f"Error loading image {f}: {e}")

image_files = valid_image_files

# 对图像进行预处理和编码
preprocess = Compose([Resize((224, 224)), ToTensor()])
image_tensors = torch.stack([preprocess(img) for img in images]).to(device)
with torch.no_grad():
    image_embeddings = model.encode_image(image_tensors).cpu().numpy()


def search_images(query, num_results=5):
    # 对查询进行编码
    with torch.no_grad():
        text_features = model.encode_text(clip.tokenize(query).to(device)).cpu().numpy()

    # 计算图像和文本之间的相似性
    similarities = (image_embeddings @ text_features.T).squeeze(1)
    print("similarities:", similarities)
    # 获取最相似的图像
    best_indices = np.argsort(similarities)[-num_results:][::-1]
    best_images = [images[i] for i in best_indices]
    best_image_files = [image_files[i] for i in best_indices]

    return best_images, best_image_files


# 搜索包含 "person" 的图像
query = "a person"
num_results = 10
best_images, best_image_files = search_images(query, num_results=num_results)

# 保存匹配到的图像
for i, (img, img_file) in enumerate(zip(best_images, best_image_files)):
    output_file = os.path.join(output_folder, f"{query}_result_{i + 1}.jpg")
    img.save(output_file)

# 显示结果
import matplotlib.pyplot as plt

fig, axes = plt.subplots(1, num_results, figsize=(15, 3))
for i, (img, ax) in enumerate(zip(best_images, axes)):
    ax.imshow(img)
    ax.set_title(f"Result {i + 1}")
    ax.axis("off")

plt.suptitle(f"Best matches for '{query}'")
plt.show()