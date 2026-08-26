# Contributing Guide

感谢你愿意为 **抖音爆款评论池** 贡献代码或想法。请先阅读本指南，让协作顺畅高效。

## 目录

- [开发环境](#-开发环境)
- [本地自测（必须通过）](#-本地自测必须通过)
- [如何贡献](#-如何贡献)
- [提交规范](#-提交规范)
- [CHANGELOG 更新](#-changelog-更新)
- [代码约定](#-代码约定)

## 🔧 开发环境

- **Python 3.9+**，仅标准库（`filter_pool / run_daily / sqlite/*` 不依赖第三方包）。
- 实时采集需要 `douyin-crawl-report` 或 `mediacrawler` 提供抓取引擎（可选依赖）。
- Clone 后直接可用，无需 `pip install`。

## 🧪 本地自测（必须通过）

改动任何逻辑前/后，先跑两套自测，确保通过：

```bat
python tests\selfcheck.py          REM 三级门槛逻辑
python tests\sqlite_selfcheck.py   REM SQLite 全链路端到端（9 项）
```

要求：
- **新增/修改筛选阈值、评分逻辑** → 补充 `tests/selfcheck.py` 断言。
- **改动 SQLite schema / 导入 / 视图** → 补充 `tests/sqlite_selfcheck.py` 断言。
- **改动批次状态、JSONL 生命周期或进度汇报** → 同步更新 README / SKILL.md / MAINTENANCE.md，并验证旧库可由 `db.ensure_schema()` 自动迁移。
- 提交前两套自测必须 100% PASS。

## 🤝 如何贡献

1. Fork 本仓库，在 `feature/xxx` 或 `fix/xxx` 分支开发。
2. 只做**一件**事：一个 PR 解决一个问题/一个特性。
3. 通过自测后提交，并跑通以下幂等校验：
   - 重复导入同批次数据不产生重复行。
   - `run_daily` 当日已满配额时直接结束、不重复抓取。
4. 提交 PR 时清晰描述：**改了什么、为什么、怎么验证**。

## 📝 提交规范

- 提交信息使用**中文**，遵循 `<type>: <描述>` 格式。
- type 取值：`feat`（新功能）/`fix`（修复）/`docs`（文档）/`refactor`（重构）/`test`（测试）/`style`（格式）/`chore`（杂项）/`perf`（性能）。
- 示例：
  - `fix: vw_hot_comments 视图作者昵称关联错误的 accounts 表`
  - `feat: 新增关键词命中占比分析命令`
  - `docs: 更新 README 快速开始`

## 📌 CHANGELOG 更新

- **新功能 / 修复**：在 `CHANGELOG.md` 的对应版本或 `[Unreleased]` 下追加条目，并归入 `Added / Fixed / Changed / Removed` 分类。
- **版本提升**：按 [Semantic Versioning](https://semver.org/lang/zh-CN/)：
  - 破坏性变更或大特性 → minor（0.x.0）
  - 兼容修复 → patch（0.0.x）
- 显式行为变更（阈值默认值、表结构、脚本参数）必须在 CHANGELOG 注明。

## 🎯 代码约定

- 仅用标准库；若确需第三方依赖，须在 PR 中说明理由并写入 `manifest.json` 的 `required_dependencies`。
- 保持幂等：所有导入/写入可安全重复执行。
- 大字段（图片、下载链接）只存 URL。
- 遵循项目既有代码风格即可；除非重构目标明确，否则不引入无关改动。
- 添加 `flush=True` / 原子写入（`.tmp` + `os.replace`）等跨切面修复时，请同步检查同目录其他脚本是否也存在相同隐患。

## 许可

提交即表示你同意以 [MIT License](./LICENSE) 授权你的贡献。
