# Changelog

本技能采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 规范，版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 规划中
- 关键词命中占比分析，自动推荐当日最易出爆款的词
- 沉淀池 → `master-copywriting` 一键改写为 IP 账号草稿
- 沉淀池趋势看板（SQLite → HTML 报表）

## [0.7.0] - 2026-08-23

技能首发并对外公开仓库。此前为本地迭代开发成型（三级门槛、达标即停、MediaCrawler 实时采集等能力已稳定），自本版本起纳入版本管理与公开追踪。

### Added
- **三级门槛筛选引擎**：高互动（点赞≥1000 或回复≥50）→ 有效字数≥30 → 可成文性评分≥55（钩子/情绪/具体意象/数字/行动主体/疑问），含口水词排除（"哈哈哈/收藏了/扣1"等）。
- **达标即停**：每日最多入池 `--quota`（默认 5）条，达标立即结束；当日计数记录在 `daily/{YYYY-MM-DD}.count`，跨天自动重置。
- **实时 MediaCrawler 采集**：API 签名直抓、`--headless` 默认，关键词广撒网 + 评论 top-N 聚合；支持 `--speed safe` + 随机延时 + 失败重试，控制风控。
- **SQLite 数据中心**：6 表（accounts / videos / comments / ancestry / hits / batches）+ 3 视图（vw_hot_comments / vw_threads / vw_daily_stats）。
  - 幂等导入（UPSERT）：重复导入同批次不产生重复行，天然支持断点续跑与累计。
  - 只入 URL：评论图片、视频下载链等大字段仅存 URL，控制库体积。
  - 只导爆款池相关：`--all` 只批量导入 `hcp*` 搜索批次。
- **可移植封装**：manifest.json + 自带空库 + LICENSE；解压到隔离目录即可独立建库运行。

### Fixed
- `vw_hot_comments` 视图：`a.nickname` 原误联 comments 表（无该列）导致查询报 `no such column`，改为经 `accounts` 以 `creator_hash` 关联取作者昵称。

### Tested
- `tests/selfcheck.py`：三级门槛 + 达标即停 + 幂等。
- `tests/sqlite_selfcheck.py`：9 项端到端（建库视图 / 导入 / 讨论串 / 幂等 / hits 回流 / 讨论串视图）。
- 隔离解压验证：新环境跑通两套自测，确认可移植。

## 版本对照说明

- 0.7.0 之前的本地迭代不授予独立版本号，未在公网追踪；能力累积自 0.7.0 起对外统一定版。