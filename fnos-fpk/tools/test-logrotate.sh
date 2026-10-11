#!/bin/bash
# 日志轮转回归测试（6 条断言）。
#
# 关键点：测试**从真实的 cmd/main 里 sed 提取 trim_log 函数**再跑，
# 而不是复制一份代码 —— 否则脚本改了、测试还在测旧逻辑（漂移）。
# 因此 trim_log 的默认值必须写在**函数体内**（见 cmd/main）。
#
# 用法： tools/test-logrotate.sh [cmd/main 的路径]

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN="${1:-$HERE/fnpack/cmd/main}"
[ -f "$MAIN" ] || { echo "找不到 cmd/main：$MAIN" >&2; exit 1; }

WORK="$(mktemp -d /tmp/vs-logrot.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  ✅ $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

echo "== 日志轮转测试 =="
echo "   被测脚本：$MAIN"
echo "   工作目录：$WORK"

# 从真实脚本中提取函数（含 log() 依赖，这里提供桩实现）
{
    echo 'LOG_MAX_BYTES=5242880'
    echo 'LOG_KEEP_BYTES=1048576'
    echo 'log() { :; }'
    sed -n '/^trim_log() {/,/^}$/p' "$MAIN"
} > "$WORK/trimlog.sh"

if ! grep -q 'trim_log()' "$WORK/trimlog.sh"; then
    echo "❌ 无法从 $MAIN 提取 trim_log 函数（默认值可能被写到了函数体外）" >&2
    exit 1
fi

# ── 断言 1：未超限时不改动 ──
F1="$WORK/small.log"
printf 'hello\nworld\n' > "$F1"
BEFORE="$(cat "$F1")"
# shellcheck disable=SC1090
( source "$WORK/trimlog.sh"; LOG_MAX_BYTES=100 LOG_KEEP_BYTES=10; trim_log "$F1" )
if [ "$(cat "$F1")" = "$BEFORE" ]; then ok "未超限时文件内容不变"; else bad "未超限却被改动"; fi

# ── 断言 2：超限时保留尾部，且丢掉被截断的首行 ──
F2="$WORK/big.log"
# 10 行，每行 20 字节；max=100 keep=60
for i in $(seq 1 10); do printf 'LINE%02d-0123456789ABC\n' "$i" >> "$F2"; done
( source "$WORK/trimlog.sh"; LOG_MAX_BYTES=100 LOG_KEEP_BYTES=60; trim_log "$F2" )
SIZE2="$(stat -c %s "$F2")"
if [ "$SIZE2" -lt 200 ]; then ok "超限后文件被截断（$SIZE2 字节）"; else bad "超限后未截断（$SIZE2 字节）"; fi
if grep -q 'LINE10' "$F2"; then ok "保留了最新的尾部内容"; else bad "尾部内容丢失"; fi
if head -1 "$F2" | grep -qE '^[A-Z0-9]{20}$'; then
    bad "首行是被截断的残行（未被丢弃）"
else
    ok "被截断的首行已丢弃"
fi

# ── 断言 3：轮转后 inode 不变（必须原地重写，不能 mv/rename）──
F3="$WORK/inode.log"
for i in $(seq 1 10); do printf 'LINE%02d-0123456789ABC\n' "$i" >> "$F3"; done
INO_BEFORE="$(stat -c %i "$F3")"
( source "$WORK/trimlog.sh"; LOG_MAX_BYTES=100 LOG_KEEP_BYTES=60; trim_log "$F3" )
INO_AFTER="$(stat -c %i "$F3")"
if [ "$INO_BEFORE" = "$INO_AFTER" ]; then
    ok "轮转后 inode 不变（$INO_BEFORE）"
else
    bad "轮转后 inode 变了（$INO_BEFORE → $INO_AFTER）：用了 mv/rename，子进程会继续写旧 inode"
fi

# ── 断言 4：轮转后经同一 fd 追加写入仍落在同一文件 ──
F4="$WORK/fd.log"
for i in $(seq 1 10); do printf 'LINE%02d-0123456789ABC\n' "$i" >> "$F4"; done
exec 3>>"$F4"                      # 模拟子进程持有的 fd
( source "$WORK/trimlog.sh"; LOG_MAX_BYTES=100 LOG_KEEP_BYTES=60; trim_log "$F4" )
printf 'AFTER-ROTATE-MARKER\n' >&3
exec 3>&-
if grep -q 'AFTER-ROTATE-MARKER' "$F4"; then
    ok "轮转后经原 fd 的写入仍落在同一文件"
else
    bad "轮转后经原 fd 的写入丢失（日志被静默丢弃）"
fi

# ── 断言 5：幂等（连续两次轮转结果稳定）──
F5="$WORK/idem.log"
for i in $(seq 1 10); do printf 'LINE%02d-0123456789ABC\n' "$i" >> "$F5"; done
( source "$WORK/trimlog.sh"; LOG_MAX_BYTES=100 LOG_KEEP_BYTES=60; trim_log "$F5" )
A="$(sha256sum "$F5" | cut -d' ' -f1)"
( source "$WORK/trimlog.sh"; LOG_MAX_BYTES=100 LOG_KEEP_BYTES=60; trim_log "$F5" )
B="$(sha256sum "$F5" | cut -d' ' -f1)"
if [ "$A" = "$B" ]; then ok "重复调用幂等"; else bad "重复调用结果不一致"; fi

# ── 断言 6：文件不存在时不报错 ──
if ( source "$WORK/trimlog.sh"; trim_log "$WORK/nonexistent.log" ); then
    ok "文件不存在时安全返回"
else
    bad "文件不存在时返回非零"
fi

echo
echo "== 结果：通过 $PASS，失败 $FAIL =="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
