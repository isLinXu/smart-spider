# smart-spider

**多模态智能爬虫** — 基于 CLIP 语义过滤，支持图片 / 视频 / 文本同步采集，内置完整反爬体系。

## 功能一览

| 能力 | 说明 |
|------|------|
| 🖼️ **图片采集** | 百度/Bing/搜狗/360 + CLIP 语义过滤，自动识别文件格式 |
| 🎬 **视频采集** | 抖音搜索/主页/分享链接、B 站、Bing 视频，支持 MP4 直链与 yt-dlp 下载 |
| 📄 **文本采集** | 百度/Bing 网页搜索 + trafilatura 正文提取，输出 JSON |
| 🔎 **远程以图搜图** | 本地图片上传到百度识图、Bing Visual Search、Google Lens，并返回网页结果 |
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

# 基础依赖（requirements.txt 是 pyproject.toml 的兼容入口）
pip install -r requirements.txt

# CLIP（必须从 GitHub 安装）
pip install git+https://github.com/openai/CLIP.git

# Playwright 浏览器依赖（动态渲染或远程以图搜图时）
pip install -e ".[browser]"
playwright install chromium
```

## 统一 CLI

安装后主入口是 `smart-spider <subcommand>`。旧的 `smart-spider-*` 脚本、`python spider.py` 和 `python -m smart_spider.*` 仍然可用。

```bash
smart-spider --help
smart-spider crawl --keywords 猫咪 --max_items 100
smart-spider dataset --keywords "cat" --total 1000 --output ./dataset_cats
smart-spider browse --profile ./site.yaml --once
smart-spider worker --once          # 默认领取队列中任意 kind
smart-spider worker --kind dataset_crawl --once
```

`browse --once` 会写出 `unified_report.json` 与 publish checklist；checklist 未就绪时退出码为 2，可加 `--allow-unready`。

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

### Jina 多模态候选标注与文本精排（可选）

通用多模态采集命令可启用 Jina 推理。`--jina-omni` 使用
`jina-embeddings-v5-omni-nano` 的 classification 适配器，对已获取的文本和
**本地暂存图片**生成候选标签；图片不会因模型推理再次请求源 URL。
同一样本有已验证图片时，候选标签以图片向量为依据；无图片时才使用文本，
避免页面正文或 alt 文本盖过图片内容。
候选标签写入 `manifest.jsonl` 的 `pipeline.annotation.candidates`，包含模型、
修订、模态、相似度和 `advisory=true`，不会直接变成正式标签。

`--jina-reranker` 可选择 v2 或 v3.5，对同一查询、同一标注批次内的文本样本
记录 `pipeline.jina_rerank` 的分数和名次。其结果只用于排序与复核，不作为
图片内容证明或未经校准的全局过滤阈值。

```bash
pip install -e '.[jina]'
smart-spider-multimodal --queries '猫,狗' --labels '猫,狗' \
  --jina-omni --jina-reranker v3.5 --output ./multimodal_dataset

# 使用 retrieval 适配器建图片索引，再用文本跨模态查询
smart-spider-image-search --build-from ./images --index ./jina-images.npz \
  --model jinaai/jina-embeddings-v5-omni-nano
smart-spider-image-search --query-text '夜间道路上的行人' \
  --index ./jina-images.npz --top-k 20
```

v2 精排器需要 Transformers 4 的接口，须在独立环境安装
`pip install -e '.[jina-v2]'`，再使用 `--jina-reranker v2`。
不要在同一个环境同时安装 `jina` 和 `jina-v2` extras。

模型首次使用时才加载；本地模型代码需要 Hugging Face `trust_remote_code`。
生产任务宜使用 `--jina-omni-revision` / `--jina-reranker-revision` 固定已审查的
模型修订。三个模型的公开权重均标注 CC BY-NC 4.0，商业使用须先解决授权。
推荐先在人工标注集上校准 `--jina-min-similarity` 和标签策略。

### 远程网页以图搜图

如果需要把本地图片直接上传到网络反向图片搜索页面并返回网页结果，使用独立命令：

```bash
smart-spider-reverse-image-search \
    --image ./query.jpg \
    --providers baidu google_lens bing \
    --top-k 20
```

结果包含 `source_url`、`image_url`、`thumbnail_url`、标题和摘要。默认使用无头 Chrome；需要登录或人工完成验证时，可以显示浏览器并保存用户目录：

```bash
smart-spider-reverse-image-search \
    --image ./query.jpg --providers google_lens \
    --no-headless --user-data-dir ./runtime/reverse-image-browser
