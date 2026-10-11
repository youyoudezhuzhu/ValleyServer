#!/bin/bash
# 沙箱模拟安装：在临时目录里完整跑一遍生命周期脚本，把装机期问题提前抓出来。
#
# ⚠️ 安全护栏：
#   1. 所有 TRIM_* 都指向临时沙箱，**绝不触碰**真实应用目录。脚本开头会断言这一点。
#   2. 健康检查端口不硬编码，默认 18842（避免打到真实运行中的应用）。
#
# 用法： tools/simulate-install.sh <path-to-fpk> [port]

set -uo pipefail

FPK="${1:-}"
PORT="${2:-18842}"

if [ -z "$FPK" ] || [ ! -f "$FPK" ]; then
    echo "用法： tools/simulate-install.sh <app.fpk> [port]" >&2
    exit 1
fi
case "$PORT" in
    ''|*[!0-9]*) echo "端口非法：$PORT" >&2; exit 1 ;;
esac
if [ "$PORT" = "18242" ] || [ "$PORT" = "18801" ]; then
    echo "拒绝使用 $PORT：这是真实应用/其它应用的端口，会得到假结论。" >&2
    exit 1
fi

SANDBOX="$(mktemp -d /tmp/vs-sim.XXXXXX)"
APPNAME="$(tar xzOf "$FPK" manifest | grep -E '^appname' | head -1 | cut -d= -f2 | tr -d ' "')"
APPVER="$(tar xzOf "$FPK" manifest | grep -E '^version' | head -1 | cut -d= -f2 | tr -d ' "')"
[ -z "$APPNAME" ] && { echo "无法从 fpk 读取 appname" >&2; exit 1; }

export TRIM_APPNAME="$APPNAME"
export TRIM_APPVER="$APPVER"
export TRIM_APPDEST="$SANDBOX/appcenter/$APPNAME"
export TRIM_PKGVAR="$SANDBOX/appdata/$APPNAME"
export TRIM_PKGHOME="$SANDBOX/apphome/$APPNAME"
export TRIM_PKGETC="$SANDBOX/appconf/$APPNAME"
export TRIM_PKGTMP="$SANDBOX/apptemp/$APPNAME"
export TRIM_PKGMETA="$SANDBOX/appmeta/$APPNAME"
export TRIM_TEMP_LOGFILE="$SANDBOX/install.log"
export TRIM_SERVICE_PORT="$PORT"
export TRIM_USERNAME="$APPNAME"
export TRIM_GROUPNAME="$APPNAME"
export TRIM_RUN_USERNAME="root"
export TRIM_UID=0
export TRIM_GID=0
export TRIM_SYS_VERSION="1.2.0701"
export TRIM_SYS_ARCH="x86"
# 模拟 fnOS 真实注入的 HOME=/root（这正是必须被脚本覆盖掉的坑）
export HOME="/root"
# 确认 DATA_LIBRARY_PATH 未注入（与 fnOS 实际行为一致）
unset DATA_LIBRARY_PATH

