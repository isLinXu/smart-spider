# ADR-0010：P0 工程化加固（测试 / 契约 / 配置 / 报告 / 拆分）

## 状态

Accepted

## 背景

2026-09-07 优化评估确认架构方向正确，短板在工程化密度：超千行模块、CLI 参数分散、报告口径不一、契约宽松、关键链路回归不足。

## 决策

1. **先测后拆**：S5 真值回归与仓库 E2E 先于大文件拆分。
2. **契约加固**：`SampleRecord` / `CandidateResource` 等增加 `validate()` 与 `format_version`。
3. **配置文件化**：以 `DatasetCrawlConfig` / `MultimodalJobConfig` 为 schema，增加 YAML 加载与 `--dump-config`。
4. **统一报告**：新增 `UnifiedReport` schema，旧报告字段经适配映射。
5. **渐进拆分**：保持门面 API，内部抽出 `rate_limit` / `proxy_pool` / `http_metrics` 与 filter 子模块。

## 结果

- 行为对外兼容；质量门禁与提交路径有回归保护。
- 配置与报告可版本化、可对比。
- 大文件拆分为后续 S6–S11 清障。

## Non-goals

不重写双轨门面；不在本期引入 Kafka / 分布式任务系统；不把 LLM 引入写盘路径。
