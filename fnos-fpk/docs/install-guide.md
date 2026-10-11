# 安装 · 升级 · 卸载 · 备份 · 恢复 · 故障排查

## 1. 前置条件

| 依赖 | 要求 | 检测方式 |
| :--- | :--- | :--- |
| 飞牛 OS（fnOS） | 支持 FPK 的版本（本项目在 `1.2.0701` 上实测） | 应用中心可安装第三方 FPK |
| Docker Engine | 必需（首次启动服务器时） | 「系统」页 → Docker 依赖 |
| Docker Compose V2 | 必需（`docker compose`） | 同上 |
| Python 3.10+ | 由 `install_dep_apps = python312` 提供 | `/api/health` 报出的版本 |
| 磁盘空间 | 首次拉取镜像约 3.4 GiB；建议留 20 GiB | 「系统」页 → 磁盘可用 |

> 缺少 Docker 时**仍可安装管理器**（界面会给出安装指引），只是无法启动游戏服务器。
> 本项目**不会**在不了解 fnOS 包管理方式的前提下强行覆盖系统组件。

## 2. 安装

### 2.1 从构建产物安装

```bash
# 1) 构建（或从 GitHub Actions artifact 下载）
cd fnos-fpk
STAGE_ROOT=/tmp/fpk-build ./tools/build-fpk.sh
# → dist/valleyserver-<version>.fpk

# 2) 校验产物
sha256sum dist/valleyserver-<version>.fpk
cat dist/valleyserver-<version>.build-info.txt
```

安装方式（选一）：

* **应用中心 → 手动安装 → 选择 .fpk**（推荐，可看到向导）
* 命令行：`trim-cli app install-fpk --remote-path <abs-path>.fpk --volume-id 1 --yes`
  （声明了 `install_dep_apps` 时命令行安装可能被 fnOS 拒绝，此时请用应用中心）

### 2.2 安装向导填什么

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| 管理器面板端口 | `18242` | 仅用于本机直连；从应用中心打开走统一网关，不依赖此端口 |
| 游戏相关端口的监听范围 | **仅本机（推荐）** | 选「所有网络接口」时 WebVNC/VNC/CommandWebUI 会一并对外暴露（有安全风险） |
| 游戏联机端口（UDP） | `24642` | 玩家联机端口；若改端口需同步放行防火墙 |
| WebVNC 端口 | `5800` | 浏览器看游戏画面（上游无认证） |
| VNC 端口 | `5900` | TigerVNC 等客户端直连 |
| VNC 密码 | 留空 = 随机生成 | VNC 协议只用 8 位；不再使用上游默认 `041041` |
| 安装后立即拉取并启动 | 是 | 首次拉取约 3.4 GiB，耗时较长 |

### 2.3 安装后第一次登录

* 从应用中心打开「ValleyServer 管理器」；
* 或访问 `http://<NAS-IP>:18242/`；
* 初始口令：`/vol1/@apphome/valleyserver/initial-password.txt`（权限 600）
  * 查看命令：`cat /vol1/@apphome/valleyserver/initial-password.txt`
  * **登录后请立即在「关于」页以外的口令修改入口更换**（`POST /api/auth/password`）。
* 经飞牛网关访问时使用飞牛账号鉴权，不需要再输口令。

## 3. 首次启动服务器

1. 「系统」页确认 Docker / Compose 就绪，磁盘充足；
2. 「配置」页确认端口与监听范围，保存；
3. 「总览」→「安装 / 首次启动」：
   * 生成 `docker-compose.yaml` 与 `entrypoint.sh`；
   * 校验 compose 配置（`docker compose config -q`）；
   * 按**镜像摘要**拉取 `lixeer/valley-server@sha256:…`；
   * 启动容器并等待三层健康判据通过（冷启动可能 1-3 分钟）；
4. 「MOD」页安装自动化 MOD（一键从上游 Release 下载，或上传自己的 ZIP）；
5. 「存档」页导入存档并「切换为当前」；
6. 「快照」页创建一次手动快照作为基线。

> ⚠️ 首次部署时 `Mods/` 是空的，这是正常的，但**服务器不会无人值守运行**——
> 必须有 ALOS 之类的自动化 MOD。

## 4. 备份

### 4.1 快照（推荐）

* 手动：「快照」→「创建手动快照」；
* 自动：默认每 360 分钟一次（可在「配置」调整），保留最近 10 个自动快照；
* 高风险操作（更新、MOD 变更、存档写操作、配置变更）前会**自动**创建快照，
  且**快照失败即中止操作**；
