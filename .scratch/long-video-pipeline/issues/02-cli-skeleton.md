# 02: CLI 骨架、任务目录与配置加载

**What to build:** `lvs` 可执行入口，含 `doctor / parse / shots / assets / voice / build / run` 子命令（本期只有 `doctor` 有实现，其余先占位报"未实现"）。每个子命令支持 `--task <name>`，某一次运行的全部中间产物落在 `.work/<task>/`。配置从仓库根的配置文件读取（LLM key、Pexels key、TTS 后端），缺失字段给中文错误。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `lvs --help` 列出全部子命令，每个子命令有 `--help`
- [ ] `--task` 生效，`.work/<task>/` 自动创建
- [ ] 配置文件缺失或字段缺失时，报错信息指出具体是哪个字段、去哪填
- [ ] `.work/` 已加入 `.gitignore`，`git status` 不显示运行产物
- [ ] 任务目录结构符合 spec §6 的约定
