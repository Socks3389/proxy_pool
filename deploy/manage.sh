#!/usr/bin/env bash
# ProxyPool 一键部署 / 运维脚本
# 用法:
#   ./deploy/manage.sh install    安装并启动 systemd 服务
#   ./deploy/manage.sh uninstall  停止并移除服务
#   ./deploy/manage.sh status     查看状态
#   ./deploy/manage.sh logs       跟随日志
#   ./deploy/manage.sh restart    重启
#   ./deploy/manage.sh smoke      冒烟测试（真实取代理）
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="proxypool"
PY="$APP_DIR/.venv/bin/python"

cd "$APP_DIR"

# 统一的干净环境：外部 PYTHONPATH 会污染 venv
export PYTHONNOUSERSITE=1
export PYTHONUNBUFFERED=1
unset PYTHONPATH || true

check_venv() {
    if [[ ! -x "$PY" ]]; then
        echo "[!] venv 不存在，正在创建..."
        /usr/bin/python3 -m venv .venv
        ./.venv/bin/pip install -q --upgrade pip
        ./.venv/bin/pip install -q -r requirements.txt
        echo "[+] venv 就绪"
    fi
}

cmd_install() {
    check_venv
    echo "[1/4] 初始化数据库"
    "$PY" -m proxypool initdb

    echo "[2/4] 安装 systemd 单元"
    # 单元文件是模板，按当前环境填路径和运行用户。
    # 写死路径的单元换台机器就起不来。
    local run_user run_group unit
    run_user="$(id -un)"
    run_group="$(id -gn)"
    unit="$(mktemp)"
    sed -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|__USER__|$run_user|g" \
        -e "s|__GROUP__|$run_group|g" \
        deploy/proxypool.service > "$unit"
    sudo cp "$unit" /etc/systemd/system/"$SERVICE".service
    rm -f "$unit"
    sudo systemctl daemon-reload

    echo "[3/4] 启用并启动服务"
    sudo systemctl enable "$SERVICE" >/dev/null
    sudo systemctl restart "$SERVICE"

    echo "[4/4] 等待启动"
    local port
    port="$(sed -n 's/^ *port: *\([0-9]*\).*/\1/p' config.yaml | head -1)"
    port="${port:-8088}"
    for _ in $(seq 1 20); do
        if curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            echo "[+] 服务已就绪"
            curl -s "http://127.0.0.1:$port/health"
            echo
            echo "    控制台: http://$(hostname -I | awk '{print $1}'):$port/"
            return 0
        fi
        sleep 2
    done
    echo "[!] 启动超时，查看日志: journalctl -u $SERVICE -n 50"
    return 1
}

cmd_uninstall() {
    sudo systemctl stop "$SERVICE" 2>/dev/null || true
    sudo systemctl disable "$SERVICE" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$SERVICE.service"
    sudo systemctl daemon-reload
    echo "[+] 服务已移除（数据保留在 $APP_DIR/data）"
}

cmd_status() {
    systemctl status "$SERVICE" --no-pager -l || true
    echo "--- 接口 ---"
    curl -s -m 5 http://127.0.0.1:8088/health || echo "接口无响应"
    echo
}

cmd_logs() {
    sudo journalctl -u "$SERVICE" -f -n 100
}

cmd_restart() {
    sudo systemctl restart "$SERVICE"
    sleep 3
    cmd_status
}

cmd_smoke() {
    check_venv
    echo "=== 1. 健康检查 ==="
    curl -sf -m 10 http://127.0.0.1:8088/health | "$PY" -m json.tool || {
        echo "[!] 服务未运行"; return 1; }

    echo
    echo "=== 2. 取 3 个代理 ==="
    local out
    out=$(curl -sf -m 10 "http://127.0.0.1:8088/get?n=3" || echo "")
    if [[ -z "$out" ]]; then
        echo "[!] 池中暂无可用代理（等首轮跑完，约 100 秒）"
        return 1
    fi
    echo "$out" | "$PY" -m json.tool

    echo
    echo "=== 3. 真实访问目标站验证 ==="
    "$PY" - <<'EOF'
import json, urllib.request
pool = json.loads(urllib.request.urlopen(
    "http://127.0.0.1:8088/get?n=5", timeout=10).read())
items = pool.get("proxies", [pool])
ok = 0
for p in items:
    try:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p["url"], "https": p["url"]}))
        body = op.open("http://httpbin.org/ip", timeout=12).read().decode()
        print(f"  OK   {p['url']:<28} {body.strip()[:60]}")
        ok += 1
    except Exception as e:
        print(f"  FAIL {p['url']:<28} {type(e).__name__}: {str(e)[:40]}")
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
        exit 1
        ;;
esac
