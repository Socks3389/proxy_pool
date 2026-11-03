# ProxyPool · 免费代理池

自动抓取网络免费代理 → 过滤清洗 → 验证存活/匿名性/HTTPS 能力 → 状态机入库 → 每 5 分钟复检清理 → HTTP API 供爬虫取用，附带可视化控制台。

**所有阈值都是在真实环境实测标定的**，不是拍脑袋。每个参数后面都写清了「为什么是这个值」和「不这样会怎样」。

---

## ⚠️ 与旧版本的关系

**这是完全重写的 v1.0，与历史版本不兼容。**

| | 旧版（≤ v3.0.0） | 新版（main 分支） |
|---|---|---|
| 血统 | [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool) 改版 | 从零重写 |
| Python | 2.7 / 3.5+ | **3.11+** |
| 存储 | Redis / Ssdb / MySQL | **SQLite / PostgreSQL** |
| 并发模型 | 多进程 + requests | **asyncio + 自建 raw socket** |
| 端口 | 5010 | **8088** |
| API | `/get` `/pop` `/all` `/count` `/delete` | **`/get` 统一入口 + 参数过滤** |
| 控制台 | 无 | **有（单页，零依赖）** |
| 依赖 | redis, ssdb, pymysql… | fastapi, httpx, sqlalchemy |

### 旧版本在哪

旧版代码完整保留在 tag **[`legacy-v3.0.0`](../../tree/legacy-v3.0.0)**，历史 Release 也都在。想用旧版：

```bash
git clone -b legacy-v3.0.0 https://github.com/Socks3389/proxy_pool.git
```

旧版文档见 [legacy-v3.0.0 的 README](../../blob/legacy-v3.0.0/README.md)。

**新版不读取旧版的 Redis 数据，也不兼容旧 API 参数**（`?type=https` 改成 `?https=1`）。迁移时爬虫侧要改调用方式。

---

## 为什么重写

旧版在免费代理这个场景下会遇到几个绕不过去的问题，都是实测出来的：

1. **`requests` 探代理会挂死** —— 对部分代理不遵守 timeout，一批 400 条能卡 5 分钟以上，整轮调度被拖死。新版用自建 `asyncio.open_connection` + `wait_for` 硬包裹，超时严格生效。
2. **Redis 不是必需品** —— 为存几万个短期数据引入中间件，运维成本过高。新版默认 SQLite，需要多实例时换 PostgreSQL。
3. **免费代理存活率只有 11-18%** —— 旧版「验证通过就入库永不移除」，跑一天库里全是僵尸。新版有完整状态机 + 墓碑表。
4. **HTTPS 能力是稀缺资源** —— 免费 HTTP 代理里只有 16% 支持 CONNECT。旧版只判断 `https` 字段，新版实测 TLS 并记录**通过率**。

---

## 快速开始

```bash
git clone https://github.com/Socks3389/proxy_pool.git
cd proxy_pool

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m proxypool initdb         # 初始化数据库
python -m proxypool serve          # 启动 API + 调度器
```

打开控制台：**http://127.0.0.1:8088/**

首次启动会立刻跑一轮抓取验证，约 100 秒后池子里就有货了。

### 取代理

```bash
# 取 10 个（排序优先给支持 HTTPS 的）
curl "http://127.0.0.1:8088/get?n=10"

# 纯 ip:port 文本，直接喂爬虫
curl "http://127.0.0.1:8088/get?n=20&format=text"

# 高匿 + HTTPS 实测通过率 ≥50% + 延迟 <4s（实测可用率 100%）
curl "http://127.0.0.1:8088/get?n=10&anonymity=elite&https_rate=0.5&max_latency=4000"

# SOCKS5
curl "http://127.0.0.1:8088/get?protocol=socks5&n=10"

# 池子健康
curl "http://127.0.0.1:8088/health"
```

### 命令行

```bash
python -m proxypool serve              # 起 API + 调度器（推荐）
python -m proxypool run                # 只跑调度器，不起 HTTP
python -m proxypool run --rounds 3     # 跑 3 轮后退出
python -m proxypool fetch              # 只抓取源，看各源可用性
python -m proxypool check 1.2.3.4:8080 # 验证单个代理（自动猜协议）
python -m proxypool initdb             # 只建表
```

---

## 架构