# ── 护栏：断言所有路径都在沙箱内 ──
for v in TRIM_APPDEST TRIM_PKGVAR TRIM_PKGHOME TRIM_PKGETC TRIM_PKGTMP TRIM_PKGMETA TRIM_TEMP_LOGFILE; do
    val="${!v}"
    case "$val" in
        "$SANDBOX"/*) : ;;
        *) echo "❌ 安全断言失败：$v=$val 不在沙箱 $SANDBOX 内，拒绝继续" >&2; exit 1 ;;
    esac
done

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

cleanup() {
    # 停模拟实例时必须带同样的 TRIM_* 环境变量，否则会去错目录找 PID
    if [ -x "$TRIM_APPDEST/cmd/main" ]; then
        "$TRIM_APPDEST/cmd/main" stop >/dev/null 2>&1 || true
    fi
    pkill -f "$TRIM_APPDEST/server/run.py" >/dev/null 2>&1 || true
    rm -rf "$SANDBOX"
}
trap cleanup EXIT

echo "=============================================="
echo " 沙箱模拟安装：$APPNAME $APPVER"
echo " 沙箱：$SANDBOX"
echo " 端口：$PORT"
echo "=============================================="

# ── 1) 解包 ──
mkdir -p "$TRIM_APPDEST"
tar xzf "$FPK" -C "$TRIM_APPDEST"
# 注意 fnOS 的布局：app.tgz 的根就是 server/ www/ ui/，
# 解包目标必须是 $TRIM_APPDEST 本身（不是它下面的 app/）。
tar xzOf "$FPK" app.tgz | tar xzf - -C "$TRIM_APPDEST"
# cmd 与 config 在同一层；install_callback 里用 $APP/../cmd 访问，这里建立等价视图
[ -d "$TRIM_APPDEST/../cmd" ] || cp -a "$TRIM_APPDEST/cmd" "$TRIM_APPDEST/../cmd" 2>/dev/null || true
echo "--> 已解包到 $TRIM_APPDEST"

echo
echo "[1/9] cmd/* 执行位"
check "cmd/main 可执行"       '[ -x "$TRIM_APPDEST/cmd/main" ]'
check "cmd 脚本数量 = 9"      '[ "$(ls "$TRIM_APPDEST/cmd" | wc -l)" -eq 9 ]'

echo
echo "[2/9] install_init（前置校验）"
if "$TRIM_APPDEST/cmd/install_init"; then ok "install_init 退出码 0"; else bad "install_init 失败：$(cat "$TRIM_TEMP_LOGFILE" 2>/dev/null | tail -3)"; fi

echo
echo "[3/9] install_callback（幂等性：跑两次）"
if "$TRIM_APPDEST/cmd/install_callback"; then ok "第 1 次 install_callback 成功"; else bad "第 1 次 install_callback 失败：$(tail -5 "$TRIM_TEMP_LOGFILE" 2>/dev/null)"; fi
if "$TRIM_APPDEST/cmd/install_callback"; then ok "第 2 次 install_callback 成功（幂等）"; else bad "第 2 次 install_callback 失败（不幂等）"; fi
check "数据目录已创建"        '[ -d "$TRIM_PKGHOME/data/Saves" ] && [ -d "$TRIM_PKGHOME/data/Mods" ] && [ -d "$TRIM_PKGHOME/data/snapshots" ]'
check "管理器配置已生成"      '[ -f "$TRIM_PKGHOME/manager/config.json" ]'
check "配置权限为 600"        '[ "$(stat -c %a "$TRIM_PKGHOME/manager/config.json")" = "600" ]'
check "任务数据库已生成"      '[ -f "$TRIM_PKGHOME/manager/tasks.db" ]'
check "初始口令文件已生成"    '[ -f "$TRIM_PKGHOME/initial-password.txt" ]'

echo
echo "[4/9] cmd/main status（未启动应为 3）"
"$TRIM_APPDEST/cmd/main" status; RC=$?
[ "$RC" = "3" ] && ok "未启动时 status=3" || bad "未启动时 status=$RC（应为 3）"

echo
echo "[5/9] cmd/main start"
if "$TRIM_APPDEST/cmd/main" start; then ok "start 返回 0"; else bad "start 失败：$(tail -5 "$TRIM_PKGVAR/logs/manager.log" 2>/dev/null)"; fi
"$TRIM_APPDEST/cmd/main" status; RC=$?
[ "$RC" = "0" ] && ok "启动后 status=0" || bad "启动后 status=$RC（应为 0）"

echo
echo "[6/9] 健康检查 /api/health"
HEALTH="$(curl -sS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
echo "     响应：${HEALTH:0:220}"
check "健康检查返回 ok"        'echo "$HEALTH" | grep -q "\"ok\": *true"'
check "健康检查报告版本号"     'echo "$HEALTH" | grep -q "$APPVER"'
check "HOME 已被覆盖为应用家目录" 'echo "$HEALTH" | grep -q "apphome"'
check "DATA_LIBRARY_PATH 已兜底" 'echo "$HEALTH" | grep -q "\"data_library_path\": *\"[^\"]"'

echo
echo "[7/9] 前端资源与网关前缀"
check "首页 200"               'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" | grep -q 200'
check "静态 JS 200"            'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/static/app.js" | grep -q 200'
check "静态 CSS 200"           'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/static/style.css" | grep -q 200'

# 升级后绝不能继续加载旧界面：index.html 的资源引用必须带版本号（缓存破坏），
# 且 index.html 本身不可被缓存。
check "首页 JS/CSS 引用带版本号（缓存破坏）" \
    'curl -sS "http://127.0.0.1:$PORT/" | grep -qE "static/app\.js\?v=[0-9]+\.[0-9]+\.[0-9]+" && curl -sS "http://127.0.0.1:$PORT/" | grep -qE "static/style\.css\?v=[0-9]+\.[0-9]+\.[0-9]+"'
check "首页不含未替换的版本占位符" \
    '! curl -sS "http://127.0.0.1:$PORT/" | grep -q "__APP_VERSION__"'
check "首页禁止缓存（no-store）" \
    'curl -sSI "http://127.0.0.1:$PORT/" | grep -qi "cache-control: *no-store"'
check "带版本号的 JS 可正常获取" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/static/app.js?v=1.2.3" | grep -q 200'

# 认证边界：TCP 上伪造网关头不得免密登录（曾经的认证绕过）
check "TCP 伪造 X-Trim-Userid 被拒（401）" \
    'curl -sS -o /dev/null -w "%{http_code}" -H "X-Trim-Userid: 1" "http://127.0.0.1:$PORT/api/dashboard" | grep -q 401'
check "TCP 伪造网关头不泄露数据" \
    '! curl -sS -H "X-Trim-Userid: 1" -H "X-Trim-Isadmin: true" "http://127.0.0.1:$PORT/api/config" | grep -q "display_number"'
check "无前导斜杠的路径同样被鉴权拦截" \
    'printf "GET api/dashboard HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n" | timeout 5 python3 -c "
import socket,sys
s=socket.create_connection((\"127.0.0.1\",$PORT),timeout=5); s.sendall(sys.stdin.buffer.read())
d=b\"\"
while True:
    c=s.recv(65536)
    if not c: break
    d+=c
sys.stdout.write(d.decode(\"utf-8\",\"replace\"))" | head -1 | grep -q 401'
check "网关前缀 /app/$APPNAME/ 首页 200" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/app/$APPNAME/" | grep -q 200'
check "网关前缀下静态资源 200" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/app/$APPNAME/static/app.js" | grep -q 200'
check "裸前缀返回 307" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/app/$APPNAME" | grep -q 307'

echo
echo "[8/9] 认证与接口"
check "未登录访问 /api/dashboard 返回 401" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/dashboard" | grep -q 401'
check "未登录访问 /api/health 返回 200" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/health" | grep -q 200'
PW="$(sed -n 's/^口令：//p' "$TRIM_PKGHOME/initial-password.txt" | head -1)"
if [ -n "$PW" ]; then
    LOGIN="$(curl -sS -c "$SANDBOX/cookies.txt" -H 'Content-Type: application/json' \
        -d "{\"password\":\"$PW\"}" "http://127.0.0.1:$PORT/api/login" 2>/dev/null)"
    check "用初始口令登录成功" "echo '$LOGIN' | grep -q '\"ok\": *true'"
    check "登录后 /api/dashboard 返回 200" \
        "curl -sS -b '$SANDBOX/cookies.txt' -o /dev/null -w '%{http_code}' 'http://127.0.0.1:$PORT/api/dashboard' | grep -q 200"
    check "Cookie 带 HttpOnly 且 CSRF 有效" "echo '$LOGIN' | grep -q 'csrf'"
    check "错误口令被拒绝" \
        "curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{\"password\":\"wrong-pass\"}' 'http://127.0.0.1:$PORT/api/login' | grep -q 401"
else
    bad "无法从 initial-password.txt 读取口令"
fi
check "健康检查无需登录" \
    'curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/health" | grep -q 200'

echo
echo "[9/9] 卸载（默认保留数据）"
"$TRIM_APPDEST/cmd/main" stop >/dev/null 2>&1
export wizard_data_action="keep"
if "$TRIM_APPDEST/cmd/install_init" >/dev/null 2>&1 && "$TRIM_APPDEST/cmd/uninstall_callback" >/dev/null 2>&1; then
    ok "uninstall_callback 返回 0"
else
    bad "uninstall_callback 失败"
fi
check "默认卸载保留存档"       '[ -d "$TRIM_PKGHOME/data/Saves" ]'
check "默认卸载保留 MOD"       '[ -d "$TRIM_PKGHOME/data/Mods" ]'
check "默认卸载保留快照"       '[ -d "$TRIM_PKGHOME/data/snapshots" ]'
check "默认卸载保留配置"       '[ -f "$TRIM_PKGHOME/manager/config.json" ]'
# 再测「删除全部数据」路径
export wizard_data_action="delete-all"
"$TRIM_APPDEST/cmd/uninstall_callback" >/dev/null 2>&1
check "选择删除后数据已清除"   '[ ! -d "$TRIM_PKGHOME/data" ] && [ ! -d "$TRIM_PKGHOME/manager" ]'

echo
echo "=============================================="
echo " 通过 $PASS 项，失败 $FAIL 项"
echo "=============================================="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
