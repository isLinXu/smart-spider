# ADR-0005：以批量标注和 JSONL 分片支撑大规模生产

## 状态

Accepted

## 背景

数据规模从几千条扩展到几十万条后，单样本调用模型会放大网络和推理开销；单个无限增长的
manifest 也不利于断点恢复、并行消费和失败重跑。与此同时，核心包不应为了分片强制依赖
PyArrow 等可选重型依赖。

## 决策

在 `AnnotationRouter` 中增加批量接口探测：后端提供 `annotate_batch` 时按批调用，否则自动
逐样本兼容。编排器以 `annotation_batch_size` 限制内存占用。

manifest 默认继续写 `manifest.jsonl`；配置分片后按记录数写入有序的
`manifest-00000.jsonl` 文件，并在已有最后分片未满时恢复追加。质量统计单独写成
`quality_report.json`，不把统计字段混入每条样本的稳定契约。

## 结果

- 旧调用方无需配置即可保持原始单文件输出；
- 批量模型、规则后端和只支持单样本的后端可以共存；
- 分片和报告只依赖标准库，适合基础安装和离线生产；
- Parquet、对象存储和分布式队列仍可作为后续 writer/worker 插件接入。

代价是分片 manifest 仍是 JSONL，分析型查询效率不如 Parquet；质量报告是运行结束时写入，
强制终止时需依赖已有 manifest 和 SQLite 状态恢复。
