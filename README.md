# ProxyPool — 免费代理池系统

自动抓取网络免费代理 → 过滤清洗 → 验证存活与匿名性 → 入库 → 每 5 分钟复检清理，对外提供 HTTP API 给爬虫取用。

## 1. 快速开始

```bash
cd /home/socks/Buckup/ProxyPool

# 初始化数据库
./.venv/bin/python -m proxypool initdb

# 起服务（API + 调度器）
./.venv/bin/python -m proxypool serve

# 打开控制台
# http://127.0.0.1:8088/
```

取代理：

```bash
# 随机取 1 个
curl "http://127.0.0.1:8088/get"

# 取 20 个，纯 ip:port 文本
curl "http://127.0.0.1:8088/get?n=20&format=text"

# 高匿 + 支持 HTTPS + 延迟 3 秒内
curl "http://127.0.0.1:8088/get?anonymity=elite&https=1&max_latency=3000&n=10"
```

## 2. 架构

```
┌─────────────┐   ┌──────────┐   ┌───────────┐   ┌─────────┐
│ 12 个免费源  │──▶│  Fetcher │──▶│ Validator │──▶│  Store  │
│ (github/api)│   │ 解析去重  │   │ 探测引擎   │   │ 状态机   │
└─────────────┘   └──────────┘   └───────────┘   └────┬────┘
                                                      │
                        ┌──────────────┐              ▼
                        │  Scheduler   │◀──────  SQLite/PG
                        │ 5min 精检热池 │
                        └──────┬───────┘
                               ▼
                        ┌──────────────┐
                        │  FastAPI     │──▶ /get 给爬虫
                        │  控制台页面    │
                        └──────────────┘
```

代码结构：

| 文件 | 职责 |
|---|---|
| `proxypool/config.py` | 配置加载与校验，启动即 fail-fast |
| `proxypool/models.py` | SQLAlchemy 模型，proxies / check_logs / round_stats |
| `proxypool/fetcher.py` | 12 个源的抓取与统一解析 |
| `proxypool/validator.py` | **自建 raw-socket 探测引擎**（核心） |
| `proxypool/store.py` | 状态机、批量 upsert、挑选、清理 |
| `proxypool/scheduler.py` | 双节奏调度（热池精检 / 冷池扩充） |
| `proxypool/api.py` | HTTP API + 管理接口 |
| `templates/dashboard.html` | 单页控制台 |
| `tests/` | 75 个测试（含 mock 服务端的协议级测试） |

## 3. 关键设计决策（都有实测依据）

### 3.1 为什么自己写 socket 探测，不用 requests/httpx

**实测**：httpx 通过代理访问时对部分代理不遵守 timeout，400 条一批能挂死 5 分钟以上不返回，整轮调度被拖死。

自建 `asyncio.open_connection` + `asyncio.wait_for` 硬包裹，超时严格生效（测试 `test_hard_timeout_is_enforced` 验证 1.5s 配置在 3s 内必返回）。

同时自建能拿到完整响应头，匿名性判定才准（httpx 的 proxy 模式拿不到全部头）。

### 3.2 为什么分两级探测

| 探测 | 目标 | 用途 | 实测通过率 |
|---|---|---|---|
| 明文 | `http://httpbin.org/get` | 判断存活 + 拿 origin/header 判匿名性 | 单次 11-18% |
| HTTPS | `https://api.ipify.org` (CONNECT+TLS) | 判断能否跑 HTTPS 目标 | **HTTP 代理里仅 16% 支持 CONNECT** |

HTTPS 是稀缺资源，必须单独标记。而且同一代理的 TLS 通过率会波动（部分代理按 SNI 过滤），所以记的是**通过率**而不是布尔值：

```json
{"supports_https": true, "https_success_rate": 0.67}
```

通过率低于 1/3 时自动撤销 `supports_https` 标记，不给爬虫不可靠的承诺。

### 3.3 为什么"5 分钟验证"是分层的

这是整个系统最关键的设计，直接来自实测：

