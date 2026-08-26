---
name: "douyin-hot-comment-pool"
description: "搜索抖音高赞高互动的爆款长评论，经三级门槛（高互动→字数→可成文性）筛选沉淀成爆款文案素材池，逐词实时入库 SQLite、已采视频自动跳过重复抓取、磁盘零 JSON 残留、每日达标即停并输出当日报告。Invoke when user wants to 猎取抖音爆款评论、挖可做成文案的高互动长评、或每天定时刷一批能成文的评论素材。"
---

# 抖音爆款评论池（Douyin Hot Comment Pool）

在"爆款评论出现在哪并不固定、只能靠刷"的场景下，用**广撒网采样 + 三级门槛筛选 + 每日配额达标即停**把随机性摊平，持续沉淀「能改写成爆款文案」的高互动长评论。

## 核心思路

人工刷的效率瓶颈是"一台设备逐条翻"。本技能用机器批量喂入海量视频样本 → 帕累托筛出高互动长评论，等效"一次刷几百个视频的评论区"。三条硬原则：

1. **样本广而浅**——宁可关键词多、每条视频评论少，提高覆盖率（"位置不固定"靠覆盖摊平）。
2. **三级门槛缺一不可**——高互动(点赞/回复) → 字数(去符号后达标) → 可成文性(自带钩子/情绪/具体细节，脱离原视频仍成立)。短到口水的"哈哈哈"就算点赞上万也拆不出文案骨架，必须淘汰。
3. **达标即停**——每天最多入池 `--quota` 条（默认 5），达到立即结束，绝不让流程无限跑。当日配额的已入选数取自 `hits` 表（`hit_date=今天`），跨天自动重置。

## 何时使用

- 用户想"找/挖/搜 抖音高赞高互动评论""爆款评论做文案"
- 用户想：每天从无限刷的评论区里稳定捞出 5 条能成文的爆款评论
- 配合 master-copywriting 把沉淀评论改写成四账号文案

## 工作流

主路径是**实时 MediaCrawler 采集**（API 签名直抓，非浏览器 DOM 模拟），逐关键词按固定 8 步管线推进：**① 默认按赞排序**（`--search-sort 1` 赞降序）→ **② 每页 15 条**（搜索接口固有分页）→ **③ 已采去重**（开抓前从库导出已采视频 ID，命中者跳过评论重抓）→ **④ 抓完翻页** → **⑤ 低于 1 万赞截断**（首条点赞不足即结束本词，替代固定条数）→ **⑥ 评论热度水位早停**（页内最高赞<1000 即止损翻页）→ **⑦ 三级门槛筛选**（高互动→字数→可成文性）→ **⑧ 配额达标即停**（每词开抓前复查当日入池数，满 `--quota` 立即停）。每词抓完实时导入 SQLite → 清理采集目录（磁盘零 JSON 残留）→ 末尾输出当日报告。离线只用于调试，不作日常入口。

### 主路径：实时 MediaCrawler 采集（日常用）

单命令完成"刷一批爆款评论、每天命中 5 条即停"：

```bat
set POOL=%USERPROFILE%\.trae-cn\skills\douyin-hot-comment-pool
REM 实时采集 + 筛选 + 沉淀 + 达标即停（一键；默认 --preset safe=慢档：并发1/每请求6-15s抖动，稳）
python %POOL%\tools\run_daily.py --root <工作根> --account <slug> ^
  --keywords "养生;智商税;避坑;宝妈" --quota 5 --preset safe
REM 提速用快档：--preset fast（并发3/延时2-6s，风控面更大）；风控期用 ultra 超稳档（12-28s+词间180s）。显式 --speed/--sleep-*/--per-keyword 会覆盖 preset
```

采集直接依赖 **MediaCrawler**（本机已安装的抓取引擎，`collect_search.py` 内嵌其调度，零外部技能依赖；API 签名直抓，**默认有头**绑指纹、防空响应）。**首次需扫码登录一次**，之后复用登录态缓存。频率经 `--preset` 控制：默认 `safe`（并发1、每请求 6-15s 抖动）稳；`ultra` 超稳档（并发1、12-28s＋词间休息 180s）供风控期/长跑用；`fast`（并发3、2-6s）提速但风控面更大。词间另有 `--kw-gap` 休息（safe/fast 默认 90s）。显式 `--speed/--sleep-*/--per-keyword/--kw-gap/--search-sort` 会覆盖 preset。`--retry-fail` 失败重试。**默认 `--search-sort 1` 最多点赞排序**（配按赞止停请求量最少）；组合词空桶时切 `--search-sort 0` 综合。

