# ADR-0004：以独立多模态编排器承接新任务

## 状态

Accepted

## 背景

现有 `DatasetCrawler` 已经包含图片下载、CLIP 过滤、分桶计数和旧 metadata 输出。直接把文本、网页上下文和 Browser Use 路由塞进这个类，会扩大并发、状态和兼容性风险。

## 决策

新增 `MultimodalDatasetOrchestrator` 作为通用任务入口，复用已有 HTTP、搜索引擎、站点解析器、SQLite 和去重基础设施；`DatasetCrawler` 保留为图像采集兼容执行器。

多模态编排器通过 `SourceCallable`、`AnnotationBackend` 和 `image_fetcher` 注入外部能力，默认使用静态来源，Browser Use 和云模型由调用方显式配置。

## 结果

- 新旧任务可以并行演进，旧 CLI 行为不变；
- 多模态任务的状态、资产和 manifest 有独立生命周期；
- 适配器可用 fake 实现测试，无需启动浏览器或访问云端；
- 后续可以在不重写图片下载器的情况下增加音频、视频和多轮对话样本。

代价是短期存在两个入口，公共文档需要明确使用场景；完成迁移后再评估是否合并底层 worker。
