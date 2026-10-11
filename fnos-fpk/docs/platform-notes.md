# fnOS FPK 平台事实基础（platform-notes）

> 面向项目：**ValleyServer 2.x FPK 管理器**
> 编制者：teammate `fnos-platform`｜编制时间：2026-10-11（Asia/Shanghai）
> 写作用域：本文件是本次任务**唯一**被写入的文件。
>
> ⚠️ **后续变更说明（0.2.0）**：本项目最终采用**原生部署**（管理器直接在本机运行游戏 +
> SMAPI + 虚拟显示栈），**不使用 Docker**。因此本文档中关于 `docker-project` 形态、
> `join-groups: ["docker"]`、`docker inspect` 判活那些段落，是**平台事实记录**，
> 但**不是本项目采用的路线**。与本项目相关的部分请以
> [native-deployment.md](native-deployment.md) 与 [architecture.md](architecture.md) 为准。

---

## 0. 本文档的证据等级与来源约定

本机 **就是一台 fnOS 设备**（`/usr/trim` 存在，`/var/apps` 下有 50 个已安装应用），因此除了读文档，
还能直接反查 fnOS 自己的二进制、nginx 配置和已装应用的落盘结果。全文所有结论都标注证据等级：

| 标记 | 含义 | 可信度 |
|---|---|---|
| `[总纲]` | 来自方法总纲标注为「官方规范」的段落 | 高（但总纲亦有个别与真机不符处，本文用 `[实测]` 修正） |
| `[实测]` | 本机（fnOS 真机）当场跑命令得到的输出 | 最高 |
| `[二进制]` | 用 `strings` 从 fnOS 自身可执行文件中提取的字符串/结构标签 | 高（原始实现，但无上下文，仅作定位） |
| `[已装包]` | 从本机 `/var/apps/*/manifest`、`*/config/*` 等已安装包反查的事实 | 高 |
| `[骨架]` | 来自可直接复制的工程骨架 fnos-fpk-template | 高（真机验证跑通） |

短引用代码：

- `[总纲 Lxx-yy]` → 方法总纲 `fnos-fpk-开发方法总纲.md` 的行区间
- `[SKILL.md:nn]` → 同技能目录下的 `SKILL.md`（fnos-fpk 技能入口，含总纲之外的实测补充）
- `[骨架 <path>]` / `[骨架 <path>:Lxx]` → 骨架内文件（含 `fnpack/` 前缀）
- `[二进制 trim_app_center]` / `[二进制 trim_http_cgi]` → `/usr/trim/bin/` 下的 fnOS 程序
- `[nginx]` → `/usr/trim/nginx/conf/conf.d/*.conf`

**来源文件清单**（绝对路径）：

- 方法总纲：`/vol1/@appdata/deepseek.harness/dsh-data/skills/fnos-fpk/reference/fnos-fpk-开发方法总纲.md`（2130 行，14 章 + 4 附录）
- 技能入口：`/vol1/@appdata/deepseek.harness/dsh-data/skills/fnos-fpk/SKILL.md`
- 工程骨架（技能资产）：`/vol1/@appdata/deepseek.harness/dsh-data/skills/fnos-fpk/assets/fnos-fpk-template/`
- 工程骨架（工作区副本）：`/vol1/@appdata/deepseek.harness/home/github/fnos-fpk-template/`
- 本机 fnOS 系统：`/usr/trim/bin/`、`/usr/trim/nginx/conf/conf.d/`
- 本机已装应用：`/var/apps/<app>/`、`/vol1/@appcenter/<app>/`

**本机工具链事实（`[实测]`）**：

```
$ which fnpack          → /usr/local/bin/fnpack
$ fnpack --help         → Command-line utility for creating and packing application for fnOS.
                          Version 1.2.4
                          Available Commands: build, create
$ file /usr/local/bin/fnpack
  ELF 64-bit LSB executable, x86-64, statically linked, stripped
$ stat -c '%s' /usr/local/bin/fnpack → 3711160   （≈3.54 MiB，mtime 2026-09-14）
```

> 注意：`fnpack version` / `fnpack -v` / `fnpack --version` **都不存在**（报 unknown command /
> unknown flag，退出码 1）；版本号只能从 `fnpack --help` 的首行读。

---

## 1. manifest 官方字段全表

manifest 是 **INI 格式、无扩展名、位于包根**`[总纲 L82]`。本机 50 个已装包中，
`version`/`source`/`maintainer`/`distributor`/`display_name`/`desc`/`appname` 出现 **50/50 次**`[实测]`。

### 1.1 fnpack 1.2.4 真正会拦的字段（打包期硬校验）

以下结论全部由本机 `fnpack build` 逐项实验得到`[实测]`：

| 情形 | fnpack 1.2.4 行为 | 原始报错 |
|---|---|---|
| 缺 `appname` | ❌ 拒绝 | `Packing failed. Required field appname is missing in manifest file` |
| 缺 `source` | ❌ 拒绝 | `Packing failed. Required field source is missing in manifest file` |
| 缺 `desc` | ✅ **放行** | — |
| 缺 `platform` | ✅ **放行** | — |
| `version` 缺失/非法 | ❌ 拒绝 | `Packing failed. Invalid version "…". Expected format: x.y.z[-r] , where x , y , z , and r are integers.` |
| 追加任何未知字段 | ✅ **静默放行**（打进去但没人读） | — |

**这是最容易踩的坑：`desc` 与 `platform` fnpack 不拦，但官方要求必填**`[总纲 L128, L130]`。
打包成功 ≠ 元数据合格，必须在 CI 里自己补字段断言。

### 1.2 官方字段表（总纲 §2.2 全表 + 本机补充）

`[总纲 L118-149]`，逐行核对，并补充「本机证据」列：

| 字段 | 必填 | 取值约束 | 示例 | 本机证据 |
|---|:--:|---|---|---|
| `appname` | ✅ | 小写字母+数字+连字符（官方）；fnpack 实测**不校验大小写**，见 §1.6 | `family-message` | fnpack 强制；50/50 已装包使用 |
| `version` | ✅ | `x.y.z[-r]`，各段整数；见 §6.7 实测矩阵 | `0.20.1`、`1.0.0`、`1.0.0-720` | fnpack 强制 |
| `display_name` | ✅ | 显示名，可带引号 | `"家庭消息"` | 50/50 |
| `desc` | ✅ | 描述，支持 HTML，可带引号 | `"<p>…</p>"` | 50/50；fnpack 不拦 |
| `source` | ✅ | 第三方固定 `thirdparty`；官方包用 `official` | `thirdparty` | fnpack 强制 |
| `platform` | ✅ | `x86` / `arm` / `all` | `all` | 42/50；fnpack 不拦 |
| `maintainer` | — | 开发者/团队名 | `"youyoudezhuzhu"` | 50/50 |
| `maintainer_url` | — | 开发者网站 | `https://github.com/…` | 45/50 |
| `distributor` / `distributor_url` | — | 发布者（与开发者不同时填） | `"veenyi"` | 50/50、46/50 |
| `service_port` | — | 应用服务端口 | `18801` | 7/50（多为需要固定端口的管理类应用） |
| `checkport` | — | 启动前是否检查端口，默认 `true`；取值 `true`/`false` | `true` | 24/50 |
| `ctl_stop` | — | 是否显示启停控制 `true`/`false` | `true` | 12/50 |
| `os_min_version` / `os_max_version` | — | 支持的最低/最高 fnOS 版本 | `1.0.0` | 28/50 |
| `install_type` | — | 空=用户选存储位置；`root`=系统分区 | `root` | 14/50 |
| `install_dep_apps` | — | 依赖应用 `:` 分隔，`>` 指定最低版本 | `python312`、`nodejs_v24` | 6/50 |
| `desktop_uidir` | — | UI 目录（相对应用目录，文档称默认 `ui`） | `ui` | 42/50 |
| `desktop_applaunchname` | — | 默认入口 ID，格式 `<appname>.<entry>` | `family-message.main` | 36/50 |
| `micro_app` | — | 是否微应用 | `true` | 18/50 |
| `changelog` | — | 更新说明，支持 HTML | `更新上游版本` | 29/50 |
| `disable_authorization_path` | — | `true` 隐藏授权目录设置；`false` 让用户在设置页授权目录 | `true` | 21/50 |
| `checksum` | — | 校验和（可留空） | `eab4bb…` | 31/50 |

### 1.3 fnOS 解析器里真实存在的字段集（二进制反查，45 项）

从 `/usr/trim/bin/trim_app_center`（107,849,064 字节，Go 程序）提取到以下 INI 结构标签`[二进制]`：

```
app_name    arch        auto_upgrade_from  change_log   checkport   checksum
ctl_stop    ctl_uninstall  desc            desktop_applaunchname  desktop_appname
desktop_apppage  desktop_uidir  disable_authorization_path  display_name
distributor  distributor_url  exclude_arch  exclude_kernel  exclude_model
install_break_apps  install_conflict_apps  install_dep_apps  install_reboot
install_replace_apps  install_type  kernel  maintainer  maintainer_url
micro_app   min_size    model   native_app  os_max_version  os_min_version
platform    service_path  service_port  service_protocol  source  support_url
third_party  trim_api_sign  trim_dev_crt  version
```

**关键推断（务必理解）**：标签里写作 `app_name` / `change_log`，而真实包写的是
`appname` / `changelog`，两者都能工作`[实测]`（50 个已装包全用 `appname`，29 个用 `changelog`）。
说明 fnOS 的 INI 解析器做了**去下划线 + 不区分大小写**的键归一化。
据此，上面 45 项就是「解析器认识的全部字段」，而 `icon`、`helpurl`、`reloadui`、
`iframe_app`、`desktop_appname`(注：这个**在**表里) 等不在表中的键属于**被忽略的历史遗留**。

> 注意：`desktop_appname`（无 `l`）**确实在解析器字段表里**；它与项目的
> `desktop_applaunchname` 是两个不同字段。本机 4 个包用 `desktop_appname`，36 个包用
> `desktop_applaunchname`。本项目**只用 `desktop_applaunchname`**。

### 1.4 已装 50 包字段频次（`[实测]`，`grep -h -oE '^[a-zA-Z_][a-zA-Z0-9_]*' /var/apps/*/manifest`）

```
50 version / source / maintainer / distributor / display_name / desc / appname
46 distributor_url   45 maintainer_url   42 desktop_uidir   40 platform
36 desktop_applaunchname   31 checksum   29 changelog   28 os_min_version
24 checkport   21 disable_authorization_path   18 micro_app   18 arch
14 install_type   12 helpurl   12 ctl_stop   7 service_port   7 reloadui
 6 native_app    6 install_dep_apps    4 desktop_appname    2 os_min_ver
 2 beta   1 thirdparty / support_url / iframe_app / icon / backend_version /
          application_version / PvZ2(残留行)
```

