# smart-spider

**多模态智能爬虫** — 基于 CLIP 语义过滤，支持图片 / 视频 / 文本同步采集，内置完整反爬体系。

## 功能一览

| 能力 | 说明 |
|------|------|
| 🖼️ **图片采集** | 百度/Bing/搜狗/360 + CLIP 语义过滤，自动识别文件格式 |
| 🎬 **视频采集** | B 站、Bing 视频 + yt-dlp 下载（支持 1000+ 平台） |
| 📄 **文本采集** | 百度/Bing 网页搜索 + trafilatura 正文提取，输出 JSON |
| 🌐 **动态渲染** | Playwright 无头浏览器（处理 JS 加密、SPA、无限滚动） |
| 🔀 **代理池** | HTTP / SOCKS5 代理轮询，失败自动摘除，独立 Session |
| 🛡️ **反爬体系** | UA 指纹随机化、完整 Header、Referer 伪造、请求抖动 |
| ⏱️ **令牌桶限速** | 全局 RateLimiter，精确控制 QPS，避免触发速率封禁 |
| 🔄 **指数退避重试** | 遭遇 429/503/超时自动等待重试，最多 N 次 |
| 🎭 **Stealth 模式** | playwright-stealth 抹去 headless 特征，通过 Bot 检测 |

## 安装

```bash
git clone <repo-url>
cd smart-spider-v260518

# 基础依赖
pip install -r requirements.txt

# CLIP（必须从 GitHub 安装）
pip install git+https://github.com/openai/CLIP.git

# Playwright 浏览器内核（需要动态渲染时）
playwright install chromium
```

## 快速开始

```bash
# 采集 100 张「猫咪」图片
python spider.py --keywords 猫咪 --max_items 100

# 同时采集图片 + 视频 + 文章正文
python spider.py --keywords 猫咪 --media_types image video text --max_items 50

# 使用代理（支持多个，自动轮换）
python spider.py --keywords 猫咪 \
    --proxies http://127.0.0.1:7890 socks5://127.0.0.1:7891 \
    --rate 5 --max_retries 4

# 小红书 / 微信文章（需要 JS 渲染）
python spider.py --keywords 猫咪 \
    --search_engines xiaohongshu weixin \
    --use_browser --media_types image text

# B 站视频 + 需要登录（传入 cookies 文件）
python spider.py --keywords 猫咪 \
    --media_types video \
    --search_engines bilibili \
    --cookies_file ./cookies.txt

# 建立本地图片索引并以图搜图
python -m smart_spider.image_search_cli \
    --build-from ./output --index ./output/image_index.npz
python -m smart_spider.image_search_cli \
    --query-image ./query.jpg \
    --index ./output/image_index.npz \
    --top-k 20 --threshold 0.75

# 在现有数据集爬取链路中使用查询图片筛选候选
python -m smart_spider.dataset_cli \
    --keywords "cat" \
    --query-image ./query.jpg \
    --image-similarity-threshold 0.75 \
    --total 1000 --output ./dataset_cats
```

### 本地以图搜图

以图搜图第一版复用项目已有的 CLIP 图像编码器，在已采集目录或任意本地图片目录中建立向量索引。索引文件只包含归一化向量、图片路径和基础图片元数据，不会修改原始图片；查询结果按余弦相似度降序输出为 JSON。

也可以使用安装后的命令：

```bash
smart-spider-image-search --build-from ./output --index ./output/image_index.npz
smart-spider-image-search --query-image ./query.jpg \
    --index ./output/image_index.npz --top-k 20
```

`--device cuda` 可启用 GPU，`--batch-size` 控制建立索引时的批大小，`--threshold` 用于过滤低相似度结果。当前实现面向本地数据集检索，索引/查询 API 已与编码器解耦，后续可以增加百度、Bing 或其他外部反向图片搜索 provider。

在 `dataset_cli` 中传入 `--query-image` 后，关键词或站点仍负责发现互联网候选，下载并完成基础图片校验后，再用查询图片做视觉相似度二次筛选；通过 `--image-similarity-threshold` 调整严格程度。最终 `metadata.jsonl` 的 `image_sim`、`manifest.jsonl` 的 `pipeline.image_query` 会记录筛选证据。

图片数据集默认在下载、校验和过滤后统一转换为 JPG（JPEG quality 默认 95），并同步更新图片路径、MIME、质量字段和样本 ID。需要保留源格式时使用：

