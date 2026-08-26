# 维护说明

当前版本：`V0.10.2`。

每次更新 `SKILL.md`、脚本、规则、引用资料或 UI 元数据时，都必须递增版本、更新 `agents/openai.yaml` 的 `display_name`，并在 `CHANGELOG.md` 记录变更。展示名固定为 `GM 抖音爆款长评采集 V版本号`。

提交前运行相关离线自测与格式检查；涉及采集策略时，确认频率控制、每日配额、去重和 SQLite 落库契约没有退化。

所有文本文件以 UTF-8（无 BOM）保存；Windows 上的 Python 校验必须启用 UTF-8 模式，避免系统默认 GBK 造成误判。

`SKILL.md` 的 YAML frontmatter 仅使用 Codex 支持的字段 `name`、`description`、`license`、`allowed-tools` 和 `metadata`；版本号统一维护在 `agents/openai.yaml`、`manifest.json` 与 `CHANGELOG.md`，避免因非标准字段导致技能无法被列表发现。

实时采集的用户可见进度必须每 60 秒汇报一次，并在关键词切换、重试、空结果、入库、清理、配额达标和异常退出时即时汇报。汇报须包含实际增量、累计漏斗、当前阶段、停滞判断和下一检查点；禁止只报告“后台运行中”或“等待完成”。
