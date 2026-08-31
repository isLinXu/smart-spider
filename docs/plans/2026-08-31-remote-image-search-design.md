# 远程网页以图搜图设计

## 目标

支持用户将本地图片直接上传到百度识图、Bing Visual Search 和 Google Lens 等网页，并返回统一格式的来源网页、图片地址、缩略图、标题和摘要。该能力与已有的本地 CLIP 索引检索、关键词采集后的视觉筛选保持独立。

## 方案

采用 Playwright 驱动真实网页上传，而不是依赖不稳定的私有上传接口：

1. CLI 校验本地图片路径和 Provider 列表。
2. `ReverseImageSearcher` 启动 Chromium/本机 Chrome，可选持久化用户目录、代理和非 headless 模式。
3. 每个 Provider 打开自己的图片搜索页面，通过页面上的文件输入或上传按钮提交图片。
4. 等待结果页加载，检查验证码/人工验证页面，然后由 Provider 解析页面 DOM。
5. 使用统一的 `RemoteImageSearchResult` 和 `ProviderSearchResponse` 返回 JSON。

Provider 失败默认不会影响其他 Provider；`--fail-fast` 可切换为遇错停止。网页结构变化时可以通过 `--debug-dir` 保存 HTML 和截图定位问题。

## Provider 契约

`ReverseImageSearchProvider` 只要求：

- `name` 和 `start_url`；
- `upload(page, image_path, timeout_ms)`；
- `parse_results(html, page_url, top_k)`。

目前实现：

- `BaiduReverseImageProvider`：百度识图网页上传和外部来源链接解析；
- `BingVisualSearchProvider`：Bing 图片页上传，优先解析 `iusc` 卡片元数据；
- `GoogleLensProvider`：Google Lens 上传和外部结果链接解析。

新增 Provider 不需要修改 CLI 或结果契约，只需注册到 `PROVIDER_REGISTRY`。

## 安全与边界

- 只有用户显式运行远程搜图 CLI 时，图片才会离开本机。
- 不自动处理验证码、不绕过登录、不注入第三方账号凭据。
- 默认 headless；遇到验证或登录需求时使用 `--no-headless --user-data-dir ...`，由用户在浏览器中完成操作。
- Provider 返回结果是网页当前可见内容的 best-effort 解析，不能承诺跨站点稳定的结果数量或相似度分数。
- 不把远程网页结果误标为 CLIP 相似度；网页返回的结果没有统一可比的 `similarity` 字段。

## CLI

```bash
smart-spider-reverse-image-search \
    --image ./query.jpg \
    --providers baidu google_lens bing \
    --top-k 20
```

验证登录/验证码时：

```bash
smart-spider-reverse-image-search \
    --image ./query.jpg \
    --providers google_lens \
    --no-headless \
    --user-data-dir ./runtime/reverse-image-browser
```

## 验证策略

- 使用固定 HTML fixture 验证 Bing `iusc`、Google 外部链接和百度外部链接解析。
- 使用 CLI parser 测试验证 Provider、浏览器、代理、持久化目录和输出参数。
- 使用本机 Chrome 做页面启动冒烟。
- 在网络可用且服务未要求验证时，执行单 Provider 真实上传；网络阻断、验证码或页面变更必须作为可诊断的 Provider 错误返回。