```
┌──────────────┐   ┌──────────┐   ┌────────────┐   ┌─────────┐
│ 29 个免费源   │──▶│ Fetcher  │──▶│ Validator  │──▶│  Store  │
│ github / api │   │ 解析去重  │   │ 自建 socket │   │ 状态机   │
└──────────────┘   └──────────┘   └────────────┘   └────┬────┘
                                    硬超时 + 完整性检查    │
                        ┌──────────────┐                 ▼
                        │  Scheduler   │◀───────── SQLite / PG
                        │ 5min 精检热池 │
                        └──────┬───────┘
                               ▼
                        ┌──────────────┐
                        │   FastAPI    │──▶ /get 给爬虫
                        │  控制台页面    │
                        └──────────────┘
```

| 文件 | 职责 |
|---|---|
| `proxypool/config.py` | 配置加载与校验，启动即 fail-fast |
| `proxypool/models.py` | SQLAlchemy 模型：proxies / check_logs / round_stats / tombstones |
| `proxypool/fetcher.py` | 29 个源的抓取与统一解析 |
| `proxypool/validator.py` | **自建 raw-socket 探测引擎**（核心） |
| `proxypool/store.py` | 状态机、批量 upsert、挑选、清理 |
| `proxypool/scheduler.py` | 双节奏调度（热池精检 / 冷池扩充） |
| `proxypool/api.py` | HTTP API |
| `templates/dashboard.html` | 单页控制台（零 JS 依赖，图表手写 SVG） |
| `tests/` | 75 个测试（含 mock 服务端的协议级测试） |

---

## 核心设计（每条都有实测依据）

### 1. 探测引擎：为什么不用 requests / httpx

**实测**：httpx 经代理访问时对部分代理不遵守 timeout，400 条一批能挂死 5 分钟以上不返回，整轮调度被拖死。

自建 `asyncio.open_connection` + `asyncio.wait_for` 外层硬包裹整个流程，超时严格生效（测试 `test_hard_timeout_is_enforced` 验证 1.5s 配置在 3s 内必返回）。同时能拿到完整响应头，匿名性判定才准。

### 2. 两级探测

| 探测 | 目标 | 用途 | 实测通过率 |
|---|---|---|---|
| 明文 | `http://httpbin.org/get` | 存活 + origin/header 判匿名 | 单次 **11-18%** |
| HTTPS | `https://api.ipify.org`（CONNECT+TLS） | 能否跑 HTTPS | HTTP 代理仅 **16%** |

HTTPS 能力记**通过率**不记布尔（同一代理 TLS 通过率波动大，部分按 SNI 过滤），通过率 <1/3 自动撤销标记：

```json
{"supports_https": true, "https_success_rate": 0.67}
```

`socks5` 也必须实测（源的 socks5 列表混有伪装成 socks5 的 HTTP 代理，实测 8 个里只有 3 个真能跑 HTTPS）。`socks4` 不测（不支持域名解析，访问 443 天然失败）。

### 3. 「5 分钟验证」为什么要分层

这是整个系统最关键的决策，直接来自实测：

| 并发 | 吞吐 | 存活率 | 5 分钟能验 |
|---|---|---|---|
| 200 | 30.7/s | 5.9% | 8,603 |
| 400 | 50.0/s | 3.8% | 13,987 |
| 600 | 67.9/s | 2.2% | 19,018 |
| 800 | 83.1/s | **1.7%** | 23,280 |

**把并发推到 800 硬塞进 5 分钟，存活率会从 5.9% 掉到 1.7%** —— 高并发把可用代理挤成超时，等于误杀。

所以分两个节奏：

- **热池精检**（每 300s）：只复检 `alive + candidate`，实测 40-100 秒跑完，**100% 覆盖**
- **冷池扩充**（每 6 轮 = 30 分钟）：抓 29 个源、验证新候选

配置项：`scheduler.refresh_every_n_rounds`

### 4. 状态机

```
                   ┌─────────── 通过 ≥1 次 ───────────┐
                   │                                  ▼
 抓取 ──▶ new ──失败─▶ candidate ──失败 2 次──▶ dead ──失败 8 次──▶ banned
           │            │                        │                  │
           │            └──── 通过 ──────────────┘                  │
           └───────────── 通过 ────────────────────────────────▶ alive
```

| 参数 | 值 | 依据 |
|---|---|---|
| `promote_consecutive_ok` | 1 | 首次通过即入池。设 2 会导致首轮结束池子为空、总量被压到 2.5% |
| `kill_consecutive_fail` | 2 | 设 3 会让上万条准僵尸长期占满每轮预算 |
| `ban_consecutive_fail` | 8 | 反复失败的彻底拉黑 |
| `score_up` | 2 | 设 12 时任何代理成功 5 次就顶满 100，面板上评分全无区分度 |

