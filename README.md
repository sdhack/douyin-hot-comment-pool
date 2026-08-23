<div align="center">

# 🎯 抖音爆款评论池

**让「爆款评论可遇不可求」变成「每天稳定捞 5 条能改写成爆款文案的高互动长评论」**

> 广撒网采样 × 三级门槛筛选 × 每日配额达标即停 —— 把随机性摊平，把爆款沉淀成资产。
> 数据可落入内置 SQLite 做累计与分析，整套技能 **可移植到任意 agent**。

<!-- badges -->
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=fff)](#)
[![SQLite](https://img.shields.io/badge/SQLite-内置-003B57?logo=sqlite&logoColor=fff)](#)
[![License](https://img.shields.io/badge/License-MIT-green)](#)
[![Skill v](https://img.shields.io/badge/Skill-v0.8.0-blue)](#)
[![Platform](https://img.shields.io/badge/Platform-TRAE%20%2F%20Claude%20%2F%20OpenAI-lightgrey)](#)

</div>

---

## ✨ 它解决什么

人工刷抖音的痛点是「一台设备、逐条翻、靠运气」，而爆款评论往往**出现在哪并不固定**——可能藏在某个随机刷到的视频评论区，根本不按你的预期出现。

本项目把这件事变成**可工程化的流水线**：用机器批量喂入海量视频样本，等效「一次刷几百个视频的评论区」，再用三道门槛把真正**脱离原视频也能成文**的评论筛出来。

```text
   大海捞针 → 广撒网采样 → 三级门槛 → 每日达标 5 条 → 沉淀成文案素材池
   （位置不固定）  （覆盖面摊平随机）（漏斗精选）   （流程永不无限跑）   （供改写成爆款文案）
```

### 三条硬原则

| # | 原则 | 说明 |
|---|------|------|
| 1 | **样本广而浅** | 宁可关键词多、每条视频评论少，覆盖优先——位置不固定靠覆盖摊平 |
| 2 | **三级门槛缺一不可** | 高互动(点赞/回复) → 字数(去符号达标) → 可成文性(自带钩子/情绪/细节)。「哈哈哈」这种短到口水的再高赞也拆不出一句文案骨架，必须淘汰 |
| 3 | **达标即停** | 每天最多入池 `--quota` 条（默认 5），达标立即结束，**流程永不自嗨到跑不完**；当日计数跨天自动重置 |

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
                    │ ④ 写入 hits（配额封顶）                  │
                    │ ⑤ 清理采集目录（磁盘零 JSON 残留）        │
                    └─────────────────────────────────────────┘
                                    │
                                    ▼
                  达标即停 —— 每词开抓前复查配额，满 5 条立即停止
                                    │
                                    ▼
              末尾输出当日报告 + 内置 SQLite 数据中心（唯一数据落点）
              accounts/videos/comments/ancestry/（原始采集）
              + hits 表（精选爆款命中 / 达标即停依据）
              └─ 累计 / 关联分析 / 跨 Agent 移植
```

> 主路径走**实时 MediaCrawler 采集**（API 签名直抓，`--headless` 默认），首次扫码登录一次后复用缓存。离线模式仅用于调试排障。

---

## 🔧 快速开始

### 0️⃣ 安装（可移植到任意 agent）

把整个 `douyin-hot-comment-pool/` 目录放到目标 agent 的 skills 目录即可。**仅需 Python 3.9+**，`filter_pool / run_daily / sqlite/*` 全部只用标准库。

```bat
python sqlite\db.py --init     REM 首次建库（自带空库，schema 已就绪）
```

**仅实时采集需本机已装 MediaCrawler**（`collect_search.py` 直接调度其 `main.py`，经 env `MEDIACRAWLER_PY/MC_ROOT` > 全局注册指针 `runtime-registry.json` > 默认缓存 `~/.cache/codex-mediacrawler/MediaCrawler` 解析）。若首次启动报 `Executable doesn't exist ...chromium_headless_shell`，补装 Playwright 浏览器二进制：

```bat
<mediacrawler>\\.venv\Scripts\python.exe -m playwright install chromium --only-shell
```

### 1️⃣ 一键日更：每天捞 5 条爆款评论即停

```bat
set POOL=%~dp0
REM 默认 --preset safe=慢档（并发1/延时3-8s，最稳）；要提速换 --preset fast（并发3/延时1-3s，风控面更大）
python %POOL%\tools\run_daily.py --root <工作根> --account <slug> ^
  --keywords "养生;智商税;避坑;宝妈" --quota 5 --preset safe --retry-fail 2
REM 显式 --speed/--sleep-*/--per-keyword/--comments-count 会覆盖 preset
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
| 每日配额 | `--quota` | `5` | 每日达标即停上限 |

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
- **空库外发**：打包只带空 schema 库，真实采集数据留在数据拥有方本地，不随包外泄。

---

## 🛡️ 合规边界

- 只抓**公开可浏览**的评论区，频率经 `--preset` 控制：默认 `safe`（并发1 + 延时3-8s + 失败重试）最稳；`--preset fast`（并发3 + 延时1-3s）提速但风控暴露面更大，正式账号务必谨慎。
- 爆款评论用于**借鉴结构 / 钩子、重写表达**落到 IP 账号，不建议照搬商用（版权风险）。
- 采集数据由数据拥有方自行保管，打包不携带真实数据外发。

---

## ✅ 验证

```bat
python tests\selfcheck.py          REM 三级门槛逻辑自测
python tests\sqlite_selfcheck.py   REM SQLite 全链路端到端自测（9 项）
```

`sqlite_selfcheck` 覆盖：建库视图 → 导入 → 讨论串 → 幂等重复导入 → hits 回流 → 讨论串视图。隔离解压后直接跑通 = 可移植。✓（v0.8.0 全通过）

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
│   ├── run_daily.py            # 每日主路径：实时采集→筛选→沉淀→达标即停
│   ├── collect_search.py       # 关键词广撒网采集（直接调度本机 MediaCrawler）
│   ├── filter_pool.py          # 三级门槛筛选（可离线单独用）
│   ├── aggregate_comments.py   # 评论 jsonl 聚合（每视频按赞 top-N）
│   └── _presets.py             # --preset 采集档位预设（safe/fast，供 run_daily 与 collect_search 共享）
├── sqlite/
│   ├── db.py                   # 建库建表（6表+3视图），一键 init
│   ├── loader.py               # 幂等导入搜索批次 → 库
│   ├── hits_backfill.py        # 沉淀池命中回流 hits 表
│   ├── report.py               # 汇总 / 爆款榜 / 账号榜 / 批次 / 讨论串
│   └── douyin_hotpool.db       # 空库（schema 就绪，数据由接收方自行导入）
└── tests/
    ├── selfcheck.py            # 三级门槛自测
    └── sqlite_selfcheck.py     # SQLite 全链路端到端自测
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