# 测试报告

> 环境：飞牛 OS 真机（`TRIM_SYS_VERSION=1.2.0701`，x86_64，btrfs）·
> Python 3.12.4（`python312`）· fnpack 1.2.4
> 被测版本：`valleyserver 0.2.4`（**原生部署**）
> 复现命令：`cd fnos-fpk && ./tools/run-tests.sh`

---

## 1. 总览

| 类别 | 组数 | 断言/用例数 | 结果 |
| :--- | ---: | ---: | :--- |
| Python 单元/集成测试（`tests/test_*.py`） | 8 | **189** | ✅ 全部通过 |
| 前端真实执行测试（`tools/test-frontend.js`） | 1 | **12** | ✅ 全部通过 |
| Shell 回归测试（日志轮转） | 1 | **8** | ✅ 全部通过 |
| Shell 回归测试（升级路径） | 1 | **16** | ✅ 全部通过 |
| 沙箱完整安装/卸载模拟（含缓存破坏与鉴权边界断言） | 1 | **43** | ✅ 全部通过 |
| **合计** | **12** | **268** | **✅ 全部通过** |

构建产物同时通过**双层 tar 结构校验**（外层 `manifest`+`cmd/`+`config/`+`wizard/`+`ICON*`+`app.tgz`；
内层 `server/*`、`www/*`、`ui/config`），并校验 `cmd/` 九个生命周期脚本的可执行位。

> **变更说明（0.2.0）**：容器方式已整体移除，`tests/test_compose.py`（29 例）与
> `tests/test_version_pinning.py`（13 例）随之删除；新增 `tests/test_native.py`（30 例）
> 覆盖原生运行环境，`tests/test_orchestration.py` 重写为原生语义（28 例）。

### 1.0 已发布产物（下载后重新校验）

