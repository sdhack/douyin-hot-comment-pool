<div align="center">

# 🎯 抖音爆款评论池

**让「爆款评论可遇不可求」变成「每天稳定捞出能改写成爆款文案的高互动长评论」**

> 广撒网采样 × 三级门槛筛选 × 每日配额达标即停 —— 把随机性摊平，把爆款沉淀成资产。
> 逐词实时入库 SQLite、磁盘零 JSON 残留、跨 Agent 可移植。

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=fff)](#)
[![SQLite](https://img.shields.io/badge/SQLite-内置数据中心-003B57?logo=sqlite&logoColor=fff)](#)
[![MediaCrawler](https://img.shields.io/badge/引擎-MediaCrawler_API直抓-orange)](#)
[![Skill v](https://img.shields.io/badge/Skill-v0.9.5-blue)](CHANGELOG.md)
[![License](https://img.shields.io/badge/License-MIT-green)](#)

**单场实测：4 小时无人值守 · 41 个关键词轮次 · 采样 3.8 万条评论 · 沉淀 100 条爆款命中 · 磁盘零 JSON 残留**

</div>

---

## 不刷“热闹”，只沉淀可再创作的证据

这套技能把评论当作可运营的数据资产，而非一次性截图：每条入选记录都保留互动、来源、评分和入选理由，跨批次去重并写入 SQLite。它的目标不是无限抓取，而是在每日配额达标时停止，把时间花在真正能转化成选题、脚本或洞察的长评论上。

适合公开评论区的内容洞察和文案素材沉淀；不适合规避平台机制、批量导出个人信息，或把互动数据当作真实消费意愿的证明。

## ✨ 它解决什么

人工刷抖音的痛点是「一台设备、逐条翻、靠运气」，而爆款评论往往**出现在哪并不固定**——可能藏在某个随机刷到的视频评论区，根本不按你的预期出现。

本项目把这件事变成**可工程化的流水线**：用机器批量喂入海量视频样本，等效「一次刷几百个视频的评论区」，再用三道门槛把真正**脱离原视频也能成文**的评论筛出来。

```text
   大海捞针 → 广撒网采样 → 三级门槛 → 每日达标即停 → 沉淀成文案素材池
   （位置不固定）  （覆盖摊平随机）（漏斗精选）  （永不无限跑）  （供改写成爆款文案）
```

### 三条硬原则

| # | 原则 | 说明 |
|---|------|------|
| 1 | **样本广而浅** | 宁可关键词多、每条视频评论少，覆盖优先——位置不固定靠覆盖摊平 |
| 2 | **三级门槛缺一不可** | 高互动(点赞/回复) → 字数(去符号达标) → 可成文性(自带钩子/情绪/细节)。「哈哈哈」这种短到口水的再高赞也拆不出一句文案骨架，必须淘汰 |
| 3 | **达标即停** | 每天最多入池 `--quota` 条（默认 5），**每个关键词开抓前复查配额**，达标立即停止，流程永不自嗨到跑不完 |

---

## 🔥 六个硬核特性

| 特性 | 说明 |
|------|------|
| ⚡ **逐词实时入库** | 每个关键词抓完**秒级**导入 SQLite（幂等 UPSERT），不等全批跑完，中断也不丢数据 |
| 🧠 **内存筛选管线** | 聚合+三级门槛全程内存完成，不再产生 `comments_aggregated.json` 等任何中间文件 |
| 🧹 **磁盘零 JSON 残留** | 每词入库后运行目录（含引擎 jsonl/cursor/指针）整段删除——**数据唯一落点是 SQLite**；入库失败才保留现场排障 |
| 🚫 **全量智能去重** | 「同 comment_id 或同文本不重复入选」跨天生效——历史命中永不重记，配额只被真正的新评论消耗 |
| 🛑 **达标即停硬保证** | 启动时 + 每词开抓前双重复查 `hits` 当日数，配额一满立即停，绝不多抓一轮 |
| 📊 **当日采集报告** | 运行结束自动输出：逐词统计 / 当日命中明细（评分·赞回·摘录）/ 库内累计 / 耗时 / 停止原因 |

---

## 🧩 工作流程

```
                    ┌────────────────────────────────────────────┐
   关键词广撒网 ──▶ │ MediaCrawler API 直抓（非浏览器 DOM 模拟）│
   （养生/智商税   │  = 一次抓几百个视频的评论区                │
     避坑/宝妈…）  └────────────────────────────────────────────┘
                                    │  ▶ 逐关键词循环：
                                    ▼
                    ┌─────────────────────────────────────────┐
                    │ ① 实时入库 SQLite（loader 幂等导入）     │
                    │ ② 内存聚合(每视频按赞 top-N，不落 JSON)  │
                    │ ③ 三级门槛筛选：高互动→字数→可成文        │
                    │    like≥1000 或 reply≥50 / 字数≥30 / 评分≥55 │
                    │ ④ 写入 hits（配额封顶 + 全量去重）        │
                    │ ⑤ 清理采集目录（磁盘零 JSON 残留）        │
                    └─────────────────────────────────────────┘
                                    │
                                    ▼
                  达标即停 —— 每词开抓前复查配额，满额立即停止
                                    │
                                    ▼
              末尾输出当日报告 + 内置 SQLite 数据中心（唯一数据落点）
              accounts/videos/comments/ancestry/（原始采集）
              + hits 表（精选爆款命中 / 达标即停依据）
              └─ 累计 / 关联分析 / 跨 Agent 移植
```

> 主路径走**实时 MediaCrawler 采集**（API 签名直抓，`--headless` 默认），首次扫码登录一次后复用缓存。离线模式仅用于调试排障。
> 采集间隔三重防护：每请求区间抖动（safe 档 6-15s）× 词间休息（默认 90s，`--kw-gap` 可调）× 超稳档 `ultra`（12-28s＋词间 180s，风控期/长跑用）。

---

## 🏆 实测战绩（v0.9.1 单场会话，2026-08-24）

一次「健康」主题抓取，从主词延伸出 39 个关联关键词，跑满 100 条配额自动停止：

| 指标 | 数值 |
|---|---|
| 爆款命中 | **100 条**（全部过三级门槛 + 全量去重） |
| 采样规模 | 909 视频 / 37,964 评论 / 653 作者 |
| 命中质量 | 平均 **6,639 赞** / 253 回复；最高 15 万赞 / 5,257 回复 |
| 词效发现 | 疾病/心理科普词最高产：脂肪肝 +5、幽门螺旋杆菌 +5、「老年痴呆+抑郁症」两词爆发 +23 |
| 无人值守 | 约 4 小时（4 个批次自动衔接，配额一满剩余关键词直接跳过） |
| 磁盘残留 | 0 个 JSON（每词入库即清理） |

**池中真实命中样例**（评分 / 互动 / 摘录）：

```text
91分  268赞/79回    24岁第一次查胃肠镜。那段时间经常不舒服，以为是胃病……
80分  8135赞/339回  我爷爷也有点痴呆了，他成天说我爸，说一天天不上班，就知道给家看电视……
79分  3540赞/306回  我就是受益者，年初250斤进医院，下定决心减肥就是16+8，现在180……
77分  52308赞/5257回 真心建议，千万不要看见别人发光，就觉得自己暗淡，他强任他强，清风拂山岗……
```

**运行结束自动输出的当日报告（节选）**：

```text
==============================================================
[当日采集报告] 2026-08-24  库: .../sqlite/douyin_hotpool.db
--------------------------------------------------------------
  「焦虑」  采集视频 20 / 入库评论 800 / 新增命中 2  [成功]
  ...
--------------------------------------------------------------
  当日入池: 100/100（已达标）
    91分 268赞/79回   24岁第一次查胃肠镜……
    ...
  库内累计: 视频 909 / 评论 37964 / 爆款命中 100
  耗时 361s  JSON 残留: 0（采集产物已随入库清理）
==============================================================
```

---

## ⚡ 性能进化：v1 → v2 两段式采集（v0.9.2，2026-08-25）

新增 `run_daily_v2.py` 两段式调度器：六关键词**合并单进程**广撒网搜索 → 本地按赞排序 + 跨天去重 → 仅对 Top 高赞视频定向深挖评论区（评论深度自适应 100~250 条/视频）。单轮全流程从 **25.5 分钟压缩到 81.8 秒（≈19×）**，四轮累计 8.4 分钟收满当日 5 条配额。

| 指标 | v1 基线 | v2 |
|---|---|---|
| 首轮全流程 | 25.5 min | **81.8 s** |
| 四轮累计（收满 5/5 配额） | — | 8.4 min |
| 评论漏斗（每视频评论数） | 10 条 → 长评颗粒无收 | 100~250 条 → 稳定命中 |

详细对比见 [`reports/compare-v1-vs-v2/`](reports/compare-v1-vs-v2/compare-v1-vs-v2.html)，基线测量与勘误见 [`养生爆款评论池运行报告`](reports/养生爆款评论池运行报告_20260824.html)。

```bat
python %POOL%\tools\run_daily_v2.py --root <工作根> --account <slug> ^
  --keywords "养生;健康;医学科普" --quota 5 --preset fast
REM 中断续跑（跳过已完成的搜索段直接进入深挖）：追加 --skip-search
```

---

## 🔧 快速开始

### 0️⃣ 安装（可移植到任意 agent）

把整个 `douyin-hot-comment-pool/` 目录放到目标 agent 的 skills 目录即可。**仅需 Python 3.9+**，`filter_pool / run_daily / sqlite/*` 全部只用标准库。

```bat
python sqlite\db.py --init     REM 首次建库（库文件不入版本库，init 一键重建 schema）
```

**仅实时采集需本机已装 MediaCrawler**（`collect_search.py` 直接调度其 `main.py`，经 env `MEDIACRAWLER_PY/MC_ROOT` > 全局注册指针 `runtime-registry.json` > 默认缓存 `~/.cache/codex-mediacrawler/MediaCrawler` 解析）。若首次启动报 `Executable doesn't exist ...chromium_headless_shell`，补装 Playwright 浏览器二进制：

```bat
<mediacrawler>\\.venv\Scripts\python.exe -m playwright install chromium --only-shell
```

### 1️⃣ 一键日更：每天捞 5 条爆款评论即停

```bat
set POOL=%~dp0
REM 默认 --preset safe=慢档（并发1/每请求6-15s抖动+词间休息90s）；要提速换 fast，风控期用 ultra（12-28s+词间180s）
python %POOL%\tools\run_daily.py --root <工作根> --account <slug> ^
  --keywords "养生;智商税;避坑;宝妈" --quota 5 --preset safe --retry-fail 2
REM 显式 --speed/--sleep-*/--per-keyword/--comments-count 会覆盖 preset
```

要冲量？把 `--quota` 调大并多给关键词即可（实测 `--quota 100` + 39 词跑满自停）：

```bat
python %POOL%\tools\run_daily.py --root <工作根> --account <slug> ^
  --keywords "健康;医学科普;脂肪肝;体检;抑郁症" --quota 100 ^
  --per-keyword 20 --comments-count 40 --sleep-min 2 --sleep-max 4
```

### 2️⃣ 只用已有的评论数据跑通筛选（离线调试）

```bat
python tools\filter_pool.py --in <comments.json> --out <candidates.json> ^
  --min-likes 1000 --min-replies 50 --min-len 30 --min-score 55 --max-n 60
```

### 3️⃣ SQLite 数据累计与分析

```bat
python sqlite\loader.py --dir <搜索批次目录>     REM 幂等导入一批采集数据
python sqlite\loader.py --all <工作根>            REM 批量回填所有 hcp* 批次
python sqlite\report.py --stats                   REM 汇总统计
python sqlite\report.py --hot --top 20            REM 爆款榜（JOIN 视频/作者）
python sqlite\report.py --threads --cid <评论id>  REM 查看某爆款的讨论串
```

---

## 📚 三级门槛参数

| 门槛 | 参数 | 默认 | 说明 |
|------|------|------|------|
| ① 高互动 | `--min-likes` / `--min-replies` | `1000` / `50` | 点赞**或**回复任一达到即过 |
| ② 字数 | `--min-len` | `30` | 去符号 / 去 emoji 后有效字数，过滤短评 |
| ③ 可成文性 | `--min-score` | `55` | 规则评分（钩子/情绪/具体意象/数字/行动主体/疑问）＋口水词排除（"哈哈哈""收藏了""扣1"） |
| 每日配额 | `--quota` | `5` | 每日达标即停上限（可按需调大到 100+） |

> 阈值全部可用命令行调整，按你的行业 / 平台 / 爆款标准微调。

---

## 🗄️ SQLite 数据中心设计

把**爆款评论池相关搜索批次**的：视频全部信息、作者信息、命中的评论、采集元信息，统一落入技能内置 SQLite，便于系统化累计、关联分析与**跨 Agent 移植**。库文件随技能走，整体复制技能目录即可迁移。

```
┌────────────┐  1:n   ┌──────────┐  1:n  ┌──────────────┐
│  accounts  │ ─────▶ │  videos  │ ─────▶ │   comments   │
│  creator PK│        │ aweme PK │        │ comment_id PK│
│  昵称/统计 │        │ 标题/互动│        │ 内容/点赞/回复│
└────────────┘        │ 各URL(只URL)│     │ 父子/URL/batch│
                      └──────────┘        └──────┬───────┘
                                                 │ 1:n
                                          ┌──────▼───────┐   ┌──────────────┐
                                          │ ancestrv 讨论串│   │    hits 爆款 │
                                          │ 子→父→根/depth│   │  score/理由  │
                                          └──────────────┘   └──────┬───────┘
                                               batches(采集元信息) ─┘
      视图: vw_hot_comments(命中+视频+作者) · vw_threads(讨论串) · vw_daily_stats(每日累计)
```

**设计约束**（可移植性 / 体积 / 数据安全的平衡）：
- **幂等**：重复导入同批次不产生重复行（UPSERT），天然支持断点续跑与跨天累计。
- **只入 URL**：评论图片、视频下载链接等大字段仅存 URL 字符串，不解析二进制，控制库体积。
- **只导爆款池相关**：`--all` 只批量导入 `hcp*` 搜索批次，不误导入定向账号采集批次。
- **数据不出本地**：库文件不入版本库（`.gitignore` 拦截），克隆后 `db.py --init` 重建空库，真实采集数据留在数据拥有方本地。

---

## 🛡️ 合规边界

- 只抓**公开可浏览**的评论区，频率经 `--preset` 控制：默认 `safe`（并发1 + 每请求 6-15s 抖动 + 词间休息 90s + 失败重试）稳；`ultra` 超稳档（12-28s + 词间 180s）用于风控期；`fast`（并发3 + 2-6s）提速但风控暴露面更大，正式账号务必谨慎。
- 爆款评论用于**借鉴结构 / 钩子、重写表达**落到 IP 账号，不建议照搬商用（版权风险）。
- 采集数据由数据拥有方自行保管，打包不携带真实数据外发。

---

## ✅ 验证

```bat
python tests\selfcheck.py           REM 三级门槛逻辑自测
python tests\sqlite_selfcheck.py    REM SQLite 全链路端到端自测（9 项）
python tests\ingest_selfcheck.py    REM 实时入库管线自测（8 项：入库/门槛/配额/清理/跨天去重）
```

`ingest_selfcheck` 覆盖：内存 top-N 聚合排序回归 → 实时入库 → 三级门槛命中 → 配额封顶 → 运行目录清理 → 空结果清理 → **跨天去重**。全部离线可跑，不依赖网络 / MediaCrawler。✓（v0.9.1 全通过）

---

## 🧱 目录结构

```
douyin-hot-comment-pool/
├── SKILL.md                    # 技能主规范（入口）
├── manifest.json               # 技能元数据 / 依赖 / 权限
├── README.md                   # 项目说明（本文件）
├── CHANGELOG.md                # 版本变更
├── SECURITY.md                 # 安全边界与漏洞报告
├── CONTRIBUTING.md             # 贡献指南
├── CODE_OF_CONDUCT.md          # 行为准则
├── LICENSE                     # MIT
├── references/
│   └── keywords.example.yml    # 关键词池配置示例
├── tools/
│   ├── run_daily.py            # 每日主路径：逐词实时入库→内存筛选→达标即停→当日报告
│   ├── run_daily_v2.py         # 两段式采集调度器（v0.9.2：合并搜索→定向深挖→断点续跑）
│   ├── collect_search.py       # 关键词广撒网采集（直接调度本机 MediaCrawler）
│   ├── filter_pool.py          # 三级门槛筛选（可离线单独用）
│   ├── aggregate_comments.py   # 评论聚合（内存 aggregate_paths 主路径 + CLI 调试落盘）
│   └── _presets.py             # --preset 采集档位预设（safe/fast，供 run_daily 与 collect_search 共享）
├── sqlite/
│   ├── db.py                   # 建库建表（6表+3视图），一键 init
│   ├── loader.py               # 幂等导入搜索批次 → 库（支持显式 batch_id 逐词记账）
│   ├── hits_backfill.py        # 沉淀池命中回流 hits 表
│   ├── report.py               # 汇总 / 爆款榜 / 账号榜 / 批次 / 讨论串
│   └── douyin_hotpool.db       # 本地数据中心（不入版本库；db.py --init 重建）
├── tests/
│   ├── selfcheck.py            # 三级门槛自测
│   ├── sqlite_selfcheck.py     # SQLite 全链路端到端自测
│   └── ingest_selfcheck.py     # 实时入库管线自测（8 项）
└── reports/
    ├── compare-v1-vs-v2/       # v1→v2 优化对比报告（HTML + 图表）
    └── 养生爆款评论池运行报告_20260824.html
```

---

## 📦 依赖

| 依赖 | 用途 | 备注 |
|------|------|------|
| Python 3.9+ | 运行 | 离线筛选 / 建库仅需标准库 |
| MediaCrawler | 实时采集（可选） | 直接调度其 main.py，需 `playwright install chromium --only-shell` 浏览器二进制 |
| `master-copywriting` | 文案改写（可选） | 把池中爆款评论改写成成 IP 账号文案 |

---

## 🤝 Roadmap / 后续

- [ ] 关键词命中占比分析，自动推荐当日最易出爆款的关键词
- [ ] 爆款评论 → `master-copywriting` 一键改写为四账号草稿
- [ ] 沉淀池趋势看板（SQLite → HTML 报表）

---

<div align="center">

Made with ❤️ for 让爆款评论不再碰运气

</div>
