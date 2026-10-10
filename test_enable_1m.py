"""Offline tests: every configuration is in a temporary, synthetic Codex home."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import enable_1m as tool


class EnableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "profile"
        self.home.mkdir()
        self.models = [{"slug": "example-alpha", "context_window": 123,
                        "visibility": "list", "nested": {"flags": [True, "unchanged"]}},
                       {"slug": "example-beta", "max_context_window": 456,
                        "description": "Example metadata", "unknown_field": None}]
        self.write_cache()
        self.before = (b'# local settings\nmodel_context_window = 123\n'
                       b'model_auto_compact_token_limit = 100\n'
                       b'model_catalog_json = "previous-catalog.json"\n'
                       b'[features]\nmulti_agent = true\n'
                       b'[model_providers.example]\nbase_url = "https://example.invalid"\n'
                       b'[mcp_servers.example]\ntoken = "MCP_SECRET_TEST_ONLY"\n')
        self.config = self.home / "config.toml"
        self.config.write_bytes(self.before)

    def write_cache(self, value=None):
        (self.home / "models_cache.json").write_text(
            json.dumps(value if value is not None else {"models": self.models}), encoding="utf-8")

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob("*") if p.is_file()}

    def install(self):
        return tool.enable(self.home)

    def test_updates_every_model_and_preserves_all_other_metadata(self):
        result = self.install()
        expected = copy.deepcopy(self.models)
        for row in expected:
            row.update(tool.LIMITS)
        catalog = json.loads(Path(result["catalog"]).read_bytes())
        self.assertEqual(catalog, {"models": expected})
        config = tomllib.loads(self.config.read_text(encoding="utf-8"))
        before = tool.parsed(self.before)
        self.assertEqual(config, dict(before, **tool.wanted(Path(result["catalog"]))))
        self.assertIn(self.before[self.before.index(b"[features]"):], self.config.read_bytes())
        self.assertEqual(Path(result["backup"]).with_name("config.before.toml").read_bytes(), self.before)

    def test_bom_crlf_and_nested_keys_preserved(self):
        self.before = b"\xef\xbb\xbf" + self.before.replace(b"\n", b"\r\n")
        self.before += b'[profiles.example]\r\nmodel_context_window = 200\r\n'
        self.config.write_bytes(self.before)
        result = self.install()
        after = self.config.read_bytes()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))
        self.assertIn(b'[profiles.example]\r\nmodel_context_window = 200\r\n', after)
        tool.restore(self.home, result["backup"])
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_multiline_root_statements_are_not_table_headers(self):
        untouched = ('instructions = """\n[fictional.table]\n'
                     'model_context_window = 88\nquoted \\"value\\"\n"""\n'
                     "literal = '''\n[another.table]\n'''\n"
                     'args = [\n  "[third.table]",\n  { x = "example" },\n]\n'
                     'settings.flag = true\n')
        self.config.write_bytes((untouched + '[features]\nflag = true\n').encode("utf-8"))
        self.install()
        self.assertIn(untouched.encode("utf-8"), self.config.read_bytes())

    def test_multiline_target_value_is_replaced_safely(self):
        self.config.write_text('"model_catalog_json" = """\nold\n[not_a_table]\n"""\n'
                               '[features]\nflag = true\n', encoding="utf-8")
        self.install()
        self.assertEqual(tool.parsed(self.config.read_bytes())["features"], {"flag": True})

    def test_existing_correct_target_format_is_preserved(self):
        catalog, _ = tool.catalog_bytes((self.home / "models_cache.json").read_bytes())
        path = self.home / tool.STATE / ("catalog-" + tool.digest(catalog) + ".json")
        line = b'model_context_window = 1_000_000 # retain this comment\n'
        self.config.write_bytes(line)
        self.install()
        self.assertTrue(self.config.read_bytes().startswith(line))
        self.assertEqual(tool.parsed(self.config.read_bytes())["model_catalog_json"], path.as_posix())

    def test_integer_root_types_and_each_original_line_ending(self):
        self.config.write_bytes(b'model_context_window = 1000000.0 # window comment\n'
                                b'model_auto_compact_token_limit = 900000.0 # compact comment\r\n'
                                b'# unrelated comment\n')
        self.install()
        after = self.config.read_bytes()
        self.assertIn(b'model_context_window = 1000000 # window comment\n', after)
        self.assertIn(b'model_auto_compact_token_limit = 900000 # compact comment\r\n', after)
        values = tool.parsed(after)
        self.assertIs(type(values["model_context_window"]), int)
        self.assertIs(type(values["model_auto_compact_token_limit"]), int)

    def test_hash_inside_target_string_is_not_a_comment(self):
        self.config.write_bytes(b'model_catalog_json = "old#name.json" # keep this\n')
        self.install()
        self.assertIn(b' # keep this\n', self.config.read_bytes())
        self.assertNotIn(b'old#name', self.config.read_bytes())

    def test_missing_cache_fails_without_any_writes(self):
        (self.home / "models_cache.json").unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Error, "Open Codex once"):
            self.install()
        self.assertEqual(self.snapshot(), before)

    def test_invalid_cache_fails_without_any_writes(self):
        for data in (b"{", b"[]", b'{"models": []}', b'{"models": [{}]}',
                     b'{"models": [42]}', b'{"models": [{"slug":"a", "x":NaN}]}',
                     b'{"models": [{"slug":"a", "slug":"b"}]}'):
            with self.subTest(data=data):
                (self.home / "models_cache.json").write_bytes(data)
                before = self.snapshot()
                with self.assertRaisesRegex(tool.Error, "Open Codex once"):
                    self.install()
                self.assertEqual(self.snapshot(), before)

    def test_invalid_config_or_target_table_has_no_partial_update(self):
        for data in (b"x = [\n", b"x = '\xff'\n", b"[model_context_window]\nx = 1\n",
                     b"model_context_window.foo = 1\n"):
            with self.subTest(data=data):
                self.config.write_bytes(data)
                before = self.snapshot()
                with self.assertRaises(tool.Error):
                    self.install()
                self.assertEqual(self.snapshot(), before)

    def test_idempotence_creates_no_more_backups_or_writes(self):
        self.install()
        before = self.snapshot()
        mtimes = {p: p.stat().st_mtime_ns for p in self.home.rglob("*") if p.is_file()}
        self.assertFalse(self.install()["changed"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(mtimes, {p: p.stat().st_mtime_ns for p in mtimes})

    def test_dry_run_creates_no_files(self):
        before = self.snapshot()
        result = tool.enable(self.home, dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.snapshot(), before)

    def test_restore_selected_and_latest_and_repeated_selected(self):
        result = self.install()
        before = self.snapshot()
        self.assertTrue(tool.restore(self.home, "latest", dry_run=True)["changed"])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(tool.restore(self.home, result["backup"])["restored"])
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse(Path(result["catalog"]).exists())
        self.assertFalse(tool.restore(self.home, result["backup"])["changed"])

    def test_restore_preserves_later_unrelated_user_config_edits(self):
        self.install()
        current = self.config.read_bytes().replace(b"# local settings\n", b'model = "example-beta"\n')
        current += b"# later edit\n"
        self.config.write_bytes(current)
        tool.restore(self.home)
        after = self.config.read_bytes()
        self.assertIn(b'model = "example-beta"\n', after)
        self.assertIn(b"# later edit\n", after)
        self.assertEqual(tool.parsed(after), dict(tool.parsed(self.before), model="example-beta"))

    def test_restore_refuses_later_managed_key_edits(self):
        self.install()
        self.config.write_bytes(self.config.read_bytes().replace(b"model_context_window = 1000000",
                                                               b"model_context_window = 200000"))
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Error, "later edits"):
            tool.restore(self.home)
        self.assertEqual(self.snapshot(), before)

    def test_restore_retains_catalog_referenced_by_later_profile(self):
        result = self.install()
        extra = ('[profiles.example]\nmodel_catalog_json = '
                 + json.dumps(Path(result["catalog"]).as_posix()) + '\n').encode("utf-8")
        self.config.write_bytes(self.config.read_bytes() + extra)
        tool.restore(self.home)
        self.assertTrue(Path(result["catalog"]).exists())
        self.assertEqual(tool.parsed(self.config.read_bytes())["profiles"]["example"]["model_catalog_json"],
                         Path(result["catalog"]).as_posix())
        self.assertTrue(tool.enable(self.home)["changed"])

    def test_restore_retains_relative_or_windows_case_catalog_reference(self):
        result = self.install()
        name = Path(result["catalog"]).name
        reference = str(Path(result["catalog"])).upper() if os.name == "nt" else "relative/" + name
        extra = ('[profiles.example]\nmodel_catalog_json = '
                 + json.dumps(reference) + '\n').encode("utf-8")
        self.config.write_bytes(self.config.read_bytes() + extra)
        tool.restore(self.home)
        self.assertTrue(Path(result["catalog"]).exists())

    def test_restore_absent_original_keeps_later_other_settings(self):
        self.config.unlink()
        self.install()
        self.config.write_bytes(b'model = "example-beta"\n' + self.config.read_bytes())
        tool.restore(self.home)
        self.assertEqual(tool.parsed(self.config.read_bytes()), {"model": "example-beta"})

    def test_manifest_absence_flag_cannot_delete_existing_backup(self):
        result = self.install()
        record = Path(result["backup"])
        info = json.loads(record.read_bytes())
        info["before_sha256"] = None
        record.write_text(json.dumps(info), encoding="utf-8")
        current = self.config.read_bytes()
        with self.assertRaisesRegex(tool.Error, "manifest"):
            tool.restore(self.home)
        self.assertEqual(self.config.read_bytes(), current)

    def test_restore_refuses_modified_backup_catalog_or_manifest(self):
        for target in ("backup", "catalog", "manifest"):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as directory:
                    home = Path(directory).resolve()
                    (home / "models_cache.json").write_bytes((self.home / "models_cache.json").read_bytes())
                    (home / "config.toml").write_bytes(self.before)
                    result = tool.enable(home)
                    if target == "backup":
                        changed = Path(result["backup"]).with_name("config.before.toml")
                    elif target == "catalog":
                        changed = Path(result["catalog"])
                    else:
                        changed = Path(result["backup"])
                    changed.write_bytes(changed.read_bytes() + b"changed")
                    config = (home / "config.toml").read_bytes()
                    with self.assertRaises(tool.Error):
                        tool.restore(home)
                    self.assertEqual((home / "config.toml").read_bytes(), config)

    def test_restore_does_not_remove_unowned_external_file(self):
        result = self.install()
        record = Path(result["backup"])
        info = json.loads(record.read_bytes())
        info["catalog"] = "../unowned.json"
        record.write_text(json.dumps(info), encoding="utf-8")
        external = self.home / "unowned.json"
        external.write_bytes(b"keep")
        with self.assertRaises(tool.Error):
            tool.restore(self.home, str(record))
        self.assertEqual(external.read_bytes(), b"keep")

    def test_unowned_preexisting_catalog_is_not_overwritten(self):
        data, _ = tool.catalog_bytes((self.home / "models_cache.json").read_bytes())
        state = self.home / tool.STATE
        state.mkdir()
        (state / ("catalog-" + tool.digest(data) + ".json")).write_bytes(data)
        before = self.snapshot()
        with self.assertRaisesRegex(tool.Error, "unowned"):
            self.install()
        self.assertEqual(self.snapshot(), before)

    def test_changed_cache_creates_new_snapshot_and_restore_returns_previous(self):
        first = self.install()
        first_config = self.config.read_bytes()
        self.models.append({"slug": "example-new", "extra": 99})
        self.write_cache()
        second = self.install()
        self.assertNotEqual(first["catalog"], second["catalog"])
        tool.restore(self.home)
        self.assertEqual(self.config.read_bytes(), first_config)
        self.assertTrue(Path(first["catalog"]).exists())
        tool.restore(self.home)
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_config_absence_restores_to_absence(self):
        self.config.unlink()
        self.install()
        self.assertTrue(self.config.exists())
        tool.restore(self.home)
        self.assertFalse(self.config.exists())

    def test_config_changed_immediately_before_replace_is_preserved(self):
        original_atomic = tool.atomic
        later = self.before + b"# concurrent edit\n"

        def race(path, data, expected, home):
            if path == self.config:
                self.config.write_bytes(later)
            return original_atomic(path, data, expected, home)

        with patch.object(tool, "atomic", side_effect=race):
            with self.assertRaisesRegex(tool.Error, "not overwritten"):
                self.install()
        self.assertEqual(self.config.read_bytes(), later)
        self.assertEqual((self.home / tool.STATE / ".lock").read_bytes(), b"\0")
        self.assertFalse(list(self.home.rglob(".1m-*")))

    def test_failed_catalog_create_does_not_claim_other_file(self):
        original_atomic = tool.atomic

        def race(path, data, expected, home):
            if path.name.startswith("catalog-"):
                path.write_bytes(data)
            return original_atomic(path, data, expected, home)

        with patch.object(tool, "atomic", side_effect=race):
            with self.assertRaisesRegex(tool.Error, "not overwritten"):
                self.install()
        self.assertEqual(self.config.read_bytes(), self.before)
        with self.assertRaisesRegex(tool.Error, "unowned"):
            self.install()

    def test_restore_latest_ignores_failed_preparation(self):
        first = self.install()
        self.models.append({"slug": "example-new"})
        self.write_cache()
        original_atomic = tool.atomic

        def fail_config(path, data, expected, home):
            if path == self.config:
                raise tool.Error("Synthetic failure before config replacement")
            return original_atomic(path, data, expected, home)

        with patch.object(tool, "atomic", side_effect=fail_config):
            with self.assertRaises(tool.Error):
                self.install()
        tool.restore(self.home)
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertFalse(Path(first["catalog"]).exists())

    def test_atomic_creation_cannot_overwrite_racing_destination(self):
        path = self.home / "synthetic-file"
        original_link = os.link

        def race(source, destination):
            Path(destination).write_bytes(b"later file")
            return original_link(source, destination)

        with patch.object(tool.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                tool.atomic(path, b"candidate", None, self.home)
        self.assertEqual(path.read_bytes(), b"later file")

    def test_restore_config_concurrent_guard(self):
        self.install()
        original_atomic = tool.atomic
        later = self.config.read_bytes() + b"# concurrent restore edit\n"

        def race(path, data, expected, home):
            if path == self.config:
                self.config.write_bytes(later)
            return original_atomic(path, data, expected, home)

        with patch.object(tool, "atomic", side_effect=race):
            with self.assertRaisesRegex(tool.Error, "not overwritten"):
                tool.restore(self.home)
        self.assertEqual(self.config.read_bytes(), later)

    def test_restore_backup_changed_before_commit_is_refused(self):
        result = self.install()
        backup = Path(result["backup"]).with_name("config.before.toml")
        original_locked = tool.locked
        current = self.config.read_bytes()

        @contextmanager
        def race(home):
            with original_locked(home) as state:
                backup.write_bytes(backup.read_bytes() + b"# concurrent backup change\n")
                yield state

        with patch.object(tool, "locked", race):
            with self.assertRaisesRegex(tool.Error, "changed during preparation"):
                tool.restore(self.home)
        self.assertEqual(self.config.read_bytes(), current)

    def test_existing_utility_lock_blocks_mutation(self):
        with tool.locked(self.home):
            with self.assertRaisesRegex(tool.Error, "Another run"):
                self.install()
            self.assertEqual(self.config.read_bytes(), self.before)
        self.assertTrue(self.install()["changed"])

    def test_inactive_empty_or_nonempty_lock_does_not_block_or_truncate(self):
        state = self.home / tool.STATE
        state.mkdir()
        lock = state / ".lock"
        for contents in (b"", b"previous unrelated contents"):
            with self.subTest(contents=contents):
                lock.write_bytes(contents)
                with tool.locked(self.home):
                    pass
                self.assertEqual(lock.read_bytes(), contents or b"\0")
        self.assertTrue(self.install()["changed"])
        self.assertEqual(lock.read_bytes(), b"previous unrelated contents")

    def test_child_lock_blocks_then_process_death_releases_it(self):
        code = ('from pathlib import Path\nimport sys\nimport enable_1m as tool\n'
                'with tool.locked(Path(sys.argv[1])):\n'
                '    print("held", flush=True)\n'
                '    sys.stdin.read()\n')
        child = subprocess.Popen([sys.executable, "-u", "-c", code, str(self.home)],
                                 cwd=Path(tool.__file__).resolve().parent,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8")
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(ready.get(timeout=10), "held\n")
            with self.assertRaisesRegex(tool.Error, "Another run"):
                self.install()
            self.assertEqual(self.config.read_bytes(), self.before)
            child.kill()  # Only the exact child created by this test.
            child.wait(timeout=10)
            self.assertEqual((self.home / tool.STATE / ".lock").read_bytes(), b"\0")
            self.assertTrue(self.install()["changed"])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_hardlinked_or_nonregular_lock_is_refused_without_write(self):
        state = self.home / tool.STATE
        state.mkdir()
        lock = state / ".lock"
        original = self.home / "synthetic-file"
        original.write_bytes(b"keep these bytes")
        os.link(original, lock)
        with self.assertRaisesRegex(tool.Error, "hard-linked"):
            self.install()
        self.assertEqual(original.read_bytes(), b"keep these bytes")
        lock.unlink()
        lock.mkdir()
        with self.assertRaisesRegex(tool.Error, "nonregular"):
            self.install()

    def test_old_python_version_reports_requirement_before_tomllib_import(self):
        code = ('import runpy, sys\nsys.version_info = (3, 10, 0)\n'
                'runpy.run_path(sys.argv[1], run_name="__main__")\n')
        result = subprocess.run([sys.executable, "-c", code, str(Path(tool.__file__).resolve())],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"Python 3.11 or newer is required", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertNotIn(b"ModuleNotFoundError", result.stderr)

    def test_symlink_input_refused_when_platform_allows(self):
        real = self.home / "synthetic-original.toml"
        self.config.rename(real)
        try:
            self.config.symlink_to(real)
        except OSError:
            self.skipTest("This platform does not permit test symlinks")
        with self.assertRaisesRegex(tool.Error, "symbolic link"):
            self.install()
        self.assertEqual(real.read_bytes(), self.before)

    def test_safe_rejects_parent_traversal(self):
        with self.assertRaisesRegex(tool.Error, "outside"):
            tool.safe(self.home / "sub" / ".." / "config.toml", self.home)

    def test_windows_reparse_attribute_is_refused(self):
        original_lstat = Path.lstat

        def reparse(path):
            if path == self.config:
                return SimpleNamespace(st_file_attributes=0x400, st_mode=0o100600)
            return original_lstat(path)

        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(tool.Error, "junction"):
                self.install()
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_auth_file_is_never_read_or_contents_printed(self):
        (self.home / "auth.json").write_bytes(b"AUTH_SECRET_TEST_ONLY")
        original_read = Path.read_bytes

        def forbid_auth(path):
            if path.name == "auth.json":
                self.fail("Auth file read")
            return original_read(path)

        output = io.StringIO()
        with patch.object(Path, "read_bytes", forbid_auth), redirect_stdout(output):
            self.assertEqual(tool.main(["--home", str(self.home)]), 0)
        self.assertNotIn("SECRET_TEST_ONLY", output.getvalue())

    def test_unicode_home_cli_output_is_safe_for_ascii_console(self):
        unicode_home = self.home / "配置-उदाहरण-😀"
        unicode_home.mkdir()
        (unicode_home / "models_cache.json").write_bytes((self.home / "models_cache.json").read_bytes())
        environment = dict(os.environ, CODEX_HOME=str(unicode_home), PYTHONIOENCODING="ascii:strict")
        result = subprocess.run([sys.executable, str(Path(tool.__file__).resolve())],
                                env=environment, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        catalog = tool.parsed((unicode_home / "config.toml").read_bytes())["model_catalog_json"]
        self.assertIn("配置-उदाहरण-😀", catalog)
        self.assertEqual(json.loads(result.stdout)["catalog"], str(Path(catalog)))

    def test_explicit_home_overrides_environment_and_errors_hide_secrets(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "unused")}), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(tool.main(["--home", str(self.home), "--dry-run"]), 0)
            self.config.write_bytes(b'token = "MCP_SECRET_TEST_ONLY"\nx = [')
            self.assertEqual(tool.main(["--home", str(self.home)]), 1)
        self.assertNotIn("SECRET_TEST_ONLY", errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["config"], str(self.config))


if __name__ == "__main__":
    unittest.main()