```

每个 Provider 默认最多尝试 2 次，并在支持时轮换备用上传入口；可通过 `--attempts 1` 关闭重试。程序会等待 URL 或 Provider 结果节点出现后再解析，避免动态页面尚未完成时误报空结果。JSON 中的 `attempts`、`elapsed_ms` 和每条结果的 `metadata.source_domain` 可用于诊断和后续去重。

该命令会把图片发送给所选第三方服务；项目不自动绕过验证码或登录。Provider 页面变化、网络阻断或验证页面会记录在对应 Provider 的 `error` 字段中。调试时可增加 `--debug-dir ./runtime/reverse-image-debug` 保存每次尝试的页面 HTML 和截图。

图片数据集默认在下载、校验和过滤后统一转换为 JPG（JPEG quality 默认 95），并同步更新图片路径、MIME、质量字段和样本 ID。需要保留源格式时使用：

```bash
python -m smart_spider.dataset_cli \
    --keywords "cat" --total 1000 --output ./dataset_cats \
    --image-output-format original
```

可通过 `--jpeg-quality 1-100` 调整 JPG 质量。

### 六类安全场景批量采集

六类场景可以分别设置目标数量。`--scene-targets` 使用 `场景=数量` 格式，
`--total` 至少应为所有场景目标之和；任务报告会保存每个场景、发现来源和图片域名的实际配额。
下载默认关闭自动重定向并逐跳校验目标地址，连接与读取超时分离，响应和图片解码均有硬性大小/像素上限。

```bash
python -m smart_spider.dataset_cli \
  --keywords "打电话,使用手机,吸烟,未穿反光衣,叉车司机未戴安全帽,物品滞留" \
  --scene-targets "打电话=20000,使用手机=20000,吸烟=20000,未穿反光衣=20000,叉车司机未戴安全帽=20000,物品滞留=20000" \
  --total 120000 \
  --output ./dataset_safety_scenes \
  --connect-timeout 5 --read-timeout 20 \
  --max-inflight-pages 8 --max-inflight-downloads 20 \
  --max-pending-candidates 200 --per-domain-concurrency 2 \
  --memory-budget-mb 512 --max-image-pixels 50000000 \
  --max-source-share 0.25 --max-domain-share 0.15 \
  --resume
```

场景质量门禁会记录模型、提示词、校准集和阈值指纹；反光衣、安全帽等专用信号缺失的样本不会被自动放行，
而是进入人工复核队列。采集完成后可使用 `smart_spider.dataset_filter` 的 `--dedupe-near` 检查 pHash/dHash
近重复；训练集切分应通过 `smart_spider.dataset_governance.LeakageSafeSplitter` 按域名、页面、日期和图片簇分组。

启用主流程质量门禁时必须显式声明场景目标。缺少专用检测器信号的样本会写入
`scene_review_queue.jsonl`，不会进入数据集：

```bash
python -m smart_spider.dataset_cli \
  --keywords "未穿反光衣" --scene-targets "未穿反光衣=100" \
  --scene-quality-gate --total 100 --output ./dataset_safety
```

多模态任务同样支持 `--scene <profile> --scene-quality-gate`。浏览器与 HTTP
路径默认拒绝 localhost、私网和保留地址；只有在明确受控的内网任务中才使用
`--allow-private-hosts`。

### 六类工业目标：person / forklift / truck_rear / head / phone / helmet

`crawl_industrial_objects.py` 复用图片采集链路，默认每类 **5,000 张候选图**，
以工厂、仓储、物流园区和装卸区为场景。类别顺序固定为
`0=person, 1=forklift, 2=truck_rear, 3=head, 4=phone, 5=helmet`。
类别定义、中英文查询词及复核要求在
[`examples/industrial_objects.json`](examples/industrial_objects.json) 中维护。

```bash
# 离线检查计划；不会创建目录或请求网络
python crawl_industrial_objects.py --dry-run

# 首批验证：独立目录，每类 100 张
python crawl_industrial_objects.py --total-per-class 100 \
  --output-root ./dataset_industrial_pilot \
  --query-overrides examples/industrial_pilot_queries.json

# 批量采集；重复同一命令续跑，各类独立状态库和日志
python crawl_industrial_objects.py --total-per-class 5000 \
  --output-root ./dataset_industrial_objects --parallel 2 --rate 2

