# 维护说明

当前版本：`V0.9.4`。

每次更新 `SKILL.md`、脚本、规则、引用资料或 UI 元数据时，都必须递增版本、更新 `agents/openai.yaml` 的 `display_name`，并在 `CHANGELOG.md` 记录变更。展示名固定为 `GM 抖音爆款长评采集 V版本号`。

提交前运行相关离线自测与格式检查；涉及采集策略时，确认频率控制、每日配额、去重和 SQLite 落库契约没有退化。

所有文本文件以 UTF-8（无 BOM）保存；Windows 上的 Python 校验必须启用 UTF-8 模式，避免系统默认 GBK 造成误判。
