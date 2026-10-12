# 阶段 1 审计报告 —— ValleyServer 管理器（0.2.12）

> 对应需求：`Transfer Dock_Text_20261012181358.txt` 第十节「阶段 1：审计」
> 原则：**先审计、不动代码**。本报告只陈述事实、给方案，不含代码改动。
> 审计日期：2026-10-12

---

## 0. 审计范围与方法

| 项 | 内容 |
| :--- | :--- |
| 应用本体 | `fnos-fpk/`（我们写的全部代码、脚本、测试、文档） |
| **不在改造范围** | 仓库根的 `Mods/`、`docs/`、`src/`、`oneclick-script/`、根 `README*.md` —— 这些是**上游 Lixeer/ValleyServer 的资产**。fork 需要与上游保持可合并，且上游 2.x 本身就是 Docker 路线（`oneclick-script/docker/`），不应由我们改写 |
| 方法 | ① 自研扫描器按关键字+语法上下文分类（不只看字符串）；② 逐项人工判定语义；③ 持久化结构逐个追踪读写点；④ 用真机数据复核 |
| 扫描器 | `/tmp/docker_scan.py`（关键字分 5 类：exec/data/string/comment/doc/test + kinds） |

### 结论摘要（先给答案）

| 问题 | 答案 |
| :--- | :--- |
| 是否还有 Docker 运行时依赖？ | **没有** |
| 是否还有代码会执行 docker 命令或访问 Docker API/socket？ | **没有**（扫描 0 处，且构建/测试脚本已有断言护栏） |
| 是否仍有 Docker 专用配置、脚本、文档？ | **有**：字段名 `image`/`image_ref`/`image_digest`、API 键 `container`、日志文件名 `container.log`、`ContainerStatus` 类名、若干用户可见文案、9 个文档共 216 处措辞 |
| `image` / `image_ref` 能否安全迁移？ | **能**：语义已核实（存的是**游戏本体目录路径**），有明确迁移路径与安全性论证（§2） |
| 是否存在不能贸然删除的共享代码？ | **有**（§7 D 类），其中 `container.log` 改名与 API 键改名有真实兼容风险 |

---

## 1. 全仓库 Docker 残留清单

### 1.1 真实 Docker 实现 / 调用：**0 处**

| 检查项 | 结果 |
| :--- | :--- |
| `server/mgr/compose_mgr.py`、`server/mgr/docker_runtime.py` | 不存在（构建脚本已断言必须不存在） |
| `subprocess` 调用 `docker` / `docker compose` | 无 |
| `/var/run/docker.sock`、`docker.sock`、`DOCKER_HOST`、`DOCKER_API_VERSION` | 无 |
| `import docker`（SDK）、`containerd`、`podman` | 无 |
| `config/resource` 里的 `docker-project` | 无 |
| `fnpack/cmd`、`fnpack/wizard`、`fnpack/manifest` 中 `docker|compose` | **0 处** |
| 前端（html/js/css）请求 Docker 相关接口 | 无 |

**现有护栏（应保留并扩展）**：
- `tools/build-fpk.sh`：打包后断言包内不存在 `server/mgr/compose_mgr.py` / `docker_runtime.py`；
- `tools/run-tests.sh`：断言源码与产物中 `docker|compose` 字样为 0，且生命周期脚本/向导/manifest 为 0。

> ⚠️ **护栏不足（阶段 2 必须扩展）**：现有护栏只匹配英文 `docker|compose`，
> **不覆盖** `容器` / `镜像` / `container` / `image` / `image_ref` 字段与文案 ——
> 所以"搜索不到 docker 字样"并不能证明清理完成，这正是需求担心的情况。

### 1.2 命中统计（`fnos-fpk/` 共 345 行）

| 性质 | 数量 | 说明 |
| :--- | ---: | :--- |
| doc | 216 | 9 个 md 文档中的措辞 |
| string（用户可见文案） | 57 | 必须改 |
| test | 32 | 测试里的 `container` 段与断言 |
| code（标识符） | 27 | 字段名/类名/函数名/字典键 |
| comment | 10 | 代码注释 |
| exec | 3 | **全部为误报**：2 处是需求文档式表格文字，1 处是 `test_native.py` 里**禁止**这些字样的断言 |

### 1.3 用户可见文案（必须修改）

