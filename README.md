# askai

简体中文 | [English](README.en.md)

**不知道终端命令怎么写？直接描述你想做的事。**

askai 会向你选择的 AI 提问，展示建议的命令及解释，再由你决定执行、复制或取消。支持 fish、zsh 和 bash，可复用已登录的模型命令行程序，也可连接 API 服务。

```sh
askai "怎样给 notes.txt 的每一行加上行号？"
```

例如，你可能会看到：

```text
[1] 给文件的所有行加上行号
nl -ba notes.txt
原理：-ba 表示给所有行编号，包括空行。结果显示在终端中。

执行：编号  解释：e编号
仅打印到终端：p编号（如 p1）  复制到剪贴板：c编号（如 c1）
q取消 [默认 q]:
```

输入 `c1` 复制命令，输入 `1` 执行，或者直接按回车取消。以上为示例，实际回答可能不同。**执行前请阅读完整命令，AI 的建议也可能出错。**

## 安装

### Homebrew（推荐）

macOS 用户推荐通过 Homebrew 安装，所需的 Python 会一并安装：

```sh
brew install --formula ActivationEnergy/tap/askai
askai --version
```

软件源为 [ActivationEnergy/homebrew-tap](https://github.com/ActivationEnergy/homebrew-tap)，下载无需 GitHub 登录或 SSH 密钥。

askai 本身不包含 AI 模型。安装后，还需要一个可用的模型命令行程序或 API 密钥，见下一节。

<details>
<summary>备选：使用 uv 从源码安装</summary>

需要 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/)：

```sh
git clone https://github.com/ActivationEnergy/askai.git
cd askai
uv tool install .
```

如果安装后找不到命令，运行 `uv tool update-shell`，按提示设置路径并重新打开终端。

更新源码后，在源码目录运行 `uv tool install --force .` 重新安装。卸载使用 `uv tool uninstall askai-cli`。也可以不安装，直接在源码目录运行 `./bin/askai`。

</details>

目前主要在 macOS 上开发与验证。Linux 尚未完成完整平台验证；Windows 和 Nushell 暂不支持。

## 第一次提问

**1. 准备一个 AI 服务。** 两种方式任选其一：

