# ADR-0009：Facade 收敛与存储插件

## 状态

Accepted

## 背景

ADR-0004 引入独立 `MultimodalDatasetOrchestrator` 后，短期双入口并存。
`DatasetCrawler` 仍承载图片兼容路径（CLIP、场景配额、`batch_*` 落盘），
而多模态编排器走契约化 sample / AssetStore。公共发现、下载与提交逻辑
重复，巨型模块与超长 argparse 增加演进成本。ADR-0001/0005/0008 已约定
未来以插件方式接入队列与对象存储，但缺少统一接口。

## 决策

1. **Facade 收敛（非合并入口）**  
   - 保留 `DatasetCrawler` 为图片兼容门面。  
   - 保留 `MultimodalDatasetOrchestrator` 为模态中立编排器。  
   - 抽取共享 pipeline 基础（discovery / materialize / commit 协议）与
     本地默认实现，两边复用。

2. **统一配置对象**  
   - 新增 `DatasetCrawlConfig`（对齐既有 `MultimodalJobConfig`）。  
   - CLI 仅负责 parse → config → facade；保留原有 flag 语义。

3. **存储插件**  
   - 引入 `TaskQueue` / `ObjectStore` Protocol。  
   - 默认实现：SQLite 本地队列、本地文件系统对象存储。  
   - Redis/S3 等作为后续插件，不在本期落地。

4. **轻量任务 API**  
   - 可选 FastAPI 骨架：提交/查询任务配置与状态，默认仍走本地队列。

## 结果

- 双 CLI 行为兼容；公共能力单点演进。  
- 配置可校验、可序列化，便于 API 与测试注入。  
- 水平扩展路径明确：替换 queue/store 插件即可，无需重写门面。

## 代价

- 短期多一层抽象；需保证 re-export 与 kwargs 兼容。  
- FastAPI 为 optional extra，未安装时 API 入口不可用。