| 并发 | 吞吐 | 存活率 | 5 分钟能验 | 覆盖 2.4 万条 |
|---|---|---|---|---|
| 200 | 30.7/s | 5.9% | 8603 | 14 分钟 |
| 400 | 50.0/s | 3.8% | 13987 | 9 分钟 |
| 600 | 67.9/s | 2.2% | 19018 | 7 分钟 |
| 800 | 83.1/s | **1.7%** | 23280 | 5 分钟 |

**把并发推到 800 硬塞 5 分钟会让存活率从 5.9% 掉到 1.7%**——高并发把可用代理挤成超时，等于误杀。

所以分成两个节奏：

- **热池精检**（每 5 分钟）：只复检 `alive + candidate`，约 1500-5000 条，100 秒内跑完，**100% 覆盖**。这才是你要求的"每 5 分钟验证清理"。
- **冷池扩充**（每 30 分钟 = 6 轮）：抓 12 个源、验证新的候选，扩充池子。

配置项：`scheduler.refresh_every_n_rounds: 6`。

### 3.4 状态机

```
                    ┌──────────────── 通过 ≥1 次 ─────────────┐
                    │                                        ▼
  抓取 ──▶ new ──失败─▶ candidate ──失败 2 次──▶ dead ──失败 8 次──▶ banned
            │             │                        │                  │
            │             └────── 通过 ────────────┘                  │
            └────────────────── 通过 ─────────────────────────────▶ alive
                                                                     │
  冷却到期后重新进入验证流程 ◀─────────────────────────────────────────┘
```

| 参数 | 值 | 依据 |
|---|---|---|
| `promote_consecutive_ok` | 1 | 首次通过即入池。设 2 会导致首轮结束池子为空、总量被压到 2.5% |
| `kill_consecutive_fail` | 2 | 设 3 会让 1.5 万条准僵尸长期占预算 |
| `ban_consecutive_fail` | 8 | 反复失败的彻底拉黑 |
| `dead_cooldown` | 1800s | 免费代理寿命短，30 分钟足够判断复活 |

### 3.5 匿名性判定

不只看 header，而是拿 `httpbin.org/get` 返回的 `origin` 链做判定：

| 等级 | 判定条件 |
|---|---|
| `elite` 高匿 | 出口只有一个 IP 且就是代理自身，无泄漏头 |
| `anonymous` 匿名 | 出口是别的 IP（多级跳转）或有代理特征头，但没暴露真实 IP |
| `transparent` 透明 | origin 链或请求头里出现了**你的真实出口 IP** |

实测发现很多"高匿"代理其实是透明代理，origin 会返回 `你的IP,代理IP`。这个判定是爬虫能否使用的关键。

## 3.5 控制台

控制台在 `http://<host>:8088/`，单页无外部依赖（图表是手写 SVG，零 JS 库）。

| 区块 | 内容 |
|---|---|
| KPI | 可用代理 / 库存总量 / 可用率 / 支持 HTTPS，含池子构成堆叠条 |
| 轮次趋势 | 双轴折线：存活+晋升走左轴，验证量走右轴 |
| 延迟分布 | 对数分桶柱状（<200ms ~ >8s） |
| 代理库存 | 分页表格，6 个筛选器，评分条 |
| 最近轮次 | 每轮 验/活/挂/晋/新 + 耗时 |
| 失败原因 | 近 6 小时，带条形背景 |
| 取用方式 | 按当前访问地址自动生成 curl 示例 |

**双轴是必须的**：验证量（12000）和存活（200）差 60 倍，同轴时存活曲线会被压成贴底直线，完全看不出趋势。这是实测踩过的坑。

**评分模型**：`score_up` 必须小（2），否则任何代理成功 5 次就顶满 100，面板上全是满分、进度条全满，失去区分度。改成 2 后长期稳定代理需 25 次全成功才满分，偶发失败会明显拉低。

**SVG 必须 overflow:hidden**：用 `preserveAspectRatio="none"` 拉伸时，超出的折线会溢出到相邻卡片上，实测出现过彩色色块。

### 对外（给爬虫）

`GET /get`

