# 官方稳定 Release 监控

`upstream-release-watch.yml` 每小时 UTC 第23分钟检查一次，也可在本仓库 `main` 手动运行。实现为 `dispatch.py`。

只选择官方最新非草稿、非预发布的 `vX.Y.Z` Release，固定其 ID、原 tag 对象、提交、源码树和发布时间。已有本仓库发布 tag 的注释必须记录相同的官方身份，否则停止并报告上游版本被改动。

本仓库的 tag 指向发行工具提交。官方完整源码和 Git 历史只出现在临时构建目录，不会镜像到本仓库。

派发目标固定为 `localized-release.yml` 的 `main`，输入只有必填的 `official_tag`。监控会查询官方最新稳定版并传入其明确 tag；手动运行打包工作流时也必须填写版本，不能留空。去重记录必须来自当前轻量 main 的合法维护祖先；旧的完整源码迁移运行不能阻止新结构构建。合法的失败或取消记录不会被定时器不停重试，检查问题后手动运行即可。

没有状态分支、候选分支或自定义版本后缀，也不自动合并官方开发 main。翻译新增项由源文案检查生成人工清单，不使用付费翻译服务。

```sh
python -B -m unittest discover -s ci/upstream-release-watch/tests -p test_dispatch.py -v
```

测试使用本地 fixture。`watch.py` 提供受控的源码组合辅助函数，命令入口转交当前派发器。
