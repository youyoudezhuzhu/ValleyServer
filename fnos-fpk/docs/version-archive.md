# 版本固定与回滚能力：实测证据

> 结论摘要：**游戏本体的版本固定与回滚在技术上可行**，因为游戏本体与 SMAPI 被烘焙在
> Docker 镜像里，镜像摘要（digest）就是不可变锚点。
> 但上游 Docker Hub **只发布了单一标签**，因此「回滚到上游历史版本」**不可行** ——
> 只能回到本地已保留的镜像摘要。本项目会**如实**报告这一点。

---

## 1. 为什么摘要就是版本锚点（已用镜像层证据确认）

审计阶段做了两件事（详见 `docs/audit-report.md` §5.1 与 §13）：

1. **确认游戏文件在构建期被 `COPY` 进镜像**：镜像 `history` 中有
   `COPY content-game /content`，而运行期入口脚本 `startapp.sh`（全文 15 行）
   **没有任何下载逻辑**，也没有任何写游戏目录的操作。
2. **直接提取镜像层验证**：把 10 个层从 registry 下载后解包最终层，得到
   `/content/Stardew Valley/` 共 **4190 个文件、783 MB**，其中
   `Stardew Valley.dll` 内为游戏 **1.6.15**、`StardewModdingAPI.dll` 内为 SMAPI **4.5.1**。

**因此**：同一 `image@sha256:<digest>` 永远对应同一份游戏本体与 SMAPI。
反过来，`repo:latest` 或任何 tag 都可能在内容变化后仍指向同一名字。

## 2. 本项目如何做到固定

| 机制 | 实现 |
| :--- | :--- |
| 默认即固定 | `ContainerConfig.pin_digest = True`，`image_digest` 预置审计到的摘要 |
| compose 用摘要 | 渲染时生成 `lixeer/valley-server@sha256:d84f9241…`，**不出现** `:v26.5` |
| 拉取用摘要 | `docker pull lixeer/valley-server@sha256:d84f9241…` |
| 校验实际摘要 | 拉取后用 `docker image inspect --format '{{index .RepoDigests 0}}'` 回读并与目标比对；不一致则**中止更新** |
| 档案记录内容 | `versions` 表 + `archives/version-*.json` 记录 `image_ref / image_digest / game_version / smapi_version / mods_hash / saves_hash / recorded_at / pinned` |
| 快照携带版本 | 快照 manifest 内记录 `image_digest`、`game_version`、`smapi_version`、`mods`（含每个 MOD 的 `tree_hash`） |

## 3. 实测 1：代码常量与 compose 渲染

运行 `tests/test_version_pinning.py`（13 个用例，全绿），其中：

```text
test_default_digest_format        ✅ 摘要形如 sha256:<64 hex>
test_config_defaults_pin_digest   ✅ 默认 pin_digest=True、image_digest=审计值
test_compose_uses_digest_not_tag  ✅ 渲染结果含 repo@sha256:…，且不含 repo:v26.5
test_image_ref_helper             ✅ pin 模式返回 repo@digest；tag 模式返回 repo:tag
test_image_ref_with_custom_digest ✅ 非 sha256:<64hex> 的摘要被拒绝
```

## 4. 实测 2：与上游 registry 的一致性核对（联网）

`test_version_pinning.py::TestRegistryDigestMatches` 直接查 Docker Registry：

```text
test_registry_digest_matches_audit
  → 从 https://registry-1.docker.io/v2/lixeer/valley-server/manifests/v26.5 取回 manifest，
    读 Docker-Content-Digest 响应头并计算 sha256(manifest 字节)
  → 断言 == sha256:d84f924108b0fed5c459f5276a56bbb47fe450c0de2db807a55990adfd58e1b5
     （即本项目管理器中记录的值）
  ✅ 通过：审计记录的摘要与上游当前一致，且 manifest 含 config + 非空 layers

test_digest_reference_is_accepted
  → 用 `GET /v2/lixeer/valley-server/manifests/sha256:d84f9241…` 按**摘要**查询
  ✅ HTTP 200：证明「按摘要引用」这种回滚所依赖的形式能被 registry 接受
```