| 文件:行 | 现文案 | 真实含义 → 建议 |
| :--- | :--- | :--- |
| `www/static/app.js:1214` | `镜像无 Healthcheck（由管理器判定）` | 无应用内健康检查 → `由管理器判定（无应用内探针）` |
| `www/static/app.js:1216` | `['镜像', st.image \|\| '—']` | 实际是游戏本体目录 → `['游戏本体目录', …]` |
| `www/static/app.js:1217` | `['游戏本体目录', st.image \|\| '—']` | **与上一行取同一个字段**（重复且语义混乱）→ 与上一行合并 |
| `www/static/app.js:1212` | `['启动器', st.exists ? (st.image \|\| 'StardewModdingAPI') : '（不存在）']` | 用"路径"当启动器名 → 固定为 `StardewModdingAPI` |
| `www/static/app.js:1268` | `拉取上游镜像并启动游戏服务器` | 已无镜像可拉 → `重建运行环境并启动游戏服务器` |
| `www/static/app.js:1988` | `['镜像引用', m.image_ref \|\| s.image_ref]` | 存的是游戏目录 → `['游戏本体目录', …]` |
| `orchestration.py:476` | `ctx.step("检查容器状态", 10)` | → `检查运行状态` |
| `orchestration.py:479` | `ctx.log("容器已在运行，执行健康检查")` | → `游戏进程已在运行，执行健康检查` |
| `orchestration.py:507` | `TaskFailure("停止超时：容器仍在运行")` | → `停止超时：游戏进程仍在运行` |
| `orchestration.py:590` | `ctx.step("重启容器以加载新存档", 75)` | → `重启游戏以加载新存档` |
| `diagnostics.py:38` | `MOD 要求的 SMAPI 版本高于镜像内版本时必定加载失败` | → `…高于当前 SMAPI 版本时…` |
| `diagnostics.py:238` | `若容器刚启动，请等待 1-3 分钟…` | → `若游戏刚启动，请等待 1-3 分钟…` |
| `api_handlers.py:556` | `原生部署没有镜像摘要；版本由 … 标识` | 属"解释已不存在的概念" → 简化为 `版本由游戏版本 + SMAPI 版本 + 内容哈希标识` |
| `api_handlers.py:730-734` | `上游发行的游戏镜像内已含完整游戏本体` / `ref: … 发行镜像 :v26.5` | 描述**上游 Docker 镜像**作为审计基线 → 若保留基线信息，改为"上游发行包"；否则删除该段 |
| `api_handlers.py:987` | `以下是从上游镜像里核实的事实…本项目不依赖该镜像运行` | 同上 → 改为"上游发行包"或删除 |
| `snapshots.py:54` | `"pre-image-update": "更新运行镜像前"` | **只改显示文案** → `更新游戏本体前`（键名保留，见 §2.1） |
| `versions.py:259/267` | `原生部署没有镜像摘要，因此不存在「按镜像回滚」…` | 属历史概念解释 → 改为直接说明能力：`回滚通过快照完成` |

### 1.4 代码标识符（必须修改）

| 位置 | 现状 | 处置 |
| :--- | :--- | :--- |
| `runtime_base.py:29` | `class ContainerStatus` | 改名为 `RuntimeStatus`（或保留别名，见 §6 风险 3） |
| `runtime_base.py:39-40` | `image: str`、`image_digest: str` | 改为 `game_dir`，删除 `image_digest` |
| `runtime_base.py:7,32` | docstring「字段名沿用早期容器时代命名，避免上层大改」 | 随改名删除该说明 |
| `native_runtime.py:620` | `image=f"native:{self.game_root}"` | 改为 `game_dir=str(self.game_root)`（去掉 `native:` 前缀） |
| `native_runtime.py:1288-1289` | `"image_ref": f"native:{…}"`, `"image_digest": ""` | 改为 `"game_dir": str(...)`，删除 digest |
| `api_handlers.py:135,213` | 响应键 `"container": st.to_dict()` | 改为 `"runtime"`（前端与测试同批改） |
| `api_handlers.py:228,240` | 日志 `source` 默认值与回显 `"container"` | 改为 `"runtime"`（**注意文件名问题见 §6 风险 1**） |
| `api_handlers.py:608,622` | `allowed_sections: ["container","native","policy"]` 与过滤 | **`container` 是死配置段**：`ALLOWED_CONFIG_FIELDS` 只有 `native`/`policy`，提交 `container` 必然报错 → 删除该别名 |
| `logmod.py:68` | `"container": "container.log"` | 日志源键与文件名（见 §6 风险 1） |
| `util.py:150` | `validate_image_ref()` + 3 条「镜像引用…」报错 | **死代码**（全仓库无调用者）→ 直接删除 |
| `versions.py:39-40,54,113` | `image_ref` 存游戏目录、`image_digest` 恒空 | 迁移为 `game_dir`，删 digest（§2） |
| `snapshots.py:108-109,126,311-312,353,431-432` | manifest 与 DB 的 `image_ref`/`image_digest` | 读旧键回退 + 写新键（§2） |
| `db.py:80-81,93-94` | `snapshots`/`versions` 表列 `image_ref`/`image_digest` | 加新列并回填，旧列保留（§2.3） |
| `orchestration.py:282-306` | SQL 与响应里的 `image_ref`/`image_digest` | 同步改名 |
| `snapshots.py:54`（reason 码，落盘在 `snapshots.reason` 列） | `pre-image-update` | **保留键**，只改文案 |

### 1.5 代码注释（10 处，建议一并修正）

| 位置 | 现注释 | 问题 |
| :--- | :--- | :--- |
| `config.py:48` | `SAVES_DIR = … # 存档（挂到容器 /root/.config/StardewValley/Saves）` | 原生实现是**符号链接**到 `$HOME/.config/StardewValley/Saves`，不是挂载 |
| `config.py:49` | `MODS_DIR = … # MOD（挂到容器 /content/Stardew Valley/Mods）` | 同上，原生是符号链接 |
| `config.py:56` | `ARCHIVE_DIR = … # 版本档案（镜像/游戏/SMAPI/MOD 版本记录）` | "镜像" |
| `config.py:162` | `# 审计时从上游镜像里核实的版本…` | 上游镜像 → 上游发行包 |
| `versions.py:3,4,13,15` | `没有镜像摘要` / `从容器外的真实二进制里读` / `而不是回滚镜像` | 历史措辞 |
| `smapi.py:446` | `上游镜像那份游戏本来就装过 SMAPI` | 历史措辞 |
| `runtime_base.py:7,32,132` | `ContainerStatus`/容器时代命名/镜像摘要 | 随改名删除 |
| `tools/build-fpk.sh:187` | `# 容器实现必须已移除` | 护栏注释，可保留（说明历史） |