- 已有 Codex、DSH 或 Claude Code：先按该程序自己的方式安装并登录，askai 会复用其登录状态。
- 使用 API：在当前终端设置对应服务的密钥，例如 `DEEPSEEK_API_KEY`，具体方法见[选择 AI](#选择-ai)。

**2. 检查是否就绪。**

```sh
askai doctor
```

它会检查相关程序是否存在、密钥环境变量是否已设置，不会验证账号是否能正常调用模型。

**3. 输入问题。**

```sh
askai "怎样查看磁盘占用？"
askai "查看当前目录中最大的十个文件"
```

普通提问不需要先修改 Shell 配置。想更换默认 AI，或者直接在终端输入自然语言来提问时，再运行 `askai-config`。

`askai` 跟随你保存的默认设置。例如，默认选择 DSH 后，以上问题就会交给 DSH，复用其已保存的凭据，不需要另外设置 DeepSeek API 密钥。只有明确使用 `askds` 时，才使用下面介绍的 DeepSeek API 连接。

## 看懂回答并选择操作

askai 会给出一个或几个方案，每个方案包含命令和简短解释。方案编号用于选择下一步：

| 输入示例 | 含义 |
| --- | --- |
| `1` | 执行第一个方案 |
| `e1` | 查看第一个方案的详细解释，然后继续选择 |
| `c1` | 复制第一个方案的命令，不执行 |
| `p1` | 只在终端输出第一个方案的命令，不复制、不执行 |
| `q` 或回车 | 取消 |

要操作第二个方案，就把编号换成 `2`，例如 `c2`。缺少关键信息时，AI 也可能先要求澄清，而不是给出命令。

命令中的示例文件名需要替换成实际文件名。问题中最好写明文件路径、处理范围和希望得到的结果。

## 选择 AI

`askai` 使用默认设置；下面这些快捷命令可直接选择对应服务，安装后即可使用，无需自己创建 alias：

| 命令 | 调用方式 | 需要准备什么 |
| --- | --- | --- |
| `askdsh` | DSH 命令行程序 | 已安装 DSH，且已配置可用凭据 |
| `askds` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `askgpt` | Codex 命令行程序 | 已安装并登录 Codex |
| `askcc` | Claude Code 命令行程序 | 已安装并登录 Claude Code |
| `askkimi` | Kimi API | `MOONSHOT_API_KEY` |
| `askmm` | MiniMax API | `MINIMAX_API_KEY` |
| `askglm` | GLM API | `GLM_API_KEY` |

```sh
askgpt "怎样查看哪个程序占用了 8080 端口？"
askdsh "怎样统计当前目录中的文件数量？"
```

`askgpt` 使用 Codex，不是 ChatGPT 网页端。`askds` 与 `askdsh` 是两种独立的连接方式：DSH 的登录状态不能代替 DeepSeek API 密钥。

<details>
<summary>备选：使用 DeepSeek API 并设置密钥</summary>

这一方式使用 `askds`，与 DSH 分开配置。已经使用 DSH 的用户无需为此额外申请 API 密钥。

以 DeepSeek 为例，在 **fish** 中：

```fish
set -gx DEEPSEEK_API_KEY '你的密钥'
askds "怎样查看磁盘占用？"
```

在 **zsh 或 bash** 中：

```sh
export DEEPSEEK_API_KEY='你的密钥'
askds "怎样查看磁盘占用？"
```

以上设置只对当前终端及其启动的程序生效，askai 不会保存密钥。其他 API 服务使用表中对应的变量名。

</details>

### 更换默认 AI 或模型

运行 `askai-config`，进入“配置 AI 模型与后端”。可以为各服务设置模型，并将其中一个设为默认。快捷命令不跟随默认设置改变。

这里有两层设置：**调用方式**决定使用 DSH、Codex 或某家 API；**具体模型**决定该调用方式实际使用的型号，例如 DeepSeek 的 Flash 或 Pro。

- **DSH、Codex 等 CLI**：直接选择“沿用当前模型”，便会跟随该程序自身的设置；不必输入模型 ID。
- **API 服务**：先从显示的常用型号中选择，例如 DeepSeek Flash 或 Pro。没有密钥也能先保存型号，但实际提问仍需要配置密钥。若需要更多型号，选择“更多型号”从服务商获取，支持 `/关键词` 筛选、`n`/`p` 翻页。

选择后仍需确认保存。常用型号是内置预设，只用于对应的官方 API 地址；自定义服务不套用这些预设。在线目录只在你主动选择“更多型号”时查询，不会发起模型生成或在每次提问时刷新。预设和目录都不保证账号有调用权限。

界面会显示常用型号的官方目录核对日期，具体型号与来源见[接口参考](docs/references.md#内置型号目录)。新版本更新预设时，不会自动覆盖你已经保存的型号。

目录获取失败时，原设置不变，可以返回选择常用或当前型号。只有自定义型号等高级用法，才需要进入“高级设置 → 手动输入模型 ID”。CLI 不维护独立的型号清单，更换原生模型可在该程序自身设置中选择。

也可以临时指定：

```sh
askai --provider dsh "怎样查看端口占用？"
askai --provider codex --model MODEL_ID "怎样查看磁盘占用？"
```

把 `MODEL_ID` 替换为当前调用方式支持的模型 ID。`--model` 优先于 askai 已保存的型号；没有保存型号时，CLI 才沿用自身配置，API 则使用内置默认值（自定义 API 需自行设置）。

“自动选择”按 **Codex → DSH → DeepSeek API → Kimi → MiniMax → GLM** 检查可用服务。Claude 仅在明确选择时使用。选定服务后如果调用失败，会显示错误，不会自动换成另一家。

## 设置终端

```sh
askai-config
```

设置菜单包括查看配置、选择 Shell、配置 AI，以及开启或关闭“直接提问”。**修改文件前会展示内容，只有确认后才保存，并保留备份。** 设置中选择 Shell 不会更换你的系统默认 Shell。

### 直接输入问题，不加 askai

开启“直接提问”，保存并重新打开终端后，可以输入：

```text
怎样查看磁盘占用
how to find large files
```

触发条件是：**开头的词不是已有命令**，并且问题包含至少两个以空格分隔的词，或第一个词中至少有两个汉字。已有命令、alias 和函数仍照常执行；带参数的拼错命令也可能触发 AI。

fish、zsh 和 Bash 4+ 支持直接提问。Bash 3.2 不支持此方式，但仍可使用 `askai "问题"`。

### 为什么执行 cd 后，当前目录没有变化？

默认情况下，选中的 Shell 命令在子进程中运行，`cd` 和环境变量修改不会保留到当前终端。通过 `askai-config` 配置 Shell 集成后，显式输入 `askai` 或快捷命令可保留这些变化。

zsh/bash 的“直接提问”仍在子进程中处理，需要保留目录或环境变化时，请使用显式的 `askai "问题"`。fish 的直接提问可保留这些变化。

Shell 配置修改后需重新打开终端；AI 默认服务和模型设置无需重开终端。`askai-config --status` 可查看已保存的设置；移除终端集成也在设置菜单中完成。

## Python 脚本

复杂任务可以给出独立 Python 脚本，先展示源码，再由你选择操作。除了执行、解释、打印和复制，还可以：

| 输入 | 含义 |
| --- | --- |
| `r1` | 复制第一个方案的 Python 源码 |
| `s1` | 保存到你指定的新文件，不覆盖已有文件 |

`p1` 和 `c1` 会生成包含完整代码的独立命令，不依赖临时文件。代码过长时，请使用 `r1` 或 `s1`。运行脚本需要 `PATH` 中有 `python3`；语法检查不代表脚本逻辑一定正确。

## 常用选项

| 用法 | 作用 |
| --- | --- |
| `askai --print "问题"` | 只输出第一个方案的命令，不执行 |
| `askai --json "问题"` | 输出 JSON，适合与其他程序配合，不执行 |
| `askai --shell fish "问题"` | 指定目标 Shell 的命令语法 |
| `askai --timeout 60 "问题"` | 将模型等待时间设为 60 秒 |
| `askai --debug "问题"` | 显示所用服务、模型参数和交互过程，帮助排错 |
| `askai --help` | 查看完整参数 |

调试信息会尽量遮蔽密钥，但问题、路径和回答仍可能包含隐私，分享前请检查。`--print` 和 `--json` 不执行建议；把普通输出重定向到文件时也不会进入执行菜单。

askai 保留终端自己的背景和文字配色，只为少量标签添加颜色。可在设置中调整，或用 `--color never` 关闭；自动模式尊重 `NO_COLOR` 环境变量。

## 更新与卸载

Homebrew 安装的版本：

```sh
brew update
brew upgrade --formula ActivationEnergy/tap/askai
```

新版 Homebrew 集成通过稳定入口启动，不在 Shell 函数中保存具体版本的程序或 Python 路径。加载新版集成后，同一终端中的命令可跟随正常升级和重装。从旧版集成迁移时，当前窗口中的旧函数仍需重载一次；Fish 可执行：

```fish
functions -e askai askdsh askgpt askds askcc askkimi askmm askglm
/opt/homebrew/opt/askai/bin/askai init fish --aliases | source
```

上例路径适用于 Apple Silicon 默认 Homebrew 安装；其他安装位置可用 `brew --prefix askai` 查看。也可以重新打开终端。卸载重装不会清除已打开窗口中的旧函数；以上操作只刷新当前窗口，不修改配置文件。

卸载前，如已配置终端集成，可先运行 `askai-config` 移除 askai 添加的终端设置，再执行：

```sh
brew uninstall --formula ActivationEnergy/tap/askai
```

卸载程序不会主动删除模型配置、密钥或备份文件。

## 常见问题

- **没有找到可用 AI？** 运行 `askai doctor`，检查模型程序是否在 `PATH` 中，或密钥环境变量是否已设置。
- **DSH 已登录，为什么 askds 仍不可用？** `askds` 使用 API 密钥；复用 DSH 凭据请使用 `askdsh`。网页服务启动也不代表已安装全局 `dsh` 命令，见 [DSH 排查说明](docs/dsh-troubleshooting.md)。
- **复制失败？** macOS 使用系统剪贴板工具；Linux 需要已安装 `wl-copy`、`xclip` 或 `xsel`。
- **问题里有 `$`、`|` 或 `*`？** Shell 会先解释这些字符，建议把问题放在引号中。需要保留字面量时使用单引号，例如 `askai '怎样查看 $HOME 的大小？'`。
- **配置文件在哪里？** 通常是 `~/.config/askai/config.toml`。自定义服务、地址等高级设置见[配置示例](examples/config.toml)。
- **会发送哪些信息给 AI？** 问题、系统与 Shell 信息、当前目录路径会发送给所选服务。原生模型 CLI 还可能按自身配置加载上下文。
- **支持英文吗？** 可以用中英文提问。目前界面和生成的解释默认使用简体中文；英文使用说明见 [English README](README.en.md)。

## 贡献与许可证

欢迎通过 [Issues](https://github.com/ActivationEnergy/askai/issues) 报告问题，请附系统、Shell、调用命令及脱敏后的错误信息。

开发者可在源码目录运行 `python3 -m unittest discover -s tests -v`。更多技术资料：[接口参考](docs/references.md) · [后端注册表](docs/backend-registry.md)。

本项目采用 [MIT License](LICENSE)。未知命令触发方式参考 [ccline](https://github.com/jianshuo/ccline)。
