# 版本追溯与回滚能力：实测证据

> 结论摘要：原生部署下**没有发行摘要**可作为版本锚点，因此版本标识改用**内容指纹**：
> 游戏版本 + SMAPI 版本 + MOD 目录哈希 + 存档目录哈希 + 游戏本体目录信息。
> **回滚一律通过快照完成**（把游戏本体、MOD、存档与配置一起恢复到某个时点）。
> 本项目会**如实**报告能力的边界，不把不具备的能力包装成"支持"。

---

## 1. 什么被记录下来（内容指纹）

版本档案同时写入 SQLite（`versions` 表）与 JSON（`$TRIM_PKGHOME/manager/archives/version-*.json`）：

| 字段 | 来源 | 作用 |
| :--- | :--- | :--- |
| `game_version` | 优先取 SMAPI 控制台日志里 SMAPI 自己打印的版本；取不到则用 `strings` 从 `Stardew Valley.dll` 读 | 游戏本体版本 |
| `smapi_version` | 同上（`StardewModdingAPI.dll`） | SMAPI 版本 |
| `mods_hash` / `mod_count` | `hash_tree(DATA_ROOT/Mods)` | MOD 集合是否变化 |
| `saves_hash` / `save_count` | `hash_tree(DATA_ROOT/Saves)` | 存档集合是否变化 |
| `game_dir` | 游戏本体目录路径（旧字段 `image_ref` 已迁移到它） | 定位本体 |
| `image_size` / `image_created` | 游戏本体目录文件数/大小/修改时间 | 内容规模 |
| `recorded_at` | 记录时间 | 时间线 |
| `pinned` | 是否被标记为固定 | 保留策略保护 |
| `note` / `source` | 触发原因 | 审计 |

**为什么用哈希而不是版本号**：MOD 与存档没有可靠的版本号，而且"同名同版本内容不同"是常态。
内容哈希能回答"这次和上次到底一样不一样"。

## 2. 怎么读版本（实测两条路径）

1. **控制台日志**（最权威，SMAPI 自己打印）：

   ```text
   [SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356 on Unix 6.18.18.1107
   ```

   `versions.read_version_from_console()` 直接从这行解析。

2. **二进制字符串回退**：游戏未运行/日志被清空时，用
   `strings -a StardewValley.dll | grep -E '\b1\.\d+\.\d+\b'` 取版本；
   SMAPI 同理匹配 `4.x.x`。

> 上游基线的版本事实（游戏 1.6.15 / SMAPI 4.5.1）是在审计阶段从上游发行物中核实的，
> 取证过程与结论见 [audit-report.md](audit-report.md) §13 与 §16；本项目不使用上游发行物运行。

## 3. 回滚能力（如实报告）

`versions.rollback_capability()` 会返回：

```json
{
  "method": "snapshot",
  "can_rollback": true,
  "snapshot_count": 3,
  "latest_snapshot": { "id": "...", "created_at": 0, "valid": 1, "kind": "manual" },
  "missing": [],
  "notes": [
    "回滚通过**快照**完成：把游戏本体、MOD、存档与配置一起恢复到某个时点。",
    "「更新游戏」= 替换游戏本体文件。新版文件需要由你自行获取（本项目不再分发游戏本体）。",
    "SMAPI 随游戏本体一起替换，无法单独升降级。",
    "更新前必须创建快照；快照失败则更新会被中止。"
  ]
}
```

* 没有任何**通过校验**的快照 → `can_rollback = false`，并给出"请先创建快照"的可操作提示；
* 磁盘可用空间低于策略下限 → 同样判为不可回滚（避免回滚到一半空间不足）。

## 4. 更新与回滚的实际行为

| 场景 | 行为 |
| :--- | :--- |
| 替换游戏本体 | `native.install-gamefiles`：ZIP 安全检查 → 解压到暂存区 → 定位启动器 → 旧本体移入 `restore-tmp` → 原子换入 → 校验 → 失败自动换回旧本体 |
| 更新前 | 强制先创建完整快照；快照失败则**中止更新**（不会"先改了再说"） |
| 回滚 | `snapshot.restore`：校验快照完整性 → 恢复 → 失败保留现场 |
| 无可用快照时要求回滚 | 明确拒绝并说明原因，而不是静默失败或假装成功 |

## 5. 未验证 / 不提供的能力（明确清单）

| # | 项 | 说明 |
| :--- | :--- | :--- |
| N1 | 按**内容摘要**固定与回滚 | **不提供**：版本回滚统一走快照。若将来需要，应新增独立实现，而不是在本项目里伪造 |
| N2 | 单独升降级 SMAPI | **不提供**：SMAPI 随游戏本体一起替换 |
| N3 | 从上游直接下载新版本游戏 | **不提供**：游戏本体由用户自备，本项目不再分发 |
| N4 | 真实的多版本回滚演练 | 需要两份不同版本的游戏本体；本项目只做了逻辑与单测覆盖，未做真实双版本演练 |
| N5 | 长时间运行后的版本漂移 | 需要数天观测；未验证 |