# 只读统计已登记且文件存在的候选，不用孤立文件充数
python crawl_industrial_objects.py --output-root ./dataset_industrial_objects --status
```

使用已有出口代理时可显式传 `--proxy http://proxy.example:8080`，地址中不能含凭据。
该参数复用框架的代理连接路径，采集目标和重定向仍须通过公网地址校验。
部分环境只设置 `HTTPS_PROXY` 时，现有 HTTP 客户端会将代理 socket 误作目标地址，
产生 `private or reserved connected peer` 错误；应显式配置代理。

每类使用 24 个中英文场景查询，覆盖视角、遮挡、夜间和远景。采集限速按单个
爬虫设置，两个任务的总速率上限约为两倍；每域并发 1，单域配额上限 15%，
基础尺寸至少 640×360。当前保留图片解码、文件大小和内容去重校验；默认不启用
未经校准的语义模型，不把搜索词写成正式标签，也不声称已完成语义过滤。

输出包含各类 `manifest.jsonl`、`metadata.jsonl`、`crawl.log` 和状态库，根目录有
`class_spec.json`、`crawl_plan.json`、`run_summary.json`。结束或正常中断后生成
`review_queue.jsonl`，按已有内容 ID 合并跨类重复并保留所有采集来源。
统计的是候选图数量，未经视觉审核；同一图可含多个类别，因此六类数量之和
不等于合并后的唯一图片数，更不等于标注实例数。近重复仍需后续聚类。
基础清单中的 `accepted` 只代表下载准入；根报告始终标记 `training_ready=false`。

试采查询文件采用较短的场景词；`--query-overrides` 只调整查询词，不修改类别定义。
查询列表会随计划保存，已有目录拒绝混入不同的类别或查询配置；新一轮查询使用新目录。
搜索页内容仍可能与查询无关，应先查看图片再扩大采集。

可使用离线 CLIP 复核排序处理一个或多个采集目录。需要已有的 OpenAI CLIP
ViT-B/32 权重及 `clip`、PyTorch 依赖；基础采集不依赖该模型。

```bash
python -m smart_spider.industrial_review \
  --input-roots ./dataset_industrial_pilot \
  --output ./.artifacts/industrial_pilot_review \
  --weights /path/to/ViT-B-32.pt --device cpu --threads 4 --batch-size 8
```

