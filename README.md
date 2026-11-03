# ProxyPool

免费代理池。抓源、验证、入库、对外供爬虫取用，带控制台。

## 与旧版的关系

这是重写版，和 ≤v3.0.0 不兼容。

| | 旧版 | 新版 |
|---|---|---|
| Python | 2.7 / 3.5+ | 3.11+ |
| 存储 | Redis / Ssdb / MySQL | SQLite / PostgreSQL |
| 端口 | 5010 | 8088 |
| API | `/get` `/pop` `/all` `/count` `/delete` | `/get` 一个入口加参数 |

旧版代码在 tag `legacy-v3.0.0`：

```bash
git clone -b legacy-v3.0.0 https://github.com/Socks3389/proxy_pool.git
```

## 跑起来

```bash
git clone https://github.com/Socks3389/proxy_pool.git
cd proxy_pool
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m proxypool initdb
python -m proxypool serve
```

控制台 http://127.0.0.1:8088/ ，首轮大概 100 秒后池子里有货。

## 取代理

```bash
curl "http://127.0.0.1:8088/get?n=10"                  # 10 个
curl "http://127.0.0.1:8088/get?n=20&format=text"      # 纯 ip:port
curl "http://127.0.0.1:8088/get?protocol=socks5&n=10"  # SOCKS5
curl "http://127.0.0.1:8088/get?anonymity=elite&https_rate=0.5&max_latency=4000"
curl "http://127.0.0.1:8088/health"
```

参数：`n` `protocol` `anonymity` `https` `https_rate` `max_latency` `min_score` `allow_transparent` `format`。

跑 HTTPS 目标一定要带 `https_rate=0.5`。免费 HTTP 代理只有 16% 支持 CONNECT，不带这个参数会拿到一堆只能走 HTTP 的。

透明代理默认不返回 —— 它会把你的真实 IP 透给目标站，用了等于没挂。要的话传 `allow_transparent=1`。

返回：

```json
{
  "count": 1,
  "proxies": [{
    "protocol": "socks5", "ip": "1.2.3.4", "port": 1080,
    "url": "socks5://1.2.3.4:1080",
    "anonymity": "elite", "supports_https": true,
    "https_success_rate": 0.92, "latency_ms": 1101, "score": 100
  }]
}
```

取多少个都是这个结构，`n=1` 也是数组，客户端不用写两套分支。

## 爬虫里怎么用

```python
import random, httpx

def fetch(url):
    proxies = httpx.get("http://127.0.0.1:8088/get",
                        params={"n": 20, "https_rate": 0.5}).json()["proxies"]
    random.shuffle(proxies)
    bad = []
    for p in proxies:
        try:
            with httpx.Client(proxy=p["url"], timeout=15, verify=False) as c:
                r = c.get(url)
                if r.status_code == 200:
                    if bad:
                        httpx.post("http://127.0.0.1:8088/report",
                                   json={"proxies": bad, "reason": "timeout"})
                    return r.text
        except Exception:
            bad.append(f'{p["ip"]}:{p["port"]}')
    if bad:
        httpx.post("http://127.0.0.1:8088/report",
                   json={"proxies": bad, "reason": "all_failed"})
    raise RuntimeError("这批代理都不行")
```

免费代理会随时失效，必须换着重试，不能指望一个用到死。

`verify=False` 是必须的：实测大量免费代理在做 TLS 中间人（自签证书），校验证书会让请求全失败。**这也意味着别用它们传密码、token、支付信息。**

## 上报失效代理

```bash
curl -X POST http://127.0.0.1:8088/report \
  -H 'Content-Type: application/json' \
  -d '{"proxies": ["1.2.3.4:8080"], "reason": "timeout"}'
```

爬虫比定时轮询更早知道哪个代理挂了。上报后不直接判死，只把复检时间提前到当下 —— 下一轮验证器会重新验，验不过自然淘汰。这样上报错了也不会冤枉好代理。

一次最多 200 个，支持 IPv6 的 `[::1]:8080` 写法。

## 命令行

```bash
python -m proxypool serve            # API + 调度器
python -m proxypool run              # 只跑调度器
python -m proxypool run --rounds 3   # 跑 3 轮退出
python -m proxypool fetch            # 只抓源，看各源可用性
python -m proxypool check 1.2.3.4:8080
```

## 工作方式

29 个源并行抓，去重后进库当 `new`。验证通过一次升 `alive`，连续失败 2 次判 `dead`，8 次拉黑。清理时先写墓碑再从库删，避免下一轮被源重新抓回来反复验。

