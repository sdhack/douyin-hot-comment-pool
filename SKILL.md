---
name: "douyin-hot-comment-pool"
version: "0.7.0"
description: "搜索抖音高赞高互动的爆款长评论，经三级门槛（高互动→字数→可成文性）筛选沉淀成爆款文案素材池，每日达标即停；数据可落入技能内置 SQLite 做累计与分析。Invoke when user wants to 猎取抖音爆款评论、挖可做成文案的高互动长评、或每天定时刷一批能成文的评论素材。"
---

# 抖音爆款评论池（Douyin Hot Comment Pool）

在"爆款评论出现在哪并不固定、只能靠刷"的场景下，用**广撒网采样 + 三级门槛筛选 + 每日配额达标即停**把随机性摊平，持续沉淀「能改写成爆款文案」的高互动长评论。

## 核心思路

人工刷的效率瓶颈是"一台设备逐条翻"。本技能用机器批量喂入海量视频样本 → 帕累托筛出高互动长评论，等效"一次刷几百个视频的评论区"。三条硬原则：

1. **样本广而浅**——宁可关键词多、每条视频评论少，提高覆盖率（"位置不固定"靠覆盖摊平）。
2. **三级门槛缺一不可**——高互动(点赞/回复) → 字数(去符号后达标) → 可成文性(自带钩子/情绪/具体细节，脱离原视频仍成立)。短到口水的"哈哈哈"就算点赞上万也拆不出文案骨架，必须淘汰。
3. **达标即停**——每天最多入池 `--quota` 条（默认 5），达到立即结束，绝不让流程无限跑。当日配额记录在沉淀池 `daily/{YYYY-MM-DD}.count`，跨天自动重置。

## 何时使用

- 用户想"找/挖/搜 抖音高赞高互动评论""爆款评论做文案"
- 用户想：每天从无限刷的评论区里稳定捞出 5 条能成文的爆款评论
- 配合 master-copywriting 把沉淀评论改写成四账号文案

## 工作流

主路径是**实时 MediaCrawler 采集**（API 签名直抓，非浏览器 DOM 模拟）：关键词广撒网 → MediaCrawler 抓视频+评论 → 聚合 → 三级门槛筛选 → 入池 → 达标即停。离线只用于调试，不作日常入口。

### 主路径：实时 MediaCrawler 采集（日常用）

单命令完成"刷一批爆款评论、每天命中 5 条即停"：

```bat
set POOL=%USERPROFILE%\.trae-cn\skills\douyin-hot-comment-pool
REM 实时采集 + 筛选 + 沉淀 + 达标即停（一键；默认 --preset safe=慢档：并发1/延时3-8s，最稳）
python %POOL%\tools\run_daily.py --root <工作根> --account <slug> ^
  --keywords "养生;智商税;避坑;宝妈" --quota 5 --preset safe
REM 提速用快档：--preset fast（并发3/延时1-3s，风控面更大）。显式 --speed/--sleep-*/--per-keyword 会覆盖 preset
```

采集直接依赖 **MediaCrawler**（本机已安装的抓取引擎，`collect_search.py` 内嵌其调度，零外部技能依赖；API 签名直抓，`--headless` 默认避免弹窗）。**首次需扫码登录一次**，之后复用登录态缓存。频率经 `--preset` 控制：默认 `safe`（并发1、延时3-8s）最稳；切换 `--preset fast`（并发3、延时1-3s）提速但风控面更大。显式 `--speed/--sleep-*/--per-keyword` 会覆盖 preset。`--retry-fail` 失败重试。

### 调试：离线筛选（仅排障用）

```bat
python %POOL%\tools\filter_pool.py --in <已聚合 comments.json> --out <candidates.json> ^
  --min-likes 1000 --min-replies 50 --min-len 30 --min-score 55 --max-n 60
python %POOL%\tools\run_daily.py --root <根> --account pool --keywords "x" --per-keyword 0 ^
  --offline-source <已聚合 comments.json> --quota 5
```