### 1.5 明确「官方不存在」的字段（易错项）

总纲给出的黑名单`[总纲 L147-149]`，与二进制 `ini:` 标签表**完全吻合**（均未出现）：

| 错误字段 | 正确字段 | 本机反证 |
|---|---|---|
| `icon` / `icon256` | 不需要（用包根 `ICON.PNG` + `ICON_256.PNG`，桌面图标由 `app/ui/config` 的 `icon` 指路） | `hermes-agent` 包真的写了 `icon = ICON.PNG`，但解析器无此标签 → 纯装饰 |
| `description` | `desc` | 0/50 使用 |
| `developer` | `maintainer` | 0/50 使用 |
| `port` | `service_port` | 0/50 使用 |
| `category` | 无此概念 | 0/50 使用 |
| `main` | 无此字段 | 0/50 使用 |
| `arch` | **见 §1.6（有争议，不是简单的不存在）** | 18/50 使用，含飞牛官方包 |

**关键风险**：fnpack 1.2.4 对未知字段**完全静默**`[实测]`——写了 `arch`/`icon`/`port`
照样 `Packing successfully`。所以「字段写错」不会在打包期暴露，只会在运行期表现为
「端口没生效 / 图标不显示 / 平台不匹配」。**必须在评审时人工对照 §1.2。**

### 1.6 冲突项：`arch` 到底存不存在

- 总纲把 `arch` 列入「官方不存在」`[总纲 L149]`。
- 但 fnOS 自身二进制里有 `ini:"arch"`、`ini:"exclude_arch"` `[二进制]`。
- 18/50 已装包使用 `arch`，其中包括飞牛官方包：`trim.photos`（`arch="x86_64"` **同时**有
  `platform="x86"`）、`trim.media`、`trim.vm`、`trim.docs`、`trim.security`、`python312`、
  `nodejs_v22`；以及 Synology 移植痕迹明显的值：`python312` 的
  `arch="apollolake avoton braswell … v1000"`（DSM 机型代号）。

**结论/建议**：`arch` 至少被解析器接受，但其语义（是架构过滤还是别的）**未能确认**。
本项目**只写 `platform`**（fnpack 脚手架默认、官方文档口径），不要写 `arch`，以免给出
互相矛盾的平台声明。该分歧列入 §10 待验证。

### 1.7 本项目建议的 manifest（ValleyServer 2.x 管理器）

```ini
appname               = valleyserver
version               = 0.1.0                # 每次打包必须 bump，见 §6.7 / §8-13
display_name          = "ValleyServer 管理器"
desc                  = "星露谷物语专用服务器（ValleyServer 2.x）的图形化管理器：…"
source                = thirdparty
platform              = x86                  # 原生部署；显示栈与游戏本体都是 x86_64 二进制
maintainer            = "…"
maintainer_url        = "…"
distributor           = "…"
desktop_uidir         = ui
desktop_applaunchname = valleyserver.Application
service_port          = 18xxx                # 见 §5；避开 5666/5667/22/445/2049/5005/5006/8000
checkport             = true
ctl_stop              = true
micro_app             = true
disable_authorization_path = true
os_min_version        = 1.0.0
```

依据：字段名与取值口径`[总纲 L118-189]`、`[骨架 fnpack/manifest:1-17]`；
`micro_app=true` 与本机管理器类应用一致（`deepseek.harness`、`hermes-agent`、`webscreen` 均为
`micro_app = true`）`[已装包]`。`desc`/`platform` 是 fnpack 不拦而官方要求的字段，**必须有人工核对**。

---

## 2. cmd/ 9 个生命周期脚本

### 2.1 目录与权限

- 9 个脚本**全部必须在包目录里 `chmod +x`**，否则安装/调用失败`[总纲 L30, L95-104]`。
- `fnpack create <app>` 生成的官方 stub 权限是 `-rw-r--r--`（**没有执行位**）`[实测]`，
  必须自己补；fnpack **不拦**（见 §6.5）。
- 已装应用落盘后是 `-rwxr-xr-x`（`/var/apps/family-message/cmd/*`）`[已装包]`。

### 2.2 九个脚本逐个说明

| 文件 | 调用时机 | 必须做什么 | 退出码 | 幂等性要求 |
|---|---|---|---|---|
| `install_init` | 安装文件应用**前** | 校验依赖（如 `python312` 存在），不满足写 `$TRIM_TEMP_LOGFILE` 并 `exit 1` | 0/1 | 无副作用即可 |
| `install_callback` | 安装**后** | 建数据目录、建 venv/装依赖、生成初始配置、`chmod +x app/bin/*` | 0/1 | **必须幂等**（可能重复执行） |
| `upgrade_init` | **升级前** | **必须停服务**：调 `cmd/main stop` + 按端口兜底 kill；删 PID 文件 | 0 | 必须幂等 |
| `upgrade_callback` | **升级后** | **必须重新拉起**：补齐依赖后调 `cmd/main start`；配置/数据保持不动 | 0/1 | 必须幂等 |
| `uninstall_init` | 卸载前 | 通常空（骨架直接 `exit 0`） | 0 | — |
| `uninstall_callback` | 卸载后 | 按向导决定是否删数据（保留在 `$TRIM_PKGHOME` 是常见选择） | 0 | 必须幂等（重复卸载） |
| `config_init` | 配置变更前 | 通常空 | 0 | — |
| `config_callback` | 配置变更后 | 把向导新值回写配置文件并重启服务 | 0 | 必须幂等 |
| `main` | 运行控制 | `start` / `stop` / `status` | 见下 | **必须幂等**（已在跑要返回 0） |

来源：`[总纲 L250-292]`；骨架实现 `[骨架 fnpack/cmd/*]`；官方 stub 见 §2.4。

### 2.3 `cmd/main status` 的 0/3 语义（硬规则）

- **`0` = 运行中，`3` = 未运行**；其它值 fnOS 判为异常`[总纲 L34, L264]`。
- 官方生成的 stub 里注释原文：`# check application status command. exit 0 if running, exit 3 if not running` `[实测，fnpack create 产物 cmd/main]`。
- 骨架实现：`status) if running; then exit 0; else exit 3; fi ;;`，且 `running()` 会在 PID
  文件存在但进程已死时**顺手删掉陈旧 PID 文件**再返回 1`[骨架 fnpack/cmd/main:78-85, 232-237]`。
- 骨架的升级回归测试直接断言 `cmd/main status` 的退出码语义（`simulate-install.sh` 期望
  未启动=3、启动后=0）`[骨架 tools/simulate-install.sh:59-72]`。
- 官方 docker 应用 stub 用 `docker inspect <container_name> | grep -q '"Status": "running",'`
  判运行中，否则 `exit 3``[已装包 /var/apps/homebox/cmd/main:5-42]`。

### 2.4 官方 stub vs 骨架（本项目应基于骨架，但要把「运行控制」换成 Docker 版）

`fnpack create`（native 模板）生成的 `cmd/main` 是一个**最小可用**实现`[实测]`：
用 `$TRIM_PKGVAR/info.log` + `$TRIM_PKGVAR/app.pid`，`start` 里 `bash -c "${CMD}"`（`CMD=""` 占位），
`stop` 里 TERM→10s 轮询→KILL，`status` 用 `kill -0`。它**没有**：陈旧进程清理、端口兜底 kill、
健康检查轮询、日志轮转、`HOME`/`DATA_LIBRARY_PATH` 覆盖。

骨架的 `cmd/main`（238 行）补齐了全部这些`[骨架 fnpack/cmd/main]`：

| 能力 | 骨架位置 | 为什么必须有 |
|---|---|---|
| `export HOME="${TRIM_PKGHOME:-…}"`（**无条件覆盖**） | L14-19 | fnOS 已注入 `HOME=/root`，`${HOME:-…}` 不生效；被管理子服务会去 `/root/.<tool>/` 找凭证`[总纲 L49-50, L1156-1164]` |
| `export DATA_LIBRARY_PATH="${DATA_LIBRARY_PATH:-$VAR}"` | L28-31 | fnOS **不注入** `DATA_LIBRARY_PATH`；本机二进制里 0 次出现该串`[二进制]` |
| 版本号从 manifest 读并导出 `APP_VERSION` | L35-47 | 让 `/healthz` 报版本，升级是否生效一眼可查`[总纲 L40, L929]` |
| `trim_log()` 原地 tail-trim | L58-76 | 日志 5 MiB 上限、保留尾部 1 MiB；**默认值写在函数体内**（测试要 `sed` 提取函数） |
| `orphan_pids()` / `kill_pids()` / `wait_port_free()` | L87-133 | 启动前清理「占端口」与「命令行匹配 `$APP/server/run.py`」的陈旧进程`[总纲 L925-926]` |
| `wait_healthy()` 轮询 `/healthz` | L135-150 | 启动成功判据 = 真能响应健康检查，不是「进程还活着」 |
| `bin/*` 启动前补 `chmod +x` | L172-177 | 上传/升级会丢执行位 |
| `venv` 缺失时回落 `install_callback` | L179-186 | 自愈 |

### 2.5 传给脚本的 TRIM_* 环境变量

文档全表`[总纲 L222-248]`，并与 fnOS 二进制中实际出现的标识符交叉核对`[二进制 trim_app_center]`：

| 类别 | 变量 | 二进制可见 |
|---|---|---|
| 应用信息 | `TRIM_APPNAME`、`TRIM_APPVER`、`TRIM_OLD_APPVER`、`TRIM_APP_STATUS` | ✅ |
| 路径 | `TRIM_APPDEST`、`TRIM_PKGETC`、`TRIM_PKGVAR`、`TRIM_PKGTMP`、`TRIM_PKGHOME`、`TRIM_PKGMETA`、`TRIM_APPDEST_VOL` | ✅ 全部可见（`TRIM_APPDEST`/`TRIM_PKGHOME`/`TRIM_PKGMETA`/`TRIM_PKGTMP`/`TRIM_PKGETC`） |
| 用户/权限 | `TRIM_USERNAME`、`TRIM_GROUPNAME`、`TRIM_UID`、`TRIM_GID`、`TRIM_RUN_USERNAME`、`TRIM_RUN_GROUPNAME`、`TRIM_RUN_UID`、`TRIM_RUN_GID` | ✅ |
| 网络/资源 | `TRIM_SERVICE_PORT`、`TRIM_DATA_SHARE_PATHS`、`TRIM_DATA_ACCESSIBLE_PATHS` | ✅ |
| 日志/临时 | `TRIM_TEMP_LOGFILE`、`TRIM_TEMP_UPGRADE_FOLDER`、`TRIM_PKGINST_TEMP_DIR`、`TRIM_TEMP_TPKFILE` | ✅（另有 `INSTALL_VOLUME_ID`） |
| 系统上下文 | `TRIM_SYS_VERSION`(+`_MAJOR`/`_MINOR`/`_BUILD`)、`TRIM_SYS_ARCH`、`TRIM_KERNEL_VERSION`、`TRIM_SYS_MACHINE_ID`、`TRIM_SYS_LANGUAGE` | ✅ |
| 向导值 | wizard 的 `field` 名即环境变量名（**无 `TRIM_` 前缀**） | 骨架/总纲口径 `[总纲 L243-244]` |

