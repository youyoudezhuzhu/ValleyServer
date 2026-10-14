<div align="center">

<img src="icon-round.png" width="180" alt="ValleyServer" />

# ValleyServer · 飞牛 fnOS 原生部署

**在飞牛 OS 上用应用中心一键装好的《星露谷物语》多人服务器 —— 原生进程、不用 Docker、网页管理、浏览器就能操作画面、玩家下线也无人值守。**

[![Release](https://img.shields.io/github/v/release/youyoudezhuzhu/ValleyServer?filter=fpk-*&label=release)](../../releases/latest)
[![Tests](https://img.shields.io/badge/tests-687%20passing-2ea44f)](fnos-fpk/docs/known-issues.md)
[![Platform](https://img.shields.io/badge/platform-fnOS%20%C2%B7%20FPK-blue)]()
[![Upstream](https://img.shields.io/badge/upstream-Lixeer%2FValleyServer%202.x-lightgrey)](https://github.com/Lixeer/ValleyServer)
[![License](https://img.shields.io/github/license/youyoudezhuzhu/ValleyServer)](LICENSE)

[上游项目](https://github.com/Lixeer/ValleyServer) · [详细技术文档](fnos-fpk/README.md) · [缺陷与设计取舍](fnos-fpk/docs/known-issues.md) · [最新 Release](../../releases/latest)

</div>

---

## 这是什么

* 本仓库是上游 **[Lixeer/ValleyServer](https://github.com/Lixeer/ValleyServer)** 的 fork，**只走 2.x 路线**
  （MOD + 真实游戏客户端），把它封装成一个**飞牛 OS 应用包（`.fpk`）**。
* 新增内容全部在 **`fnos-fpk/`**：管理器、打包脚本、测试与文档。
  **上游的 `Mods/`、`src/`、`docs/` 等目录原样保留、不作修改**，以便持续跟上游合并；
  服务器内行为（自动睡觉、跳过剧情等）都由上游 MOD（ALOS 等）提供。
* 管理器负责**进程与状态**：启动/停止/重启、自动载档开服、崩溃自愈、快照兜底、网页配置与排障。

| 项目 | 值 |
| :--- | :--- |
| 管理器版本 | **0.2.24**（[Release](../../releases/latest)） |
| 部署方式 | fnOS 应用（FPK）+ 本机原生进程（**无 Docker**） |
| 上游基线 | `v2.0.0` · 游戏 1.6.15 · SMAPI 4.5.1 |
| 测试 | **687 项断言全绿**（582 Python + 35 前端 + 8 日志轮转 + 16 升级路径 + 46 沙箱安装/卸载） |
| 默认端口 | 面板 `18242` · 游戏 `24642/UDP` · VNC `5900` · WebVNC `5800` · CommandWebUI `29103` |

---

## 特性

| | 说明 |
| :--- | :--- |
| 🎮 **无人值守** | 上游 ALOS 在游戏内 22:00 让房主自动入睡、跳过剧情弹窗；玩家不在也能一直开服 |
| 💾 **存档优先** | 所有高风险操作前强制快照；**快照失败即中止操作**；恢复失败自动回滚并保留故障现场 |
| 🔒 **三步互相独立** | **Steam 扫码登录 → 下载游戏本体 → 启动服务端**：每一步都要你手动点，管理器不会替你串起来，也不会自动建存档 |
| 🖥️ **浏览器操作画面** | 内置 noVNC/WebVNC，经管理器反代（相对地址），直连/局域网/网关都能用 |
| ♻️ **睡眠与恢复** | 启动、重启、以及**每次快照/恢复之后**都会自动重新载入最新存档并开服 |
| 🛟 **无人兜底快照** | 无人在线且最近没有存档写入时**不停服**直接备份（默认每 5 分钟），不会回滚当天进度 |
| 🩺 **可排障** | 双状态模型（服务/农场）、**在线玩家数**、结构化日志、一键脱敏诊断包 |
| 📱 **移动端可用** | 界面三档响应式（桌面 / 平板 / 手机） |
| 🚫 **不需要 Docker** | 管理器直接在本机拉起 Weston+Xwayland（或用 Xvfb）、x11vnc、websockify 与游戏进程 |

---

## 快速开始

1. **安装**：从 [最新 Release](../../releases/latest) 下载 `valleyserver-<版本>.fpk` → 飞牛应用中心「手动安装」。
2. **扫码登录 Steam**：「设置与维护 → 游戏与运行环境 → Steam 扫码登录」（用**你自己的账号**）。
3. **下载游戏本体**：同页「获取 / 更新游戏本体」（走 Steam 官方下载；本仓库**不分发**游戏文件）。
4. **安装服务器组件**：「模组」页 →「下载并安装」上游维护的 MOD 包（含 ALOS / ServerCMD 等）。
5. **新建存档**：到「总览」页点「打开 WebVNC」，在游戏里**手动新建一个存档** ——
   管理器**故意不替你建**（自动建出来的存档玩家名/农场名都是空的）。已有存档可在「存档」页导入。
6. **启动**：回到「总览」点「启动服务器」；此后每次启动都会自动载入最新存档并开服。

> 提示：如果哪次没能自动载入农场，总览页会出现 **「手动载入最新存档并开服」** 按钮，点一下即可。

---

## 联机：怎么让朋友进来

| 场景 | 做法 |
| :--- | :--- |
| **同一局域网** | 游戏主菜单 →「加入 LAN 游戏」自动发现；或手动填 `NAS内网IP:24642` |
| **跨外网（推荐）** | Tailscale / ZeroTier 等组网，朋友加入同一虚拟网络后填**虚拟内网地址** `IP:24642`，**不用开公网端口** |
| 跨外网（备选） | 路由器把 **24642/UDP** 映射到 NAS，朋友填「公网IP:24642」（家用宽带多为 CGNAT，且有暴露风险） |
| ❌ 邀请码 / Steam 好友邀请 | **不可用**。本部署不在 NAS 上运行 Steam，游戏退化为 **LAN 服务器**，没有 lobby 邀请码（管理器能看到在线人数，但没有可分享的码） |

---

## 无人值守是怎么工作的

* **上游 ALOS** 管"游戏内行为"：22:00 让房主自动入睡、跳过节日/剧情、关闭弹窗。
* **本管理器**管"进程与状态"：自动载档开服、异常退出自动恢复、快照兜底、状态可视化。
* **无人在线时世界会暂停**（ALOS 原生行为，默认）：时间与天数不推进，等你回来继续。
  想要"没人也推进天数"，在「设置 → 启动与恢复策略」打开 **「无人时也推进天数」** ——
  注意上游把这两个行为绑在同一个 `debug` 开关上，**二者互斥**（开了推进就没暂停）。
* **无人兜底快照**：0 名玩家 + 最近 60 秒无存档写入 → 直接复制备份且**不停服**
  （停服再拉起会把世界回滚到上次日结存档）。间隔默认 **5 分钟**，受「快照保留策略」约束。

---

## 日常运维

| 页面 | 你可以做什么 |
| :--- | :--- |
| **总览** | 服务/农场双状态、**在线玩家数**、快捷操作、WebVNC 入口、手动载入存档并开服 |
| **控制台** | 实时日志（管理器 / 任务 / 游戏控制台）、SMAPI 指令、健康诊断 |
| **存档** | 导入 / 切换 / 删除 / 从快照恢复（删除是移入现场备份区，可人工找回） |
| **模组** | 安装 / 启用禁用 / 卸载（含每存档私有数据的保留） |
| **快照** | 手动与自动快照、深度校验、整包恢复、下载、保留策略 |
| **任务** | 长任务队列与历史、进度、失败原因、可取消标记 |
| **设置与维护** | 端口与监听、运行参数、启动与恢复策略、显示后端、备份策略、诊断包 |

---

## 安全说明（请读完）

* 管理面板 `18242` 需要**口令 + 会话 Cookie + CSRF**；口令安装时生成，明文只存于应用家目录（权限 600）。
* **WebVNC(5800) 无认证**、**VNC(5900) 用 8 位随机密码**、**CommandWebUI(29103) 无认证**（由上游模组自行监听所有网卡）。
  「监听范围」只影响 VNC/WebVNC（默认 `0.0.0.0`，可改为「仅本机」）。
* 建议只在**内网或 VPN**内使用；不要把 5800/5900/29103 直接暴露到公网。
* 管理器不会上传你的存档、账号或日志；诊断包在本地生成并**自动脱敏**（口令/令牌替换为 `[REDACTED]`）。

---

## 常见问题

**总览显示「尚未载入农场」，游戏在跑却没开服？**
点总览页的 **「手动载入最新存档并开服」**。管理器会发送
`ServerCMD.set_multiplayermode 2` + `ServerCMD.load_save <最新存档>`。
若提示没有存档，请先到 WebVNC 里新建一个。

**天数不推进 / 时间停住？**
无人在线时世界按设计暂停。想继续推进就打开「无人时也推进天数」。

**为什么没有邀请码？**
本部署不在 NAS 上运行 Steam，游戏以 LAN 服务器方式运行 —— 只能局域网/VPN/直连加入。

**卸载会删存档吗？**
卸载向导默认**保留数据**；只有选「删除全部数据」才会连游戏本体、存档、Steam 凭证一起删。
数据在应用家目录（`@apphome/valleyserver`），**升级不会删除**。

**升级要注意什么？**
版本号必须递增（同版本号会被应用中心拒绝）。升级时管理器先停服再换代码，避免旧进程占着端口。

---

## 开发与发布

```bash
cd fnos-fpk
tools/run-tests.sh                # Python + 前端 + 日志轮转 + 升级路径 + 沙箱安装/卸载
tools/build-fpk.sh                # 产物：dist/valleyserver-<版本>.fpk + build-info（sha256/size）
tools/simulate-install.sh <fpk>   # 在临时目录完整跑一遍安装/升级/卸载
```

* GitHub Actions 负责构建与发布（标签 `fpk-v<版本>`），产物附 `sha256` 与 `size`，可用于校验。
* Release 页面只保留**最近 3 个版本**；更早的构建仍可用 **tag** 追溯。
* 每个 bug 的"为什么会这样 + 怎么修"都记录在
  [`fnos-fpk/docs/known-issues.md`](fnos-fpk/docs/known-issues.md)，便于日后回归排查。

---

## 上游、致谢与许可

* 上游项目：**[Lixeer/ValleyServer](https://github.com/Lixeer/ValleyServer)** ——
  它包含两条主线：**2.x（MOD + Docker，维护模式）** 与 **3.x（反编译/mock 协议端，实验性）**；
  相关文档见上游的 [`docs/`](https://github.com/Lixeer/ValleyServer/tree/main/docs)。**本仓库只封装 2.x。**
* 服务器组件（ALOS / ServerCMD / CommandWebUI / ChangeServerPort / ChatCommand 等）版权归各作者所有；
  ALOS 基于 [perkmi/Always-On-Server-for-Multiplayer](https://github.com/perkmi/Always-On-Server-for-Multiplayer)
  （MIT + 附加协议），**商业使用请按上游要求署名**。
* 本项目**不分发**《星露谷物语》游戏本体 —— 请用你自己的账号通过 Steam 官方途径获取。
* 本仓库代码：MIT（见 [LICENSE](LICENSE)）。
* 免责：非官方项目，与 ConcernedApe / Chucklefish / Steam 无关；使用前请自行备份存档。
