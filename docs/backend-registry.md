# 后端元数据注册表

`src/askai_cli/registry.py` 是内置后端元数据的唯一运行时来源。每个 `ProviderSpec` 定义
标识、显示名、快捷入口、默认配置、菜单位置和可选的自动选择位置。API 常用型号以只读的
`ModelSpec` 记录放在同一注册表，第一项派生内置默认型号，避免重复维护 ID。

- `providers.py` 从注册表获取预设与自动顺序，合并用户覆盖时复制预设，保留原有后端选择逻辑。
- `setup.py` 从注册表获取显示名、菜单顺序和恢复默认配置；自定义后端继续按配置名显示。
- `shells.py` 与普通 CLI 使用同一份快捷入口映射。
- 预设及索引只读，配置覆盖不会改变后续请求的默认值。

菜单顺序与自动选择顺序是两个不同的产品行为，分别记录，避免用字典插入顺序隐式控制两者。
Codex 优先，Claude 没有自动顺序，因此即使安装也不会被自动调用；DeepSeek API 与 DSH
各有独立注册项。网页搜索开关仍属于默认配置，用户可按需覆盖。

常用型号只用于原官方 API 地址；自定义网关不套用原服务型号。菜单优先显示选择项，
更多型号按需向服务查询，手动输入仅在高级设置中提供。Codex/DSH 默认跟随原生程序；Claude 使用隔离设置。askai 不复制 CLI 的原生型号目录。

增加内置后端时，在注册表增加记录，并实现实际需要的适配器；已有通用 API 或 command
适配器适用时无需新增适配器。用户自定义后端无需修改注册表。

`pyproject.toml` 的安装入口必须静态声明，源码 `bin/` 脚本和 Homebrew Formula 也属于
分发层。`tests/test_registry.py` 校验它们与注册表一致，并保护自动选择、固定路由、菜单顺序、
自定义后端和配置隔离行为。

`ProviderSpec.models_source` 与 `models_checked_on` 记录内置型号的来源和核对日期；`reasoning_levels` 只声明适配器已实现的可请求档位，不保证账号或服务端支持。DSH 的默认 `reasoning_effort` 集中在 `defaults`。

Claude 调用使用空 `--setting-sources`，不读取用户、项目或本地设置文件；显式配置的型号通过 `--model` 传递。未指定型号时由隔离运行环境决定，界面不应声称沿用用户设置文件中的型号。

Codex 的 `reasoning_effort` 通过单次 `codex exec -c model_reasoning_effort=...` 传入；未指定时不添加覆盖项。菜单允许恢复沿用原生配置，不写入 Codex 全局配置。可用档位取决于客户端与模型，参见[官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

API 密钥统一由 `credentials.py` 解析：设置菜单、就绪检查、型号目录请求和推理请求共用当前来源。本地密钥与普通配置分离，按 API 地址及环境变量名绑定；环境变量导入或粘贴后持久保存，删除操作显式确认。CLI 登录凭据不进入此存储。
