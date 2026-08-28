# 本地图片以图搜图设计

## 范围

第一版支持：给定一张本地查询图片，在 SmartSpider 已采集目录或任意本地图片目录中，按视觉相似度返回 Top-K 图片。

第一版不直接调用百度、Bing、Google 等第三方反向图片上传接口。此类接口通常需要单独的 API 凭据、上传协议和服务条款校验，且稳定性与可用性不能由本项目保证。后续可在不改变本地索引契约的前提下增加远程 provider。

## 方案

复用项目已有的 CLIP 模型：

1. 扫描目录中的常见图片格式，解码为 RGB。
2. 用 CLIP 批量生成图片向量并做 L2 归一化。
3. 将向量、绝对路径和基础图片元数据保存到压缩 `npz` 文件。
4. 查询时只对查询图片编码一次，与索引矩阵点积得到余弦相似度。
5. 按分数降序返回结果，可用阈值过滤，并默认排除查询图片自身。

这个规模下 NumPy 矩阵检索足够简单；当图片量明显扩大后，再替换为 FAISS 或其他向量数据库，不改变 `ImageSimilarityIndex.search()` 的调用语义。

## 组件与接口

- `smart_spider/image_retrieval.py`：索引、持久化、查询结果和编码器协议。
- `smart_spider/perception/clip_inference.py`：新增批量 `encode_images()`，减少建立索引时的模型调用开销。
- `smart_spider/image_search_cli.py`：建立索引和查询的 JSON CLI。
- `smart_spider/dataset_crawler.py`：在现有下载校验链中增加查询图片二次筛选。
- `smart_spider/dataset_cli.py`：增加 `--query-image` 与 `--image-similarity-threshold`。
- `pyproject.toml`：注册 `smart-spider-image-search` 命令。

索引文件包含 `format_version`，用于未来升级格式时拒绝静默误读；保存采用临时文件 + 原子替换，避免任务中断留下半个索引。

## 错误处理与质量边界

- 不存在、损坏或无法解码的图片计入 `skipped_files`，不会中断其他图片索引。
- 编码器返回空向量、零向量、非有限值或错误维度时直接报错，避免生成不可用索引。
- 查询参数校验 `top_k > 0`，相似度阈值限制在 `[-1, 1]`。
- 空索引可以保存和加载，查询返回空结果。
- `DatasetCrawler` 传入 `query_image` 时强制要求 CLIP；候选失败会释放暂存去重 claim，避免影响重试。

## 验证

- 使用可预测的测试编码器验证相似度排序、损坏图片跳过、阈值/参数校验。
- 验证索引保存/加载后结果一致。
- 验证 CLI 参数解析、Python 编译和全量回归测试。
