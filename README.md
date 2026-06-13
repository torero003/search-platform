# Smart-Search 本地智能搜索平台

本地运行的多源搜索平台，聚合 **25+ 信息源**（搜索引擎、中文社区、财经门户、社交媒体、技术社区、视频平台），通过 CDP 浏览器自动化 + 直接 API 双通道并行搜索，RRF 跨源融合 + LLM 相关性评分 + 交叉验证，输出结构化结果。

**CLI 架构** — 直接调用 Python 脚本，无需启动 HTTP 服务器，可被 Agent 直接调度。

## 信息源（25+）

| 类别 | 源 | 方式 |
|------|-----|------|
| 搜索引擎 | Google、Bing、Yandex | CDP 浏览器 |
| 中文社区 | 知乎、雪球、V2EX、搜狗微信 | CDP 浏览器 / API |
| 视频平台 | YouTube (yt-dlp)、Bilibili | yt-dlp / CDP 浏览器 |
| 社交媒体 | Twitter (X) | CDP 浏览器 |
| 财经门户 | 东方财富、TrendForce、Yahoo Finance、CoinGecko、Binance、巨潮资讯、新浪财经、世界银行、SEC EDGAR、恐惧贪婪指数 | API |
| 技术社区 | GitHub、Hacker News (Algolia)、GitHub Trending、Reddit (RSS + .json) | API |
| 官方渠道 | 国家统计局 | CDP 浏览器 |
| 聚合源 | RSSHub | API |

## 快速开始

```bash
# 前置条件：Edge 浏览器以 CDP 模式运行（端口 9222）
# --remote-debugging-port=9222

# CLI 全源搜索
python search.py "Kubernetes 和 Docker 的区别" --all --max 50 --json

# 指定源搜索
python search.py "HBM 价格走势" --sources xueqiu,eastmoney,trendforce --max 20 --json

# 人类可读输出
python search.py "最新 AI 大模型" --all --max 20
```

## 核心能力

### 意图驱动的智能源选择

平台通过正则模式自动识别 **8 种搜索意图**（事实查询、产品对比、概念解释、观点征集、教程指南、比较分析、突发新闻、趋势预测）和 **4 大领域**（投资、AI、技术、政策），自动排除不相关源：

- 技术问题 → 优先 GitHub / V2EX / 知乎 / YouTube / B站，排除财经源
- 投资问题 → 优先雪球 / 东方财富 / TrendForce，排除技术社区
- 通用查询 → 全源并行

### RRF 跨源融合 + 五阶段去重

基于 Reciprocal Rank Fusion (k=60) 的跨源融合，经过五个处理阶段：

1. **URL 级去重** — 同 URL 跨源出现时合并 RRF 分数，保留更完整的摘要
2. **近似去重** — n-gram Jaccard + token Jaccard 双阈值，识别中英文近重复内容
3. **实体锚定降权** — 结果文本与查询关键词零重叠的结果降权 10 倍
4. **作者去重** — 同一作者最多保留 3 条，防止单一来源垄断
5. **源多样性保证** — 截断前确保每个有结果的源至少保留 2 条

### LLM 相关性评分

本地 LLM（Qwen3.6-27B）对融合后的结果进行 0-100 相关性评分，结合 engagement（点赞、评论、转发）min-max 归一化，最终综合排序：

```
综合得分 = 0.70 × LLM评分 + 0.15 × 参与度 + 0.10 × 源权重 + 0.05 × 内容长度
```

LLM 不可用时自动降级为确定性评分（实体匹配 + 标题加分 + 源质量 + 内容长度）。

### 交叉验证引擎

8 种正则模式从搜索结果中提取结构化事实（价格变动、产能利用率、市占率、营收规模、增长率、时间节点等），多源交叉比对：

- **3 源以上一致** → `High` 置信度 + `Verified`
- **2 源一致** → `Medium` + `Verified`
- **单源** → `Low` + `Unverified`
- **冲突数据** → 按源权重排序，标注优先级

### 视频字幕提取

