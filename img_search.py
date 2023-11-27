import os
import sys
import requests
from PIL import Image
from googlesearch import search
from io import BytesIO
import torch
import torchvision.transforms as T
from dall_e import map_pixels, unmap_pixels, load_model

# 初始化DALL-E模型
device = "cuda" if torch.cuda.is_available() else "cpu"
model = load_model("https://cdn.openai.com/dall-e/encoder.pkl", device)

# 图片预处理
preprocess = T.Compose([
    T.Resize(256, interpolation=Image.LANCZOS),
    T.CenterCrop(224),
    T.ToTensor(),
    map_pixels
])

def preprocess_image(image):
    return preprocess(image).unsqueeze(0).to(device)

# 计算余弦相似度
def cos_sim(a, b):
    return torch.nn.functional.cosine_similarity(a, b, dim=-1)

# 下载图片
def download_image(url):
    response = requests.get(url)
    return Image.open(BytesIO(response.content)).convert("RGB")

# 搜索与目标图片相似的图片
def search_similar_images(target_image, num_results=10):
    target_image_tensor = preprocess_image(target_image)
    target_image_features = model(target_image_tensor)

    query = "image"
    search_results = search(query, num_results=num_results, lang="en", area="com", ncr=True, safe="off")

    best_similarity = -1
    best_result = None

    for result in search_results:
        try:
            image = download_image(result)
            image_tensor = preprocess_image(image)
            image_features = model(image_tensor)

            similarity = cos_sim(target_image_features, image_features).item()
            if similarity > best_similarity:
                best_similarity = similarity
                best_result = result
        except Exception as e:
            print(f"Error processing image: {e}")

    return best_result

# 主程序
def main():
    if len(sys.argv) != 2:
        print("Usage: python image_search.py <image_path>")
        sys.exit(1)

    image_path = sys.argv[1]
    target_image = Image.open(image_path).convert("RGB")
    similar_image_url = search_similar_images(target_image)

    if similar_image_url:
        print(f"Found a similar image at: {similar_image_url}")
    else:
        print("No similar images found.")

if __name__ == "__main__":
    main()