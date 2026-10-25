# SPDX-License-Identifier: Apache-2.0
import io
import json
import logging
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openfill.config import Paths, Settings, comfy_layout, corrupt_backup_path
from openfill.log import RedactingFilter, get_logger, redact

TOKEN = "hf_abcdefghijklmnopqrstuvwxyz0123456789"


class SettingsTests(unittest.TestCase):
    def test_unvalidated_passes_are_off_by_default(self):
        # The Expand detail windows and Fill's refine pass stay opt-in until an A/B shows they help.
        self.assertFalse(Settings().expand_detail)
        self.assertFalse(Settings().refine)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            s = Settings(steps=31, cfg=5.5, autostart=False, checkpoint="x.ckpt")
            s.save(path)
            loaded = Settings.load(path)
            self.assertEqual(
                (loaded.steps, loaded.cfg, loaded.autostart, loaded.checkpoint), (31, 5.5, False, "x.ckpt")
            )

    def test_missing_or_broken_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self.assertEqual(Settings.load(path).steps, Settings().steps)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(Settings.load(path).backend, "comfyui")

    def test_unknown_keys_and_coercion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"steps": "40", "autostart": "false", "cfg": 3, "bogus": 1}), encoding="utf-8")
            s = Settings.load(path)
            self.assertEqual(s.steps, 40)
            self.assertFalse(s.autostart)
            self.assertEqual(s.cfg, 3.0)
            self.assertFalse(hasattr(s, "bogus"))

    def test_model_for(self):
        s = Settings(sdxl_checkpoint="sdxl.safetensors", fooocus_head="h", fooocus_patch="p")
        self.assertEqual(s.model_for("fast").arch, "sd15")
        q = s.model_for("quality")
        self.assertEqual(q.arch, "sdxl")
        self.assertTrue(q.uses_fooocus)
        s.sdxl_checkpoint = ""
        self.assertEqual(s.model_for("quality").arch, "sd15")

    def test_active_url(self):
        s = Settings(backend="a1111", a1111_url="http://x:1")
        self.assertEqual(s.active_url, "http://x:1")


