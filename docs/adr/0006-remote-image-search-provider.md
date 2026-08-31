# ADR 0006：通过浏览器 Provider 接入远程以图搜图

## 状态

已接受

## 背景

项目已有本地 CLIP 图片索引和关键词图片采集能力，但用户需要把本地图片直接提交到网络反向图片搜索页面，并获得外部网页结果。百度、Bing、Google 的上传流程和页面契约不同，且公开 API 并不能覆盖完整的网页反向搜图体验。

## 决策

增加独立的 `ReverseImageSearcher` 和 Provider 注册表，使用 Playwright 打开各站点公开页面、提交本地文件并解析结果。Provider 之间共享统一的数据类，但不共享站点选择器和解析规则。

首批 Provider 为百度识图、Bing Visual Search、Google Lens。CLI 默认对三个 Provider 顺序执行，单个 Provider 失败时保留错误并继续其他 Provider。

## 取舍

优点：

- 真实模拟网页上传，覆盖网页反向搜图功能；
- Provider 隔离，页面结构变化只影响单个站点；
- 支持用户目录、代理和非 headless 模式，便于登录和人工验证；
- 不需要把图片先上传到本项目自建中转服务器。

代价：

- 依赖 Playwright/Chromium 或本机 Chrome；
- 页面结构、验证码、登录和服务条款可能导致结果不稳定；
- 解析结果是 best-effort，不提供跨 Provider 的统一相似度分数。

## 后续

如将来获得稳定且合规的官方 API，可以在相同 Provider 契约下增加 API 实现；不改变现有 CLI 和 JSON 结果格式。