### 1.6 文档（216 处，9 个文件）

| 文档 | 命中 | 判定 |
| :--- | ---: | :--- |
| `docs/audit-report.md` | 109 | **迁移史文档**：记录"已移除 Docker"的过程 → 保留（这是审计证据），但需注明"仅历史" |
| `docs/test-report.md` | 27 | 历史测试报告 → 保留 |
| `docs/architecture.md` | 17 | **描述当前架构却用容器术语 → 必须改写** |
| `docs/platform-notes.md` | 14 | 含上游容器布局对比 → 拆分：当前架构部分改写，上游基线部分标注"上游资料" |
| `docs/native-deployment.md` | 12 | 混有容器对比 → 改写 |
| `docs/install-guide.md` | 10 | 改写 |
| `docs/known-issues.md` | 7 | 历史问题记录（I-* 编号）→ 保留，新增说明 |
| `docs/licensing.md` | 6 | 含上游镜像内软件的许可说明 → 需核实法律表述准确性 |
| `docs/version-archive.md` | 4 | 改写 |
| `README.md` | 11 | 改写 |

### 1.7 上游内容（不在改造范围，仅登记）

| 路径 | 内容 | 处置 |
| :--- | :--- | :--- |
| `oneclick-script/docker/`（3 个文件）+ `run.sh` | 上游 2.x 的 Docker 部署脚本 | **不改**（上游资产，fork 可合并性） |
| `docs/`（上游 16 处 docker/compose） | 上游 v2/容器手册 | **不改** |
| `Mods/`、`src/` | 上游 MOD 源码与主程序 | **不改** |

> 建议：在仓库 README 明确一句"本 fork 的应用部分（`fnos-fpk/`）为原生部署，不依赖 Docker；上游目录保留原样以维持可合并性"，避免后续误判。

---

## 2. 历史字段与数据迁移清单

### 2.1 字段真实语义（已逐处核实）

| 字段 | 存的是什么 | 证据 | 迁移目标 |
| :--- | :--- | :--- | :--- |
| `versions.image_ref` | **游戏本体目录路径字符串** | `versions.py:113 rv.image_ref = str(root)` | `game_dir` |
| `versions.image_digest` | **恒为空字符串**（有写入点但只写空值，无任何赋值逻辑） | `versions.py:40` 注释；全仓库无 `image_digest = <非空>` 赋值 | **不迁移**为 `content_hash`；删除 |
| `ContainerStatus.image` | `"native:<游戏本体目录>"` | `native_runtime.py:620` | 改为 `game_dir`（去前缀） |
| `snapshots.image_ref`（DB + manifest） | 同上（快照时从 runtime 取得） | `native_runtime.py:1288` | `game_dir` |
| `snapshots.image_digest` | **恒为空** | `native_runtime.py:1289` | 删除 |
| `snapshots.reason` 值 `pre-image-update` | 持久化的理由码（DB 列 `reason`） | `snapshots.py:54` | **保留键**，只改显示文案 |
| `config.json` | 段名是 `native` / `policy` | 真机 `config.json` 顶层键实测 | **无需迁移** |

> **对 `content_hash` 的明确判定**：需求要求"只有确认 `image_ref` 的值确实是内容指纹时，才能迁移为 `content_hash`"。
> 实测结论：`image_ref` 存的是**路径**（→ `game_dir`），`image_digest` 是**空值**（→ 不是指纹）。
> 因此**不进行 `content_hash` 迁移**。已有的真实指纹是 `mods_hash` / `saves_hash`（保留不动）。
> 若希望版本档案具备"游戏本体内容指纹"，那是**新功能**（对游戏目录做内容哈希），应作为独立提案，不属于本次迁移。

### 2.2 持久化位置（5 处）

| # | 位置 | Docker 字段 | 现有数据是否存在 |
| :-- | :--- | :--- | :--- |
| 1 | SQLite `versions` 表 | `image_ref`（NOT NULL）、`image_digest`（NOT NULL） | 有（用户运行过即写入） |
| 2 | SQLite `snapshots` 表 | `image_ref`、`image_digest`（有默认值） | 有 |
| 3 | 磁盘 `data/archives/version-<stamp>-<id>.json` | 键 `image_ref`/`image_digest` | 有 |
| 4 | 磁盘 `data/snapshots/<id>/manifest.json` | 键 `image_ref`/`image_digest` | 有 |
| 5 | `manager/config.json` | 无 | — |

### 2.3 迁移方案（含安全性论证）

**原则**：新增列/键 → 回填 → 停止读写旧字段 → 旧数据保留为审计记录；**不改动任何存档、MOD、Steam 凭据、快照 payload 内容**。