**❌ 不存在的变量**：`DATA_LIBRARY_PATH`。总纲明确写了这一点`[总纲 L246-248]`；本机
`strings trim_app_center | grep -c DATA_LIBRARY_PATH` = **0**，`trim_open_gateway` 同样 = 0`[实测]`。
代码读它必须带 `TRIM_PKGVAR` 兜底，bash 侧必须 `export` 桥接，两边都兜才算稳
`[骨架 fnpack/cmd/main:28-31]`、`[骨架 server/run.py:33-35]`。

**❌ 向导字段禁用 `TRIM_` 前缀**`[总纲 L244]`（会与系统变量撞名）。

### 2.6 本项目（Docker Compose 管理）的落点

- 如果「管理器自身」是一个 Python/Go 常驻服务（提供 Web 面板），`cmd/main` 用骨架版；
  Compose 生命周期由 fnOS appcenter 托管（`config/resource` 的 `docker-project`）。
- 如果管理器的职责是「调 docker compose 管 ValleyServer 容器」，参考本机真实 docker 应用
  的做法：`cmd/main` 的 `start`/`stop` **什么都不做**，注释写明
  `# do nothing, docker application will be started by appcenter`；`status` 用
  `docker inspect <container> | grep '"Status": "running",'` 判 0/3
  `[已装包 /var/apps/homebox/cmd/main:19-42]`。
- 两种形态可以并存：管理器服务自己 `start/stop`（骨架版），被管理的 ValleyServer 容器
  由 `docker-project` 声明（§3.3）。

---

## 3. config/privilege 与 config/resource

两者都是**包根 `config/` 下的 JSON 文件，必须存在**，fnpack 会校验存在性与 JSON 合法性
（缺失/非法 JSON 直接打包失败，见 §6.5）`[实测]`。

### 3.1 config/privilege

**schema**（总纲 §2.6）`[总纲 L296-312]`：

```json
{
  "defaults": { "run-as": "package" },
  "username": "<appname>",
  "groupname": "<appname>",
  "join-groups": ["docker"]
}
```

| 键 | 含义与取值 |
|---|---|
| `defaults.run-as` | `package` = 以专用应用用户运行（**推荐**，fnOS 按 appname 自动建用户）；`root` = 以 root 运行（需跨用户 kill / 绑特权端口 / 管理外部服务时） |
| `username` / `groupname` | 专用应用用户/组名 |
| `join-groups` | 附加用户组：`docker`、`video`、`render`、`audio` 等；**声明后仍需用户在 WEB UI 授权**（设置 → 应用管理 → 该应用 → 权限设置） |

**本机三类真实写法** `[已装包]`：

1. 普通 package 应用（`family-message`）——username/groupname 与 appname 同名：
   ```json
   { "defaults": { "run-as": "package" }, "username": "family-message", "groupname": "family-message" }
   ```
2. **Docker 应用（homebox / mgvideo / pvz2-gardendless）——用户组名是 `docker-<appname>`**：
   ```json
   { "defaults": { "run-as": "package" }, "username": "docker-homebox", "groupname": "docker-homebox" }
   ```
   这是 `docker-project` 形态的既定约定，`data-share.permission.rw` 里也要写这个用户名（§3.3）。
3. 需要 root 的管理器（`deepseek.harness`，一个真实在跑的 fpk 管理器）——只写 `run-as`：
   ```json
   { "defaults": { "run-as": "root" } }
   ```

> **与本项目直接相关**：ValleyServer 管理器要操作 Docker（`docker compose`、`docker inspect`、
   挂载 `/var/run/docker.sock`），两种可行路线：
> ① `docker-project` 托管路线 → 用 `docker-<appname>` 身份，**不需要 root，也不需要 docker 组**；
> ② 自己调 docker 的路线 → 需要 `run-as: root` 或 `join-groups: ["docker"]`
>   （后者还要用户手动在 Web UI 授权）`[总纲 L309-310]`。
> 官方 docker 模板（`fnpack create --template docker`）走 ① `[实测]`。

### 3.2 config/resource

总纲给出 3 个 section`[总纲 L314-339]`，本机 50 个已装包实际出现的 section 有 **6 个**`[实测]`：

| section | 本机使用数 | 作用 |
|---|---|---|
| `data-share` | 15 | 声明共享目录（Windows ACL 模型），经 `TRIM_DATA_SHARE_PATHS` 或 `/var/apps/<app>/shares/` 软链访问 |
| `usr-local-linker` | 6 | 把包内文件链到 `/usr/local/bin|lib|etc/` |
| `docker-project` | 4 | **形态 C 必需**，交给 fnOS 管理 Compose 项目生命周期 |
| `systemd-unit` | 13 | 官方/系统应用用（如 `{}` 空对象即可），把包内 unit 交给 fnOS 管理 |
| `api-scope` | 6 | 申请 fnOS API 权限，字符串数组，如 `trim.file.sharedAccess`、`trim.file.userAccess`、`trim.file.path` |
| `backup` | 2 | 备份集成（`trim.media`、`trim.photos`） |

`data-share` 写法（骨架与已装包一致）`[骨架 fnpack/config/resource:1-12]`、`[已装包]`：

```json
{
  "data-share": {
    "shares": [
      { "name": "myapp/data", "permission": { "rw": ["myapp"] } }
    ]
  }
}
```

- `name` 是**共享目录名**，官方脚手架默认生成两个：`<appname>` 与 `<appname>/data` `[实测 fnpack create]`。
- `permission.rw` 列出**可读写该共享目录的应用用户名**；整段不写 `permission` 也合法
  （官方脚手架默认就不带 permission）`[实测]`。
- 运行期落点：`/var/apps/<appname>/shares/<name>`；官方 docker 模板的 compose 文件里
  直接用绝对路径 `/var/apps/<appname>/shares/<appname>/data:/data` `[实测 fnpack create --template docker]`。

`api-scope` 真实写法`[已装包 /var/apps/deepseek.harness/config/resource、miyin、fndepot]`：

```json
{ "api-scope": ["trim.file.sharedAccess", "trim.file.userAccess"] }
```

> 与本项目相关：如果管理器要读写用户目录 / 调 fnOS 文件 API，应声明 `api-scope`
> （不必照抄参考项目的全部 scope，用不到就删`[总纲 L619]`）。

### 3.3 `docker-project` 怎么声明（本项目形态 C 的关键）

**官方脚手架生成的原样写法**`[实测 fnpack create probedocker --template docker]`：

```json
{
  "docker-project": {
    "projects": [
      { "name": "probedocker", "path": "docker" }
    ]
  },
  "data-share": {
    "shares": [
      { "name": "probedocker" },
      { "name": "probedocker/data" }
    ]
  }
}
```

**本机真实 docker 应用（homebox）原样**`[已装包]`：

```json
{
  "docker-project": { "projects": [ { "name": "homebox", "path": "docker" } ] },
  "data-share": {
    "shares": [
      { "name": "homebox",      "permission": { "rw": ["docker-homebox"] } },
      { "name": "homebox/data", "permission": { "rw": ["docker-homebox"] } }
    ]
  }
}
```

规则总结：

1. `projects[].path` 相对 **`app/`** 目录；`path = "docker"` ⇒ 包内路径 `app/docker/`。
2. 该目录内必须有 `docker-compose.yaml`（**注意是 `.yaml`**；本机 4 个 docker 应用的
   落盘文件全部叫 `docker-compose.yaml`）`[已装包]`。
3. 落盘后位置：`$TRIM_APPDEST/docker/docker-compose.yaml`，即
   `/vol1/@appcenter/<appname>/docker/`，**属主是 `docker-<appname>` 用户**`[已装包]`。
4. 官方 docker 脚手架的 compose 里用 `{host_port}:{container_port}` 这类**占位提示**，
   需要开发者替换成真实端口`[实测]`；而真实包（mgvideo）证实 compose 文件里**可以用
   wizard 变量做 `${wizard_xxx}` / `${wizard_xxx:-默认值}` 替换**：

   ```yaml
   environment:
     - mport=${wizard_user_port:-7788}
     - mrateType=${wizard_user_type:-2}
   ```
   `[已装包 /vol1/@appcenter/mgvideo/docker/docker-compose.yaml]`
   —— 这是本项目「安装时让用户填端口/口令 → 直接进 compose」的现成机制。

### 3.4 本项目建议

- 采用 `docker-project`，`name = valleyserver`（或与 appname 一致），`path = docker`。
- `privilege` 用 `username/groupname = docker-valleyserver`，`run-as = package`。
- ValleyServer 2.x 的 Compose 需要三个 host 端口：`24642/udp`（游戏联机）、`5800`（Web）、
  `5900`（VNC），以及 `29103`；镜像 `lixeer/valley-server:v26.5`，卷 `./Mods`、`./Saves`
  `[上游 /vol1/…/valleyserver-work/upstream/oneclick-script/docker/docker-compose.yml:1-17]`。
  这些端口都不在 fnOS 保留段内（见 §8-端口）。
- 持久化数据（`Mods/`、`Saves/`）建议挂到 `data-share` 声明的目录，而不是包内相对目录，
  否则升级/重装会丢（§4）。

---

## 4. 数据分区：TRIM_APPDEST / PKGVAR / PKGHOME / PKGETC / PKGTMP

### 4.1 五个变量

`[总纲 L209-220]`，并与本机真实 symlink 完全吻合`[实测]`：