### 运行中每分钟汇报（强制）

实时采集必须以前台、可轮询的进程运行，禁止把 `run_daily.py` / `run_daily_v2.py` 丢到后台后只在结束时汇报。启动命令后每 60 秒向用户汇报一次；关键词切换、重试、空结果、入库完成、清理完成、配额达标、异常退出等事件立即汇报，不等待整分钟。

每次汇报必须包含**本分钟增量 + 累计值 + 当前阶段 + 下一检查点**，至少使用以下格式：

```text
[进度 HH:MM] 运行 Xm | 阶段: 关键词「养生」/评论翻页
本分钟: 新视频 N / 新评论 N / 新命中 N / 请求或页数 N
累计: 关键词 i/总数 | 视频 N | 评论 N | 命中 N/配额 | 已采跳过 N
状态: 正在请求、词间等待、重试第 k 次、入库、清理、已暂停或失败
判断: 有效进展/暂无新数据/疑似空结果或限流；依据=最近日志、进程状态或 SQLite 计数
下一检查点: 约 Ns 后或事件 X；若连续 2 分钟无计数变化，说明处理动作
```

“暂无新数据”必须说明等待原因（请求延时、词间休息、浏览器登录、接口空桶、重试或入库），不得用“正在运行”“一切正常”代替指标。每分钟数值只能来自实际 stdout/stderr、采集目录文件计数或 SQLite 查询；无法读取时明确标记“未读到新指标”，不能估算或编造。连续 2 次无新增视频/评论/命中时，必须主动检查子进程是否存活、最近日志时间、采集目录是否增长和 SQLite 是否可读；发现停滞、异常或空结果率达到 50% 立即暂停后续关键词并报告风险，不得静默继续跑。

短等待（如 `--kw-gap` 或请求抖动）可以合并到下一次汇报，但必须报告等待剩余时间；长任务结束时还要给出总耗时、各关键词耗时、视频/评论/命中漏斗、跳过数、重试数、失败原因、SQLite 写入数、清理结果和是否达到配额。

### 调试：离线筛选（仅排障用）

```bat
python %POOL%\tools\filter_pool.py --in <已聚合 comments.json> --out <candidates.json> ^
  --min-likes 1000 --min-replies 50 --min-len 30 --min-score 55 --max-n 60
python %POOL%\tools\run_daily.py --root <根> --account pool --keywords "x" --per-keyword 0 ^
  --offline-source <已聚合 comments.json> --quota 5
```

`--offline-source` 喂存量聚合 `comments.json`（douyin-crawl-report 的 comments.py 产物或本技能 aggregate 产物）做筛选，**仅当无法实时抓取时调试用，日常请走上方实时主路径**。

### 循环利用沉淀池
精选爆款评论**默认直接入库** `hits` 表（含打分、来源视频、理由），供 master-copywriting 按人设改写成文案；`report.py --hot` 可随时查看爆款榜。去重规则：同 `comment_id` 或同文本不重复入选。

## SQLite 数据中心（累计 + 分析）

把**爆款评论池相关的搜索批次**（视频全部信息、作者、命中的评论、采集元信息）统一落入技能内置的 SQLite，便于系统化累计、关联分析与跨 Agent 移植。库文件随技能走，**整体复制技能目录即可移植到其他 agent 使用**。

| 命令 | 作用 |
|---|---|
| `python sqlite\db.py --init` | 建库建表（6表+2视图） |
| `python sqlite\loader.py --dir <批次目录> --account <slug> [--keyword 词]` | 幂等导入单个搜索批次 |
| `python sqlite\loader.py --all <工作根>` | 回填工作根下所有 `hcp*` 搜索批次（幂等，重复跑不翻倍） |
| `python sqlite\hits_backfill.py --pool <pool.json>` | 把存量 pool.json 命中回流 `hits` 表（兼容旧版产物） |
| `python sqlite\report.py --stats / --hot / --accounts / --batches / --threads --cid` | 汇总 / 爆款榜 / 作者榜 / 批次 / 讨论串 |