`--offline-source` 喂存量聚合 `comments.json`（douyin-crawl-report 的 comments.py 产物或本技能 aggregate 产物）做筛选，**仅当无法实时抓取时调试用，日常请走上方实时主路径**。

### 循环利用沉淀池
`<root>/pool/<account>.json` 跨天累积所有入选爆款评论（含打分、来源视频、理由），供 master-copywriting 按人设改写成文案。去重规则：同 `comment_id` 或同文本不重复入选。

## SQLite 数据中心（累计 + 分析）

把**爆款评论池相关的搜索批次**（视频全部信息、作者、命中的评论、采集元信息）统一落入技能内置的 SQLite，便于系统化累计、关联分析与跨 Agent 移植。库文件随技能走，**整体复制技能目录即可移植到其他 agent 使用**。

| 命令 | 作用 |
|---|---|
| `python sqlite\db.py --init` | 建库建表（6表+2视图） |
| `python sqlite\loader.py --dir <批次目录> --account <slug> [--keyword 词]` | 幂等导入单个搜索批次 |
| `python sqlite\loader.py --all <工作根>` | 回填工作根下所有 `hcp*` 搜索批次（幂等，重复跑不翻倍） |
| `python sqlite\hits_backfill.py --pool <pool.json>` | 把沉淀池命中回流 `hits` 表 |
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
batches(batch_id PK, 采集时间/词/视频/评论/成功率)            ← 采集元信息
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
| `run_daily.py` | `--root --account --keywords(必填,分号分隔) --quota --preset(safe默认|fast) --per-keyword(10) --comments-count(30) --speed --sleep-min/--sleep-max --retry-fail --max-min --min-* [`--offline-source` 仅调试]。显式 `--speed/--sleep-*` 覆盖 preset |
| `collect_search.py` | `--root --account --keywords(分号分隔) --preset(safe默认|fast) --per-keyword(10) --comments-count(30) --speed --sleep-min/--sleep-max --retry-fail --max-min --lt [--cookies] [--headless] [--raw-crawler]` |
| `filter_pool.py` | `--in --out --min-likes --min-replies --min-len --min-score --max-n` |
| `aggregate_comments.py` | `--in <jsonl或目录> --out --max`（每视频按赞 top-N） |

## 输出结构

```
<root>/pool/<account>.json
  { meta:{account, quota_per_day},
    pool:[ {content, nickname, like_count, sub_comment_count, comment_id, aweme_id,
            create_time, len, score, score_breakdown:{...}, reasons:[...]}, ... ],
    daily:{ YYYY-MM-DD:{count, added} } }   # count 即当日已达标数，达 quota 即停
```

## 关键约束（跨 Agent 复用安全）

- **达标即停是硬保证**：`run_daily` 每次先读当日 `daily` 计数，已满 `quota` 立即返回，不会重复抓/重复筛。
- **沉淀池断点续跑**：`<account>.json` 是唯一事实来源，损坏则拒绝覆盖（不静默丢数据）。
- **合规边界**：只抓公开可浏览评论区、控制频率避免风控。默认 `--preset safe`（并发1、延时3-8s）最稳；`--preset fast`（并发3、延时1-3s）提速但风控面更大，用于正式账号务必谨慎。爆款评论用于**借鉴结构/钩子，重写表达**落到 IP 账号，不建议照搬商用（版权风险）。
- **运行库**：`filter_pool / aggregate_comments / run_daily / collect_search` 仅用标准库（`collect_search` 另需本机已装的 MediaCrawler），任意 Python3 可跑。

## 依赖

- **MediaCrawler**（实时采集时）——直接调其 `main.py`，无需任何外部技能中转；经 env、全局注册指针 `runtime-registry.json` 或默认缓存 `~/.cache/codex-mediacrawler/MediaCrawler` 解析
- Python 3.9+（离线筛选仅需标准库）
- （可选）master-copywriting 用于把池中评论改写成文案