class DamagedConfigTests(unittest.TestCase):
    def _backups(self, folder: Path) -> list[Path]:
        return sorted(folder.glob("config.json.corrupt-*"))

    def test_corrupt_json_is_moved_aside_and_defaults_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{ not json "hf_token": "' + TOKEN + '"', encoding="utf-8")
            s = Settings.load(path)
            self.assertEqual(s.steps, Settings().steps)
            self.assertFalse(path.exists())
            backups = self._backups(Path(tmp))
            self.assertEqual(len(backups), 1)
            self.assertIn(TOKEN, backups[0].read_text(encoding="utf-8"))  # the user's data is kept, not lost
            self.assertIn("could not be read and was moved aside to", s.load_problem)
            self.assertIn(str(backups[0]), s.load_problem)
            self.assertIn("defaults are in use", s.load_problem)
            self.assertNotIn(TOKEN, s.load_problem)
            s.save(path)  # the backup succeeded, so saving defaults is safe
            self.assertTrue(path.exists())
            self.assertEqual(len(self._backups(Path(tmp))), 1)

    def test_json_list_is_treated_as_damaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            s = Settings.load(path)
            self.assertIsNotNone(s.load_problem)
            self.assertEqual(len(self._backups(Path(tmp))), 1)
            self.assertEqual(s.backend, "comfyui")

    def test_good_file_has_no_problem_and_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"steps": 12, "future_option": True, "load_problem": "x"}), encoding="utf-8")
            s = Settings.load(path)
            self.assertIsNone(s.load_problem)
            self.assertEqual(s.steps, 12)
            self.assertTrue(path.exists())
            self.assertEqual(self._backups(Path(tmp)), [])

    def test_missing_file_is_not_a_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(Settings.load(Path(tmp) / "config.json").load_problem)

    def test_load_problem_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            s = Settings()
            s.load_problem = "something"
            s.save(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("load_problem", data)
            self.assertNotIn("_protected_path", data)
            self.assertNotIn("something", repr(s))

    def test_backup_name_uses_timestamp_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            when = time.mktime((2026, 10, 9, 1, 2, 3, 0, 0, -1))
            first = corrupt_backup_path(path, when)
            self.assertEqual(first.name, "config.json.corrupt-20261009-010203")
            self.assertEqual(first.parent, path.parent)
            first.write_text("old backup", encoding="utf-8")
            second = corrupt_backup_path(path, when)
            self.assertEqual(second.name, "config.json.corrupt-20261009-010203-2")
            self.assertTrue(re.fullmatch(r"config\.json\.corrupt-\d{8}-\d{6}", corrupt_backup_path(path).name))

    def test_existing_backup_is_kept_when_another_corrupt_file_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for content in ("{ first", "{ second"):
                path.write_text(content, encoding="utf-8")
                Settings.load(path)
            contents = sorted(p.read_text(encoding="utf-8") for p in self._backups(Path(tmp)))
            self.assertEqual(contents, ["{ first", "{ second"])

    def test_failed_backup_leaves_file_and_blocks_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json", encoding="utf-8")
            with mock.patch("openfill.config.os.rename", side_effect=PermissionError(13, "Access is denied")):
                s = Settings.load(path)
            self.assertIn("left untouched", s.load_problem)
            self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")
            with self.assertRaises(OSError):
                s.save(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")
            s.save(Path(tmp) / "other.json")  # other paths are fine

    def test_backup_false_only_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json", encoding="utf-8")
            s = Settings.load(path, backup=False)
            self.assertEqual(s.steps, Settings().steps)
            self.assertTrue(path.exists())
            self.assertEqual(self._backups(Path(tmp)), [])


class TokenStorageTests(unittest.TestCase):
    def test_token_roundtrips_through_config_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths.for_profile(tmp).ensure()
            s = Settings(hf_token=TOKEN)
            s.save(paths.config)
            self.assertEqual(Settings.load(paths.config).hf_token, TOKEN)
            self.assertEqual(json.loads(paths.config.read_text(encoding="utf-8"))["hf_token"], TOKEN)
            # nothing else in the profile folder holds it
            others = [p for p in paths.base.rglob("*") if p.is_file() and p != paths.config]
            for other in others:
                self.assertNotIn(TOKEN, other.read_text(encoding="utf-8", errors="replace"), other)

    def test_token_not_in_repr(self):
        s = Settings(hf_token=TOKEN)
        self.assertNotIn(TOKEN, repr(s))
        self.assertNotIn(TOKEN, str(s))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_config_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Settings(hf_token=TOKEN).save(path)
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_redact(self):
        self.assertNotIn(TOKEN, redact(f"token {TOKEN} here"))
        self.assertEqual(redact("Authorization: Bearer abc.def"), "Authorization: Bearer [redacted]")
        self.assertNotIn("secretvalue", redact('{"hf_token": "secretvalue", "steps": 25}'))
        self.assertIn('"steps": 25', redact('{"hf_token": "secretvalue", "steps": 25}'))
        self.assertEqual(redact("plain message, 25 steps"), "plain message, 25 steps")

    def test_logger_redacts_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = get_logger(Path(tmp) / "x.log", name="openfill-test-redact")
            stream = io.StringIO()
            extra = logging.StreamHandler(stream)
            log.addHandler(extra)
            try:
                log.info("settings: %s", {"hf_token": TOKEN})
                log.info("header Bearer %s", TOKEN)
            finally:
                log.removeHandler(extra)
                for h in list(log.handlers):
                    h.close()
                    log.removeHandler(h)
            self.assertNotIn(TOKEN, stream.getvalue())
            self.assertIn("[redacted]", stream.getvalue())
            self.assertTrue(any(isinstance(f, RedactingFilter) for f in log.filters))


class PathsTests(unittest.TestCase):
    def test_layout(self):
        p = Paths.for_profile("/gimp/3.2")
        self.assertEqual(p.config.name, "config.json")
        self.assertEqual(p.state.parent.name, "openfill")
        self.assertEqual(p.temp, p.base / "tmp")

    def test_old_config_folder_is_copied_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "stable-boy"
            old.mkdir()
            (old / "config.json").write_text('{"steps": 31}', encoding="utf-8")
            (old / "server-state.json").write_text("{}", encoding="utf-8")
            paths = Paths.for_profile(tmp)
            self.assertEqual(json.loads(paths.config.read_text(encoding="utf-8")), {"steps": 31})
            self.assertFalse(paths.state.exists())
            self.assertTrue((old / "config.json").exists())
            paths.config.write_text('{"steps": 5}', encoding="utf-8")
            Paths.for_profile(tmp)  # second run leaves the new folder alone
            self.assertEqual(json.loads(paths.config.read_text(encoding="utf-8")), {"steps": 5})


class ComfyLayoutTests(unittest.TestCase):
    def test_portable_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(comfy_layout(root))
            (root / "python_embeded").mkdir()
            (root / "python_embeded" / "python.exe").write_bytes(b"")
            (root / "ComfyUI").mkdir()
            (root / "ComfyUI" / "main.py").write_text("")
            layout = comfy_layout(root)
            self.assertEqual(layout["main"], root / "ComfyUI" / "main.py")
            self.assertEqual(layout["cwd"], root)

    def test_checkout_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("")
            layout = comfy_layout(root)
            self.assertEqual(layout["main"], root / "main.py")


if __name__ == "__main__":
    unittest.main()


class ComfyProblemTests(unittest.TestCase):
    """What the user is told when the ComfyUI folder cannot be started."""

    def test_reasons(self):
        from openfill.config import comfy_problem

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("No ComfyUI folder", comfy_problem(""))
            self.assertIn("does not exist", comfy_problem(root / "nope"))
            self.assertIn("neither main.py", comfy_problem(root))
            (root / "main.py").write_text("")
            (root / "venv" / "bin").mkdir(parents=True)
            (root / "venv" / "bin" / "python3").write_text("")
            self.assertEqual(comfy_problem(root), "")

    def test_checkout_without_venv_on_macos_inside_gimp(self):
        # GIMP.app's Python can neither be started by the supervisor (launch constraint) nor run ComfyUI.
        from openfill import config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("")
            with (
                mock.patch.object(config.sys, "platform", "darwin"),
                mock.patch.object(config.sys, "executable", "/Applications/GIMP.app/Contents/MacOS/python3"),
            ):
                self.assertIsNone(config.comfy_layout(root))
                self.assertIn("no venv", config.comfy_problem(root))
            with (
                mock.patch.object(config.sys, "platform", "linux"),
                mock.patch.object(config.sys, "executable", "/usr/bin/python3"),
            ):
                self.assertEqual(config.comfy_layout(root)["python"], Path("/usr/bin/python3"))


class StaleCpuFlagTests(unittest.TestCase):
    def test_dropped_only_for_another_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp) / "OpenFill" / "ComfyUI_windows_portable"
            own = Path(tmp) / "MyComfy"
            s = Settings(comfy_dir=str(setup), extra_server_args="--lowvram --cpu", cpu_comfy_dir=str(setup))
            self.assertFalse(s.drop_stale_cpu_flag())  # still the folder it was added for
            s.comfy_dir = str(own)
            self.assertTrue(s.drop_stale_cpu_flag())
            self.assertEqual((s.extra_server_args, s.cpu_comfy_dir), ("--lowvram", ""))
            self.assertFalse(s.drop_stale_cpu_flag())

    def test_typed_by_the_user_is_kept_and_old_settings_use_the_setup_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp) / "ComfyUI_windows_portable"
            mine = Settings(comfy_dir=str(Path(tmp) / "MyComfy"), extra_server_args="--cpu")
            self.assertFalse(mine.drop_stale_cpu_flag(setup))  # no setup folder on disk: the user typed it
            setup.mkdir()
            self.assertTrue(mine.drop_stale_cpu_flag(setup))  # settings from before cpu_comfy_dir existed
            same = Settings(comfy_dir=str(setup), extra_server_args="--cpu")
            self.assertFalse(same.drop_stale_cpu_flag(setup))
