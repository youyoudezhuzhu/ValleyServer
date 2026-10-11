#!/bin/bash
# 运行全部自动化测试（纯标准库，不需要 pip / 外网）。
# 用法：
#   tools/run-tests.sh            # 每个测试文件独立进程
#   tools/run-tests.sh -v         # 详细输出（透传给 unittest）
#   tools/run-tests.sh --python3.12   # 指定解释器
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1

PY="${PYTHON_BIN:-}"
ARGS=()
for a in "$@"; do
    case "$a" in
        --python*) PY="${a#--}" ;;
        *) ARGS+=("$a") ;;
    esac
done
if [ -z "$PY" ]; then
    for c in python3.12 python3; do
        if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
    done
fi
[ -z "$PY" ] && { echo "未找到 python3" >&2; exit 1; }

FAILED=0
echo "== Python 单元/集成测试 =="
for t in tests/test_*.py; do
    echo "--- $t"
    if ! "$PY" "$t" ${ARGS[@]+"${ARGS[@]}"}; then
        FAILED=$((FAILED + 1))
    fi
done

echo
echo "== 前端执行测试（真实后端 + DOM 替身；防"界面卡在正在检查登录状态"这类缺陷） =="
if command -v node >/dev/null 2>&1; then
    if node tools/test-frontend.js; then
        echo "  ✅ 前端执行测试通过"
    else
        echo "  ❌ 前端执行测试失败"; FAILED=$((FAILED + 1))
    fi
else
    echo "  ⚠️  未安装 node，跳过前端执行测试（CI 上有 node，本地请留意）"
fi

echo "== Shell 测试（日志轮转 / 升级路径） =="
echo "--- tools/test-logrotate.sh"
if ! bash tools/test-logrotate.sh; then FAILED=$((FAILED + 1)); fi

echo "--- tools/test-upgrade.sh"
# 升级测试需要一个已构建的 .fpk（其中 app.tgz 才有 server/run.py）。
# 没有产物时先构建一次，避免用未打包的目录得到假结论。
FPK="$(ls -1t dist/*.fpk 2>/dev/null | head -1)"
if [ -z "$FPK" ]; then
    echo "    未找到 dist/*.fpk，先构建…"
    if STAGE_ROOT="${STAGE_ROOT:-/tmp/fpk-build-selftest}" DIST_DIR="$PWD/dist" \
       bash tools/build-fpk.sh >/tmp/fpk-build-selftest.log 2>&1; then
        FPK="$(ls -1t dist/*.fpk | head -1)"
    else
        echo "    ❌ 构建失败，见 /tmp/fpk-build-selftest.log"
        FAILED=$((FAILED + 1))
    fi
fi
if [ -n "$FPK" ]; then
    echo "    使用产物：$FPK"
    if ! bash tools/test-upgrade.sh "$FPK"; then FAILED=$((FAILED + 1)); fi

    echo "--- tools/simulate-install.sh（沙箱完整安装/卸载）"
    # 端口必须动态取：硬编码会打到线上实例或上一轮残留，得出假结论
    SIM_PORT="$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
    if ! bash tools/simulate-install.sh "$FPK" "$SIM_PORT"; then FAILED=$((FAILED + 1)); fi
fi

echo
echo "== 构建产物校验（双层 tar 结构 + 脚本可执行位） =="
if [ -n "${FPK:-}" ]; then
    echo "--- 产物结构"
    if tar tzf "$FPK" | sed 's:/$::' | sort -u | grep -qx manifest \
       && tar xzOf "$FPK" app.tgz | tar tzf - | grep -q '^server/run.py$'; then
        echo "  ✅ 外层 manifest 与内层 server/run.py 均存在"
    else
        echo "  ❌ 产物结构不完整"; FAILED=$((FAILED + 1))
    fi
    echo "--- 原生实现齐全、无容器实现残留"
    APP_LIST="$(tar xzOf "$FPK" app.tgz | tar tzf -)"
    if echo "$APP_LIST" | grep -q '^server/mgr/native_runtime.py$' \
       && echo "$APP_LIST" | grep -q '^server/mgr/runtime_base.py$' \
       && echo "$APP_LIST" | grep -q '^server/mgr/xstack.py$'; then
        echo "  ✅ 原生运行后端与显示栈供给模块已打入"
    else
        echo "  ❌ 缺少原生实现模块"; FAILED=$((FAILED + 1))
    fi
    if echo "$APP_LIST" | grep -qE '^server/mgr/(compose_mgr|docker_runtime)\.py$'; then
        echo "  ❌ 包内仍残留容器实现（应已移除）"; FAILED=$((FAILED + 1))
    else
        echo "  ✅ 包内无容器实现残留"
    fi
    echo "--- cmd 生命周期脚本可执行位"
    MISSING_X=""
    for s in main install_init install_callback upgrade_init upgrade_callback \
             uninstall_init uninstall_callback config_init config_callback; do
        if ! tar tvzf "$FPK" | grep -E "cmd/$s$" | grep -q '^-rwx'; then
            MISSING_X="$MISSING_X $s"
        fi
    done
    if [ -z "$MISSING_X" ]; then
        echo "  ✅ 九个生命周期脚本均带执行位"
    else
        echo "  ❌ 缺少执行位：$MISSING_X"; FAILED=$((FAILED + 1))
    fi
    echo "--- 包内不得出现任何容器运行时字样（含「不需要容器」这类对冲说法）"
    CONTAINER_HITS="$(tar xzOf "$FPK" app.tgz | tar xzf - -O --wildcards '*' 2>/dev/null | grep -ciE 'docker|compose' || true)"
    SRC_HITS="$(grep -rniE 'docker|compose' fnpack/ --include='*' 2>/dev/null | grep -v __pycache__ | wc -l | tr -d ' ')"
    if [ "$SRC_HITS" = "0" ] && [ "$CONTAINER_HITS" = "0" ]; then
        echo "  ✅ 源码与产物中均无容器运行时字样"
    else
        echo "  ❌ 仍有容器运行时字样残留：源码 $SRC_HITS 处、产物 $CONTAINER_HITS 处"
        grep -rniE 'docker|compose' fnpack/ --include='*' 2>/dev/null | grep -v __pycache__ | head -5
        FAILED=$((FAILED + 1))
    fi
    echo "--- cmd/ 与 wizard/ 单独复核"
    CW_HITS="$(grep -rniE 'docker|compose' fnpack/cmd fnpack/wizard fnpack/manifest 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$CW_HITS" = "0" ]; then
        echo "  ✅ 生命周期脚本 / 向导 / manifest 均无容器运行时字样"
    else
        echo "  ❌ 仍有容器运行时字样残留（$CW_HITS 处）"
        grep -rniE 'docker|compose' fnpack/cmd fnpack/wizard fnpack/manifest | head -5
        FAILED=$((FAILED + 1))
    fi

    echo "--- manifest 版本必须为 x.y.z"
    if tar xzOf "$FPK" manifest | grep -qE '^version[[:space:]]*=[[:space:]]*[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?[[:space:]]*$'; then
        echo "  ✅ manifest 版本格式合法"
    else
        echo "  ❌ manifest 版本格式不合法"; FAILED=$((FAILED + 1))
    fi
fi

echo
if [ "$FAILED" -gt 0 ]; then
    echo "❌ 有 $FAILED 组测试失败"
    exit 1
fi
echo "✅ 全部测试通过"