| 变量 | 绝对路径 | 放什么 | 升级时 | 卸载时 | 本机 `/var/apps/<app>` 中的形态 |
|---|---|---|---|---|---|
| `TRIM_APPDEST` | `/vol{n}/@appcenter/{app}` | 代码/二进制 | **整体替换** | 删 | `target -> /vol1/@appcenter/<app>` |
| `TRIM_PKGVAR` | `/vol{n}/@appdata/{app}` | 运行数据、日志、PID、socket | ✅ 保留 | 视向导 | `var -> /vol1/@appdata/<app>` |
| `TRIM_PKGHOME` | `/vol{n}/@apphome/{app}` | **venv、配置、用户数据、被管理服务凭证** | ✅ 保留 | 视向导 | `home -> /vol1/@apphome/<app>` |
| `TRIM_PKGETC` | `/vol{n}/@appconf/{app}` | 配置 | ✅ 保留 | 视向导 | `etc -> /vol1/@appconf/<app>` |
| `TRIM_PKGTMP` | `/vol{n}/@apptemp/{app}` | 临时文件 | — | 删 | `tmp -> /vol1/@apptemp/<app>` |
| `TRIM_PKGMETA` | （元数据） | 元数据 | — | — | `meta -> /vol1/@appmeta/<app>`（本机 `deepseek.harness` 的 meta 为空目录） |

本机实测的 `/var/apps/family-message/` 目录（`[实测]`）：

```
ICON.PNG  ICON_256.PNG  manifest  cmd/  shares/
config/   ← 注意：drw-r--r--（0644，无 x 位，非 root 无法遍历）
wizard/   ← 同上
etc  -> /vol1/@appconf/family-message
home -> /vol1/@apphome/family-message
meta -> /vol1/@appmeta/family-message
target -> /vol1/@appcenter/family-message
tmp  -> /vol1/@apptemp/family-message
var  -> /vol1/@appdata/family-message
```

### 4.2 铁律

> **venv、`config.yaml`、数据库、被管理服务的凭证目录一律放 `$TRIM_PKGHOME`**
> （`/vol1/@apphome/<appname>/`）。放 `TRIM_APPDEST` 会在每次升级时被覆盖掉。`[总纲 L219-220]`

推论对本项目：**被管理的 ValleyServer Compose 目录、Saves、Mods、管理器自身配置/数据库、
Docker Compose 生成文件，全部应放 `$TRIM_PKGHOME` 或 `data-share`**，
绝不能放 `$TRIM_APPDEST`（升级即丢）。

### 4.3 Docker 应用的卷落点

真实 docker 应用把持久数据挂到 `data-share` 声明的目录，落盘在
`/vol1/@appcenter/<app>/shares/<name>`。官方 docker 脚手架直接写成
`/var/apps/<appname>/shares/<appname>/data:/data`（`/var/apps/<app>/shares/` 是到真实
share 的稳定入口）`[实测 fnpack create --template docker]`。真实包更常用相对路径
`./config`、`./logs`（相对 `$TRIM_APPDEST/docker/`）`[已装包 prunemate-cn]`——
**但这意味着数据在 `@appcenter` 下，升级会被替换**，本项目不要照抄。

### 4.4 HOME 与 DATA_LIBRARY_PATH 的兜底（两个致命坑）

1. **`HOME` 必须无条件覆盖为 `$TRIM_PKGHOME`**，写法只能是
   `export HOME="${TRIM_PKGHOME:-/vol1/@apphome/<app>}"`，**绝不能写 `${HOME:-…}`**：
   fnOS 拉起脚本时已注入 `HOME=/root`，`:-` 只在变量为空时生效 ⇒ 保留 `/root` ⇒
   被管理子服务去 `/root/.<tool>/` 找凭证（空的）⇒ 登录不上`[总纲 L49-50, L1156-1164]`。
   三处必须一致：`cmd/main`、`cmd/install_callback`、`cmd/upgrade_callback`（骨架还加了
   `config_callback`）`[骨架 fnpack/cmd/main:14-19]`、`[骨架 fnpack/cmd/install_callback:9-13]`、
   `[骨架 fnpack/cmd/upgrade_callback:9-12]`、`[骨架 fnpack/cmd/config_callback:4-6]`。
   Go/Python 侧再兜一层：`HOME_DIR = TRIM_PKGHOME or HOME` 并 `os.environ["HOME"]=HOME_DIR`
   `[骨架 server/run.py:37-41]`。
2. **`DATA_LIBRARY_PATH` 必须桥接 + 代码兜底**（fnOS 不注入，§2.5）。
   骨架已在 `cmd/main`、`install_callback`、`upgrade_callback` 里
   `export DATA_LIBRARY_PATH="${DATA_LIBRARY_PATH:-${TRIM_PKGVAR:-…}}"`，
   并在 `server/run.py` 里 `or os.environ.get("TRIM_PKGVAR","")` `[骨架 server/run.py:33-35]`。

**自检手段**：骨架的 `/healthz` 会报 `home` 与 `data_library_path` 两个字段；
若 `home` 报 `/root`，一眼就能断定 `HOME` 覆盖没生效
`[SKILL.md:278]`、`[骨架 server/run.py:106-119]`。

---

## 5. 桌面入口（app/ui/config）与统一网关

### 5.1 三处必须对齐（否则「装完点开空白」）

1. `manifest.desktop_uidir = ui`
2. `manifest.desktop_applaunchname = <appname>.<entry>`
3. `app/ui/config` 的 `".url"` 里存在**同名 key** `<appname>.<entry>`

`[总纲 L378-381]`。**好消息：fnpack 1.2.4 会强制校验这两条对齐**`[实测]`：

```
The entry name "other.main" in "app/ui/config" should start with myapp
The value of desktop_applaunchname in manifest does not match any entry name defined in app/ui/config
```

### 5.2 `app/ui/config` 字段表

骨架/官方/真实包三种样例一致的核心结构`[骨架 fnpack/app/ui/config:1-14]`、`[总纲 L385-399]`：

```json
{
  ".url": {
    "family-message.main": {
      "title": "家庭消息",
      "icon": "images/icon_{0}.png",
      "type": "iframe",
      "protocol": "",
      "gatewayPrefix": "/app/family-message",
      "gatewaySocket": "family-message.sock",
      "url": "/app/family-message",
      "allUsers": true
    }
  }
}
```

| 键 | 取值/含义 |
|---|---|
| `title` | 桌面显示名；**建议写常量**（见 §5.3） |
| `icon` | 固定写 `images/icon_{0}.png`（**照抄，唯一被特殊处理的裸花括号**）`[总纲 L419]` |
| `type` | `iframe`（fnOS 桌面窗口内打开）/ `url`（浏览器标签页） |
| `protocol` | 走统一网关时**置空**；写死 TCP 端口时才用 `http` |
| `gatewayPrefix` | 公开路径，形式 `/app/<name>` |
| `gatewaySocket` | **socket 文件名**（放在 `$TRIM_APPDEST` 下） |
| `url` | iframe 打开的相对地址；走网关通常是 `/app/<name>` 或 `/app/<name>/` |
| `allUsers` | 是否所有用户可见；管理员专属面板写 `false` |
| `fileTypes` | 注册文件打开方式时的扩展名列表 |
| `control.accessPerm` | `editable` / `readonly` / `hidden` |

**本机真实的多入口 + control 实例**（管理类应用的极佳参考）`[已装包 /vol1/@appcenter/deepseek.harness/ui/config]`：

```json
{
  ".url": {
    "deepseek.harness.Application": {
      "title": "Harness Admin", "icon": "images/icon_{0}.png",
      "type": "iframe", "protocol": "http",
      "gatewaySocket": "web.sock", "gatewayPrefix": "/app/deepseek-harness",
      "url": "/app/deepseek-harness/", "allUsers": false,
      "control": { "accessPerm": "readonly", "fullUrlPerm": "readonly",
                   "portPerm": "readonly", "pathPerm": "readonly" }
    },
    "deepseek.harness.ui.Application": {
      "title": "Harness WebUI", "icon": "images/icon_{0}.png",
      "type": "iframe", "protocol": "http",
      "gatewaySocket": "web.sock", "gatewayPrefix": "/app/deepseek-harness",
      "url": "/app/deepseek-harness/fngateway/", "allUsers": false
    }
  }
}
```

要点：**两个入口可以共用同一个 socket**；`gatewayPrefix` 可以带连字符
（appname 是 `deepseek.harness`，前缀是 `/app/deepseek-harness`）——即前缀不强制等于 appname；
`control` 的 4 个 Perm 键在总纲里只提到 `accessPerm`。

### 5.3 占位符：只认 `${...}`

- fnOS 的解析正则是 **`\$\{\s*([^}\s]+)\s*\}`**（本机 `trim_app_center` 二进制里能直接搜到这个
  正则字符串`[二进制]`），即**只替换 `${xxx}`**。
- 写 `{port}`、`{display_name}` 会**原样保留**并进入 iframe 地址 ⇒ 空白页`[总纲 L417-420]`。
- **例外**：图标尺寸 `images/icon_{0}.png` 是唯一被特殊处理的裸花括号（所有应用都这么写）。
- 稳妥做法：`title` 写常量；需要变量时用 `${...}`，并确认变量确实存在。
- **注意脚手架会误导你**：`fnpack create` 生成的 `app/ui/config` 默认写的是
  `"title": "{display_name}"`、`"port": "{port}"`、`"url": "/{url-path}"`——这些是**给人填的占位提示**，
  不是运行时替换语法（同理 docker 模板 compose 里的 `{host_port}`）`[实测]`。
- **反证（`${...}` 确实会被替换）**：真实包 `trim.music` 的 manifest 里写着
  `helpurl="${common.helpurl}"`；真实包 `mgvideo` 的 compose 里写着
  `${wizard_user_port:-7788}` 并正常工作`[已装包]`。

### 5.4 统一网关链路（本机反向确认，含「不剥前缀」的机制）

本机 nginx 配置 `[nginx /usr/trim/nginx/conf/conf.d/trim_http_cgi.conf:5-12]`：

```nginx
location /app/ {
    proxy_pass http://unix:/var/run/trim_http_cgi.socket:;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 300s;
}
```

- `proxy_pass` 后面**只有 `unix:...:`，不带任何路径**⇒ nginx 按原 URI 转发，**因此网关不剥
  `/app/<name>` 前缀**，原样透传给应用。这就是硬规则 #14 的实现层证据
  `[总纲 L47, L429-431]`。
- 应用侧必须：① 自己剥前缀（FastAPI/Starlette 只设 `scope["root_path"]`，**绝不改写
  `scope["path"]`**）；② 对**裸前缀**（`/app/<name>` 无结尾斜杠）返回 **307** 跳到
  `/app/<name>/`，否则页面内相对资源（`css/app.css`）会解析成 `/app/css/...` 全 404。
  骨架已实现：`GatewayPrefixMiddleware` `[骨架 server/run.py:85-99]`。
- 前端 API 用**相对路径**（`api/status` 而非 `/api/status`）即可在前缀下工作
  `[总纲 L432]`、`[骨架 server/run.py:122-129]`。

### 5.5 Unix socket 与权限