| 对象 | 方案 |
| :--- | :--- |
| SQLite `versions` | `ALTER TABLE versions ADD COLUMN game_dir TEXT NOT NULL DEFAULT ''` → `UPDATE versions SET game_dir = image_ref WHERE game_dir = '' AND image_ref <> ''` → 代码只读写 `game_dir`。**旧列 `image_ref`/`image_digest` 不删**（SQLite 删列受限 + 审计要求不破坏历史记录），但在 API 输出中隐藏 |
| SQLite `snapshots` | 同上（`game_dir` 加列 + 回填），`valid`/`checksum` 等校验列不动 |
| `archives/version-*.json` | **不重写历史文件**；读取时 `game_dir = data.get("game_dir") or data.get("image_ref", "")`；新写入只写 `game_dir` |
| `snapshots/<id>/manifest.json` | **不重写历史文件**；读取时新键优先、旧键回退；新快照只写 `game_dir`。**安全性已验证**：`verify()` 只遍历 `manifest["files"]` 逐个比对 payload 文件的 size/sha256，**manifest 自身不参与校验**，因此新增/改名键不会让历史快照失效；且已存在迁移钩子 `migrate_manifest()`（含 `manifest_version` 版本判定与"高版本拒绝解读"保护）可复用 |
| `pre-image-update` 理由码 | **不改键**（DB 里已有该值），只改显示文案；新快照可用新键，显示层做双键映射 |
| 迁移幂等性 | 所有步骤以"目标列为空才回填"为条件，可重复执行；迁移函数在 DB 打开时执行一次并记录 `settings` 表标记 |
| 无法确定含义的数据 | 按需求要求：**保留数据 + 报告**，不猜测、不静默丢弃（当前审计未发现语义不明的 Docker 字段） |

### 2.4 迁移不涉及的内容（明确排除）

- 存档（`data/Saves/**`）、MOD（`data/Mods/**`）内容；
- 快照 payload（`data/snapshots/<id>/payload/**`）与其 sha256 校验值；
- Steam 凭据（`PKGHOME/steamtools/.local/**`）、游戏本体文件；
- 任务/审计历史记录（`tasks`、`task_logs`、`audit` 表的文本内容）。

---

## 3. 当前原生运行架构说明

### 3.1 形态

管理器是**本机原生进程管理**：没有容器，直接启动「显示环境 → 窗口管理器 → VNC → 游戏进程（SMAPI）」。

### 3.2 进程清单

| 进程 | 作用 | 后端 |
| :--- | :--- | :--- |
| `xvfb` **或** `weston`+`xwayland` | 虚拟显示 | 软件渲染 / 核显（`display_backend` = auto/xvfb/weston，auto 时有核显即用核显） |
| `openbox`（可配） | 窗口管理器 | 游戏需要窗口管理器建窗 |
| `x11vnc` | VNC 服务 | 供 WebVNC / VNC 客户端 |
| `websockify`(+noVNC) | VNC → WebSocket | 浏览器访问 |
| `game`（`StardewModdingAPI`） | 游戏本体 + SMAPI | 前台进程，stdout 重定向到 `console.log` |

### 3.3 端口

| 端口 | 用途 |
| :--- | :--- |
| `18242` | 管理器面板（fnOS `service_port`，同时监听 UDS 供统一网关） |
| `24642/udp` | 游戏联机端口（**UDP**，健康判据必须用 UDP 检测） |
| `5900` | VNC |
| `5800` | WebVNC（noVNC + websockify） |
| `29103` | CommandWebUI（SMAPI 控制台，**无认证**） |

### 3.4 路径

| 变量 | 默认路径 | 说明 |
| :--- | :--- | :--- |
| `APPDEST` | `/vol1/@appcenter/valleyserver` | 代码（升级整体替换） |
| `PKGVAR` | `/vol1/@appdata/valleyserver` | 运行数据/日志 |
| `PKGHOME` | `/vol1/@apphome/valleyserver` | **升级保留**：配置、数据库、凭证、游戏本体 |
| `DATA_ROOT` | `PKGHOME/data` | 游戏数据根 |
| `GAME_FILES_DIR` | `DATA_ROOT/gamefiles` | 游戏本体（用户提供） |
| `SAVES_DIR` / `MODS_DIR` | `DATA_ROOT/Saves` / `DATA_ROOT/Mods` | 通过**符号链接**接到 `$HOME/.config/StardewValley/Saves` 与游戏的 `Mods/`（非容器挂载） |
| `SNAPSHOT_DIR` | `DATA_ROOT/snapshots` | 快照仓库 |
| `ARCHIVE_DIR` | `DATA_ROOT/archives` | 版本档案 JSON |
| `CONFIG_DIR` | `PKGHOME/manager` | 配置 + SQLite + 审计 |
| `LOG_DIR` | `PKGVAR/logs` | 日志 |

### 3.5 健康判据（现状）

`readiness()` 输出 4 个布尔层 + 一句话 phase/detail：

| 判据 | 含义 |
| :--- | :--- |
| `L1_game_process` | 游戏进程存活 |
| `L1_display_stack` | 显示栈（xvfb 或 weston+xwayland）就绪 |
| `L2_ports_listening` | VNC 端口监听 |
| `L3_smapi_loaded` | SMAPI 已加载（解析 console.log，只看最后一次启动横幅之后的日志） |

> **需求缺口**：现有 phase 是**单一状态**（如"运行中（未开服）"），需求要求把
> 「服务运行状态」与「农场开服状态」拆成**两个正交维度**。当前 `needs_save` 字段已能区分
> "有存档/无存档"，是拆分的基础，但尚未成为独立状态模型。→ 阶段 3 处理。

### 3.6 监管与自动化（现有行为，阶段 2/3 必须保留）

| 机制 | 默认 | 说明 |
| :--- | :--- | :--- |
| 后台监管 | 开 | 异常退出自动恢复（指数退避 + 最大重试次数） |
| 随 NAS 启动 | 开 | 开机自启服务端 |
| 启动后自动载档开服 | 开 | `set_multiplayermode 2` → `load_save <最新>`（ALOS 不会自己载档） |
| 自动快照 | 开（360 分钟） | 周期性 + 所有高风险变更前 |
| 崩溃循环保护 | 3 次 | 自动开服连续失败即停手 |

