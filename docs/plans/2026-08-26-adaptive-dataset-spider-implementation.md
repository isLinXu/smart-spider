# Adaptive Multimodal Dataset Spider Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将现有 SmartSpider 演进为可恢复、可审计、支持文本、图片、图文关系、多标签和多源扩展的通用多模态训练数据生产内核，先保证单机几千条样本稳定，再为几十万条样本保留分片扩展接口。

**Architecture:** 保留现有 `DatasetCrawler`、搜索引擎、站点解析器和 Browser Use 组件作为兼容入口；新增 modality-neutral 的候选资源/样本契约和 SQLite WAL 状态库。第一阶段将文本、媒体、跨模态关系的保存和状态记录做成幂等边界，后续再接入自适应来源路由和模型标签路由。

**Tech Stack:** Python 3.10+, dataclasses, SQLite WAL, Pillow, NumPy, pytest, 现有 SmartHttpClient/UrlDeduplicator/JSONL manifest 文件存储。

---

## Phase 2: 多模态抽取与自适应路由

第二阶段新增 `PageSampleExtractor`、`AdaptiveSourceRouter` 和 `AnnotationRouter`。
页面抽取器负责把静态 HTML 或 Browser Use 返回的 HTML 统一转换为文本、图片、网页上下文及其关系；来源路由器只根据状态码、页面长度、阻断和候选数量决定是否切换浏览器；标注路由器通过注入的本地、云端或规则后端生成 `LabelDecision`，不把任何模型供应商写死在核心下载路径。

第二阶段编排闭环由 `MultimodalDatasetOrchestrator` 执行，`SearchDiscoverySource`
和 `SiteDiscoverySource` 复用现有搜索引擎/站点解析器，图片可通过 `AssetStore`
按内容哈希落地，最终输出 `assets/`、`manifest.jsonl` 和 SQLite 状态库。
`smart-spider-multimodal` 提供 queries、URL 和站点三种启动入口。

## Phase 3: 规模化生产闭环

第三阶段在不改变默认 `manifest.jsonl` 的前提下，增加以下能力：

- `AnnotationRouter.annotate_batch` 优先调用后端的 `annotate_batch`，没有批量接口时逐样本降级；
- `annotation_batch_size` 控制一个批次的上限，避免几十万条任务一次性占满内存；
- `MultimodalManifestWriter` 支持 `manifest-00000.jsonl` 形式的记录数分片，并能在已有最后分片上恢复追加；
- `quality_report.json` 汇总接受/拒绝、模态、任务类型、标签、路由和失败原因，便于抽检与审计。

CLI 对应参数为 `--annotation-batch-size`、`--manifest-shard-size` 和
`--quality-report`。`--manifest-shard-size 0` 保留单文件兼容模式。

## Task 1: 建立实现基线与回归入口

**Files:**
- Create: `tests/test_dataset_contracts.py`
- Create: `tests/test_dataset_state.py`
- Modify: `tests/test_dataset_crawler.py`

**Step 1: 先运行当前相关测试，记录基线**

Run: `pytest -q tests/test_dataset_crawler.py tests/test_engines.py`

Expected: 现有数据集测试通过；Baidu 图片 URL 优先级测试暴露当前契约不一致。

**Step 2: 为新契约写失败测试**

覆盖 `CandidateResource`、多标签 `LabelDecision`、固定/发现/混合标签策略，以及 SQLite 候选租约、重试和样本幂等。

**Step 3: 保留旧入口行为**

不删除旧的 `DatasetDirManager.get_save_path` 和 `UrlDeduplicator` API；新 API 通过新增方法接入，避免影响已有调用方。

## Task 2: 修复图片保存链路的类型和并发问题

**Files:**
- Modify: `smart_spider/dataset_crawler.py`
- Modify: `smart_spider/perception/page_perception.py`
- Modify: `tests/test_dataset_crawler.py`
- Create: `tests/test_page_perception.py`

**Step 1: 添加真实 JPEG 下载测试**

使用内存生成的有效图片和假的 HTTP response，验证 `_download_and_save` 能完成解码、保存和 metadata 写入。

**Step 2: 实现原子保存**

让目录管理器在锁内完成目标数量检查、编号分配、临时文件写入和原子替换；只有文件写入成功才增加计数，避免并发 worker 超过目标或 metadata 使用错误编号。

**Step 3: 修复 Response/bytes 边界**