- 后端必须监听 **Unix 域套接字**，路径 `$TRIM_APPDEST/<name>.sock`，文件名与
  `gatewaySocket` 一致；可与 TCP 端口并存`[总纲 L423-424]`。
- **socket 文件权限 0666**（网关进程不一定是应用用户）；**所在目录必须对应用用户可写**，
  否则降权启动后 bind 失败。`run-as: root` + 降权执行的应用，其 app 目录默认 `root:root`，
  需在 `install_callback`/`upgrade_callback`/`cmd/main start` 里
  `chgrp $RUN_USER "$APP" && chmod 775 "$APP"`，并在 stop/start 清理残留 socket`[总纲 L425-428]`。
- 本机真实 socket 列表（模式各异，说明 0666 是应用自己设的，fnOS 不代设）`[实测]`：

```
srw-rw-rw- root:root            /vol1/@appcenter/deepseek.harness/web.sock
srw-rw-rw- family-message:family-message  /vol1/@appcenter/family-message/family-message.sock
srwxrwxrwx hermes-agent:hermes-agent      /vol1/@appcenter/hermes-agent/hermes-agent.sock
srw-rw---- fygo-browser:fygo-browser      /vol1/@appcenter/fygo-browser/app.sock
srwxr-xr-x fn-installer:fn-installer      /vol1/@appcenter/fn-installer/fn-installer.sock
```

骨架实现：`socket.socket(AF_UNIX)` → `os.chmod(sock_path, 0o666)`；**绑定失败要降级为
「只跑 TCP」而不是启动失败**`[骨架 server/run.py:143-172]`。

### 5.6 网关注入的可信 Header

| Header | 说明 | 示例 |
|---|---|---|
| `X-Trim-Userid` | 当前用户 UID | `1000` |
| `X-Trim-Isadmin` | 是否管理员 | `true` / `false` |
| `X-Trim-Username` | 当前用户名 | `admin` |

`[总纲 L434-440]`。**本机二进制证据**：这三个 Header 名字出现在
`/usr/trim/bin/trim_http_cgi` 的字符串里（`[二进制]`，该程序正是 §5.4 中 nginx
`/app/` 反代的后端）——即在网关链路上注入、可被应用直接信任。

> 另外本机还可见其它 fnOS Header（**不属于网关给应用的**，勿混用）：
> `X-Trim-Safe-Code-Challenge`（security_service）、`X-Trim-Device` /
> `X-Trim-LicenseCode` / `X-Trim-LicenseSign` / `X-Trim-DL`（trim_license）、
> `X-Trim-Sign` / `X-Trim-Ts`（liveupdate）、`X-Trim-Source`（trim）`[二进制]`。

### 5.7 网关自检命令

```bash
curl -s --unix-socket "$TRIM_APPDEST/<name>.sock" -o /dev/null -w '%{http_code}\n' \
     http://localhost/app/<appname>/
curl -s -o /dev/null -w '%{http_code}\n' http://<NAS>:5666/app/<appname>/   # 需登录态 cookie
```
`/app/<appname>/` 返回 200 HTML、css/js 200 即入口可用`[总纲 L442-450]`。

### 5.8 更名（appname）牵连清单

改了 appname 必须同步：`manifest` 全部相关字段 → **`app/ui/config` 的 key、`gatewayPrefix`、
`gatewaySocket`、`url`（最易漏、后果最严重）** → `cmd/*` 与 `app/**/*.sh` 里的默认路径
（`/var/apps/<appname>`、`/vol1/@appdata/<appname>`、`/vol1/@apphome/<appname>`、socket 文件名）
→ 应用内网关前缀常量、`config/privilege` 的 username/groupname、Python 包目录名 →
构建产物名、README/文档。收尾必跑 `grep -rn '<旧名>' .` 确认零残留`[总纲 L452-460]`。

---

## 6. fnpack 打包

### 6.1 本机版本与路径（`[实测]`）

| 项 | 值 |
|---|---|
| 路径 | `/usr/local/bin/fnpack` |
| 版本 | **1.2.4**（`fnpack --help` 首行 `Version 1.2.4`；无 `--version`/`-v`/`version` 子命令） |
| 体积 | 3,711,160 字节 |
| 类型 | ELF 64-bit LSB executable, x86-64, **statically linked**, stripped |

总纲提到的 `fnpack-1.2.3-linux-amd64` 与本机 1.2.4 是相邻版本；本机所有实测结论基于 **1.2.4**。
云编译要求把二进制**提交进仓库**（官方下载页返回 HTML 不是二进制）`[总纲 L712-717]`。

### 6.2 命令用法（`[实测]`）

```bash
fnpack --help
fnpack build                      # 打包当前目录
fnpack build -d <dir>             # 等价 --directory <dir>
fnpack create <appname>           # 普通应用（native）
fnpack create <appname> -w        # 等价 --without-ui（无桌面入口）
fnpack create <appname> -t docker # Docker 应用模板
```

`fnpack build --help` 输出：
`Build command (fnpack build) will package the application source into an fpk file.`
唯一 flag：`-d, --directory string  Directory of the application source, defaults to current directory (default ".")`。

### 6.3 产物命名

**fnpack 产出的文件名是 `<appname>.fpk`，不含版本号**（实测输出原文：
`Packing successfully. The output file myapp.fpk can be found in the working directory.`）。
骨架的 `build-fpk.sh` 会把它复制成 `<appname>_<version>.fpk` 作为交付名
`[骨架 build-fpk.sh:82-87]`——**两套命名要分清**，别去找 `test.py` 式的版本化 fnpack 输出。

### 6.4 产物结构（双层 tar，`[实测]` 完整清单）

`myapp.fpk` 外层（tar.gz）：

```
app.tgz
cmd/                  （含 9 个脚本，模式位被原样保留）
config/privilege
config/resource
ICON.PNG
ICON_256.PNG
manifest
wizard/config  wizard/install  wizard/uninstall
```

`app.tgz` 内层（也是 tar.gz）：

```
ui/config
ui/images/icon_64.png
ui/images/icon_256.png
config/privilege         ← 注意：fnpack 把 config/ 也复制进了 app.tgz
config/resource
```

> 实测发现：**fnpack 1.2.4 会把 `config/privilege` 与 `config/resource` 同时放进包根和
> `app.tgz` 内部**（`app/` 里并没有这个目录）。运行期 `$TRIM_APPDEST` 下因此也有一份
> `config/`。这是观察到的行为，不做因果解释。

### 6.5 fnpack 1.2.4 的校验清单（逐项实测，含对总纲的修正）

| # | 检查项 | 结果 | 原始输出 |
|---|---|---|---|
| 1 | `manifest` 存在 | ❌ 拦 | `Required file manifest is missing` |
| 2 | 缺 `appname` | ❌ 拦 | `Required field appname is missing in manifest file` |
| 3 | 缺 `source` | ❌ 拦 | `Required field source is missing in manifest file` |
| 4 | 缺 `desc` / 缺 `platform` | ✅ **不拦** | — |
| 5 | 未知字段（`arch`/`icon`/`description`/`developer`/`port`/`category`/`main`） | ✅ **不拦** | — |
| 6 | `config/privilege` 存在 | ❌ 拦 | `Required file config/privilege is missing` |
| 7 | `config/resource` 存在 | ❌ 拦 | `Required file config/resource is missing` |
| 8 | `privilege` 是合法 JSON | ❌ 拦 | `Parse privilege file … failed: invalid character 'n' looking for beginning of object key string` |
| 9 | `ICON.PNG` / `ICON_256.PNG` 存在 | ❌ 拦 | `Required file ICON.PNG is missing` / `Required file ICON_256.PNG is missing` |
| 10 | `cmd/main` 存在 | ❌ 拦 | `Required file cmd/main is missing`（删整个 `cmd/` 也是这条） |
| 11 | **`cmd/*` 有执行权限** | ✅ **不拦**（⚠️ 修正总纲） | `[总纲 L729]` 表格称会校验；实测把 `cmd/*` 全部 `chmod -x` 后仍 `Packing successfully`，且**产物里 `cmd/main` 保持 644** |
| 12 | `app/` 目录存在 | ❌ 拦（报错信息绕） | `Compress /tmp/fnpack.<ts>/app.tgz file error : lstat /tmp/fnpack.<ts>/bin: no such file or directory` |
| 13 | `app/<desktop_uidir>/` 存在 | ❌ 拦 | `Compress … app.tgz file error : lstat …/app/ui: no such file or directory` |
| 14 | `wizard/` 目录存在 | ✅ **不拦**（整个目录可缺） | — |
| 15 | `wizard/*` 是合法 JSON | ❌ 拦 | `File "install" is not valid due to JSON format or content validation failure.` |
| 16 | `app/ui/config` 是合法 JSON | ❌ 拦 | `Parse config file … failed: invalid character 'o' in literal null (expecting 'u')` |
| 17 | `app/ui/config` 的 key 以 `<appname>.` 开头 | ❌ 拦 | `The entry name "other.main" in "app/ui/config" should start with myapp` |
| 18 | `desktop_applaunchname` 能在 `ui/config` 里找到 | ❌ 拦 | `The value of desktop_applaunchname in manifest does not match any entry name defined in app/ui/config` |
| 19 | `appname` 大小写/下划线 | ✅ **不拦**（`MyApp`、`my-app` 均成功；`My_App` 只是被第 17 条连累） | — |
| 20 | `privilege.username` 与 appname 不一致 | ✅ **不拦** | — |

**结论**：`[总纲 L719-729]` 的「打包前校验清单」基本正确，**唯一需要修正的是第 11 条**
（`cmd/*` 执行权限 fnpack 不校验）。也就是说：**忘记 `chmod +x cmd/*` 不会在打包期报错，
只会在装机时失败**——这正是硬规则 #2 的由来`[总纲 L30]`。

### 6.6 ⚠️ 打包失败时退出码仍为 0（本机 8 类失败全部复现）

**本节结论全部由本机真实运行得到**（`fnpack build` 输出 + `echo $?`）：

| 失败类型 | 输出关键行 | 退出码 | 是否产出 fpk |
|---|---|---|---|
| 缺 `manifest` | `Packing failed. Required file manifest is missing` | **0** | 无 |
| 缺 `appname` | `Packing failed. Required field appname is missing in manifest file` | **0** | 无 |
| 缺 `source` | `Packing failed. Required field source is missing in manifest file` | **0** | 无 |
| 非法 version | `Packing failed. Invalid version "0.1.22-streamfix". Expected format: x.y.z[-r] , where x , y , z , and r are integers.` | **0** | 无 |
| 缺 `cmd/main` | `Packing failed. Required file cmd/main is missing` | **0** | 无 |
| 缺 `ICON_256.PNG` | `Packing failed. Required file ICON_256.PNG is missing` | **0** | 无 |
| `privilege` 非法 JSON | `Packing failed. Parse privilege file … failed: …` | **0** | 无 |
| `ui/config` key 不匹配 | `Packing failed. The entry name … should start with …` | **0** | 无 |

