# 许可证与第三方声明

> 本文档同时作为 FPK 的许可证声明与再分发合规依据。凡与代码实现不一致处，**以本文档为准**
> 并需要修正实现。

## 1. 本管理器

| 项 | 值 |
| :--- | :--- |
| 名称 | ValleyServer 管理器（`appname = valleyserver`） |
| 许可证 | **Apache License 2.0** |
| 版权 | 与上游 ValleyServer 保持一致，随包附上游 `LICENSE` 副本 |
| 范围 | `fnos-fpk/` 目录下的全部源码与文档 |

本管理器**未修改**上游任何源码文件（`Mods/`、`src/`、`oneclick-script/` 均保持原样），
因此无需在上游文件上添加修改说明。若将来必须修改，须在文件头注明修改者与修改内容。

## 2. 上游项目

| 项 | 值 |
| :--- | :--- |
| 仓库 | https://github.com/Lixeer/ValleyServer |
| 基线 commit | `97604b6aa2388c10d767be63b4dc516f3bfec4fb` |
| 2.x 标签 | `v2.0.0` → `3cab814d429c4e2fa96207e56286cfdea3a9819f` |
| 许可证 | Apache License 2.0 |
| NOTICE 文件 | **不存在**（已核实） |

## 3. 逐组件许可证清单

### 3.1 随上游镜像分发（本项目**不打包、不再分发**）

| 组件 | 许可证 | 再分发要求 | 本项目处理 |
| :--- | :--- | :--- | :--- |
| Stardew Valley 游戏本体 | 🔴 **专有**（ConcernedApe） | **不得再分发** | FPK 内不含；仅运行时 `docker pull` 上游镜像获得 |
| SMAPI 4.5.1 | MIT | 保留声明 | 同上（在镜像内） |
| SMAPI 自带 MOD（SaveBackup / ConsoleCommands 4.5.1） | MIT | 保留声明 | 同上（在镜像内） |
| 上游镜像 `lixeer/valley-server:v26.5` | ⚠️ **镜像内未找到许可证文件**（已核实各层无 `LICENSE`/`COPYING`） | 授权状态不明 | 🚫 **本项目不使用也不分发该镜像**；它仅作为审计取证对象（用来核实上游基线版本），不参与运行 |

### 3.2 上游维护的 5 个 MOD（可由用户选择安装）

`v2.0.0` Release 的 `Mods.zip`（86,328 字节，
sha256 `1676fbcfe56e84f7aa5008fb1455a02747689860f6515481059926cb87d5628c`）包含：

| MOD | UniqueID | 版本 | 许可证 | 再分发要求 |
| :--- | :--- | :--- | :--- | :--- |
| ALOS | `Lixeer.Always_On_Server` | 1.0.6 | **MIT + 附加协议** | 基于 [perkmi/Always-On-Server-for-Multiplayer](https://github.com/perkmi/Always-On-Server-for-Multiplayer) 二次开发；**商业使用须署名原仓库地址** |
| ServerCMD | `Lixeer.ServerCMD` | 1.0.1 | Apache-2.0 | 保留声明 |
| ChatCommand | `Lixeer.ChatCommand` | 1.0.2 | Apache-2.0 | 保留声明 |
| CommandWebUI | `Lixeer.CommandWebUI` | 1.0.1 | Apache-2.0 | 保留声明 |
| ChangeServerPort | `Lixeer.ChangeServerPort` | 1.0.0 | Apache-2.0 | 保留声明 |

**本项目的处理**：FPK 内**不包含**这些 MOD 二进制。界面提供「从上游 Release 下载并安装」，
由用户点击确认，下载完成后展示实际 SHA-256 供核对；本项目不缓存、不再分发。

### 3.3 构建期 / 传递依赖（出现在 MOD 二进制中）

| 包 | 版本 | 用于 | 许可证 |
| :--- | :--- | :--- | :--- |
| `Pathoschild.Stardew.ModBuildConfig` | 4.3.2 / 4.4.0 | 全部 MOD 构建 | MIT |
| `Lib.Harmony` | 2.3.5 | ChangeServerPort | MIT |
| `Lidgren.Network` | 1.0.2 | ChangeServerPort | MIT |
| `EmbedIO` | 3.5.2 | CommandWebUI | MIT |
| `System.Text.Json` | 9.0.2 | ChangeServerPort | MIT |
| `Force.DeepCloner` | 源码使用但 `.csproj` **未显式声明** | ChatCommand | MIT（待验证，见审计报告 P8） |

### 3.4 第三方 MOD（SVE 等）

**只接受用户上传**。本项目：

* ❌ 不做在线 MOD 仓库爬取；
* ❌ 不做不稳定网页解析；
* ❌ 不自动下载第三方 MOD；
* ❌ 不把用户上传的 MOD 用于任何再分发。

界面提供「可插拔」位置，但首版不接入任何未授权来源。

### 3.5 本管理器自身的运行时依赖

**无第三方依赖**。后端只使用 Python 标准库；前端为原生 JS（无 npm 依赖、无构建步骤）。
这既是可靠性设计，也使许可证清单保持极简。

## 4. 再分发策略（合规结论）

1. FPK 内只含本项目自有代码 + 上游 `LICENSE` 副本 + 本项目文档。
2. FPK 内**不含**游戏本体、SMAPI、上游镜像、任何 MOD 二进制。
3. 上游 MOD 由用户在界面确认后从官方 Release 下载，本项目不参与分发。
4. 第三方 MOD 仅接受用户上传。
5. **不使用、不发布、不再打包上游 Docker 镜像**（其授权状态不明；仅在审计阶段用于取证）。
6. 本项目代码以 Apache-2.0 发布，欢迎符合该许可证条款的再分发。

## 5. 用户责任提示（界面「关于」页同步展示）

* 请通过合法渠道获得《星露谷物语》副本；本项目不提供游戏本体。
* 使用 ALOS 进行商业用途时，请署名
  [perkmi/Always-On-Server-for-Multiplayer](https://github.com/perkmi/Always-On-Server-for-Multiplayer)。
* 上游 2.x 处于维护模式；镜像与 MOD 的可用性依赖上游，本项目无法保证长期可获取。
