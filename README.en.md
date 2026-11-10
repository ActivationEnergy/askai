# askai

[简体中文](README.md) | English

**Can't remember a terminal command? Describe what you want to do.**

askai asks your chosen AI for commands and explanations, then lets you run, copy, or cancel. It works with fish, zsh, and bash, reusing existing model CLIs or connecting to APIs.

```sh
askai "How do I add a line number to every line in notes.txt?"
```

For example, the AI might suggest:

```sh
nl -ba notes.txt
```

The explanation describes the key options: `-ba` numbers every line, including blank lines, and the result is printed to the terminal. With a single suggestion, press `c` to copy the command, `1` to run it, or press Enter to cancel.

Answers may vary. **Read commands before running them: AI suggestions can be wrong.** The interface and generated explanations currently use Simplified Chinese, including when you ask in English. This document explains the controls in English.

## Install

### Homebrew — recommended

On macOS, install with Homebrew. The required Python dependency is installed automatically:

```sh
brew install --formula ActivationEnergy/tap/askai
askai --version
```

The tap is [ActivationEnergy/homebrew-tap](https://github.com/ActivationEnergy/homebrew-tap). Downloads do not require a GitHub login or SSH key.

askai does not include an AI model. You also need a working model CLI or an API key, as described below.

<details>
<summary>Alternative: install from source with uv</summary>

Requires Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/ActivationEnergy/askai.git
cd askai
uv tool install .
```

If the command is not found after installation, run `uv tool update-shell`, follow its instructions, and open a new terminal.

After updating the source, run `uv tool install --force .` from the source directory to reinstall. To uninstall, use `uv tool uninstall askai-cli`. For a source checkout, use `uv run ./bin/askai` so uv installs the interactive dependencies.

</details>

Development and validation currently focus on macOS. Linux has not completed full platform validation. Windows and Nushell are not currently supported.

## Ask your first question

**1. Prepare an AI service.** Choose either approach:

- Already using Codex, DSH, or Claude Code? Install and sign in through that program first. askai reuses its login state.
- Using an API? Run `askai-config` to paste the service's key, or use an existing environment variable. See [Choose an AI](#choose-an-ai).

**2. Check your setup.**

```sh
askai doctor
```

This checks whether programs are available and API keys are configured. It does not verify that your account can successfully call a model.

**3. Ask a question.**

```sh
askai "How do I check disk usage?"
askai "Show the ten largest files in the current directory"
```

You do not need to change shell settings for normal questions. Run `askai-config` later to change the default AI or type natural-language questions directly at the shell prompt.

`askai` follows your saved default. For example, when DSH is your default, these questions use DSH and its saved credentials, without a separate DeepSeek API key. The API connection below applies to `askds`, `--provider deepseek`, or DeepSeek API selected as the default backend.

## Read the answer and choose an action

askai offers one or more options, each with a command and a short explanation. Use its number to choose an action:

| Input | Action |
| --- | --- |
| `1` | Run the first option |
| `e1` | Show its detailed explanation, then return to the menu |
| `c1` | Copy its command without running it |
| `p1` | Print its command to the terminal without copying or running it |
| `q` or Enter | Cancel |

With a single suggestion, press `e` to explain, `p` to print, or `c` to copy. For Python scripts, `v` copies the source and `s` saves it. No option number is needed. In a plain terminal, submit the letter with Enter; numbered forms such as `c1` remain accepted.

Commands use bold terminal green; shortcut keys and the active prompt use terminal magenta. Command lines have no added borders, line numbers, or indentation for mouse selection. Shortcuts share one row when space allows and wrap into compact groups in narrower terminals.

In an interactive terminal, number keys execute immediately without Enter for up to 9 suggestions. With 10 suggestions, enter the selection and press Enter. When there are multiple suggestions, press an action letter followed by an option number. Esc goes back; q or Enter cancels. Text input still uses Enter to submit. Basic terminals (`TERM=dumb`) retain line-based input.

Accepted choices remain in terminal scrollback, for example `已选择：c · 复制方案 1`. This records the requested action, not successful execution; results are reported separately. AskAI does not create a separate action-history file.

Replace `1` with `2` for the second option, for example `c2`. If important information is missing, the AI may ask for clarification instead of suggesting a command.

Replace example filenames with actual filenames. Include paths, the intended scope, and the desired result in your question when possible.

Use these first-level actions to improve an answer:

| Key | Action |
| --- | --- |
| `r` | Select a reasoning level and retry; supported for DSH and Codex |
| `m` | Select another backend and model, then retry |
| `a` | Add requirements and press Enter to retry; blank input returns |
| `b` | View the previous successful answer and its settings; shown when available |
| `f` | View the next successful answer and its settings; shown when available |

DSH defaults to low reasoning, with off, low, high, and max available. Codex inherits its native settings by default. Press `r` to request low, medium, high, xhigh, max, or ultra, or return to native settings. Available levels depend on the Codex version and model; a failed request preserves the current answer. The displayed level is a requested setting, not verified server behavior. API backends offer presets and an explicit model-list fetch; Codex/DSH reuse native settings or an explicitly configured model. Claude uses isolated settings and does not load user/project settings files.

`b`/`f` browse saved answers without requesting the model again and restore their question and settings. A successful retry from an older answer replaces its forward history; cancellation and failures preserve it.

Changes apply only to this question. Retries include the original question and previously accepted supplements, without previous model answers. Cancellation, failure, or a clarification-only response preserves the current answer. Selecting unchanged settings makes no additional request.

## Choose an AI

`askai` follows your default setting. These shortcuts select a particular service and are available after installation; no shell aliases are required:

| Command | Connection | What you need |
| --- | --- | --- |
| `askdsh` | DSH CLI | DSH installed with working credentials |
| `askds` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `askgpt` | Codex CLI | Codex installed and signed in |
| `askcc` | Claude Code CLI | Claude Code installed and signed in |
| `askkimi` | Kimi API | `MOONSHOT_API_KEY` |
| `askmm` | MiniMax API | `MINIMAX_API_KEY` |
| `askglm` | GLM API | `GLM_API_KEY` |

```sh
askgpt "How do I find which process is using port 8080?"
askdsh "How do I count files in the current directory?"
```

`askgpt` uses Codex, not the ChatGPT website. `askds` and `askdsh` are independent connections: a DSH login does not replace a DeepSeek API key.

<details>
<summary>Alternative: use the DeepSeek API with a key</summary>

This connection uses `askds` and is configured separately from DSH. If you only use DSH, you do not need a separate DeepSeek API key.

Run `askai-config`, open AI models and backends, and select an API service such as DeepSeek or Kimi. The service page separates model selection from API key management. Selecting a model without an available key leads to key setup; cancellation clearly leaves the service incomplete. No model is called automatically. Key management supports:

- **Import and save from environment**: read the inherited variable and save its key after confirmation; no need to set it again.
- **Use, paste or replace a local key**: input is masked and saved only after confirmation. Press Enter to reactivate an existing saved key.
- **Delete the saved local key**: confirm deletion for this API connection only.

Local keys are stored in **plaintext**, not encrypted, in `credentials.json` beside the selected configuration file (normally `~/.config/askai/credentials.json`), with owner-only permissions (0600). An explicitly relative `ASKAI_CONFIG` is based on the starting working directory; confirmation shows the absolute storage path. Keys are not written to `config.toml`, shell configuration, command history or debug output. Replacing a key does not retain a backup of the old value.

Save once to use the key in new terminals and after restarting. Saved keys take precedence over environment variables. Importing or pasting can replace the saved key. Deleting a saved key falls back to the environment if that variable is still set; askai cannot change its parent shell's environment. Saved keys are bound to the API URL and environment variable name, so changing the endpoint does not automatically reuse the previous key.

The menu shows the active source without revealing the key. Saving a key or model does not verify account access or automatically call a model. CLI backends continue to use their own credential management.

</details>

### Change the default AI or model

Run `askai-config` and choose “配置 AI 模型与后端” (configure AI models and backends). Set a model for each service or make one the default. Shortcuts continue to select their designated service regardless of the default.

These are two distinct choices: the **connection** selects DSH, Codex, or an API service; the **model** selects a specific offering, such as DeepSeek Flash or Pro.

- **CLI connections such as DSH and Codex**: select the option to use the CLI's current model. No model ID is required.
- **API services**: choose a displayed common model, such as DeepSeek Flash or Pro. You can save that choice before setting a key, although actual questions still require authentication. Choose “更多型号” (more models) to fetch the service's catalog, then filter with `/keyword` or change pages with `n`/`p`.

Confirm before saving. Common models are built-in presets for the corresponding official API endpoint and are not applied to custom gateways. Online catalogs are fetched only when you explicitly choose more models, without inference or automatic refresh. Neither presets nor catalog listings guarantee account access.

The menu shows when the built-in catalog was checked against official documentation. See the [model catalog and sources](docs/references.md#内置型号目录) (Chinese). Updates to presets do not automatically overwrite a model you have already saved.

If discovery fails, existing settings stay unchanged; return to a common or currently configured model. Custom IDs are available under “高级设置 → 手动输入模型 ID” (advanced settings → enter a model ID). askai does not maintain a separate CLI model catalog. Codex/DSH can use their native model settings; Claude uses isolated settings and requires an explicit AskAI model override to select a configured model.

To override settings for one question:

```sh
askai --provider dsh "How do I check which ports are in use?"
askai --provider codex --model MODEL_ID "How do I check disk usage?"
```

Replace `MODEL_ID` with an ID supported by the selected connection. `--model` overrides the model saved in askai. Without a saved model, Codex/DSH use native settings. Claude does not load user/project/local settings files; its actual model may still depend on its environment. API connections use the built-in default; custom APIs need an explicit model.

Automatic selection checks **Codex → DSH → DeepSeek API → Kimi → MiniMax → GLM**. Claude is used only when explicitly selected. If the selected service fails, askai reports an error rather than silently switching to another service.

## Shell settings

```sh
askai-config
```

The menu lets you view configuration, select a shell, configure AI connections, and enable or disable direct questions. **Changes are shown before saving and require confirmation; backups are kept.** Selecting a shell here does not change your system's default shell.

### Ask without typing askai

Enable “直接提问” (direct questions), save, and open a new terminal. You can then type:

```text
how to find large files
怎样查看磁盘占用
```

This works only when **the first word is not a recognized command** and the input contains at least two space-separated words, or the first word contains at least two Chinese characters. Existing commands, aliases, and functions still run normally. A misspelled command with arguments can also trigger AI.

fish, zsh, and Bash 4+ support direct questions. Bash 3.2 does not; use `askai "question"` instead.

### Why doesn't cd change my current directory?

By default, selected shell commands run in a child process, so directory and environment changes do not persist in your current terminal. Configure shell integration through `askai-config` to preserve these changes when explicitly using `askai` or a shortcut.

Direct questions in zsh/bash still run through a child process. Use an explicit `askai "question"` when you need directory or environment changes to persist. Direct questions in fish can preserve these changes.

Open a new terminal after changing shell integration. Default AI and model settings do not require restarting the terminal. Use `askai-config --status` to view saved settings; shell integration can also be removed through the settings menu.

## Python scripts

For complex tasks, an answer may contain a standalone Python script. The source is shown before you choose an action. In addition to running, explaining, printing, and copying, you can use:

| Input | Action |
| --- | --- |
| `v1` | Copy the first option's Python source |
| `s1` | Save it to a new file you choose, without overwriting an existing file |

`p1` and `c1` produce a self-contained command containing the full code, without relying on temporary files. Use `v1` or `s1` if the code is too long. Running scripts requires `python3` on your `PATH`. Syntax checks do not prove that a script's logic is correct.

## Useful options

| Usage | Purpose |
| --- | --- |
| `askai --print "question"` | Print the first command without running it |
| `askai --json "question"` | Output JSON for other programs, without running commands |
| `askai --shell fish "question"` | Generate commands for the specified shell |
| `askai --timeout 60 "question"` | Set the model wait time to 60 seconds |
| `askai --debug "question"` | Show service details, model parameters, and interactions for troubleshooting |
| `askai --help` | List all options |

`--debug` also reports local preparation, CLI milestones, and answer parsing times. CLI milestones are cumulative observations, not additive durations. DSH catalog and reasoning markers do not measure individual server-side phases. Timings go to stderr without changing command or JSON output.

Debug output attempts to hide keys. Questions, paths, and answers may still contain private information; review logs before sharing. Neither `--print` nor `--json` executes suggestions. Redirecting normal output to a file also skips the execution menu.

askai keeps your terminal's background and normal text colors, adding color only to selected labels. Adjust colors in settings or disable them with `--color never`. Automatic mode respects the `NO_COLOR` environment variable.

## Update and uninstall

For a Homebrew installation:

```sh
brew update
brew upgrade --formula ActivationEnergy/tap/askai
```

The updated Homebrew integration launches through a stable entry point, without storing version-specific application or Python paths in shell functions. Once loaded, it follows normal upgrades and reinstalls in the same terminal. When migrating from older integration, reload the functions already loaded in your current terminal once. In Fish:

```fish
functions -e askai askdsh askgpt askds askcc askkimi askmm askglm
/opt/homebrew/opt/askai/bin/askai init fish --aliases | source
```

This path is for a default Apple Silicon Homebrew installation; use `brew --prefix askai` for other locations. Alternatively, open a new terminal. Uninstalling and reinstalling does not clear functions already loaded in an open terminal. Reloading only refreshes the current session and does not edit configuration files.

If you enabled shell integration, first remove askai's terminal settings through `askai-config`, then uninstall:

```sh
brew uninstall --formula ActivationEnergy/tap/askai
```

Uninstalling does not automatically delete model configuration, credentials, or backup files. Shell `.askai-backup-*` files are retained without automatic rotation. Once recovery is no longer needed, delete only the specific backup paths shown during setup.

## Common questions

- **No AI service found?** Run `askai doctor`. Check that the model CLI is on `PATH` or that the required API-key environment variable is set.
- **DSH is signed in, but askds does not work?** `askds` uses an API key; use `askdsh` to reuse DSH credentials. A running DSH web service also does not imply a global `dsh` command is installed. See [DSH troubleshooting](docs/dsh-troubleshooting.md) (Chinese).
- **Copy failed?** macOS uses the system clipboard tool. Linux requires an installed `wl-copy`, `xclip`, or `xsel`.
- **Does your question contain `$`, `|`, or `*`?** Your shell processes these characters first. Quote your question, using single quotes to preserve literal text: `askai 'How do I check the size of $HOME?'`.
- **Where is the configuration file?** Normally `~/.config/askai/config.toml`. For custom services and endpoints, see the [configuration example](examples/config.toml) (Chinese comments).
- **What is sent to AI?** Your question, OS and shell information, and current directory path are sent to the selected service. Native model CLIs may also load context according to their own configuration.
- **Is the interface available in English?** Questions can be in English or Chinese. The interface and generated explanations currently use Simplified Chinese. This English README does not change the application's language.

## Contributing and license

Report problems through [Issues](https://github.com/ActivationEnergy/askai/issues). Include your OS, shell, command, and error messages with sensitive information removed.

Developers can run `uv run python -m unittest discover -s tests -v` from the source directory. Further technical documentation (Chinese): [interface references](docs/references.md) · [backend registry](docs/backend-registry.md).

Licensed under the [MIT License](LICENSE). The command-not-found integration approach was inspired by [ccline](https://github.com/jianshuo/ccline).

Python execution, copied commands, and the suggested command for saved scripts use `python3 -I -X utf8`. This ignores PYTHONPATH, user site packages, and implicit imports from the working directory. Explicitly add an import path when required. `__file__` is a temporary path for immediate execution, undefined for inline commands, and the saved path for saved scripts; locate input data via arguments or the working directory.

Version 0.1.4 changes the shell execution exchange protocol. Reopen the terminal or reload the integration once after upgrading. Integrations from 0.1.2 onward report a protocol mismatch and reload guidance. Earlier functions may fail with a missing installation path; erase and reload those functions too. Without integration, `$SHELL` is a login-shell hint; use `--shell` when running a different nested shell.

[Release verification workflow](docs/releasing.md) (Chinese).
