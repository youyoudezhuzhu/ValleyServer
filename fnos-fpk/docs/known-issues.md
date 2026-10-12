# 已知问题与待办（issue log）

> 记录人：Development Lead · 最近更新：2026-10-12
> 状态约定：`待修` / `修复中` / `已修（未发布）` / `已修（已发布）` / `不修（说明原因）`

本文档记录**已确认存在、但尚未（或尚未完整）处理**的问题。每条都包含：现象、根因、
实证方式、影响面、计划。凡未经实证的猜测一律标注为「未验证」。

---

## 状态总览

| 编号 | 问题 | 严重度 | 状态 |
| :--- | :--- | :--- | :--- |
| I-1 | MOD 包下载 / MOD 诊断接口被通配路由遮蔽 | **高**（功能不可用） | **已修** |
| I-2 | WebVNC 链接硬编码 `127.0.0.1`，远程（fnconnect）打不开 | **高**（功能不可用） | **已修**（改为管理器反代） |
| I-3 | 当前游戏本体的来源与合规性 | **高**（合规） | 待用户决策 |
| I-4 | `_host_hint()` 兜底返回占位符 `<NAS-IP>` | 低 | 待修 |
| I-5 | `x11vnc.pid` 因 `-bg` 守护化而陈旧 | 低 | 待修 |
| I-6 | `native.install-smapi` 覆盖运行中的可执行文件（ETXTBSY） | **高**（功能不可用） | 已修（未发布） |
| I-7 | 「在线玩家」纯占位卡片（只有"暂不支持"） | 低（无用 UI） | 已修 |
| I-8 | `confirm_destructive` / `metrics_interval_seconds` 是死设置 | 中（开关无效果） | 已修 |
| I-9 | Steam 扫码取游戏本体的长任务：取消无效、二维码拿不到 | **高**（新功能不可用） | 已修 |
| I-10 | 游戏用 CPU 软件渲染，吃满约 3 个核（本机有核显却没用上） | **高**（性能） | **已修**（新增核显后端，实测 290% → 114%） |
| I-11 | Steam 扫码后管理器不刷新 / 扫到过期码 | **高**（新功能不可用） | **已修** |
| I-12 | 458 MB 下载**全部成功**却被判"没找到游戏目录" | **高**（白下载一遍） | **已修** |
| I-13 | 登录明明成功却报 `cached_login: False`，每次都要重扫码 | 中 | **已修** |
| I-14 | 用干净的游戏目录整合 SMAPI 后**启动器起不来** | **高**（致命） | **已修** |
| I-15 | WebVNC 打开变成"下载文件"（Content-type 被换成 octet-stream） | **高**（功能不可用） | **已修** |
| I-16 | Steam depot 的 `steam_appid.txt` 是 480（Spacewar），导致 Steam 联机/成就失效 | 中 | **已修** |
| I-17 | 扫码登录与下载揉成一个动作 → "扫完码界面像卡住" | **高**（体验/可诊断性） | **已修**（拆成两个独立动作） |
| I-18 | 登录凭据的存放位置与检测位置错位（能免密登录却报未登录） | 中 | **已修**（含旧凭据迁移） |
| I-19 | 「已加载 MOD 0」：SMAPI 4.x 的 MOD 列表没被解析 | 高（显示错误） | **已修** |
| I-20 | 服务在跑但**不开服**：ALOS 重启后不会自动载档 | **高**（无人值守不成立） | **已修**（启动后自动载档） |
| I-21 | PC 端 Edge 仍把 WebVNC 当文件下载（缺少缓存头） | 高 | **已修** |
| I-22 | 配置页与系统页有完全相同的按钮组、两页都有日志查看器 | 中（重复内容） | **已修** |
| I-23 | 指令通道：stdin 实测不通；CommandWebUI 的 WS 可用但有上游崩溃缺陷 | **高** | **已修**（改用 WS + 排空 + 防崩溃循环） |
| I-24 | 游戏端口是 **UDP**，用 TCP 检测把"已开服"判成"未就绪" | 中 | **已修** |
| I-25 | 历史崩溃文本让 `L3_smapi_loaded` 永远为 False | 中 | **已修** |
| I-26 | 僵尸子进程长期残留（从不 wait/poll） | 低 | **已修** |
| I-27 | 默认显示后端是 `xvfb`，有核显也不默认启用 | **高**（用户视角：GPU 占用 0） | **已修**（默认改 `auto`） |
| I-28 | 切换显示后端时旧后端进程不停，导致切换必然失败 | **高** | **已修**（`_all_specs` + 残留清理） |
| I-29 | 三个动作被串成自动接力（登录→下载→启动） | **高**（违反用户明确要求） | **已修**（三步各自手动） |
| I-30 | 自动载档开服只挂在 `server.start` 上，其它启动路径全漏 | **高**（重启后无人值守失效） | **已修**（抽成共享实现并全覆盖） |
| I-31 | 设置页表述啰嗦（含实现细节与实测数字） | 中（可读性） | **已修**（逐项精简） |
| I-32 | 单测会碰到真实实例的端口 | **高**（测试污染生产） | **已修**（沙箱端口隔离） |

---

## I-1 MOD 包下载与 MOD 诊断接口被通配路由遮蔽

**现象**
用户在「MOD」页点「下载并安装上游 MOD 包」时，收到：

```
只允许从上游仓库下载 MOD 包（https://github.com/Lixeer/ValleyServer/releases/download/…）
```

而把这条提示里的地址手工打开是**能正常下载**的（HTTP 200），所以提示本身在误导用户。

**根因**
`api_handlers.py` 里的路由注册顺序有问题：

```python
r.add("GET",  r"/api/mods", mods_list)
r.add("GET",  r"/api/mods/(?P<dir_name>[^/]+)", mod_detail)      # ← 通配，先注册
...
r.add("GET",  r"/api/mods/upstream-bundle", mod_bundle_info)     # ← 被上一行吃掉
r.add("POST", r"/api/mods/upstream-bundle", mod_bundle_download)
r.add("GET",  r"/api/mods/smapi-log", mods_smapi_report)         # ← 同样被吃掉
```

路由器按注册顺序匹配，`GET /api/mods/upstream-bundle` 命中通配路由，
`dir_name="upstream-bundle"`，于是前端拿到的是一个**MOD 对象**而不是下载信息：

```js
const info = await api('mods/upstream-bundle');   // info.url === undefined
... api('mods/upstream-bundle', { json: { url: info.url } })   // 提交 undefined
```

后端校验 `url.startswith(allowed_prefix)` 失败 → 抛出那句"只允许从上游仓库下载"。

**实证**（对用户实际安装的 0.2.5 调用）

```
GET /api/mods/smapi-log        → {"dir_name":"smapi-log","manifest":{"problems":["manifest.json 不存在"]},...}
GET /api/mods/upstream-bundle  → {"dir_name":"upstream-bundle",...}   ← 应为下载信息
GET /api/mods                  → 正常，2 个 MOD（ConsoleCommands、SaveBackup）
```

**影响面**
* 上游 MOD 包一键安装：**完全不可用**；
* MOD 诊断（读 SMAPI 日志报告）：**完全不可用**；
* MOD 列表 / 单个 MOD 详情 / 安装 / 卸载 / 启停：正常。

**计划**
1. 把**具体路径**注册在**通配路径之前**（并加一条测试，断言顺序不被破坏）；
2. 让通配路由显式排除保留名（`upstream-bundle`、`smapi-log`、`install-plan`、`install`、`remove`、`toggle`），双保险；
3. 前端在 `info.url` 缺失时直接报"接口异常"而不是把一个 undefined 提交给后端 —— 避免再次出现误导性的错误文案。

---

## I-2 WebVNC 链接硬编码 `127.0.0.1`，远程访问打不开

**现象**
用户从 fnconnect（远程）打开管理界面，点「在新窗口打开 WebVNC」，
打开的地址是 `http://127.0.0.1:5800/vnc.html` —— 那是**用户自己电脑**的 127.0.0.1，必然打不开。

**根因**
前端「总览」页直接把主机名写死：

