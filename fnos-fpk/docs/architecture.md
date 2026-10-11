# 架构与数据目录说明

## 1. 设计目标与硬约束

| # | 约束 | 落实方式 |
| :--- | :--- | :--- |
| 1 | 管理器与游戏都**原生运行**于飞牛 OS，无容器依赖 | FPK 形态：`cmd/main` 直接拉起 Python 服务（纯标准库，无 venv/pip） |
| 2 | 游戏客户端 / SMAPI / MOD / 图形 / VNC 运行在**独立容器** | 由管理器生成并驱动项目专属 compose 工程 |
| 3 | 游戏数据与 FPK 生命周期**完全分离** | 全部持久数据放 `$TRIM_PKGHOME`（升级/默认卸载保留） |
| 4 | 不改造为 3.x、不重写游戏逻辑 | 只调用上游镜像与 MOD，不修改上游源码 |
| 5 | 统一任务队列 | 所有写操作都经 `mgr.tasks.TaskManager` |
| 6 | 失败路径必须明确 | 快照失败即中止；回滚失败保留现场并显式标记 |
| 7 | 只提供白名单操作 / 不开放任意 Shell | 不依赖任何容器运行时；命令一律 argv 列表 |
| 8 | 高风险操作必先快照 | 由 `orchestration._auto_snapshot` 强制 |

## 2. 运行环境（原生）

管理器直接在本机运行游戏。进程模型：

```text
Xvfb :N ──┬─ openbox（窗口管理器，需要 XDG_DATA_DIRS）
          └─ x11vnc :5900 ── websockify :5800（noVNC，可选）

StardewModdingAPI（管理器作为父进程，stdout/stderr → console.log）
```

| 项 | 做法 |
| :--- | :--- |
| 显示栈来源 | `apt-get download` + `dpkg -x` 解包到 `$TRIM_PKGHOME/xstack/`，**不写系统包数据库** |
| 运行时依赖 | 游戏与 SMAPI 是 .NET 6 **自包含**发布，**不需要安装 .NET** |
| 游戏本体 | 由用户自备（本项目不再分发），经「系统」页上传并做 ZIP 安全检查 |
| 进程识别 | `resolve_pid()`：PID 文件 → `/proc/pid/exe`（须指向捆绑目录）→ cwd/cmdline |
| 健康判据 | L1 显示栈 + 游戏进程 → L2 VNC 端口 → L3 SMAPI 日志 |

`Runtime` 接口（`mgr/runtime_base.py`）：`status / start / stop / restart / up / down /
logs / console_log / stats / detect / validate / readiness / prepare /
write_selected_save / version_info`。

## 3. 组件

```text
┌────────────────────────── 飞牛统一网关 (/app/valleyserver/) ──────────────────────────┐
│  nginx location /app/ → unix:/var/run/trim_http_cgi.socket（不剥前缀）                │
│  注入可信 Header：X-Trim-Userid / X-Trim-Isadmin / X-Trim-Username                  │
└───────────────────────────────────┬──────────────────────────────────────────────────┘
                                    │
                    $TRIM_APPDEST/valleyserver.sock (0666)   +   0.0.0.0:18242 (TCP)
                                    │
┌───────────────────────────────────▼──────────────────────────────────────────────────┐
│ run.py  HTTP 服务                                                                     │
│  ├─ 前缀处理（自己剥 /app/valleyserver，裸前缀 307）                                   │
│  ├─ 鉴权（网关 Header 或本地口令 + HttpOnly Cookie + CSRF）                            │
│  ├─ 静态资源（www/，index.html no-store，避免升级后界面没变）                          │
│  └─ 路由 → api_handlers.py                                                            │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ orchestration.py  任务处理器（每个动作 = 一个任务类型）                                 │
│  ├─ 前提校验（显示栈/游戏本体/磁盘/运行状态/目标合法性）                                         │
│  ├─ _auto_snapshot()  高风险操作强制先快照（失败即中止）                                │
│  └─ 失败 → rollback 处理器 → 保留现场                                                  │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ 领域模块 (mgr/)                                                                        │
│  config util db tasks snapshots saves mods versions native_runtime xstack runtime_base│
│  diagnostics  supervisor  logmod                                                       │
└───────────────────────────────────┬──────────────────────────────────────────────────┘
                                    ▼
     受管进程：Xvfb / openbox / x11vnc / websockify / StardewModdingAPI
```