### 5. 墓碑表：删除必须留记忆

purge 删掉 dead 后，源下一轮会把同一批代理原样吐回来，被当成「全新」重新入库、验证、判死、删除。

**实测一次抓取 22676 条里 13809 条是刚删的垃圾** —— 白烧带宽和 CPU 的死循环。

所以有 `tombstones` 表：删除前先写墓碑，`upsert_many` 按墓碑过滤（有效期 6 小时），过期自动清理允许重验。

### 6. 匿名性判定

不只看 header，用 httpbin 返回的 `origin` 链：

| 等级 | 判定 |
|---|---|
| `elite` 高匿 | 出口唯一且等于代理 IP，无泄漏头 |
| `anonymous` 匿名 | 出口是别的 IP 或有代理特征头，但没暴露真实 IP |
| `transparent` 透明 | origin 或 header 里出现**你的真实出口 IP** |

实测大量「高匿」其实是透明代理，origin 会返回 `你的IP,代理IP`。

### 7. 取用排序

**必须 `supports_https` 优先，其次 score，再按延迟。**

纯 score 排序会把「score 满分但只支持 HTTP」的代理排最前，用 HTTPS 的爬虫拿到 10 个全废（实测可用率只有 10%）。修后 80-100%。

---

## API

### 对外（给爬虫）

`GET /get`

| 参数 | 说明 | 示例 |
|---|---|---|
| `n` | 数量 1-100 | `10` |
| `protocol` | `http` / `https` / `socks4` / `socks5` | `socks5` |
| `anonymity` | `elite` / `anonymous` / `transparent` | `elite` |
| `https` | `1` 只要支持 HTTPS 的 | `1` |
| `https_rate` | **HTTPS 实测通过率下限 0-1（推荐）** | `0.5` |
| `max_latency` | 延迟上限 ms | `3000` |
| `min_score` | 最低评分 0-100 | `60` |
| `format` | `json` / `text` / `list` | `text` |

返回（`n=1` 时直接返回对象）：

```json
{
  "protocol": "socks5",
  "ip": "192.252.220.92",
  "port": 17328,
  "url": "socks5://192.252.220.92:17328",
  "anonymity": "elite",
  "supports_https": true,
  "https_success_rate": 0.92,
  "latency_ms": 1101,
  "score": 100,
  "last_ok_at": "2026-11-03T14:15:08"
}
```

`GET /get/{protocol}` — 纯文本，按协议取。
`GET /health` — `{"status":"ok","alive":864,"total":13815}`

### 对内（控制台用）

| 接口 | 说明 |
|---|---|
| `GET /api/stats` | 池子总览 |
| `GET /api/trend?limit=60` | 轮次趋势 + 状态分布 |
| `GET /api/latency` | 延迟直方图（8 个对数分桶） |
| `GET /api/proxies` | 分页列表 + 筛选 |
| `GET /api/rounds` | 最近轮次 |
| `GET /api/errors` | 近 6 小时失败原因聚合 |
| `POST /api/trigger` | 立即跑一轮 |
| `POST /api/proxies/delete` | 批量删除 |
| `GET /api/config` | 生效配置（脱敏） |

管理接口鉴权：`config.yaml` 的 `api.admin_token` 非空时需带 `X-Admin-Token` 头。

---

## 控制台

`http://<host>:8088/`

| 区块 | 内容 |
|---|---|
| KPI | 可用 / 总量 / 可用率 / 支持 HTTPS + 池子构成堆叠条 |
| 轮次趋势 | **双轴折线**：存活+晋升走左轴，验证量走右轴 |
| 延迟分布 | 8 档对数分桶 |
| 代理库存 | 分页表格 + 6 个筛选器 + 评分条 |
| 最近轮次 | 每轮 验/活/挂/晋/新 + 耗时 |
| 失败原因 | 近 6 小时 |
| 取用方式 | 按当前地址自动生成 curl |

图表手写 SVG，**零 JS 库**，不加载任何 CDN。

---

## 爬虫侧接入

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
    # HTTPS 目标必须带 https_rate，否则可能拿到纯 HTTP 代理
    proxies = pool.get(n=20, https_rate=0.5, max_latency=5000)

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

**免费代理必须实现「失败换一个」的重试**，不能假设长期可用。