**两个次生陷阱**：

1. **`&&` 链会把失败当成功**：`cmd && fnpack build && mv <app>.fpk …` 会继续走到 `mv`，
   报 `cannot stat` —— 一个与真实原因完全无关的错误，排查成本极高`[总纲 §4.1 陷阱 1]`。
2. **陈旧 fpk 会让「文件存在性检查」失效**：本机实测中，改造 version 后重新 build 失败，
   但**上一轮产生的 `myapp.fpk` 仍在目录里**；此时 `if [ -f "$STAGE/$APPNAME.fpk" ]` 会判定成功。
   `[骨架 build-fpk.sh:70]` 用 `rm -f "$STAGE"/*.fpk` 规避了这一点——**任何自定义打包脚本都必须先删旧产物**。

**正确判据（必须同时做）**：

```bash
LOG=<打包日志文件>
if ! grep -q "Packing successfully" "$LOG"; then
    cat "$LOG" >&2            # 把 fnpack 的真实报错原样打出来
    echo "fnpack 打包失败" >&2; exit 1
fi
```

**绝不要把 fnpack 输出重定向到 `/dev/null`**，否则永远看不到失败原因`[总纲 §4.1]`。

> **骨架现状提醒**：`[骨架 build-fpk.sh:80-92]` 只做「产物文件是否存在」判断，
> **没有 grep `Packing successfully`**。因为它先 `rm -f *.fpk`（L70），所以在当前版本下
> 「文件不存在 ⇒ 失败」仍然成立；但它**不会把 fnpack 的真实报错打给人看**。
> 项目落地时应把 L80-92 升级为 grep 判据 + 打印日志。

### 6.7 version 格式实测矩阵

`fnpack 1.2.4` 的实际接受情况（`[实测]`）：

| 版本串 | 结果 | 版本串 | 结果 |
|---|---|---|---|
| `1.0.0` | ✅ | `1.0.0-beta` | ❌ |
| `1.0.0-1` | ✅ | `1.0.0-` | ❌ |
| `1.0.0-720` | ✅ | `1.0` | ❌ |
| `1.0.0-0` | ✅ | `1` | ❌ |
| `01.0.0` | ✅（前导零也收） | `1.0.0.0` | ❌ |
| | | `v1.0.0` | ❌ |

即：**`x.y.z[-r]`，每段必须是整数**；想表达「测试版/预发布」，把语义放到 GitHub Release 的
prerelease 标记与 changelog 里，**不要放进版本号字符串**`[总纲 §4.1 陷阱 2]`。

**同 `appname` + 同 `version` 重装 → `errno 10236`（应用已存在）；每次重新打包都要 bump version**。
本机 `trim_app_center` 二进制中存在 `10236` 字符串`[二进制]`（未做触发实验，见 §10）`[总纲 L902-908]`。
上传重名文件时 trim-cli 会自动改名（`x.fpk` → `x_1.fpk`），**装机前务必 `tar tzf` 看 manifest 的 version 行**。

### 6.8 打包后必须验证的命令（双层 tar）

```bash
# 外层结构
tar tzf <appname>.fpk
#   期望：manifest / cmd/ / config/ / wizard/ / ICON.PNG / ICON_256.PNG / app.tgz

# 内层 app.tgz
tar xzOf <appname>.fpk app.tgz | tar tzf -
#   期望（形态 B）：bin/* + ui/config
#   期望（形态 A）：server/ + www/ + ui/config
#   期望（形态 C）：docker/docker-compose.yaml + ui/config
```

`[总纲 L690-692, L731-736]`、`[实测]`（本机已按此验证完整清单，见 §6.4）。
`[总纲 附录 D]` 的交付 checklist 也把这两条列为必做`[总纲 L2121-2122]`。

### 6.9 暂存目录与 ACL 陷阱（绝不在仓库目录里直接 build）

`fnpack build` 会对它处理的目录施加限制性 ACL（`getfacl` 可见 `user::---` +
`default:user::rwx`），打包失败一次后目录会变成 `d---------`，连属主都 `rm -rf` 不掉；
更坑的是 **`cp -r` 会把这些 ACL 一起复制出去**`[总纲 L640-642, L41-42]`。

标准做法（骨架 `build-fpk.sh` 已实现）`[骨架 build-fpk.sh:21-37]`：

1. `chmod -R u+rwX "$STAGE_ROOT"` → `rm -rf "$STAGE"`（先修权限才删得掉）
2. `mkdir -p "$STAGE"`，**断言目录为空**（防止旧文件打进包）
3. `cp -r "$FPK_SRC/." "$STAGE/"`
4. **立刻 `chmod -R u+rwX "$STAGE"`，再往里 mkdir/清理**
   —— 从带 TrimACL 的目录 `cp -r` 出来的子目录可能直接是 `d---------`，
   `cp` 不报错，但紧接着的 `mkdir` 会 `Permission denied`（实测就是卡在这里）
5. 清 `__pycache__`/`*.pyc`/`.DS_Store`/开发用 `config.yaml`/旧 `*.fpk`
6. `chmod +x cmd/*`（`app/bin/*` 也补执行位）
7. `( cd "$STAGE" && fnpack build )`

**另外**：项目里非标准目录（`.venv`、`node_modules`）也会被 fnpack 打进包，报
`copy_file_range: is a directory` 失败 ⇒ 必须移出项目目录`[总纲 L114-116]`。

暂存根默认建议：`/vol1/@apphome/<app>/data/fpk-build`（骨架用
`${APP_BUILD_ROOT:-/vol1/@apphome/hermes-agent/data/fpk-build}`）`[骨架 build-fpk.sh:15]`。

### 6.10 云编译（NAS 无 Go/npm 时）

GitHub Actions 完整 workflow 见 `[骨架 .github/workflows/build-fpk.yml:1-74]`，要点`[总纲 L738-817]`：

- 顺序关键：**先 `npm run build` 出 `frontend/dist`，再编译 Go**（`go:embed` 会把它编进去）。
- `//go:embed all:frontend/dist` —— **`all:` 前缀必须有**，写 `frontend/dist/*` 会漏掉
  `assets/` 子目录`[总纲 L43, L562]`。
- `actions/upload-artifact` 下载到 NAS 时**必须 `-L`**（302 到对象存储，否则拿到空文件）。
- 手动触发 dispatch 返回 **204 且 body 为空，别 `json.loads`**。
- `.gitignore` 要放行 `fnpack/fnpack-*`（工具二进制入库）`[骨架 .gitignore:10-11]`。
- NAS 侧免装工具链：`export PATH=/var/apps/nodejs_v24/target/bin:$PATH`。

---

## 7. 骨架文件清单（fnos-fpk-template）

### 7.1 完整目录树与每个文件的作用

路径前缀 `/vol1/@appdata/deepseek.harness/dsh-data/skills/fnos-fpk/assets/fnos-fpk-template/`：

```
├── .github/workflows/build-fpk.yml   # 云编译 workflow（Go+Node 步骤默认注释掉，按形态启用）
├── .gitignore                        # 忽略 dist/node_modules/bin/*.fpk；放行 fnpack-* 二进制
├── README.md                         # 6 步上手（改名→放源码→生成图标→打包→沙箱模拟→装机）
├── build-fpk.sh                      # 本地打包（独立暂存目录 + ACL 防护 + 内层源码拷贝）
├── fnpack/                           # ← fnpack 处理的包目录（appname 目录）
│   ├── manifest                      # INI 元数据，17 行，样板 myapp 0.1.0
│   ├── ICON.PNG                      # 64×64 应用中心图标
│   ├── ICON_256.PNG                  # 256×256 应用中心图标
│   ├── app/ui/config                 # 桌面入口 JSON（单入口 myapp.main，走统一网关）
│   ├── app/ui/images/icon_64.png     # 桌面图标（小）
│   ├── app/ui/images/icon_256.png    # 桌面图标（大）
│   ├── cmd/main                      # 238 行生产级：陈旧进程清理/健康检查/日志轮转/HOME+DATA_LIBRARY_PATH
│   ├── cmd/install_init              # 校验 python312 存在
│   ├── cmd/install_callback          # 建 venv + pip + 生成 config.yaml + 补 bin 执行位
│   ├── cmd/upgrade_init              # 停服务 + 端口兜底 kill + 删 PID
│   ├── cmd/upgrade_callback          # 依赖失败不阻断 + 重新拉起服务
│   ├── cmd/uninstall_init            # 空
│   ├── cmd/uninstall_callback        # 按 wizard_data_action 决定是否删数据（带路径白名单）
│   ├── cmd/config_init               # 空
│   ├── cmd/config_callback           # 回写 wizard 值 + 重启
│   ├── config/privilege              # run-as: package + username/groupname
│   ├── config/resource               # data-share 示例
│   ├── wizard/install                # 安装向导（password + 端口 text）
│   ├── wizard/config                 # 设置页向导
│   └── wizard/uninstall              # 卸载向导（radio keep/delete）
├── server/requirements.txt           # Python 依赖清单（fastapi/uvicorn/websockets/pyyaml/httpx）
├── server/run.py                     # 184 行参考服务端：双监听(TCP+UDS)、网关前缀、/healthz 报版本、日志轮转
├── tools/make_icons.py               # 用 Pillow 生成 4 个尺寸图标
├── tools/simulate-install.sh         # 沙箱模拟安装（解包→install_init→install_callback→start/status→健康检查）
├── tools/test-logrotate.sh           # 日志轮转 6 条断言（从真实脚本 sed 提取 trim_log）
├── tools/test-upgrade.sh             # 升级路径 2 场景 9 条断言（含「依赖装不上不能把应用升级死」）
├── web/index.html                    # 参考前端首页
└── web/static/app.js  web/static/style.css
```

（逐文件读完确认；工作区副本 `/vol1/@appdata/deepseek.harness/home/github/fnos-fpk-template/`
与技能资产**逐字节一致**，`diff -r` 唯一差异是多了一个打包产物 `myapp_0.1.0.fpk`）`[实测]`。

### 7.2 tools/ 下的自动化测试脚本与运行方式

