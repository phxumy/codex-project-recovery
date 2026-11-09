import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import codex_project_recovery as recovery


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(__file__).resolve().parents[1] / ".test-data"
        cls.temp_root.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if not any(cls.temp_root.iterdir()):
            cls.temp_root.rmdir()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "codex"
        self.work = self.root / "recovery"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.source = {
            "local-projects": {"legacy-project": {
                "id": "legacy-project", "name": "Synthetic example", "rootPaths": [str(self.project)],
                "createdAt": 1, "updatedAt": 2,
            }},
            recovery.MAP_KEY: {"local:" + str(self.home): {"legacy-project": "db-project"}},
            "thread-project-assignments": {
                "existing-thread": {"projectKind": "local", "projectId": "legacy-project"},
                "missing-thread": {"projectKind": "local", "projectId": "legacy-project"},
            },
            "project-order": ["legacy-project"],
            "thread-project-membership-host-ids": {"existing-thread": "local"},
        }
        self.state = {
            recovery.ACCOUNT_KEY: {"owner": "current-profile", "inactive": {
                "original-profile": {"atoms": {"keep": "original-ui"}, "globals": self.source},
                "another-profile": {"atoms": {"keep": "another-ui"}, "globals": {"setting": 1}},
            }},
            "electron-persisted-atom-state": {"theme": "dark"},
            "app-server-projects-migration-by-host": {"test": {"projectsMigrated": True}},
            "selected-remote-host-id": None,
        }
        self.save(self.state)
        (self.home / (recovery.STATE_NAME + ".bak")).write_bytes(b'{"old":"fallback"}')
        (self.home / "auth.json").write_bytes(b'{"synthetic":"credential-sentinel"}')
        (self.home / "session_index.jsonl").write_bytes(b'{"synthetic":"index-sentinel"}\n')
        connection = sqlite3.connect(self.home / recovery.DB_NAME)
        connection.executescript(
            "CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT);"
            "CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT);"
            "CREATE TABLE threads(id TEXT PRIMARY KEY);"
        )
        connection.execute("INSERT INTO projects VALUES (?,?)", ("db-project", "Synthetic example"))
        connection.execute("INSERT INTO project_roots VALUES (?,?,?)", ("db-project", 0, str(self.project)))
        connection.execute("INSERT INTO threads VALUES (?)", ("existing-thread",))
        connection.commit()
        connection.close()

    def save(self, value):
        (self.home / recovery.STATE_NAME).write_bytes(recovery.encode(value))

    def prepare(self):
        return recovery.prepare(self.home, self.work, 1)

    def test_preview_uses_explicit_ids_and_skips_missing_thread(self):
        before = (self.home / recovery.STATE_NAME).read_bytes()
        plan_path, summary = self.prepare()
        plan = json.loads(plan_path.read_bytes())
        self.assertEqual(summary["local_memberships"], 1)
        self.assertEqual(summary["skipped_memberships"], 1)
        self.assertEqual(plan["skipped"][0]["reason"], "local_thread_absent")
        self.assertEqual(set(plan["patch"]["thread-project-assignments"]), {"existing-thread"})
        self.assertEqual((self.home / recovery.STATE_NAME).read_bytes(), before)
        self.assertNotIn("original-profile", json.dumps(recovery.diagnose(self.home)))

    def test_success_preserves_other_accounts_credentials_and_database(self):
        untouched = {name: (self.home / name).read_bytes()
                     for name in (recovery.DB_NAME, "auth.json", "session_index.jsonl")}
        original = (self.home / recovery.STATE_NAME).read_bytes()
        plan, _ = self.prepare()
        report = recovery.apply_plan(self.home, plan, process_check=lambda: [])
        updated = json.loads((self.home / recovery.STATE_NAME).read_bytes())
        self.assertEqual(updated[recovery.ACCOUNT_KEY], self.state[recovery.ACCOUNT_KEY])
        for key, value in self.state.items():
            self.assertEqual(updated[key], value)
        self.assertEqual(len(updated["local-projects"]), 1)
        self.assertEqual(updated["thread-project-assignments"]["existing-thread"]["projectId"], "legacy-project")
        for name, data in untouched.items():
            self.assertEqual((self.home / name).read_bytes(), data)
        backup = Path(report["backup_directory"])
        self.assertEqual((backup / recovery.STATE_NAME).read_bytes(), original)
        connection = sqlite3.connect(backup / recovery.DB_NAME)
        self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        connection.close()
        self.assertEqual((self.home / recovery.STATE_NAME).read_bytes(),
                         (self.home / (recovery.STATE_NAME + ".bak")).read_bytes())

    def test_running_app_blocks_all_writes(self):
        plan, _ = self.prepare()
        before = (self.home / recovery.STATE_NAME).read_bytes()
        with self.assertRaisesRegex(recovery.RecoveryError, "Fully quit"):
            recovery.apply_plan(self.home, plan, process_check=lambda: [123])
        self.assertEqual((self.home / recovery.STATE_NAME).read_bytes(), before)
        self.assertFalse(list(plan.parent.glob("backup-*")))

    def test_changed_account_blocks_write(self):
        plan, _ = self.prepare()
        self.state[recovery.ACCOUNT_KEY]["owner"] = "different-profile"
        self.save(self.state)
        with self.assertRaisesRegex(recovery.RecoveryError, "account changed"):
            recovery.apply_plan(self.home, plan, process_check=lambda: [])

    def test_changed_source_blocks_write(self):
        plan, _ = self.prepare()
        self.source["local-projects"]["legacy-project"]["name"] = "Changed after preview"
        self.save(self.state)
        with self.assertRaisesRegex(recovery.RecoveryError, "saved source changed"):
            recovery.apply_plan(self.home, plan, process_check=lambda: [])

    def test_tampered_plan_is_recomputed(self):
        path, _ = self.prepare()
        plan = json.loads(path.read_bytes())
        plan["patch"]["selected-remote-host-id"] = "unexpected-change"
        path.write_bytes(recovery.encode(plan))
        with self.assertRaisesRegex(recovery.RecoveryError, "reviewed patch"):
            recovery.apply_plan(self.home, path, process_check=lambda: [])

    def test_nonempty_destination_is_not_overwritten(self):
        self.state["local-projects"] = {"already-here": {"name": "Keep me"}}
        self.save(self.state)
        with self.assertRaisesRegex(recovery.RecoveryError, "already has"):
            self.prepare()

    def test_project_mapping_mismatch_is_rejected(self):
        other = self.root / "other-folder"
        other.mkdir()
        self.source["local-projects"]["legacy-project"]["rootPaths"] = [str(other)]
        self.save(self.state)
        with self.assertRaisesRegex(recovery.RecoveryError, "differ from"):
            self.prepare()

    def test_cloud_linked_project_is_rejected(self):
        self.source["local-projects"]["legacy-project"]["chatGptBacking"] = {"projectId": "cloud"}
        self.save(self.state)
        with self.assertRaisesRegex(recovery.RecoveryError, "Cloud-linked"):
            self.prepare()

    def test_explicit_projectless_membership_is_preserved(self):
        self.state["projectless-thread-ids"] = ["existing-thread"]
        self.save(self.state)
        plan, summary = self.prepare()
        self.assertEqual(summary["local_memberships"], 0)
        self.assertEqual(summary["skipped_memberships"], 2)
        recovery.apply_plan(self.home, plan, process_check=lambda: [])
        updated = json.loads((self.home / recovery.STATE_NAME).read_bytes())
        self.assertEqual(updated["projectless-thread-ids"], ["existing-thread"])

    def test_shutdown_stashed_target_is_supported(self):
        plan, _ = self.prepare()
        self.state[recovery.ACCOUNT_KEY]["owner"] = None
        self.state[recovery.ACCOUNT_KEY]["inactive"]["current-profile"] = {
            "atoms": {"keep": "current-ui"}, "globals": {"unrelated": "preserve"}}
        self.save(self.state)
        recovery.apply_plan(self.home, plan, process_check=lambda: [])
        updated = json.loads((self.home / recovery.STATE_NAME).read_bytes())
        saved = updated[recovery.ACCOUNT_KEY]["inactive"]
        self.assertEqual(saved["original-profile"], self.state[recovery.ACCOUNT_KEY]["inactive"]["original-profile"])
        self.assertEqual(saved["current-profile"]["globals"]["unrelated"], "preserve")
        self.assertEqual(len(saved["current-profile"]["globals"]["local-projects"]), 1)
        self.assertNotIn("local-projects", updated)

    def test_state_race_during_backup_is_not_overwritten(self):
        plan, _ = self.prepare()
        real_backup = recovery.backup_database
        def concurrent_change(home, output):
            real_backup(home, output)
            changed = copy.deepcopy(self.state)
            changed["concurrent-change"] = True
            self.save(changed)
        with patch.object(recovery, "backup_database", concurrent_change):
            with self.assertRaisesRegex(recovery.RecoveryError, "state changed"):
                recovery.apply_plan(self.home, plan, process_check=lambda: [])
        actual = json.loads((self.home / recovery.STATE_NAME).read_bytes())
        self.assertTrue(actual["concurrent-change"])
        self.assertNotIn("local-projects", actual)

    def test_backup_failure_prevents_primary_write(self):
        plan, _ = self.prepare()
        before = (self.home / recovery.STATE_NAME).read_bytes()
        with patch.object(recovery, "backup_database", side_effect=OSError("Synthetic backup error")):
            with self.assertRaises(OSError):
                recovery.apply_plan(self.home, plan, process_check=lambda: [])
        self.assertEqual((self.home / recovery.STATE_NAME).read_bytes(), before)

    def test_remote_metadata_is_saved_without_claiming_remote_verification(self):
        self.source["remote-projects"] = [{"id": "remote-project", "hostId": "ssh:example", "remotePath": "/example"}]
        self.source["thread-project-assignments"]["remote-thread"] = {
            "projectKind": "remote", "projectId": "remote-project", "hostId": "ssh:example"}
        self.save(self.state)
        plan, summary = self.prepare()
        self.assertEqual(summary["saved_remote_memberships"], 1)
        self.assertEqual(summary["remote_projects"], 1)


if __name__ == "__main__":
    unittest.main()
