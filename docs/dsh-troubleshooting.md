# DSH 排查说明

## askdsh 启动失败：`$.mode expected nullable but got native`

DSH 0.2.2 将工具执行拆为独立插件，移除了 `tools.mode` 配置。旧版 AskAI 的临时补丁仍写入 `mode: native`，因此 `tools` 插件校验失败，依赖它的其他插件也无法启动。这发生在请求模型之前，与提问内容、Fish 环境变量或账号认证无关。

修复后的 AskAI 使用 `tools` 的空配置覆盖旧字段，沿用 headless profile 提供的原生执行器，同时继续通过工具守卫禁止模型执行工具。这只影响本次调用，不修改 DSH 保存的配置和凭据。遇到此错误应更新 AskAI 的兼容修复，无需删除 `~/.dsh` 或重新登录。

来源：[DSH 0.2.2-rc.1 发布说明](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1)。

## DSH 网页启动后提示模型列表 HTTP 401

`dsh web: http://127.0.0.1:3080/…` 表示网页服务已启动。随后出现：

```text
[llm-deepseek-api-key] model listing from https://api.deepseek.com/models failed (… HTTP 401); using the bundled catalog
```

这是 DeepSeek API key 路由获取模型列表时认证失败，程序回退到自带模型目录。它不等于包安装失败，也不代表后续调用模型已获得授权。下载慢、弃用依赖和 peer dependency 提示，与这个认证错误是不同问题。日志中已经完成的构建步骤不必因此重新执行。

处理方式：

1. 保持 DSH 服务所在终端运行，打开启动日志提供的本地网页。若服务已停止，重新运行原来的 `pnpm dlx @deepseek-ai/dsh web`；不要把这一步与重新配置凭据混为一谈。
2. 如果使用 API key，在网页的模型配置中核对 DeepSeek 官方 API 路由并更新有效密钥；如果使用 DeepSeek 账号，在网页完成账号登录并选择对应账号路由。这两种认证不能互相代替。
3. 重新检查模型列表，并用一个简短问题确认模型能够回答。只有目录回退或网页打开，都不能证明模型调用成功。

DSH 保存的凭据优先于启动环境变量；已有旧密钥时，仅在另一个终端重新设置 `DEEPSEEK_API_KEY` 未必有效。通过 DSH 自己的设置更新对应凭据，不需要删除整个 `~/.dsh`，也不需要重新安装。密钥和登录输入由用户在产品界面完成。

`pnpm dlx @deepseek-ai/dsh web` 会下载到 pnpm 缓存并启动服务，不等价于持久安装全局 `dsh` 命令。因此网页能运行时，`command -v dsh` 仍可能找不到；这也不会使 askai 自动检测到全局 DSH CLI。

来源：[DeepSeek API 错误码](https://api-docs.deepseek.com/quick_start/error_codes/)、[DSH API-key 路由](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek-api-key/src/index.ts)、[DSH 凭据优先级](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/credentials/credentials-local/README.md)。
