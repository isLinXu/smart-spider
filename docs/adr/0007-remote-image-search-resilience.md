# ADR 0007：增强远程以图搜图的结果就绪判断与 Provider 容错

## 状态

已接受

## 背景

百度、Bing 和 Google Lens 都是动态网页。上传完成后，结果可能通过 URL 跳转或异步 DOM 注入出现；固定 sleep 容易过早解析，而单一入口在地区重定向或页面改版时会直接失败。

## 决策

- Provider 声明可用的 `start_urls`，每次尝试轮换入口；`ReverseImageSearcher.max_attempts` 默认值为 2。
- 等待逻辑同时支持 URL 变化和 Provider 专属结果选择器，并把结果尚未出现记录为可诊断错误。
- `ProviderSearchResponse` 返回 `attempts` 和聚合后的 `elapsed_ms`；调试目录为每次尝试保留独立的 HTML 与截图。
- 结果统一记录 `metadata.source_domain`，Bing 额外保留其卡片格式字段。

## 取舍

重试和动态等待提高了 SPA 页面及区域入口变化时的成功率，但会增加单个 Provider 的最长耗时；调用方可使用 `--attempts 1` 和较小的 `--timeout` 控制成本。验证码、登录和区域限制仍然由 Provider 返回错误，不通过自动化绕过。
