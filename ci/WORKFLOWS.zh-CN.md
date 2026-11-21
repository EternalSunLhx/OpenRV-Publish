# GitHub 工作流中文说明

本仓库只保存汉化资源、自有代码、补丁与发行工具。官方源码、子模块和依赖在 GitHub Actions 临时目录下载，完成后随运行器清理。

## 自动与手动入口

名称中的 **(Auto)** 表示定时或其他工作流调用，**(Manual)** 表示可点击 Run workflow。

| 工作流 | 触发方式 | 用途 |
| --- | --- | --- |
| [官方稳定版监控与构建 (Auto) (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/upstream-release-watch.yml) | 每小时 UTC 第23分钟，也可手动 | 检查官方最新稳定 Release，去重后派发打包 |
| [官方版本中英文打包与发布 (Auto) (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/localized-release.yml) | 监控派发，或手动 | 固定官方 tag 和本仓库维护快照，三平台编译、验证、打包、自动发布 |
| [安装器打开方式验证 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/installer-registration.yml) | 手动 | 快速检查 Windows 安装注册、中文路径、打开方式与卸载，不编译完整软件 |
| [构建错误日志摘要 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/build-diagnostics.yml) | 手动 | 输入本仓库 job ID，读取完整日志并显示有大小上限的错误上下文 |
| [macOS 原生语言提示框诊断 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/macos-language-dialog-diagnostic.yml) | 手动 | 检查原生语言选择和重启提示框相关的小型测试 |

上游仓库的通用 CI、Conan 和发版工作流不包含在本仓库。旧 macOS 图形实验、Qt/Python 混合启动实验入口已退休；正式打包流程中的图形、语言和依赖验收仍然保留。

## 手动打包 v4.1.0

打开“官方版本中英文打包与发布” → Run workflow，选择 `main`，在必填的 `official_tag` 中填写 `v4.1.0` 等明确版本，不能留空。自动监控会自行查询并填写官方最新稳定 Release 的 tag。这里的 `main` 是发行工具分支；实际 OpenRV 源码由输入的官方 tag 决定。

三个平台使用 Release 编译配置：Windows x64、macOS Intel x86_64、macOS Apple Silicon arm64。它们共用一份固定来源计划，必须重建出相同的已验证源码树，再开始编译。

## tag 与来源

本仓库同名 `v4.1.0` tag 指向**发行工具和汉化资源的维护提交**。它的注释记录官方 Release ID、原 tag 对象、原源码提交和发布时间，从而检查上游是否移动过版本。官方 Git 对象不会被推送到本仓库。

第一次构建创建发布 tag；以后同一版本重打复用它，不移动 tag。每次实际使用的维护提交、官方源码、资源清单、补丁哈希、生成翻译差异和最终源码树，都记录在包的清单和来源文件中。

临时源码目录使用独立的 `refs/openrv-upstream/tags/` 保存官方对象，避免与发行工具自己的 tag 混淆。GitHub 自动生成的 Source code ZIP/TAR 只包含本仓库的维护文件。

## 下载与自动发布

平台产物先出现在该次打包任务底部的 Artifacts，当前保留14天。所有平台通过原生运行、语言持久化、音视频解码、安装器或应用包校验之后，汇总任务核对全部文件并上传。再次核对远端文件状态、大小、SHA-256、下载地址与来源身份后，才公开到 [Releases](https://github.com/EternalSunLhx/OpenRV-Publish/releases)。

新 Release 上传中断或校验失败会保持草稿。后续成功的同版本构建追加独立文件，不覆盖旧文件；发布阶段串行执行，旧版本不会抢占较新版本的 Latest。

| 平台 | 文件类型 |
| --- | --- |
| Windows x64 | 全用户中英文 Setup EXE、便携 ZIP |
| macOS Intel | DMG、APP ZIP |
| macOS Apple Silicon | DMG、APP ZIP |

每个平台另有 `.build.json` 与 `.sha256.txt`，共同来源有 provenance JSON 和生成翻译差异 ZIP。包名含官方版本和内部产品编号。为延续旧安装包的更新顺序，产品编号为 `28 + GITHUB_RUN_NUMBER`；重跑同一次 Actions 任务保留编号。下载说明不展示这些内部构建细节。

“帮助 → 检查更新”读取本仓库稳定 Release，筛选当前平台完整的一组文件，打开下载链接安装。

## 更新上游与补译

监控只跟随官方稳定 Release，不自动合并官方开发 main，也不创建候选分支、状态分支或自定义版本后缀。已处理的官方版本不会被定时器重复派发；失败或取消后，检查问题并手动重跑。

新的官方文案生成待补译清单。修改 [`localization/i18n_zh.ts`](../localization/i18n_zh.ts) 后刷新资源清单并提交；补丁冲突则修正 [`patches/`](../patches/)。未完成翻译或来源校验失败时，流程停止，不发布不完整版本。没有付费 AI 翻译接口。

新流程使用工作流自己的 `GITHUB_TOKEN`：监控仅授予 Actions 派发权限，发布标签与安装包的任务授予 Contents 写权限。它不再导入官方 Git 历史，不使用 `OPENRV_TAG_MIRROR_TOKEN`。
