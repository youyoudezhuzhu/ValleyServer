# 上游代码与许可证审计报告

> 项目：ValleyServer 2.x 的飞牛 OS 原生 FPK 管理器
> 审计对象：`https://github.com/Lixeer/ValleyServer`（2.x 路线）
> 审计日期：2026-10-11
> 审计方法：源码逐文件阅读 + Git/Release/镜像仓库 API 核对 + **镜像层直接提取验证**（不依赖 README 描述）

---

## 0. 审计结论摘要

> **补充（第 3 轮）：上游的容器方式不是技术必需。** 审计初期只看到上游文档给出的容器部署方式，
> 后续实测确认游戏本体与 SMAPI 是 **.NET 6 自包含**发布、唯一硬依赖是**图形环境**，
> 因此本项目采用**原生部署**。
> 详见 [native-deployment.md](native-deployment.md) 与 §16。


| 结论 | 判定 |
| :--- | :--- |
| 2.x 是否可继续作为管理对象 | ✅ 可以。上游 2.x 处于维护模式，但镜像、编排与 MOD 仍然可用且可获取 |
| 游戏本体是否在启动时下载/覆盖 | ✅ **不会**。游戏文件在构建期被 `COPY` 进镜像（已核实镜像层） |
| 能否固定游戏版本 | ✅ **可以**。镜像 digest 即版本锚点；字段级证据见 §5.1 |
| 游戏本体的完整回滚 | ✅ **可以**（回滚到旧镜像 digest 即回滚游戏本体），前提是旧镜像未被删除 |
| SMAPI 能否独立升级 | ❌ **不能**。SMAPI 与游戏本体被烘焙在同一镜像里，SMAPI 升级 = 换镜像 |
| 上游是否提供 HTTP 管理接口 | ❌ **不提供**。没有一个供外部调用的管理 API；管理只能经容器 + VNC + SMAPI 控制台 |
| 上游镜像健康检查 | ❌ **不存在**（镜像 config 中 `Healthcheck` 字段缺失），必须由 FPK 自建 |
| 上游 MOD 能否随 FPK 再分发 | ⚠️ 有条件。`Mods/` 5 个 MOD 为上游二次开发作品（MIT + 附加协议），**必须署名**；第三方 MOD 不得默认打包 |
| 上游自带 WebUI/VNC 的认证强度 | ❌ **不合格**。WebVNC 用固定密码 `041041`，`CommandWebUI` 的 WebSocket 无认证（上游 README 自承有渗透风险） |
| 是否存在未授权爬取/不稳定网页解析依赖 | ✅ 不存在。项目不使用任何在线 MOD 仓库爬取 |

**总体风险等级：中。** 核心数据面（Saves/Mods）路径明确、可完整备份；风险集中在**安全暴露面**与**版本升级链**两处。

---

## 1. 基线标识（可复现）

| 项 | 值 |
| :--- | :--- |
| 上游仓库 | `https://github.com/Lixeer/ValleyServer` |
| Fork 仓库 | `https://github.com/youyoudezhuzhu/ValleyServer` |
| 默认分支 | `main` |
| **基线 commit SHA** | `97604b6aa2388c10d767be63b4dc516f3bfec4fb`（2026-09-27 03:49:21 +0800，*docs: use a circular logo in READMEs*） |
| 2.x 标签 | `v2.0.0` → `3cab814d429c4e2fa96207e56286cfdea3a9819f`（2026-05-30） |
| `v2.0.0` 是否为 `main` 祖先 | ✅ 是（`git merge-base --is-ancestor v2.0.0 main` 成立） |
| **2.x 镜像** | `lixeer/valley-server:v26.5` |
| **镜像 manifest digest** | `sha256:d84f924108b0fed5c459f5276a56bbb47fe450c0de2db807a55990adfd58e1b5` |
| 镜像 config digest | `sha256:bf38c16d993f626af9367679d27e9997038f20525e910b6b9a99db4ced30cb79` |
| 镜像平台 | `linux/amd64` 单一架构（上游发行源 仅 amd64，**无 arm64**） |
| 镜像总层大小 | ≈ 3.39 GiB（10 层，最大 3 层各 ≈ 813 MB） |
| 上游发行源 可用 tag | **仅 `v26.5` 一个**（`page_size=100` 查询 count=1）→ 无历史版本可选 |
| 分支 | `main`、`d-server-fix`（2026-06-05）、`dedicated_server_dev`（2026-06-02） |
| 标签 | `mod`、`v1.0.1`–`v1.0.7`、`v2.0.0`、`v3.0.0`–`v3.0.3` |

> ⚠️ **注意**：`main` 分支的 HEAD 是**3.x 文档**，不是 2.x 运行代码。2.x 的运行面只有
> `oneclick-script/`（容器编排）+ `Mods/`（SMAPI MOD）+ 一个**不在本仓库内**的发行镜像。
> 本项目管理器**不修改 3.x 源码，也不把 3.x 代码混入 2.x 运行路径**。

### 1.1 与 2.x 相关的文件清单（全仓库仅 76 个受版本控制文件）

| 目录 | 文件数 | 与 2.x 运行的关系 |
| :--- | :--- | :--- |
| `oneclick-script/` | 4 | **直接相关**：`run.sh`、上游构建脚本、上游编排文件、`startapp.sh` |
| `Mods/` | 34 | **直接相关**：5 个 MOD 的源码/C# 工程/README/manifest |
| `docs/v2/` | 2 | 文档 |
| `src/ValleyServer/` | 22 | **3.x 实验代码**，与 2.x 运行无关（不纳入本项目运行路径） |
| `.github/workflows/` | 3 | 上游 CI（其中 `release.yml` 自动发 3.x 版本） |
| 其他 | 11 | LICENSE / README / 图标 / 文档 |

---

## 2. 2.x 运行架构（已核实事实）

```text
宿主机（飞牛 OS）
└── 容器（上游发行方式，本项目不采用）
    └── stardew_server   ← 上游发行镜像（已烘焙游戏本体）
        ├── /content/Stardew Valley/      ← 游戏 1.6.15 + SMAPI 4.5.1（镜像内，只读语义）
        │   ├── StardewModdingAPI         ← 启动入口（可执行）
        │   ├── Mods/                     ← 镜像内有 2 个 SMAPI 自带 MOD，但**被卷挂载覆盖**
        │   └── Content/                  ← 游戏资源（4190 个文件，783 MB）
        ├── /root/.config/StardewValley/Saves/   ← 存档目录（**卷挂载点**）
        ├── Xvfb :99 (1280x720x24)        ← 虚拟显示器
        ├── x11vnc :5900（固定密码 041041）
        ├── websockify/novnc :5800        ← WebVNC
        └── openbox                        ← 窗口管理器
```

### 2.1 `startapp.sh` 实际行为（`oneclick-script/` 下的 `startapp.sh`，共 15 行，逐行核实）