**Shell 直查**（SQLite 已内置 Python）：
```bat
python -c "import sqlite3;c=sqlite3.connect(r'%USERPROFILE%\.trae-cn\skills\douyin-hot-comment-pool\sqlite\douyin_hotpool.db');\
print([r for r in c.execute('select source_keyword,count(*) from videos group by source_keyword')])"
```

**库设计**：
```
accounts(creator_hash PK, nickname, 首见/末见, 统计)
videos(aweme_id PK → accounts, 标题/互动/来源词/各URL[仅URL不解析])
comments(comment_id PK → videos, 内容/点赞/回复/父id/pictures仅URL/batch)
ancestry(子→父→根, depth)          ← 讨论串
hits(comment_id PK → comments, score, 命中理由/明细, 日期)   ← 爆款沉淀
batches(batch_id PK, 采集时间/词/视频/评论/状态/阶段/最后进度/错误) ← 采集元信息与进度心跳
视图: vw_hot_comments(命中+视频+作者), vw_threads(讨论串层级), vw_daily_stats
```

**约定**：
- **幂等约定**：重复导入同批次不产生重复行（UPSERT），天然支持断点续跑与累计。
- **只入 URL**：评论图片、视频下载链接等大字段仅存 URL 字符串，不解析二进制/图片，控制库体积。
- **只导爆款池相关**：`--all` 只批量导入目录名含 `hcp` 的搜索批次，不导入定向账号采集批次。

## 三级门槛参数（阈值自行调整）

| 门槛 | 参数 | 默认 | 说明 |
|---|---|---|---|
| ① 高互动 | `--min-likes` / `--min-replies` | 1000 / 50 | 点赞或回复任一达到即过 |
| ② 字数 | `--min-len` | 30 | 去符号/去 emoji 后有效字数，过滤短评 |
| ③ 可成文性 | `--min-score` | 55 | 规则评分（钩子/情绪/具体意象/数字/行动主体/疑问）；另含口水词排除（"哈哈哈""收藏了""扣1"等） |
| 每日配额 | `--quota` | 5 | 每日达标即停上限 |

## 命令行参数速查

| 脚本 | 关键参数 |
|---|---|
| `run_daily.py` | `--root --account --keywords(必填,分号分隔) --quota --preset(safe默认|ultra|fast) --per-keyword(10) --comments-count(30) --speed --sleep-min/--sleep-max --kw-gap(90) --min-* [--sort-by-likes 搜索按最多点赞排序] [--show-browser 弹窗采集] [`--offline-source` 仅调试]。显式参数覆盖 preset |
| `run_daily_v2.py` | 两段式提速调度器：`--root --account --keywords(分号分隔) --quota --preset --per-keyword --comments-count [--skip-search 断点续跑]`。合并搜索→本地按赞排序去重→仅对 Top 高赞视频定向深挖（评论深度自适应 100~250 条/视频） |
| `collect_search.py` | `--root --account --keywords(分号分隔) --preset(safe默认|ultra|fast) --per-keyword(10) --comments-count(30) --speed --sleep-min/--sleep-max --retry-fail --max-min --lt [--cookies] [--min-likes/--min-replies 评论翻页热度水位，低于即早停] [--skip-file 已采视频ID列表] [--no-headless 弹窗采集] [--raw-crawler]` || 内存聚合入口 `aggregate_paths(fps, max_n)`（主路径）；CLI `--in <jsonl或目录> --out --max` 仅调试落盘 |

## 输出结构

**数据唯一落点是技能内置 SQLite，磁盘零 JSON 残留**：

```
技能内置 SQLite（sqlite/douyin_hotpool.db）
  accounts   ← 采集到的视频作者（幂等 upsert）
  videos     ← 命中关键词的每个视频（标题/互动/各URL仅URL）
  comments   ← 采集到的全部评论
  ancestry   ← 评论上下级讨论串
  hits       ← 精选爆款命中（score/理由/日期，达标即停依据）
  batches    ← 每次采集批次元信息（时间/词/视频/评论数/状态/阶段/最后进度）
