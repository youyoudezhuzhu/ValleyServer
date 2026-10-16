#!/bin/bash
# 构建可安装的 .fpk 包。
#
# 关键点（均来自 fnOS 真机实测踩坑记录）：
#   1. **绝不在仓库目录里直接 fnpack build**：fnpack 会给被处理目录加限制性 ACL，
#      打包失败一次后目录会变成 d---------，连属主都删不掉，而且 `cp -r` 会把该 ACL 复制出去。
#      因此一律在独立暂存目录里构建。
#   2. fnpack **打包失败时退出码仍为 0**，必须 grep "Packing successfully" 判成功，
#      并且**先删旧产物**（否则上一轮的 .fpk 会让「文件存在性检查」误判成功）。
#   3. cmd/* 必须 chmod +x（fnpack 1.2.4 不校验执行位，只会在装机时炸）。
#   4. 产物名是 <appname>.fpk（不带版本号）。
#
# 用法：
#   tools/build-fpk.sh                 # 使用默认暂存目录
#   STAGE_ROOT=/tmp/x tools/build-fpk.sh
#   FNPACK=/path/to/fnpack tools/build-fpk.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE/fnpack"
DIST="${DIST_DIR:-$HERE/dist}"
STAGE_ROOT="${STAGE_ROOT:-/vol1/@apphome/valleyserver/data/fpk-build}"
STAGE="$STAGE_ROOT/stage"
FNPACK="${FNPACK:-fnpack}"
# fnpack 不在 PATH 时，回落到仓库内已提交的二进制（云编译环境常常没装）。
#
# 注意：这里**不能**用 `[ ! -x "$FNPACK" ]` 做判断 —— 仓库里有个名为 `fnpack/`
# 的**目录**，而目录是可执行的（可搜索位），会被误判成"找到了可执行文件"（实测踩到）。
# 必须要求它是常规文件且有执行位。
if ! command -v "$FNPACK" >/dev/null 2>&1; then
    if [ ! -f "$FNPACK" ] || [ ! -x "$FNPACK" ]; then
        for cand in "$HERE"/dist/fnpack-*-linux-amd64; do
            [ -f "$cand" ] && [ -x "$cand" ] || continue
            FNPACK="$cand"
            break
        done
    fi
fi
if ! command -v "$FNPACK" >/dev/null 2>&1; then
    echo "找不到 fnpack。" >&2
    echo "  · 安装到 PATH，或" >&2
    echo "  · 用 FNPACK=/path/to/fnpack 指定，或" >&2
    echo "  · 把二进制放到 $HERE/dist/fnpack-<ver>-linux-amd64" >&2
    exit 1
fi
LOG="$STAGE_ROOT/fnpack.log"

mkdir -p "$DIST" "$STAGE_ROOT"

APPNAME="$(grep -E '^appname' "$SRC/manifest" | head -1 | cut -d= -f2 | tr -d ' "')"
VERSION="$(grep -E '^version' "$SRC/manifest" | head -1 | cut -d= -f2 | tr -d ' "')"
[ -z "$APPNAME" ] && { echo "manifest 缺少 appname" >&2; exit 1; }
[ -z "$VERSION" ] && { echo "manifest 缺少 version" >&2; exit 1; }

echo "==> 构建 $APPNAME $VERSION"
echo "    源目录: $SRC"
echo "    暂存区: $STAGE"
echo "    产物名: $APPNAME.fpk"

# ── 1) 清理暂存目录（先修权限，否则带 ACL 的目录删不掉）──
STAGE_ROOT="${STAGE_ROOT:-}"
if [ -z "$STAGE_ROOT" ]; then echo "STAGE_ROOT 为空，拒绝继续" >&2; exit 1; fi
case "$STAGE_ROOT" in
    /|/vol1|/vol2|/tmp|/var|/vol1/@apphome) echo "STAGE_ROOT 过于宽泛：$STAGE_ROOT" >&2; exit 1 ;;
esac
chmod -R u+rwX "$STAGE_ROOT" 2>/dev/null || true
rm -rf "$STAGE"
mkdir -p "$STAGE"

