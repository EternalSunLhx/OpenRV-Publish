# OpenRV-Publish

基于 [ASWF OpenRV](https://github.com/AcademySoftwareFoundation/OpenRV) 的中英文社区发行工具。本仓库只维护汉化资源、自有代码、补丁、测试和打包发布流程。

构建时，GitHub Actions 下载指定的**官方稳定版 tag**，应用本仓库固定提交中的补丁和汉化资源，然后在 Windows x64、macOS Intel、macOS Apple Silicon 上原生编译。官方完整源码、子模块和依赖只放在临时构建目录。

- [下载安装包](https://github.com/EternalSunLhx/OpenRV-Publish/releases)
- [一键打包](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/localized-release.yml)：选择 `main`，必须填写明确的官方稳定版 tag；自动监控会填写官方最新稳定版。同一官方版本可以重新打包。
- [工作流中文说明](ci/WORKFLOWS.zh-CN.md)
- [安装包功能测试](https://github.com/EternalSunLhx/OpenRV-Publish/actions/workflows/post-package-tests.yml)：手动选择已发布版本、平台和用例，下载现有包进行验证；与打包流程独立。

## 维护文件

| 路径 | 用途 |
| --- | --- |
| [`localization/i18n_zh.ts`](localization/i18n_zh.ts) | 简体中文翻译词库 |
| [`overlay/src/`](overlay/src/) | 自有语言切换、更新检查、菜单路径及平台辅助代码 |
| [`patches/`](patches/) | 针对官方源码的汉化、解码和兼容性修复补丁 |
| [`tools/localization/`](tools/localization/) | 提取界面文案、检查翻译、生成待补译清单 |
| [`ci/localized-release/`](ci/localized-release/) | 编译、安装器、运行验证和自动发布脚本 |
| [`.github/workflows/`](.github/workflows/) | 自动监控和手动打包入口 |

官方新增文案会生成人工补译清单。缺失翻译或补丁冲突会阻止发布，修正并提交后可重新打包；没有付费 AI 翻译服务。

## 发行功能

软件支持跟随系统、English 和简体中文，切换后重启生效。Windows 提供所有用户安装的中英文 Setup 和便携 ZIP，并加入视频文件的“打开方式”候选。macOS 分别提供 Intel 与 Apple Silicon 的 DMG / APP ZIP，最低系统版本以实际软件包为准。

发行配置启用 AAC、AC-3/E-AC-3、ProRes、DNxHD、DV、VP9、HEVC、MPEG-2 等软件解码支持。全部平台必须通过实际运行、语言切换、音视频解码及打包校验，之后自动发布。软件的“帮助 → 检查更新”会查询本仓库的稳定 Release，并打开对应平台的下载链接。

Windows 安装包未使用发布者证书签名；macOS 应用名为 `OpenRV.app`，使用 ad-hoc 签名，未做 Apple 公证。确认安装包来源和完整性后，如遇“Apple 无法验证”提示，可先尝试打开，再到“系统设置 → 隐私与安全性 → 仍要打开”确认，详见 [Apple 官方说明](https://support.apple.com/zh-cn/102445)。

发布者为 EternalSunLhx。macOS 的 DMG 根目录和应用的 `Contents/Resources/PUBLISHER.txt` 包含中英文发布者说明及社区发行仓库、上游项目链接。

## 版本与来源

Release 使用与官方一致的版本名称，例如 `v4.1.0`。**本仓库的发布 tag 标记的是发行工具快照**；官方原始 tag 对象、源码提交、补丁、资源和最终构建树记录在随包发布的来源清单中。GitHub 自动生成的 Source code ZIP/TAR 是本仓库的发行工具，不是 OpenRV 完整源码。

同一版本可以重新构建，安装包使用递增的内部编号；全部平台通过验证后，新构建可以替换旧附件。公开文件名只包含版本、平台和用途，不带构建编号；`.build.json` 只保留在 Actions 产物中。精确来源和 SHA-256 校验文件随包提供。旧版软件升级到此格式时，请从 Release 页面手动下载一次；新版更新检查兼容两种格式。

这是独立的社区发行仓库。OpenRV 上游项目、源码和许可证归属见 [ASWF OpenRV](https://github.com/AcademySoftwareFoundation/OpenRV)、[LICENSE](LICENSE) 和 [Notice.txt](Notice.txt)。旧仓库的安装包未迁移，发行包从官方 v4.1.0 重新构建。
