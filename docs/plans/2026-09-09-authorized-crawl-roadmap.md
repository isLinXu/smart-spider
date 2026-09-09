# 授权可达爬取路线图（待确认）

**Date:** 2026-09-09  
**Status:** Confirmed — implementing S17→S20  
**Goal:** 在合法授权范围内，尽量覆盖可访问的公开/登录页面；用浏览器完成真人式交互；对拦截可观测、可恢复；**不**追求「任意站点永不被拦」。

---

## 1. 产品目标（锁定）

| 要 | 不要 |
|----|------|
| 站点白名单 / 用户自备会话 | 指纹伪装、验证码打码、WAF 对抗 |
| 静态 → 浏览器剧本降级 | 保证绕过反爬 |
| 礼貌限速 + robots（默开） | 无视 ToS / 未授权抓取 |
| 拦截分类 → retry / dead-letter | 静默硬刚挑战页 |
| 发布清单 / 许可 / URL 脱敏 | 把受保护内容当默认可爬 |

---

## 2. 已完成底座（S12–S16）

| 切片 | 能力 |
|------|------|
| S12 | 队列租约、重试/死信、多进程 worker、任务 API |
| S13 | `SiteCrawlPolicy`、授权浏览剧本、拦截分类 |
| S14 | `storage_state` 会话复用、静态失败 → Playbook 回退 |
| S15 | 路由原因/block 观测、`unified_report`、发布清单 |
| S16 | `/metrics`、dataset 轨道合规对齐 |

---

## 3. 建议下一阶段（S17–S20）

按「授权站点可用性」优先，不做豆包草稿里的 Parquet 优先。

### S17 — 站点画像与任务模板（配置产品化）
**Why:** 现在策略散落在 CLI/payload；运营一个站点仍要手写 JSON。

- YAML/JSON `SiteProfile`：`allow_hosts`、速率、robots、会话路径、max_depth、scroll、是否强制浏览器
- 任务模板：`seed_urls` + profile → 一键 enqueue `authorized_browse` / multimodal
- CLI：`smart-spider-browse --profile sites/example.yaml --url ...`
- 验收：同一 profile 可复跑；非法域在入队前拒绝

### S18 — 交互剧本扩展（正当自动化）
**Why:** 当前剧本只有 navigate/scroll/抽链；真实站点常需「点下一页 / 展开 / 等选择器」。

- 声明式步骤：`wait_for`、`click`、`type`（仅用户提供的选择器与账号上下文）
- 深度遍历：同域 BFS，受 `max_depth` / 链接预算约束；与 S12 队列衔接
- 失败分类复用 `block_signals`；挑战页仍进 dead，**不**自动破解
- 验收：对本地 fixture HTML 走完步骤；真实站只测白名单域

### S19 — 人工介入与会话运营
**Why:** 登录墙/挑战需要人，而不是爬虫硬闯。

- dead 任务面板语义：API 已有 list/retry；补「按 block kind 过滤」与原因摘要
- 会话工作流：headful 登录一次 → `--export-storage-state` → 无头复用（文档 + 可选小 CLI）
- 可选：任务挂起状态 `needs_attention`（或沿用 `dead` + 标签），人工更新会话后 `retry`
- 验收：挑战页 → dead → 导入新 storage_state → retry 成功（mock）

### S20 — 端到端观测与发布门禁
**Why:** metrics/清单已有，缺「一次授权爬取任务」的统一验收视图。

- job 级 trace 摘要：seed → route 决策 → block → 入队链接数 → 接受样本数（写入 unified_report）
- `/metrics` 增加 browse 相关计数（policy_denied、robots_skip、challenge_dead）
- 发布门禁：`publish_checklist.ready==false` 时 CLI 非零退出（可 `--allow-unready`）
- 验收：一次 fixture 浏览任务产出完整报告且 metrics 可刮取

---

## 4. 明确延后（非本阶段）

- Parquet / 资产并行物化（原豆包 S13）
- 完整 Prometheus + Grafana 平台、分布式 tracing
- Agent ReAct 全自动未知站探索（可在 S18 稳定后再接）
- 任何反检测 / 打码 / 住宅代理「防拦截」方案

---

## 5. 建议实施顺序与体量

| 顺序 | 切片 | 预估 | 依赖 |
|------|------|------|------|
| 1 | S17 SiteProfile + browse CLI | 小–中 | S13/S14 |
| 2 | S18 声明式交互 + BFS | 中 | S17 |
| 3 | S19 人工会话闭环 | 小–中 | S12/S14 |
| 4 | S20 门禁与 browse metrics | 小 | S15/S16 |

推荐确认后**先做 S17**，把「授权站点」变成一等配置，再扩剧本。

---

## 6. 请你确认的问题

1. 下一刀是否同意 **S17 → S18 → S19 → S20**，还是要调序（例如先 S19 会话运营）？
2. S18 的交互步骤是否限制为**声明式 YAML**（推荐），还是继续依赖 Browser Use / LLM Agent？
3. 目标站点类型更接近：**(A) 公开内容站** / **(B) 需登录的自有业务站** / **(C) 两者都要**？
4. 延后项里有没有必须提前插入的（例如 Parquet）？

确认后按你的选择落 ADR + 实现；在确认前不改代码。