# ── 2) 拷贝（排除开发残留）──
cp -a "$SRC/." "$STAGE/"
# 立刻修权限：从带 TrimACL 的目录 cp 出来的子目录可能是 d---------
chmod -R u+rwX "$STAGE"

# 清理缓存与开发残留（绝不能打进包里）
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true
find "$STAGE" -name '.DS_Store' -delete 2>/dev/null || true
rm -rf "$STAGE/.venv" "$STAGE/node_modules" 2>/dev/null || true

# ── 3) 执行位 ──
chmod +x "$STAGE"/cmd/* 2>/dev/null || true
[ -f "$STAGE/app/server/run.py" ] && chmod +x "$STAGE/app/server/run.py"

# ── 4) 断言关键文件存在 ──
for f in manifest ICON.PNG ICON_256.PNG cmd/main app/ui/config config/privilege config/resource; do
    if [ ! -e "$STAGE/$f" ]; then echo "缺少必要文件：$f" >&2; exit 1; fi
done
if [ ! -d "$STAGE/app/server" ]; then echo "缺少 app/server 目录" >&2; exit 1; fi
if [ ! -f "$STAGE/app/www/index.html" ]; then echo "缺少前端资源 app/www/index.html" >&2; exit 1; fi
for f in "$STAGE"/cmd/install_init "$STAGE"/cmd/install_callback \
         "$STAGE"/cmd/upgrade_init "$STAGE"/cmd/upgrade_callback \
         "$STAGE"/cmd/uninstall_init "$STAGE"/cmd/uninstall_callback \
         "$STAGE"/cmd/config_init "$STAGE"/cmd/config_callback; do
    [ -x "$f" ] || { echo "cmd 脚本缺少执行位：$f" >&2; exit 1; }
done

# manifest 必须包含官方必填字段（fnpack 不拦，只能自己断言）
for field in appname version display_name desc source platform desktop_uidir desktop_applaunchname; do
    grep -qE "^${field}[[:space:]]*=" "$STAGE/manifest" || {
        echo "manifest 缺少官方必填字段：$field" >&2; exit 1; }
done
# version 必须是 x.y.z[-r]，每段整数（否则 fnpack 会拒绝）
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?$'; then
    echo "version 格式非法：$VERSION（要求 x.y.z 或 x.y.z-r，每段为整数）" >&2
    exit 1
fi
# ui/config 的 key 必须与 desktop_applaunchname 一致
LAUNCH="$(grep -E '^desktop_applaunchname' "$STAGE/manifest" | cut -d= -f2 | tr -d ' "')"
if ! grep -q "\"$LAUNCH\"" "$STAGE/app/ui/config"; then
    echo "app/ui/config 中缺少桌面入口 key：$LAUNCH" >&2
    exit 1
fi
for j in "$STAGE/config/privilege" "$STAGE/config/resource" "$STAGE/app/ui/config"; do
    python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$j" \
        || { echo "非法 JSON：$j" >&2; exit 1; }
done

# ── 5) 删除旧产物（关键：否则失败时会被误判成功）──
rm -f "$STAGE_ROOT"/*.fpk "$DIST"/*.fpk 2>/dev/null || true

# ── 6) 打包 ──
echo "==> 运行 fnpack build"
set +e
( cd "$STAGE" && "$FNPACK" build ) > "$LOG" 2>&1
RC=$?
set -e
# 注意：不能只信退出码（fnpack 失败时也可能是 0）
if ! grep -q "Packing successfully" "$LOG"; then
    echo "----- fnpack 输出 -----" >&2
    cat "$LOG" >&2
    echo "------------------------" >&2
    echo "fnpack 打包失败（退出码 $RC）。" >&2
    exit 1
fi

BUILT="$STAGE/$APPNAME.fpk"
if [ ! -f "$BUILT" ]; then
    cat "$LOG" >&2
    echo "fnpack 报告成功，但没有产出 $BUILT" >&2
    exit 1
fi

# ── 7) 校验双层 tar 结构 ──
echo "==> 校验产物结构"
OUT="$DIST/${APPNAME}-${VERSION}.fpk"
cp -f "$BUILT" "$OUT"
chmod 0644 "$OUT"

# 注意 tar 会为目录条目输出结尾斜杠，因此统一先规范化再比较
OUTER="$(tar tzf "$OUT" | sed 's:/$::' | sort -u)"
for want in manifest ICON.PNG ICON_256.PNG app.tgz \
            cmd/main cmd/install_init cmd/install_callback cmd/upgrade_init \
            cmd/upgrade_callback cmd/uninstall_init cmd/uninstall_callback \
            cmd/config_init cmd/config_callback \
            config/privilege config/resource wizard/install wizard/config wizard/uninstall; do
    echo "$OUTER" | grep -qx "$want" || { echo "外层缺少 $want" >&2; echo "$OUTER" >&2; exit 1; }
done

INNER="$(tar xzOf "$OUT" app.tgz | tar tzf - | sed 's:/$::' | sort -u)"
# app.tgz 的顶层必须是白名单。
# 实测 fnpack 1.2.4 会把**包根的 config/ 也一并塞进 app.tgz**，
# 解包到 $TRIM_APPDEST 后会得到 {server,www,ui,config} —— 属正常现象。
TOPLEVEL="$(echo "$INNER" | awk -F/ '{print $1}' | sort -u | grep -v '^$')"
for top in $TOPLEVEL; do
    case "$top" in
        server|www|ui|bin|config) : ;;
        *) echo "app.tgz 顶层出现非预期目录：$top" >&2; echo "$TOPLEVEL" >&2; exit 1 ;;
    esac
done
# 开发残留绝不能进包
for junk in __pycache__ .venv node_modules .git .gradle; do
    if echo "$INNER" | grep -qE "(^|/)${junk}(/|$)"; then
        echo "app.tgz 内出现开发残留：$junk" >&2; exit 1
    fi
done
for want in server/run.py server/http_base.py server/mgr/__init__.py server/mgr/config.py server/mgr/tasks.py \
            server/mgr/snapshots.py server/mgr/mods.py \
            server/mgr/saves.py server/mgr/versions.py server/mgr/diagnostics.py \
            server/mgr/supervisor.py server/api_handlers.py server/orchestration.py \
            server/mgr/runtime_base.py server/mgr/native_runtime.py server/mgr/xstack.py \
            www/index.html www/static/app.js www/static/style.css \
            ui/config ui/images/icon_64.png ui/images/icon_256.png; do
    echo "$INNER" | grep -qx "$want" || { echo "app.tgz 缺少 $want" >&2; echo "$INNER" | head -60 >&2; exit 1; }
done
# 容器实现必须已移除（本项目为原生部署）
for gone in server/mgr/compose_mgr.py server/mgr/docker_runtime.py; do
    if echo "$INNER" | grep -qx "$gone"; then
        echo "app.tgz 内仍残留容器实现：$gone（应已移除）" >&2; exit 1
    fi
done

# 版本一致性（防止「装了但版本没变」的隐形故障）
TAR_VER="$(tar xzOf "$OUT" manifest | grep -E '^version' | head -1 | cut -d= -f2 | tr -d ' "')"
[ "$TAR_VER" = "$VERSION" ] || { echo "包内版本 $TAR_VER 与源 $VERSION 不一致" >&2; exit 1; }

# ── 8) 记录校验值 ──
SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
SIZE="$(stat -c %s "$OUT")"
{
    echo "appname=$APPNAME"
    echo "version=$VERSION"
    echo "file=$OUT"
    echo "size=$SIZE"
    echo "sha256=$SHA"
    echo "built_at=$(date '+%F %T %z')"
    echo "fnpack=$(command -v "$FNPACK" || echo "$FNPACK")"
} > "$DIST/${APPNAME}-${VERSION}.build-info.txt"

cat <<EOF

==> 构建成功
    产物：$OUT
    大小：$SIZE 字节（$(echo "scale=2; $SIZE/1024" | bc 2>/dev/null || echo ?) KiB）
    SHA-256：$SHA
    信息：$DIST/${APPNAME}-${VERSION}.build-info.txt

下一步（在飞牛 OS 上）：
    trim-cli app install-fpk --remote-path "$OUT" --volume-id 1 --yes
  或：应用中心 → 手动安装 → 选择该 .fpk
EOF