* 容器在运行时会先安全停服并确认进程退出，然后创建，完成后自动拉起。

快照内容：全部存档、完整 MOD 目录（含每存档 `data/`）、容器配置、
游戏/SMAPI/MOD 版本信息、MOD 清单与文件校验值、镜像摘要、时间/触发原因/校验结果。

### 4.2 导出到本机

| 目标 | 方式 |
| :--- | :--- |
| 单个存档 | 「存档」→ 对应行「导出」（下载 ZIP） |
| 快照整包 | 「快照」→ 对应行「下载」（`.tar.gz`） |
| 诊断包 | 「系统」→「生成诊断包」（已脱敏，不含存档与 MOD 文件本体） |

### 4.3 手工备份

```bash
# 停服（保证存档一致）
docker stop --time 60 valleyserver-game

# 备份全部数据
tar czf /path/to/backup-$(date +%F).tar.gz \
  -C /vol1/@apphome/valleyserver data/Saves data/Mods data/snapshots manager

# 重新启动
docker start valleyserver-game
```

## 5. 恢复

### 5.1 从快照整体恢复

「快照」→ 选一个**校验通过**的快照 →「恢复」。系统会：

1. 深度校验快照（逐文件重算 SHA-256）；**校验不通过直接拒绝**并说明原因；
2. 先把**当前状态**再存一份快照（作为回滚点）；
3. 解包 → 自校验 → 原子换入；
4. 恢复后再次校验；
5. 任一步失败 → 自动回滚到操作前状态，并**保留故障现场**（`data/restore-tmp/`）。

### 5.2 从快照恢复单个存档

「存档」→ 对应行「恢复」→ 选择快照。

### 5.3 从导出的 ZIP 导入存档

「存档」→「导入存档」→ 选择 ZIP。系统会：

* 预检结构（路径、条目数、体积、压缩比、是否含 `SaveGameInfo`）；
* 若目标同名存档已存在，要求**手工输入存档名**确认覆盖；
* 服务器必须已停止（否则拒绝）。

### 5.4 卸载重装后恢复

数据在 `/vol1/@apphome/valleyserver`，默认卸载**不会删除**。重装后：

1. 确认 `data/Saves`、`data/Mods` 存在；
2. 「快照」页应能看到历史快照（索引在 `manager/tasks.db`）；
3. 若只需存档，直接在「存档」页确认列表即可。

## 6. 升级

### 6.1 升级管理器本身

1. 构建更高版本（`manifest.version` 必须递增，否则 fnOS 报 `errno 10236`）；
2. 应用中心手动安装新 FPK；
3. `upgrade_init` 会停服务并清理陈旧进程与端口占用，`upgrade_callback` 会重新拉起；
4. 验证：**「系统」页或 `/api/health` 报出的版本号必须是新版本**
   （`curl -s http://127.0.0.1:18242/api/health`）。

数据保留：`data/`、`manager/`、快照、版本档案全部保留（`upgrade_init` 只停服务，不动数据）。

### 6.2 升级游戏运行版本

「版本」页 →「检查可用更新」→ 若有新摘要则「更新到该摘要」。

流程：磁盘/回滚能力检查 → 完整快照 → 拉取目标镜像 → 切换并重建 →
健康检查 + SMAPI 加载检查 + 存档可读性检查。
**失败时**保留新版本现场与日志，并尝试恢复旧镜像与快照数据。

> ⚠️ 上游 Docker Hub **只发布了单一标签**（`v26.5`）。因此现实中通常**没有可更新的版本**，
> 也没有上游历史版本可回滚。「版本」页会如实显示这一点。

## 7. 卸载

| 选项 | 行为 |
| :--- | :--- |
| **保留数据（默认）** | 停止游戏容器；停止管理器服务；**保留**存档/MOD/快照/版本档案/配置；保留镜像 |
| 删除全部数据 | 上述内容一并删除；移除容器与网络；可选移除镜像。**不可撤销** |

卸载向导中还会单独询问「是否移除上游游戏镜像（约 3.4 GiB）」。

**建议**：卸载前先导出存档或下载快照整包。删除路径经过白名单校验，
只会删除本应用目录下的内容，不会触碰其它应用或系统目录。

## 8. 故障排查

### 8.1 打开面板空白 / 应用中心点不开

