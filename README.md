ProxyPool 爬虫代理IP池（优化版）
================================
# 该项目由 JHao104 开发
# 我只是进行了相关部分优化，会持续的对该项目进行优化
# 官方Github地址：https://github.com/jhao104/proxy_pool/

# 该项目我已增加爬虫代码，将IP池爬取到的IP:Prot保存到本地以方便使用，详细代码在最下面

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

##### 安装 supervisor 进程守护

* Debian/Ubuntu类
```bash
sudo apt install -y supervisor
```

* RedHat/CentOS类
```bash
yum install -y supervisor
```

##### 创建 schedule 配置文件

```bash
vim /etc/supervisor/conf.d/proxypool_schedule.conf
```

```bash
[program:proxypool-schedule]
command=python /www/wwwroot/Proxy_Pool/proxyPool.py schedule
directory=/www/wwwroot/Proxy_Pool
autostart=true
autorestart=true
startretries=3
user=root
redirect_stderr=true
stdout_logfile=/www/wwwroot/Proxy_Pool/log/schedule.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
```

##### 创建 server 配置文件

```bash
vim /etc/supervisor/conf.d/proxypool_server.conf
```

```bash
[program:proxypool-server]
command=python /www/wwwroot/Proxy_Pool/proxyPool.py server
directory=/www/wwwroot/Proxy_Pool
autostart=true
autorestart=true
startretries=3
user=root
redirect_stderr=true
stdout_logfile=/www/wwwroot/Proxy_Pool/log/server.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
```

##### 查看 supervisor 服务

```bash
supervisorctl status
```

##### supervisor 相关命令

```bash
supervisorctl status                   #查看服务进程

supervisorctl stop 服务器名称           #停止服务

supervisorctl start 服务器名称          #启动服务

supervisorctl restart 服务器名称        #重启服务
```

##### 安装 Redis 数据库 (Windows)

自行下载 Windows Redis 版本
https://github.com/redis-windows/redis-windows

测试使用的 7.4.3

##### 安装 Redis 数据库 (Linux)

* Debian/Ubuntu类
```bash
sudo apt install -y redis
```

* RedHat/CentOS类
```bash
yum install -y redis
```

##### Docker 安装 Redis 服务
* Pull Redis 镜像
```bash
docker pull redis
```
* 检查镜像
```bash
docker images
```
* 创建Redis配置文件
启动前需要先创建Redis外部挂载的配置文件 （ /home/redis/conf/redis.conf ）
之所以要先创建 , 是因为Redis本身容器只存在 /etc/redis 目录 , 本身就不创建 redis.conf 文件
当服务器和容器都不存在 redis.conf 文件时, 执行启动命令的时候 docker 会将 redis.conf 作为目录创建 , 这并不是我们想要的结果
```bash
## 创建目录
mkdir -p /home/redis/conf
## 创建文件
touch /home/redis/conf/redis.conf
```

##### 创建Redis容器并启动
```bash
docker run \
-d \
--name redis \
-p 6379:6379 \
--restart unless-stopped \
-v /home/redis/data:/data \
-v /home/redis/conf/redis.conf:/etc/redis/redis.conf \
redis-server /etc/redis/redis.conf \
redis:bullseye 
 ```


##### 修改项目 setting.py 

```python
# setting.py 为项目配置文件

# 配置API服务

HOST = "0.0.0.0"               # IP（内网可填写127.0.0.1 外网可填写0.0.0.0）
PORT = 5000                    # 监听端口


# 配置数据库

DB_CONN = 'redis://:pwd@127.0.0.1:6379/0'               # redis://:密码@127.0.0.1:8888/0


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

### 爬虫提取（将爬取到的IP:Prot保存下来）

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
