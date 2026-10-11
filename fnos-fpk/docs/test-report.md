# 测试报告

> 环境：飞牛 OS 真机（`TRIM_SYS_VERSION=1.2.0701`，x86_64，btrfs）·
> Python 3.12.4（`python312`）· Docker Engine 28.5.2 · Docker Compose v2.40.3 · fnpack 1.2.4
> 被测版本：`valleyserver 0.1.0`
> 复现命令：`cd fnos-fpk && ./tools/run-tests.sh`

---

## 1. 总览

| 类别 | 组数 | 断言/用例数 | 结果 |
| :--- | ---: | ---: | :--- |
| Python 单元/集成测试（`tests/test_*.py`） | 7 | **191** | ✅ 全部通过 |
| Shell 回归测试（日志轮转） | 1 | **8** | ✅ 全部通过 |
| Shell 回归测试（升级路径） | 1 | **16** | ✅ 全部通过 |
| 沙箱完整安装/卸载模拟 | 1 | **36** | ✅ 全部通过 |
| **合计** | **10** | **251** | **✅ 全部通过** |

构建产物同时通过**双层 tar 结构校验**（外层 `manifest`+`cmd/`+`config/`+`wizard/`+`ICON*`+`app.tgz`；
内层 `server/*`、`www/*`、`ui/config`）与 **`docker compose config -q` 严格校验**。

### 1.1 各测试文件明细

| 文件 | 用例数 | 覆盖重点 |
| :--- | ---: | :--- |
| `tests/test_security.py` | 35 | 名称/路径校验、ZIP 攻击（穿越、绝对路径、盘符、反斜杠穿越、符号链接、FIFO、ZIP 炸弹、超量、超大）、命令注入、端口/镜像/环境变量校验、脱敏、原子写 |
| `tests/test_mods_saves.py` | 45 | 存档列表与元信息解析、导入预检（含 `Saves/` 包裹层兼容）、运行中拒绝写、覆盖确认、导出往返、删除二次确认与现场保留、MOD 大小写不敏感 manifest、依赖/重复/版本检查、启停幂等、卸载保留私有数据、整包安装、SMAPI 日志解析 |
| `tests/test_compose.py` | 29 | YAML 标量转义（防注入）、端口/IPv6/大小/CPU/重启策略/口令/镜像/摘要校验、挂载路径必须绝对且落在数据目录、不得挂载 Docker Socket、健康检查存在、entrypoint `bash -n`、口令来自环境变量、原地日志轮转、`docker compose config -q` 真校验、Docker 检测与安装指引 |
| `tests/test_orchestration.py` | 30 | 编排与后台监管的**直接**测试：快照失败即中止（且不误走回滚）、磁盘守卫中止、更新失败回滚与「回滚不可行时如实报错」、摘要不符中止、NAS 重启自动恢复、重试上限 + 指数退避、用户主动停止后不自动拉起 |
| `tests/test_snapshots.py` | 21 | 一致性协议（停服顺序）、manifest 版本与内容、MOD 私有数据入快照、停服失败即拒绝、磁盘守卫、篡改/缺失/未提交检测、**损坏快照拒绝恢复且原数据不变**、恢复回滚、保留策略计数与固定保护、manifest 迁移 |
| `tests/test_tasks.py` | 18 | 状态机（成功/失败/异常/取消）、回滚成功与**回滚失败标记**、`skip_rollback`、未知类型拒绝、**两个高风险任务严格串行**、冲突检测、低风险不阻塞、崩溃残留标记 `interrupted`、查询与统计 |
| `tests/test_version_pinning.py` | 13 | 默认按摘要固定、compose 使用摘要而非标签、**联网核对 registry 摘要与审计记录一致**、按摘要查询 manifest 返回 200、回滚能力**如实报告不具备**、限制披露、版本档案记录实际内容、固定切换 |

---

## 2. 覆盖项目要求的逐条对照