`PagePerception` 使用 `SmartHttpClient.get_bytes` 或 response.content，不再把 response 对象直接传给 `BytesIO`。

**Step 4: 验证失败不会消耗编号**

文件写入异常、格式验证失败和目标已满都不能增加保存计数。

## Task 3: 实现统一数据契约与可选标签策略

**Files:**
- Create: `smart_spider/dataset_contracts.py`
- Create: `tests/test_dataset_contracts.py`
- Modify: `smart_spider/__init__.py`

**Step 1: 定义候选资源和样本记录**

引入可序列化的 `CandidateResource`、`QualityMetrics`、`LabelDecision`、`SampleRecord`，字段覆盖来源、查询、原始 URL、质量证据、标签列表和 pipeline 版本。

**Step 2: 定义标签模式**

实现 `LabelMode.FIXED`、`DISCOVERY`、`HYBRID`。固定标签限制正式标签集合；发现模式输出候选标签；混合模式允许固定标签进入正式结果、自动标签进入候选结果。单张图片的正式标签使用列表，不强制互斥。

**Step 3: 增加 JSONL 兼容序列化**

所有契约提供 `to_dict`/`from_dict`，保证未来可以无缝写入 manifest、candidates 和 Parquet 转换层。

## Task 4: 实现 SQLite WAL 状态库

**Files:**
- Create: `smart_spider/dataset_state.py`
- Create: `tests/test_dataset_state.py`

**Step 1: 建立最小状态表**

增加 `jobs`、`candidates`、`samples`、`events` 四类记录，保存状态、重试次数、租约时间、错误和幂等键。

**Step 2: 实现候选资源幂等入库**

以 URL 的规范化哈希作为候选键；重复发现只更新来源元数据，不创建重复候选。

**Step 3: 实现租约和恢复**

提供 `claim_candidates`、`complete_candidate`、`fail_candidate`、`recover_expired_leases`。临时网络失败进入 `retry_wait`，超过最大次数才进入 `failed`。

**Step 4: 实现样本幂等提交**

以内容哈希或候选键防止同一资源重复写入样本状态；状态库提交和 manifest 写入保持可重试。

## Task 5: 接入 DatasetCrawler 的最小闭环

**Files:**
- Modify: `smart_spider/dataset_crawler.py`
- Modify: `smart_spider/dataset_cli.py`
- Modify: `tests/test_dataset_crawler.py`

**Step 1: 初始化任务状态库**

为 `DatasetCrawler` 增加可选 `state_db` 和 `job_id` 参数；默认开启本地 SQLite，传空值时保持轻量旧模式。

**Step 2: 记录候选、拒绝和接受事件**

在下载前后写入候选状态、过滤原因、质量指标和最终文件路径；失败重试不污染永久成功去重。

**Step 3: 输出统一 manifest**

保留现有 `metadata.jsonl`，同时生成兼容设计文档的数据字段，`labels` 始终为列表。

**Step 4: 增加 CLI 可选配置**

支持 `--label-mode fixed|discovery|hybrid`、`--labels` 和 `--state-db`，不改变旧命令的默认行为。

## Task 6: 修复打包和现有回归契约

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/test_engines.py`

**Step 1: 修复 setuptools build backend**

改用标准 `setuptools.build_meta`，并确保根目录 CLI 模块会被打包。

**Step 2: 明确 Baidu URL 优先级测试**

测试 fixture 同时覆盖 `middleURL`、`hoverURL`、`thumbURL` 和 `objURL`，锁定“原图优先，缩略图兜底”的预期。

**Step 3: 验证可构建**

Run: `python -m build --wheel --no-isolation`

Expected: wheel 构建成功，且不修改用户源码。

## Task 7: 全量验证与交付报告

**Step 1: 运行针对性测试**

Run: `pytest -q tests/test_dataset_crawler.py tests/test_dataset_contracts.py tests/test_dataset_state.py tests/test_page_perception.py`

**Step 2: 运行全量测试**

Run: `pytest -q`

**Step 3: 做最小规模真实闭环演练**

用本地假的图片响应完成若干张、多线程、重复 URL、失败重试和任务恢复验证，检查 `metadata.jsonl`、manifest 和 SQLite 状态一致。

**Step 4: 记录剩余风险**

明确 Browser Use 自适应路由、批量 CLIP 推理、云端模型和 Parquet 分片属于可选扩展，不在基础闭环中隐式开启。
