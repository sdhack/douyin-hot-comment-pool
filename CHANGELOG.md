# Changelog

本技能采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 规范，版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 规划中
- 关键词命中占比分析，自动推荐当日最易出爆款的词
- 沉淀池 → `master-copywriting` 一键改写为 IP 账号草稿
- 沉淀池趋势看板（SQLite → HTML 报表）

## [0.10.8] - 2026-08-26

### Changed
- `run_daily.py` 固化生产采集策略：无限翻页、最多点赞排序、首个低于 1 万赞视频止停、单视频 30 条评论、三级筛选、safe 频率和 120 秒熔断均由入口强制设置。
- 生产入口只接受必要输入及 `--quota`；运行调优参数会明确拒绝。

### Docs
- README、SKILL.md、维护说明和技能元数据同步为固定生产流程。

### Verified
- 通过 `py_compile`、固定策略函数验证，以及越权参数拒绝测试。

## [0.10.7] - 2026-08-26

### Added
- 实时采集期间按 4 秒增量入库和三级筛选，命中达到当日配额立即终止采集。
- Markdown 实时进度汇报，展示视频、评论、候选和命中漏斗，以及最新视频和最新评论明细。
- 连续无视频、评论或候选新增时的自动停滞熔断。

### Fixed
- 修复采集进程中断时子进程、批次状态和临时控制文件未完整清理的问题。
- 修复 MediaCrawler 当前版本的 `os` 兼容补丁和已采视频跳过补丁兼容性。
- 修复实时命中结果在最终收尾阶段可能遗漏的问题。
- 修复 `requests` / `chardet` 导入告警。

### Verified
- `tests/selfcheck.py` 通过。
- `tests/sqlite_selfcheck.py`：9 PASS / 0 FAIL。
- `tests/ingest_selfcheck.py`：10 项通过。

## [0.10.6] - 2026-08-26

### Docs
- 全量校正文档版本、验证说明、目录结构和安全支持范围，消除旧版本行为描述。
- 补充贡献规范：涉及批次状态、JSONL 生命周期或进度汇报时，必须同步更新相关文档并验证旧库迁移。

## [0.10.5] - 2026-08-26

### Docs
- 新增 README、SKILL.md 和 SECURITY.md 的免责声明与责任边界，明确平台规则、隐私、版权、凭据安全和停止使用条件。

## [0.10.4] - 2026-08-26

### Docs
- 补充 SQLite 批次状态字段、实时查询示例和失败批次恢复流程。
- 明确 `status/phase/last_progress_at` 是每分钟汇报的事实来源，避免只依赖后台进程状态。

## [0.10.3] - 2026-08-26

### Changed
- SQLite 连接启用 `WAL`、`synchronous=NORMAL`、30 秒 `busy_timeout`，降低进度查询与采集写入互相阻塞的概率。
- `batches` 增加 `status`、`phase`、`last_progress_at`、`retry_count`、`skipped_count`，并支持旧库在线迁移。
- `run_daily` 与 `run_daily_v2` 写入批次生命周期和错误状态，支持每分钟汇报查询实际阶段与最后进度。
- JSONL 继续作为临时采集缓冲；成功导入后 SQLite 是唯一长期数据源，失败时保留现场用于重放。

## [0.10.2] - 2026-08-26

### Changed
- 新增实时采集的强制每分钟汇报协议：以前台可轮询进程运行，每 60 秒报告本分钟增量、累计漏斗、当前阶段、停滞判断和下一检查点。
- 新增事件触发汇报和停滞处置：关键词切换、重试、空结果、入库、清理、配额达标、异常退出立即报告；连续两分钟无指标增长时检查进程、日志、采集目录和 SQLite，并在疑似限流时暂停后续关键词。
- 明确禁止无指标的“后台运行中”“一切正常”等低价值状态汇报，所有数值必须来自实际日志、文件或数据库。

## [0.10.1] - 2026-08-26

### Fixed
- 修复 `SKILL.md` frontmatter 缺少结束分隔线、正文粘入 `description` 的发布包问题。
- 移除当前 Codex 校验器不支持的 frontmatter `version` 字段；版本仍由 `agents/openai.yaml`、`manifest.json` 和本变更记录统一维护。
- 增加维护文档中的 frontmatter 兼容性约束，避免技能文件存在但无法出现在技能列表。

## [0.10.0] - 2026-08-26