---

## 配置

全部在 `config.yaml`，代码里不写死任何阈值。启动时校验必填项，缺配置直接失败。

关键段：

```yaml
validator:
  concurrency: 400        # 实测平衡点，推高会误杀
  timeout: 8.0            # 实测 p99 延迟 7.9s，低于 8 会误杀

policy:
  promote_consecutive_ok: 1   # 首次通过即入池
  kill_consecutive_fail: 2
  score_up: 2

scheduler:
  interval: 300                # 5 分钟
  refresh_every_n_rounds: 6    # 每 30 分钟扩充冷池
  candidate_batch: 12000
  round_budget: 280

cleanup:
  cold_pool_limit: 15000
  dead_purge_seconds: 60
  tombstone_seconds: 21600

api:
  host: "0.0.0.0"    # 局域网可访问
  port: 8088
```

完整注释见 [`config.yaml`](config.yaml)，每个值都写了标定依据。

---

## 部署

### systemd（推荐）

```bash
./deploy/manage.sh install   # 装服务 + 开机自启
./deploy/manage.sh status
./deploy/manage.sh logs
./deploy/manage.sh smoke     # 冒烟测试（真实取代理访问目标站）
./deploy/manage.sh restart
./deploy/manage.sh uninstall
```

**注意**：`LimitNOFILE` 必须 ≥ 4096。并发 400 时每探测占 2-3 个 fd，默认上限会报 EMFILE（表现为莫名的 OSError）。程序启动时也会自动提升，但 systemd 下建议显式配置。

### 换 PostgreSQL

```yaml
database:
  url: "postgresql+asyncpg://user:pass@127.0.0.1:5432/proxypool"
```

装 `asyncpg` 即可，其余不变。

---

## 测试

```bash
python -m pytest tests/ -q

# 只跑不依赖网络的单元测试
python -m pytest tests/test_parse.py tests/test_store.py tests/test_scheduler.py -q

# 并发标定（消耗真实网络，约 5 分钟）
python tests/bench_concurrency.py

# 端到端（从 API 取代理真实访问目标站）
python tests/test_e2e_live.py
```

覆盖：

- `test_parse.py`（20）：IP 校验、三种源格式解析、匿名性三级判定
- `test_store.py`（36）：状态机全路径、调度饥饿、HTTPS 通过率、墓碑、清理
- `test_validator.py`（9）：**本地 mock 服务端测协议**——HTTP 绝对 URI、SOCKS5/4 握手报文、硬超时、chunked
- `test_scheduler.py`（10）：轮次互斥、预算截断、任务泄漏

---

## 实测数据

本机 24 核 / 香港出网，2026-11 实测：

| 指标 | 数值 |
|---|---|
| 源数量 | 29 个（实测存活率后筛选，砍掉 0% 的） |
| 抓取规模 | 原始 70,000 条 → 去重 35,000 条 |
| 单次存活率 | 11-18% |
| 连续 3 轮都存活 | 23% |
| HTTP 代理支持 CONNECT | 16% |
| 延迟 p50 / p99 | 5.6s / 7.9s |
| 稳定后 alive | 700-900 条（其中 ~340 支持 HTTPS） |
| 默认取 10 个对 HTTP 目标 | **100% 可用** |
| `https_rate=0.5` 对 HTTPS 目标 | **100% 可用** |

---

## 已知限制

1. **免费代理本质不可靠**。单次存活率 11-18%，5 分钟后可能失效 70%。必须实现换代理重试。
2. **HTTPS 是稀缺资源**。HTTP 代理里只有 16% 支持 CONNECT；HTTP 代理即使标 `supports_https`，TLS 通道也不稳（实测 50% 失败，会按 SNI 过滤）——**需要稳定 HTTPS 请优先 SOCKS5，或必须带 `https_rate=0.5`**。
3. **匿名性判定依赖 httpbin.org**。该站不可用会退化到只看 header。可改 `validator.check_url_http` 为自建探测页。该站高并发下会返回 429/409，已做区分处理（不计入代理失败）。
4. **单机 SQLite 有写锁**。已启用 WAL + 定期 checkpoint + 自动 VACUUM；多实例部署请换 PostgreSQL。
5. **不处理需认证的代理（407）**。
6. **socks4 不做 HTTPS 探测**。不支持域名解析，`supports_https` 恒为 false。

---

## 致谢

设计思路参考了 [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool)（旧版即其改版）。

## License

[MIT](LICENSE)
