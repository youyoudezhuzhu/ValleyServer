# 字段迁移说明（容器时代命名 → 原生语义）

> 适用范围：从 **≤0.2.12** 升级到 **0.2.13+** 的用户数据。
> 迁移是**自动**的：管理器打开数据库时执行，幂等、可重复。

## 1. 为什么迁移

早期版本按"容器 + 镜像"建模，字段名沿用了 `image_ref` / `image_digest`。
Docker 已彻底移除，这些名字既误导使用者，也让人误以为存在"镜像级回滚"。

实测确认了各字段的**真实语义**（不是猜测）：

| 旧字段 | 实际存的内容 | 处理 |
| :--- | :--- | :--- |
| `image_ref` | **游戏本体目录路径**（如 `/vol1/@apphome/valleyserver/data/gamefiles/Stardew Valley`） | 原样迁移到 **`game_dir`** |
| `image_digest` | **恒为空字符串**（原生模式无摘要，也没有任何赋值逻辑） | **不迁移**；不再读写，旧列保留为历史物证 |
| `image_created` | 游戏本体目录的修改时间 | 迁移到 **`game_dir_mtime`** |
| `image_size` | 游戏本体目录大小 | 迁移到 **`game_dir_size`** |
| `ContainerStatus.image` | `"native:<游戏目录>"` 伪标识 | 改为 **`game_dir`**（去掉前缀） |

### 为什么没有 `content_hash`

需求规定"只有确认 `image_ref` 的值确实是内容指纹时，才能迁移为 `content_hash`"。
实测：`image_ref` 存的是**路径**，`image_digest` 是**空值** —— 都不是指纹。
因此**不做** `content_hash` 迁移。真实的指纹字段本来就有：`mods_hash` / `saves_hash`。

## 2. 迁移覆盖的存储

| # | 存储 | 迁移方式 |
| :-- | :--- | :--- |
| 1 | SQLite `versions` 表 | 加 `game_dir` 列 → 从 `image_ref` 回填 → **必要时重建表**（见下） |
| 2 | SQLite `snapshots` 表 | 同上 |
| 3 | `data/archives/version-*.json` | **不重写历史文件**；读取时新键优先、旧键回退；新写入只写新键 |
| 4 | `data/snapshots/<id>/manifest.json` | 同上 |
| 5 | `manager/config.json` | **无需迁移**（段名本来就是 `native`） |

### 为什么需要"重建表"

老库里 `versions.image_ref` 是 `TEXT NOT NULL` **且没有默认值**，而新代码不再写这一列 ——
若不处理，升级后**任何新插入都会失败**：

```
sqlite3.IntegrityError: NOT NULL constraint failed: versions.image_ref
```

（这是迁移测试真实抓到的，不是推测。）SQLite 无法给已有列补默认值，因此采用标准做法：
建新表 → 搬数据 → 换名 → 重建索引。重建后旧列**保留**但放宽为 `DEFAULT ''`，
历史记录继续可查（审计要求：不破坏历史记录），代码不再读写它们。

## 3. 迁移的安全性

| 保证 | 说明 |
| :--- | :--- |
| **幂等** | 只在 `game_dir` 为空时回填；重复执行不会覆盖已迁移的值（有专门测试） |
| **不破坏审计** | 旧列与历史 JSON 都保留，不删除、不重写 |
| **不触碰数据内容** | 只动元数据列/键；存档、MOD、快照 payload 及其 SHA-256 一律不变（有专门测试） |
| **不猜测** | 语义不明或为空的数据不迁移、不丢弃 |
| **快照仍可校验** | `verify()` 只比对 `manifest["files"]` 里的 payload 文件，**manifest 自身不参与校验**，因此新增/改名键不会让历史快照失效 |
| **版本保护** | 快照 manifest 已有 `manifest_version` 机制，遇到更高版本会**拒绝按低版本语义解读** |

## 4. 升级后会发生什么

1. 首次打开管理器面板时，数据库自动完成迁移（毫秒级，失败会中止打开而不是写坏数据）；
2. 「版本」页的"游戏本体目录"显示的是 `game_dir`（原来显示为"镜像引用"）；
3. 「控制台」/「系统」页的日志来源名称由 `container` 改为 `runtime`
   （**磁盘文件名 `container.log` 保持不变**，避免旧日志成为孤儿）；
4. API 中的 `container` 字段改为 `runtime`；旧参数值 `container` 仍被接受（兼容）。

## 5. 回滚到旧版本？

**不建议，也没有自动化回滚**：旧版本代码读不了新列（会显示空值），但**不会损坏数据**。
如需回退，请先备份 `$TRIM_PKGHOME`（含 `manager/*.db` 与 `data/archives`）。