### Changed
- **默认排序改为「最多点赞」**（`--search-sort` 默认 `1`）：最多点赞降序配「按赞止停」请求量最少、最稳，作为默认采集形态；`--search-sort 0`（综合）作组合词空桶的破空备用，`2` 最新发布。
- **默认有头绑定指纹**：CDP 会话默认有头（`--headless` 才无头），扫码重登后绑定可见浏览器指纹，显著降低空响应反爬。
- SKILL.md / README 全面同步「反爬与风控机制」与默认排序说明；manifest 修正 `repo` 仓库名拼写。

### Added
- **变体词破空策略**：验证「节气养生」在网页版通用搜索为空桶（App 端真有数据），但其**变体词**「二十四节气」「二十四节气养生」「节气养生知识」可稳定产出（60~300 评论）。组合词先做综合排序探测，勿武断判空。
- **组合词空桶诊断矩阵**与**软限流预警**（空结果率骤升≥50% 即冷却）：记录于 SKILL.md「反爬与风控机制」章节。

### Verified
- 多轮实测（2026-08-26）：有头+去固定15 单词视频 14→53（近 4 倍扩容）、「二十四节气」综合排序 300 评论、变体词共 55 视频/219 评论入库；软限流信号（空结果率 2/6→5/6）与「空结果=浏览器实例回收（闪退）非崩溃」均已落日志验证。
- **8 步管线冒烟（2026-08-26，真实实跑「养生」词）**：按赞排序（`MC_SEARCH_SORT_TYPE=1`）→ 每页 15 条翻页采 92 视频 → 去重 92 唯一 → `stop_at_like_floor below_floor_n=6` 低于 1 万赞自然截断 → `early_stop_low_heat` 多处触发 → 三级筛选入池 2 条（74/65 分）→ `当日入池 2/2 达标即停`；92 视频/973 评论实时入库、零 JSON 残留、exit 0。

## [0.9.13] - 2026-08-25

### Added
- **按赞止停（`--stop-at-like-floor`，用户定义的新采集形态）**：配合最多点赞排序——按赞降序抓取，遇到首个点赞低于视频门槛（1 万）的视频即结束本关键词翻页，**替代固定 per-keyword 条数**：大词自动多抓、小词自动早收。实现为幂等 patch 注入 core.py 分页循环（env `MC_STOP_AT_LIKE_FLOOR`，break 仅跳出分页不影响后续关键词）；低于门槛的视频仍入库视频信息供「二次复活」。已实测触发（300k 阈值下 below_floor_n=4 正确截断），env 未设置时零行为变化。

### Fixed
- `ensure_mc_sleep_patch` 的 jitter-v2 标记实际位于 config/__init__.py 而非 base_config.py，修正检测位置后 no-var 告警彻底消除。

## [0.9.12] - 2026-08-25

### Added
- **搜索排序可选（`--sort-by-likes`）**：实测搜索接口的「最多点赞」排序真实有效——返回严格赞降序（对比综合排序混有低赞尾部）。开启后配合万赞视频门槛几乎零浪费；默认关闭保持综合排序的内容多样性。实现为幂等 patch 注入 core.py（env `MC_SEARCH_SORT_TYPE`），`collect_search --search-sort {0,1,2}` 同样可用。

### Verified
- 全链路实战验证（养生/ultra/--sort-by-likes/--show-browser）：搜索排序生效、9 个已采视频去重跳过、2 个低热视频翻页早停（page_max_digg=133/21<门槛）、14 视频/100 评论实时入库后目录清理，零 JSON 残留。

## [0.9.11] - 2026-08-25

### Added
- **视频级点赞门槛（用户需求硬编码 1 万赞）**：数据佐证——915 个已采视频中低于 1 万赞的 183 个消耗约 20% 评论请求、命中产出为零（100 条命中全部来自万赞以上视频）。现搜索结果中 `digg_count`<10000 的视频跳过评论抓取、仅保留视频信息入库。
- **低赞二次复活**：被门槛跳过的视频不写入 skip 名单（无评论），二次遇到时若点赞已涨过门槛则正常抓取评论；无统计数据的视频保守放行。`--video-min-likes` 可调（默认 10000）。patch 幂等带 `.gate.bak` 回滚，真实 core.py 四场景验证 PASS。

## [0.9.10] - 2026-08-25

