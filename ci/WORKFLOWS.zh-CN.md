# GitHub 工作流中文说明

本仓库只保存汉化资源、自有代码、补丁与发行工具。官方源码、子模块和依赖在 GitHub Actions 临时目录下载，完成后随运行器清理。

## 自动与手动入口

名称中的 **(Auto)** 表示定时或其他工作流调用，**(Manual)** 表示可点击 Run workflow。

| 工作流 | 触发方式 | 用途 |
| --- | --- | --- |
| [官方稳定版监控与构建 (Auto) (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/upstream-release-watch.yml) | 每小时 UTC 第23分钟，也可手动 | 检查官方最新稳定 Release，去重后派发打包 |
| [官方版本中英文打包与发布 (Auto) (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/localized-release.yml) | 监控派发，或手动 | 固定官方 tag 和本仓库维护快照，三平台编译、验证、打包、自动发布 |
| [复用现有安装包发布 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/resume-publication.yml) | 手动 | 三平台已成功、仅发布失败时，复用原产物并重新校验后发布，不编译软件 |
| [已发布安装包功能测试 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/post-package-tests.yml) | 仅手动 | 下载指定 Release 的现有 ZIP，校验后运行旋转播放、偏好窗口生命周期用例；独立于打包和发布 |
| [安装器打开方式验证 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/installer-registration.yml) | 手动 | 快速检查 Windows 安装注册、中文路径、打开方式与卸载，不编译完整软件 |
| [构建错误日志摘要 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/build-diagnostics.yml) | 手动 | 输入本仓库 job ID，读取完整日志并显示有大小上限的错误上下文 |
| [macOS 原生语言提示框诊断 (Manual)](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/macos-language-dialog-diagnostic.yml) | 手动 | 检查原生语言选择和重启提示框相关的小型测试 |

上游仓库的通用 CI、Conan 和发版工作流不包含在本仓库。旧 macOS 图形实验、Qt/Python 混合启动实验入口已退休；正式打包流程中的图形、语言和依赖验收仍然保留。

## 手动打包官方稳定版

打开“官方版本中英文打包与发布” → Run workflow，选择 `main`，在必填的 `official_tag` 中填写要发布的官方稳定版标签，例如 `v4.1.0`，不能留空。自动监控会自行查询并填写官方最新稳定 Release 的 tag。这里的 `main` 是发行工具分支；实际 OpenRV 源码由输入的官方 tag 决定。

`replace_legacy_v410` 默认关闭，仅首次将原 `v4.1.0 build35` 附件换成新格式时勾选。它只允许替换清单中固定 Release ID、文件 ID、名称、大小和 SHA-256 全部匹配的旧附件；自动监控不会开启它。新三平台安装包全部通过验证、上传并核对之后，才移除旧附件。迁移中断时应复用原构建恢复发布，不再另起一轮编译。

三个平台使用 Release 编译配置：Windows x64、macOS Intel x86_64、macOS Apple Silicon arm64。它们共用一份固定来源计划，必须重建出相同的已验证源码树，再开始编译。

## tag 与来源

本仓库同名 `v4.1.0` tag 指向**发行工具和汉化资源的维护提交**。它的注释记录官方 Release ID、原 tag 对象、原源码提交和发布时间，从而检查上游是否移动过版本。官方 Git 对象不会被推送到本仓库。

第一次构建创建发布 tag；以后同一版本重打复用它，不移动 tag。每次实际使用的维护提交、官方源码、资源清单、补丁哈希、生成翻译差异和最终源码树，都记录在包的清单和来源文件中。

临时源码目录使用独立的 `refs/openrv-upstream/tags/` 保存官方对象，避免与发行工具自己的 tag 混淆。GitHub 自动生成的 Source code ZIP/TAR 只包含本仓库的维护文件。

## 下载与自动发布

新版打包器采用简洁命名。历史旧格式默认保持原样；仅 `v4.1.0 build35` 可通过上述显式开关迁移，且必须重新编译含新版更新检查器的软件，不能只给旧安装包改名。

平台产物先出现在该次打包任务底部的 Artifacts，当前保留14天。所有平台通过原生运行、语言持久化、音视频解码、安装器或应用包校验之后，汇总任务核对全部文件并上传。再次核对远端文件状态、大小、SHA-256、下载地址与来源身份后，才公开到 [Releases](https://github.com/EternalSunLhx/OpenRV-Publish/releases)。内部构建清单 `.build.json` 保留在 Actions 产物中，不作为 Release 附件发布。

新 Release 上传中断或校验失败会保持草稿。Release 对外文件名只包含官方版本、平台和用途，例如 `OpenRV-4.1.0-Windows-x64-Setup.exe`。内部构建编号仍用于来源校验和新旧比较；同一官方版本的新构建通过完整验收后更新同名附件，旧构建不能覆盖新构建。发布阶段串行执行，旧版本不会抢占较新版本的 Latest。

如果三平台任务均已成功，仅最后的发布失败，可运行“复用现有安装包发布”，填写原构建地址 `actions/runs/` 后的数字 ID。该入口使用当前维护分支的发布器修复，同时保留原源码快照、内部产品编号和安装包；不会修改原构建的结果记录。恢复时重新检查原轮次的五个前置/平台任务、四个 artifact 的 ID 和 SHA-256、源码组成、所有原生验收及安装包校验值。发布器按 Release ID 识别草稿，将已验证的内部产物转换成十二个公开附件，核对上传结果后才公开。

此入口目前接受原始执行（run attempt 为 1），要求原 artifacts 尚未过期；恢复流程自身可以重跑。恢复上述旧格式迁移时仍需勾选 `replace_legacy_v410`。发布恢复回执另存为 artifact，区分原编译提交与本次发布器提交。原草稿及已上传且内容一致的文件可继续使用。

| 平台 | 文件类型 |
| --- | --- |
| Windows x64 | 全用户中英文 Setup EXE、便携 ZIP |
| macOS Intel | DMG、包含 OpenRV.app 的 ZIP |
| macOS Apple Silicon | DMG、包含 OpenRV.app 的 ZIP |

每个平台公开 `.sha256.txt`，共同来源公开 provenance JSON、生成翻译差异 ZIP 和来源校验文件。三个 `.build.json` 仅保留在 Actions 原始产物中；Release 文件名不包含内部产品编号。为延续旧安装包的更新顺序，产品编号为 `28 + GITHUB_RUN_NUMBER`；重跑同一次 Actions 任务保留编号。下载说明使用隐藏的结构化标记记录更新检查所需的编号与附件摘要，页面正文不展示这些内部数据。

“帮助 → 检查更新”的新实现读取本仓库稳定 Release，校验隐藏的版本标记和完整附件摘要，打开对应平台下载链接安装，并兼容旧文件名。已编译的旧实现依赖 `-build.N` 文件名和 `.build.json` 附件；升级到新格式重打包时，请从 Release 页面手动下载一次。

## 独立的打包后测试

运行“已发布安装包功能测试”，填写已发布的 `release_tag`，选择单个平台或 `all`、单个用例或 `all`、英文/中文或 `both`。任务直接下载本仓库 Release 的 Windows 便携 ZIP 或 macOS 应用 ZIP，核对 SHA-256 并解压，只在 GitHub 托管运行器上测试。

- `rotation-playback`：打开固定的合成 MP4，通过实际菜单旋转 180°，观察持续播放和帧变化，再正常关闭。
- `preferences-owner-close`：在会话 A 打开偏好设置，创建会话 B，关闭 A 并确认它拥有的偏好窗口销毁，再正常关闭 B。

结果和必要日志出现在该任务的 `post-package-test-<平台>` artifact，保留 14 天。此工作流不自动触发，不编译软件，不替换 Release，不改变打包工作流结果。旧版包可能暴露已知崩溃并产生失败报告；测试失败应依据日志修复，不能当作安装包已经通过验证。

## 更新上游与补译

监控只跟随官方稳定 Release，不自动合并官方开发 main，也不创建候选分支、状态分支或自定义版本后缀。已处理的官方版本不会被定时器重复派发；失败或取消后，检查问题并手动重跑。

新的官方文案生成待补译清单。修改 [`localization/i18n_zh.ts`](../localization/i18n_zh.ts) 后刷新资源清单并提交；补丁冲突则修正 [`patches/`](../patches/)。未完成翻译或来源校验失败时，流程停止，不发布不完整版本。没有付费 AI 翻译接口。

偏好设置窗口的生命周期修复单独保存在 `patches/openrv-preferences-lifetime.patch`。每次组成源码时，检查该完整修复是否已经存在：完全匹配则跳过，否则必须完整应用成功。仅修了一部分、上游采用其他实现或发生冲突时停止并提示人工核查，不按版本号、关键词或 Issue 是否关闭来猜测。来源计划记录本次应用或跳过的结果，三个平台重新核对；确认上游解决方案后可以删除这一独立补丁及对应检测。

新流程使用工作流自己的 `GITHUB_TOKEN`：监控仅授予 Actions 派发权限，发布标签与安装包的任务授予 Contents 写权限。它不再导入官方 Git 历史，不使用 `OPENRV_TAG_MIRROR_TOKEN`。