> 该测试若上游覆盖了 tag 内容，会立刻变红并给出「审计记录与上游当前不一致」的说明 ——
> 这正是必须按摘要固定的原因。

## 5. 实测 3：把镜像真的载入并运行

`docker pull` 在本环境解包阶段阻塞，因此改用 registry API 直接下载 10 个层
（3.3 GB，全部校验通过）并组装成 `docker load` 可接受的镜像 tar：

```text
docker load -i valleyserver-image.tar
  → Loaded image: lixeer/valley-server:v26.5

docker image inspect lixeer/valley-server:v26.5 --format '{{.Id}}'
  → sha256:bf38c16d993f626af9367679d27e9997038f20525e910b6b9a99db4ced30cb79
     ↑ 与 registry 上的 config digest 逐字节一致 ⇒ 组装镜像与上游等价
```

随后用**管理器生成的 entrypoint** 跑真实容器，容器内 SMAPI 自己报出：

```text
[SMAPI] SMAPI 4.5.1 with Stardew Valley 1.6.15 build 24356 on Unix 6.18.18.1107
```

这把「游戏 1.6.15 / SMAPI 4.5.1」从字符串推断升级为**确定性事实**。

## 6. 实测 4：回滚能力必须如实报告

`VersionManager.rollback_capability()` 的回答是**结构化的**，不是一句「支持回滚」：

```json
{
  "can_rollback": false,
  "target_digest": "sha256:…",
  "image_present": false,
  "missing": ["本地没有镜像 lixeer/valley-server@sha256:…；需要重新拉取（上游可能已删除该摘要）。"],
  "notes": [
    "上游 Docker Hub 仅发布了单一镜像标签（v26.5），因此**不存在**来自上游的历史版本可供回滚；回滚只能回到本地已保留的镜像摘要。",
    "SMAPI 与游戏本体被烘焙在同一镜像内，无法单独升级或降级 SMAPI。",
    "管理器的回滚 = 旧镜像 + 配置 + MOD + 存档四者一起恢复；若旧镜像已被本地删除，则只能恢复配置/MOD/存档，不能恢复游戏本体。"
  ]
}
```

对应的自动化断言：

```text
test_no_compose_reports_missing          ✅ 不具备条件时 can_rollback=False 且 missing 非空
test_unknown_target_digest_reports_missing ✅ 本地无该镜像时明确写出「本地没有镜像 …」
test_limitations_disclosed               ✅ 三条限制都会出现在输出里
```

界面上「版本」页在无法回滚时**不会提供**回滚按钮，而是弹出说明列出缺少什么；
`api/versions/rollback` 也会返回 409 并附上原因，**不会伪造「已回滚」**。

## 7. 更新与回滚的执行顺序（失败也要可恢复）

```text
更新（server.update）
  1. 磁盘空间检查 + 回滚能力评估（把结论写进任务日志）
  2. 创建完整快照（失败 → 直接中止，不做任何变更）
  3. 拉取目标镜像（按摘要）
  4. 回读实际摘要并与目标比对（不一致 → 中止）
  5. 写配置 → 重建容器
  6. 健康检查 + 存档可读性检查
  7. 记录新版本档案
若第 5–6 步失败：
  rollback_server_update →
    保留新版本现场与故障日志 → 拉取/确认旧摘要存在 → 切回旧摘要 → 重建 → 健康检查
    →（默认）再从第 2 步的快照恢复 Saves/Mods
  若旧镜像本地不存在且拉取失败 → 明确抛错「无法回滚镜像，故障现场已保留」，不谎报成功
```

## 8. 能力边界（必须一起告诉用户）

| 能做 | 不能做 |
| :--- | :--- |
| 固定当前镜像摘要，之后内容不可变 | 回滚到上游历史版本（上游只有一个 tag） |
| 回滚到**本地已保留**的镜像摘要 | 单独升级/降级 SMAPI（它与游戏同镜像） |
| 连同配置、MOD、存档一起恢复到某个快照时点 | 在旧镜像已被删除的情况下恢复游戏本体 |
| 检查上游 tag 内容是否被重新推送（digest 变化即告警） | 下载「上一个官方版本」（上游不存在） |
