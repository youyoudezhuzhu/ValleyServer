# ValleyServer 管理器（飞牛 OS 原生 FPK）

基于上游 **[Lixeer/ValleyServer](https://github.com/Lixeer/ValleyServer) 2.x 路线**开发的
**飞牛 OS 原生管理应用**，提供中文 Web 管理界面。

**原生部署**：管理器直接在本机运行 游戏客户端 + SMAPI + 自动化 MOD + 虚拟显示栈，
以本机进程方式运行，不依赖任何编排或运行时框架。

```text
飞牛 OS
├── ValleyServer 管理器（本 FPK，原生 Python 服务 + Web 面板）      ← 只做管理
│     ├── 服务器控制台 / 存档 / MOD / 快照 / 版本 / 任务 / 诊断
│     └── 驱动原生运行环境 ↓
│
└── 原生运行                                                        ← 只跑游戏
      Xvfb :99 ─┬─ openbox
                └─ x11vnc :5900 ── websockify :5800 (noVNC)
      StardewModdingAPI（游戏 + SMAPI，.NET 6 **自包含**，无需安装 .NET）
      显示栈由管理器捆绑在应用目录内提供，不修改系统包状态
```

## 当前版本与安装

| | |
| :--- | :--- |
| 版本 | **0.2.26**（原生部署） |
| Release | [fpk-v0.2.26](https://github.com/youyoudezhuzhu/ValleyServer/releases/tag/fpk-v0.2.26) |
| 安装包 sha256 | 见 Release 页（下载后可用 `sha256sum` 与 `build-info.txt` 对照） |
| 测试 | 686 项断言全绿（581 Python + 35 前端真实执行 + 8 日志轮转 + 16 升级路径 + 46 沙箱安装/卸载） |

安装：飞牛 OS 应用中心 → 手动安装 → 选择 Release 里的 `.fpk`。

**安装后还需要两步**（「总览」页会给出按钮，顺序执行即可）：

1. 安装虚拟显示组件（管理器自动下载并解包，**不修改系统包状态**）；
2. 取得游戏本体，**二选一**：
   * **Steam 扫码（推荐）**：「系统」页 →「Steam 扫码登录」（秒级完成）→ 再点「获取 / 更新游戏本体」下载（两个独立动作）；
   * 上传你自己已合法拥有的 **Linux 版**游戏本体 ZIP（**不需要带 SMAPI**，管理器会自动获取并整合）。
   ⚠️ 不要传 Windows 版：Windows 版不含 Linux 的 .NET 运行时，管理器会在安装前就拒绝并说明原因。

> 本项目**不再分发**《星露谷物语》游戏本体、SMAPI 与任何第三方 MOD。

## 这个项目解决什么问题

上游 2.x 提供了「能把游戏跑起来的容器化方案」，但**没有任何管理面**：

| 需求 | 上游 2.x | 本管理器 |
| :--- | :--- | :--- |
| 部署方式 | 容器化（上游方案） | **本机原生进程** |
| 启动 / 停止 / 监控 | 手工命令行 | 一键操作 + 三层健康判据 + 异常自动恢复 |
| 存档管理 | 无 | 列表 / 导入 / 导出 / 切换 / 恢复 / 删除（带完整性校验） |
| MOD 管理 | 手工放文件 | manifest 解析、依赖与重复检查、启停、覆盖安装、卸载 |
| 备份 | 上游自带的 SaveBackup 在容器化挂载下**实际不生效** | 独立快照系统（含一致性协议与校验） |
| 版本追溯 / 回滚 | 无 | 内容指纹版本档案 + **快照回滚** + 能力边界如实披露 |
| 诊断 | 无 | SMAPI 日志解析、兼容性提示、一键脱敏诊断包 |
| 安全 | WebVNC 无认证、VNC 固定密码 `041041`、CommandWebUI WebSocket 无认证 | 默认仅本机监听、随机 VNC 口令、面板鉴权 |

> 上游 2.x 已进入**维护模式**（只修崩溃与兼容性问题）。本项目**不改造为 3.x、不重写游戏逻辑、
> 不替换为其他服务器项目**，只在 2.x 之上补管理能力。

### 原生部署的依据（实测）

本项目已实测确认：

* 游戏本体与 SMAPI 是 **.NET 6 自包含**发布（`runtimeconfig.json` 用 `includedFrameworks`），
  机器上**不需要安装 .NET 运行时**；
* 唯一的硬依赖是**图形环境**（游戏是 SDL/OpenGL 程序）；
* 需要的那几个组件（Xvfb / openbox / x11vnc / websockify / noVNC）都能从 Debian 仓库取得，
  由管理器**捆绑进应用目录**并在运行时解析，**不写系统包数据库**；
* 飞牛 OS 已自带 `libGL`、`libSDL2`、`libX11`、`libopenal`、`libgbm`；
* 游戏本体与 SMAPI 由**用户自备**（本项目不打包、不再分发）。

⚠️ **必须有图形环境** —— 显示栈是必需的，
本项目自己提供它，而不是绕过它。完整实测记录见
[docs/native-deployment.md](docs/native-deployment.md)。

## 目录结构

```text
fnos-fpk/                       ← 本管理器（FPK 源码，全部内容）
├── fnpack/                     ← fnpack 打包目录（appname=valleyserver）
│   ├── manifest                元数据（INI）
│   ├── ICON.PNG / ICON_256.PNG 应用图标
│   ├── app/
│   │   ├── server/             后端：纯 Python 标准库，无 pip 依赖
│   │   │   ├── run.py          HTTP 服务（TCP + Unix socket、网关前缀、鉴权）
│   │   │   ├── api_handlers.py REST API
│   │   │   ├── orchestration.py 业务任务编排
│   │   │   ├── http_base.py    共享 HTTP 类型
│   │   │   └── mgr/            领域模块
│   │   │       ├── config.py        配置与数据目录
│   │   │       ├── util.py          路径/ ZIP / 子进程安全工具
│   │   │       ├── db.py            SQLite 持久层
│   │   │       ├── tasks.py         统一任务队列与状态机
│   │   │       ├── snapshots.py     快照系统（一致性协议）
│   │   │       ├── saves.py         存档管理
│   │   │       ├── mods.py          MOD 管理
│   │   │       ├── runtime_base.py  运行时接口与进程/端口工具
│   │   │       ├── native_runtime.py 原生运行后端（显示栈 + 游戏 + 3 层健康判据）
│   │   │       ├── xstack.py        虚拟显示栈供给（不改系统包状态）
│   │   │       ├── versions.py      版本档案（内容指纹）
│   │   │       ├── diagnostics.py   诊断与脱敏
│   │   │       ├── supervisor.py    后台监管（自愈/定时快照）
│   │   │       └── logmod.py        日志与原地轮转
│   │   ├── www/                前端（原生 JS，无构建步骤）
│   │   └── ui/                 桌面入口配置与图标
│   ├── cmd/                    9 个生命周期脚本（可执行）
│   ├── config/                 privilege / resource
│   └── wizard/                 install / config / uninstall 向导
├── tools/                      构建与测试脚本
├── tests/                      自动化测试
├── docs/                       原生部署、审计报告、架构、许可证、运维手册、测试报告
└── dist/                       构建产物（.fpk 与 build-info）
```

## 快速开始

### 1. 构建 FPK

```bash
cd fnos-fpk
./tools/run-tests.sh                       # 全部自动化测试
STAGE_ROOT=/tmp/fpk-build ./tools/build-fpk.sh
ls -l dist/                                 # valleyserver-<version>.fpk
```

也可以直接用 GitHub Actions：`Actions → Build FPK (ValleyServer Manager) → Run workflow`，
构建完成后下载 artifact。

### 2. 安装到飞牛 OS

应用中心 → 手动安装 → 选择 `dist/valleyserver-<version>.fpk`。
安装向导会询问端口、监听范围与 VNC 密码；**默认只监听本机**（安全优先）。

安装完成后：

* 从应用中心打开「ValleyServer 管理器」（走飞牛统一网关，无需额外端口）；
* 或直接访问 `http://<NAS-IP>:18242/`；
* 首次登录口令在应用数据目录：`/vol1/@apphome/valleyserver/initial-password.txt`
  （权限 600，登录后请立即修改）。

### 3. 首次启动服务器

1. 进入「配置」页确认端口与监听范围；
2. 回到「总览」，点击「安装 / 首次启动」，等待显示栈拉起与 SMAPI 加载（实测约 16 秒）；
3. 在「MOD」页安装 ALOS 等自动化 MOD（可一键从上游 Release 下载，也可自行上传）；
4. 在「存档」页导入或创建存档，并设为当前存档；
5. 创建一次手动快照作为基线。

## 安全设计（重要）

| 项 | 做法 |
| :--- | :--- |
| 接口面 | 只提供白名单操作，没有任意命令执行入口；不依赖也不暴露任何宿主机编排接口 |
| 启动配置 | 只接受白名单配置项（端口、显示、策略等）；**不接受任意 YAML / Shell / 宿主机路径** |
| 面板鉴权 | 网关访问信任 `X-Trim-Userid`；直连 TCP 用口令 + HttpOnly Cookie + CSRF 校验 |
| 危险端口 | WebVNC(5800)/VNC(5900)/CommandWebUI(29103) 默认仅 `127.0.0.1` |
| VNC 口令 | 安装时随机生成（上游方案里是硬编码的 `041041`，本项目不复用该做法） |
| 网关可信头 | **只在 Unix 套接字上信任** `X-Trim-*`；TCP 直连伪造这些头无效（修复的认证绕过） |
| ZIP 处理 | 防路径穿越、防符号链接、防特殊文件、防 ZIP 炸弹、限数量与体积 |
| 运行中保护 | 游戏运行时拒绝覆盖/删除/切换存档与 MOD |
| 操作可追溯 | 所有写操作进任务队列 + 审计表 |

## 数据生命周期

| 数据 | 位置 | 升级 | 默认卸载 | 「卸载并删除全部数据」 |
| :--- | :--- | :--- | :--- | :--- |
| 管理器代码 | `/vol1/@appcenter/valleyserver` | 替换 | 删除 | 删除 |
| **存档 Saves** | `/vol1/@apphome/valleyserver/data/Saves` | 保留 | **保留** | 删除（二次确认） |
| **MOD** | `/vol1/@apphome/valleyserver/data/Mods` | 保留 | **保留** | 删除（二次确认） |
| 快照 | `.../data/snapshots` | 保留 | **保留** | 删除（二次确认） |
| 版本档案 | `.../data/archives` | 保留 | 保留 | 删除 |
| 配置 / 任务库 | `/vol1/@apphome/valleyserver/manager` | 保留 | 保留 | 删除 |
| 运行进程 | 本机 | 不动 | 停止 | 停止并清理 |

**卸载、升级或重新安装管理器都不会默认删除游戏数据。**

## 文档导航

| 文档 | 内容 |
| :--- | :--- |
| [docs/ui-navigation.md](docs/ui-navigation.md) | **导航与路由**：5 个一级入口、旧路由兼容映射、双状态模型、术语规范、响应式三档 |
| [docs/field-migration.md](docs/field-migration.md) | 容器时代字段迁移到原生语义（自动、幂等、不触碰数据） |
| [docs/known-issues.md](docs/known-issues.md) | **已知问题与排查记录**（含核显后端、VNC 反代、真机故障） |
| [docs/native-deployment.md](docs/native-deployment.md) | **原生部署**：依赖清单、图形环境要求、实测证据 |
| [docs/audit-report.md](docs/audit-report.md) | 上游代码与许可证审计（含上游发行物实测证据，仅审计取证） |
| [docs/architecture.md](docs/architecture.md) | 架构、数据目录、任务状态机、API |
| [docs/platform-notes.md](docs/platform-notes.md) | fnOS FPK 打包规范核实（字段表、cmd、privilege/resource） |
| [docs/install-guide.md](docs/install-guide.md) | 安装 / 升级 / 卸载 / 备份 / 恢复 / 故障排查 |
| [docs/licensing.md](docs/licensing.md) | 上游与第三方许可证清单、再分发策略 |
| [docs/test-report.md](docs/test-report.md) | 自动化测试与验收结果（含未验证项） |
| [docs/version-archive.md](docs/version-archive.md) | 版本固定与回滚的实测证据 |

## 许可证

本管理器（`fnos-fpk/`）以 **Apache-2.0** 分发，与上游保持一致。
上游 ValleyServer 的 `LICENSE` 与版权声明一并保留。

**本包不包含**：游戏本体、SMAPI、任何第三方 MOD 二进制；也不依赖任何外部运行时或编排平台。
详见 [docs/licensing.md](docs/licensing.md)。

## 已知限制（如实披露）

* **回滚通过快照完成**：把游戏本体、MOD、存档与配置
  一起恢复到某个时点）。
* SMAPI 与游戏本体一起替换，**无法单独升级或降级 SMAPI**。
* 游戏本体需要你自己提供（本项目不再分发）。
* 「在线玩家列表」上游没有可靠接口，**首版明确不支持**，界面不展示推测数据。
* 使用 SVE 等内容 MOD 的兼容性必须实测确认，本项目的兼容性提示**不能代替测试**。
* 上游 2.x 处于维护模式，适合存量部署；新部署请自行评估上游给出的替代方案。
