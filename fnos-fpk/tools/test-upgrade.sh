#!/bin/bash
# 升级路径回归测试。
#
# 覆盖「升级后功能没变」这个最难排查的隐形故障：
#   场景 1：正常升级 —— upgrade_init 必须停服务并删 PID；upgrade_callback 必须重新拉起，
#           且 /api/health 报出的版本号必须是**新版本**。
#   场景 2：启动依赖异常 —— 不能把「本来还能跑」的应用升级成「彻底起不来」，
#           但启动失败必须明确非零退出（不能静默 exit 0 骗用户说升级成功）。
#
# ⚠️ 安全护栏：cmd/ 会被复制到 mktemp 沙箱，并断言所有 TRIM_* 都落在沙箱内。
#
# 用法： tools/test-upgrade.sh [fpk 路径或 fnpack 目录]

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-$HERE/fnpack}"

# 端口不能硬编码：本机 18801 就被真实应用占着，硬编码会打到线上实例、
# 或者被上一轮测试的残留进程占用，得出假结论。这里动态取一个空闲端口。
free_port() {
    "${PYTHON_BIN:-python3}" - <<'PYEOF'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF
}
PORT="${TEST_PORT:-$(free_port)}"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  ✅ $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

WORK="$(mktemp -d /tmp/vs-upgrade.XXXXXX)"
cleanup() {
    # 停服务时必须带同样的 TRIM_* 环境变量，否则会去错目录找 PID
    if [ -x "$WORK/app/cmd/main" ]; then
        env TRIM_APPDEST="$WORK/app" TRIM_PKGVAR="$WORK/var" TRIM_PKGHOME="$WORK/home" \
            TRIM_PKGETC="$WORK/etc" TRIM_PKGTMP="$WORK/tmp" TRIM_SERVICE_PORT="$PORT" \
            "$WORK/app/cmd/main" stop >/dev/null 2>&1 || true
    fi
    pkill -f "$WORK/app/server/run.py" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "== 升级路径测试 =="
echo "   被测包：$TARGET"
echo "   沙箱：$WORK"
echo "   端口：$PORT"

# ── 准备沙箱里的应用 ──
mkdir -p "$WORK/app" "$WORK/cmd" "$WORK/var" "$WORK/home" "$WORK/etc" "$WORK/tmp"
if [ -f "$TARGET" ]; then
    tar xzf "$TARGET" -C "$WORK/app"
    tar xzOf "$TARGET" app.tgz | tar xzf - -C "$WORK/app"
else
    # 目录形态：把 app/ 的内容摊到 $WORK/app（与 fnOS 落盘布局一致），
    # 但 server/run.py 等文件在 app.tgz 里，必须一并解开。
    mkdir -p "$WORK/app"
    cp -a "$TARGET/." "$WORK/app/"
    if [ -f "$TARGET/../dist/app.tgz" ]; then
        tar xzf "$TARGET/../dist/app.tgz" -C "$WORK/app/"
    fi
    if [ -f "$WORK/app/app.tgz" ]; then
        tar xzf "$WORK/app/app.tgz" -C "$WORK/app/" 2>/dev/null || true
        rm -f "$WORK/app/app.tgz"
    fi
    # 摊平 app/ 子目录（server/ www/ ui/ docker/ 直接放到 $WORK/app 下）
    if [ -d "$WORK/app/app" ]; then
        cp -a "$WORK/app/app/." "$WORK/app/"
        rm -rf "$WORK/app/app"
    fi
fi
# cmd 与 app 同级（与 fnOS 落盘布局一致：$TRIM_APPDEST/../cmd）
cp -a "$WORK/app/cmd/." "$WORK/cmd/" 2>/dev/null || true
rm -rf "$WORK/app/cmd" "$WORK/app/config" "$WORK/app/wizard" 2>/dev/null || true
mkdir -p "$WORK/app/../cmd"
cp -a "$WORK/cmd/." "$WORK/app/../cmd/" 2>/dev/null || true
chmod +x "$WORK/app/../cmd"/* 2>/dev/null || true
chmod +x "$WORK/app/server/run.py" 2>/dev/null || true

# 旧的 manifest（供 cmd/main 读版本号）
MANIFEST="$WORK/app/manifest"
[ -f "$MANIFEST" ] || printf 'appname = valleyserver\nversion = 0.1.0\n' > "$MANIFEST"

export TRIM_APPNAME="valleyserver"
# 必须显式给默认值：CI 环境里没有 TRIM_APPVER，而脚本开了 `set -u`。
# 之前靠开发者 shell 里恰好存在该变量才通过（实测在 CI 上直接 unbound variable）。
export TRIM_APPVER="${TRIM_APPVER:-0.1.0}"
export APP_VERSION="${APP_VERSION:-$TRIM_APPVER}"
export TRIM_APPDEST="$WORK/app"
export TRIM_PKGVAR="$WORK/var"
export TRIM_PKGHOME="$WORK/home"
export TRIM_PKGETC="$WORK/etc"
export TRIM_PKGTMP="$WORK/tmp"
export TRIM_TEMP_LOGFILE="$WORK/upgrade.log"
export TRIM_SERVICE_PORT="$PORT"
export HOME="/root"      # 模拟 fnOS 注入的 HOME，脚本必须覆盖它
unset DATA_LIBRARY_PATH  # 模拟 fnOS 不注入该变量

# ── 安全护栏：断言所有路径都在沙箱内 ──
for v in TRIM_APPDEST TRIM_PKGVAR TRIM_PKGHOME TRIM_PKGETC TRIM_PKGTMP TRIM_TEMP_LOGFILE; do
    val="${!v}"
    case "$val" in
        "$WORK"/*) : ;;
        *) echo "❌ 安全断言失败：$v=$val 不在沙箱内" >&2; exit 1 ;;
    esac
done

MAIN="$WORK/app/../cmd/main"
# install_callback / upgrade_callback 里用 $APP/../cmd/xxx 访问，构造一致的视图
ln -sfn "$WORK/app" "$WORK/app/target" 2>/dev/null || true

echo
echo "[准备] 安装并启动旧版本"
"$WORK/app/../cmd/install_callback" >/dev/null 2>&1
if "$MAIN" start >/dev/null 2>&1; then ok "旧版本启动成功"; else bad "旧版本启动失败：$(tail -3 "$TRIM_TEMP_LOGFILE" 2>/dev/null)"; fi
sleep 1
V_OLD="$(curl -sS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')"
ok "旧版本 /api/health 版本号 = ${V_OLD:-（空）}"

echo
echo "[场景 1] 正常升级：0.1.0 → 9.9.9"
sed -i 's/^version *=.*/version               = 9.9.9/' "$MANIFEST"
OLD_VER="$TRIM_APPVER"; export TRIM_OLD_APPVER="$OLD_VER"; export TRIM_APPVER="9.9.9"

"$WORK/app/../cmd/upgrade_init" >/dev/null 2>&1
RC_INIT=$?
[ "$RC_INIT" = "0" ] && ok "upgrade_init 返回 0" || bad "upgrade_init 返回 $RC_INIT"

sleep 1
LISTEN="$(ss -lnt 2>/dev/null | grep -cE "[:.]${PORT}[[:space:]]" || true)"
[ "$LISTEN" = "0" ] && ok "upgrade_init 后端口已释放（无监听）" || bad "upgrade_init 后端口仍被占用（$LISTEN 个监听）"

if [ -f "$TRIM_PKGVAR/valleyserver.pid" ]; then
    PID="$(cat "$TRIM_PKGVAR/valleyserver.pid" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        bad "upgrade_init 后旧进程仍在运行（pid $PID）"
    else
        ok "upgrade_init 后 PID 文件已失效/进程已退出"
    fi
else
    ok "upgrade_init 后 PID 文件已删除"
fi

"$WORK/app/../cmd/upgrade_callback" >/dev/null 2>&1
RC_CB=$?
[ "$RC_CB" = "0" ] && ok "upgrade_callback 返回 0" || bad "upgrade_callback 返回 $RC_CB"

sleep 1
HEALTH="$(curl -sS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
V_NEW="$(echo "$HEALTH" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')"
if [ "$V_NEW" = "9.9.9" ]; then
    ok "升级后 /api/health 报告新版本 9.9.9"
else
    bad "升级后 /api/health 报告版本为「${V_NEW:-空}」，不是 9.9.9 —— 典型的「升级了但功能没变」"
fi
echo "$HEALTH" | grep -q '"ok": *true' && ok "升级后健康检查通过" || bad "升级后健康检查未通过"
echo "$HEALTH" | grep -q 'pkghome' && ok "升级后 HOME 仍被正确覆盖（健康检查报告 pkghome）" || bad "升级后 HOME 覆盖失效"

"$MAIN" status; RC_ST=$?
[ "$RC_ST" = "0" ] && ok "升级后 cmd/main status=0" || bad "升级后 status=$RC_ST（应为 0）"

echo
echo "[场景 2] 启动失败必须明确报错（不能静默 exit 0）"
"$MAIN" stop >/dev/null 2>&1
# 故意破坏：把 run.py 移走，模拟代码不完整
mv "$WORK/app/server/run.py" "$WORK/app/server/run.py.bak"
# 先单独验证 cmd/main 的退出码没有被末尾的 exit 0 掩盖
if "$MAIN" start >/dev/null 2>&1; then
    bad "代码缺失时 cmd/main start 返回 0（退出码被掩盖：函数的 return 1 被 case 后的 exit 0 覆盖）"
else
    ok "代码缺失时 cmd/main start 返回非零（退出码正确传递）"
fi
if "$WORK/app/../cmd/upgrade_callback" >/dev/null 2>&1; then
    bad "代码缺失时 upgrade_callback 仍返回 0（等于骗用户「升级成功」）"
else
    ok "代码缺失时 upgrade_callback 返回非零"
fi
if [ -s "$TRIM_TEMP_LOGFILE" ] && grep -q '启动失败\|缺失' "$TRIM_TEMP_LOGFILE"; then
    ok "失败原因已写入 \$TRIM_TEMP_LOGFILE（用户能看到）"
else
    bad "失败原因未写入 \$TRIM_TEMP_LOGFILE"
fi
# 恢复，验证重试路径能自愈
mv "$WORK/app/server/run.py.bak" "$WORK/app/server/run.py"
if "$WORK/app/../cmd/upgrade_callback" >/dev/null 2>&1; then
    ok "代码恢复后再跑 upgrade_callback 能成功（可自愈）"
else
    bad "代码恢复后 upgrade_callback 仍失败"
fi
sleep 1
V_FIX="$(curl -sS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')"
[ "$V_FIX" = "9.9.9" ] && ok "自愈后 /api/health 版本号正确" || bad "自愈后版本号为「${V_FIX:-空}」"

echo
echo "[场景 3] 卸载/重装数据保留"
"$MAIN" stop >/dev/null 2>&1
mkdir -p "$TRIM_PKGHOME/data/Saves/MyFarm_123"
echo "<SaveGame/>" > "$TRIM_PKGHOME/data/Saves/MyFarm_123/SaveGameInfo"
export wizard_data_action="keep"
"$WORK/app/../cmd/uninstall_init" >/dev/null 2>&1
"$WORK/app/../cmd/uninstall_callback" >/dev/null 2>&1
[ -f "$TRIM_PKGHOME/data/Saves/MyFarm_123/SaveGameInfo" ] \
    && ok "升级/卸载后存档仍在（数据与生命周期分离）" \
    || bad "卸载后存档丢失"

echo
echo "== 结果：通过 $PASS，失败 $FAIL =="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
