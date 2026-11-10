# 后端与接口参考

以下资料用于查阅后端接口、模型配置与 Shell 集成方式。模型服务和原生 CLI 会更新，请以所用版本的官方文档为准。

- ccline：[文档](https://ccline.jianshuo.dev/)、[主程序](https://github.com/jianshuo/ccline/blob/main/ccline)、[zsh 集成](https://github.com/jianshuo/ccline/blob/main/ccline.zsh)。
- Codex：[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[参数参考](https://learn.chatgpt.com/docs/developer-commands)。
- Claude：[CLI 参考](https://code.claude.com/docs/en/cli-reference)、[程序化调用](https://code.claude.com/docs/en/headless)。
- DSH：[CLI 参数](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md)、[headless 认证及行为](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)。
- DeepSeek：[首次调用](https://api-docs.deepseek.com/)、[型号目录](https://api-docs.deepseek.com/quick_start/pricing/)。
- Kimi：[中国区 API 型号目录](https://platform.kimi.com/docs/models)、[服务地址与协议](https://platform.kimi.com/docs/api/overview)。Kimi Code 订阅是另一种服务，不能将其短型号 ID 与此 API 的 `kimi-…` ID 混用。
- MiniMax：[中国区模型与 OpenAI 兼容接口](https://platform.minimax.cn/docs/guides/text-generation)。
- GLM：[型号总览](https://docs.bigmodel.cn/cn/guide/start/model-overview)、[GLM-5.3 与接入地址](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)、[GLM-5.3-Flash/FlashX](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)。
- fish：[fish_command_not_found](https://fishshell.com/docs/current/cmds/fish_command_not_found.html)。
- Homebrew：[Formula Cookbook](https://docs.brew.sh/Formula-Cookbook)、[Tap 配置](https://docs.brew.sh/Taps.html)。
- Codex 官方网页查询：[Web search](https://learn.chatgpt.com/docs/web-search)。

## 内置型号目录

最近一次官方文档核对：**2026-11-06**。每次发布前需按上述官方来源重新核对；本表不代表账号实际权限或连接测试通过。

| API 服务 | 新配置的默认型号 | 其他常用选项 |
| --- | --- | --- |
| DeepSeek | `deepseek-flash` | `deepseek-v4-pro` |
| Kimi | `kimi-k3.1-pro` | `kimi-k3.1-pro-highspeed`、`kimi-k3` |
| MiniMax | `MiniMax-M3.1-Flash` | `MiniMax-M3` |
| GLM | `glm-5.3` | `glm-5.3-flash`、`glm-5.3-flashx` |

Kimi 官方已将 `kimi-k2.5` 标记为下线，因此不再将其作为预设或新配置默认值。已经保存的型号不会自动替换；请在设置中自行选择新型号。Codex/DSH 连接沿用原生配置；Claude 使用隔离设置，不加载 user/project/local 设置文件。未指定 Claude 型号时仍可能受其环境变量影响，未验证实际型号。askai 不维护 CLI 原生型号清单。

## 终端输入边界

所有交互输入通过 `interaction.py` 管理，业务菜单不自行创建 `PromptSession` 或直接读取终端。菜单、文字和密钥具有各自的提交与取消语义，但共享输入隔离规则：本阶段的剩余按键不能成为下一阶段的选择。

使用 prompt_toolkit 的输入阶段由同一上下文管理器持有输入流：进入时清除终端队列及库内 typeahead，正常返回、取消或异常时均清除库内缓存并关闭拥有的流。终端模式由库负责恢复。关闭输入流本身不等于清除按文件描述符共享的缓存，因此两者都必须处理。

简单终端按回车提交，通过同一行输入函数检查标准行模式、清除旧输入；不满足模式要求时明确停止，不降级为自动确认。正常文字及括号粘贴可在单次输入中保留多行内容，但菜单不把粘贴内容解释为选择。密钥保持隐藏、不保存输入历史，也不进入通用选择回显。

输入边界测试应覆盖跨组件的用户流程：密钥到保存确认、补充要求到新答案、脚本路径到后续菜单，以及取消、EOF和异常。仅验证单个菜单的按键绑定不足以证明这些边界成立。