| 行 | 行为 | 影响 |
| :--- | :--- | :--- |
| 3 | `Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +extension RANDR +render -noreset -core &` | 虚拟显示；分辨率固定 1280x720x24 |
| 4 | `export DISPLAY=:99` | |
| 5 | `sleep 3` | 固定等待，非就绪探测 |
| 6 | `openbox-session &` | 窗口管理器 |
| 7 | `cd content/Stardew\ Valley` | **相对路径**；镜像 `WORKDIR` 为 `/`，故实际进入 `/content/Stardew Valley` |
| 9–10 | `x11vnc -display :99 -forever -shared -passwd "041041" -rfbport 5900 -noxdamage -bg` | 🔴 **密码硬编码在脚本里** |
| 12 | `websockify --web=/usr/share/novnc/ 5800 localhost:5900 &` | 🔴 **noVNC 页面无任何认证**，能连 5800 即可操作游戏 |
| 14–15 | `export DISPLAY=:99` + `./StardewModdingAPI` | SMAPI 在前台运行；**无 `--log`、无标准输出重定向** |

**关键推论：**
1. 容器内**没有任何"检查游戏是否启动成功"的机制**；SMAPI 输出只进 `容器日志`。
2. 镜像**没有 Healthcheck**（已核实 config：`Healthcheck` 缺失）→ FPK 必须自建健康判据。
3. `x11vnc` 密码是**镜像内常量**，无法通过 compose 环境变量改变 → 要改密码必须**替换 startapp.sh**
   （FPK 采用「管理器提供自己的 entrypoint 包装脚本」的方式解决，见 §8）。
4. `sleep 3` + 无就绪探测 → 冷启动判定需要较长 `start_period`。

### 2.2 端口（上游 `docs/v2/README.md` + compose 双向核实）

| 端口 | 协议 | 用途 | 暴露风险 |
| :--- | :--- | :--- | :--- |
| `24642` | **UDP** | 游戏联机端口 | 设计上需对外开放 |
| `5800` | TCP | WebVNC (noVNC) | 🔴 无认证，可操作游戏 |
| `5900` | TCP | 原生 VNC | 🔴 固定密码 `041041` |
| `29103` | TCP | `CommandWebUI`（SMAPI 控制台） | 🔴 WebSocket 无认证，可执行任意 SMAPI 命令 |

> `ChangeServerPort` MOD 的**源码默认值是 5885**（`Mods/ChangeServerPort/ModConfig.cs:3`），
> 但 `v2.0.0` Release 里打包的 `config.json` 是 `{"port": 24642}`
> （已下载 `Mods.zip` 核实）。**两者不一致**，本项目以发布包为准并显式管理该文件。

---

## 3. MOD 审计

### 3.1 仓库内 5 个 MOD（全部为上游 Lixeer 二次开发）