| 项 | 值 |
| :--- | :--- |
| Release | [fpk-v0.2.0](https://github.com/youyoudezhuzhu/ValleyServer/releases/tag/fpk-v0.2.0) |
| 产物 | `valleyserver-0.2.0.fpk`（151572 字节） |
| sha256 | `5314f4cdb813dfb18bbcb444f7d002aa60cac3e5a90156d4acf29daba0c46910` |
| 构建 | GitHub Actions 从 tag 构建（`ubuntu-latest` + `python312` + 仓库内提交的 fnpack 1.2.4） |
| 重新下载校验 | ✅ 下载后 sha256 与 `build-info.txt` 记录一致 |
| 包内检查 | ✅ 原生模块齐全；容器相关模块**无残留**；包内**不含任何容器字样**（有硬性断言） |
| 沙箱安装/卸载 | ✅ 用**下载下来的**产物跑（不是本地构建） |
| 端到端原生启动 | ✅ 安装即自动预置显示栈；启动 3.2 s、就绪 19.4 s、5/5 进程、三层判据全绿、`[SMAPI] Mods loaded and ready!`、停止后 0 残留 |

> FPK 不是字节可复现构建（tar 内含时间戳），因此 CI 产物的 sha256 与本地构建不同属正常；
> 关键是**下载到的那一份**通过了上述校验。

### 1.1 各测试文件明细

| 文件 | 用例数 | 覆盖重点 |
| :--- | ---: | :--- |
| `tests/test_security.py` | 35 | 名称/路径校验、ZIP 攻击（穿越、绝对路径、盘符、反斜杠穿越、符号链接、FIFO、ZIP 炸弹、超量、超大）、命令注入、端口/环境变量/配置白名单校验、脱敏、原子写 |
| `tests/test_mods_saves.py` | 45 | 存档列表与元信息解析、导入预检（含 `Saves/` 包裹层兼容）、运行中拒绝写、覆盖确认、导出往返、删除二次确认与现场保留、MOD 大小写不敏感 manifest、依赖/重复/版本检查、启停幂等、卸载保留私有数据、整包安装、SMAPI 日志解析 |
| `tests/test_native.py` | 30 | 显示栈解析与捆绑优先、三个关键环境变量（`PYTHONPATH`/`XDG_DATA_DIRS`/`XKB_CONFIG_ROOT`）注入、`apt-get` 缺失时明确报错、运行后端工厂、旧版（容器时代）配置键安全忽略、端口/口令类型与范围校验（含 `bool` 拒绝）、端口占用归属（不误杀其它应用）、进程归属三级判定、PID 文件优先、MOD 目录并入与符号链接、存档参数校验、三层健康判据（未安装/启动中/显示栈掉线/图形不兼容/就绪）、控制台日志**原地**轮转（inode 不变） |
| `tests/test_orchestration.py` | 28 | 编排与后台监管的**直接**测试：快照失败即中止（含 MOD/存档/配置三条路径）、磁盘守卫中止、`skip_snapshot` 生效、安装前置校验（显示栈/游戏本体）、安装后记录版本档案、启停/重建、**断言原生模式不存在镜像式更新与回滚任务**、回滚能力如实走快照、版本档案记录内容指纹而非标签、NAS 重启自动恢复、重试上限 + 指数退避、用户主动停止后不自动拉起、监管关闭后不拉起 |
| `tests/test_snapshots.py` | 21 | 一致性协议（停服顺序）、manifest 版本与内容、MOD 私有数据入快照、停服失败即拒绝、磁盘守卫、篡改/缺失/未提交检测、**损坏快照拒绝恢复且原数据不变**、恢复回滚、保留策略计数与固定保护、manifest 迁移 |
| `tests/test_wizard.py` | 4 | 向导结构：控件类型必须来自平台实测集合、非 tips 控件必须有 field、tips 不得伪装成输入、radio/select 的 initValue 必须在 options 内、字段不得带 `TRIM_` 前缀或重复、**禁止用输入框承载纯说明**（回归：曾出现默认值「知道了」的假输入框） |
| `tests/test_tasks.py` | 18 | 状态机（成功/失败/异常/取消）、回滚成功与**回滚失败标记**、`skip_rollback`、未知类型拒绝、**两个高风险任务严格串行**、冲突检测、低风险不阻塞、崩溃残留标记 `interrupted`、查询与统计 |

---

## 2. 覆盖项目要求的逐条对照

| 项目要求（§9） | 类型 | 状态 | 证据 |
| :--- | :--- | :--- | :--- |
| 1. 全新安装与重复安装 | 自动化 | ✅ 通过 | `simulate-install.sh`（install_init/install_callback 跑两次验证幂等） |
| 2. 首次启动与停止（原生） | 自动化 + 真机 | ✅ 通过 | 沙箱安装后真实启动：3.5 s 拉起、15.6 s 达到「运行中」，5/5 进程在跑，三层判据全绿；停止后 0 残留（见 §3.2b） |
| 3. NAS 重启后的自动恢复 | 自动化 | ✅ 通过 | `test_orchestration.py::TestSupervisorRecovery`（含策略关闭、已运行两种不应拉起的场景） |
| 4. 游戏进程异常退出与重启次数限制 | 自动化 | ✅ 通过 | `test_orchestration.py::test_retry_limit_stops_recovery`（断言恰好重试 3 次后停手并写告警事件）+ `test_user_stop_prevents_auto_restart` + `test_disabled_supervisor_does_not_restart` |
| 5. 存档导入、导出、切换、恢复 | 自动化 | ✅ 通过 | `test_mods_saves.py`（12 个存档用例） |
| 6. MOD 安装、更新、禁用、卸载、重复安装 | 自动化 | ✅ 通过 | `test_mods_saves.py`（15 个 MOD 用例） |
| 7. 快照完整性校验和损坏快照拒绝恢复 | 自动化 | ✅ 通过 | `test_snapshots.py::test_restore_requires_valid_snapshot`（并断言原数据未被改动） |
| 8. 快照创建期间磁盘空间不足 | 自动化 | ✅ 通过 | `test_snapshots.py::test_disk_guard` |
| 9. 快照失败时原操作必须取消 | 自动化 | ✅ 通过 | `test_orchestration.py::TestSnapshotFailureCancelsOperation`（5 个用例：MOD 安装 / 存档删除 / 配置应用都被中止且目标未改动，磁盘守卫中止，`skip_snapshot` 生效） |
| 10. 更新失败后的回滚 | 自动化 | ✅ 通过（语义已变） | 原生部署**没有镜像回滚**，回滚 = 从快照恢复。已自动化验证：原生模式**不存在**镜像式更新/回滚任务（`TestNoImageSemantics`）；回滚能力如实走快照并披露限制。回滚执行路径由 `test_tasks.py::test_rollback_failure_is_recorded_and_scene_kept` 与快照恢复用例覆盖 |
| 11. 恢复失败时保留现场 | 自动化 | ✅ 通过 | `SnapshotStore.restore` 失败路径断言 + `test_tasks.py::test_rollback_failure_is_recorded_and_scene_kept` |
| 12. 同一时间发起两个高风险任务 | 自动化 | ✅ 通过 | `test_tasks.py::test_two_high_risk_tasks_serialize`（断言严格串行顺序）+ `test_conflict_detection` |
| 13. 恶意 ZIP、路径穿越、符号链接、异常文件 | 自动化 | ✅ 通过 | `test_security.py`（12 个 ZIP 用例） |
| 14. 错误密码、越权访问、命令注入、非法路径 | 自动化 | ✅ 通过 | `test_security.py` + `simulate-install.sh` 的认证断言 |
| 15. FPK 升级、卸载、重装及数据保留 | 自动化 | ✅ 通过 | `test-upgrade.sh` 场景 1/3；`simulate-install.sh` 卸载保留 |
| 16. SVE 和其他内容 MOD 的实际加载测试 | 真实运行 | ❌ 未验证 | 需要授权副本与目标机运行时间，见 §5 |
| 17. 无玩家在线时的自动运行、节日、过夜、弹窗、崩溃恢复 | 真实运行 | ❌ 未验证 | 需要长时间真实运行，见 §5 |

---

## 3. 关键断言的实际输出

### 3.1 沙箱完整安装/卸载（36 项，全绿）

```text
[1/9] cmd/* 执行位                          ✅ ×2
[2/9] install_init（前置校验）              ✅
[3/9] install_callback（幂等性：跑两次）    ✅ ×2 + 数据目录/配置(600)/任务库/初始口令 ✅ ×4
[4/9] cmd/main status（未启动应为 3）       ✅
[5/9] cmd/main start                        ✅ start 返回 0、启动后 status=0
[6/9] 健康检查 /api/health                  ✅ 返回 ok、报版本号、HOME 已覆盖、DATA_LIBRARY_PATH 已兜底
[7/9] 前端资源与网关前缀                    ✅ 首页/JS/CSS 200、/app/valleyserver/ 下同样 200、裸前缀 307
[8/9] 认证与接口                            ✅ 未登录 401、health 200、登录成功、Cookie+CSRF、错误口令 401
[9/9] 卸载（默认保留数据）                  ✅ 保留存档/MOD/快照/配置；选择删除后清除
```

### 3.2 升级路径（16 项，全绿）

```text
[准备]     旧版本启动 + /api/health 版本号 = 0.2.0
[场景 1]   upgrade_init 停服、端口释放、PID 删除
           upgrade_callback 重新拉起，/api/health 报告新版本 9.9.9
           → 直接证明「升级后功能没变」这一隐形故障不会发生
[场景 2]   代码缺失时 cmd/main start 返回非零（退出码未被掩盖）
           upgrade_callback 返回非零 + 失败原因写入 $TRIM_TEMP_LOGFILE
           代码恢复后再次 upgrade_callback 成功（可自愈）
[场景 3]   卸载后存档仍在（数据与生命周期分离）
```

### 3.2b 真机原生启动复核（从构建产物直接跑）

用 `dist/valleyserver-0.2.0.fpk` 解包后的运行后端，配真实游戏本体（238 个文件 + Content），
在飞牛 OS 真机上完整走了一遍「检测 → 校验 → 启动 → 就绪 → 停止」：

```text
检测: ready=True xstack_ok=True 启动器=True
校验: ok=True errors=[] warnings=[]
启动返回: ok=True 3.5s
就绪耗时: 15.6s
phase=运行中
processes={'xvfb': True, 'openbox': True, 'x11vnc': True, 'websockify': True, 'game': True}
layers={'L1_container_running': True, 'L1_display_stack': True,
        'L2_ports_listening': True, 'L3_smapi_loaded': True}
SMAPI 证据: [SMAPI] Mods loaded and ready!
停止: ok=True
停止后进程: False
```

说明：

* 显示栈由 `install_callback` 在**安装期**自动预置（实测解析依赖闭包 60 个包、
  下载 19 MiB、解包 60/60 成功），**不写系统包数据库**；
* 启动耗时 3.5 s、达到「运行中」（SMAPI 加载完成）15.6 s；
* 停止后无残留进程。

仍未完成的部分见 §5：**多人实际联机、ALOS 完整无人值守、SVE 加载**需要真实客户端与长时间运行。

### 3.3 日志轮转（8 项，全绿）

```text
✅ 未超限时文件内容不变
✅ 超限后文件被截断
✅ 保留了最新的尾部内容
✅ 被截断的首行已丢弃
✅ 轮转后 inode 不变
✅ 轮转后经原 fd 的写入仍落在同一文件
✅ 重复调用幂等
✅ 文件不存在时安全返回
```

---

### 3.4 本阶段新发现并修复的缺陷（都是真实逻辑问题，不是测试写错）

补充 `tests/test_orchestration.py` 时暴露出 4 个**生产代码**里的真实问题：

| # | 缺陷 | 后果 | 修复 |
| :--- | :--- | :--- | :--- |
| B1 | **快照校验拒绝合法的空 `Mods/`**：`_validate_payload` 要求每个包含目录都至少有一个文件 | 首次部署（上游文档明确说首次 `Mods/` 为空是正常的）**完全无法创建快照**，而快照是所有高风险操作的前置条件 → 新装用户一步都走不动 | 改为「源目录存在则快照里必须有同名目录」，只有**整体清单为空**才拒绝，并给出可操作的提示 |
| B2 | **未产生任何变更的失败也会走一遍回滚** | 例如「快照失败」这种什么都没做的失败，会被追加执行 `rollback_mod`，把状态从 `failed` 改写成 `rollback_failed`，**掩盖真实原因**，还会在日志里输出误导性的「回滚」信息 | `TaskContext` 新增 `_mutated` 标记；只有真正完成变更的处理器调用 `mark_mutated()` 之后，失败才触发回滚。未变更时记录「任务在产生任何变更之前失败，无需回滚」 |
| B3 | **回滚处理器调用了不存在的参数**：`SnapshotStore.restore(..., skip_pre_snapshot=True)`，但该方法没有这个参数 | 更新失败或恢复失败时，回滚一律抛 `TypeError` → 状态变 `rollback_failed`，**回滚能力实际不可用** | 去掉该参数；并补上「恢复前快照由任务层负责，store 本身不创建」的说明 |
| B4 | **回滚成功仍标记为 `failed`** | 数据其实已安全回到操作前状态，但界面/任务历史显示「失败」，无法区分「改坏了」和「已撤销」 | 回滚成功后终态改为 `rolled_back`（原始失败原因仍保留在 `error` 字段），界面新增该状态的中文标签 |

另外修正一处**回滚语义**：更新任务的 `old_digest` 原本记录得偏晚，改为在快照之后就写入任务结果 —— 否则后续任何一步失败时，回滚处理器都拿不到回滚目标。

### 3.4b 移除容器后端这一轮暴露出的缺陷（0.2.0）

改成原生单一后端时，重构过程又暴露出 5 个真实问题（都不是测试写错）：

| # | 缺陷 | 后果 | 修复 |
| :--- | :--- | :--- | :--- |
| D1 | **安装脚本仍引用已删除的 `cfg.container`**（`install_callback` 的内嵌 Python） | 安装时抛 `AttributeError` 被 `except` 吞掉 → **配置与初始口令都不生成**，用户装完不知道登录密码。沙箱测试一眼抓到 | 全部改为 `cfg.native.*`；并把「配置已生成」纳入沙箱断言 |
| D2 | **端口唯一性校验把两套后端配置混在一起比较** | `native` 与 `container` 默认端口本就重叠 → **原生配置永远无法应用**，报「端口配置存在重复」 | 只在当前生效后端内部比较 |
| D3 | **MOD 目录没被快照覆盖** | ALOS 把每存档状态写在 `Mods/<MOD>/data/<存档>.json`；MOD 留在游戏目录时这些状态**不进快照**，卸载/覆盖 MOD 会静默丢失 | 把游戏目录内的 `Mods` 符号链接到 `DATA_ROOT/Mods`，并把游戏目录里已有的 MOD **并入**数据区（不丢文件） |
| D4 | **`validate()` 对非法端口类型直接崩溃** | 传 `"abc"` 时 `int(port)` 抛 `ValueError`，本该返回「校验失败」却变成 500 | 先判类型再用；同时显式拒绝 `bool`（`int(True)` 是合法的，会漏检） |
| D5 | **管理器构造函数的运行时参数接错** | 重构时写成 `runtime if runtime is not None else runtime`（恒为 None），运行中状态判断静默失效 | 恢复双参数签名并统一属性名，由测试直接覆盖 |

这些都是「测试真的抓到了生产缺陷」的例子，因此也说明 §4 的变异测试不是形式主义。

### 3.4c 真机首装后暴露的缺陷（0.2.0 → 0.2.1）

用户真机装 0.2.0 后出现两个症状：**界面永远停在「正在检查登录状态…」**、
**安装向导还在提示要拉取容器镜像**。排查后确认这两个都是真实缺陷：

| # | 缺陷 | 后果 | 修复 |
| :--- | :--- | :--- | :--- |
| E1 | **`app.js` 末尾的启动段整段丢失**：重写「关于」页时把从该处到文件结尾的内容一起替换掉了，`window.addEventListener('hashchange')` 与 `document.addEventListener('DOMContentLoaded', …, await boot())` 都没了 | 浏览器里 `boot()` 从不执行 → 界面**永远停在「正在检查登录状态…」**。`node --check` 只看语法（文件合法），`curl` 只取字节（200 OK），两者都发现不了 | 恢复启动段；并新增**前端真实执行测试**（见下） |
| E2 | **`installXstack` / `uploadGameFiles` 两个函数同样被误删** | 「安装 / 检查显示组件」「上传游戏本体」按钮点了没反应 | 恢复函数；新测试会逐页执行渲染函数，引用不存在的函数立刻失败 |
| E3 | **安装向导仍问「是否立即拉取镜像（约 3.4 GiB）」**，卸载向导仍问「是否移除上游镜像」，`install_init` 仍警告缺容器运行时，`uninstall_init` 仍调用容器命令，`wizard_auto_start` 是**死控件**（没有任何代码读它） | 安装时提示拉取容器镜像 —— 与「原生部署」完全矛盾 | 向导改为原生措辞；删掉镜像选项与死控件，换成「安装后还需要做什么」说明步骤；`install_init` 改为原生环境预检；`uninstall_init` 改为按 PID 文件停本应用进程 |
| E4 | **认证绕过（安全）**：`X-Trim-Userid` 等网关头被**无条件信任**，而服务监听 `0.0.0.0:<panel_port>` | 局域网内任何人只要自己加一个 `X-Trim-Userid: 1` 请求头，就能**免密拿到完整管理员权限**。已实证复现（`/api/dashboard`、`/api/config` 均返回 200 与真实数据） | 只有经 **Unix 套接字**（即 `app/ui/config` 声明的 `gatewaySocket`，飞牛网关真实使用的通道）进来的请求才信任这些头；TCP 直连一律要求口令 + 会话 Cookie + CSRF |
| E5 | **路径归一化不一致**：鉴权按 `path.startswith("/api/")` 判断，而 HTTP 请求目标允许不带前导斜杠（`GET api/dashboard HTTP/1.1`） | 该形式返回 200 且绕过了鉴权判断（当时路由器没匹配上、未真正泄露数据，但"鉴权与路由各自解释路径"是隐患） | 统一归一化路径（补前导斜杠）后再做鉴权与路由，两种形式都返回 401 |

E1/E2 说明**必须有"真的把前端跑起来"的测试**，因此新增 `tools/test-frontend.js`：

* 起一个**真实后端**（临时沙箱目录 + 真实初始口令 + 真实登录），不是手写 mock；
* 用极小 DOM 替身把真实 `app.js` 在 `vm` 里执行，`fetch` 直接转发到真实后端；
* 断言：脚本可求值 / 注册了启动处理器 / `boot()` 真被调用 / 未登录渲染登录表单 /
  已登录渲染主界面 / **10 个页面全部渲染通过**（渲染函数引用不存在的变量会立刻失败）/
  经 Unix 套接字的网关场景直接进入主界面且隐藏「退出」按钮。

E6 | **静态资源缓存导致升级后可能继续加载旧界面**：`app.js`/`style.css` 用 `max-age=300`，而 index.html 里的引用没有版本号 | 升级后最多 5 分钟内浏览器仍可能用**旧的、正是卡在登录页的那份** app.js —— 用户会以为"重装没用" | index.html 改用 `static/app.js?v=<版本>` 引用并在服务时替换 `__APP_VERSION__`；index.html 设 `no-store`；带版本的资源可长缓存。已加断言：引用必须带版本号、页面不得残留占位符、首页必须 no-store |

E4/E5 由新增的 `tests/test_auth_gateway.py`（7 个用例）钉死：

### 3.4e 向导里的假控件（0.2.4）

安装向导最后一步原本是：

```
【安装后还需要做什么】
  type=text  field=wizard_after_install_note
  label='说明（可直接使用默认值）'  initValue='知道了'
```

也就是说 —— 用户安装时会看到一个**要他填「知道了」的输入框**。这是把纯说明硬做成输入控件凑步骤。

处理：

1. 查出 fnOS 向导本身有 **`tips` 类型**（本机 50 个已装应用里出现 24 次，且最小写法只要
   `{"type":"tips","helpText":"…"}`，`helpText` 支持 HTML）→ 纯说明改用 `tips`；
2. 安装向导补了一个「开始之前」的 `tips`（说明这是原生部署、装完还要做哪两步、本应用不分发什么）；
3. 新增 `tests/test_wizard.py`（4 个用例）钉死规则：控件类型必须在平台实测集合内、
   非 tips 控件必须有 `field`、tips 不得带 `field`/`initValue`/`rules`、
   radio/select 的 `initValue` 必须在 `options` 内、字段不得带 `TRIM_` 前缀或重复、
   **禁止用输入框承载说明文字**（含 `知道了`/`好的`/`已阅读` 这类凑数默认值的黑名单）。

### 3.4d 彻底清除容器残留（0.2.3）

按需求"把包内涉及容器运行时的东西全部删掉"，这一轮把**产品里所有容器字样的源头**都清掉了，
而不只是改成"不使用容器"这种对冲说法：

| # | 残留位置 | 处理 |
| :--- | :--- | :--- |
| G1 | `manifest.desc` 仍写"容器作为可选备选模式"（应用中心可见） | 改为纯原生描述 |
| G2 | 后端 docstring/注释/日志/接口文案里 15 处"不使用容器" | 全部删除或改为中性说法（"运行方式：原生"） |
| G3 | 前端「运行环境」标签、系统页"部署方式" | 改为「原生部署」 |
| G4 | 向导 helpText（3 处） | 删除 |
| G5 | `install_init` 的容器预检注释 | 改为原生环境预检 |
| G6 | **管理器的 `compose` 兼容参数与 `self.compose` 别名**（Docker 时代遗留） | 全部移除，统一为 `runtime` |
| G7 | **前端「版本」页整页仍停留在已删除的镜像固定逻辑**：显示"镜像标签/镜像摘要"、按钮调用 `versions/pin`、`versions/update` —— 这些接口**已不存在**，点了就报错 | 整页重写为「内容指纹 + 快照回滚 + 能力边界」；后端补齐 `versions/updates`（`check` 保留為别名）与 `versions/record`（新增 `version.record` 任务） |
| G8 | 快照表/仪表盘里的"镜像摘要"列 | 改为 MOD 指纹 / 游戏本体目录 |
| G9 | 内部字段名 `L1_container_running` | 改为 `L1_game_process`（与界面标签一致） |

新增**硬性断言**（`tools/run-tests.sh`）：源码与构建产物中 `docker`/`compose` 出现次数必须为 **0**，
`cmd/`、`wizard/`、`manifest` 同样为 0；`compose_mgr.py` / `docker_runtime.py` 不得出现在包内。
这样以后任何人再把容器字样写回产品里，CI 会直接失败。

> 说明：`tools/` 与 `tests/` 里仍会出现该词 —— 那是**断言"它不得出现"的检查代码本身**
> （不写出这个词就无法断言其不存在），且这两个目录不随 FPK 发布。
TCP 伪造网关头（4 种变体 + 写接口 + 无前导斜杠）必须 401 且不泄露数据；
UDS + 网关头必须认证成功（**别把网关修坏**）；UDS 无头必须 401；本地口令登录仍可用。


---

## 4. 测试自身的可靠性（变异测试）

**只跑绿不算修好**。为确认断言真的会红，对每个关键断言故意注入了错误：

| 注入的错误 | 预期变红的断言 | 实测 |
| :--- | :--- | :--- |
| 把 `cat "$tmp" > "$file"` 改成 `mv "$tmp" "$file"` | 「轮转后 inode 不变」「经原 fd 的写入仍落在同一文件」 | ✅ 2 条变红 |
| 把 `start) start_process; exit $?` 改成 `exit 0` | 「代码缺失时 cmd/main start 返回非零」「upgrade_callback 返回非零」 | ✅ 2 条变红 |
| 让 `inspect_zip` 只把异常压缩比记为 `suspicious` 而不拒绝 | 「ZIP 炸弹被拒绝」 | ✅ 变红（随后改为直接拒绝） |
| 让 `_validate_size` / `_validate_cpu` 静默 `strip()` 用户输入 | 「拒绝含首尾空白的大小/CPU」 | ✅ 变红（随后改为显式拒绝） |

---

## 5. 未验证项（明确清单，不冒充已验证）

### 5.1 环境限制导致未能完成的

| # | 未验证内容 | 原因 | 已有的间接证据 |
| :--- | :--- | :--- | :--- |
| U1 | 游戏在无头 Xvfb 下**完整启动并进入主菜单/加载存档** | 本 NAS 出现 btrfs 元数据保留区导致的假 ENOSPC（磁盘 `df` 显示 385 GB 可用） | 已实测到 **SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356 初始化成功**（容器内真实输出） |
| U2 | ~~拉取上游发行物~~（已不作为运行路径） | 本环境的容器拉取在层解包阶段阻塞在 futex | 改用 registry API 直接下载全部 10 层并组装成可载入的镜像 tar，**config digest 与 registry 完全一致**，并用它完成了 U1 的实测 |
| U3 | VNC / WebVNC 画面连通性 | 依赖 U1 完成 | entrypoint 已确认启动 Xvfb 与 x11vnc/websockify |
| U4 | ~~真实镜像回滚~~ | 容器路径已移除 | 不适用；原生回滚 = 快照恢复 |
| U5 | 在真正的飞牛应用中心完成安装/升级/卸载 | 本环境未获授权在用户 NAS 上安装 | 沙箱模拟已覆盖全部 9 个生命周期脚本的调用路径与退出码语义 |

### 5.2 必须由真实游戏与目标机验证的

| # | 未验证内容 | 说明 |
| :--- | :--- | :--- |
| U6 | SVE 及其他内容 MOD 的实际加载 | 需要授权副本；管理器只做结构/依赖检查，**兼容性提示不能代替实测** |
| U7 | 无玩家在线时的长期无人值守（节日、过夜、弹窗、跨季） | 需要数天到数周的连续运行 |
| U8 | 在线玩家列表 | 上游无可靠接口，**首版明确不支持**，界面不展示推测数据 |
| U9 | 多人联机与端到端延迟 | 需要真实客户端 |
| U10 | FnConnect 远程访问下的网关注入 Header | 需要远程访问实测；本地网关路径已验证 200/307 |

> 以上任一项在获得真实运行证据前，**不会**在界面或文档中标注为「已通过」。

---

## 6. 如何复现

```bash
cd fnos-fpk
chmod +x tools/*.sh

# 全部自动化测试（约 40 秒；含沙箱安装/卸载与升级路径）
./tools/run-tests.sh

# 只跑某个文件
python3 tests/test_security.py -v

# 单独构建 + 沙箱安装
STAGE_ROOT=/tmp/fpk-build ./tools/build-fpk.sh
./tools/simulate-install.sh dist/valleyserver-0.2.0.fpk 18842

# 升级路径（端口自动选取，避免打到线上实例）
./tools/test-upgrade.sh dist/valleyserver-0.2.0.fpk
```

测试自带安全护栏：

* `_bootstrap.isolated_env()` 把 `TRIM_*` 全部重定向到 `mktemp` 沙箱，
  并**清除** fnOS 真实注入的 `DATA_LIBRARY_PATH`，用于验证代码的兜底逻辑；
* `simulate-install.sh` 与 `test-upgrade.sh` 在开头**断言**所有 `TRIM_*` 都落在沙箱内，
  否则立即停手（本机就是 fnOS 真机，真实路径确实存在）；
* 端口一律动态选取或使用专用测试端口，**绝不硬编码**（本机 18801 被真实应用占用）。
