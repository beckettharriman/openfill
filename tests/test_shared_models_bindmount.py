# SPDX-License-Identifier: Apache-2.0
"""OpenFill's models folder bind-mounted on a shared one (review 2, N1): the folders are the same
by identity though their paths differ, so nothing is deleted or downloaded there and check()
refuses the setting. Needs a private mount namespace (``unshare -rm``, no real root); skipped
where that is not available."""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from openfill import shared_models as sm
from tests.test_shared_models import WorkerFixture, catalog_file, snapshot

ROOT = Path(__file__).resolve().parents[1]
INSIDE = "OPENFILL_IN_MOUNT_NAMESPACE"


class BindMountTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and shutil.which("unshare"), "needs unshare (Linux)")
    def test_in_a_private_mount_namespace(self):
        env = {**os.environ, INSIDE: "1", "PYTHONDONTWRITEBYTECODE": "1"}
        cmd = ["unshare", "-rm", sys.executable, "-m", "unittest", "-v", f"{__name__}.InNamespace"]
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        if "unshare failed" in out or "Operation not permitted" in out or "skipped 'no bind mount" in out:
            self.skipTest("no private mount namespace here: " + out.strip().splitlines()[-1])
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("OK", out)


@unittest.skipUnless(os.environ.get(INSIDE), "runs inside the private mount namespace only")
class InNamespace(WorkerFixture):
    def test_own_models_folder_bind_mounted_on_the_shared_one_stays_read_only(self):
        r = subprocess.run(["mount", "--bind", str(self.server / "models"), str(self.models_root)], capture_output=True)
        if r.returncode:
            self.skipTest(f"no bind mount: {r.stderr.decode().strip()}")
        try:
            xl = catalog_file("realvisxl-v5", "checkpoint")
            target = self.server / "models" / xl.dest
            self.assertTrue((self.models_root / xl.dest).is_file())  # one folder, two names
            _folders, problem = sm.check(str(self.server), self.models_root)
            self.assertIn("link", problem)
            before = snapshot(self.server)
            self.worker._remove(
                {
                    "id": "t",
                    "model_id": "realvisxl-v5",
                    "type": "remove",
                    "models_root": str(self.models_root),
                    "files": [xl.dest],
                }
            )
            self.assertTrue(target.is_file(), "Krita's RealVisXL was deleted through the bind mount")
            lama = catalog_file("lama-big", "inpaint_model")
            task = {
                "type": "download",
                "model_id": "lama-big",
                "models_root": str(self.models_root),
                "files": [{"url": lama.url, "dest": lama.dest, "size": lama.size}],
            }
            with self.assertRaises(ValueError):
                self.worker.checked_download_files(task)
            self.assertEqual(snapshot(self.server), before)
        finally:
            subprocess.run(["umount", str(self.models_root)], check=False)


if __name__ == "__main__":
    unittest.main()