| 参数 | 说明 | 示例 |
|---|---|---|
| `n` | 数量，1-100 | `10` |
| `protocol` | `http` / `https` / `socks4` / `socks5` | `socks5` |
| `anonymity` | `elite` / `anonymous` / `transparent` | `elite` |
| `https` | `1` 只要支持 HTTPS 的 | `1` |
| `https_rate` | **HTTPS 实测通过率下限 0-1（推荐用这个）** | `0.5` |
| `max_latency` | 延迟上限 ms | `3000` |
| `min_score` | 最低评分 0-100 | `60` |
| `allow_transparent` | `1` 允许返回透明代理（默认不返回） | `0` |
| `format` | `json` / `text` / `list` | `text` |

默认会过滤掉透明代理。透明代理会把 `X-Forwarded-For` 里的真实 IP 透给目标站，用了等于没挂代理，还暴露自己。池子里这类占比不小（实测约四成），所以要的话得显式要。

**实测最优组合**（对 HTTPS 目标可用率 100%）：

```bash
curl "http://127.0.0.1:8088/get?n=10&https_rate=0.5&max_latency=4000"
```

排序规则：**支持 HTTPS 的优先**，其次 score，再按延迟。
（纯 score 排序会让"只支持 HTTP"的代理占满前排，实测可用率只有 10%。）

返回（`n=1` 时直接返回对象）：

```json
{
  "protocol": "socks5",
  "ip": "1.2.3.4",
  "port": 1080,
  "url": "socks5://1.2.3.4:1080",
  "anonymity": "elite",
  "supports_https": true,
  "https_success_rate": 0.8,
  "latency_ms": 512,
  "score": 86,
  "last_ok_at": "2026-11-03T07:15:22"
}
```

`GET /get/{protocol}` — 纯文本，按协议取。
`GET /health` — `{"status":"ok","alive":1434,"total":24000}`

### 对内（管理）

| 接口 | 说明 |
|---|---|
| `GET /api/stats` | 池子总览（状态/协议/匿名度分布、HTTPS 能力、调度状态） |
| `GET /api/trend?limit=60` | 轮次趋势（存活/晋升/验证量/耗时）+ 状态分布 |
| `GET /api/latency` | 可用代理延迟直方图（8 个对数分桶 + 均值/中位） |
| `GET /api/proxies` | 分页列表，支持 status/protocol/anonymity/https/search/order |
| `GET /api/rounds` | 最近轮次记录 |
| `GET /api/errors` | 近 6 小时失败原因聚合 |
| `POST /api/trigger` | 立即跑一轮 |
| `POST /api/proxies/delete` | 批量删除（body: `{"ids":[1,2]}`） |
| `GET /api/config` | 当前生效配置（脱敏） |

管理接口鉴权：`config.yaml` 的 `api.admin_token` 非空时，需带 `X-Admin-Token` 头。

## 5. 爬虫侧接入示例

```python
import random
import httpx

class ProxyPoolClient:
    def __init__(self, base="http://127.0.0.1:8088"):
        self.base = base

    def get(self, n=10, **filters):
        r = httpx.get(f"{self.base}/get", params={"n": n, **filters}, timeout=10)
        r.raise_for_status()
        d = r.json()
        return d.get("proxies", [d])

def crawl(urls):
    pool = ProxyPoolClient()
    proxies = pool.get(n=20, anonymity="elite", https=1, max_latency=5000)
    for url in urls:
        for _ in range(3):                      # 换 3 个代理重试
            p = random.choice(proxies)
            try:
                with httpx.Client(proxy=p["url"], timeout=15) as c:
                    resp = c.get(url)
                    if resp.status_code == 200:
                        yield url, resp.text
                        break
            except Exception:
                continue
```

## 6. 参数标定依据

所有数值都是在本机（24 核 / 香港出网 43.247.132.71）实测标定的，不是拍脑袋：

| 参数 | 值 | 测量方式 |
|---|---|---|
| `validator.timeout` | 8.0s | 存活代理延迟 p99 = 7.9s，低于 8s 会误杀 |
| `validator.concurrency` | 400 | 2000 条随机样本，四档并发实测（见 3.3） |
| `validator.attempts` | 1 | 失败占 82-89%，对失败也重试会让单轮耗时翻倍超出周期 |
| `policy.promote_consecutive_ok` | 1 | 连续 2 轮存活率仅 23%，设 2 会把池子压到 2.5% |
| `policy.kill_consecutive_fail` | 2 | 设 3 会让 1.5 万条准僵尸占满预算 |
| `cleanup.cold_pool_limit` | 12000 | 免费源存活率 11-18%，保留过多死数据会挤掉复检预算 |