### 3.7 安全机制（必须保留）

会话 Cookie + CSRF；网关认证（`X-Trim-*` 头）；危险操作 `confirmRisk()`（含输入对象名）；
变更前自动快照（失败即中止变更）；审计记录；诊断包脱敏；快照一致性协议（锁定 → 复制 → 清单+SHA-256 → 校验）。

---

## 4. UI 页面 ↔ API 对照表

| 页面 | 路由 | 调用的接口 |
| :--- | :--- | :--- |
| 总览 | `#dashboard` | `GET /api/dashboard` |
| 控制台 | `#console` | `GET /api/server/status`、`GET /api/server/logs?source=&tail=&level=`、`GET /api/mods/smapi-log`、`POST /api/console/send` |
| 存档 | `#saves` | `GET /api/saves`、`GET /api/snapshots`、`POST /api/saves/import-plan`、`POST /api/saves/import`、`POST /api/saves/activate`、`POST /api/saves/restore`、`POST /api/saves/delete`、`GET /api/saves/export/download` |
| MOD | `#mods` | `GET /api/mods`、`POST /api/mods/install-plan`、`POST /api/mods/install`、`POST /api/mods/toggle`、`POST /api/mods/remove`、`GET /api/mods/upstream-bundle`、`POST /api/mods/upstream-bundle`、`GET /api/mods/smapi-log` |
| 快照 | `#snapshots` | `GET /api/snapshots`、`GET /api/snapshots/{id}`、`POST /api/snapshots/create`、`verify`、`restore`、`delete`、`policy`、`GET /api/snapshots/download` |
| 版本 | `#versions` | `GET /api/versions`、`GET /api/versions/updates`、`POST /api/versions/record`、`POST /api/versions/rollback`（**前端未用** `GET /api/versions/history`、`GET /api/versions/check`，但接口保留） |
| 配置 | `#config` | `GET /api/config`、`POST /api/config`、`GET /api/runtime`、`POST /api/runtime/xstack` |
| 任务 | `#tasks` | `GET /api/tasks`、`GET /api/tasks/{id}`、`POST /api/tasks/cancel` |
| 系统 | `#system` | `GET /api/system`、`/system/health`、`/system/deps`、`/system/audit`、`GET /api/server/logs`、`POST /api/diagnostics/bundle`、`GET /api/diagnostics/download`、`POST /api/native/gamefiles`、`POST /api/native/smapi`、`GET /api/native/smapi/releases`、`POST /api/native/steam/prepare`、`login`、`fetch`、`logout`、`GET /api/native/steam`、`/steam/qr`、`/steam/progress`、`POST /api/native/host-bootstrap`、`GET /api/native/gamefiles` |
| 关于 | `#about` | `GET /api/about`、`POST /api/logout` |
| 全局 | — | `GET /api/auth/state`、`POST /api/login`、`POST /api/auth/password`、`POST /api/server/start`、`stop`、`restart`、`install`、`recreate`、`POST /api/snapshots/create`（快捷） |

> 全部接口（含未在前端使用的）：`/api/health`（健康探针）。**阶段 2/3 不得删除任何接口。**

---

## 5. UI 新旧页面映射表（10 → 5 + 关于）

| 新一级入口 | 包含的旧页面 | 新子路由（建议） | 旧路由兼容 |
| :--- | :--- | :--- | :--- |
| **总览** | `#dashboard` | `#dashboard` | 原样 |
| **游戏管理** | `#console` + `#saves` + `#mods` | `#game/runtime`（运行与日志）、`#game/saves`、`#game/mods`、`#game/diagnose` | `#console`→`#game/runtime`；`#saves`→`#game/saves`；`#mods`→`#game/mods` |
| **备份与恢复** | `#snapshots`（主体） + 版本页的回滚入口 | `#backup`、`#backup/detail/{id}` | `#snapshots`→`#backup` |
| **设置与维护** | `#config` + `#system` + `#versions` | `#settings/runtime`（运行设置）、`#settings/display`（显示与访问）、`#settings/backup`（备份策略）、`#settings/environment`（游戏与运行环境：本体/SMAPI/显示组件/Steam）、`#settings/version`（版本管理）、`#settings/advanced`（高级）、`#settings/audit`（维护记录） | `#config`→`#settings/runtime`；`#system`→`#settings/environment`；`#versions`→`#settings/version` |
| **任务与诊断** | `#tasks` + `#system` 的诊断部分 | `#tasks`、`#tasks/diagnose` | `#tasks` 原样 |
| （次级）**关于** | `#about` | 侧栏底部入口 | 原样 |

**路由兼容要求**：统一的路由解析器（`#a/b` → 页面 + 子标签 + 页面标题 + 导航激活态），
未知或旧路由做**重定向映射**而非 404；旧书签、`location.hash` 内部跳转、以及
`#system`/`#config` 之间现有的互相跳转都要跟着改到新地址。

**功能保留核对**（不得因合并而丢失）：

