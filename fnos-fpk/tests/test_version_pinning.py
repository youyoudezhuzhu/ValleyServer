"""版本固定能力与镜像不可变标识的实测。

这是「版本固定和回滚能力」的**实证**，不是声称：
  1. 直接查 Docker Registry，证明配置里记录的镜像摘要确实存在且与 manifest 一致；
  2. 证明按 digest 拉取的引用形式（`repo@sha256:...`）能被 registry 接受；
  3. 证明生成的 compose 使用不可变摘要而不是会漂移的标签；
  4. 证明回滚能力评估在缺少本地镜像时会**如实报告不具备**，而不是假装可以。

需要外网（Docker Hub）。无外网时自动跳过，不会失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import isolated_env  # noqa: E402

ENV = isolated_env()

from mgr import config as C  # noqa: E402
from mgr.compose_mgr import ComposeManager  # noqa: E402
from mgr.util import run  # noqa: E402
from mgr.versions import RuntimeVersion, VersionManager  # noqa: E402

REPO = "lixeer/valley-server"
TAG = "v26.5"
AUDITED_DIGEST = C.DEFAULT_IMAGE_DIGEST


def registry_available() -> bool:
    res = run(["curl", "-sS", "--max-time", "8", "-o", "/dev/null",
               "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull"],
              timeout=15)
    return res.ok


class TestImageDigestRecorded(unittest.TestCase):
    def test_default_digest_format(self):
        """配置里的默认摘要必须是合法的 sha256 形式。

        注意：这是**代码常量**的断言，与镜像是否真实存在无关（后者见下面的联网测试）。
        """
        self.assertRegex(AUDITED_DIGEST, r"^sha256:[a-f0-9]{64}$")

    def test_config_defaults_pin_digest(self):
        cfg = C.ManagerConfig()
        self.assertTrue(cfg.container.pin_digest,
                        "默认必须按摘要固定，否则 latest/标签漂移会导致不可复现")
        self.assertEqual(cfg.container.image_digest, AUDITED_DIGEST)
        self.assertEqual(cfg.container.image, REPO)
        self.assertEqual(cfg.container.image_tag, TAG)

    def test_compose_uses_digest_not_tag(self):
        C.ensure_dirs()
        cfg = C.ManagerConfig()
        cfg.container.vnc_password = "Ab3dEf9z"
        text = ComposeManager().render(cfg)
        self.assertIn(f"{REPO}@{AUDITED_DIGEST}", text)
        self.assertNotIn(f"{REPO}:{TAG}", text)


class TestRegistryDigestMatches(unittest.TestCase):
    """联网核对：配置里记录的摘要必须与 Docker Hub 上的 manifest 一致。"""

    @classmethod
    def setUpClass(cls):
        if not registry_available():
            raise unittest.SkipTest("无外网或 Docker Hub 不可达，跳过联网核对")

    def _fetch_manifest_digest(self) -> tuple[str, dict]:
        token_res = run([
            "curl", "-sS", "--max-time", "20",
            "https://auth.docker.io/token?service=registry.docker.io"
            f"&scope=repository:{REPO}:pull",
        ], timeout=40)
        if not token_res.ok:
            raise unittest.SkipTest(f"无法获取 registry token：{token_res.tail[:120]}")
        try:
            token = json.loads(token_res.stdout)["token"]
        except (json.JSONDecodeError, KeyError):
            raise unittest.SkipTest("registry token 响应无法解析")
        out = Path(ENV["TRIM_PKGTMP"]) / "manifest.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        res = run([
            "curl", "-sS", "--max-time", "30", "-D", str(out) + ".headers",
            "-o", str(out),
            "-H", f"Authorization: Bearer {token}",
            "-H", "Accept: application/vnd.oci.image.manifest.v1+json,"
                  "application/vnd.docker.distribution.manifest.v2+json",
            f"https://registry-1.docker.io/v2/{REPO}/manifests/{TAG}",
        ], timeout=60)
        if not res.ok or not out.is_file():
            raise unittest.SkipTest(f"无法获取 manifest：{res.tail[:120]}")
        digest_header = ""
        headers = Path(str(out) + ".headers").read_text(errors="replace")
        for line in headers.splitlines():
            if line.lower().startswith("docker-content-digest:"):
                digest_header = line.split(":", 1)[1].strip()
        body_digest = "sha256:" + hashlib.sha256(out.read_bytes()).hexdigest()
        try:
            manifest = json.loads(out.read_text())
        except json.JSONDecodeError:
            manifest = {}
        return (digest_header or body_digest), manifest

    def test_registry_digest_matches_audit(self):
        digest, manifest = self._fetch_manifest_digest()
        self.assertEqual(
            digest, AUDITED_DIGEST,
            f"审计记录的摘要 {AUDITED_DIGEST} 与上游当前 {digest} 不一致 —— "
            "说明上游 tag 内容已变化（这正是必须按摘要固定的原因）",
        )
        # manifest 里应有 config + layers，证明这是真实可拉取的镜像
        self.assertIn("config", manifest)
        self.assertGreater(len(manifest.get("layers", [])), 0)

    def test_digest_reference_is_accepted(self):
        """证明 `repo@sha256:...` 这种引用形式能被 registry 解析（回滚要靠它）。"""
        token_res = run([
            "curl", "-sS", "--max-time", "20",
            "https://auth.docker.io/token?service=registry.docker.io"
            f"&scope=repository:{REPO}:pull",
        ], timeout=40)
        if not token_res.ok:
            self.skipTest("无法获取 token")
        try:
            token = json.loads(token_res.stdout)["token"]
        except (json.JSONDecodeError, KeyError):
            self.skipTest("token 无法解析")
        res = run([
            "curl", "-sS", "--max-time", "30", "-o", "/dev/null", "-w", "%{http_code}",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Accept: application/vnd.oci.image.manifest.v1+json,"
                  "application/vnd.docker.distribution.manifest.v2+json",
            f"https://registry-1.docker.io/v2/{REPO}/manifests/{AUDITED_DIGEST}",
        ], timeout=60)
        self.assertEqual(res.stdout.strip(), "200",
                         f"按摘要查询 manifest 失败（HTTP {res.stdout.strip()}）")


class TestRollbackCapabilityHonesty(unittest.TestCase):
    """回滚能力评估必须如实反映现状，不能伪造「可以回滚」。"""

    def setUp(self):
        C.ensure_dirs()
        self.vm = VersionManager(compose=None)

    def test_no_compose_reports_missing(self):
        cap = self.vm.rollback_capability()
        self.assertFalse(cap["can_rollback"])
        self.assertTrue(cap["missing"])

    def test_limitations_disclosed(self):
        limits = self.vm.limitations()
        joined = " ".join(limits)
        self.assertIn("单一镜像标签", joined)
        self.assertIn("SMAPI", joined)
        self.assertIn("本地", joined)

    def test_unknown_target_digest_reports_missing(self):
        cap = self.vm.rollback_capability(target_digest="sha256:" + "0" * 64)
        self.assertFalse(cap["image_present"])
        self.assertFalse(cap["can_rollback"])
        self.assertTrue(any("本地没有镜像" in m for m in cap["missing"]))


class TestDigestUsedForPull(unittest.TestCase):
    """按 digest 拉取时，compose 与 pull 命令都必须使用同一摘要。"""

    def setUp(self):
        C.ensure_dirs()
        self.cfg = C.ManagerConfig()
        self.cfg.container.vnc_password = "Ab3dEf9z"
        self.cmp = ComposeManager()

    def test_image_ref_helper(self):
        ref = self.cmp._image_ref(self.cfg.container)
        self.assertEqual(ref, f"{REPO}@{AUDITED_DIGEST}")

    def test_image_ref_with_custom_digest(self):
        from mgr.util import ValidationError
        self.cfg.container.image_digest = "sha256:notahash"
        with self.assertRaises(ValidationError):
            self.cmp._image_ref(self.cfg.container)

    def test_image_ref_tag_mode(self):
        self.cfg.container.pin_digest = False
        ref = self.cmp._image_ref(self.cfg.container)
        self.assertEqual(ref, f"{REPO}:{TAG}")


class TestVersionArchiveRecordsContent(unittest.TestCase):
    """版本档案必须记录实际内容，而不是只存一个会变的 latest。"""

    def test_record_and_history(self):
        C.ensure_dirs()
        vm = VersionManager(compose=None)
        rv = RuntimeVersion(
            image_ref=f"{REPO}@{AUDITED_DIGEST}",
            image_digest=AUDITED_DIGEST,
            game_version="1.6.15", smapi_version="4.5.1",
            mods_hash="deadbeef", mod_count=5,
            saves_hash="cafebabe", save_count=2, recorded_at=1.0,
        )
        before = {f.name for f in C.ARCHIVE_DIR.glob("version-*.json")}
        vid = vm.record(rv, note="测试记录", pinned=True)
        self.assertGreater(vid, 0)
        hist = vm.history(limit=10)
        self.assertTrue(any(h["id"] == vid for h in hist))
        row = next(h for h in hist if h["id"] == vid)
        self.assertEqual(row["game_version"], "1.6.15")
        self.assertEqual(row["smapi_version"], "4.5.1")
        self.assertEqual(row["image_digest"], AUDITED_DIGEST)
        self.assertEqual(row["pinned"], 1)
        # 同时落一份 JSON 档案，便于离线查看（用新增文件定位，避免测试间相互污染）
        new_files = [f for f in C.ARCHIVE_DIR.glob("version-*.json") if f.name not in before]
        self.assertTrue(new_files, "版本档案 JSON 未生成")
        payload = json.loads(new_files[0].read_text())
        self.assertEqual(payload["image_digest"], AUDITED_DIGEST)
        self.assertEqual(payload["game_version"], "1.6.15")

    def test_pin_toggle(self):
        C.ensure_dirs()
        vm = VersionManager(compose=None)
        vid1 = vm.record(RuntimeVersion(image_digest="sha256:" + "1" * 64, recorded_at=1.0),
                         note="v1", pinned=True)
        vid2 = vm.record(RuntimeVersion(image_digest="sha256:" + "2" * 64, recorded_at=2.0),
                         note="v2", pinned=False)
        vm.set_pinned(vid2, True)
        pinned = vm.pinned()
        self.assertIsNotNone(pinned)
        self.assertEqual(pinned["id"], vid2)
        vm.set_pinned(vid2, False)
        self.assertIsNone(vm.pinned())
        _ = vid1


if __name__ == "__main__":
    unittest.main(verbosity=2)