| 脚本 | 用途 | 运行方式 | 本机运行结果 |
|---|---|---|---|
| `tools/test-logrotate.sh` | 日志轮转 6 条断言：①限额内不动 ②超限保留尾部且丢残缺首行 ③**轮转后 inode 不变** ④**轮转后经同一 fd 写入仍落该文件** ⑤幂等 ⑥文件不存在不报错 | `./tools/test-logrotate.sh [cmd/main 路径]` | ✅ **全部通过**（`[实测]`） |
| `tools/test-upgrade.sh` | 升级路径 2 场景 9 条断言：①正常升级 `/healthz` 报新版本、upgrade_init 后端口=0/PID 已删 ②pip 失败仍必须拉起服务且失败原因写进 `$TRIM_TEMP_LOGFILE` | `./tools/test-upgrade.sh [骨架根目录]` | ✅ **全部通过**（`[实测]`，默认测试端口 `18879`） |
| `tools/simulate-install.sh` | 沙箱模拟真安装：解包 fpk → `install_init` → `install_callback`（建 venv + pip）→ `status`=3 → `start` → `status`=0 → `/healthz` → 首页 → unix socket → 日志尾 | `./tools/simulate-install.sh <app.fpk>` | ⚠️ **本次未运行**（见下） |

`simulate-install.sh` 未运行的原因：它默认写入 `SIM=${APP_SIM_ROOT:-/vol1/@apphome/hermes-agent/data/fpk-sim}`
`[骨架 tools/simulate-install.sh:10]`，而本次任务的写作用域**只允许写 platform-notes.md**，
不得在 `/vol1/@apphome/` 下创建目录；且 `install_callback` 会建 venv 并 `pip install`（需外网）。
脚本本身是安全设计良好的（停止模拟实例时必须带同样的 `TRIM_*` 环境变量，健康检查端口不硬编码）
`[骨架 tools/simulate-install.sh:88-91]`。

**两个测试脚本自带安全护栏，值得沿用**：
- `test-upgrade.sh` 把 `cmd/` 整体复制到 `mktemp -d` 沙箱再跑，并断言
  `TRIM_APPDEST`/`TRIM_PKGVAR`/`TRIM_PKGHOME`/`TRIM_PKGETC`/`TRIM_TEMP_LOGFILE`
  **全部落在沙箱内**，否则直接停手——因为脚本里的默认值是**真机上真实存在的路径**
  `[骨架 tools/test-upgrade.sh:27-36, 85-92]`。
- `test-logrotate.sh` 用 `sed -n '/^trim_log() {/,/^}$/p' "$MAIN"` **从真实脚本提取函数**再跑，
  不是复制一份代码（防漂移）；正因如此，`trim_log` 的默认值必须写在**函数体内**
  `[骨架 tools/test-logrotate.sh:21-28]`、`[总纲 L1301-1322]`。

### 7.3 骨架的已知缺口（落地时要补）

1. `build-fpk.sh` 不做 `grep "Packing successfully"` 判据（§6.6），只判产物文件是否存在。
2. 全套默认值都是 `myapp` / `/vol1/@apphome/myapp` 等（含 `cmd/*` 与 `server/run.py`），
   改名必须靠 `grep -rln 'myapp' . | xargs sed -i 's/myapp/<your-app>/g'` 全局替换
   `[骨架 README.md:10-12]`。
3. `manifest` 里 `service_port = 18801` 与本机真实应用 `family-message` 占用的端口相同
   `[已装包]`——**新项目必须换成别的端口**（提示：`tools/simulate-install.sh` 已专门警告
   「健康检查端口别硬编码，否则打到线上实例、结论是假的」`[骨架 tools/simulate-install.sh:33]`）。
4. 骨架 `fnpack/server/run.py` 是「参考服务端」，只适合形态 A/B；形态 C 需要
   `fnpack create --template docker` 的 `app/docker/docker-compose.yaml`。

### 7.4 参考项目（强烈建议本项目对齐）

总纲点名的 Go+Vue fpk 管理器参考模板：<https://github.com/yuexps/deepseek.harness.fnos>
`[总纲 L529-531]`。**该应用就装在本机**，可直接读它的落盘产物作为管理器形态的活样本：

- `/var/apps/deepseek.harness/manifest`（`platform=x86`、`micro_app=true`、
  `install_dep_apps=nodejs_v24`、`desktop_applaunchname=deepseek.harness.Application`）`[实测]`
- `/var/apps/deepseek.harness/config/privilege` → `{"defaults":{"run-as":"root"}}`（管理器需要 root）`[实测]`
- `/vol1/@appcenter/deepseek.harness/ui/config` → 两个桌面入口、共享 `web.sock`、
  `gatewayPrefix=/app/deepseek-harness`、`control.accessPerm=readonly` 等（§5.2）`[实测]`
- `/vol1/@appcenter/deepseek.harness/web.sock` → `srw-rw-rw-`（0666）`[实测]`

---

## 8. 硬规则速查（总纲第 0 章 20 条 + 本项目相关性）

`[总纲 L27-56]` 原文 20 条，逐条标注与本项目（Python/Go 后端 + Docker Compose 管理）的关系：

| # | 硬规则 | 与本项目 |
|:--:|---|---|
| 1 | `.fpk` = 固定目录结构的 `tar.gz`，用 `fnpack` 打包；manifest 用 INI，其余元数据用 JSON | 🔴 直接相关 |
| 2 | `cmd/*` 生命周期脚本必须 `chmod +x`（含 tools 脚本），否则安装直接失败 | 🔴 直接相关；且 **fnpack 不拦**（§6.5 第 11 条） |
| 3 | manifest 用官方字段名：`platform`（不是 `arch`）、`desc`（不是 `description`）、`maintainer`（不是 `developer`）、`service_port`（不是 `port`）；`source=thirdparty` 必填 | 🔴 直接相关；`arch` 一节有争议，见 §1.6 |
| 4 | 脚本内一律用 `$TRIM_*` 引用路径，绝不硬编码 `/var/apps/<app>` | 🔴 直接相关（管理器要拼容器卷路径，最容易硬编码） |
| 5 | `cmd/main status` 必须严格返回 0（运行中）/ 3（未运行） | 🔴 直接相关 |
| 6 | fnOS 不注入 `DATA_LIBRARY_PATH`（只有 `TRIM_PKGVAR`/`TRIM_APPDEST`）；代码必须带兜底，`cmd/main` 要 `export DATA_LIBRARY_PATH="${DATA_LIBRARY_PATH:-${TRIM_PKGVAR}}"` | 🔴 直接相关；本机二进制 0 次出现该串（§2.5） |
| 7 | `upgrade_init` 必须停服务（`main stop` + 按端口兜底 kill + 删 PID 文件），`upgrade_callback` 必须重新拉起 | 🔴 直接相关（否则「升级了但功能没变」） |
| 8 | 启动成功判据是能通过健康检查（轮询 `/healthz`），不是「进程还活着」；启动前清理陈旧进程 | 🔴 直接相关 |
| 9 | 把版本号暴露到 `/healthz` | 🔴 直接相关 |
| 10 | 绝不在仓库目录里直接 `fnpack build`（ACL 0000 传染，`cp -r` 也会复制出去）；一律独立暂存目录 | 🔴 直接相关 |
| 11 | `//go:embed` 必须写 `all:frontend/dist`（写 `frontend/dist/*` 会漏 `assets/`） | 🟡 Go 后端形态相关 |
| 12 | 带 wizard 或有 `install_dep_apps` 的应用，CLI 直装/卸载会被 fnOS 主动拒绝，只能走 App Center UI | 🟡 交付/测试流程相关（本项目大概率要 wizard 填端口/口令） |
| 13 | 同 `appname` + 同 `version` 重装报 `errno 10236`；改包必须 bump version | 🔴 直接相关 |
| 14 | 统一网关（`/app/<appname>`）不剥前缀，原样透传；应用自己剥，裸前缀要 307 跳斜杠 | 🔴 直接相关；本机 nginx 配置已证实机制（§5.4） |
| 15 | 走统一网关的后端要监听 Unix 域套接字（`$TRIM_APPDEST/<name>.sock`，权限 0666） | 🔴 直接相关 |
| 16 | 被管理子服务的凭证目录靠 `$HOME` 定位时，`HOME` 必须无条件覆盖为 `$TRIM_PKGHOME`，不能用 `${HOME:-…}` | 🔴 直接相关（管理器的被管理服务/子进程） |
| 17 | 长期挂着的 daemon 必须做日志上限与轮转，且轮转只能原地重写（不能 `mv`/`rename`） | 🔴 直接相关（管理器 daemon + 容器日志采集） |
| 18 | 卷根 `/vol1`、`/vol2` 权限 `0000`：应用用户 `ls`/`find`/`os.walk` 静默返回空；查卷内用 `trim-cli file ls` 或 WS API；真实落盘用 `stat` 验证（overlay 会骗人） | 🔴 直接相关（管理器要枚举/校验 Saves、Mods 目录） |
| 19 | UI / 图标改动必须先出截图给人确认再打包发布 | 🔴 流程相关 |
| 20 | 交付时不要顺手装到用户的 NAS 上，除非对方明确要求；装机验证先问 | 🔴 流程相关（本次任务亦遵守：未安装任何包） |

**端口规划**（硬规则之外但本项目必用）`[总纲 L1353-1365]`：

- 必须避开：`5666`/`5667`（fnOS Web UI）、`22`（SSH）、`445`/`2049`（SMB/NFS）、
  `5005`/`5006`（WebDAV）、`8000`（飞牛影视）。
- 第三方应用建议段：**18000–19999**。
- ValleyServer 2.x 自带端口 `24642/udp`、`5800`、`5900`、`29103`
  `[上游 oneclick-script/docker/docker-compose.yml:13-17]` 均不在保留段内。
- 注意本机 `18801` **已被真实应用 `family-message` 占用**`[实测，/var/apps/family-message/manifest]`，
  骨架默认值正好是 18801 ⇒ 新项目务必换端口。

---

## 9. 关键结论速览（供架构决策）

1. **形态选择**：本项目是「管理别人（ValleyServer Docker Compose）」⇒ 形态 B（Go/编译二进制 +
   Vue3 面板）或形态 A（Python 服务）做管理器主体，同时用 `config/resource.docker-project`
   声明被管理的 Compose 项目`[总纲 L61-72, L314-339]`。