### Added
- **评论翻页热度水位早停**：实测抖音评论接口为固定智能排序（`sort_type`/`sort_order_by` 等 5 种排序参数变体全部被服务端忽略，返回顺序完全一致）——高赞集中在前部、页码越深越冷。现 `collect_search` 新增 `--min-likes/--min-replies`（默认与三级门槛同源 1000/50）：某页最高赞低于门槛且最高回复也低于门槛时，立即停止该视频后续翻页（`MC_PAGE_HEAT_FLOOR_*` env + 幂等 patch 注入 client.py）。传 `0` 关闭。

### Changed
- `run_daily` 采集时自动把三级门槛阈值透传给评论翻页水位，冷尾视频从「翻满 max_count」变为「按热度止损」。
## [0.9.9] - 2026-08-25

### Fixed
- **无头会话指纹问题（搜索连续空结果根因）**：扫码重登后的新会话绑定有头浏览器指纹，无头启动被抖音判未登录（status 2483、pong 假阴性）。新增 `collect_search --no-headless` 与 `run_daily --show-browser` 弹窗采集支持；SKILL.md 补充「重登后首次采集须 --show-browser」约束。
- **延时补丁 no-var 误报**：并行引入的 jitter-v2 把 `CRAWLER_MAX_SLEEP_SEC` 动态化（标记在 config/__init__.py），旧补丁按静态行匹配报 `no-var`。现识别 jitter-v2 标记直接视为 already（MC_SLEEP_MIN/MAX 已被每请求消费），兼容两种形态。

### Added
- **搜索空结果诊断**：幂等 patch 给 core.py 空结果日志附 `status_code|status_msg`（2483=未登录/会话失效，其他=风控或无匹配）；run_daily 空结果提示同步给出 `--show-browser` / ultra 冷却建议。

## [0.9.8] - 2026-08-25

### Changed
- **加大采集间隔（三重防护）**：
  1. 档位整体上调：`safe` 每请求延时 3-8s → **6-15s**，`fast` 1-3s → 2-6s；
  2. 新增 **`ultra` 超稳档**：并发1、每请求 12-28s 抖动、词间休息 180s——风控期/长跑首选；
  3. 延时从「每次调用一个固定随机值」升级为**每请求区间抖动**：透传 `MC_SLEEP_MIN/MC_SLEEP_MAX` 给 MediaCrawler MC_OPT 层，评论/详情每个请求独立在区间内 uniform 取值。
- **新增 `--kw-gap`（run_daily，默认 90s）**：关键词轮次之间自动休息，配额达标即停前不空等；`ultra` 档默认 180s，显式传参可覆盖。

## [0.9.7] - 2026-08-25

### Added
- **已采视频跳过（降重复抓取与风控面）**：实测发现语义相近关键词会命中同一批热门视频（单场 100 条会话中约 13.7% 视频抓取、12.8% 评论抓取为重复，浪费约 25 分钟）。现 `run_daily` 在**每个关键词开抓前**从库内导出「已有评论的视频 ID」为临时 skip 文件（`.hcp-skip-<account>.txt`，用后即删），`collect_search` 经新增 `--skip-file` 参数与幂等 patch 注入的 MediaCrawler `MC_SKIP_FILE` 钩子，让命中视频**跳过评论重抓**（视频信息仍正常入库更新）。单场多词轮次的重复抓取可基本消除。
  - patch 幂等（`.bak` 自动回滚锚点），env `MC_SKIP_FILE` 未设置时零行为变化，不影响本机其他 MediaCrawler 项目；
  - 空库自动不启用；导出失败降级为「本词不跳过」，不阻断采集。

### Fixed
- `_export_skip_file` 在 dict 行工厂连接下 `r[0]` 取值失败被静默吞掉（返回 None 使跳过永不启用）；改为按列名 `aweme_id` 取值并在失败时显式提示。


## [0.9.1] - 2026-08-24

### Fixed
- **跨天重复入选（历史计数虚增）**：`_already` 原只查当日 `hits`，同一评论跨天会被再次计为"新命中"（零点后首轮即把昨日命中整批重记）。现对齐 SKILL.md 去重规则「同 comment_id 或同文本不重复入选」：实时/离线两条筛选路径均以**全量历史命中（id + 文本）**做去重，当日配额只被真正的新评论消耗。`tests/ingest_selfcheck.py` 新增"跨天去重"场景（8 项断言全过）。
- **批次记账互相覆盖**：`import_batch` 的 `batch_id` 原回落到 `account`，同账号连导多个关键词时 `batches` 表后词覆盖前词统计。现新增可选 `batch_id` 参数，`run_daily` 按 `<account>.<关键词>.<时间戳>` 传入，逐词独立记账。
- **库文件移出版本库（防数据外泄）**：`sqlite/douyin_hotpool.db` 不再被 git 跟踪（`.gitignore` 新增 `sqlite/*.db`），真实采集数据绝不随包外发；接收方克隆后执行 `python sqlite/db.py --init` 一键重建空库，manifest 同步更新库说明。

