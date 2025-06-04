ProxyPool 爬虫代理IP池（优化版）
================================
## 征集免费代理IP池，让我们将项目持续性更新优化下去！

### 投稿邮箱：socks@kmail.xin

* 示例：（网站名称） - （网址）如：站大爷 - https://www.zdaye.com/free/

## 该项目我已增加爬虫提取代码，将IP池爬取到的IP:Prot保存到本地以方便使用

## 更新内容：修复检查IP地理位置及ISP信息，增加爬虫提取代码（请看最下方）

## 预更新：删除部分在国内机器上无法爬取的IP池，更换新的IP池。

* 代理IP地址信息检查可自行搭建API（目前已通过纯真CZ88给出的社区IP库使用Golang编写API服务，将在测试完成之后推出开源，并将下次更新使用）

### 运行项目

该项目支持 Python 版本
[![](https://img.shields.io/badge/Python-2.7-green.svg)](https://docs.python.org/2.7/)
[![](https://img.shields.io/badge/Python-3.5-blue.svg)](https://docs.python.org/3.5/)
[![](https://img.shields.io/badge/Python-3.6-blue.svg)](https://docs.python.org/3.6/)
[![](https://img.shields.io/badge/Python-3.7-blue.svg)](https://docs.python.org/3.7/)
[![](https://img.shields.io/badge/Python-3.8-blue.svg)](https://docs.python.org/3.8/)
[![](https://img.shields.io/badge/Python-3.9-blue.svg)](https://docs.python.org/3.9/)
[![](https://img.shields.io/badge/Python-3.10-blue.svg)](https://docs.python.org/3.10/)
[![](https://img.shields.io/badge/Python-3.11-blue.svg)](https://docs.python.org/3.11/)

##### 下载代码:

* Git 源码

```bash
git clone https://github.com/Socks3389/proxy_pool.git
```

```bash
https://github.com/Socks3389/proxy_pool/releases 下载对应zip文件
```

##### 安装依赖:

```bash
pip install -r requirements.txt
```

##### 后台运行

* 在项目根目录下执行 nohup 命令（重启需手动再次启动）

```bash
nohup python proxyPool.py schedule >> schedule.log 2>&1 &
```
```bash
nohup python proxyPool.py server >> server.log 2>&1 &
```
* 也可以使用 supervisor 进程守护（重启自启动）

##### 安装 Redis 数据库 (Windows)

自行下载 Windows Redis 版本
```bash
https://github.com/redis-windows/redis-windows
```

##### 安装 Redis 数据库 (Linux)

自行百度安装教程，实在不行就用宝塔面板！

##### 修改项目 setting.py 

```python
# setting.py 为项目配置文件

# 配置API服务

HOST = "0.0.0.0"               # IP（内网可填写127.0.0.1 外网可填写0.0.0.0）
PORT = 5010                    # 监听端口


# 配置数据库

DB_CONN = 'redis://:pwd@127.0.0.1:6379/0'               # redis://:密码@127.0.0.1:6379/0


# 配置 ProxyFetcher

PROXY_FETCHER = [
    "freeProxy01",      # 这里是启用的代理抓取方法名，所有fetch方法位于fetcher/proxyFetcher.py
    "freeProxy02",
    # ....
]
```

### 使用

* Api

启动web服务后, 默认配置下会开启 http://127.0.0.1:5010 的api接口服务:

| api | method | Description | params|
| ----| ---- | ---- | ----|
| / | GET | api介绍 | None |
| /get | GET | 随机获取一个代理| 可选参数: `?type=https` 过滤支持https的代理|
| /pop | GET | 获取并删除一个代理| 可选参数: `?type=https` 过滤支持https的代理|
| /all | GET | 获取所有代理 |可选参数: `?type=https` 过滤支持https的代理|
| /count | GET | 查看代理数量 |None|
| /delete | GET | 删除代理  |`?proxy=host:ip`|

### 爬虫提取示例（将WebApi中proxy值提取到ip.txt中）
#### 将目标网址修改成自己的

```python
import requests

# 目标网址
url = "http://网址/all"

# 设置请求头模拟浏览器访问
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
}

try:
    # 发送GET请求
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # 检查请求是否成功

    # 解析JSON数据
    proxy_data = response.json()
    
    # 提取所有proxy字段的值
    proxies = [item["proxy"] for item in proxy_data]

    # 写入文件
    with open("ip.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(proxies))
    
    print(f"成功获取 {len(proxies)} 个代理IP，已保存至ip.txt")

except requests.exceptions.RequestException as e:
    print(f"请求失败: {e}")
except KeyError:
    print("响应数据结构不符合预期，请检查网站内容")
except Exception as e:
    print(f"发生未知错误: {e}")
```