```bash
python -m smart_spider.dataset_cli \
    --keywords "cat" --total 1000 --output ./dataset_cats \
    --image-output-format original
```

可通过 `--jpeg-quality 1-100` 调整 JPG 质量。

## 参数说明

### 基础参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--keywords` | 必填 | 关键词列表（支持多个） |
| `--max_items` | `50` | 每关键词每模态最大采集数 |
| `--media_types` | `image` | 模态：`image` `video` `text`（可多选） |
| `--output_dir` | `./output` | 输出根目录，按 `{keyword}/{media_type}/` 组织 |

### 引擎参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--search_engines` | 自动 | 手动指定引擎，默认按模态自动选取 |

**可用引擎**

| 引擎名 | 模态 | 渲染方式 |
|--------|------|----------|
| `baidu` | image | static |
| `bing` | image | static |
| `sogou` | image | static |
| `360` | image | static |
| `bilibili` | video | static（API） |
| `bing_video` | video | static |
| `baidu_text` | text | static |
| `bing_text` | text | static |
| `weixin` | text | **dynamic** |
| `xiaohongshu` | image | **dynamic** |

### CLIP 过滤参数（image 模态）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--similarity_threshold` | `0.20` | CLIP 余弦相似度阈值（越高越严格） |
| `--batch_size` | `16` | 推理批大小（GPU 可调大至 32-64） |

**阈值建议**

| 场景 | 推荐阈值 |
|------|----------|
| 宽松采集（数量优先） | `0.15` |
| 平衡（默认） | `0.20` |
| 严格过滤（质量优先） | `0.25~0.30` |

### 网络 & 反爬参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--proxies` | 无 | 代理列表，支持 `http://` 和 `socks5://`，空格分隔多个 |
| `--rate` | `8.0` | 每秒最大请求数（令牌桶） |
| `--max_retries` | `3` | 遭遇 429/503/超时时的最大重试次数 |
| `--timeout` | `10` | 单次请求超时秒数 |
| `--max_workers` | `20` | 下载线程池大小 |

### Playwright 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--use_browser` | 否 | 启用无头浏览器（所有引擎均通过浏览器访问） |
| `--no_headless` | 否 | 显示浏览器窗口（调试用） |
| `--browser_proxy` | 无 | 浏览器专用代理 |

### 视频参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--video_format` | `bestvideo[height<=1080]+bestaudio/best` | yt-dlp 格式选择 |
| `--video_max_size` | `500m` | 单文件最大体积 |
| `--cookies_file` | 无 | yt-dlp cookies.txt（用于 B 站/抖音登录态） |

## 输出目录结构

```
output/
└── {keyword}/
    ├── image/
    │   ├── a3f2b1c4d5e6f7a8.jpg
    │   ├── a3f2b1c4d5e6f7a8.jpg.json   ← 图片元数据
    │   └── ...
    ├── video/
    │   ├── 猫咪合集.mp4
    │   ├── 猫咪合集.info.json           ← yt-dlp 元数据
    │   └── ...
    └── text/
        ├── b4c3d2e1f0a9b8c7.json        ← {url, title, text, date, author}
        └── ...
```

## 添加自定义引擎

```python
# smart_spider/engines.py
class MyEngine(SearchEngine):
    name = "myengine"
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC

    def build_search_url(self, keyword, page):
        return f"https://example.com/api?q={quote_plus(keyword)}&p={page}"

    def extract_items(self, html):
        data = json.loads(html)
        return [
            {"url": item["img_url"], "meta": {"title": item["title"]}}
            for item in data.get("results", [])
        ]

ENGINE_REGISTRY["myengine"] = MyEngine()
```

## 架构设计

```
SmartSpider
├── SmartHttpClient        反爬 HTTP 层
│   ├── ProxyPool          代理轮换 + 失败摘除
│   ├── RateLimiter        令牌桶限速
│   └── 指数退避重试
├── DynamicRenderer        Playwright 动态渲染
│   ├── Stealth 补丁
│   ├── 随机视口 + UA
│   └── 资源拦截（提速）
├── CLIP 推理线程          生产者-消费者，批量推理
├── VideoDownloader        yt-dlp 包装
└── TextExtractor          trafilatura 正文提取
```

## License

见 [LICENSE](./LICENSE)
