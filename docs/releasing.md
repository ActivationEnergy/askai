# 发布校验

公开仓库只推送正式版本提交，不推送本地开发分支的历史。每个候选版本应是上一个正式版本之后的单个提交。

1. 将最终源码整理到独立发布仓库，确认 `pyproject.toml`、`__init__.py`、`uv.lock` 与源码 Formula 的版本一致。运行该候选的完整测试。
2. 准备阶段可运行只读校验（替换示例参数）：

   ```sh
   python3 scripts/check-release.py RELEASE_REPO --version VERSION --tag vVERSION --parent PREVIOUS_RELEASE_COMMIT --allow-untagged
   ```

3. 从校验输出的明确提交执行 `git archive`，在导出的目录构建 wheel/sdist，验证安装入口。不要直接从可能包含忽略文件的工作目录构建发布包。Formula 安装测试也使用同一份候选源码。
4. 核实远端 main 和标签状态后再创建标签；同名标签已指向其他提交时停止，不改写标签。独立 tap Formula 的 `revision` 必须为标签对应的提交。源码内 Formula 只记录版本和标签，避免自引用提交号。
5. 标签与 tap Formula 就绪后，再次校验：

   ```sh
   python3 scripts/check-release.py RELEASE_REPO --version VERSION --tag vVERSION --parent PREVIOUS_RELEASE_COMMIT --formula TAP_FORMULA --online
   python3 scripts/check-public-content.py --history
   ```

   隐私扫描应在独立发布仓库运行，它检查模式并需要人工复核。离线校验成功不代表远端可发布；网络失败必须按状态未知处理。
6. 只推送明确的 `main` 和本次标签，使用原子推送；随后更新 tap。不要使用 `--all`、`--tags` 或强制推送。完成后重新读取远端引用，核对安装版本。

`check-release.py` 不修改文件、标签或远端。它检查 Git 清洁状态、候选直接父提交、版本、标签指向及可选 tap revision；测试、构建、安装和隐私检查仍须单独完成。