| 原页面能力 | 去向 |
| :--- | :--- |
| 控制台：日志源/等级筛选/自动刷新/下载 | 游戏管理 › 运行与日志 |
| 控制台：发送指令（含崩溃风险提示） | 游戏管理 › 运行与日志（默认折叠） |
| 控制台：崩溃诊断 | 游戏管理 › 诊断（独立区域） |
| 存档：导入/导出/切换/恢复单个/删除 + 运行中限制 | 游戏管理 › 存档 |
| MOD：已安装/上传安装/上游包/依赖诊断/启停/卸载 | 游戏管理 › 模组（三区：已安装 / 安装 / 服务器组件） |
| 快照：创建/校验/详情/下载/恢复/删除/策略/清理 | 备份与恢复 |
| 版本：更新检查/记录指纹/回滚入口 | 设置与维护 › 版本管理（回滚**链接**到快照，不复制实现） |
| 配置：4 组设置 | 设置与维护 › 运行设置 / 显示与访问 / 备份设置 |
| 系统：路径/环境/占用/安装操作/审计/诊断包/日志入口 | 设置与维护 › 游戏与运行环境 / 高级 / 维护记录 + 任务与诊断 › 诊断 |
| 任务：队列/进度/取消/回滚结果 | 任务与诊断 |

---

## 6. 可能影响原生运行的风险项

| # | 风险 | 说明 | 建议处置 |
| :-- | :--- | :--- | :--- |
| R1 | **`container.log` 文件名** | 该文件是**真实落盘的日志文件**（`LOG_DIR/container.log`），日志轮转、诊断包、`server/logs` 默认 source、前端日志标签、测试都可能引用它。改名会让**旧日志文件成为孤儿**，并可能让正在追加写入的 fd 指向旧 inode | **方案 A（推荐，零风险）**：文件名保留，仅改界面标签与日志源键的显示名；**方案 B**：改名 + 启动时若旧文件存在则追加迁移（原地重写，不可 rename） |
| R2 | **API 键 `container` → `runtime`** | 前端（`srv.container`）、`server/logs` 的 source 默认值、测试（`test_security.py`）都依赖它 | 输出新键 + **一个版本周期内同时输出旧键**（或明确同步改前端与测试后一次性切换）；`section` 名 `container` 是**死配置段**可直接删 |
| R3 | **`ContainerStatus` 类名** | 被 `native_runtime`、`supervisor`、`api_handlers`、测试、前端（`st.mode`）消费；是 `Runtime` 基类的返回契约 | 类名可改（纯内部），但要**一次改全**；若担心第三方（无）可留 `ContainerStatus = RuntimeStatus` 别名 |
| R4 | **快照 manifest 键** | 历史快照的 `manifest.json` 含 `image_ref`；**已验证 manifest 不参与自身校验**，所以只读键回退即可，**不得重写历史文件**（重写虽不破坏校验，但会改动审计物证） | 读旧键回退 + 新快照写新键；复用 `migrate_manifest()` |
| R5 | **DB 删列** | SQLite 删列有版本限制，且需求要求"不破坏审计记录" | 只加新列 + 回填，旧列保留但不再读写；在文档中说明 |
| R6 | **护栏脚本** | `run-tests.sh` 断言源码中 `docker|compose` 为 0；若我们保留"历史说明"注释会触发失败 | 阶段 2 同步调整护栏规则：区分"真实现/调用"（必须 0）与"历史说明文档"（允许），并**扩展覆盖** `容器/镜像/container/image` |
| R7 | **测试引用 `container` 段** | `test_security.py`（脱敏用例）、`test_native.py`（旧配置忽略用例） | 前者改用 `native`；后者**必须保留**（它验证旧 Docker 配置不会让应用起不来） |
| R8 | **上游目录误改** | `docs/`、`oneclick-script/`、`Mods/`、`src/` 是上游资产 | 明确排除；批量替换脚本必须限定在 `fnos-fpk/` 内 |
| R9 | **原生启动流程** | 需求要求不得改变已验证的启动/显示/Steam 流程 | 阶段 2 只做**命名与文案**改动，任何涉及 runtime 行为的改动都必须单独评审 + 真机验证 |
| R10 | **窗口管理器 / 显示后端 / 额外启动参数** | 属于已验证有效的运行参数 | 不因清理或 UI 重构而改动（阶段 3 只调整它们在界面上的**归组**） |
| R11 | **UI 合并导致重复实现** | 版本页回滚若复制快照实现 | 版本页只做**链接**到备份与恢复 |
| R12 | **章节名 `native` vs `container`** | 落盘 `config.json` 用 `native`（实测），API 却 advertise `container` | 删除 `container` 别名；`native` 保持 |

---

## 7. 四类分类结论

### A. 已确认不再使用的 Docker 代码（可安全删除）
- `util.validate_image_ref()`（**无任何调用者**）；
- API 的 `container` 配置段别名（`allowed_sections` / 过滤条件）—— `ALLOWED_CONFIG_FIELDS` 里根本没有该段；
- `image_digest` 全链路（DB 列、dataclass 字段、API 输出、UI 展示）—— 只被写为空字符串、无赋值来源，属死字段；
- 所有"镜像"语义的用户可见文案（§1.3）；
- `app.js` 里重复的「镜像 / 游戏本体目录」两行（取同一字段）；
- 注释中的容器历史解释（§1.5，除构建护栏注释外）。

### B. 仍在运行的原生实现（**不可改行为**）
- `native_runtime` 全流程（显示栈、游戏启停、stdin/控制台、健康判据、残留清理）；
- `supervisor`（开机自启、崩溃恢复、退避、自动载档开服）；
- `snapshots`（一致性协议、校验、恢复、保留策略、pre-snapshot）；
- `tasks`（队列、进度、取消、回滚）、`audit`、`diagnostics`；
- `steamtools`（登录/下载分离）、`smapi`、`gamebody`；
- 前端所有安全确认与任务化交互。