| 项目要求（§9） | 类型 | 状态 | 证据 |
| :--- | :--- | :--- | :--- |
| 1. 全新安装与重复安装 | 自动化 | ✅ 通过 | `simulate-install.sh`（install_init/install_callback 跑两次验证幂等） |
| 2. 首次拉取镜像、启动和停止 | 半自动 | ⚠️ 部分 | 容器启停逻辑与健康判据已测；镜像拉取受本环境 `docker pull` 阻塞与 btrfs 假 ENOSPC 限制，见 §5 |
| 3. NAS 重启后的自动恢复 | 自动化 | ✅ 通过 | `test_orchestration.py::test_boot_recovery_starts_container`（含策略关闭/已运行/未安装三种不应拉起的场景） |
| 4. 容器异常退出与重启次数限制 | 自动化 | ✅ 通过 | `test_orchestration.py::test_retry_limit_stops_recovery`（断言恰好重试 3 次后停手并写告警事件）+ `test_backoff_grows`（退避未到不重试） |
| 5. 存档导入、导出、切换、恢复 | 自动化 | ✅ 通过 | `test_mods_saves.py`（12 个存档用例） |
| 6. MOD 安装、更新、禁用、卸载、重复安装 | 自动化 | ✅ 通过 | `test_mods_saves.py`（15 个 MOD 用例） |
| 7. 快照完整性校验和损坏快照拒绝恢复 | 自动化 | ✅ 通过 | `test_snapshots.py::test_restore_requires_valid_snapshot`（并断言原数据未被改动） |
| 8. 快照创建期间磁盘空间不足 | 自动化 | ✅ 通过 | `test_snapshots.py::test_disk_guard` |
| 9. 快照失败时原操作必须取消 | 自动化 | ✅ 通过 | `test_orchestration.py::TestSnapshotFailureCancelsOperation`（4 个用例：MOD 安装 / 存档删除 / 配置应用都被中止，且目标状态未被改动） |
| 10. 更新失败后的回滚 | 自动化 | ✅ 通过 | `test_orchestration.py::TestUpdateFailureRollback`（回滚成功→`rolled_back` 且摘要复原；旧镜像不可得→`rollback_failed` 并保留现场；早期失败不误触发回滚）；**真实镜像回滚未实测**（见 §5） |
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
[准备]     旧版本启动 + /api/health 版本号 = 0.1.0
[场景 1]   upgrade_init 停服、端口释放、PID 删除
           upgrade_callback 重新拉起，/api/health 报告新版本 9.9.9
           → 直接证明「升级后功能没变」这一隐形故障不会发生
[场景 2]   代码缺失时 cmd/main start 返回非零（退出码未被掩盖）
           upgrade_callback 返回非零 + 失败原因写入 $TRIM_TEMP_LOGFILE
           代码恢复后再次 upgrade_callback 成功（可自愈）
[场景 3]   卸载后存档仍在（数据与生命周期分离）
```

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

另外修正一处**回滚语义**：`server.update` 的 `old_digest` 原本记录得偏晚，改为在快照之后就写入任务结果 —— 否则后续任何一步失败时，回滚处理器都拿不到回滚目标。


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
| U2 | `docker pull` 拉取上游镜像 | 本环境的 Docker 拉取在层解包阶段阻塞在 futex | 改用 registry API 直接下载全部 10 层并组装成镜像 tar，`docker load` 后 **config digest 与 registry 完全一致**，并用它完成了 U1 的实测 |
| U3 | VNC / WebVNC 画面连通性 | 依赖 U1 完成 | entrypoint 已确认启动 Xvfb 与 x11vnc/websockify |
| U4 | 更新失败后的**真实镜像**回滚 | 需要两个可拉取的镜像摘要；上游只有一个 tag | 回滚逻辑与「不具备条件时如实报告」已自动化测试 |
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

# 全部自动化测试（约 30 秒；联网会额外核对 registry 摘要）
./tools/run-tests.sh

# 只跑某个文件
python3 tests/test_security.py -v

# 单独构建 + 沙箱安装
STAGE_ROOT=/tmp/fpk-build ./tools/build-fpk.sh
./tools/simulate-install.sh dist/valleyserver-0.1.0.fpk 18842

# 升级路径（端口自动选取，避免打到线上实例）
./tools/test-upgrade.sh dist/valleyserver-0.1.0.fpk
```

测试自带安全护栏：

* `_bootstrap.isolated_env()` 把 `TRIM_*` 全部重定向到 `mktemp` 沙箱，
  并**清除** fnOS 真实注入的 `DATA_LIBRARY_PATH`，用于验证代码的兜底逻辑；
* `simulate-install.sh` 与 `test-upgrade.sh` 在开头**断言**所有 `TRIM_*` 都落在沙箱内，
  否则立即停手（本机就是 fnOS 真机，真实路径确实存在）；
* 端口一律动态选取或使用专用测试端口，**绝不硬编码**（本机 18801 被真实应用占用）。