| MOD | UniqueID | 版本 | 目标框架 | 作用 | 许可证/来源 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `ALOS` | `Lixeer.Always_On_Server` | 1.0.6 | `net6.0` | **无人值守核心**：自动睡觉、跳过大部分节日/事件、暂停 bug 补丁 | MIT + 附加协议，基于 [perkmi/Always-On-Server-for-Multiplayer](https://github.com/perkmi/Always-On-Server-for-Multiplayer)；**商业使用须署名原仓库**（`Mods/ALOS/README.md` 末段） |
| `ServerCMD` | `Lixeer.ServerCMD` | 1.0.1 | `net6.0` | 无头模式控制指令（`load_save`/`get_save_list`/`new_save`/`set_multiplayermode`/`get_save_path`/`tp`） | 上游自有，Apache-2.0 |
| `ChatCommand` | `Lixeer.ChatCommand` | 1.0.2 | `net8.0` | 游戏内聊天框 `!cmd>` 前缀执行控制台命令 | 上游自有，Apache-2.0 |
| `CommandWebUI` | `Lixeer.CommandWebUI` | 1.0.1 | `net6.0` | 浏览器中的 SMAPI 控制台（HTTP + WebSocket） | 上游自有，Apache-2.0。**上游 README 自承有渗透风险** |
| `ChangeServerPort` | `Lixeer.ChangeServerPort` | 1.0.0（源码 manifest）/ 1.0.0（发布） | `net6.0` | 用 Harmony 改写 Lidgren `NetPeerConfiguration.Port` | 上游自有，Apache-2.0 |

**第三方 NuGet 依赖（来自 `.csproj`，会随 MOD 二进制再分发）：**

| 包 | 版本 | 用于 | 许可证 |
| :--- | :--- | :--- | :--- |
| `Pathoschild.Stardew.ModBuildConfig` | 4.3.2 / 4.4.0 | 全部 MOD（构建期） | MIT |
| `Lib.Harmony` | 2.3.5 | `ChangeServerPort` | MIT |
| `Lidgren.Network` | 1.0.2 | `ChangeServerPort` | MIT |
| `EmbedIO` | 3.5.2 | `CommandWebUI` | MIT |
| `System.Text.Json` | 9.0.2 | `ChangeServerPort` | MIT |
| `Force.DeepCloner` | （源码中 `using Force.DeepCloner`，未在 csproj 显式声明） | `ChatCommand` | MIT（**待验证**：未在 csproj 见到显式声明，疑由 ModBuildConfig 传递引入） |

> ⚠️ **`ChatCommand.csproj` 未声明 `Force.DeepCloner`，但 `ModEntry.cs` 使用了它**
> （`Mods/ChatCommand/ModEntry.cs:1`）。这属于**上游构建配置与源码不一致**：
> 本地 `dotnet build` 可能因缺少引用而失败，取决于 `Pathoschild.Stardew.ModBuildConfig`
> 是否传递引入。**本项目不修改该 MOD 核心逻辑**，仅记录为已知问题（见 §7）。

### 3.2 MOD 清单的字段不统一（影响 FPK 的 manifest 解析健壮性）

| MOD | manifest 字段风格 | 问题 |
| :--- | :--- | :--- |
| `ALOS` | Microsoft 风格（`Name`/`Version`/`EntryDll`） | 正常 |
| `ServerCMD` | 同上 | 正常 |
| `ChatCommand` | 同上 | 正常 |
| `CommandWebUI` | 同上 | 正常 |
| `ChangeServerPort`（**源码**） | Microsoft 风格 | 正常 |
| `ChangeServerPort`（**v2.0.0 发布包**） | 小驼峰（`version`/`name`/`author`/`EntryDLl`）且 **`EntryDLl` 大小写错误** | 🔴 SMAPI 的 manifest 解析**大小写不敏感**，所以尚能工作，但说明发布包与源码脱节 |

**结论：FPK 的 MOD manifest 解析器必须大小写不敏感**（SMAPI 本身如此），否则会误判
`ChangeServerPort` 为损坏 MOD。本项目实现按 SMAPI 语义做大小写不敏感读取。

### 3.3 镜像内自带的 SMAPI MOD（已核实）

| MOD | 版本 | 说明 |
| :--- | :--- | :--- |
| `SaveBackup` (`SMAPI.SaveBackup`) | 4.5.1 | SMAPI 官方 MOD，**每天自动备份一次存档到自己的文件夹** |
| `ConsoleCommands` (`SMAPI.ConsoleCommands`) | 4.5.1 | SMAPI 官方 MOD，提供控制台指令 |

> 🔴 **重要交互**：`Mods/` 被宿主机目录挂载**覆盖**后，这两个镜像自带 MOD **将不再可见**。
> 也就是说上游镜像自带的「每日存档备份」在标准部署下**实际不生效**。
> FPK 必须自己提供自动快照（本项目已实现），**不能依赖上游的 SaveBackup**。

---

## 4. 数据路径与生命周期（已核实）

### 4.1 容器内外路径映射

| 容器内路径 | 宿主机（上游 compose） | 内容 | 谁管理 |
| :--- | :--- | :--- | :--- |
| `/content/Stardew Valley` | 无挂载（镜像内） | 游戏 1.6.15、SMAPI 4.5.1、游戏资源 | 镜像 |
| `/content/Stardew Valley/Mods` | `./Mods` | 所有 MOD（含 MOD 私有数据 `/data/`） | **用户/FPK** |
| `/root/.config/StardewValley/Saves` | `./Saves` | 存档（每个存档一个目录） | **用户/FPK** |
| `/root/.config/StardewValley/ErrorLogs` | **无挂载** ⚠️ | SMAPI 错误日志 | 容器（**重建即丢**） |
| 其余文件系统 | 无挂载 | 操作系统层 | 容器 |

**已核实**（提取镜像层 `09-9e59...`）：
* `/content/Stardew Valley/` 内共 **4190 个文件、783 MB**，其中游戏本体为 `Stardew Valley.dll`
  （2024-12-27），SMAPI 为 `StardewModdingAPI.dll` + `StardewModdingAPI` 启动器（2026-01-26）。
* 镜像内**没有** `Saves/` 目录，也没有任何 `Saves` 挂载点以外的存档数据 → 存档完全由用户持有。
* 镜像内自带 `Mods/{SaveBackup,ConsoleCommands}`（各 2 文件），但如上所述被挂载覆盖。

### 4.2 ⚠️ 未被挂载的数据（升级/重建会丢）

`/root/.config/StardewValley/ErrorLogs/`（SMAPI 错误日志）**不在任何卷里**。
一旦执行受控重建（`容器编排 down && up`）或镜像更新，**历史错误日志全部丢失**。
FPK 的策略：把容器日志采集到宿主机数据目录（见 §8.4），保证日志可追溯。

### 4.3 MOD 私有数据的隐藏位置

`ALOS` 把每个存档的运行数据写在自己的 MOD 目录下：

```csharp
// Mods/ALOS/ModEntry.cs:140,146
var data = this.Helper.Data.ReadJsonFile<ModData>($"data/{Constants.SaveFolderName}.json")
this.Helper.Data.WriteJsonFile($"data/{Constants.SaveFolderName}.json", data);
// Mods/ALOS/ModEntry.cs:429,450
File.WriteAllText(Path.Combine(this.Helper.DirectoryPath, "InviteCode.txt"), inviteCode);
File.WriteAllText(Path.Combine(this.Helper.DirectoryPath, "ConnectionsCount.txt"), ...);
```

即 `Mods/ALOS/data/<存档名>.json`、`Mods/ALOS/InviteCode.txt`、`Mods/ALOS/ConnectionsCount.txt`。

> **对 FPK 的硬要求**：MOD 目录**不只是代码**，还包含**每存档的运行状态**。
> 「MOD 删除 = 删一个代码目录」是错的。快照与 MOD 卸载必须把 `Mods/<MOD>/data/` 一起考虑，
> 并且删 MOD 前必须快照（本项目已实现）。**这是上游文档没有写、但会直接导致数据丢失的陷阱。**

---

## 5. 版本、升级与回滚能力审计

### 5.1 游戏文件获取方式（关键结论，已用镜像层证据确认）

| 问题 | 结论 | 证据 |
| :--- | :--- | :--- |
| 镜像启动时会下载游戏文件吗？ | ❌ **不会** | `startapp.sh` 全文 15 行，无任何 `curl`/`wget`/下载逻辑；镜像 config `Env` 中无下载相关变量 |
| 镜像启动时会覆盖游戏文件吗？ | ❌ **不会** | 唯一的 `chmod`/`chown` 在构建期（`history` 中 `RUN chmod -R 777 /content/...`、`RUN chown -R 1000:1000 ...`），运行期无写操作 |
| 游戏文件从哪里来？ | 构建期 `COPY content-game /content` | image `history`：`COPY content-game /content # buildkit` |
| 该构建上下文在哪？ | **不在本仓库**（`.gitignore` 忽略 `oneclick-script/content-game/`）；游戏内容来自 `Lixeer/ValleyContent` Release `G1.6.15` 的 `Content.zip`（434 MB，2026-06-04） | `.github/workflows/build-server.yml` 中 `curl -L .../ValleyContent/releases/download/G1.6.15/Content.zip`（同一数据源） |
| 能否固定旧版本游戏？ | ✅ **能**。镜像 tag 固定（`v26.5`）+ digest 固定即完全可复现 | manifest digest 已记录于 §1 |

**因此：** 「更新游戏」= 「换镜像」；「回滚游戏」= 「换回旧镜像」。
本项目**不使用会随时间变化的 `latest` 标签**：默认配置写入
`image_digest = sha256:d84f9241...`，并以 `image@sha256:...` 形式拉取。

### 5.2 ⚠️ 本审计发现的最大结构性问题：**上游发行源 只有一个 tag**

上游发行源 `lixeer/valley-server` 仅有 `v26.5`（2026-05-22 推送，此后未更新）。
这意味着：

1. **没有"上一个已验证版本"可以回滚到**（除了本地保留的旧镜像）。
2. 一旦上游删除或覆盖该 tag，本项目就失去可复现来源。

**FPK 的应对（已实现）：**
* 首次安装时记录并**持久化 image digest**；此后一律按 digest 拉取与运行。
* 版本档案（`versions` 表 + `archives/`）记录每次实际运行的 `digest + 游戏版本 + SMAPI 版本 + MOD 哈希`。
* 快照 manifest 中固定记录 digest，恢复时据此校验。
* **明确不宣称**「可以回滚到任意历史版本」——只能回滚到**本地已保留**的镜像或**同一 digest**。
  此限制在 UI 的「版本」页面显式展示（见 §9「未实现/未验证清单」）。

### 5.3 组件升级链

| 组件 | 当前版本 | 升级方式 | 风险 |
| :--- | :--- | :--- | :--- |
| 游戏本体 | **1.6.15**（`Stardew Valley.dll` 内 `1.6.15` 字符串） | 换镜像 | 高：镜像只有 1 个 tag，换 = 不可逆（除非本地留旧镜像） |
| SMAPI | **4.5.1**（`StardewModdingAPI.dll` 内 `4.5.1`，与自带 MOD 的 `MinimumApiVersion` 一致） | 换镜像（**不能独立升级**） | 高：同上 |
| 上游 5 个 MOD | ALOS 1.0.6 / ServerCMD 1.0.1 / ChatCommand 1.0.2 / CommandWebUI 1.0.1 / ChangeServerPort 1.0.0 | 下载 `v2.0.0` Release 的 `Mods.zip`（86,328 B，sha256 `1676fbcfe56e84f7aa5008fb1455a02747689860f6515481059926cb87d5628c`）后放入 `Mods/` | 中：需要停服 + 快照 |
| 第三方 MOD（SVE 等） | 无（需用户上传） | 用户上传 ZIP | 中：需依赖/兼容性检查 |
| 上游镜像 | `v26.5` | `拉取上游发行物` | 中 |

### 5.4 上游 CI 与发布

| workflow | 触发 | 作用 | 与本项目关系 |
| :--- | :--- | :--- | :--- |
| `build-server.yml` | push `**.cs`/`src/**` | 构建 3.x 的 Windows/Linux 自包含包 | 无关（3.x） |
| `build.yml` | push `Mods/**`/`manifest.json` | 用 `Pathoschild/SMAPI-ModBuildWorkflow` 构建全部 MOD，产物 `Mods.zip` + 单个 MOD zip，发布到 `refs/heads/stable` | **相关**：这是 `Mods.zip` 的来源 |
| `readme-render.yml` | push `README.md` | 把 README 渲染成 PNG 上传 artifact | 无关 |
| `release.yml` | `workflow_dispatch` | 自动递增 `v3.*` tag 并发 3.x Release | 无关（**不要误触发**） |

> ⚠️ **`release.yml` 会自动打 tag 并推送**。在本 Fork 中若保留该 workflow，误点会生成 `v3.x` tag。
> 本项目对 Fork 的 CI 做了隔离：新增的 FPK 构建 workflow 只构建 FPK，不改动上游 workflow。

---

## 6. 许可证与再分发要求

### 6.1 上游主许可证

* 仓库根目录 `LICENSE`：**Apache License 2.0**（GitHub API 确认 `spdx_id: Apache-2.0`）。
* **无 `NOTICE` 文件**（已核实：仓库内不存在 `NOTICE`）。
* Apache-2.0 要求：保留版权声明与许可证副本；对修改过的文件标注修改说明；不授予商标权。

### 6.2 逐组件许可证清单

| 组件 | 许可证 | 再分发要求 | 本项目是否随 FPK 打包 |
| :--- | :--- | :--- | :--- |
| ValleyServer 仓库（含 5 个 MOD 源码） | Apache-2.0 | 保留 LICENSE、声明修改 | 仅当用户选择时；默认**不打包** MOD 二进制 |
| `ALOS` | MIT + 附加协议（基于 perkmi/Always-On-Server-for-Multiplayer） | **署名原仓库地址**；商业使用须署名 | 默认不打包；用户自行获取或从 Release 下载 |
| `ServerCMD`/`ChatCommand`/`CommandWebUI`/`ChangeServerPort` | Apache-2.0 | 保留声明 | 同上 |
| 镜像 `lixeer/valley-server` | ⚠️ **镜像内未找到许可证声明**（已核实各层无 `LICENSE`/`COPYING`） | **版权与授权状态不明** | 🔴 **不打包、不再分发镜像**；运行时 `拉取上游发行物`，与 FPK 分开发行 |
| Stardew Valley 游戏本体（镜像内） | 🔴 **专有版权作品**（ConcernedApe） | **不得再分发** | 🔴 **绝不打包进 FPK**；仅由用户运行时拉取上游镜像 |
| SMAPI | MIT | 保留声明 | 同上（在镜像内） |
| SMAPI 自带 MOD（SaveBackup/ConsoleCommands） | MIT | 保留声明 | 同上（在镜像内） |
| `Pathoschild.Stardew.ModBuildConfig` 4.4.0 | MIT | | 构建期依赖 |
| `Lib.Harmony` 2.3.5 | MIT | | 随 MOD（若打包） |
| `Lidgren.Network` 1.0.2 | MIT | | 随 MOD（若打包） |
| `EmbedIO` 3.5.2 | MIT | | 随 `CommandWebUI` |
| `System.Text.Json` 9.0.2 | MIT | | 随 MOD |
| 游戏本体（`Lixeer/ValleyContent` Release） | 🔴 专有 | 不得再分发 | **绝不打包** |

### 6.3 本项目的再分发策略（写入 `docs/licensing.md`）

1. **FPK 内不含** 游戏本体、SMAPI、上游镜像、任何第三方 MOD 二进制。
2. FPK 内**只包含**本项目自有的管理器代码（Apache-2.0）与上游 LICENSE/声明副本。
3. 「一键安装上游 MOD」走**运行时从上游 Release 下载**（`v2.0.0` 的 `Mods.zip`），
   由用户点击确认，FPK 不缓存、不再分发。**记录下载 URL 与 SHA-256 供用户核对。**
4. 第三方 MOD（SVE 等）**只接受用户上传**，不做在线仓库爬取、不自动下载。
5. 保留上游 Apache-2.0 的 `LICENSE` 与修改说明；本项目修改过的上游文件（如果有）逐一标注。
6. 本项目**不修改**上游 MOD 的核心逻辑（本次交付未修改任何上游文件）。

---

## 7. 已知问题及其影响

| # | 问题 | 证据 | 影响 | FPK 应对 |
| :--- | :--- | :--- | :--- | :--- |
| P1 | **WebVNC (5800) 无认证**，可操作游戏 | `startapp.sh:12` 无任何认证 | 🔴 局域网内任何人可接管服务器 | 默认把 5800/5900/29103 绑定到 `127.0.0.1`，通过管理器的鉴权代理访问；提供显式开关允许对外开放并给出警告 |
| P2 | **VNC 密码硬编码 `041041`** | `startapp.sh:10` | 🔴 已知口令 | 管理器提供自己的 entrypoint 包装脚本，密码在安装时随机生成并写入管理器配置 |
| P3 | **`CommandWebUI` WebSocket 无认证**，能执行任意 SMAPI 命令 | `Mods/CommandWebUI/Server.cs:139-160` 的 `/ws` 无 token 校验；上游 README 自承「获取到 WebSocketIO 终结点，将会获取到控制台权限…存在渗透风险」 | 🔴 容器内命令执行 | 默认不映射 29103；若启用则仅绑 `127.0.0.1` 并经管理器代理鉴权；安装向导中把 `AccessToken` 改为随机值 |
| P4 | **镜像内 `SaveBackup` 被挂载覆盖，实际不生效** | compose 挂载 `./Mods` 覆盖镜像 `Mods/` | 🟠 用户以为有每日备份，实际没有 | 管理器自带自动化快照（不依赖上游） |
| P5 | **SMAPI 错误日志不在卷里，重建即丢** | compose 无 `ErrorLogs` 挂载 | 🟠 故障排查时历史日志丢失 | 管理器把容器 stdout/日志采集到宿主机数据目录 |
| P6 | **上游发行源 只有一个 tag** | `count=1` | 🟠 无法回滚到上游历史版本 | 固定 digest + 本地保留镜像 + 显式披露限制 |
| P7 | **镜像无 Healthcheck** | image config 无 `Healthcheck` | 🟠 `读取容器元数据` 的 health 状态不可用 | FPK 自建健康判据（见 §8.4） |
| P8 | **`ChatCommand.csproj` 未声明 `Force.DeepCloner`** | `ModEntry.cs` 使用但 csproj 无声明 | 🟡 上游从源码构建可能失败 | 不修改上游；本项目不构建该 MOD，只用上游 Release 产物；登记为已知问题 |
| P9 | **`ChangeServerPort` 源码默认 5885 vs 发布包 24642** | `ModConfig.cs:3` vs `Mods.zip` 内 `config.json` | 🟡 源码构建的 MOD 与发布包行为不同 | 管理器在安装后显式写入 24642 并校验与 compose UDP 映射一致 |
| P10 | **`ChangeServerPort` 的 manifest 字段名大小写错误 `EntryDLl`** | `Mods.zip` 内 `ChangeServerPort/manifest.json` | 🟡 严格解析器会误判 | MOD 解析器按 SMAPI 语义大小写不敏感 |
| P11 | **ALOS MOD 私有数据在 `Mods/<MOD>/data/`** | `ALOS/ModEntry.cs:140,429,450` | 🟠 卸载 MOD = 潜在丢每存档状态 | 删除 MOD 前强制快照并保留 `data/` 副本 |
| P12 | **镜像内未找到许可证文件** | 各层无 LICENSE/COPYING | 🟠 再分发授权不明 | 不再分发镜像，仅运行时拉取 |
| P13 | **`restart: unless-stopped` 无重试上限** | compose | 🟡 崩溃循环 | 管理器叠加自己的重试计数与退避，超限告警并停止自动拉起 |
| P14 | **`v2.0.0` 与镜像 `v26.5` 版本号体系脱节** | Release 名 `v2.0.0`（2026-05-29）vs 镜像 `v26.5`（2026-05-22） | 🟡 版本对应关系不直观 | 版本档案同时记录 Release 版本与镜像 digest |
| **P15** | 🔴 **以非 root 运行游戏时 SMAPI 直接崩溃**：SMAPI 启动时 `VerifyPath("$HOME/.config/StardewValley/ErrorLogs")` 会尝试 `mkdir`，若 `/root` 不可写就抛 `UnauthorizedAccessException` 并 `Abort`（退出码 134） | **本项目用真实镜像实测**：`Couldn't create a path: /root/.config/StardewValley/ErrorLogs` → `SMAPI failed to initialize: Access to the path ... is denied`；上游 `startapp.sh` 以 root 运行，因此从未暴露 | 🔴 若管理器按"降权运行"实现，容器会反复崩溃重启 | 管理器 entrypoint 在**仍是 root 时**预建 `$HOME/.config/StardewValley/ErrorLogs` 与 `Saves`、`chown` 给游戏用户、`chmod 0751 /root`，并把 `ErrorLogs` 挂载到宿主机持久化。修完后实测 SMAPI 已能正常初始化（见 §13） |
| **P16** | 🔴 **`runuser -u "#1000"` 在镜像内不可用**：`getent passwd 1000` 能查到 `ubuntu`，但数字形式会报 `user #1000 does not exist or the user entry does not contain all the required fields` | **本项目实测**（`运行容器` 内直接验证） | 🔴 任何用数字 UID 降权的实现都会启动失败 | entrypoint 先用 `getent passwd <uid>` / `awk /etc/passwd` 解析出**用户名**，再用 `runuser -u <用户名>` |
| P17 | **SMAPI 会吞掉未知启动参数**：本环境实测 `./StardewModdingAPI --help` 并不打印帮助，而是**直接把参数透传给游戏**（会真的去启动游戏）。因此无法用「探测参数」的方式确认某个 SMAPI 参数是否受支持 | 🟡 若 entrypoint 盲目加参数，可能引入难以定位的启动行为变化 | entrypoint **完全不依赖 SMAPI 启动参数**：用 `stdout/stderr` 重定向 + `tee -a` 同时写 `console.log` 与 `smapi-console.log`；并用 `runuser -m` / `dotnet -m` 保证 `$!` 拿到真实进程 PID，使 SIGTERM 能送达游戏进程 |
| **P18** | **宿主机 btrfs 元数据保留区会触发假 ENOSPC**：磁盘 `df` 显示 385 GB 可用，但容器内对 bind mount 的小文件写入仍可能报 `No space left on device` | 实测：`dd` 写到同一 bind mount 正常，而 SMAPI 追加日志时报 ENOSPC；`dmesg` 无 EDQUOT 记录 | 🟡 可能让"首次启动"在特定 NAS 上失败且原因难以理解 | 管理器在「系统」页展示分类磁盘占用与可用值；启动失败时把容器 exit code 与日志尾部一并呈现；已在安装文档中记录该现象与排查方向 |

---

## 8. 最小可运行部署方案

### 8.1 宿主机前置条件（已在目标机核实）

| 依赖 | 目标机状态 | FPK 检测方式 |
| :--- | :--- | :--- |
| 容器运行时 | 上游镜像内的运行时 | 仅审计记录，本项目不依赖 |
| 编排工具 | 上游镜像的编排版本 | 仅审计记录 |
| Python 3.10+ | ✅ fnOS `python312` 应用提供 3.12.4；系统另有 3.11.2 | `install_dep_apps = python312`；启动时探测并回落 |
| 磁盘空间 | ✅ `/vol1` 390 GB 可用 | `os.statvfs` |
| 架构 | x86_64 | `manifest.platform = x86` |

### 8.2 目录布局（FPK 生命周期与游戏数据完全分离）

```
/vol1/@appcenter/valleyserver/          $TRIM_APPDEST   代码；【升级时整体替换】
├── server/                           管理器 Python 源码
├── www/                              前端静态资源

└── valleyserver.sock                 网关 Unix socket

/vol1/@appdata/valleyserver/            $TRIM_PKGVAR    运行数据；【升级保留】
└── logs/                             管理器与容器日志

/vol1/@apphome/valleyserver/            $TRIM_PKGHOME   【升级/卸载默认保留】★游戏数据在这里
├── manager/
│   ├── config.json                    管理器配置（权限 600）
│   └── tasks.db                       任务/审计/快照索引（SQLite）
└── data/
    ├── game/                          compose 工作目录 + entrypoint 包装
    │   ├── 上游编排文件        由管理器生成（严格模板，无用户 YAML）
    │   └── entrypoint.sh              管理器自有的启动包装（替换上游 startapp.sh）
    ├── Saves/                         ★存档（挂到容器 /root/.config/StardewValley/Saves）
    ├── Mods/                          ★MOD（挂到容器 /content/Stardew Valley/Mods）
    ├── snapshots/                     ★快照仓库（每个快照一个目录 + manifest.json）
    ├── archives/                      版本档案
    ├── staging/                       更新暂存区
    ├── restore-tmp/                   恢复临时区
    └── branding/                      VNC 密码等运行期凭证
```

### 8.3 数据生命周期矩阵（升级/卸载时会怎样）

| 数据 | 位置 | App 升级 | App 卸载（默认） | 卸载并删除全部数据 |
| :--- | :--- | :--- | :--- | :--- |
| 管理器代码 | `$TRIM_APPDEST` | 🔄 替换 | 🗑 删除 | 🗑 删除 |
| 管理器配置/任务库 | `$TRIM_PKGHOME/manager` | ✅ 保留 | ✅ 保留 | 🗑 删除 |
| **存档 Saves** | `$TRIM_PKGHOME/data/Saves` | ✅ 保留 | ✅ **保留** | 🗑 删除（二次确认） |
| **MOD** | `$TRIM_PKGHOME/data/Mods` | ✅ 保留 | ✅ **保留** | 🗑 删除（二次确认） |
| 快照 | `$TRIM_PKGHOME/data/snapshots` | ✅ 保留 | ✅ **保留** | 🗑 删除（二次确认） |
| 版本档案 | `$TRIM_PKGHOME/data/archives` | ✅ 保留 | ✅ 保留 | 🗑 删除 |
| 运行日志 | `$TRIM_PKGVAR/logs` | ✅ 保留 | ✅ 保留 | 🗑 删除 |
| 运行进程 | 本机进程 | ♻️ 不动 | ⏹ 停止 | 🗑 停止并清理 |

### 8.4 健康判据（替换上游缺失的 Healthcheck）

上游镜像无 Healthcheck，且 SMAPI 输出默认只进 `容器日志`。FPK 的判据分三层：

```text
L1 容器层：读取容器元数据 -f '{{.State.Running}}' == true
L2 端口层：容器 VNC(5900) 与 WebVNC(5800) 在内网可达（nc -z / socket connect）
L3 游戏层：管理器提供的 entrypoint 把 SMAPI 控制台输出同时写入
          $DATA/game/console.log 与 stdout；
          L3 通过 = 该日志出现 SMAPI 加载完成标志（"SMAPI 4.5.1" + "Mods loaded"）
          且在 start_period(240s) 内未出现致命异常
```

全部通过才判定「运行中」；否则为「启动中」。**不使用**「进程还在」作为成功判据。

### 8.5 与上游 compose 的差异（本项目为什么必须改）

| 项 | 上游 | FPK 管理器 | 原因 |
| :--- | :--- | :--- | :--- |
| `container_name` | `stardew_server`（全局名） | `valleyserver-game`（项目专属） | 避免与其他应用冲突 |
| 项目名 | 由目录推断 | `-p valleyserver` | 项目专属网络/卷/标签 |
| VNC 密码 | 硬编码 `041041` | 安装时随机生成，注入 entrypoint | P2 |
| 端口绑定 | `0.0.0.0` 全开 | 默认 `127.0.0.1`，显式开关才对外 | P1/P3 |
| 镜像引用 | `:v26.5` tag | `@sha256:d84f9241...` digest | §5.2 可复现 |
| Healthcheck | 无 | 管理器侧三层判据 | P7 |
| 日志 | 仅 `容器日志` | 采集到宿主机 | P5 |
| 挂载路径 | 相对 `./Mods`、`./Saves` | 绝对路径（数据目录白名单校验） | 防止路径注入 |
| `restart` | `unless-stopped` | 由管理器监管重试计数 | P13 |

---

## 9. 尚未确认的接口与行为（「待验证」清单）

以下条目**未经过实际运行验证**，本项目**不会把它们当作已实现能力对外宣称**：

| # | 待验证项 | 为什么无法确认 | 计划验证方式 |
| :--- | :--- | :--- | :--- |
| V1 | **游戏本体在 1280x720 无头 Xvfb 下能否长期稳定运行**（含节日、过夜、跨季） | 需要的运行时间远超单次任务；本环境还遇到 btrfs 假 ENOSPC（P18），无法完成完整启动 | 目标机长时间运行 + 日志采集；**已实测到 SMAPI 正常初始化阶段**（见 §13） |
| V2 | **`ALOS` 对 SVE 等大型内容 MOD 的兼容性** | 需要 SVE 授权副本与真实存档 | 用户提供 MOD 后实测（项目要求 §9.16） |
| V3 | **容器内 `Mods/` 挂载为空目录时 SMAPI 是否报错** | 未实跑；镜像内该目录非空 | 部署后查 `console.log` |
| V4 | **`CommandWebUI` 在 29103 上的实际启动时机**（`Mods/` 空时该 MOD 不存在，端口不会监听） | 依赖用户是否安装该 MOD | 安装该 MOD 后实测 |
| V5 | **`AdminPassword`/`AccessToken` 是否有长度或字符限制** | 源码未做校验 | 代码审查已覆盖（任意字符串），运行时确认 |
| V6 | **`ChangeServerPort` 改写端口后，Lidgren 是否真的在新端口监听** | 需要真实游戏运行 | 安装后 `ss -lunp` 核实 |
| V7 | **镜像 `chown 1000:1000` 对应的用户名** | 层内 `/etc/passwd` 未逐行确认；仅知 uid 1000 | 启动容器后 `id 1000` |
| V8 | **存档目录在 uid 1000 下的写权限** | ✅ **已实测**：entrypoint 内 `chown -R 1000:1000` 后，`ubuntu` 用户可正常读写挂载目录 | — |
| V13 | **游戏在无存档 / 存档结构异常时的行为** | 本环境未完成完整启动（P18） | 目标机实测；管理器侧已在存档页做结构校验并给出可读性检查 |
| V9 | **上游镜像是否会在未来被删除或覆盖 tag** | 无法预测 | 本项目固定 digest 缓解 |
| V10 | **游戏中"玩家在线列表"能否可靠获取** | 上游无 HTTP 接口；只能解析日志或协议，均不稳定 | **首版明确标注「暂不支持」**，不伪造数据 |
| V11 | **fnOS 系统版本与 `os_min_version` 的实际下限** | 未做多版本测试 | 目标机单一版本实测 |
| V12 | **`X-Trim-Isadmin` 等网关注入 Header 在所有访问路径（含 FnConnect 远程）都可靠** | 需远程访问实测 | 目标机联网实测 |

---

## 10. 上游可直接复用的能力

| 能力 | 来源 | 复用方式 |
| :--- | :--- | :--- |
| 游戏运行时（游戏 + SMAPI + Xvfb + VNC） | 镜像 `lixeer/valley-server:v26.5` | 直接运行，按 digest 固定 |
| 无人值守自动化逻辑 | `ALOS` MOD | 直接安装，不修改源码 |
| 无头控制指令 | `ServerCMD` MOD | 直接安装；FPK 通过 SMAPI 控制台调用 |
| 游戏内命令执行 | `ChatCommand` MOD | 直接安装 |
| 浏览器 SMAPI 控制台 | `CommandWebUI` MOD | 选择性安装（默认关闭外网暴露） |
| 端口改写 | `ChangeServerPort` MOD | 直接安装；FPK 校验其配置与 compose 一致 |
| 编排参考 | `oneclick-script/` 下的上游编排文件 | 仅作审计参考 |
| MOD 构建流水线 | `.github/workflows/build.yml` | 仅作为用户获取 MOD 的途径；不修改 |
| 许可证文本 | `LICENSE`（Apache-2.0） | 复制进 FPK 的 `docs/` |

## 11. 必须由 FPK 新增的能力

| # | 能力 | 为什么上游没有 |
| :--- | :--- | :--- |
| 1 | 原生 Web 管理界面（飞牛应用中心入口） | 上游只有 VNC 画面与 SMAPI 控制台 |
| 2 | 服务器生命周期编排（装/启/停/重启/重建） | 上游靠手工 `容器编排` |
| 3 | **带一致性保证的快照系统**（停服→快照→校验→原子提交） | 上游 `SaveBackup` 既被挂载覆盖，也不做 MOD/配置 |
| 4 | 存档导入/导出/切换/恢复 + ZIP 安全校验 | 上游无任何存档管理 |
| 5 | MOD 管理（manifest 解析、依赖检查、启用/禁用、校验值） | 上游靠手工放文件 |
| 6 | 统一任务队列 + 状态机 + 回滚语义 | 上游无任务概念 |
| 7 | 版本档案 + digest 固定 + 更新检查 + 回滚 | 上游无版本管理 |
| 8 | 三层健康判据 + 异常退出自动恢复（重试上限/退避/告警） | 上游仅 `restart: unless-stopped` |
| 9 | 自建 entrypoint（随机 VNC 密码、日志采集、控制台落盘） | 上游密码硬编码、日志不落盘 |
| 10 | 安全加固（路径校验、ZIP 防穿越、端口默认关闭外网、接口鉴权） | 上游为内网单机设计 |
| 11 | 诊断包（脱敏导出） | 上游无 |
| 12 | 磁盘空间守卫（不足时拒绝高风险变更） | 上游无 |

---

## 12. 审计方法说明（可复现）

本次审计**不依赖 README 描述**，所有关键结论均有下列之一作为依据：

1. **源码逐文件阅读**：76 个受控文件全部读过，引用均带文件路径与行号。
2. **Git 元数据**：`git log`/`git tag`/`git merge-base` 核实分支与标签关系。
3. **GitHub API**：Release 列表、资产大小与下载量、许可证 SPDX、仓库元数据。
4. **镜像仓库 API**：直接读取 manifest 与 config blob，得到 digest、层清单、
   构建历史（`history`）、`Healthcheck`/`Env`/`Entrypoint` 是否存在。
5. **镜像层直接提取**（最硬的证据）：把 10 个层从 registry 下载到本地，
   解包最终层后核实游戏文件、`Mods/` 内容、`Content/` 文件数与体积、
   `Stardew Valley.dll`/`StardewModdingAPI.dll` 的版本字符串。
   *（本机 `拉取上游发行物` 卡在 layer 解包阶段，故改用 registry 直取，结论等价。）*
6. **发布产物核实**：下载 `v2.0.0` 的 `Mods.zip`（86,328 B），解包核对 5 个 MOD 的
   manifest 与 `config.json`，得到 `sha256:1676fbcfe56e84f7aa5008fb1455a02747689860f6515481059926cb87d5628c`。
7. **目标机环境核实**：python312 3.12.4 / `/vol1` 390 GB 可用。

**未做的事（不冒充）：**
* 未实际运行游戏容器并观察游戏画面（本环境 `拉取上游发行物` 解包阶段阻塞、且有 btrfs 假 ENOSPC，见 §14）。
* 未安装 SVE 或任何第三方内容 MOD。
* 未做长时间（跨天/跨季）运行测试。

---

## 13. 真机运行时验证（把镜像真的跑起来）

审计初期本机 `拉取上游发行物` 在解包阶段阻塞，因此先把 10 个层从 registry 直接下载并**组装成
可被 `载入镜像` 接受的镜像 tar**，成功载入本地：

```bash
# 关键校验：载入后的 config digest 与 registry 完全一致
读取镜像元数据 lixeer/valley-server:v26.5 --format '{{.Id}}'
# sha256:bf38c16d993f626af9367679d27e9997038f20525e910b6b9a99db4ced30cb79
# registry 上的 config digest 同样是 bf38c16d…
```

这证明**组装出的镜像与上游镜像逐字节等价**，随后用它跑了真实的容器。

### 13.1 版本信息：容器内实测（不再是"从二进制里 grep"）

```text
[SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356 on Unix 6.18.18.1107
```

**这是 SMAPI 自己报出来的**，与前面从 DLL 字符串推断的结论一致，因此
审计报告 §5.3 的「游戏 1.6.15 / SMAPI 4.5.1」升级为**确定性事实**。

### 13.2 发现并修复的两个致命问题

用真实镜像跑容器暴露了两个**只看文档绝对发现不了**的问题（详见 P15、P16）：

1. **以非 root 运行游戏 → SMAPI 立刻崩溃**
   （`Couldn't create a path: /root/.config/StardewValley/ErrorLogs` →
   `SMAPI failed to initialize: … is denied` → `Aborted (core dumped)`，退出码 134）。
   上游之所以没暴露，是因为它的 `startapp.sh` 以 root 运行游戏。
2. **`runuser -u "#1000"` 在镜像内不可用**，必须先用 `getent passwd 1000` 解析出
   `ubuntu` 再传用户名。

两个问题都已在管理器的 entrypoint 生成模板里修掉。修复后重跑，容器日志显示：

```text
[entrypoint] ValleyServer 启动中：2026-10-11T20:09:48+08:00，VNC/WebVNC 已就绪（5800）
[entrypoint] 以用户 ubuntu（uid 1000）运行游戏
[SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356 on Unix 6.18.18.1107
```

即 SMAPI **已经初始化成功**，不再因权限/用户解析而崩溃。

### 13.3 未能完成的部分（如实记录）

继续启动时，SMAPI 写 `ErrorLogs/SMAPI-latest.txt` 报
`IOException: No space left on device`（P18）。同一 bind mount 用 `dd` 写 2 MiB 正常，
宿主 `df` 显示 385 GB 可用，`dmesg` 无 `EDQUOT` 记录 —— 属于该 NAS 上
**btrfs 元数据保留区**导致的假 ENOSPC，与本项目的实现无关。

因此以下内容**仍未验证**，本项目不声称已验证：

* 游戏本体在无头 Xvfb 下的完整启动与运行；
* VNC 画面连通性；
* 存档被游戏实际读取/写入；
* 玩家联机与无人值守自动化（节日、过夜、弹窗恢复）。

这些属于 `docs/test-report.md` 中明确列出的「未验证项」，需要在
**磁盘空间充足的飞牛 OS 目标机**上继续验证。

## 14. 审计期间的异常记录

| 现象 | 说明 | 影响 |
| :--- | :--- | :--- |
| `拉取上游发行物 lixeer/valley-server:v26.5` 阻塞 | 层下载完成后，解包阶段进程阻塞在 futex 且 I/O 计数不再增长；并发终止后重试仍在同一处阻塞 | **不影响审计结论**：改用 registry API 直接下载 10 个层（3.3 GB 全部校验通过）后解包核对，得到等价且更细的证据 |
| 上游发行源 仅有 1 个 tag | `page_size=100` 查询 `count=1` | 见 P6 |

---

## 15. 审计结论

1. **2.x 路线可以继续使用，且适合做管理对象**：游戏本体与 SMAPI 完全烘焙在镜像里，
   运行时不下载、不覆盖，**镜像 digest 就是最可靠的版本锚点**。这使「版本固定」与
   「游戏本体回滚」在技术上可行。
2. **但上游 2.x 没有任何管理面**：没有 API、没有健康检查、没有存档/MOD 管理、
   没有快照。所有这些必须由 FPK 从零构建。
3. **最大风险不是技术不可行，而是安全暴露面**：WebVNC 无认证、VNC 固定密码、
   `CommandWebUI` WebSocket 无认证。FPK 默认关闭外网暴露并随机化口令。
4. **数据安全的关键洞见**：MOD 目录里藏着每存档运行状态（`ALOS/data/*.json`），
   且镜像自带的每日备份**实际不生效**。FPK 必须自己保证「快照先于变更」。
5. **许可证方面**：游戏本体与镜像不得再分发。FPK 只含本项目自有代码；
   上游 MOD 走用户确认的运行时下载；第三方 MOD 只接受用户上传。

> **本报告不含任何推测性结论。** 所有「待验证」项已在 §9 显式列出，
> 在获得实际运行证据前不会在 UI 或文档中标注为「已实现/已通过」。

---

## 16. 补充审计：原生部署的可行性

上游 2.x 的文档只提供容器方式部署（`oneclick-script/run.sh` + 编排文件），
但没有说明容器是否为技术必需。补充审计的结论：**不是必需**。

### 16.1 实测事实

| # | 事实 | 依据 |
| :--- | :--- | :--- |
| 1 | 游戏与 SMAPI 是 **.NET 6 自包含**发布 | `StardewModdingAPI.runtimeconfig.json` / `Stardew Valley.runtimeconfig.json` 用 `includedFrameworks`；目录内自带 `libcoreclr.so`/`libhostfxr.so`；`ldd StardewModdingAPI` 无 dotnet 依赖 |
| 2 | **不需要安装 .NET 运行时** | 同上 |
| 3 | 唯一硬依赖是**图形环境** | 对照实验：无 X 显示器 → `NoSuitableGraphicsDeviceException: Failed to create graphics device!`；有显示器 → `[SMAPI] Mods loaded and ready!` |
| 4 | 所需显示组件在飞牛 OS（Debian 12）仓库中可得 | `apt-cache policy xvfb x11vnc websockify openbox novnc` 均有 candidate |
| 5 | 基础图形库系统已自带 | `libGL.so.1`、`libSDL2-2.0.so.0`、`libX11.so.6`、`libopenal.so.1`、`libgbm.so.1` 均 present |
| 6 | 无需 GPU | `LIBGL_ALWAYS_SOFTWARE=1` 下 SMAPI 正常加载 |
| 7 | 原生与容器内**版本一致** | 两者都报 `SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356` |

### 16.2 原生部署实测（完整栈）

在飞牛 OS 上、**不安装任何系统包**（仅用应用目录内捆绑的 `apt-get download` + `dpkg -x` 产物）：

```
phase=运行中  procs={'xvfb': True, 'openbox': True, 'x11vnc': True, 'websockify': True, 'game': True}
```

* `Xvfb`：`xdpyinfo` 报 `dimensions: 1280x720 ... depth of root window: 24 planes`
* `x11vnc`：VNC 握手返回 `RFB 003.008`
* `websockify` + noVNC：`curl /vnc.html` → HTTP 200
* 游戏：`[SMAPI] Mods loaded and ready!`，`VmRSS ≈ 200 MiB`

### 16.3 原生部署引入的新的工程约束（已处理）

| 约束 | 症状 | 处理 |
| :--- | :--- | :--- |
| 必须有网络获取显示组件 | — | 捆绑到应用目录；也支持「系统已装」来源；失败给出明确指引 |
| `websockify` 需要 `PYTHONPATH` | `PackageNotFoundError: No package metadata was found for websockify` | `xstack.env_for_tools()` 注入 `PYTHONPATH` |
| 窗口管理器需要 `XDG_DATA_DIRS` | `openbox` 报 `Unable to load the theme 'Clearlooks'` 并**直接退出** → 游戏在跑但没有窗口管理器 | 注入 `XDG_DATA_DIRS` |
| daemon 化进程的 PID 不可信 | `x11vnc -bg` / `openbox` 的 PID 文件很快失效，健康判据误报"没在跑" | `resolve_pid()` 三级归属判定（PID 文件 → `/proc/pid/exe` → cwd/cmdline） |
| 子进程缺 `DISPLAY` | `openbox` 报 `Failed to open the display from the DISPLAY environment variable.` | 在统一的 `_env()` 里注入 `DISPLAY`/`HOME` |
| MOD 目录与快照的一致性 | ALOS 的每存档状态写在 `Mods/<MOD>/data/`；若 MOD 留在游戏目录则**不进快照** | 把游戏目录内 `Mods` 符号链接到 `DATA_ROOT/Mods` |
| 存档位置 | 游戏读 `$HOME/.config/StardewValley/Saves` | 固定专用 `HOME`，并把该路径符号链接到 `DATA_ROOT/Saves` |
| 端口唯一性校验口径 | 早期把两套配置混在一起比较 → 配置**永远无法应用** | 只校验当前生效的这一套 |

### 16.4 与上游容器方案的能力差异（必须如实告知用户）

| 能力 | 原生 | 上游容器方案 |
| :--- | :--- | :--- |
| 运行游戏 + SMAPI + MOD | ✅ | ✅ |
| 三层健康判据 / 任务队列 / 快照 / MOD / 存档管理 | ✅ | ✅ |
| 按**镜像摘要**固定版本 | ❌ 无镜像概念 | ✅ |
| 版本回滚 | ⚠️ 只能通过**快照**回到历史时点 | ✅ 镜像摘要回滚 |
| 依赖容器运行时 | ❌ 不需要 | ✅ 需要 |

因此「版本」页会明确说明差异，而不是把不具备的能力包装成"支持回滚"。

### 16.5 仍未验证

原生模式的**多人实际联机、ALOS 完整无人值守行为、SVE 加载、NAS 重启真实恢复、
长时间稳定性**均未验证（见 test-report.md §5）。已获得运行证据的止于：
SMAPI 加载完成、五个进程受管、启动/停止/快照/配置应用/API 鉴权。