### 2.1 为什么后端只用 Python 标准库

* 飞牛 OS 应用中心可能在没有外网的 NAS 上安装，`pip install` 会失败并让应用装不上；
* 本项目功能（HTTP、SQLite、ZIP、tar、subprocess、hashlib）标准库全覆盖；
* 结果：**零网络依赖、零 venv、安装不可能因为依赖失败而中断**。

解释器优先级：`$TRIM_APPDEST/python312`（install_dep_apps 提供）→
`/var/apps/python312/target/bin/python3` → `python3.12` → `python3`，且要求 ≥ 3.10。

### 2.2 为什么需要「自建 entrypoint」

上游镜像的 `startapp.sh` 有三个问题（见审计报告 P2/P5 与 §2.1）：
VNC 密码硬编码 `041041`、SMAPI 控制台输出不落盘、无健康检查。
管理器生成自己的 `entrypoint.sh` 挂在容器里，解决这三点，并顺带支持
「从管理器选择的当前存档自动加载」（`--load-save`）。

## 4. 数据目录

```text
/vol1/@appcenter/valleyserver/          $TRIM_APPDEST   代码    【升级整体替换】
├── server/                             后端源码
├── www/                                前端静态资源
├── valleyserver.sock                   网关 socket（0666）
└── manifest

/vol1/@appdata/valleyserver/            $TRIM_PKGVAR    运行数据 【升级保留】
├── logs/
│   ├── manager.log                     服务日志（5 MiB 上限，原地 tail-trim）
│   ├── task.log                        任务日志
│   ├── console.log                     SMAPI 控制台输出
│   └── audit.log
└── valleyserver.pid

/vol1/@apphome/valleyserver/            $TRIM_PKGHOME   数据     【升级/默认卸载保留】
├── initial-password.txt                初始口令（600）
├── xstack/                             原生模式的虚拟显示栈（捆绑，不改系统）
├── gamehome/                           原生模式下游戏的 $HOME
│   └── .config/StardewValley/
│       ├── Saves -> ../../data/Saves   存档（符号链接到数据区）
│       └── ErrorLogs/                  SMAPI 错误日志
├── manager/
│   ├── config.json                     管理器配置（600）
│   ├── tasks.db                        任务/审计/快照索引/版本档案（SQLite WAL）
│   └── install-info.txt
└── data/
    ├── gamefiles/                      原生模式：游戏本体（用户自备，本项目不再分发）
    │   └── Stardew Valley/
    │       ├── StardewModdingAPI       启动器（.NET 6 自包含）
    │       ├── Content/                游戏资源
    │       └── Mods -> ../../../Mods   MOD 符号链接到数据区
    ├── game/
    │   ├── runtime-manifest.txt        启动描述（实际以什么参数启动）
    │   ├── run/                        原生后端：各进程 PID 文件
    │   ├── selected-save               当前存档选择
    │   ├── console.log                 SMAPI 控制台输出
    │   └── logs/                       显示栈日志（xvfb/x11vnc/websockify/openbox）
    ├── Saves/                          ★存档（原生：$HOME 下符号链接指向此处）
    ├── Mods/                           ★MOD（原生：游戏目录内符号链接指向此处）
    ├── snapshots/                      快照仓库（<id>/manifest.json + payload/）
    ├── archives/                       版本档案 JSON
    ├── staging/
    │   ├── uploads/                    上传暂存
    │   └── exports/                    导出暂存
    ├── restore-tmp/                    恢复临时区 + **故障现场保留区**
    └── branding/                       运行期凭证
```

### 4.1 哪些数据由谁管理