```

- 每个关键词抓完**立即入库**（loader 幂等导入原始数据 + hits 写入选），随后整段删除该词的运行目录（MediaCrawler jsonl/cursor 一并清理）；入库失败则保留现场排障。
- 运行结束删除 `.douyin-crawl-current-<account>.json` 指针，末尾打印**当日采集报告**（逐词统计/当日命中明细/库内累计/停止原因）。
- 查看爆款榜：`python sqlite\report.py --hot`；汇总 `--stats`；讨论串 `--threads --cid <id>`。

## 关键约束（跨 Agent 复用安全）

SQLite 是长期主库，JSONL 只作为采集期间的临时 staging 和入库失败后的重放现场；导入成功后删除 JSONL，失败时保留对应批次目录。`batches.status/phase/last_progress_at` 是运行监控的事实来源，旧库由 `db.ensure_schema()` 自动补列；SQLite 连接使用 WAL、`synchronous=NORMAL` 和 30 秒忙等待，允许进度查询与写入并存。

运行中查询最近批次时读取 `batch_id/keyword/status/phase/videos_count/comments_count/skipped_count/retry_count/last_progress_at/error`；`last_progress_at` 超过两分钟未更新必须检查子进程、日志、采集目录和数据库。失败批次保留 JSONL，可用 `sqlite/loader.py --dir` 重放；重放成功后才清理现场。

- **达标即停是硬保证**：`run_daily` 在启动时与**每个关键词开抓前**都查 `hits` 表当日（`hit_date=今天`）已入选数，已满 `quota` 立即停止后续采集，不会重复抓/重复筛。
- **入库即唯一数据落点**：逐词实时幂等写入 `accounts/videos/comments/ancestry/batches`，精选命中写 `hits`；筛选全程在内存完成，运行目录用后即删——工作根下不残留任何采集 JSON（入库失败时保留现场便于排障）。
- **已采视频跳过**：每词开抓前从库导出「已有评论的视频 ID」（临时 `.hcp-skip-<account>.txt`，用后即删），经 MediaCrawler `MC_SKIP_FILE` 钩子跳过重复视频的评论重抓（视频信息仍入库更新）；空库自动不启用，patch 幂等且 env 未设置时零行为变化，不影响本机其他 MediaCrawler 项目。
- **默认最多点赞排序**：`--search-sort 1` 为默认，搜索结果按最多点赞返回（实测严格赞降序），配合「按赞止停」请求量最少。网页版对部分**组合词**在最多点赞下返回空桶（`is empty(status=0) data:[]`），切 `--search-sort 0` 综合可破空（实测「二十四节气」综合成功 300 评论）。同词固定榜单的重复结果由已采跳过机制兜底。要单轮命中密度用默认最多点赞，要发现新内容用综合。
- **按赞止停（推荐与 --sort-by-likes 同用）**：`run_daily --stop-at-like-floor` —— 按赞降序抓到首个低于视频门槛（1 万）的视频即结束本关键词的翻页，**替代固定 per-keyword 条数**：大词自动多抓、小词自动早收，低赞视频连页面请求都省下。低于门槛的视频信息仍入库（供「二次复活」判断）。已实测触发（300k 测试阈值下 below_floor_n=4 正确截断）。
- **视频级点赞门槛（硬编码 1 万）**：搜索结果中 `digg_count`<10000 的视频跳过评论抓取、仅存视频信息——实测命中全部来自万赞以上视频，低赞视频评论请求产出为零。被门槛跳过的视频不进 skip 名单：**二次遇到时若点赞已涨过门槛会正常抓取**；无统计数据的视频保守放行。可用 `--video-min-likes` 调整。
- **评论翻页热度早停**：抖音评论接口为固定智能排序（实测不接受排序参数），页码越深越冷。翻页时页内最高赞 < `--min-likes` 且最高回复 < `--min-replies`（默认同三级门槛 1000/50）即停止该视频后续翻页，省掉冷尾请求；传 `0` 关闭。
- **默认有头会话指纹**：CDP 会话默认**有头**（`--headless` 才转无头），扫码重登后绑定可见浏览器指纹，显著降低空响应反爬。空结果日志已附 `status_code|msg`（诊断 patch 自动注入）可区分未登录/风控。
- **合规边界**：只抓公开可浏览评论区、控制频率避免风控。默认 `--preset safe`（并发1、每请求 6-15s 抖动＋词间休息 90s）稳；`ultra` 超稳档（12-28s＋词间 180s）用于风控期；`fast`（并发3、2-6s）提速但风控面更大，正式账号务必谨慎。爆款评论用于**借鉴结构/钩子，重写表达**落到 IP 账号，不建议照搬商用（版权风险）。
- **运行库**：`filter_pool / aggregate_comments / run_daily / collect_search` 仅用标准库（`collect_search` 另需本机已装的 MediaCrawler），任意 Python3 可跑。

## 免责声明与责任边界

本技能是本地自动化与数据整理工具，不构成法律意见、合规认证、平台授权或商业使用许可。使用者必须自行确认采集行为符合所在地法律法规、抖音及数据源平台规则、账号授权范围和适用的隐私/数据保护要求；公开可见不代表数据可以任意复制、传播或商业化。

使用者对登录态、Cookie/Token、采集范围与频率、数据保存和导出、第三方版权/个人信息/名誉权，以及由此产生的申诉、封禁、索赔、处罚或其他损失承担全部责任。不得采集非公开数据，不得绕过访问控制，不得将评论原样复制或用于骚扰、画像、歧视、诈骗、政治操纵、未成年人识别等违法或侵权用途。发现权限不明、敏感个人信息、版权投诉、平台警告或疑似限流时，立即停止实时采集并按适用规则处理或删除数据。

项目不保证数据的完整性、准确性、持续可用性或规避平台风控；如无法确认用途和采集依据的合法性，请不要运行实时采集。使用本技能即表示使用者已阅读并接受本责任边界。

## 反爬与风控机制（实战验证）

实时采集直接暴露于抖音风控，以下机制均经 2026-08 多轮真实运行日志验证，是「提速且不封号」的核心：

1. **默认最多点赞排序**（`--search-sort 1`）：高赞降序下「按赞止停」遇到首个低赞视频即结束本词，请求量最少、最省风控面。默认无需显式传参。
2. **组合词空桶 → 切综合破空**：网页版搜索对部分组合词（如「节气养生」全程空、「二十四节气」在最多点赞下空）返回 `is empty(status=0) data:[]`。切 `--search-sort 0` 综合可恢复（实测「二十四节气」综合出 300 评论）；仍空则属网页版正词无数据（App 端才有），改用**变体词**（如「二十四节气养生」「节气养生知识」实测可出 60~292 评论）或 **URL 定点抓取**。**教训：组合词先做综合排序探测，勿武断判空**。
3. **软限流预警**：连续多轮快速采集后空结果率骤升（实测 2/6→5/6）是接近风控线的强信号——应停止并冷却 30-60 分钟（或换 IP/账号），勿硬跑升级为封号。单轮空词比例达 50% 即视为软限流征兆。
4. **空结果=浏览器实例回收（正常，非崩溃）**：某词返回空后该词立即结束并回收 CDP 实例，表现为「打开抖音主页后浏览器闪退」；有数据时浏览器保持打开直至采集完成。**浏览器是否闪退可直接当该词有无数据的风向标**。
5. **搜索纠错开关**：`--query-correct 0` 关闭抖音纠错；对正词空桶实测帮助有限，保留作兜底开关。
6. **逐请求抖动 + 词间休息**：`--sleep-min/--sleep-max` 每请求时序扰动（safe 6-15s）、`--kw-gap` 词间休息（默认 90s）——摊平请求节奏，规避固定频率识别。
7. **默认有头绑定指纹**：CDP 会话默认有头（`--headless` 才无头），绑定可见浏览器指纹，实测显著降低空响应反爬（如「二十四节气」既有头又切综合后稳定出数据）。

## 依赖

- **MediaCrawler**（实时采集时）——直接调其 `main.py`，无需任何外部技能中转；经 env、全局注册指针 `runtime-registry.json` 或默认缓存 `~/.cache/codex-mediacrawler/MediaCrawler` 解析
- Python 3.9+（离线筛选仅需标准库）
- （可选）master-copywriting 用于把池中评论改写成文案

## 版本同步

任何 Agent 更新本技能时，必须同步递增 `agents/openai.yaml` 中 `interface.display_name` 的版本号。展示名格式固定为 `GM 中文用途 V版本号`；不得只更新技能内容而遗漏列表版本。

## 文件编码

技能内的文本文件必须使用 **UTF-8（无 BOM）** 保存。读取、生成或校验中文文件时，必须显式指定 UTF-8；在 Windows 上运行 Python 校验器时使用 `python -X utf8` 或设置 `PYTHONUTF8=1`，不得依赖系统默认 GBK 编码。