```js
const host = d.binding === '127.0.0.1' ? '127.0.0.1' : location.hostname;
const url = `http://${host}:${port}/vnc.html`;
```

用户按推荐把 `host_bind` 设为 `127.0.0.1`（更安全），于是 `host` 恒为 `127.0.0.1`。
即便改成 `location.hostname` 也不够：5800 端口只监听 `127.0.0.1`，远程本来就**连不上**。

**影响面**
远程（fnconnect / 局域网其它设备）看游戏画面：**不可用**。
只有在这台 NAS 本机浏览器里打开 127.0.0.1:5800 才行。

**计划（按推荐度排序）**
1. **在管理器自身 HTTP 服务下反代 noVNC 与 VNC WebSocket**
   （例如 `/app/valleyserver/vnc/` → `127.0.0.1:5800`，
   `/app/valleyserver/vnc/websockify` → `127.0.0.1:5900` 的 WebSocket）。
   这样"能打开管理界面的地方就能看画面"，无需额外暴露端口，也不受 fnconnect 限制；
   需要给 WebSocket 加**与会话一致的鉴权**（否则变成新的未授权入口）。
2. 退一步：把 `host_bind` 设为 `0.0.0.0` 并用 NAS 实际可访问地址拼链接 ——
   但这会把**无认证的 noVNC**暴露到局域网，不推荐作为默认。
3. 无论如何都要修掉"把 `127.0.0.1` 当作可访问地址展示给远程用户"这件事，
   至少在界面上说明该链接仅本机可用。

**注意**：方案 1 涉及在管理器里实现 WebSocket 代理与鉴权，属于**新功能**，
需要连同安全评审一起做（未授权 WebSocket = 任何人都能看/操作游戏画面）。

---

## I-3 当前游戏本体的来源与合规性（需用户决策）

**事实（如实记录）**

当前 `/vol1/@apphome/valleyserver/data/gamefiles/Stardew Valley/` 里的 Linux 版游戏文件，
**不是用户上传的**，而是：

1. 2026-10-11 我做原生部署验证时，从上游发行镜像 `lixeer/valley-server:v26.5`
   （digest `sha256:d84f924108b0fed5c459f5276a56bbb47fe450c0de2db807a55990adfd58e1b5`）
   通过 registry API 下载全部 10 个层，解包取出 `/content/Stardew Valley/`，
   落在 `/vol1/tmp/vsimg/layer09/content/Stardew Valley/`；
2. 2026-10-12 用户选择了「直接用本机那份 Linux 版」，我把它复制进 `data/gamefiles/`，
   并把用户上传的 **Windows 版**移到
   `data/restore-tmp/win-gamefiles-20261012-102002/` 保留。

**合规提示（必须让用户知情）**

* 《星露谷物语》游戏本体是 ConcernedApe 的**专有**作品；本项目从不打包、不再分发；
* 上述来源是**第三方上游镜像**，本项目**未核实**上游是否有权那样分发游戏本体；
* 用户自己那份 Windows 版才是其正当购买的副本，只是平台不匹配；
* 整个过程**只在本机内复制**，没有对外分发任何文件；
* 建议：若要合规，改用用户自己的 **Linux 版**游戏副本替换（管理器现已会在安装前校验平台）。

**待用户决策**：保留现状 / 换成自己的 Linux 版。

---

## I-4 `_host_hint()` 兜底返回占位符

```python
def _host_hint() -> str:
    ...
    except OSError:
        return "<NAS-IP>"        # ← 不是可点击地址
```

`webvnc_url` 会用这个值拼出一个形如 `http://<NAS-IP>:5800/vnc.html` 的假链接。
**计划**：兜底返回空串，前端据此显示"无法自动判断访问地址"而不是给一个点不开的链接。
（与 I-2 一起处理更自然。）

---

## I-5 `x11vnc.pid` 陈旧

`x11vnc -bg` 会 fork 到后台，`$!` 拿到的是父进程 PID，父进程随即退出，
因此 PID 文件里的 PID 很快变成 dead。

**影响**：健康判据靠 `resolve_pid()` 的二级回退（按 `/proc/<pid>/exe` 与 cmdline 归属）
纠正，实测 `readiness` 里 `x11vnc: True` 是对的，**不影响功能**；
但 PID 文件本身误导排查（`cat x11vnc.pid` → 进程不存在）。

**计划**：写 PID 文件时改为记录**真实子进程**（不用 `-bg`，或启动后回读实际监听 5900 的 PID），
或干脆在 PID 文件里存 `-bg` 模式下的"逻辑名"并让停止逻辑始终走 `resolve_pid()`（现状）。

---

## I-6 `native.install-smapi` 覆盖运行中的可执行文件（ETXTBSY）— 已修，未发布

**现象**（真机复现）

在游戏**正在运行**时点「获取 / 更新 SMAPI」：

```
OSError: [Errno 26] Text file busy:
'/vol1/@apphome/valleyserver/data/gamefiles/Stardew Valley/StardewModdingAPI'
```

**根因**：Linux 不允许写正在执行的可执行文件。`smapi.integrate()` 直接用
`shutil.copy2` 覆盖目标，撞上 `ETXTBSY`。

**已做的修复**
1. 新增 `_atomic_copy()`：先写同目录临时文件，再 `os.replace()` 原子替换
   （运行中的进程仍持有旧 inode，因此不受 ETXTBSY 限制）；
2. `task_native_install_smapi` **改为先停服再替换**，整合完成后把原本运行中的游戏拉起来；
   停服失败 / 停不干净则明确报错，不硬来；
3. 新增测试：`TestAtomicOverwrite`（含"覆盖正在执行的程序"）、
   `TestInstallStopsServerFirst`（断言任务里 `stop()` 在 `integrate` 之前）。

**状态**：代码已修并测试通过（版本号已置为 0.2.7），**尚未发布** —— 按用户要求先记录。

---

## 验证记录（本次排查）

| 项 | 结果 |
| :--- | :--- |
| 用 Linux 版游戏本体启动 | ✅ `phase=运行中`，5/5 进程，三层判据全绿 |
| SMAPI 版本 | ✅ 4.5.2（由管理器从官方 Release 获取整合） |
| MOD 加载 | ✅ `Loaded 2 mods`（Console Commands 4.5.2、Save Backup 4.5.2） |
| 整机负载 | ✅ load average 8.16 → 0.44（此前是我遗留的测试进程空转在 230%） |

---

## I-7 「在线玩家」纯占位卡片（已修）

总览页有一张「在线玩家」卡片，内容只有：

> ⚠️ 暂不支持 · 上游 2.x 不提供可靠的在线玩家接口；本项目不伪造数据。可在 WebVNC 画面中直接查看，或参考后续版本。
> 本页不展示推测数据：只有在上游接口能可靠提供时才会显示玩家列表。

**问题**：它不提供任何信息，纯粹占版面，还要用户读一段免责声明。
**处理**：删除该卡片，并同时删掉接口里对应的 `players` 占位字段
（「不伪造数据」这句原则本身是对的，但不需要用一张卡片来宣告）。

## I-8 两个死设置（已修）

用脚本逐个配置字段核对「服务端是否真的读它」，发现两个**从未被任何代码读取**：

| 设置 | 界面文案 | 处理 |
| :--- | :--- | :--- |
| `confirm_destructive` | 「破坏性操作要求二次确认」 | **接上**：关闭后界面不再弹确认框（需输入指定文字才放行的少数操作仍强制确认）；服务端快照/校验不受影响，界面说明已写清生效范围 |
| `metrics_interval_seconds` | 「指标采样间隔（秒）」 | **删除**：本项目没有指标采集子系统，这个设置是空的 |

**教训**：新增配置项时必须有代码消费它，否则就是在骗用户。
（已用脚本对全部字段做过一次核对：其余字段都有真实引用。）

## I-9 Steam 扫码取游戏本体的三个实现缺陷（已修，未发布）

新增「用 Steam 手机 App 扫码获取/更新游戏本体」功能，实测中踩到 3 个问题：

| # | 问题 | 根因 | 修复 |
| :--- | :--- | :--- | :--- |
| a | 任务在跑，但界面一直拿不到二维码 | `run_fetch` 边读边按行输出日志，**日志循环把二维码字符行从同一缓冲区里消费掉了**，导致永远凑不齐一整张码 | 二维码用**独立的累积缓冲** `qr_buffer`，与行日志缓冲分离 |
| b | 点「取消任务」返回 500「服务器内部错误：TaskFailure」，且后台进程还在跑 | ① `cancellable` 是**提交**时的参数，接口层没传 → 任务不允许取消；② 通用 500 处理把具体原因吞掉了 | 提交时传 `cancellable=True`；`TaskFailure`/`TaskCancelled` 在 HTTP 层透出**具体原因**（409 + 原文） |
| c | 取消后任务仍停在 `running` | `os.read()` **阻塞**在"等扫码"上（那段时间没有输出），取消/超时检查永远得不到执行 | 改用 `select.select(timeout=1)` 轮询，无数据时回到循环顶部检查取消与超时 |

**实测结果**（真机沙箱，做到"等待扫码"为止）：二维码 29×29 自校验通过 →
取消返回 ok → 终态 `cancelled`（不是 failed）→ **0 个残留进程** → 二维码状态自动清空。

---

## 已实现：Steam 扫码获取 / 更新游戏本体

**为什么这样做**：之前要求用户"自己准备 Linux 版游戏本体"，但用户很容易传错平台
（实测就传了 Windows 版），而且从第三方镜像取文件在合规上是灰的。
用 Steam 扫码让用户**用自己的账号**下载，是唯一既方便又干净的路径。