| 数据 | 归属 | 说明 |
| :--- | :--- | :--- |
| 游戏本体、SMAPI、游戏资源 | **镜像**（上游） | 只读语义；运行时不下载不覆盖（审计已核实） |
| `Saves/`、`Mods/` | **FPK 管理**（用户可见） | 容器通过绝对路径挂载使用 |
| 容器内 `ErrorLogs/` | 容器（**不在卷里**） | 重建即丢 → 管理器把日志采集到宿主机 |
| 快照 / 版本档案 / 任务库 | **管理器** | 升级与默认卸载均保留 |
| 运行进程 | 本机 | 卸载默认只停止；「删除全部数据」才清理 |

## 5. 统一任务队列

### 5.1 状态机

```text
pending ──▶ running ──▶ succeeded
               │
               ├──▶ cancelled                （仅 cancellable=true 的任务）
               │
               └──▶ failed ──▶ rolling_back ──▶ rolled_back
                                  │
                                  └──▶ rollback_failed   （保留故障现场）

崩溃残留：running / rolling_back ──(管理器重启)──▶ interrupted
```

`interrupted` **绝不等于成功**。管理器启动时会扫描并标记，任务详情里给出明确说明。

**`rolled_back` 与 `failed` 的区别**（很重要）：

| 状态 | 含义 |
| :--- | :--- |
| `failed` | 操作失败，且**没有产生需要回滚的变更**（例如变更前快照失败、前置校验失败）—— 什么都没改，保持原状 |
| `rolled_back` | 操作失败，但**变更已被成功撤销**，数据回到操作前状态；原始失败原因保留在 `error` 字段 |
| `rollback_failed` | 操作失败且**回滚也失败** —— 故障现场被保留，必须人工介入 |

任务只有在真正完成了一次变更之后才会触发回滚：处理器在改完数据后显式调用
`ctx.mark_mutated()`。这样「快照失败就中止」这类保护性失败不会被执行一遍无意义的回滚，
也不会把真实原因覆盖成 `rollback_failed`。

### 5.2 互斥

* `HIGH_RISK_KINDS`（安装/更新/回滚/存档写操作/MOD 变更/快照/配置应用）按
  `server_id` 加锁，**严格串行**；
* API 层在提交前先做冲突预检，直接返回 409 并指出正在运行的任务；
* 低风险任务（如诊断包）不受影响。

### 5.3 任务字段

| 字段 | 说明 |
| :--- | :--- |
| `id` | 唯一 ID（16 位 hex） |
| `kind` / `kind_label` | 操作类型与中文名 |
| `operator` | 操作者（网关用户或 `admin`） |
| `state` / `progress` / `step` | 状态机、进度（0-100）、当前步骤 |
| `needs_stop` | 是否需要停服 |
| `snapshot_id` | 关联快照 |
| `cancellable` | 是否允许取消 |
| `result` / `error` | 成功结果 / 失败原因 |
| `rollback_state` / `rollback_error` | 回滚结果 |
| `logs`（表 `task_logs`） | 逐条进度日志 |

## 6. 快照系统

### 6.1 一致性协议（`SnapshotStore.create`）

1. **检查服务器状态**（由调用方持有高风险任务锁）；
2. **必要时安全停服**，并轮询确认游戏进程真的退出（`pause_for_snapshot`）；
3. 复制 `Saves/`、`Mods/`、`game/runtime-manifest.txt`、`game/selected-save` 到
   `staging/<id>.part/payload/`；
4. 生成文件清单：每个文件的相对路径、大小、SHA-256、权限位；
5. 校验：必要内容存在、清单非空、随机抽样重算哈希比对；
6. **原子提交**：`os.rename(staging, final)`（同文件系统内原子），提交后置
   `committed=true`；
7. 任一步失败：半成品移到 `staging/<id>.failed/` 保留现场，**绝不标记成功**，
   并重新拉起服务。

### 6.2 manifest 格式（带版本号）