1. 检查服务是否在跑：`/var/apps/valleyserver/cmd/main status`（`0`=运行，`3`=未运行）
2. 看日志：`/vol1/@appdata/valleyserver/logs/manager.log`
3. 常见原因：
   * `manager.log` 为空 → 服务根本没起来（看应用中心提示的安装日志）
   * socket 不存在 → `ls -l /vol1/@appcenter/valleyserver/valleyserver.sock`
   * 浏览器缓存旧界面 → 强刷（界面 `index.html` 已是 `no-store`）

### 8.2 「升级了但功能没变」

这是最典型的隐形故障。排查顺序：

```bash
curl -s http://127.0.0.1:18242/api/health     # 看 version 字段
ss -lntp | grep 18242                          # 看进程启动时间
cat /vol1/@appdata/valleyserver/logs/manager.log | tail -30
```

若 `version` 是旧版本 → 旧进程没被停掉。执行
`/var/apps/valleyserver/cmd/main stop` 后再 `start`。

### 8.3 服务器启动不起来

1. 「总览」看健康判据的三层状态：
   * L1 容器运行 = ❌ → 容器没起来，看 `docker logs valleyserver-game`
   * L1 ✅ L3 ❌ → 容器在跑但 SMAPI 没加载完成，看「控制台」页的 SMAPI 控制台日志
2. 常见原因：
   * `Mods/` 里有损坏 MOD → 在「MOD」页看诊断结果
   * 端口被占用 → `ss -lntup | grep -E '24642|5800|5900|29103'`
   * 镜像拉取失败 → 「版本」页检查网络与 Docker 日志
3. 容器重启次数超过上限后，后台监管会**停止自动拉起**并告警（避免崩溃循环）。

### 8.4 存档不见了 / 存档损坏

1. 「存档」页看是否有「结构异常」标记；
2. `ls -l /vol1/@apphome/valleyserver/data/Saves/`；
3. 从快照恢复（「快照」→「恢复」）；
4. 删除过的存档在 `/vol1/@apphome/valleyserver/data/restore-tmp/deleted-save-*` 里
   保留着故障现场，可手工找回。

### 8.5 MOD 加载失败

1. 「MOD」页看诊断结果：重复 UniqueID、缺失依赖、依赖版本不满足、manifest 结构问题；
2. 「控制台」页看 SMAPI 报错（已按确定性错误/规则推断分类）；
3. 用「MOD」页的「禁用」逐个排除；
4. 注意：**MOD 目录里可能含每存档运行数据**（如 `Mods/ALOS/data/*.json`），
   卸载前系统会强制快照并额外保留一份副本。

### 8.6 磁盘空间不足

* 「系统」页看分类占用；优先清理「快照」与 `data/staging`；
* 低于策略下限时管理器**会拒绝**高风险变更（这是保护行为，不是 bug）；
* 调整下限：「配置」页 → 备份与保留策略 → 磁盘空间下限。

### 8.7 VNC / WebVNC 打不开

* 默认只监听 `127.0.0.1`，只能从 NAS 本机访问。需要远程看画面时：
  * 改「配置」→ 监听范围 → 所有网络接口（**会同时暴露无认证的 WebVNC，请确保网络可信**）；
  * 或在 NAS 上用 SSH 端口转发：`ssh -L 5800:127.0.0.1:5800 <NAS>`。

### 8.8 收集诊断信息

「系统」→「生成诊断包」，然后下载。诊断包已自动脱敏（口令、令牌、
`Authorization` 头等替换为 `[REDACTED]`），且**不含存档与 MOD 文件本体**。
披露前仍建议人工复核一遍。

## 9. 常用命令速查

```bash
APP=/vol1/@appcenter/valleyserver
VAR=/vol1/@appdata/valleyserver
HOME_DIR=/vol1/@apphome/valleyserver

$APP/../cmd/main status            # 0=运行 3=未运行
$APP/../cmd/main stop
$APP/../cmd/main start

curl -s http://127.0.0.1:18242/api/health
curl -s --unix-socket $APP/valleyserver.sock http://localhost/app/valleyserver/api/health

docker ps --filter name=valleyserver-game
docker logs --tail 200 valleyserver-game
cat $HOME_DIR/data/game/console.log | tail -100
cat $VAR/logs/manager.log | tail -100
sqlite3 $HOME_DIR/manager/tasks.db 'select id,kind,state,error from tasks order by created_at desc limit 10;'
```