改动这些值前先跑 `tests/bench_concurrency.py` 重新标定。

## 7. 运维

### systemd 部署

```bash
sudo cp deploy/proxypool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proxypool
sudo journalctl -u proxypool -f
```

**关键**：`LimitNOFILE` 必须 ≥ 4096。Linux 默认 soft limit 是 4096，并发 400 时会撞上限报 EMFILE（表现为大量莫名的 OSError）。服务已内置启动时自动提升，但 systemd 下仍建议显式配置。

### 日志

- 启动时打印本机出口 IP（匿名性判定基准）
- 每轮结束打印：验证数 / 存活 / 失败 / 晋升 / 淘汰 / 拉黑 / 清理 / 耗时
- 源失败逐个告警（免费源日常，不影响整体）
- 预算到达时打印 `预算到达，取消 N 条未完成验证`

### 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `/get` 返回 503 | 池子为空 | 等首轮跑完（约 100s），或 `POST /api/trigger` |
| 可用率持续 < 3% | 源质量下降 | 检查 `/api/errors`，换源或降低 `promote_consecutive_ok` |
| 轮次耗时 > 280s | 并发不足或 fd 不足 | 检查 fd 上限，调 `validator.concurrency` |
| 大量 `os_24` (EMFILE) | fd 不够 | `ulimit -n 8192` 或改 systemd `LimitNOFILE` |
| 标称支持 HTTPS 但爬虫用不了 | TLS 通过率低 | 用 `https_success_rate` 筛选 > 0.5 的 |

## 8. 测试

```bash
./.venv/bin/python -m pytest tests/ -q

# 只跑不依赖网络的单元测试
./.venv/bin/python -m pytest tests/test_parse.py tests/test_store.py -q

# 并发标定（会消耗真实网络，约 5 分钟）
./.venv/bin/python tests/bench_concurrency.py

# 端到端验证（从 API 取代理真实访问目标站）
./.venv/bin/python tests/test_e2e_live.py
```

测试覆盖：

- `test_parse.py`（20 个）：IP 校验、三种源格式解析、匿名性三级判定
- `test_store.py`（28 个）：状态机全路径、调度饥饿回归、HTTPS 通过率、清理
- `test_validator.py`（9 个）：**用本地 mock 服务端测协议实现**——HTTP 绝对 URI 格式、SOCKS5/SOCKS4 握手报文、硬超时、chunked 解析

## 9. 已知限制

1. **免费代理本质不可靠**。单次存活率 11-18%，5 分钟后可能失效 70%。爬虫必须实现"取代理 → 失败换一个"的重试逻辑，不能假设代理长期可用。
2. **HTTPS 是稀缺资源**。HTTP 代理里只有 16% 支持 CONNECT；池子里真正能跑 HTTPS 的约 360 条（主要为 SOCKS5）。HTTP 代理即使标记 supports_https，TLS 通道也不稳定（实测 50% 失败，会按 SNI 过滤）——**需要稳定 HTTPS 请优先用 SOCKS5，或必须带 `https_rate=0.5` 筛选**。
3. **默认取用排序偏向 HTTP 可用性**。默认返回的 10 个对 HTTP 目标 100% 可用，但对 HTTPS 目标只有 70%。明确要跑 HTTPS 时必须加参数。
4. **匿名性判定依赖 httpbin.org**。该站若不可用会退化到只看 header（准确性下降）。该站在高并发下会返回 429/409，已做区分处理（`target_throttled`，不计入代理失败）。可改 `validator.check_url_http` 为自建探测页。
5. **单机 SQLite 有写锁**。已启用 WAL，2000 并发读没问题；若要多实例部署请换 PostgreSQL（`database.url` 改成 `postgresql+asyncpg://...`）。
6. **不处理需要认证的代理**（407）。免费源里这类占比不高（近 1 小时约 200 条），直接判失败。
7. **socks4 不做 HTTPS 探测**。它不支持域名解析，访问 443 目标天然失败，`supports_https` 恒为 false 是正确的。