YouTube 通过 yt-dlp 自动下载并解析字幕（优先中文，其次英文），Bilibili 通过 CDP 浏览器从页面提取字幕数据。字幕内容附加在结果 `content` 字段中，使视频搜索结果和文本结果一样可被交叉验证。

### 主题聚类

对观点征集、比较分析、突发新闻、趋势预测四类查询，自动将结果按主题聚类。每个聚类通过 MMR（Maximal Marginal Relevance）选择最具代表性的 3 条结果，避免信息冗余。

## 架构

```
用户查询
  │
  ▼
search.py (CLI 入口)
  │
  ▼
意图识别 (8 类意图 + 4 大领域) → 智能源选择 → 排除不相关源
  │
  ▼
并行搜索 (asyncio.gather)
  ├── CDP 浏览器通道  ──→ Google, Bing, 知乎, 雪球, Twitter, B站, ...
  └── 直接 API 通道    ──→ YouTube(yt-dlp), GitHub, HN(Algolia),
                          Reddit(RSS), CoinGecko, Yahoo Finance, ...
  │
  ▼
RRF 融合 (k=60) → 五阶段去重 → 实体锚定降权
  │
  ▼
LLM 相关性评分 → engagement 归一化 → 综合排序
  │
  ▼
交叉验证 (事实提取 + 多源比对) + 主题聚类
  │
  ▼
结构化 JSON / Markdown 输出
```

## 项目结构

```
├── search.py                    # CLI 入口脚本
├── config/
│   ├── sources.yml              # 25+ 源配置（类型/优先级/分类/搜索模板）
│   └── extraction_rules.yml     # 各源数据提取规则
├── app/
│   ├── api/
│   │   └── search.py            # 核心搜索管线（意图→源选择→并行搜索→融合→评分→输出）
│   ├── sources/
│   │   ├── api_source.py        # 所有 API 源实现（YouTube/GitHub/HN/Reddit/财经等）
│   │   ├── cdp_client.py        # CDP 浏览器直连客户端
│   │   ├── edge_mcp_source.py   # CDP 浏览器源基类（登录检测/自动恢复）
│   │   ├── reddit_source.py     # Reddit RSS + .json 双通道
│   │   └── video_transcript.py  # YouTube/B站字幕提取
│   ├── config.py                # 意图识别、源能力标签、分类排除规则
│   ├── fusion.py                # RRF 融合 + 五阶段去重
│   ├── judge.py                 # LLM 相关性评分 + engagement 归一化
│   ├── validator.py             # 交叉验证引擎（事实提取 + 多源比对）
│   ├── cluster.py               # 主题聚类 + MMR 代表选择
│   ├── llm_client.py            # 本地 LLM 客户端
│   ├── cache.py                 # SQLite 缓存（每源独立 TTL）
│   └── storage/                 # SQLite 存储层
└── requirements.txt
```

## 技术栈

- **CDP (Chrome DevTools Protocol)** — 通过 WebSocket 直连 Edge 浏览器，自动化搜索和登录检测
- **yt-dlp** — YouTube 搜索和字幕提取，无需浏览器
- **Asyncio** — 所有搜索源并行执行，单查询 30-120 秒完成
- **SQLite** — 每源独立缓存（TTL 60s-3600s），减少重复请求
- **FastAPI** — 可选的 HTTP 服务层（`app/main.py`）

## 配置

- `config/sources.yml` — 源注册表：类型（`api` / `edge_mcp`）、优先级、分类、搜索模板、登录配置
- `config/extraction_rules.yml` — 各源专属数据提取规则

## 应用场景

- **技术调研** — "Rust vs Go 微服务性能对比" → 自动搜索 GitHub Issues、V2EX 讨论、HN 帖子、YouTube 技术视频（含字幕）
- **投资分析** — "HBM 价格走势" → 并行查询 TrendForce 产业数据、雪球讨论、东方财富公告、Yahoo Finance 行情
- **政策追踪** — "最新半导体扶持政策" → 国家统计局 + 搜索引擎 + 搜狗微信
- **突发新闻** — "AI 大模型最新发布" → Twitter + HN + RSSHub + 搜索引擎全源并行
- **Agent 集成** — 通过 CLI 脚本直接被 Agent 调度，无需启动服务器