### C. 仅命名遗留、但影响数据兼容（**必须迁移，不能只改名**）
- `versions.image_ref`（DB 列 + `archives/version-*.json` 键）；
- `snapshots.image_ref` / `image_digest`（DB 列 + `manifest.json` 键）；
- `ContainerStatus.image`（含 `native:` 前缀的伪标识）；
- `snapshots.reason` 值 `pre-image-update`（**保留键**，只改文案）；
- 日志文件名 `container.log` 与日志源键 `container`（见 R1/R2）；
- API 响应键 `container`（见 R2）。

### D. 不能贸然删除的共享代码 / 内容
- `runtime_base.Runtime` / `ContainerStatus` 返回契约（被 supervisor、api、测试消费）→ 改名需一次改全或留别名；
- `migrate_manifest()` 与 `manifest_version` 保护逻辑（旧快照恢复的前提）；
- 旧配置键忽略逻辑（`test_old_config_with_docker_keys_ignored`）—— 这是**兼容性保障**，必须保留；
- 日志轮转/诊断包对日志文件的引用（R1）；
- 上游 `docs/`、`oneclick-script/`、`Mods/`、`src/`（fork 可合并性）；
- 显示栈组件（Xvfb/Weston/x11vnc/websockify/noVNC）—— 需求明确不得以移除 Docker 为由删除。

---

## 8. 阶段 2 执行清单（待确认后执行）

| 顺序 | 动作 | 涉及文件 |
| :-- | :--- | :--- |
| 1 | 删除死代码与死配置段 | `util.py`、`api_handlers.py`（allowed_sections/过滤） |
| 2 | 运行状态字段改名（`game_dir`，删 `image_digest`） | `runtime_base.py`、`native_runtime.py`、`orchestration.py`、`snapshots.py`、`versions.py` |
| 3 | **数据迁移**（DB 加列回填 + JSON 读旧键回退） | `db.py`（迁移函数）、`versions.py`、`snapshots.py` |
| 4 | API 键改名（`container`→`runtime`，含兼容期） | `api_handlers.py`、`logmod.py`、`app.js` |
| 5 | 用户可见文案清理（含任务步骤/异常信息） | `app.js`、`orchestration.py`、`diagnostics.py`、`api_handlers.py` |
| 6 | 注释与文档改写（限定 `fnos-fpk/`） | 代码注释、`fnos-fpk/docs/*.md`、`fnos-fpk/README.md` |
| 7 | 护栏与测试扩展 | `tools/run-tests.sh`（覆盖 `容器/镜像/container/image` + 真实调用审计）、`tests/*` |
| 8 | 验证 | 语法检查、全量测试、沙箱安装、真机启动/停止/快照/迁移回归 |

> 阶段 2 完成后进入阶段 3（UI 重构 5 入口 + 路由兼容 + 状态模型 + 移动端），阶段 4 全面测试。

---

## 附：本报告的证据产出物

| 文件 | 内容 |
| :--- | :--- |
| `/tmp/docker_scan.py` | 扫描器（可复跑：`python3 docker_scan.py <dir> <out.json>`） |
| `/tmp/docker-scan-app.json` | `fnos-fpk/` 全部命中明细（345 行，含文件/行号/性质/文本） |
| 本文档 | 审计结论与方案 |
---

## 附录 B：执行层 / 产物层 Docker 依赖审计（硬证据）

需求明确要求"不得只通过搜索字符串就宣称 Docker 已彻底清除"。因此另做**执行层**与**产物层**核验
（脚本：`/tmp/docker_exec_audit.py`，可复跑）。

### B.1 源码执行层

| 问题 | 方法 | 结果 |
| :--- | :--- | :--- |
| 有无进程创建调用能执行 docker？ | 匹配 `subprocess.*` / `os.system` / `os.popen` / `os.exec*` / `shutil.which` / `execvp` 等调用行，且该行含 `docker|compose|podman|containerd` | **0 处 ✅** |
| 有无 Docker socket / API 访问？ | 匹配 `docker.sock` / `DOCKER_HOST` / `DOCKER_API_VERSION` / `/var/run/docker` | 1 处，**且是本文档自身列举这些词**（自指误报）→ 实际代码 **0 处 ✅** |
| 有无 Docker 运行时依赖？ | `import docker/podman` + `config/resource` 资源类型 | **0 处 ✅**；`config/resource` 只有 `data-share`（共享目录声明），无 `docker-project` |

### B.2 打包产物层（实测 0.2.12 的 .fpk）

| 检查 | 结果 |
| :--- | :--- |
| 包顶层结构 | `ICON.PNG` `ICON_256.PNG` `app.tgz` `cmd` `config` `manifest` `wizard` |
| `server/mgr/compose_mgr.py` | **不在包内 ✅** |
| `server/mgr/docker_runtime.py` | **不在包内 ✅** |
| app.tgz 条目数 | 39 |
| 包内 docker/compose 命名的文件 | **无 ✅** |
| 包内文本内容中的 `docker` 字样 | **0 行 ✅** |

### B.3 由此得到的一个新注意事项（补充 R6）

审计文档本身会**自指**命中扫描（例如本文档列举了 `docker.sock` 等字样）。
阶段 2 扩展护栏时必须**排除审计/历史文档**，否则护栏会被自己的报告判红 ——
这也是"只靠字符串搜索会误判"的反面例证。
---

## 阶段 2 执行结果（本轮已完成，供阶段 3 衔接）