```json
{
  "schema": "valleyserver.snapshot",
  "manifest_version": 1,
  "id": "20261011-193000-manual-a1b2c3",
  "kind": "manual", "reason": "pre-mod-install", "reason_label": "安装 MOD 前",
  "created_at": 1791719200.0, "created_at_iso": "2026-10-11T19:30:00+0800",
  "operator": "admin", "task_id": "…", "note": "…",
  "server_stopped_for_snapshot": true,
  "game_version": "1.6.15", "smapi_version": "4.5.1",
  "image_ref": "lixeer/valley-server@sha256:…", "image_digest": "sha256:…",
  "mods": [{"dir":"ALOS","name":"ALOS","version":"1.0.6","unique_id":"…","enabled":true,"tree_hash":"…"}],
  "counts": {"files": 42, "bytes": 123456, "mod_dirs": 5, "save_dirs": 1},
  "hashes": {"mods": "…", "saves": "…"},
  "files": [{"path":"Saves/Farm_1/SaveGameInfo","size":123,"sha256":"…","mode":420}],
  "committed": true,
  "checksum_algorithm": "sha256"
}
```

* **schema 校验**：`migrate_manifest()` 把所有旧版本升级到当前 `MANIFEST_VERSION`；
  遇到**高于**本管理器支持的版本会**拒绝**（不按低版本语义硬解读，避免损坏数据）。
* 快照同时包含**镜像摘要**与**版本档案引用**（游戏/SMAPI/MOD 版本），因此「恢复到
  某个快照」时可一并判断镜像是否需要回滚。

### 6.3 保留策略

* 自动与手动**独立计数**（默认自动 10、手动 5，可在界面调整）；
* 已固定（pinned）的快照**永不清理**；
* 仍被任务引用的快照跳过；
* 磁盘可用空间低于策略下限时，**拒绝**高风险变更并给出明确提示。

## 7. 安全模型

### 7.1 网关可信头的信任边界（重要）

服务同时监听 TCP `<panel_port>` 与 Unix 套接字（`app/ui/config` 里声明的
`gatewaySocket: valleyserver.sock`）。飞牛网关经 **Unix 套接字**转发并注入
`X-Trim-Userid` / `X-Trim-Username` / `X-Trim-Isadmin`。

> ⚠️ **只有经 Unix 套接字进来的请求才信任这些头。**
> 曾经无条件信任它们，结果是**局域网内任何人加一个 `X-Trim-Userid: 1` 请求头
> 就能免密拿到管理员权限**（已实证并在 0.2.1 修复）。
> TCP 直连一律要求口令 + 会话 Cookie + CSRF。

| 来源 | 认证方式 |
| :--- | :--- |
| Unix 套接字（飞牛网关） | 信任 `X-Trim-*`；无该头则视为未登录 |
| TCP（浏览器直连面板端口） | 口令登录 → HttpOnly 会话 Cookie → 写操作校验 CSRF |

另外：路径在鉴权前统一归一化（补前导斜杠），避免 `GET api/x`（不带前导斜杠）
与 `GET /api/x` 被"鉴权"和"路由"各自解释成不同的路径。


```text
浏览器 ──(飞牛网关注入 Header / 本地口令)──▶ run.py
                                              │  仅有白名单 REST
                                              ▼
                                        orchestration
                                              │  argv 列表，无 shell
                                              ▼
```