| 项 | 说明 |
| :--- | :--- |
| 下载实现 | [DepotDownloader](https://github.com/SteamRE/DepotDownloader)（成熟的 Steam 客户端协议开源实现） |
| 登录方式 | **`-qr` 扫码**：不需要在 NAS 上输入 Steam 账号密码，也不需要 Steam Guard 验证码 |
| 平台 | 显式 `-os linux -osarch 64`，避免拉到 Windows/macOS depot |
| 许可证 | DepotDownloader 是 **GPL-2.0** → 本项目**只在运行时从官方 Release 下载**，**不打进 FPK**（与显示栈一致），并随附其 LICENSE |
| 登录态 | `-remember-password` 缓存登录密钥 → 之后「更新游戏本体」无需再次扫码 |
| 隐私 | 密钥保存在 `$TRIM_PKGHOME/steamtools/credentials`（**0700**），位于数据区**之外** → **不进快照、不进诊断包**（有测试断言） |
| 二维码 | DepotDownloader 用字符画打印（模块宽 2 字符、高 1 行，且会打印多张）；后端把它**解析回 0/1 矩阵**并用**三个定位图案 + 时序图案自校验**，前端用 **SVG** 精确渲染（矢量、不依赖字体，手机可扫） |
| 就位流程 | 与上传 ZIP **共用同一套**逻辑（平台校验 → 替换线上目录 → 建立链接 → 补齐 SMAPI） |

---

## I-10 游戏在软件渲染上吃满 CPU（本机有核显却没用上）

**现象**：用户反馈"NAS CPU 占满了"。
**排查结论（先排除误判，再下结论）**：

1. 用 5 秒采样测**瞬时** CPU，而不是看 `ps` 的 `%CPU`（那是**进程生命周期平均值**，
   曾把我误导成"管理器占 30%"，实际它当时只有 2.4%）；
2. 逐线程采样：**5 个线程各占 55–100%**，合计约 **290%** —— 这是
   **Mesa 软件光栅化器 llvmpipe** 的典型特征（它按核数并行）；
3. 确认本机**其实有 GPU**：`/dev/dri/card0` + `renderD128`，
   `i915` 驱动正常，PCI `8086:46d4`（Intel ADL-N 核显），
   且 Mesa 的 `iris_dri.so` 硬件驱动**已安装**；
4. 根因：显示栈用的是 **Xvfb**，而 **Xvfb 不支持硬件 GLX（只能用软件渲染）**；
   叠加我们的 `software_gl=True` 又显式设了 `LIBGL_ALWAYS_SOFTWARE=1`。

**已验证的解决路径（实测数据）**：改用 **Weston headless（GL 渲染器）+ Xwayland**，
让 X11 客户端走核显。两个组件本机**已装**（`/bin/weston`、`/bin/Xwayland`），**零下载**。

| 项目 | Xvfb（现状） | Weston headless + Xwayland |
| :--- | :--- | :--- |
| 图形驱动 | llvmpipe（CPU 软件渲染） | **iris（Intel 核显）** |
| 驱动证据 | — | `LIBGL_DEBUG=verbose` → `pci id for fd 112: 8086:46d4, driver iris` |
| 就绪后 CPU | **约 290%** | **约 109%**（降约 2.6 倍） |
| 内存 | 933 MB | 853 MB |
| 达到 `Mods loaded and ready!` | 14–19 s | 33 s（首帧着色器编译更慢） |
| 依赖 | 无 | weston + Xwayland（已装） |

**实测命令序列**（可复现）：

```sh
export XDG_RUNTIME_DIR=<0700 目录>          # 必须在应用家目录内
weston --backend=headless-backend.so --renderer=gl --socket=wl-vs --idle-time=0
WAYLAND_DISPLAY=wl-vs Xwayland :96 -rootless -noreset -nolisten tcp
# 游戏侧：DISPLAY=:96，且**不要**设 LIBGL_ALWAYS_SOFTWARE
```

**实现计划（待用户确认后做）**：
1. `ManagerConfig` 增加 `native.display_backend`：`xvfb`（默认，兼容）| `weston`（核显）；
2. `NativeRuntime._specs()` 在 `weston` 模式下增加 `weston` 与 `xwayland` 两个受管进程，
   并注入 `XDG_RUNTIME_DIR`（`$TRIM_PKGHOME` 内的 0700 目录）与 `WAYLAND_DISPLAY`；
3. `software_gl` 与 `display_backend` 联动校验（weston 模式下不得强制软件渲染，配置页给出说明）；
4. `stop`/`uninstall` 的进程清理扩展到这两个新进程；
5. 配置页提供切换与**实测对比说明**（明确写清收益与"首次启动更慢"的代价）；
6. 测试：模式切换的 argv 构造、环境变量注入、`XDG_RUNTIME_DIR` 权限为 0700、
   缺少 weston/Xwayland 时给出可操作错误、停止后无残留。

**注意**：核显与 fnOS 其它应用（界面、硬件转码等）共享，切换后需观察是否互相影响。
**未验证**：切换后长时间稳定性、VNC 画面是否正常显示（本次只验证了渲染与 CPU，未抓 VNC 画面）。

---

## I-1 修复说明（已修）

1. **路由顺序**：MOD 的**具体路径**全部移到通配路由 `GET /api/mods/{dir_name}` **之前**；
2. **保留名排除**：通配正则加上否定环视，排除 `upstream-bundle` / `smapi-log` /
   `install-plan` / `install` / `remove` / `toggle`（双保险：即使将来有人调整顺序，
   保留路径也不会再被当成 MOD 目录名）；
3. **前端守卫**：新增 `bundleInfo()`，取不到 `url` 时**明确报"接口返回的不是上游
   MOD 包信息"**，而不是把一个 `undefined` 提交回后端、得到一句与真实原因无关的提示。

**测试**：新增 `tests/test_routes.py`（10 项），用**真实注册路径**
（`api_handlers.register(app)`）断言：
* `upstream-bundle` / `smapi-log` 命中各自处理器，且**不被当成 MOD 名**；
* 真实 MOD 名（`ConsoleCommands` 等）仍然命中 `mod_detail`；
* 不变量：**所有 MOD 具体路由的下标都小于通配路由**。

## I-2 修复说明（已修）

**方案**：在管理器自身 HTTP 前缀下反代 `/vnc/*` → 本机 websockify，
于是 **能打开管理界面的地方就能看画面**（直连、飞牛网关、fnconnect 都行），
也不需要把 5800 暴露到局域网。

* `vncproxy.py`：HTTP 转发（剥离逐跳首部）+ WebSocket 双向字节转发
  （`select` 轮询，非阻塞，带空闲超时）；
* **鉴权**：该路径能实时看到并操作游戏画面，等同管理权限 → **未登录一律 401**
  （否则反代就是一个新的未授权入口）；
* 上游未就绪 → **502**（而不是 500 或挂死）；
* 前端改用**相对地址** `appBase() + 'vnc/vnc.html?…&path=…/vnc/websockify'`，
  因此在任何访问方式下都指向正确位置。

**真实组件验证**（不是假的替身）：

| 检查 | 结果 |
| :--- | :--- |
| 经反代取真实 noVNC 页面 | HTTP 200，15212 字节 |
| 经反代取真实 `core/rfb.js` | HTTP 200，105544 字节 |
| 经反代做真实 WebSocket 握手 → websockify → x11vnc | **101 + 收到 x11vnc 的真实版本串 `RFB 003.008`** |
| 未登录访问（直连 / 网关前缀） | **401 / 401** |
| websockify 未启动 | **502** |

**测试**：新增 `tests/test_vncproxy.py`（19 项），含握手转发、双向回显、
客户端断开不抛异常、逐跳首部不透传、路径不越出反代根、
以及"前端不得再拼 host:port"的结构断言。

## I-10 修复说明（已修）：新增核显显示后端

新增配置 `native.display_backend`（默认 `xvfb`，保守）：

| 值 | 显示栈 | 渲染 | 实测就绪后 CPU | 就绪耗时 |
| :--- | :--- | :--- | :--- | :--- |
| `xvfb`（默认） | Xvfb | llvmpipe 软件 | 约 290% | 14–19 s |
| `weston` | Weston headless + Xwayland | **iris 核显** | **约 114%** | 约 33 s |

实现要点（每一条都来自实测踩到的坑）：

1. `_specs()` 按后端产出进程：`weston` / `xwayland` 加入受管进程；
2. **`status()/readiness()` 的进程清单改为遍历 `_specs()`** —— 原先写死名字，
   切后端后查不到 weston/xwayland，显示栈判据永远 `False`，
   **游戏明明在跑却被判"启动失败"**；
3. **环境变量按进程角色区分**（`x11` / `weston` / `xwayland`）：
   x11vnc 一旦看到 `WAYLAND_DISPLAY` 就会
   `Wayland display server detected ... Exiting.` —— 表现为游戏在跑但 VNC 连不上；
4. **Xwayland 必须非 rootless**：`-rootless` 模式下没有根窗口，
   x11vnc 抓屏报 `X GetImage BadMatch` 并崩溃；rootful 模式有根窗口，工作正常；
5. Xwayland **不支持** Xorg 的 `-logfile`（传了直接启动失败）；
6. Weston 输出尺寸跟随配置的 `screen_width/height`；
7. `stop()` 覆盖新进程并把显示服务器**放在最后**停（x11vnc/openbox 依赖它），
   同时清理 Wayland socket；
8. 配置页可切换，并写明实测对比；缺少 GPU/组件时校验阶段报错并指出可退回 Xvfb；
9. `gpu_status()` 上报**当前实际渲染器**（读 weston 日志的 `GL renderer`），
   若退化成 `llvmpipe` 能被识别出来 —— 不让用户误以为用上了核显。

**真机全流程验证**（用的是管理器自己的 API，不是手工命令）：
6/6 进程（含 x11vnc）、三层判据全绿、CPU **114%**（对比软件渲染 290%）、
停止后 **0 残留**、X socket 已清理。

**未验证**：核显模式下的长时间稳定性、以及与飞牛其它应用争用核显的影响。

---

## I-11 ~ I-14：Steam 扫码取游戏本体的第二轮实测缺陷（已修）

用户用真机扫码后反馈"手机上已经扫码登录了，但管理器上没刷新"。
查证发现是**四个独立缺陷**，其中 I-14 最严重（会让游戏根本起不来）。

### I-11 界面永远显示第一张二维码（用户扫到的是过期码）

DepotDownloader 会周期性刷新挑战码，输出里反复出现 `The QR code has changed:`。
**每次刷新的二维码都是 29×29 模块，长度完全相同**，而我前端的刷新判断写的是
"模块数量变了才重绘" → 界面永远停在第一张图上。

后果正是用户遇到的：手机端扫码"完成"了，管理器这边
`Failed to authenticate with Steam (AsyncJobFailedException)` —— 因为扫的是**过期码**。

修复：
* 前端改为比较**二维码内容指纹**（对矩阵做哈希），内容一变就重绘，并提示"已刷新 N 次，请扫最新这一张"；
* 后端在**登录成功的瞬间就清空二维码状态**，前端据此显示"扫码已确认，正在下载…"，
  不再让用户面对一张已经用完的码猜"到底成没成功"。

### I-12 下载全部成功后被判"没有找到下载出来的游戏目录"

用户第一次尝试的日志里其实已经：

```
Success! Next time you can login with -username fee11one1y -remember-password instead of -qr.
Depot 413153 - Downloaded 458056832 bytes (708901690 bytes uncompressed)
Disconnected from Steam
```

**登录成功、458 MB 下载完成**，但我的收尾逻辑报
"DepotDownloader 已结束，但没有找到下载出来的游戏目录" —— 任务判失败。

根因：DepotDownloader 的 `-dir` 就是目标目录，它**直接**把 depot 文件写进 `-dir` 的根，
**不会额外建一层 `Stardew Valley/`**；而我最初只找"名为 Stardew Valley 的子目录"或
"恰好只有一个子目录"。

修复：新增 `locate_game_root()`，按游戏标志物（`Stardew Valley.dll` /
`StardewModdingAPI` / `Stardew Valley.deps.json`）判定，**包含 `-dir` 自身**；
并用真机那次残留目录做了回归测试。

### I-13 登录密钥找错位置，每次都要重新扫码

DepotDownloader 用 .NET 的 **Isolated Storage** 保存登录密钥，实际落在
`$HOME/.local/share/IsolatedStorage/.../account.config`，
**不是** `$XDG_CONFIG_HOME/DepotDownloader`。我按后者找 → 明明登录成功却报
`cached_login: False`，下次又要求扫码。

修复：`login_key_file()` 按真实位置查找；并解析 DepotDownloader 回显的
`Success! ... -username fee11one1y -remember-password ...`，把账号名记下来，
**后续更新改用 `-username <账号> -remember-password`，不再需要扫码**。

### I-14 干净游戏目录整合 SMAPI 后启动器起不来（最严重）

用 Steam 正版副本（**干净**、原本没装过 SMAPI）时，整合完成后启动游戏，
控制台里只有一段 .NET 主机用法帮助：

```
host-options:
  --additionalprobingpath <path>  ...
  --runtimeconfig <path>          ...
```

根因：SMAPI 的 Linux 负载里**不含** `StardewModdingAPI.deps.json`，
官方 `install on Linux.sh` 是拿**游戏自己的 deps.json 复制一份**过去的。
缺了它，.NET apphost 会把应用判定为"依赖共享框架"（split/FX 模式），
而本机没有装 .NET 运行时（游戏是自包含发布），于是启动器直接打印帮助并退出。

**实测证据**：上游镜像里 `StardewModdingAPI.deps.json` 与 `Stardew Valley.deps.json`
**md5 完全一致**（都是 `4982acfbad256a44af34d941ab08d6d4`），确认官方做法就是复制。

**为什么以前没暴露**：上游镜像那份游戏本来就装过 SMAPI，该文件已存在；
换成干净的游戏目录立刻踩到。

修复：`integrate()` 末尾调用 `_ensure_deps_json()`，缺失时从游戏的 deps.json 复制一份。

**顺带发现**：修复前失败的启动尝试会留下**僵尸进程**，而管理器的
`resolve_pid` 通过 PID 文件找不到时不会误判（`pid_alive` 已排除 `Z` 状态），
但僵尸会一直挂到管理器重启 —— 已在文档中记录，暂不改动进程回收逻辑。

### 真机结果

用用户自己已下载好的 458 MB（复用，不重下）走产品自身的就位逻辑：

* 平台校验通过（linux、18 个 `.so`、游戏 1.6.15）；
* SMAPI 4.5.2 整合完成，并补上 `StardewModdingAPI.deps.json`；
* 启动后：**5/5 进程、三层判据全绿、`Loaded 2 mods`、`Mods loaded and ready!`**；
* 连续观察 30 秒 PID 未变、状态稳定（不再崩溃重启）。

**说明**：现在跑的是**用户自己的 Steam 正版副本**（之前那份来自上游镜像的游戏已被替换掉），
I-3 的合规问题因此消解。

---

## I-15 WebVNC 打开变成"下载文件"（已修）

**现象**：用户点「在新窗口打开 WebVNC」，浏览器把页面**当成文件下载**（或显示源码），
而不是渲染 noVNC 界面。

**根因**：HTTP 头名是**大小写不敏感**的，但 websockify 基于 Python 的 `http.server`，
它发的是 **`Content-type:`（小写 t）**：

```
HTTP/1.1 200 OK
Server: WebSockify Python/3.11.2
Content-type: text/html          ← 小写 t
Content-Length: 15212
```

而我在反代里按 `Content-Type`（大写 T）取值 → 取不到 → 落到默认值
`application/octet-stream` → 浏览器下载：

```
HTTP/1.1 200 OK
Content-Type: application/octet-stream   ← 修复前
```

**修复**：`_parse_response()` 把响应头名**统一转小写**，取值也统一用小写；
取不到时**不再默认成 `application/octet-stream`**，而是 `text/html`
（noVNC 的入口就是页面）。同时把上游的 `Server`/`Date` 列为
"由管理器自己生成、不透传"，避免响应里出现重复头。

**对真实 websockify 验证**：

| 资源 | 上游 | 修复后经反代 |
| :--- | :--- | :--- |
| `vnc.html` | `Content-type: text/html` | `Content-Type: text/html` ✅ |
| `core/rfb.js` | `Content-type: text/javascript` | `Content-Type: text/javascript` ✅ |
| `core/util/logging.js` | `Content-type: text/javascript` | `Content-Type: text/javascript` ✅ |
| `Content-Disposition` | 无 | 无（浏览器正常渲染） ✅ |

**测试**：新增 `TestContentTypePreserved`（6 项），假上游刻意用**小写 `Content-type`**
发送（与 websockify 真实行为一致），并断言头名已小写、类型被保留、
管理器侧不再出现 `application/octet-stream`。

---

## I-16 Steam depot 里的 `steam_appid.txt` 是 480（已修）

**发现过程**：收尾核对游戏目录时注意到 `steam_appid.txt` 的内容是 **480**，
而《星露谷物语》在 Steam 上是 **413150**。（480 是 Valve 的 Spacewar 测试应用。）

**查证**（不下结论，先取证）：

| 来源 | `steam_appid.txt` 内容 |
| :--- | :--- |
| 上游镜像里那份（上游装的 SMAPI） | `413150` ✅ |
| **SMAPI 4.5.2 官方负载** `internal/linux/install.dat` | `413150` ✅ |
| **DepotDownloader 解出来的 Steam depot** | **`480`** ❌ |

结论：SMAPI 没问题，**是 depot 自带的**。原因很清楚 —— `steam_appid.txt` 是
开发者构建时留在 depot 里的测试值，**Steam 客户端在安装时会把它改写成正确的 App ID**，
而 DepotDownloader 只是**原样解包 depot**，于是这个错误值被带进了游戏目录。

**影响**：Steamworks 会以 480 初始化 → Steam 联机（好友/邀请码加入）与成就失效。
局域网直连（本项目的 `game_port`）不受影响，所以服务本身照常运行 ——
这也正是它容易被忽略的原因。

**修复**：
* 新增 `smapi.ensure_app_id()`：内容不是 413150 就改写为 413150（缺失则创建）；
  已经正确时**不重写**（避免无意义的文件变更）；
* 在 `integrate()` 与 `_place_game_body()` 两处都调用 ——
  后者是必要的，因为"游戏目录本来就带 SMAPI"时会跳过整合；
* 原来的实现是"仅在缺失时写入"，其对应的旧测试
  （`test_steam_appid_only_when_missing`）断言的正是这个不够的契约，已按
  正确契约改写（新增 7 项测试）。

**真机**：已把用户当前安装的 `480` 修正为 `413150`。

---

## I-17 登录与获取/更新拆成两个独立动作（已修）

**用户提出的问题**（原话："登录和获取更新/下载是两码事，应该分开"）。

原来的做法是把「扫码登录」和「下载 400+ MB」塞进**同一个任务**，带来两个后果：

1. 用户在手机上扫码成功后，**还要继续等下载几分钟**，而任务进度里整段下载算一步，
   界面上一直冻在 20% —— 看起来就是"卡住了"；
2. 失败时无法区分是**登录失败**还是**下载失败**（两者混在一个任务的错误里）。

**现在的做法**：

| 动作 | 做什么 | 耗时 | 接口 |
| :--- | :--- | :--- | :--- |
| **Steam 扫码登录** | 只建立会话。用 `-manifest-only`：登录 + 拉清单（据此验证账号确实拥有该游戏），**不下载文件内容** | **秒级**（实测 6.8 s 免密登录） | `POST /api/native/steam/login` |
| **获取 / 更新游戏本体** | 用已登录的会话下载并就地替换 | 几分钟（取决于体积） | `POST /api/native/steam/fetch` |
| 退出登录 | 丢弃已保存的登录信息 | 即时 | `POST /api/native/steam/logout` |

* 未登录就点「获取 / 更新」→ 明确提示"请先完成 Steam 扫码登录"，并可一键跳转登录；
* 登录使用 `-manifest-only` 后，整个过程与"下载几百 MB"解耦，秒级就能看到结果。

**测试**：新增 `TestLoginFetchSeparation`（8 项），断言两者的命令行、接口与任务互相独立
（例如登录任务**不得**触碰游戏目录、不得触发快照；下载命令**不得**再出现 `-qr`）。

## I-18 登录凭据的存放位置与检测位置错位（已修）

**发现过程**：实测免密登录成功（`Logging 'fee11one1y' into Steam3... Done!`），
但 `login_key_file()` 返回 `None`；而磁盘上确实存在密钥文件，只是在**别处**：

```
/vol1/@apphome/valleyserver/.local/share/IsolatedStorage/…/account.config   ← 实际位置
/vol1/@apphome/valleyserver/steamtools/.local/share/IsolatedStorage/…       ← 检测位置
```

**根因**：`env["HOME"]` 写成 `credentials_dir().parent` —— 我把 `credentials_dir()`
改成 `tools_dir()` 之后，这一行把 `HOME` 指到了**上一级**（应用家目录），
于是 .NET Isolated Storage 落在 `$PKGHOME/.local/...`，与检测位置错开。

**修复**：
* `env["HOME"] = str(credentials_dir())`（就是 `tools_dir()`），与检测位置一致；
* 新增 `migrate_legacy_login()`：**把旧位置的凭据搬过来**，用户不必重新扫码
  （已实测迁移后仍能免密登录，8.7 s 成功）；
* `login_key_file()` 同时查找新旧两处（迁移失败也不至于误判为"未登录"）；
* 状态里新增 `login_incomplete`：只有账号、没有密钥时明确提示"建议重新登录"。

**测试**：新增 `TestCredentialLocation`（9 项），覆盖路径关系、迁移（含幂等与不覆盖新位置）、
旧位置兜底、以及"`HOME` 赋值不得带 `.parent`"的结构断言。

---

## I-19 ~ I-26：第三轮真机测试（已修）

用户反馈 4 个问题，逐个查证后共修掉 8 个缺陷，其中 I-20 直接决定"无人值守"能否成立。

### I-19 「已加载 MOD 0」

SMAPI 4.x 控制台打的是 `Loaded 7 mods:` **加缩进列表**，
而解析器只认旧版单行格式 `[HH:MM:SS INFO SMAPI] Loaded mod: <名>`。
另外 `console.log` 跨重启累积，里面有多块 `Loaded N mods:`（旧 2 + 新 7），
必须**只取最后一次**，否则会得到 9 这种虚高数字。

修复：`_parse_loaded_mods()` 支持 SMAPI 4.x 多行格式 + 只取最后一次加载记录。
真机结果：`loaded_count = 7`（ALOS / ChangeServerPort / ChatCommand /
CommandWebUI / Console Commands / Save Backup / ServerCMD）。

### I-20 服务在跑但不开服（ALOS 重启后不会自动载档）

**实测过程**：

| 步骤 | 结果 |
| :--- | :--- |
| 重启服务后观察 180 秒 | 一直停在 `titleScreenGameMode (0)`，`24642/UDP` 未监听 |
| 只发 `ServerCMD.load_save <存档>` | 不开服 |
| **先** `ServerCMD.set_multiplayermode 2` **再** `load_save` | **5 秒后** `Starting LAN server` + `[ALOS] Auto Mode On!` |

结论：ALOS 是 `SaveLoaded` 之后才 `setServerMode`/`enableServer`，
**重启后不会自己载档**；顺序也不能反 —— 游戏是在**载档时**读 `Game1.multiplayerMode`
（上游 `ServerCMD.LoadSave` 源码里也注释着 `//Game1.multiplayerMode = 2;`）。

修复：新增 `native.host-bootstrap` 任务 + 启动后自动载档
（配置项 `native.auto_host_on_start`，默认开启）。

**关于"没有存档"**（按用户要求）：

* **不自动创建存档** —— 上游 `ServerCMD.new_save` 的实现是
  `new TitleMenu().createdNewCharacter(true)`，它会**跳过命名流程**，
  建出玩家名/农场名都为空的存档（实测生成目录 `_451468556`、`<player><name />`）。
* 检测到没有存档时，提示：**「需手动新建存档并重启服务端」** ——
  ① 打开 WebVNC 在游戏里手动新建存档；② 重启服务端，管理器自动载入并开服。
* 用词说明：**服务本身能启动**，只是不会开服（不会在游戏端口上开主机）。

真机验证（完整重启 → 自动开服）：**18.1 秒**后 `24642/UDP` 正在监听、ALOS 接管。

### I-21 PC 端 Edge 仍把 WebVNC 当文件下载

服务端 `Content-Type` 在 0.2.9 已修对（实测 `text/html`），但反代响应**缺少 `Cache-Control`**，
却带着 `Last-Modified: 2021`（noVNC 自带时间戳）→ Edge 按启发式规则**长期缓存**了
修复前那个 `application/octet-stream` 的副本；手机是新请求所以正常。
修复：反代响应加 `Cache-Control: no-store, must-revalidate` + `Pragma: no-cache`，
HTML 额外加 `Content-Disposition: inline`。
**注意**：旧副本在客户端，用户需硬刷新（Ctrl+F5）一次。

### I-22 页面重复内容

| 重复 | 处理 |
| :--- | :--- |
| 配置页与系统页各有一份**完全相同的 5 个按钮** | 配置页移除，改为指向系统页的入口；操作统一留系统页 |
| 「日志」卡片在控制台页与系统页各一份 | 系统页移除卡片，改为「查看日志」按钮跳控制台页 |

存档页与快照页的「恢复/删除」作用于不同对象，属合理，未动。
新增**跨页重复检测测试**，以后不会再悄悄重复。

### I-23 指令通道：stdin 不通，CommandWebUI 的 WS 可用但会崩游戏

1. **stdin 不通**：把游戏进程 stdin 接成管道后，`/proc/<pid>/fd/0` 确实是 `pipe:[…]`、
   写入也成功，但游戏侧**没有任何线程读它**（线程 wchan 无管道读），
   `help` 发出去毫无回显 → 指令进黑洞。
2. **CommandWebUI 的 WS 可用**：`ws://127.0.0.1:29103/ws`，指令是**纯文本消息**。
   实测发 `help` 能收到 SMAPI 完整帮助输出。
3. **但它有上游崩溃缺陷**：该模组的 WS 写循环没有捕获异常
   （`Mods/CommandWebUI/Server.cs:129`）。客户端断开时它写死 socket →
   `Broken pipe` → **Unhandled exception → 整个游戏进程崩溃**。
   **本项目在测试中真的触发过一次**（已重启恢复）。
   这也意味着：浏览器里那个 SMAPI 控制台页面，关标签页同样可能崩游戏。

缓解措施（已实现）：发送后**持续排空**对端输出再关闭（把"写坏管道"的窗口压到最小）；
界面与代码里写明风险；自动开服加**防崩溃循环**（连续失败 3 次即停手）；
所有相关测试改用**本地假 WS 服务**，绝不碰真实实例。

> 用户决定：**29103 的局域网暴露不处理**（局域网可信）。已在配置页注明该 WS 无认证。

### I-24 游戏端口是 UDP

`port_open()` 是 TCP 连接检测，而星露谷的游戏端口是 **UDP** →
"服务器是否已开服"永远判为 False。实测：ALOS 已开服
（`[ALOS] Auto Mode On!`、UDP 24642 已监听），管理器却报
"24642 未在 90s 内就绪"，把成功判成可疑。
修复：新增 `udp_port_open()`（直接解析 `/proc/net/udp{,6}`，不依赖 ss/lsof）。

### I-25 历史崩溃文本污染 L3 判据

`console.log` 是**追加**写入的，里面留着上一次崩溃的 `Unhandled exception`，
使 `L3_smapi_loaded`（含"致命错误"判定）**永远为 False** —— 明明 SMAPI 已正常加载。
修复：只截取**最后一次 SMAPI 启动横幅之后**的日志做判定。

### I-26 僵尸子进程

游戏/显示栈子进程此前只用 PID 文件记录，从不 `wait()`/`poll()`；
启动失败时会留下 `<defunct>` 直到管理器重启（真机留下过 3 个）。
修复：登记 `Popen` 对象，`readiness()` 与 `stop()` 时回收。

### 本轮真机测试结论汇总

| 项 | 结果 |
| :--- | :--- |
| ALOS 重启后自动载档 | **否**（必须由管理器主动指令） |
| 正确指令顺序 | `set_multiplayermode 2` → `load_save <名>` |
| 完整重启 → 开服耗时 | **18.1 秒** |
| 核显后端（Weston + Xwayland） | 游戏 CPU **290% → 约 30%**；整机负载 8~10 → **0.83** |
| GPU 证据 | `GL vendor: Intel`；weston / xwayland / 游戏均持有 `/dev/dri/renderD128` |
| 指令通道稳定性 | 连发 8 次指令未再崩溃（排空保护生效） |

---

## I-27 ~ I-29：第四轮（用户反馈，已修）

### I-27 默认显示后端不是核显

用户反馈："这版默认还是没用 gpu"（GPU 占用 0、CPU 99%）。

原因：我加核显后端时**把默认值设成了保守的 `xvfb`（纯软件渲染）**，
需要用户自己去配置页手动切换 —— 但用户的机器有 Intel 核显，
这等于把"本该自动的事"丢给了用户。

修复：默认值改为 **`auto`** —— 新增 `NativeRuntime.effective_backend()`：
`auto` 时若本机核显栈可用（render 节点 + weston + Xwayland）就用 `weston`，
否则退回 `xvfb`。所有后端判断（进程清单、环境变量、启停分支、就绪判据、
校验）都改走 `effective_backend()`，不再直接比较配置值。
配置页选项改为：**自动（推荐）/ 强制 Weston / 强制 Xvfb**。

真机验证（本机）：`auto → weston`，进程清单变成
`[game, openbox, websockify, weston, x11vnc, xwayland]`（软件渲染用的 xvfb 不再出现），
且不会设置 `LIBGL_ALWAYS_SOFTWARE`。

### I-28 切换显示后端必然失败

在 0.2.11 上尝试把已安装的 0.2.9 切到核显时，`config.apply` **超时回滚、回滚也失败**，
暴露出：`stop()` 只按**新配置**的进程清单停服 → 切到 weston 时**旧的 Xvfb 没被停掉**、
仍占着 `:99` → Xwayland 报
`failed to read Wayland events: Broken pipe` / `SocketCreateListener() failed` 起不来。

修复：
* `_all_specs()`：`stop()` 覆盖**两个后端**的进程；
* `_stop_stale_backend_stack()`：`start()` 前先清掉"另一个显示后端"遗留的进程
  （xvfb 模式清 weston/xwayland，反之亦然）；
* 新增 5 项测试（含"仅这两个方法可以启动服务端"的结构断言）。

### I-29 三个动作必须是独立的手动动作

用户明确要求：**「Steam 扫码登录」「下载获取游戏」「启动服务端」是三件独立的事，
每个都要人自己点**。此前实现存在自动接力：

| 位置 | 原行为 | 现行为 |
| :--- | :--- | :--- |
| `native.steam-fetch` | `start_after` 默认 true → 下载完成后**自动启动服务端** | **不启动**；明确提示"请到「总览」页点「启动服务器」" |
| `native.install-smapi` | 若原本在运行，更新后**自动重启** | **不重启**；同样明确提示 |
| 上述两者的失败/取消路径 | 自动"恢复运行" | **不自动恢复**；提示服务端处于停止状态及如何启动 |

现在**只有两个方法允许启动服务端**，且都属于"启动语义本身"：
`task_server_install`（安装/首次启动）与 `rollback_config_apply`（配置回滚必须重启才生效）。

接口层面也去掉 `start_after` 参数；界面文案写明"下载完成后不会自动启动服务端"。
新增 `TestThreeActionsAreIndependent`（8 项）把这条原则钉成测试，
包括"除这两个方法外不得出现 `runtime.up()`"。

> 登录任务本身从 0.2.10 起就只做登录（`-manifest-only`），不会触发下载；
> 本次进一步确保它也不会启动服务端。

### 关于"没有存档"的措辞（用户澄清）

用户指出我把"服务能启动"与"会开服"混为一谈。正确表述：
**服务本身能启动（游戏进程在跑），但没有存档就不会开服**（不会在游戏端口上开主机）。
因此提示统一为：**「需手动新建存档并重启服务端」** ——
① 打开 WebVNC 在游戏里新建存档；② 重启服务端，管理器自动载入并开服。

---

## I-30 ~ I-32：第五轮（用户要求"排查还有没有类似的自动运行"）

### I-30 自动载档开服的覆盖缺口（本轮最有价值的发现）

"载档开服"这一步此前只挂在 `task_server_start` 上。排查后发现**还有四条由管理器
启动服务端的路径完全漏掉了它** —— 每一条都会留下"进程在跑但不开服"的假运行状态：

| 路径 | 后果 |
| :--- | :--- |
| supervisor 的 **NAS 开机自启**（`_boot_recovery`） | 重启 NAS 后服务器**永远不开服** —— 无人值守在重启后失效 |
| supervisor 的**崩溃自动恢复**（`_tick` → `_auto_start`） | 崩溃恢复后同样停在标题页 |
| **配置变更**后重启（`task_config_apply`） | 改任何设置都会让服务器从"开服中"变成"未开服" |
| **配置回滚**后重启（`rollback_config_apply`） | 同上 |
| 首次安装启动（`task_server_install`） | 装完不会开服（无存档时给出提示） |

根因是它们都直接调 `runtime.up()`，绕过了 `server.start` 任务里的自动开服逻辑。

修复：把逻辑抽成**模块级共享实现** `auto_host_load_save(runtime, log, cfg, failures=...)`，
`_Services._maybe_auto_host` 只是薄封装；上述五条路径全部接上。
supervisor 侧用**后台线程**执行（不阻塞监管循环，含 `_host_lock` 并发保护、
`_stop` 退出检查、失败计数），状态写进事件库便于排查。

顺带把等待时长做成可用环境变量覆盖（`VS_CONSOLE_CHANNEL_WAIT` / `VS_HOST_PORT_WAIT`）——
因为单测里的 `fresh_services()` 会 `importlib.reload(orchestration)`，
直接改模块常量会被重置（实测：设了 0.2 仍真等了 60 秒）。

### I-31 设置页表述精简

逐项去掉实现细节与实测数字，只留防错要点。示例：

| 项 | 原表述 | 现表述 |
| :--- | :--- | :--- |
| 显示组件来源 | 「应用内捆绑」用 apt-get download + dpkg -x 解到应用数据目录，不修改系统包状态 | 捆绑模式不改动系统 |
| 软件渲染回退 | NAS 通常没有 GPU；开启后使用 Mesa 软件渲染（实测可用） | 无核显时用软件渲染 |
| 显示后端 | 默认「自动」…实测对比（Intel 核显）：核显约 30% CPU、负载 0.83；软件约 290%… | 自动 = 有核显用核显，否则软件渲染 |
| 启动后自动载档开服 | 实测确认 ALOS 重启后不会自动载档…（三行长文） | ALOS 不会自己载档；开启后每次启动都会载入最新存档并开服 |
| 高危提示 | 高风险操作：请先阅读下面的影响范围；系统会在执行前自动创建快照。 | 高风险操作：执行前会自动创建快照。 |

安全类警告（无认证、端口暴露、上游 WS 崩溃缺陷）**保留**，但同样压缩到一两句。

### I-32 单测会碰到真实实例的端口

排查时发现测试耗时从 3.5s 涨到 63s，追下去是：测试沿用了**默认端口**
（游戏 24642、CommandWebUI 29103 …），而本机正是真机 —— 这些端口上跑着**用户的真实实例**。
历史踩过两次：一次单测直接连上真实游戏的控制台通道并发出指令；一次"断言通道不可用"
却因真机在监听而失败。

修复（两层）：
1. `tests/_bootstrap.isolated_env()` 在引导阶段把沙箱配置里的
   `service_port/game_port/vnc_port/webvnc_port/cmdwebui_port` 全部改成**绑定 port 0 得到的空闲端口**；
2. 等待时长走环境变量（见 I-30），测试设为 0.2 秒。

原则：**测试不得依赖"本机没有真机实例"这个假设。**
---

## I-33：容器时代字段迁移（阶段 2，已修）

**问题**：早期按容器/镜像建模，库里与快照 manifest 里存的是 `image_ref`（**实际是游戏本体
目录路径**）与 `image_digest`（恒空）。名字既误导，也让人以为存在"镜像级回滚"。

**实测语义**（不是猜测）：`image_ref` = 游戏本体目录路径 → 迁移为 `game_dir`；
`image_digest` 恒空、无赋值逻辑 → **不**迁移为 `content_hash`（需求规定只有确认是指纹才能迁移）。
真正的指纹字段本来就有：`mods_hash` / `saves_hash`。

**迁移中发现的真实缺陷**：老库 `versions.image_ref TEXT NOT NULL` **且无默认值**，
而新代码不再写该列 → 升级后**任何新插入都会 `NOT NULL constraint failed`**。
（迁移测试实测抓到。）SQLite 无法给已有列补默认值，因此采用"建新表 → 搬数据 → 换名"重建，
重建后旧列保留但放宽为 `DEFAULT ''`，历史记录继续可查。

**安全性**：幂等（只在目标列为空时回填）、不重写历史 JSON、不触碰存档/MOD/快照 payload 与
其 SHA-256、快照仍可通过校验（manifest 自身不参与校验）。详见 `docs/field-migration.md`。

**顺带清理**：删除死代码 `util.validate_image_ref()`（全仓库无调用者）；
删除 API 里名不副实的 `container` 配置段（`ALLOWED_CONFIG_FIELDS` 里根本没有该段，
提交必然报错）；运行状态类型 `ContainerStatus` → `RuntimeStatus`，字段 `image` → `game_dir`；
日志源键 `container` → `runtime`（**磁盘文件名 `container.log` 保留**，否则旧日志会成孤儿）。

**护栏升级**：原护栏只匹配英文 `docker|compose`，覆盖不到 `容器/镜像/container/image`。
现改为两层：①真实实现与调用（docker 命令、socket/API、`import docker`、`docker-project`）必须为 0；
②旧命名只允许出现在迁移/兼容白名单内，且**按文件设上限**（超出即失败）。
---

## I-34：状态模型把"运行中"误报成「未启动」（0.2.14 修复）

**现象**（真机，0.2.13）：6 个进程全在跑、`phase` 是「运行中（未开服）」，但接口与界面
报 **服务运行状态 = 未启动**。

**根因**：`_state_model()` 读的是 `out.get("exists")`，而 `readiness()` 的**顶层没有 `exists` 键**
（只有 `runtime` 状态字典），于是恒为 `None` → 判定为未启动。

**修复**：改为 `(out.get("runtime") or {}).get("exists")`，并加回归测试
（进程在跑时不得报「未启动」）。真机验证：修复前 `未启动` → 修复后 `运行中`。

---

## I-35：`host_bind` 从未作用到 x11vnc / websockify（待决策）

**现象**：配置里 `host_bind = 127.0.0.1`（界面提示"仅本机（推荐）"），但实测两个端口
**都监听在 0.0.0.0**：

```
实际监听: 0.0.0.0:5900 (x11vnc)   0.0.0.0:5800 (websockify)
配置值:   127.0.0.1
```

**根因**：`host_bind` 只用于校验与安全提示，**没有传给进程**：
* `_x11vnc_argv()` 没有 `-localhost`；
* `_websockify_argv()` 是 `websockify --web=… <port> localhost:<vnc_port>`，没有 `--listen 127.0.0.1:<port>`。

**影响**：WebVNC / VNC 均无认证（VNC 有 8 位随机密码），而界面告诉用户"仅本机"——
设置与实际不符。**同时**这也是局域网能直连 VNC 的原因。

**处置（用户选择方案 B，0.2.14）**：
* 默认值改为 `0.0.0.0`（保留局域网直连 VNC/WebVNC）；
* **同时让设置真正生效**：`host_bind=127.0.0.1` 时 x11vnc 加 `-localhost`、
  websockify 绑 `127.0.0.1:<port>`（websockify 源参数支持 `host:port`）；
* 文案改为如实说明：「只影响 VNC 与 WebVNC；CommandWebUI 由上游模组自行监听所有网卡，无法约束」，
  总览页风险横幅同步改写；
* **一次性迁移**：0.2.13 及以前没记录过用户选择、且值为旧默认 `127.0.0.1` 的配置，
  在升级后自动改为 `0.0.0.0` 并打上 `host_bind_reviewed` 标记 ——
  否则新生效的绑定会把用户原本可用的局域网 VNC 掐断。用户之后在界面上的选择一律尊重（不再自动改）。
---

## I-36：同后端的陈旧显示栈没被清理 → Xwayland 起不来（0.2.15 修复）

**现象**（真机，0.2.14）：点「启动服务器」失败：

```
启动失败：Xwayland 未创建 X socket（显示 :99）
日志尾部：… server already running … Cannot establish any listening sockets
```

**现场**：机器上同时存在**两个 weston 栈** ——

```
20:10:10  weston 1875742 + Xwayland :99 (1875751)   ← 旧栈，Xwayland 仍占着 :99
20:46:29  weston 1938302（新栈）                     ← 它的 Xwayland 绑不上 :99
xwayland.pid = 1940073（dead：PID 文件已被覆盖/指向死进程）
```

**根因**：0.2.12 加的 `_stop_stale_backend_stack()` **只清"另一个后端"**（weston ↔ xvfb），
当陈旧栈是**同一个后端**时（旧 weston/Xwayland 仍在，但 PID 文件已失去跟踪）就漏掉了。
且 `readiness` 会因为孤儿进程存在而把它们算成"在运行"，掩盖问题。

**修复**：新增 `_cleanup_stale_display()`，在 `start()` 里、清理另一个后端之后执行：
按「是否仍被 PID 文件跟踪」判断 —— 受跟踪且存活 → 保留；否则若归属本应用 → 停掉，
删陈旧 PID 文件并清理 X socket/lock 与 Wayland socket。**绝不碰不属于本应用的进程**
（真机验证：同机的 fygo-browser openbox 未被误杀）。

**真机验证**：`stop()` 收掉两个栈 → `start()` 后 **Xwayland 正常拿到 `:99`**（PID 1942386），
6/6 进程在跑、SMAPI 12 秒就绪、`server already running` 不再出现；
管理器接口报告 `服务 = 运行中`、`农场 = 尚未创建存档`。

**新增测试**：4 项确定性用例（受跟踪保留 / 孤儿停掉并清 socket / 外来进程绝不碰 / 多孤儿），
外加 start() 必须调用清理的结构断言。
---

## I-37：noVNC 的 WebSocket 路径拼成 `//app/...` → 404 → 每 5 秒重连（0.2.15 修复）

**现象**（真机）：局域网打开 WebVNC「还是没修复」，而且**VNC 里鼠标点击经常失效**。

**证据链**：
1. nginx 访问日志里那条 WebSocket 请求是 **`GET //app/valleyserver/vnc/websockify` → 404**（注意双斜杠）；
2. noVNC 的 `app/ui.js` 构造地址是 `url = 'ws://' + host + (port?':'+port:'') + '/' + path`
   —— 它**自己会加一个 `/`**，所以 `path` 带前导斜杠必然拼成 `//…`；
3. 而管理器自己的 UI 生成地址时用的正是 `encodeURIComponent(appBase() + 'vnc/websockify')`
   （`appBase()` 形如 `/app/valleyserver/`，带前导斜杠）→ 复现 1+2；
4. 结果：WebSocket 从未建立，noVNC 每 5 秒重试一次（x11vnc 日志里能看到规律性的
   `Got connection / Client gone`，其中还有我们自己的 `port_open()` 探测连接）。
   **断开期间点击必然丢失** —— 所以"页面加载不出来"和"点击经常失效"是同一个根因。

**X 侧已排除（用 ctypes + XTEST 实测）**：输入焦点就在游戏窗口
（`Stardew Valley 1.6.15 - running SMAPI 4.5.2 with 7 mods`）；
`XTestFakeMotionEvent(640,360)` 后指针**精确落在 (640,360)**；
Xwayland 屏幕 1280x720、游戏窗口 1280x720@(0,0) **完全重合** → 不存在坐标错位。

**修复**：
* 前端新增 `vncWsPath()`，**去掉前导斜杠**（`path=app/valleyserver/vnc/websockify`）；
* 服务端**折叠重复斜杠**（`//app/...` 也能正常工作）—— 让用户手抄/旧的绝对地址立刻可用；
* 回归测试：前端不得出现带前导斜杠的 `path`；`//app/.../vnc/*` 必须归一化后受鉴权保护（401 而非 404）；
  另有一条测试确保 `//api/dashboard` 仍然 401（不能被归一化绕过鉴权）。

> 注意：`x11vnc -display :99` 日志里那串每 5 秒一次的 `Got connection / Client gone`
> **多数是管理器自己的 `port_open(5900)` 健康探测**（连接即断开），不是真实客户端，
> 排查时别被它误导。
---

## I-38：`NameError: name 'LOG' is not defined` → 启动服务器直接失败（0.2.16 修复）

**现象**（真机，0.2.15）：点「启动服务器」报
`任务失败：启动服务器 —— NameError: name 'LOG' is not defined`。

**根因**：0.2.12 我在 `start()` 里加了「清理上一个显示后端的残留进程」，并写了
`LOG.info(...)`，但 `native_runtime.py` **从未定义 `LOG`**（其它模块有，
这个模块没有）。该分支只有在**确实发现陈旧进程**时才执行，所以：
* 平时（无残留）不会触发 → 单元测试全绿也发现不了（**分支未被覆盖**）；
* 一旦真有残留（用户这次正是这种情况）→ `NameError` → 启动任务直接失败。

**修复**：在 `native_runtime.py` 定义
`from .logmod import get_logger` + `LOG = get_logger("manager")`
（与 supervisor 同一日志源，写 manager.log）。

**回归测试**（3 条，且已验证"注入 bug 会变红"）：
1. `native_runtime` 必须定义 `LOG` 且可正常写日志；
2. **让两个清理函数都返回非空**，驱动 `start()` 真正走过那两行日志代码 ——
   为此需要绕过 `start()` 前面的三个早退守卫（显示组件/启动器/已在运行），
   否则测试会依赖沙箱里是否恰好有游戏本体（顺序依赖）；
3. `start()` 必须调用这两个清理函数。
4. 附带：`launcher` 是**类级 property**，测试改了要在 `tearDown` 还原，否则污染同进程用例。

**真机验证**：用修复后的代码实际执行 `stop()` → `start()`，日志分支被走到且无异常，
服务器 6/6 进程起来、SMAPI 15 秒就绪、`服务 = 运行中`。
---

## I-39：「重启服务端」从不自动载档开服（0.2.17 修复）

**现象**（真机）：按需求文档的做法"新建存档 → **重启服务端**"，连续重启两次都不会自动加载存档，
一直停在标题页。

**根因**：自动开服只挂在 `task_server_start` 上；`task_server_restart`（界面上的「重启服务端」）
只做了 `runtime.restart() → _wait_ready() → return`，**从未调用 `_maybe_auto_host`**。
`task_server_recreate` 同样漏了。日志印证：21:39 之后再没有任何 `[自动开服]` 记录。

**修复**：`task_server_restart` 与 `task_server_recreate` 在 `_wait_ready()` 之后、
确认游戏进程在跑时，补上 `versions.record(...)` + `_maybe_auto_host(...)`（与 start 完全一致）。

**测试**：结构断言（两个任务源码必须含 `_maybe_auto_host`）+ 运行时断言
（用替身 runtime 跑 `task_server_restart`，断言 `_maybe_auto_host` 恰好被调用一次）。

---

## I-40：日志页 500 —— `can only concatenate str (not "bytes") to str`（0.2.17 修复）

**现象**（真机）：用户点开「日志」页看原因，接口直接抛未处理异常。

**根因**：`logmod.Logger.tail()` 用 `open(path, "r", ...)` **文本模式**打开，
却按字节块回读并拼接 `data = fh.read(step) + data`（`data` 初始为 `b""`）
→ `TypeError`。该接口此前**没有任何测试覆盖"非空文件"的读取路径**
（`tools/test-logrotate.sh` 只测轮转）。

**修复**：改为 `open(path, "rb")`。**新增 `tests/test_logmod.py`（5 项）**：
真文件读取、级别过滤、未知级别返回全部、文件不存在返回空、>256 KiB 大文件从尾部回读。
已验证"把实现改回文本模式会让 4 项变红"。

---

## I-41：ALOS 在没人联机时不会让房主睡觉 → 天数不推进（0.2.17 增加开关）

**用户反馈**："手动加载以后房主玩家也并不会自动地睡觉"。

**根因**（读上游源码，不是猜的）：

```csharp
// Mods/ALOS/ModEntry.cs:789
this.numPlayers = Game1.otherFarmers.Count;      // **不含房主自己**
// ModEntry.cs:1165
else if (currentTime >= this.Config.timeOfDayToSleep && (numPlayers >= 1 || debug))
    GoToBed();                                    // 至少 1 名联机玩家才睡
```

即：**没有玩家在线时，ALOS 有意不睡觉**（避免无人时无限推进天数）。
配置里 `timeOfDayToSleep = 2200`、`allowSleepBeforeTimeOfDayToSleep = false`。

**解决**：ALOS 自带官方开关（`ModEntry.cs:98`）：

```csharp
helper.ConsoleCommands.Add("alos.debug_server",
    "Turns debug mode on/off, lets server run when no players are connected", ...);
```

打开后 `debug = true`，`numPlayers >= 1 || debug` 即成立 → 没人也自动作息 = 真无人值守。
管理器新增配置项 `native.alos_unattended`（默认 **开**）：开服（游戏端口 UDP 就绪、
世界已加载）后发送 `alos.debug_server`，并在任务日志中记录结果。
注意该命令只在世界已加载后生效，所以必须等端口起来再发。
关闭该开关时行为回到"需至少 1 名玩家在线才推进天数"。
---

## I-42：ALOS 无人值守开关"发了但没生效" —— 时序 + 开关语义 + 并发（0.2.18 修复）

**现象**（真机，0.2.17）：管理器日志显示
`[自动开服] → alos.debug_server（成功）`，但 ALOS 日志里**完全没有 `Server Debug On`**，
于是到 22:00 时 `numPlayers >= 1 || debug` 两边都不成立 → 房主不睡觉、天数不推进。

**证据**（SMAPI 日志时间线）：

```
22:35:43 setGameMode('playingGameMode (3)')   ← 世界正在载入
22:35:43 $>alos.debug_server                  ← 我们的命令（过早）
22:35:44 $>alos.debug_server                  ← 又一条
22:35:45 [ALOS] Auto Mode On!                 ← 世界才真正就绪
```

**三个原因**（缺一不可）：
1. **时序**：`alos.debug_server` → `DebugToggle` 里有 `if (Context.IsWorldReady)`，
   世界没加载完就发会**静默无效**。我们的命令比 `Auto Mode On!` 早约 2 秒。
2. **开关语义**：它是 **toggle** 而不是"设为开"（上游 `ModEntry.cs:160-170`）——
   发两次 = 开→关，等于没开。
3. **并发**：开服序列可能被并发跑两遍（用户连点 + 监管器自动恢复），
   导致 `set_multiplayermode` / `load_save` / `alos.debug_server` 各发两次。

**修复**（`orchestration.py`）：
* 新增 `alos_debug_state(runtime)`：从游戏控制台日志判断 `on/off/unknown`，
  **只统计最后一次 `Auto Mode On!` 之后**的内容（日志是追加的，有历史残留）；
* 新增 `alos_world_ready(runtime)`：以 ALOS 自己的 `Auto Mode On!` 为"世界已就绪"信号；
* 新增 `enable_alos_unattended(...)`：**等世界就绪 → 读日志确认状态 → 状态不是 on 才发
  → 再读日志确认**的收敛循环（最多 3 次），彻底避免"发早了"和"连发两次变成关"；
* `auto_host_load_save` 外层加 `_AUTO_HOST_LOCK` **串行化**（实现体移入
  `_auto_host_load_save_locked`），并发调用不再交错。

**真机实测（非破坏性）**：读到 `on` → 故意关掉（状态变 `off`）→ 调用新逻辑
**自动发一次并确认 `Server Debug On`**（返回 True）→ 再调一次**不再发送**（幂等，
不会把开着的关掉）→ 结束状态 `on`。当晚 22:00 房主自动入睡、存档写入（22:53:08）、
次日 `Tue. 2 / 6:20 am` 正常开始。

**测试**：`TestAlosUnattendedConverge`（6 项：unknown / 只看 Auto Mode 之后 / 已 on 不发 /
发一次并确认 / 反复失败后放弃 / 世界未就绪先等一下再发）+
`TestAutoHostSerialized`（锁存在 + 两线程并发不交错）。
另外把 90 秒的世界等待做成模块常量 `ALOS_WORLD_WAIT`/`ALOS_DEBUG_SETTLE`，
避免单测被拖慢（此前一次测试跑 133 秒）。

**诊断手法备查**：本机没有 `xwd`/`import`，`ffmpeg` 也没有 x11/xcb 抓屏；
用 ctypes 调 `XGetImage`（libX11，从**系统** `/usr/lib/x86_64-linux-gnu` 加载，
xstack 里的 X 工具缺 `libXxf86dga` 跑不起来）+ 手写 PNG（zlib）即可直接看游戏画面，
无需 VNC 密码。注意 `XImage` 用 `ctypes.Structure` 读出来是垃圾值，要用
`struct.unpack_from` 手工偏移（masks 在偏移 56，前面有 4 字节对齐填充）。
---

## I-43：已开服时又发了一次 `load_save` → 重置当天进度、客户端卡死（0.2.19 修复）

**现象**（真机）：为测试日志调用一次 `/api/server/start`（服务本就在运行），
结果游戏又收到 `set_multiplayermode 2` + `load_save` —— **在线玩家的当天进度被直接重置**，
客户端随后进入"等待其他玩家入睡"却永不结算的**卡死/失同步**状态。

**根因**：`task_server_start` 的"已在运行"分支会调 `_maybe_auto_host`，而它无条件重发载档指令。

**修复**：`auto_host_load_save` 在发指令**之前**先判断是否已开服（游戏端口 UDP 在听）：

```python
if udp_port_open(hosted_port):
    log("检测到已在开服状态…跳过自动载档以免打断在线玩家。")
    enable_alos_unattended(...)      # 只确保无人值守开关
    return "hosted"
```

**测试**：结构断言 + 运行时断言（已开服时 `sent` 里不得出现 `load_save` / `set_multiplayermode`）。

---

## I-44：`DATA_LIBRARY_PATH` 继承污染 → 应用日志写到别的应用目录（0.2.19 修复）

**现象**：某次用带外部环境的 shell 执行 `cmd/main start` 后，应用日志**再也不写**到
`@appdata/valleyserver/logs/`；排查耗时很久，最后发现它写到了
`@appdata/deepseek.harness/logs/manager.log`。

**根因**：`cmd/main` 是 `export DATA_LIBRARY_PATH="${DATA_LIBRARY_PATH:-$VAR}"` ——
**信任继承来的值**；而 Python 侧 `PKGVAR = Path(DATA_LIBRARY_PATH or TRIM_PKGVAR)` 直接采信。
继承环境里带着另一个应用的 `DATA_LIBRARY_PATH=/vol1/@appdata/deepseek.harness`，
于是数据/日志根整体被改到别的应用目录。

**修复**（两侧都堵）：
* `cmd/main`：只有当 `DATA_LIBRARY_PATH` 的末级目录与本应用一致时才采信，否则打印警告并忽略；
* `config.py`：新增 `_data_library_path()`，同样只采信"末级目录名 == 本应用"的值，否则回退 `TRIM_PKGVAR`。

**测试**：可疑值被忽略、本应用的值被采信。

---

## I-45：重启后 ALOS 无人值守开关偶发没打开 → 监管器自愈（0.2.19 修复）

**现象**：真机多次观察到 `server.restart` 之后 `alos.debug_server` 状态仍是 `unknown`
（即开关没打开），导致"没人联机时房主不睡、天数不推进"。手动执行同一条命令却能成功。

**修复**：不再只依赖启动/重启任务路径 —— 监管器 `_tick` 在游戏运行时**每分钟**
检查一次：世界已就绪（`Auto Mode On!`）且开关不是 `on` 就补开，日志写入 `manager.log`。
与"服务是被哪条路径拉起的"无关，属自愈。

**真机验证**：
```
[00:30:12] 监管器：ALOS 无人值守开关未开启，正在补开…
[00:30:16] [无人值守] → ALOS 无人值守已开启（日志确认 Server Debug On）
```

**测试**：`off` → 调用补开；`on` → 不动；世界未就绪 → 不动；并含节流（每分钟一次）。

---

## 备注：瞬时入睡模组（已按用户决定撤下，源码保留）

曾按"任一玩家入睡 → 主机立即入睡"实现并真机试跑（`fnos-fpk/addons/ValleyServer.InstantSleep/`，
C# 源码 + 本机 .NET SDK 编译成 DLL）。真机上主机确实会自动入睡，但**当天结算速度不稳定**，
用户最终决定**改回 ALOS 自带规则（游戏内 22:00 自动睡）**。

因此本版：**不安装、不随 FPK 分发**该模组，只把源码留在仓库里（`addons/` 位于 `fnpack/` 之外，
构建脚本不会打包它），日后需要可直接编译使用。

期间由该实验发现并已修复的问题：农舍位置名大小写（`FarmHouse`）、
`warpFarmer` 静默失败导致的重复调用、以及"已开服时重发 load_save"（I-43）。