| 步骤（报告 §8） | 状态 | 说明 |
| :--- | :--- | :--- |
| 1 删除死代码与死配置段 | ✅ | 删除 `util.validate_image_ref()`（无调用者）；删除 API 的 `container` 配置段别名 |
| 2 运行状态字段改名 | ✅ | `ContainerStatus` → `RuntimeStatus`；`image` → `game_dir`；删除 `image_digest` 链路 |
| 3 数据迁移 | ✅ | 加列 + 回填 + **必要时重建表**；JSON 读旧键回退；`docs/field-migration.md` 记录；5 项专项测试 |
| 4 API 键改名 | ✅ | 响应键 `container` → `runtime`；日志 source 默认 `runtime`，旧值 `container` 仍兼容；**磁盘文件名 `container.log` 保留**（R1 方案 A） |
| 5 用户可见文案清理 | ✅ | 前端 5 处、后端任务步骤/异常 4 处、诊断 2 处、运行信息表去重 |
| 6 注释与文档改写 | ✅ | 代码注释全部修正；9 个文档清理，仅保留审计/历史/许可事实与上游对比 |
| 7 护栏与测试扩展 | ✅ | 护栏升级为**两层**：①真实实现/调用必须 0；②旧命名仅限白名单（按文件设上限） |
| 8 验证 | ✅ | Python 语法、全量测试、沙箱安装、迁移专项测试 |

### 迁移中发现的真实缺陷（迁移测试抓到，非推测）

老库 `versions.image_ref` 为 `TEXT NOT NULL` 且无默认值，而新代码不再写该列 →
**升级后任何新插入都会 `NOT NULL constraint failed`**。已通过"建新表→搬数据→换名"修复，
并把该场景固化为测试。

### 阶段 2 后的残留（均为**正当保留**）

| 位置 | 残留 | 为什么保留 |
| :--- | :--- | :--- |
| `server/mgr/db.py` | 旧列名（迁移 DDL 与回填 SQL） | 不引用旧列名就无法迁移 |
| `server/mgr/logmod.py` | `container.log` 文件名 + 旧参数别名 | 改文件名会让旧日志成孤儿（R1） |
| `server/api_handlers.py` | 旧日志参数值兼容、旧快照键回退 | 旧书签/旧数据兼容 |
| `server/mgr/snapshots.py` | 旧 manifest 键回退 | 不重写历史快照文件 |
| `docs/audit-report.md`、`test-report.md`、`phase1-audit.md`、`known-issues.md` | 审计与历史记录 | 需求要求保留审计证据 |
| `docs/licensing.md` | 上游发行物内的许可证事实 | 法律事实，必须准确 |
| `README.md`、`platform-notes.md` | 与上游容器方案的**对比**及"已移除/不适用"说明 | 帮助读者理解差异，非本项目的运行描述 |

### 遗留待办（不属于阶段 2）

- 阶段 3：UI 5 入口重构、路由兼容、双状态模型、术语统一、移动端三档；
- `content_hash`（游戏本体内容指纹）如需，作为**独立提案**（新功能，非迁移）。
---

## 阶段 3 执行结果（UI 重构，已完成）

| 需求项 | 结果 |
| :--- | :--- |
| 10 项 → 5 项一级导航 | ✅ 总览 / 游戏管理 / 备份与恢复 / 设置与维护 / 任务与诊断 + 侧栏底部「关于」 |
| 页内二级分区 | ✅ 共 14 个二级标签（游戏管理 4、备份 1、设置 7、任务 2） |
| 路由兼容 | ✅ `parseRoute()` 纯函数映射旧路由；`history.replaceState` 规范化；未知路由回落且不死循环（15 条断言） |
| 统一状态模型 | ✅ `readiness.service.state` 与 `readiness.farm.state` 两个正交维度（各 7 / 4 个取值），带 hint；四层健康判据保留 |
| 状态视觉 | ✅ `.state-badge` **图标 + 文字 + 颜色**同时表达；侧栏底部同时显示两个维度 |
| 术语统一 | ✅ 模组 / 存档 / 快照 / 恢复存档 / 恢复整个快照 / 服务器组件 / 游戏进程·显示环境·运行环境 |
| 设置标签 ≤6 汉字 | ✅ 已逐项精简（端口名保留英文产品名）；说明 ≤20 字 |
| 设置与安装操作分离 | ✅ 普通配置四项分栏（运行/显示/备份/高级），安装·下载·升级集中在「游戏与运行环境」 |
| 配置文件跨标签保存 | ✅ 草稿放在 `state.cfgDraft`，任一二级页都能「保存并应用」，并显示「有 N 项未保存」 |
| 移动端三档 | ✅ >1100 桌面 / 720–1100 平板 / <720 手机；手机端侧栏变顶部导航、表格转卡片（`data-label`）、长路径可换行 |
| 不删除任何 API/功能 | ✅ 全部接口与操作在新 UI 中可到达 |
| 不引入框架/构建 | ✅ 仍是手写原生 JS |

### 阶段 3 的验证

* 前端测试逐页渲染 **24 条路由**（新旧都有）全部通过；
* 新增断言：路由映射 15 条、一级导航数量、二级标签数量、双状态模型齐备、表格卡片化钩子、
  三档断点与样式钩子、既有安全约束未丢失、三步不自动串联；
* 该轮**发现的自身缺陷**：批量替换时 `String.replace` 命中了首个匹配（配置页而非系统页），
  导致多页出现未定义变量 `want` —— 已按行号撤回并只保留控制台/系统页的守卫（有 `node --check` 与逐页渲染验证兜底）。
* `stop()` 重构为 `stop()` + `_stop_locked()` 后，3 个结构断言测试同步更新（顺序/合并/默认后端）。
