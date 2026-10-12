# 已知问题与待办（issue log）

> 记录人：Development Lead · 最近更新：2026-10-12
> 状态约定：`待修` / `修复中` / `已修（未发布）` / `已修（已发布）` / `不修（说明原因）`

本文档记录**已确认存在、但尚未（或尚未完整）处理**的问题。每条都包含：现象、根因、
实证方式、影响面、计划。凡未经实证的猜测一律标注为「未验证」。

---

## 状态总览

| 编号 | 问题 | 严重度 | 状态 |
| :--- | :--- | :--- | :--- |
| I-1 | MOD 包下载 / MOD 诊断接口被通配路由遮蔽 | **高**（功能不可用） | 待修 |
| I-2 | WebVNC 链接硬编码 `127.0.0.1`，远程（fnconnect）打不开 | **高**（功能不可用） | 待修 |
| I-3 | 当前游戏本体的来源与合规性 | **高**（合规） | 待用户决策 |
| I-4 | `_host_hint()` 兜底返回占位符 `<NAS-IP>` | 低 | 待修 |
| I-5 | `x11vnc.pid` 因 `-bg` 守护化而陈旧 | 低 | 待修 |
| I-6 | `native.install-smapi` 覆盖运行中的可执行文件（ETXTBSY） | **高**（功能不可用） | 已修（未发布） |
| I-7 | 「在线玩家」纯占位卡片（只有"暂不支持"） | 低（无用 UI） | 已修 |
| I-8 | `confirm_destructive` / `metrics_interval_seconds` 是死设置 | 中（开关无效果） | 已修 |
| I-9 | Steam 扫码取游戏本体的长任务：取消无效、二维码拿不到 | **高**（新功能不可用） | 已修 |

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
