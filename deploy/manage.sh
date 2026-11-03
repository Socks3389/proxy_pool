#!/usr/bin/env bash
# ProxyPool 一键部署 / 运维脚本
#
# 用法（在项目根目录或任意位置执行均可）：
#   ./deploy/manage.sh install    安装并启动 systemd 服务
#   ./deploy/manage.sh uninstall  停止并移除服务
#   ./deploy/manage.sh status     查看状态
#   ./deploy/manage.sh logs       跟随日志
#   ./deploy/manage.sh restart    重启
#   ./deploy/manage.sh smoke      冒烟测试（真实取代理访问目标站）
#
# 路径自动探测，不需要手改。若要用非默认路径：
#   APP_DIR=/opt/proxypool ./deploy/manage.sh install
set -euo pipefail

# ── 自动定位项目根目录（本脚本在 <root>/deploy/ 下）──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SERVICE="proxypool"
PY="$APP_DIR/.venv/bin/python"
RUN_USER="${RUN_USER:-$(id -un)}"
RUN_GROUP="${RUN_GROUP:-$(id -gn)}"
UNIT_SRC="$APP_DIR/deploy/proxypool.service"
UNIT_DST="/etc/systemd/system/$SERVICE.service"

cd "$APP_DIR"

# 统一的干净环境：外部 PYTHONPATH 会污染 venv（实测会加载到别的项目的包）
export PYTHONNOUSERSITE=1
export PYTHONUNBUFFERED=1
unset PYTHONPATH || true

die() { echo "[!] $*" >&2; exit 1; }
ok()  { echo "[+] $*"; }