| 面 | 防护 |
| :--- | :--- |
| 输入 | 名称白名单字符集、端口范围、镜像引用正则、大小/CPU 格式、相对路径白名单 |
| 路径 | `safe_join` / `assert_within`：拒绝绝对路径、`..`、逃逸 |
| ZIP | 条目名校验、符号链接/特殊文件拒绝、数量/体积/压缩比上限、解包后复检父链 |
| 命令 | `run(argv_list)`，传入字符串直接抛错；无 `shell=True` |
| Web | HttpOnly Cookie、CSRF 头、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy` |
| 数据 | 高风险操作前强制快照；覆盖/删除需输入名称二次确认；删除移入现场备份区 |
| 日志 | 诊断包导出前按字段与正则双重脱敏 |

## 8. REST API

所有接口在网关前缀 `/app/valleyserver/` 下同样可用（前端使用相对路径）。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `api/health` | 健康检查（公开），报版本/HOME/DATA_LIBRARY_PATH |
| GET | `api/auth/state` | 登录态查询（公开） |
| POST | `api/login` / `api/logout` | 登录 / 登出 |
| POST | `api/auth/password` | 修改口令 |
| GET | `api/dashboard` | 总览聚合 |
| GET | `api/server/status` | 容器状态、三层健康判据、端口 |
| GET | `api/server/logs?source=&tail=&level=` | 日志（container/console/manager/task） |
| POST | `api/server/{install,start,stop,restart,recreate}` | 生命周期操作（返回 task_id） |
| GET | `api/saves` · `api/saves/{name}` | 存档列表 / 详情 |
| POST | `api/saves/{import-plan,import,export,delete,activate,restore}` | 存档操作 |
| GET | `api/saves/export/download?name=` | 下载导出包 |
| GET | `api/mods` · `api/mods/{dir}` | MOD 列表与诊断 / 详情 |
| POST | `api/mods/{install-plan,install,remove,toggle}` | MOD 操作 |
| GET/POST | `api/mods/upstream-bundle` | 上游 MOD 包来源 / 下载安装 |
| GET | `api/mods/smapi-log` | SMAPI 日志解析结果 |
| GET | `api/snapshots` · `api/snapshots/{id}` | 快照列表 / 详情 |
| POST | `api/snapshots/{create,verify,restore,delete,policy}` | 快照操作 |
| GET | `api/snapshots/download?id=` | 下载快照包 |
| GET | `api/versions` · `api/versions/history` · `api/versions/check` | 版本档案与更新检查 |
| POST | `api/versions/{pin,update,rollback}` | 固定 / 更新 / 回滚 |
| GET/POST | `api/config` | 读取 / 应用配置（白名单字段） |
| GET | `api/tasks` · `api/tasks/{id}` · POST `api/tasks/cancel` | 任务队列 |
| GET | `api/system` · `api/system/health` · `api/system/audit` · `api/system/deps` | 系统与诊断 |
| POST | `api/diagnostics/bundle` · GET `api/diagnostics/download?name=` | 诊断包 |
| GET | `api/about` | 版本、上游、许可证、第三方声明 |

**所有会改数据的请求都需要 `X-CSRF-Token`**（登录响应里返回）。

## 9. 自动恢复与后台监管

`mgr/supervisor.py` 每 10 秒巡检：

| 行为 | 规则 |
| :--- | :--- |
| 开机自启 | `auto_start_with_nas=true` 且容器存在但未运行时拉起 |
| 异常恢复 | 非用户主动停止的情况下自动 `up`，失败计数 +1 |
| 退避 | `backoff × 2^(失败次数-1)`，封顶 600 秒 |
| 上限 | 失败次数 ≥ `start_retry_max` 后**停止自动拉起**并写入告警事件 |
| 定时快照 | 按 `auto_snapshot_interval_minutes` 排入统一任务队列（与其它高风险任务串行） |
| 磁盘告警 | 低于下限时写事件；同时所有高风险操作会被拒绝 |

## 10. 与上游方案的差异

| 项 | 上游 | 本项目 | 原因 |
| :--- | :--- | :--- | :--- |
| VNC 密码 | 硬编码 `041041` | 安装时随机生成 | 安全 |
| 监听地址 | `0.0.0.0` | 默认 `127.0.0.1` | 上游 WebVNC 无认证 |
| 健康检查 | 无 | 管理器三层判据 | 上游镜像无 Healthcheck |
| 日志 | 仅标准输出 | 采集到文件 + 控制台落盘 | 便于事后诊断 |
| 挂载路径 | 相对 `./Mods` | 数据目录下的绝对路径（校验后） | 防路径注入 |
| 当前存档 | 手工在 VNC 内选择 | `--load-save` 自动加载 | 无人值守 |