2. **manifest 必填但 fnpack 不拦的字段**：`desc`、`platform` —— 必须人工保证（§1.1）。
3. **打包退出码不可信**：必须 `grep "Packing successfully"` + 先删旧 fpk（§6.6）。
4. **版本号只能是 `x.y.z[-r]`**，每段整数（§6.7）。
5. **`cmd/*` 必须 +x，且 fnpack 不检查**（§6.5 第 11 条）。
6. **数据一律放 `$TRIM_PKGHOME`**（升级保留），代码/二进制放 `$TRIM_APPDEST`（升级即替换）（§4）。
7. **`HOME` 无条件覆盖，`DATA_LIBRARY_PATH` 必须桥接**（§4.4）。
8. **统一网关不剥前缀**：应用自己剥，裸前缀 307（§5.4）；socket 0666（§5.5）。
9. **Docker 形态的用户名约定是 `docker-<appname>`**（§3.3）。
10. **compose 文件里可用 `${wizard_xxx}` / `${wizard_xxx:-默认}` 做安装期参数注入**（§3.3）。

---

## 10. 待验证（未能从文件/真机确认的点）

> 以下每一条都写清「我查了什么、为什么无法确认」。**绝不猜测**。

1. **官方文档原文未联网核实。** 本文「官方规范」部分全部转引自方法总纲的 `[官方规范]` 标注
   `[总纲 L10-11]`，本次任务未访问 <https://developer.fnnas.com/docs/guide/>。
   若需最高权威口径，应另行抓取官方页面核对（尤其 manifest 字段表与 wizard schema）。

2. **`arch` 是否为 fnOS 生产可用的字段、语义是什么。**
   查过：`/usr/trim/bin/trim_app_center` 的 `strings`（存在 `ini:"arch"` / `ini:"exclude_arch"`）、
   50 个已装包 manifest（18 个使用，含飞牛官方包 `trim.photos`/`trim.media`/`python312`）、
   `fnpack create` 脚手架（只生成 `platform`）。
   无法确认：无法在不安装包的前提下验证 `arch` 是否真的参与平台过滤/安装判定；
   也无法解释为什么官方包同时给 `platform` 与 `arch` 且取值不同（`x86` vs `x86_64`）。
   **处置**：本项目只写 `platform`。

3. **裸花括号 `{port}` / `{display_name}` 在 `app/ui/config` 中是否会被替换。**
   查过：`trim_app_center` 二进制中只有正则 `\$\{\s*([^}\s]+)\s*\}`（无 `{...}` 版本）；
   `fnpack create` 却生成 `{display_name}`/`{port}`/`{url-path}`。
   无法确认：没有在真机上安装一个用 `{...}` 的包来观察最终 iframe 地址。
   **处置**：按总纲实测口径只用 `${...}` 或常量；`{...}` 视为「开发者填空标记」。

4. **`fnOS` 安装时是否会替应用补 `chmod +x cmd/*`。**
   查过：`fnpack create` 生成的 stub 是 `-rw-r--r--`；已装应用 `/var/apps/*/cmd/*` 全是 `-rwxr-xr-x`。
   无法确认：无法区分「安装器补的」还是「打包者自己 chmod 的」。
   **处置**：无条件自己 `chmod +x`（硬规则 #2）。

5. **`cmd/main status` 返回 0/3 之外的码时 fnOS 的具体处理。**
   总纲只说「其它值会被 fnOS 判成异常」`[总纲 L34]`。
   查过：`trim_app_center` 字符串里有 `app.start.failed` / `APP_START_FAILED` 等错误标识，
   但未能定位 status 退出码的判定分支。
   **处置**：严格只返回 0/3，不打折扣。

6. **wizard JSON 的完整 schema。**
   查过：`fnpack` 只校验 JSON 合法性（非法 JSON 被拦，字段级错误不拦，`[实测]`）；
   总纲给的类型是 `text`/`password`/`radio`/`checkbox`/`select`/`switch`/`tips`
   `[总纲 L370]`，骨架用了 `type`+`stepTitle`+`items`+`label`+`initValue`+`helpText`+`rules`+`options`
   `[骨架 fnpack/wizard/*]`。
   无法确认：字段全集、各字段是否有额外约束（如 `rules` 支持哪些键）。
   另注：总纲 §2.7 出现「典型是 `dtype: radio`」`[总纲 L373]`，与骨架实际的 `"type": "radio"`
   `[骨架 fnpack/wizard/uninstall:6]` 不一致，且 fnpack 不校验 ⇒ **只能用 `type`**。

7. **`install_dep_apps` / wizard 导致 CLI 装机被拒的具体报错原文。**
   来源为总纲 `[总纲 L868-876]`，本次未在真机复现（未执行任何安装）。
   `[总纲 L2124]` 还建议用 `tools/simulate-install.sh` 替代真装验证——本次因写作用域限制未运行（§7.2）。

8. **`errno 10236` 的触发实验。**
   查过：`trim_app_center` 二进制里存在 `10236` 字符串；总纲给出「同 appname+version 重装」
   的因果`[总纲 L904]`。未做真实重装实验（不安装任何包）。

9. **`desktop_uidir` 的默认值。**
   总纲称默认 `ui``[总纲 L140]`；fnpack 实测在 `desktop_uidir = ui` 而 `app/ui` 缺失时
   会因压缩失败而拦下（`lstat …/app/ui`）⇒ 对该配置它**不是可选的**。
   无法确认「不写 `desktop_uidir` 时 fnOS 是否默认找 `ui`」。
   **处置**：显式写 `desktop_uidir = ui`。

10. **`config/resource` 各 section 的完整 schema（尤其 `systemd-unit`、`backup`、`api-scope` 的可选值域）。**
    查过：真实包中出现的键名与若干实例（§3.2）；`api-scope` 只见到
    `trim.file.sharedAccess` / `trim.file.userAccess` / `trim.file.path` 三种取值。
    无法确认：全部合法取值、以及 `docker-project` 是否支持多个 project、project `name`
    与 appname 不一致时的行为。

11. **`docker-project` 应用在「升级」时 compose 文件与容器卷的实际行为。**
    查过：homebox 的 `cmd/main` 注释称容器启停「由 appcenter 负责」`[已装包]`；
    compose 落在 `$TRIM_APPDEST/docker/`（升级会被替换）。
    无法确认：appcenter 在 upgrade 时是否会 `docker compose up -d` 重建容器、
    以及 `data-share` 卷是否会保留（本次未做真机升级实验）。

12. **`gatewayPrefix` 是否必须等于 `/app/<appname>`。**
    查过：骨架与 `family-message` 用 `/app/<appname>`；真实包 `deepseek.harness` 的
    appname 是 `deepseek.harness` 而 `gatewayPrefix` 是 `/app/deepseek-harness`。
    无法确认：前缀是「任意注册路径」还是「由某处（也许 app.json/DB）额外绑定」。
    **处置**：本项目让 `gatewayPrefix` 与 `url` 使用同一常量，并在 appname 定稿后
    用 §5.7 的自检命令验证 200。

13. **`X-Trim-Userid`/`Isadmin`/`Username` 之外，网关是否还注入别的可信 Header。**
    查过：`trim_http_cgi` 二进制中确认存在这三个名字`[二进制]`；本机其它
    `X-Trim-*` Header 分别属于 license/liveupdate/security 等其它服务。
    无法确认：是否还有（如用户组、语言）未出现的 Header。

14. **本机 `/var/apps/<app>/config` 与 `wizard` 目录模式为 `drw-r--r--`（0644，无 x）的后果。**
    查过：`stat -c '%A'` 实测；`fnOS` 自己能读（root），但非 root 用户无法遍历。
    无法确认：这是刻意的 ACL 形态还是 fnpack ACL 传染的残留；
    也未验证应用用户读取自身 `config/` 是否会失败。

15. **云端编译（GitHub Actions）与 `trim-cli` 装机命令本次完全未执行。**
    全部来自总纲 `[总纲 L738-817, L851-876]`，属文档转述，非本机验证。
    `trim-cli` 路径也不在本机 PATH（总纲给的是
    `/vol1/@apphome/hermes-agent/data/skills/trim-cli/scripts/trim-cli`）`[总纲 L854]`。

16. **`fnpack 1.2.3` 与 1.2.4 的行为差异。**
    本机只有 1.2.4；总纲若干实测（如 1.2.3 的报错文案）未逐条比对。
    本文所有 `[实测]` 结论**仅对 fnpack 1.2.4 有效**。

---

## 附：本次核查实际执行过的命令（可复现）

```bash
# 版本与能力
which fnpack; fnpack --help; fnpack build --help; fnpack create --help

# 用官方脚手架取「官方口径」
cd $(mktemp -d) && fnpack create probeapp          # native 模板
cd $(mktemp -d) && fnpack create probedocker -t docker

# 打包成功/失败矩阵（在 /tmp 独立暂存目录里做，避免污染仓库）
cp -r <骨架>/fnpack /tmp/x/pkg && chmod -R u+rwX /tmp/x/pkg && chmod +x /tmp/x/pkg/cmd/*
cd /tmp/x/pkg && fnpack build ; echo $?            # 成功：Packing successfully，exit 0
# 逐项破坏 manifest / config / ICON / cmd / ui config，观察报错与退出码（见 §6.5、§6.6）

# version 格式矩阵
# 对 1.0.0 / 1.0.0-1 / 1.0.0-720 / 1.0.0-0 / 01.0.0 / 1.0 / 1 / 1.0.0.0 / v1.0.0 / 1.0.0-beta / 1.0.0- 各跑一次 build

# 产物结构
tar tzf /tmp/x/pkg/myapp.fpk
tar xzOf /tmp/x/pkg/myapp.fpk app.tgz | tar tzf -

# 本体测试（全部通过）
cd <骨架> && ./tools/test-logrotate.sh
cd <骨架> && ./tools/test-upgrade.sh

# 真机反查
grep -h -oE '^[a-zA-Z_][a-zA-Z0-9_]*' /var/apps/*/manifest | sort | uniq -c | sort -rn
cat /var/apps/*/manifest | head
cat /var/apps/{family-message,homebox,deepseek.harness}/config/{privilege,resource}
cat /vol1/@appcenter/{family-message,deepseek.harness}/ui/config
stat -c '%A %U:%G' /var/apps/family-message /var/apps/family-message/config
ls -la /vol1/@appcenter/*/*.sock
strings -n 4 /usr/trim/bin/trim_app_center | grep -oE 'ini:"[a-zA-Z0-9_]+"' | sort -u
strings -n 4 /usr/trim/bin/trim_app_center | grep -oE 'TRIM_[A-Z0-9_]+' | sort -u
strings -n 5 /usr/trim/bin/trim_http_cgi | grep -oE 'X-Trim-[A-Za-z-]+' | sort -u
cat /usr/trim/nginx/conf/conf.d/trim_http_cgi.conf
```

（本机 `/vol1/@appdata/deepseek.harness/dsh-data/skills/fnos-fpk/` 下的任何文件均**未被修改**；
`valleyserver-work/upstream/` 仅读取。所有破坏性打包实验都在 `/tmp` 下的一次性目录中进行并已清理。）