## [0.9.0] - 2026-08-23

实时入库管线重构：逐词即时入库 + 磁盘零 JSON 残留 + 末尾当日报告。

### Changed
- **逐关键词实时入库**：`run_daily` 主路径重构为"单关键词采集 → 立即 loader 导入 SQLite → 写入 hits（配额封顶）→ 清理"的逐词循环；不再"全部词跑完后统一聚合入库"。
- **筛选全程内存化，聚合 JSON 不再落盘**：`aggregate_comments.py` 抽出纯内存函数 `aggregate_paths()`（CLI 落盘仅保留调试用途）；`run_daily` 的三级门槛筛选改在内存中完成，全程不产生 `comments_aggregated.json`。
- **磁盘零 JSON 残留**：每词入库成功（或确认无数据）后，整段删除运行目录（含 MediaCrawler 的 `search_*.jsonl`、cursor、聚合残留）；运行结束删除 `.douyin-crawl-current-<account>.json` 指针。数据唯一落点是 SQLite；**入库失败则保留现场便于排障**。
- **达标即停颗粒度到词**：每个关键词开抓前实时复查 `hits` 表当日数，配额已满立即停止后续采集（原有仅"启动前查一次"）。
- **末尾输出当日采集报告**：逐词统计（采集视频/入库评论/新增命中/状态）、当日入池明细（评分/赞回/内容摘录）、库内累计、耗时与停止原因，一次运行结束即得全景。

### Fixed
- **聚合 top-N 失真**：旧版 `aggregate_comments` 先按原始顺序切片 top-N、之后才排序，导致"每视频按赞 top-N"名不副实；现改为先按 `like_count` 降序再截取。

### Added
- **`tests/ingest_selfcheck.py`**：实时入库管线离线自测（入库/三级门槛/配额封顶/目录清理/空结果清理/top-N 排序回归），共 7 项断言，不依赖网络与 MediaCrawler。

## [0.8.0] - 2026-08-23

采集链路解耦重构 + 提速 + 可靠性修复。

### Changed
- **实时采集零外部技能依赖**：`collect_search.py` 改为直接解析并调用本机 MediaCrawler（env `MEDIACRAWLER_PY/MC_ROOT` > 全局注册指针 `runtime-registry.json` > 默认缓存），完全内嵌调度，移除对 douyin-crawl-report 的任何引用；`SKILL.md`/`manifest.json` 依赖声明同步更新。
- **新增 `--preset` 采集档位**（默认 `safe`）：
  - `safe`：并发1、延时 3-8s（最稳，风控压力最小）
  - `fast`：并发3、延时 1-3s（提速但风控暴露面更大）
  - 显式 `--speed/--sleep-*/--per-keyword/--comments-count` 始终优先于 preset。
  - 预设解析收拢到共享模块 `tools/_presets.py`，避免 run_daily 与 collect_search 默认值漂移。
- **提速默认**：`per-keyword` 30→10、`comments-count` 100→30（达标 5 条无需超采），配合 fast 档全链路实测约 13min → ~1min。
- **采集数据默认入库、不再写 json**：`run_daily` 主路径将原始采集幂等导入 `accounts/videos/comments/ancestry/batches`，精选命中写入 `hits` 表；不再输出 `pool/<account>.json`。达标即停的当日计数改从 `hits` 表（`hit_date=今天`）读取，跨天自动重置。
- **版本同步**：`SKILL.md` 元数据版本号 0.7.0 → 0.8.0；README / SKILL / manifest 产物描述统一为「默认入库 SQLite」。

### Fixed
- **部分成功即丢数据**：采集进程返回非 0（超时/重试失败）时，`run_daily` 原直接 `continue` 丢弃已抓数据；现改为仍尝试聚合本次抓取的存量评论并入池，避免"抓到一半被判今日无新增"。
- **评论产物递归扫描**：MediaCrawler 将 jsonl 落在 `crawl_<account>/douyin/jsonl/`（含平台子目录），评论产物检测改为递归 glob，修复"已抓评论却误判未发现"。
- **Playwright 环境缺陷**：README/SECURITY 补充 `playwright install chromium --only-shell` 装机说明（缺失 `chromium_headless_shell` 会导致启动崩溃）。

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