need_root() {
    if [[ $EUID -ne 0 ]]; then
        command -v sudo >/dev/null || die "需要 root 权限，且未安装 sudo"
    fi
}
as_root() {
    if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

check_python() {
    command -v python3 >/dev/null || die "未找到 python3"
    local ver
    ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local maj min
    maj=${ver%.*}; min=${ver#*.}
    if (( maj < 3 || (maj == 3 && min < 11) )); then
        die "需要 Python 3.11+，当前是 $ver"
    fi
    echo "    Python $ver"
}

check_venv() {
    if [[ ! -x "$PY" ]]; then
        echo "[*] venv 不存在，正在创建..."
        check_python
        python3 -m venv .venv
        ./.venv/bin/pip install -q --upgrade pip
        ./.venv/bin/pip install -q -r requirements.txt
        ok "venv 就绪"
    fi
    # 校验关键依赖真的能 import（venv 建好但装坏的情况真实发生过）
    if ! "$PY" -c "import fastapi, httpx, sqlalchemy, aiosqlite" 2>/dev/null; then
        echo "[*] 依赖不完整，重新安装..."
        ./.venv/bin/pip install -q -r requirements.txt
    fi
}

cmd_install() {
    need_root
    echo "=== 安装 ProxyPool ==="
    echo "    项目目录: $APP_DIR"
    echo "    运行用户: $RUN_USER:$RUN_GROUP"
    echo

    check_venv

    echo "[1/4] 初始化数据库"
    "$PY" -m proxypool initdb

    echo "[2/4] 生成 systemd 单元"
    [[ -f "$UNIT_SRC" ]] || die "找不到 $UNIT_SRC"

    # ProtectHome 处理：项目在家目录下时必须关掉，否则服务读不到自己的文件
    local protect_home="read-only"
    if [[ "$APP_DIR" == /home/* || "$APP_DIR" == /root* ]]; then
        protect_home="false"
        echo "    (项目在家目录下，已关闭 ProtectHome)"
    fi

    local tmp
    tmp=$(mktemp)
    sed -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|__RUN_USER__|$RUN_USER|g" \
        -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
        -e "s|__PROTECT_HOME__|$protect_home|g" \
        "$UNIT_SRC" > "$tmp"
    as_root cp "$tmp" "$UNIT_DST"
    rm -f "$tmp"
    as_root chmod 644 "$UNIT_DST"

    echo "[3/4] 启用并启动服务"
    as_root systemctl daemon-reload
    as_root systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    as_root systemctl restart "$SERVICE"

    echo "[4/4] 等待启动"
    local host port
    host=$("$PY" - <<'EOF'
from proxypool.config import load_config
c = load_config()
print("127.0.0.1" if c.api.host in ("0.0.0.0", "::") else c.api.host)
EOF
)
    port=$("$PY" - <<'EOF'
from proxypool.config import load_config
print(load_config().api.port)
EOF
)

    for _ in $(seq 1 20); do
        if curl -sf -m 2 "http://$host:$port/health" >/dev/null 2>&1; then
            ok "服务已就绪"
            curl -s "http://$host:$port/health"; echo
            echo "    控制台: http://$host:$port/"
            echo "    API:    http://$host:$port/get?n=10"
            return 0
        fi
        sleep 2
    done
    echo "[!] 启动超时，查看日志：journalctl -u $SERVICE -n 50 --no-pager"
    return 1
}

cmd_uninstall() {
    need_root
    as_root systemctl stop "$SERVICE" 2>/dev/null || true
    as_root systemctl disable "$SERVICE" 2>/dev/null || true
    as_root rm -f "$UNIT_DST"
    as_root systemctl daemon-reload
    ok "服务已移除（数据保留在 $APP_DIR/data）"
}

api_url() {
    "$PY" - <<'EOF'
from proxypool.config import load_config
c = load_config()
host = "127.0.0.1" if c.api.host in ("0.0.0.0", "::") else c.api.host
print(f"http://{host}:{c.api.port}")
EOF
}

cmd_status() {
    systemctl status "$SERVICE" --no-pager -l 2>/dev/null || echo "(服务未安装)"
    echo "--- 接口 ---"
    curl -s -m 5 "$(api_url)/health" || echo "无响应"
    echo
}

cmd_logs() {
    as_root journalctl -u "$SERVICE" -f -n 100
}

cmd_restart() {
    need_root
    as_root systemctl restart "$SERVICE"
    sleep 3
    cmd_status
}

cmd_smoke() {
    check_venv
    local base
    base=$(api_url)

    echo "=== 1. 健康检查 ($base) ==="
    curl -sf -m 10 "$base/health" | "$PY" -m json.tool || {
        echo "[!] 服务未运行"; return 1; }

    echo
    echo "=== 2. 取 3 个代理 ==="
    local out
    out=$(curl -sf -m 10 "$base/get?n=3" || echo "")
    if [[ -z "$out" ]]; then
        echo "[!] 池中暂无可用代理（等首轮跑完，约 100 秒）"
        return 1
    fi
    echo "$out" | "$PY" -m json.tool

    echo
    echo "=== 3. 真实访问目标站验证 ==="
    "$PY" - "$base" <<'EOF'
import json, sys, urllib.request
base = sys.argv[1]
pool = json.loads(urllib.request.urlopen(f"{base}/get?n=5", timeout=10).read())
items = pool.get("proxies", [pool])
ok = 0
for p in items:
    try:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p["url"], "https": p["url"]}))
        body = op.open("http://httpbin.org/ip", timeout=12).read().decode()
        print(f"  OK   {p['url']:<30} {body.strip()[:60]}")
        ok += 1
    except Exception as e:
        print(f"  FAIL {p['url']:<30} {type(e).__name__}: {str(e)[:40]}")
print(f"\n可用 {ok}/{len(items)}")
EOF
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    restart)   cmd_restart ;;
    smoke)     cmd_smoke ;;
    *)
        echo "用法: $0 {install|uninstall|status|logs|restart|smoke}"
        echo
        echo "  install    安装并启动 systemd 服务（需 sudo）"
        echo "  uninstall  停止并移除服务（保留数据）"
        echo "  status     查看服务状态与健康检查"
        echo "  logs       实时日志"
        echo "  restart    重启服务"
        echo "  smoke      冒烟测试：真实取代理访问目标站"
        exit 1
        ;;
esac