调度分两个节奏：

- 每 5 分钟复检 `alive` + `candidate`（热池，约 1500 条，几十秒跑完）
- 每 6 轮（30 分钟）抓一次源、验新代理（冷池）

之所以分层：全量 3.5 万条用并发 400 要 9 分钟，塞不进 5 分钟；推到并发 800 能塞进去，但存活率从 5.9% 掉到 1.7%，等于把好代理判死。

验证器是自己写的 raw socket，没用 requests/httpx —— 实测 httpx 对部分代理不遵守 timeout，400 条能卡 5 分钟，整轮调度被拖死。

探测分两级：明文（`httpbin.org/get`）判存活和匿名性，HTTPS（CONNECT + TLS 实测）判能否跑 HTTPS 目标。HTTPS 探测会校验响应里有没有回显探测串 —— 有代理 CONNECT 任何目标都返回自己的 IP，只看状态码会被骗过去。

## 配置

全在 `config.yaml`，改完重启生效。常用的几个：

```yaml
validator:
  concurrency: 400     # 400 是实测平衡点，调高会误杀
  timeout: 8.0         # 存活代理延迟 p99 是 7.9s，低于 8 会误判

scheduler:
  interval: 300              # 热池复检周期
  refresh_every_n_rounds: 6  # 每 6 轮抓一次源

api:
  host: "0.0.0.0"      # 要局域网访问就这个，只本机用改 127.0.0.1
  port: 8088
  admin_token: ""      # 有公网 IP 时填上，不然谁都能删数据

cleanup:
  cold_pool_limit: 15000
```

数据库加新列会自动迁移，不用手工改表。

## 管理接口

| 接口 | 用途 |
|---|---|
| `/api/stats` | 池子总览、状态分布、协议分布 |
| `/api/usage` | 取用统计：被取走多少次、爬虫上报失效多少次 |
| `/api/proxies` | 分页列表，支持筛选排序 |
| `/api/rounds` | 最近轮次记录 |
| `/api/errors` | 失败原因聚合 |
| `/api/trend` | 轮次趋势（控制台画图用） |
| `/api/latency` | 延迟分布直方图 |
| `/api/config` | 当前配置（脱敏） |
| `POST /api/trigger` | 立即跑一轮 |
| `POST /api/proxies/delete` | 批量删除 |

配了 `admin_token` 的话，管理接口要带 `X-Admin-Token` 头。

## 部署

```bash
./deploy/manage.sh install   # 装 systemd 服务
./deploy/manage.sh status
./deploy/manage.sh logs
./deploy/manage.sh smoke     # 冒烟测试
```

换 PostgreSQL 改 `database.url` 为 `postgresql+asyncpg://...`，装 `asyncpg` 就行。

## 测试

```bash
python -m pytest tests/ -q
python tests/bench_concurrency.py   # 并发标定，约 5 分钟
python tests/test_e2e_live.py       # 端到端
```

121 个测试。其中 `test_validator.py` 是起本地 mock 服务端测协议实现（SOCKS5/SOCKS4 握手、硬超时、chunked、状态码分类），不依赖外网。

## 实测数据

24 核机器、香港出网：

- 29 个源，原始 7 万条，去重 3.5 万
- 单次存活率 11-18%，连续 3 轮都活的只有 23%
- 稳定后 alive 400-900 条，其中约六成支持 HTTPS
- 默认取 10 个打 HTTP 目标：10/10
- `https_rate=0.5` 打 HTTPS 目标：8/10

## 限制

1. 免费代理单次存活率 11-18%，5 分钟后可能失效七成。这是免费源本身的情况，换实现也改善不了。
2. HTTPS 是稀缺资源。HTTP 代理即使标了 `supports_https`，TLS 通道也不稳（会按 SNI 过滤），要稳定就优先用 SOCKS5。
3. 免费代理会解密 TLS。实测大量代理返回自签证书（中间人），别拿它传任何敏感数据。
4. 匿名性判定靠 httpbin.org 的 origin，那个站挂了会退化成只看 header。可以改成自建探测页。
5. SQLite 有写锁，已经开了 WAL 和自动 VACUUM，但要多实例就换 PG。
6. 需要认证的代理（407）直接判失败，没做账号密码。
7. socks4 不做 HTTPS 探测，它不支持域名解析，访问 443 天然失败。

## License

MIT
