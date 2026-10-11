"""诊断与兼容性提示 + 诊断包导出。

原则：
  * 区分「确定性错误」（来自日志/命令的明确输出）、「规则推断」（按已知规则判断）
    与「无法确认」（列出来但不给结论）。
  * 一键诊断包**导出前必须脱敏**（密码、令牌、AccessToken、VNC 密码、Authorization 头等）。
  * 兼容性提示**不能代替实际测试**，界面必须标注这一点。
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config as C
from .util import dir_size_bytes, disk_usage, free_mb, human_bytes, read_json, run

# 已知兼容性规则（每条都标注确定性等级）
COMPAT_RULES: list[dict[str, Any]] = [
    {
        "id": "sve-version",
        "level": "inferred",
        "title": "SVE 与游戏版本的对应关系",
        "detail": "Stardew Valley Expanded 有严格的游戏版本对应要求；"
                  "SVE 版本与游戏 1.6.15 不匹配时会加载失败。",
        "check": "安装 SVE 后必须查看 SMAPI 控制台的实际加载结果。",
    },
    {
        "id": "smapi-mod-api",
        "level": "certain",
        "title": "MOD 要求的 SMAPI 版本高于镜像内版本时必定加载失败",
        "detail": "manifest 的 MinimumApiVersion 高于当前 SMAPI（4.5.1）时，SMAPI 会直接跳过该 MOD。",
        "check": "对比每个 MOD 的 MinimumApiVersion 与 SMAPI 版本。",
    },
    {
        "id": "multiplayer-heavy-mods",
        "level": "unknown",
        "title": "部分 MOD 的联机兼容性无法自动判断",
        "detail": "很多 MOD 在单人下正常，但多人联机时表现不同（尤其是修改事件/时间的 MOD）。",
        "check": "只能通过实际联机测试确认，本项目不做自动结论。",
    },
    {
        "id": "alos-festivals",
        "level": "certain",
        "title": "ALOS 只覆盖部分节日与事件",
        "detail": "ALOS 文档明确列出支持 9 个节日/事件（复活节、花舞节、夏威夷宴会、"
                  "展览会、万灵节、冰雪节、冬日星盛宴、爷爷幽灵），并明确「不支持其他 Mod 的事件」"
                  "与「必须由农场主人接取的任务」。",
        "check": "已由上游 README 表格确认为确定性限制。",
    },
    {
        "id": "cmdwebui-risk",
        "level": "certain",
        "title": "CommandWebUI 的 WebSocket 无认证",
        "detail": "上游 README 自承「获取到 WebSocketIO 终结点，将会获取到控制台权限…存在渗透风险」。",
        "check": "确定性风险；本管理器默认不把 29103 暴露到外网。",
    },
]


@dataclass
class DiagnosticFinding:
    level: str          # error | warn | info
    certainty: str      # certain | inferred | unknown
    source: str
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "certainty": self.certainty,
                "source": self.source, "message": self.message, "detail": self.detail}


class Diagnostics:
    def __init__(self, compose=None, snapshots=None, mods=None, saves=None,
                 versions=None, tasks=None, runtime=None) -> None:
        # 兼容旧参数名；现在只有原生运行时
        self.runtime = runtime if runtime is not None else compose
        self.runtime = self.runtime
        self.snapshots = snapshots
        self.mods = mods
        self.saves = saves
        self.versions = versions
        self.tasks = tasks

    # ── 系统信息 ────────────────────────────────────────────────────────────
    def system_info(self) -> dict[str, Any]:
        total, used, free = disk_usage(C.DATA_ROOT)
        info: dict[str, Any] = {
            "manager_version": C.APP_VERSION,
            "python": _python_version(),
            "pid": os.getpid(),
            "hostname": os.uname().nodename,
            "kernel": os.uname().release,
            "arch": os.uname().machine,
            "now": time.strftime("%Y-%m-%d %H:%M:%S"),
            "paths": {
                "appdest": str(C.APPDEST),
                "pkgvar": str(C.PKGVAR),
                "pkghome": str(C.PKGHOME),
                "pkgetc": str(C.PKGETC),
                "data_root": str(C.DATA_ROOT),
                "saves": str(C.SAVES_DIR),
                "mods": str(C.MODS_DIR),
                "snapshots": str(C.SNAPSHOT_DIR),
                "logs": str(C.LOG_DIR),
            },
            "env": {
                "TRIM_APPNAME": os.environ.get("TRIM_APPNAME", ""),
                "TRIM_APPVER": os.environ.get("TRIM_APPVER", ""),
                "TRIM_APPDEST": os.environ.get("TRIM_APPDEST", ""),
                "TRIM_PKGVAR": os.environ.get("TRIM_PKGVAR", ""),
                "TRIM_PKGHOME": os.environ.get("TRIM_PKGHOME", ""),
                "DATA_LIBRARY_PATH": os.environ.get("DATA_LIBRARY_PATH", ""),
                "HOME": os.environ.get("HOME", ""),
            },
            "disk": {
                "total_bytes": total, "total_human": human_bytes(total),
                "used_bytes": used, "used_human": human_bytes(used),
                "free_bytes": free, "free_human": human_bytes(free),
                "free_mb": free // (1024 * 1024),
            },
            "storage_breakdown": self.storage_breakdown(),
        }
        if self.runtime is not None:
            info["runtime"] = self.runtime.detect()
            info["processes"] = self.runtime.status().to_dict()
            info["stats"] = self.runtime.stats()
            info["readiness"] = {
                k: v for k, v in self.runtime.readiness().items() if k != "console_tail"
            }
        return info

    def storage_breakdown(self) -> dict[str, Any]:
        parts = {
            "saves": C.SAVES_DIR,
            "mods": C.MODS_DIR,
            "snapshots": C.SNAPSHOT_DIR,
            "logs": C.LOG_DIR,
            "staging": C.STAGING_DIR,
            "restore_tmp": C.RESTORE_TMP_DIR,
            "archives": C.ARCHIVE_DIR,
        }
        out: dict[str, Any] = {}
        for name, path in parts.items():
            if path.is_dir():
                size = dir_size_bytes(path)
                out[name] = {"path": str(path), "bytes": size, "human": human_bytes(size)}
            else:
                out[name] = {"path": str(path), "bytes": 0, "human": "0 B", "missing": True}
        return out

    # ── 健康检查 ────────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        findings: list[DiagnosticFinding] = []
        cfg = C.load_config()

        # 原生运行环境
        if self.runtime is not None:
            det = self.runtime.detect()
            if not det.get("ready"):
                findings.append(DiagnosticFinding(
                    "error", "certain", "runtime",
                    "原生运行环境未就绪",
                    "；".join(det.get("guidance") or ["显示组件或游戏本体缺失"])[:600],
                ))
            else:
                st = self.runtime.status()
                if st.exists and not st.running:
                    findings.append(DiagnosticFinding(
                        "warn", "certain", "runtime",
                        "游戏进程处于停止状态",
                        st.error or "可通过「总览」页的启动按钮重新拉起。",
                    ))

        # 磁盘
        free = free_mb(C.DATA_ROOT)
        if free < cfg.policy.min_free_space_mb:
            findings.append(DiagnosticFinding(
                "error", "certain", "disk",
                f"磁盘可用空间不足：{free} MiB（策略下限 {cfg.policy.min_free_space_mb} MiB）",
                "低于下限时管理器会拒绝高风险变更。",
            ))
        elif free < cfg.policy.min_free_space_mb * 2:
            findings.append(DiagnosticFinding(
                "warn", "certain", "disk",
                f"磁盘可用空间偏低：{free} MiB",
                "建议清理旧快照或扩大存储。",
            ))

        # MOD
        if self.mods is not None:
            try:
                report = self.mods.diagnose()
                for issue in report["issues"]:
                    findings.append(DiagnosticFinding(
                        issue["level"], "certain" if issue["level"] == "error" else "inferred",
                        "mods", issue["message"], issue.get("impact", ""),
                    ))
            except Exception as exc:  # noqa: BLE001
                findings.append(DiagnosticFinding("warn", "unknown", "mods",
                                                  f"MOD 诊断失败：{exc}"))

        # 存档
        if self.saves is not None:
            try:
                for save in self.saves.list_saves():
                    if not save.valid:
                        findings.append(DiagnosticFinding(
                            "error", "certain", "saves",
                            f"存档「{save.dir_name}」结构不完整",
                            "；".join(save.problems),
                        ))
            except Exception as exc:  # noqa: BLE001
                findings.append(DiagnosticFinding("warn", "unknown", "saves",
                                                  f"存档检查失败：{exc}"))

        # SMAPI 控制台
        if self.runtime is not None and self.mods is not None:
            console = self.runtime.console_log(tail_bytes=400_000)
            parsed = self.mods.parse_smapi_log(console)
            if parsed.get("available"):
                for err in parsed.get("errors", []):
                    findings.append(DiagnosticFinding(
                        "error", "certain", "smapi",
                        f"SMAPI 报错（{err.get('mod') or '未知 MOD'}）：{err['line'][:200]}",
                        "见控制台页面完整日志。",
                    ))
                if not parsed.get("loaded_count"):
                    findings.append(DiagnosticFinding(
                        "warn", "inferred", "smapi",
                        "尚未在控制台日志中识别到 MOD 加载记录",
                        "若容器刚启动，请等待 1-3 分钟；若持续如此请检查 MOD 目录。",
                    ))

        # 快照
        if self.snapshots is not None:
            try:
                infos = self.snapshots.list()
                if not infos:
                    findings.append(DiagnosticFinding(
                        "warn", "certain", "snapshots", "尚未创建任何快照",
                        "建议先创建一次手动快照，作为后续所有高风险操作的安全基线。",
                    ))
                elif not any(i.valid for i in infos):
                    findings.append(DiagnosticFinding(
                        "error", "certain", "snapshots", "所有快照都未通过校验",
                        "恢复功能将不可用，请重新创建快照。",
                    ))
            except Exception as exc:  # noqa: BLE001
                findings.append(DiagnosticFinding("warn", "unknown", "snapshots",
                                                  f"快照检查失败：{exc}"))

        # 安全
        if self.runtime is not None:
            st = self.runtime.status()
            if st.exists:
                cfg2 = C.load_config()
                if cfg2.native.host_bind in ("0.0.0.0", "::"):
                    findings.append(DiagnosticFinding(
                        "warn", "certain", "security",
                        "WebVNC/VNC/CommandWebUI 端口正在对所有网络接口监听",
                        "WebVNC（noVNC）本身没有认证、CommandWebUI 的 WebSocket 也无认证。"
                        "若不需要远程操作游戏，请在「配置」中把监听地址改为 127.0.0.1，"
                        "或确认已在防火墙层面限制来源。",
                    ))
                if cfg2.native.vnc_password and len(cfg2.native.vnc_password) < 8:
                    findings.append(DiagnosticFinding(
                        "warn", "certain", "security", "VNC 密码长度不足 8 位",
                        "VNC 协议只使用前 8 个字符。",
                    ))

        errors = sum(1 for f in findings if f.level == "error")
        warns = sum(1 for f in findings if f.level == "warn")
        return {
            "checked_at": time.time(),
            "status": "error" if errors else ("warn" if warns else "ok"),
            "counts": {"error": errors, "warn": warns,
                       "info": sum(1 for f in findings if f.level == "info")},
            "findings": [f.to_dict() for f in findings],
            "compat_rules": COMPAT_RULES,
            "disclaimer": "本页提示不能代替实际测试；标注为「规则推断」与「无法确认」的条目需人工验证。",
        }

    # ── 诊断包 ──────────────────────────────────────────────────────────────
    def build_bundle(self, *, task_id: str = "", progress=None) -> Path:
        """生成诊断包 tar.gz。导出前逐项脱敏。"""
        emit = progress or (lambda *_a, **_k: None)
        C.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bundle = C.STAGING_DIR / f"diagnostics-{stamp}.tar.gz"
        sections: dict[str, Any] = {
            "system.json": self.system_info(),
            "health.json": self.health(),
            "config.redacted.json": self._redact_config(C.load_config().to_dict()),
        }
        emit("收集系统信息与健康检查…", 20)
        if self.runtime is not None:
            emit("收集运行日志…", 40)
            console = self.runtime.console_log(tail_bytes=1_500_000)
            sections["smapi-console.log"] = self.redact_text(console)[-1_000_000:]
            sections["runtime-logs.log"] = self.redact_text(
                self.runtime.logs(tail=1500))[-800_000:]
        from .logmod import get_logger
        emit("收集管理器日志…", 60)
        sections["manager.log"] = self.redact_text(
            "\n".join(get_logger("manager").tail(lines=2000)))[-800_000:]
        if self.snapshots is not None:
            try:
                sections["snapshots.json"] = json.dumps(
                    [i.to_dict() for i in self.snapshots.list()], indent=2, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                sections["snapshots.json"] = "[]"
        if self.mods is not None:
            try:
                sections["mods.json"] = json.dumps(
                    self.mods.diagnose(), indent=2, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                sections["mods.json"] = "{}"
        if self.versions is not None:
            try:
                sections["versions.json"] = json.dumps(
                    self.versions.history(limit=100), indent=2, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                sections["versions.json"] = "[]"
        if self.tasks is not None:
            try:
                sections["tasks.json"] = json.dumps(
                    self.tasks.list(limit=100), indent=2, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                sections["tasks.json"] = "[]"
        emit("打包…", 85)
        with tarfile.open(bundle, "w:gz") as tar:
            for name, content in sections.items():
                data = content.encode("utf-8") if isinstance(content, str) else \
                    json.dumps(content, indent=2, ensure_ascii=False).encode("utf-8")
                data = self.redact_bytes(data)
                info = tarfile.TarInfo(name=f"valleyserver-diagnostics/{name}")
                info.size = len(data)
                info.mtime = int(time.time())
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
            readme = (
                "ValleyServer 管理器诊断包\n"
                f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"任务 ID：{task_id}\n\n"
                "本包已自动脱敏：VNC 密码、访问令牌、Authorization 头、管理员口令、"
                "密码字段等均已替换为 [REDACTED]。\n"
                "内不含存档内容与 MOD 文件本体，仅含清单与校验值。\n"
                "披露前仍建议人工复核一遍。\n"
            ).encode("utf-8")
            info = tarfile.TarInfo(name="valleyserver-diagnostics/README.txt")
            info.size = len(readme)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(readme))
        emit("诊断包已生成", 99)
        return bundle

    # ── 脱敏 ────────────────────────────────────────────────────────────────
    SENSITIVE_KEYS = (
        "password", "passwd", "secret", "token", "apikey", "api_key",
        "accesstoken", "access_token", "authorization", "cookie", "session",
        "vnc_password", "admin_password", "private_key",
    )
    # 形如 KEY=VALUE / "KEY": "VALUE" 的敏感赋值
    _KV_RE = re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?token|"
        r"authorization|vnc[_-]?password|admin[_-]?password)([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    )
    _AUTH_RE = re.compile(r"(?i)(authorization:\s*)(\S+\s+\S+|\S+)")
    _BEARER_RE = re.compile(r"(?i)\b(bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")

    @classmethod
    def redacted_text(cls, text: str) -> str:
        if not text:
            return ""
        text = cls._KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
        text = cls._AUTH_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
        text = cls._BEARER_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", text)
        # 常见 token 形态
        text = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{20,}", "[REDACTED_GITHUB_TOKEN]", text)
        text = re.sub(r"\b[A-Za-z0-9_-]{40,}\b", "[REDACTED_LONG_SECRET]", text)
        return text

    # 历史名称别名
    redact_text = redacted_text

    @classmethod
    def redact_bytes(cls, data: bytes) -> bytes:
        try:
            return cls.redact_text(data.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return data

    @classmethod
    def _redact_config(cls, obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if str(key).lower() in cls.SENSITIVE_KEYS:
                    out[key] = "[REDACTED]" if value else ""
                else:
                    out[key] = cls._redact_config(value)
            return out
        if isinstance(obj, list):
            return [cls._redact_config(v) for v in obj]
        return obj


def _python_version() -> str:
    import sys
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
