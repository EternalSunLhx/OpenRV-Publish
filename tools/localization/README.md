# 人工维护中文翻译 / Maintaining Chinese translations

翻译维护不调用付费 AI 或在线翻译服务。已完成的译文按 Qt 的 context、英文原文和消歧备注复用。上游新增或改写文案会进入人工补译清单；检查没有通过时，不能生成中文版发布草稿。

使用 Qt Linguist 打开本仓库的 `localization/i18n_zh.ts` 可以编辑译文。构建时会将它放入临时官方源码的 `src/lib/app/RvCommon/qrc/translations/i18n_zh.ts`。菜单和用户数据的内部名称保持原样；技术标识可以明确审核后保留英文。不能仅删除 `unfinished` 标记来跳过未完成的翻译。

## 更新候选版本

从可信维护代码调用工具，用 `--repo` 指向已经下载官方指定 tag 并应用本仓库补丁的临时工作区。这些工具只解析候选源码，不运行其中的 Python、Mu、CMake 或插件代码。不要直接把轻量发布仓库当成完整源码工作区。

```sh
python tools/localization/update_menu_paths.py --repo /path/to/candidate
python tools/localization/sync_sources.py --repo /path/to/candidate \
  --lupdate /path/to/Qt/bin/lupdate \
  --output-dir /path/to/candidate/ci/upstream-release-watch/translations --apply
python tools/localization/check_translations.py --repo /path/to/candidate \
  --report /path/to/candidate/ci/upstream-release-watch/catalog-check.json
```

`sync_sources.py` 的结果：

- `missing-translations.md`：中文人工补译清单，包含英文原文、Qt context、消歧备注和源码位置。
- `missing-translations.json`：相同清单的机器可读版本，供自动流水线判断。
- `candidate-i18n_zh.ts`：保留全部已有译文，并增加 `unfinished` 条目的候选 TS。
- 使用 `--apply` 时，新条目还会加入候选工作区的正式 TS；默认只输出报告，不修改正式 TS。

退出码 `0` 表示没有待补译或待接入项；`1` 表示清单已生成、需要人工处理；`2` 表示工具或源码解析失败。即使退出 `1`，报告和 `--apply` 的候选 TS 也已经保存。报告使用仓库相对位置，没有主机绝对路径或运行时间；重复检查同一候选分支时输出稳定。

补译时保留 `%1` 等格式参数、菜单缩进和换行。在候选 TS 中填入中文后，移除该条目的 `type="unfinished"`；再次运行这三条命令。检查通过后，将候选 TS 保存回本仓库的 `localization/i18n_zh.ts`，并刷新资源清单。补译完成之前，流水线上传待补译清单并停止发布，不创建候选分支。

```sh
git add localization/i18n_zh.ts
python -B tools/refresh_patch_manifest.py
git add ci/upstream-release-watch/localization-base.json
python -B tools/refresh_patch_manifest.py --check
```

## 检查覆盖范围

Qt `lupdate` 是原生 Qt 文案的抽取器，必需安装：原生构建使用已有的 Qt SDK；Ubuntu 可安装 `qt6-l10n-tools`，通常位于 `/usr/lib/qt6/bin/lupdate`。工具按真实 Qt6 构建扫描 `RvCommon` 的头文件、C++、Qt6 表单，以及 session manager 和 annotation 的表单。旧 Qt5 preferences 表单不参与 Qt6 发布。

另外扫描已加入显示翻译桥接的固定 Mu 菜单、显式 `QCoreApplication.translate` 和 Python `_rv_tr` / `translate_ui` 调用。文件名、用户命名的 source/view、设备名、插件内部 ID 和其他动态值不会自动加入翻译。

新出现的固定 `setText`、`setToolTip` 等界面调用，以及当前翻译范围之外的新表单，会显示在“新增界面文案接入检查”中。维护者需要先在对应代码中加入 `tr`、`QCoreApplication.translate` 或 `translate_ui`，再生成 TS 并补译。无法仅在 TS 中添加一个没有运行时翻译入口的词来通过检查。

如果新文案确实是无需翻译的技术标识，可在审核后从可信维护分支运行 `--accept-ui-baseline`，并审查 `unwrapped-ui-baseline.json` 的差异。这个选项不能在自动监控中使用。新增复杂动态菜单或全新插件界面仍需人工检查翻译桥接。

## 发布前验证

正式发布在 Qt SDK 安装后运行不带 `--apply` 的 `sync_sources.py`，以及 `check_translations.py`。前者发现新增源码文案，后者检查完成状态、重复键、格式参数、换行和菜单路径。语言切换和跨重启记忆由实际应用运行测试验证。

工具的集成测试使用真实 `lupdate`，无需编译 OpenRV：

```sh
OPENRV_TEST_LUPDATE=/path/to/Qt/bin/lupdate \
  python -m unittest discover -s tests/localization -p test_source_sync.py -v
```

## English

No paid AI or online translation service is used. Existing finished translations are retained. Source synchronization produces a Markdown/JSON checklist and a candidate TS; missing translations or new literal UI calls without a translation bridge block release creation. A maintainer translates the official TS, preserves placeholders and layout, removes each completed `unfinished` marker, then reruns synchronization and validation.

Run trusted maintenance tools against a candidate with `--repo`. `sync_sources.py` requires a trusted Qt `lupdate`; it parses the checkout without executing its scripts or plugins. The check covers the Qt6 native UI, reviewed package forms, opted-in literal Mu menus, and explicit script translation calls. New unwrapped literal UI calls and forms outside that reviewed scope require a human to add the runtime translation bridge. Dynamic user data and internal identifiers are not automatically translated.
