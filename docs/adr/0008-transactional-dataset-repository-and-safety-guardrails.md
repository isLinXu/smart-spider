# ADR 0008：事务化数据集仓库与安全场景质量护栏

## 状态

已接受

## 背景

大规模图片采集同时写入图片文件、`metadata.jsonl`、`manifest.jsonl`、URL 去重状态和 SQLite 状态库。进程可能在任意写入边界被终止；旧的多份追加写入会造成计数漂移、重复内容和索引不一致。远程图片还可能通过重定向访问内网地址，搜索来源的成功率和退避成本也缺少可比较指标。

## 决策

- 引入 `DatasetRepository` 作为单机任务的提交边界：最终落盘字节计算 SHA-256，SQLite 以 `(job_id, content_hash)` 唯一键分配编号，文件先写入 staging 并 `fsync`，再原子 rename，最后幂等提交样本。
- SQLite 是事实源；`metadata.jsonl` 和 `manifest.jsonl` 是可重建兼容视图。启动时恢复 `pending/prepared` 记录，并在视图缺失或顺序不一致时重建。
- HTTP 客户端默认仅允许 HTTP(S)，拒绝凭据、localhost、私有/保留地址，并对每个重定向 hop 和最终 URL 重复校验；`allow_private_hosts=True` 仅用于明确受控的内网任务。
- HTTP 与 Discovery 层暴露线程安全的计数、状态码、字节、延迟和来源成功率指标；来源响应将快照写入任务报告。
- 六个安全场景使用版本化 `SceneQualityProfile`，可通过内置中文别名或严格 JSON 覆盖。缺失型违规（无反光衣、未戴安全帽）默认标记为需要人工/专用检测模型复核。

## 取舍

SQLite WAL 和单写者仓库适合当前单机/单输出目录模型，不能替代跨机器队列或对象存储事务；未来水平扩展时应把提交接口迁移到集中式元数据服务。JSONL 仍保留以兼容既有工具，但不再作为恢复依据。最终内容哈希去重会保留编号空洞（失败 reservation 不复用序号），换取并发安全和可审计性。SSRF DNS 校验可能增加一次解析开销，解析失败则交由 HTTP 层报告常规 DNS 错误。

## 影响

- 断点续传不依赖“目录文件数”等近似值，重启后可从 SQLite 计数继续。
- 事件默认 compact payload，避免把完整 HTML/大字段无限写入审计表；需要完整审计时可配置 `event_payload_mode="full"`。
- 质量档案只负责高召回候选筛选，不把模型分数误认为安全事件的最终事实。