模块只读取清单中通过 SHA-256 和解码验证的本地图片，跨目录去重后每图编码一次，
同时比较六类目标、易混物体、工业场景及插画/商品图提示词。不会重新下载媒体，
不会修改原清单、删除低分图或产生检测框。模型权重固定为
`sha256:40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af`；
启动时校验实际文件。OpenAI CLIP 的 [MIT 许可](https://github.com/openai/CLIP/blob/main/LICENSE)
适用于模型项目，图片来源许可仍须独立核验。

输出 `visual_review.jsonl`、`invalid_assets.jsonl`、`summary.json`、`profile.json`
及可在本地浏览器打开的 `review.html`（每类展示前 30 张）。逐图保留模型 ID、权重摘要、
代码版本及摘要、余弦相似度、匹配提示词、原始来源和配置指纹。
`priority_review` / `needs_review` / `lower_priority` 仅表示复核顺序；
阈值来自 [`examples/industrial_visual_profile.json`](examples/industrial_visual_profile.json)，
目前未经业务校准，分数不代表正确率。报告分别列出各检索类别内的分布和全部图片的
六类评分分布，不能把后者当作六类独立图片数量或正式标签。

训练前先确认类别口径：`truck_rear` 暂定义为尾门、后灯及后保险杠所在的尾部区域；
`head` 包含戴帽人头，不能直接用作“未戴帽”类别；`helmet` 只含工业安全帽本体；
`phone` 只框可见手机，不能从手势猜测手机存在。每张图需要标全六类可见实例，
额外记录小目标尺寸、遮挡、截断，以及安全帽是否佩戴、与哪名人员关联。

建议每类先人工审核 100–200 张，测量相关率、可标注率和来源授权通过率，
再按实际通过率补采。例如通过率为 60% 时，5,000 张有效图约需 8,334 张候选；
这是预算估算，真实目标仍要按去重后的审核结果验收。商品白底图、海报、
示意图、合成图和已画框的展示图需筛除；真实夜间和遮挡难例应单独保留。
另建已完整标注的难负样本切片，重点覆盖扫码枪、对讲机、托盘车和普通帽子。

搜索结果不附带统一训练许可，脚本不会填写虚构的许可声明。逐图核验来源与授权、
完成检测框审核、全局近重复聚类后再导出训练数据。按摄像头、视频片段、站点
及相似图簇分组切分，避免同一现场相邻帧进入训练与测试两侧；独立业务评测集
优先使用授权现场监控。CVAT 支持审核后导出
[Ultralytics YOLO 检测格式](https://docs.cvat.ai/docs/dataset_management/formats/format-yolo-ultralytics/)。
尚未标注的候选不能通过空标签文件伪装成负样本。

可用 Commons 的分类页和文件页补采带逐图许可声明的原图：

```bash
python -m smart_spider.industrial_commons \
  --output ./dataset_industrial_commons_v1 \
  --target-per-class 30 --results-per-query 50
```

查询在 `examples/industrial_commons_queries.json` 中维护。默认使用允许访问的
`/wiki/Category:` 和 `/wiki/File:` 页面，遵守 robots；不绕过被拒绝的接口。
单连接、每秒最多约 1 次请求，媒体额外按 2 MB/s 节流；显式出口代理仍使用
`--proxy`，来源和跳转限于 Commons 及其上传域的 HTTPS 公网地址。
这是一轮有界补采：每个分类页最多取 `--results-per-query` 个文件，不递归子分类、
不翻页，目标未满退出码为 2，不会用重复图补足配额。以相同参数重复命令可续跑。

模块只接收有作者和明确 CC BY / CC BY-SA / CC0 声明的 JPEG、PNG、WebP，
下载后检查真实格式、尺寸、字节上限和 SHA-256，跨类别复用已有原图。
原始字节不转换，`source_evidence/` 保留来源页面快照，清单记录原图链接、
作者、署名资料、许可链接和页面摘要。HTML 页面没有提供的外部 SHA-1
不声称已校验，另记录本地文件摘要；接口方式仅在 robots 允许时可用。
`rejected_candidates.json` 和 `summary.json` 记录本轮拒绝原因及候选数量。
许可状态始终是 `source_license_declared_review_pending`；文件级声明、其他使用限制、
业务场景适配和检测框仍需审核，不能据此宣布已取得完整业务训练授权。
新目录可以直接交给 `smart_spider.industrial_review` 进行离线视觉排序。

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
| `douyin` | video | **dynamic**（显式选择，支持连续滚动） |
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
| `--connect-timeout` / `--read-timeout` | 同 `--timeout` | 分离连接建立与响应读取超时 |
| `--max_workers` | `20` | 下载线程池大小 |
| `--max-inflight-pages` / `--max-inflight-downloads` | 自动 | 有界分页与下载窗口 |
| `--max-pending-candidates` | 自动 | 候选处理队列上限 |
| `--per-domain-concurrency` | `2` | 单图片域名并发上限 |
| `--memory-budget-mb` | `512` | 下载响应内存预算，自动收紧下载并发 |
| `--max-image-pixels` | `50000000` | Pillow 解码像素上限，防止解压炸弹 |
| `--scene-targets` | 无 | 独立场景目标，如 `打电话=20000,吸烟=20000` |
| `--max-source-share` / `--max-domain-share` | `1.0` | 单一来源/域名最大占比 |

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

## 数据集广告与语义过滤

对已采集的数据集执行离线过滤。默认只分析并生成报告，不移动图片：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset_truck_loading_area_door_operation_5000_20260831 \
  --query "货车装卸区 开关门" \
  --limit 500
```

确认抽样报告后，对完整数据集应用过滤：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset_truck_loading_area_door_operation_5000_20260831 \
  --query "货车装卸区 开关门" \
  --apply
```

过滤器组合 CLIP 正负提示词、二维码、文字区域占比、网址/电话/促销词等信号。
拒绝项移动到 `_quarantine/{run_id}/files/`，原始 `metadata.jsonl` 和
`manifest.jsonl` 会备份。可使用报告中的 `run_id` 恢复：

阈值提供四个预设档位：`conservative` 偏向保留、`balanced` 为默认、`strict`
提高通用负向阈值，`precision` 还会要求装卸动作、敞开货厢门或货车对接月台等
核心场景证据。每条决定都会记录命中的提示词、信号和 `confidence`，便于复核。
OpenAI CLIP 对中文短句的直接匹配不稳定，因此内置中文场景使用已校准的英文提示词；
英文 `--query` 会作为额外正向提示词参与评分。

重复调参时可用 `--cache-dir ./.filter_cache` 缓存图像特征。缓存按模型隔离，
后续仅重新计算文本相似度和视觉信号。正式过滤可以叠加在已有隔离结果上，
恢复时必须从最新一轮开始，逐层执行 `--restore`。

应用过滤后，可不加载 CLIP 模型直接验证索引、隔离链和图片可读性；加入
`--verify-hashes` 还会比对活动图片与 `metadata.jsonl` 中的 SHA-256：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset_truck_loading_area_door_operation_5000_20260831 \
  --run-id truck_loading_filter_v1_20260901 \
  --verify --verify-hashes
```

OCR 默认关闭；安装 Tesseract 后可用 `--ocr` 仅识别疑似广告，或用
`--ocr-all` 识别全部图片。中文 OCR 需另外安装 `chi_sim` 语言数据。

模型筛选后可用逐行相对路径文件记录人工复核的明确误图，并以独立、可恢复的
隔离运行应用；精确重复可按文件 SHA-256 另行清理。这两个操作都不会初始化 CLIP：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset \
  --reject-list ./review_reject_paths.txt \
  --apply --run-id visual_review_v1

python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset \
  --dedupe-exact \
  --apply --run-id exact_dedupe_v1
```

对 CLIP 隔离的语义误图可用 BLIP 做非破坏性二次复核。默认阈值经过固定标注集
校准，优先保证救回候选的精度；`--review-labels` 可同时输出 precision、recall
和 F1，`--blip-cache-dir` 可避免重复推理：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset \
  --review-quarantine semantic_filter_v2 \
  --review-limit 300 \
  --review-labels ./borderline_review_labels.csv \
  --blip-cache-dir ./.filter_cache/blip \
  --run-id blip_review_v1
```

人工确认救回候选后，可将当前活动图片和批准列表导出为独立数据集，不修改源数据集：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset \
  --review-quarantine semantic_filter_v2 \
  --export-review-list ./approved_recovery_paths.txt \
  --export-output ./dataset_optimized
```

感知近重复检查结合 pHash、dHash、宽高比和平均颜色，默认仅生成报告；确认后加入
`--apply` 才会隔离命中项：

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset_optimized \
  --dedupe-near \
  --run-id near_dedupe_check_v1
```

```bash
python -m smart_spider.dataset_filter_cli \
  --dataset ./dataset_truck_loading_area_door_operation_5000_20260831 \
  --restore 20260901_120000
```

常用控制：`--profile`、`--min-relevance`、`--mismatch-margin`、
`--advertisement-score`、`--advertisement-margin`、`--text-area-ratio`、
`--minimum-scene-evidence`、`--scene-evidence-margin`。
完整判定写入 `_filter_runs/{run_id}/decisions.jsonl`（试跑）或
`_quarantine/{run_id}/decisions.jsonl`（正式应用）。

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

数据集双轨（ADR-0009 Facade 收敛）：

```
CLI / API
  ├─ DatasetCrawlConfig → DatasetCrawler（图片兼容门面）
  └─ MultimodalJobConfig → MultimodalDatasetOrchestrator
              └─ shared: pipeline.ObjectStore / TaskQueue（本地 FS + SQLite 默认）
```

运行产物请放在 `dataset_*` / `output_*` / `.artifacts/` 等目录（已 gitignore）。轻量任务 API：`pip install -e ".[api]" && smart-spider-api`；worker：`smart-spider-worker`（与 API 共用 SQLite 队列）。可选插件：`pip install -e ".[redis]"` / `".[s3]"` / `".[config]"`（YAML）。

配置文件示例：

```bash
python -m smart_spider.dataset_cli --config job.yaml --dump-config resolved.json
python -m smart_spider.dataset_cli --config job.yaml --total 100
```

## License

见 [LICENSE](./LICENSE)

## 抖音视频采集

支持关键词搜索、单视频（含分享短链接和分享文案）、用户主页作品列表。
参考 [XiaoFeng2233/douyin-spider](https://github.com/XiaoFeng2233/douyin-spider)
的单视频/主页入口设计，以 Python 独立实现。通过 Playwright 读取页面和视频接口响应，
在同一页面滚动加载并按视频 ID 去重；图集和直播不作为视频采集。

```bash
pip install -e ".[browser,video]"
playwright install chromium

# 在原有 SmartSpider 命令中使用抖音搜索
python spider.py --keywords 猫咪 --media_types video \
  --search_engines douyin --max_items 20 --cookies_file ./cookies.txt

# 专用命令：搜索视频
python -m smart_spider.douyin_cli --keyword 猫咪 --max-items 20 \
  --cookies-file ./cookies.txt --output ./output_douyin

# 视频链接或分享文案（替换成实际链接）
python -m smart_spider.douyin_cli --url 'https://v.douyin.com/你的分享码/' \
  --cookies-file ./cookies.txt

# 用户主页批量下载（替换成实际主页）
python -m smart_spider.douyin_cli --url 'https://www.douyin.com/user/用户标识' \
  --max-items 100 --cookies-file ./cookies.txt

# 显示浏览器，并留出两分钟手动登录或完成验证
python -m smart_spider.douyin_cli --keyword 猫咪 --max-items 5 \
  --no-headless --login-wait 120

# 已安装 Chrome 时可直接使用，无需下载 Playwright 完整浏览器
python -m smart_spider.douyin_cli --keyword 三角洲行动 --max-items 10 \
  --browser-channel chrome --no-headless --login-wait 180
```

`--browser-channel chrome` 使用独立的临时浏览器会话，不读取日常 Chrome 的个人资料，
手动登录或验证仅对本次会话有效。没有 Cookie 时，可在等待时间内自行完成页面验证。

安装后也可使用 `smart-spider-douyin`。`--cookies-file` 使用 Netscape cookies.txt 格式，
同时用于浏览器和 yt-dlp；仅抖音相关且未过期的 Cookie 会导入浏览器。省略此参数可尝试
匿名访问，遇到登录/验证或空结果时会报告原因。不会自动绕过验证码或获取私密作品。

专用命令支持 `--metadata-only`（只发现和导出元数据）、`--max-scrolls 50`（滚动次数上限）、
`--timeout 30`（页面和网络超时）、`--proxy`（浏览器和下载共用代理）、
`--max-size 500m`（单视频大小上限）。`--max-items` 是最多发现/尝试下载的数量，
作品不足、平台限制或下载失败时可能达不到该数量。只有发现成功且没有下载失败才返回退出码 0；
空结果或部分下载失败返回 1。

输出按 `output_douyin/{视频ID}/` 组织，包含视频和同 ID 的 JSON 元数据；
`douyin_results.json` 记录本次发现数、下载成功数和每条结果状态。
视频优先使用页面实际返回的 MP4 地址，以有界流式下载校验大小和完整性；
没有直链时交给 yt-dlp，可能需要有效的 cookies.txt。直链下载保留平台返回的版本，
不保证去水印，也不执行格式转码；原有 `--video_format` 仅适用于 yt-dlp 路径。
不在结果清单中保存临时签名直链或 Cookie。图集、直播和评论采集不在此入口的范围内。

抖音不会自动加入默认搜索引擎列表，需要显式选择 `douyin`。
本节已接入的入口为 `spider.py` / `SmartSpider` 和专用 `douyin_cli`。
真实可用性受登录态、网络和平台页面变化影响，单元测试不代表线上始终可用。

抖音发现阶段失败时，输出目录中的 `douyin_diagnostics.json` 会记录页面标题、
搜索/作品接口路径、HTTP 状态码和解析到的视频数，便于区分验证拦截与解析问题。
诊断文件不包含 Cookie、接口查询参数和响应原文。

人工验证推荐使用确认模式，浏览器会持续等待，直到你看到搜索结果后在终端按回车，
不会因固定等待时间结束而丢失临时会话：

```bash
python -m smart_spider.douyin_cli --keyword 三角洲行动 --max-items 10 \
  --browser-channel chrome --wait-for-login
```

该模式自动显示浏览器；验证期间继续处理页面事件，关闭浏览器或终端输入结束会报错，
不会被当作已经完成验证。

### 小红书独立关键词采集

采集搜索结果封面（不是笔记完整相册），使用专用 Chrome 登录目录，图片经过解码校验与内容去重。

```bash
python -m smart_spider.xiaohongshu_cli --keyword "高颜值 妹子照片" \
  --max-items 50 --output output_xiaohongshu_portraits_50 --wait-for-login
```

在弹出的专用 Chrome 完成登录后，在运行命令的终端按回车继续。默认登录目录为 `.artifacts/xiaohongshu-profile`，仅保存在本地；后续运行可省略 `--wait-for-login`，也可添加 `--headless` 测试无窗口采集。不要同时使用同一登录目录运行多个任务。

安装项目后也可以运行 `smart-spider-xiaohongshu`。输出 `results.json` 包含图片来源、尺寸、SHA-256 和失败数量。只有达到指定的不重复图片数量才返回成功；搜索耗尽、登录失效或失败时返回非零退出码，并保留已下载图片及报告。已有内容相同的文件不会重复写入。此入口独立于通用引擎中旧的小红书页面解析器。
