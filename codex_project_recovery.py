"""Restore a saved local Codex project catalog into an empty account profile.

Windows desktop helper; Python 3.10+, standard library only. No network calls.
The on-disk Codex format is internal and may change between app versions.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import json
import ntpath
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

VERSION = "0.1.0"
ACCOUNT_KEY = "electron-account-renderer-state-v1"
MAP_KEY = "app-server-project-id-by-legacy-project-id-by-host"
SOURCE_KEYS = (
    "local-projects", "remote-projects", MAP_KEY, "thread-project-assignments",
    "project-order", "thread-project-membership-host-ids", "project-appearances",
    "sidebar-project-thread-orders", "pinned-project-ids",
)
STATE_NAME = ".codex-global-state.json"
DB_NAME = "state_5.sqlite"


class RecoveryError(Exception):
    """A precondition failed; do not guess or expand the recovery scope."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def source_fingerprint(source):
    return fingerprint({key: source.get(key) for key in SOURCE_KEYS})


def atomic_write(path: Path, data: bytes):
    """Stage beside the destination so replacement stays on the same volume."""
    path = Path(path)
    if path.is_symlink():
        raise RecoveryError("Refusing to replace a symbolic-link destination.")
    fd, name = tempfile.mkstemp(prefix=".project-recovery-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_state(home: Path):
    path = home / STATE_NAME
    if path.is_symlink():
        raise RecoveryError("A symbolic-link state file needs manual review.")
    raw = path.read_bytes()
    state = json.loads(raw)
    account = state.get(ACCOUNT_KEY) if isinstance(state, dict) else None
    if not isinstance(account, dict) or not isinstance(account.get("inactive"), dict):
        raise RecoveryError("Unsupported state format: no saved account profiles.")
    return raw, state


def sources(state):
    return [(owner, value["globals"])
            for owner, value in state[ACCOUNT_KEY]["inactive"].items()
            if isinstance(value, dict) and isinstance(value.get("globals"), dict)
            and value["globals"].get("local-projects")]


def read_database(home: Path):
    connection = sqlite3.connect((home / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryError("The state database failed its integrity check.")
        projects = {}
        for project_id, name in connection.execute("SELECT id,name FROM projects"):
            roots = [row[0] for row in connection.execute(
                "SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (project_id,))]
            projects[project_id] = {"name": name, "roots": roots}
        threads = {row[0] for row in connection.execute("SELECT id FROM threads")}
        return projects, threads
    finally:
        connection.close()


def normalized(path):
    return ntpath.normcase(ntpath.normpath(path))


def build_patch(target, source, db_projects, db_threads):
    """Validate saved IDs and explicit memberships; never infer them from cwd."""
    if any(target.get(key) for key in ("local-projects", "remote-projects", "thread-project-assignments")):
        raise RecoveryError("The target already has projects or memberships; automatic merging is not supported.")
    local = source.get("local-projects", {})
    remote = source.get("remote-projects", [])
    if not isinstance(local, dict) or not local or not isinstance(remote, list):
        raise RecoveryError("The source has no supported local project catalog.")
    mappings, validated = {}, {}
    for host, mapping in source.get(MAP_KEY, {}).items():
        if not host.startswith("local:"):
            continue
        subset = {}
        for legacy_id, server_id in mapping.items():
            if legacy_id not in local:
                continue
            project, stored = local[legacy_id], db_projects.get(server_id)
            if project.get("id") != legacy_id or stored is None:
                raise RecoveryError("An original project ID is missing from the database.")
            if any(key in project for key in ("chatGptBacking", "chatGptBackingCreation", "retiredChatGptBacking")):
                raise RecoveryError("Cloud-linked project ownership requires separate review.")
            roots = project.get("rootPaths")
            if not isinstance(roots, list) or not roots:
                raise RecoveryError("A local project has no supported folder roots.")
            if list(map(normalized, roots)) != list(map(normalized, stored["roots"])):
                raise RecoveryError("Saved project folders differ from the database.")
            if any(not Path(root).is_dir() for root in roots):
                raise RecoveryError("A source project folder is unavailable.")
            if legacy_id in validated and validated[legacy_id] != server_id:
                raise RecoveryError("A project has conflicting database mappings.")
            subset[legacy_id] = server_id
            validated[legacy_id] = server_id
        if subset:
            mappings[host] = subset
    if set(validated) != set(local):
        raise RecoveryError("Some source projects lack a validated database mapping.")
    remote_by_id = {}
    for project in remote:
        if not all(isinstance(project.get(key), str) and project[key] for key in ("id", "hostId", "remotePath")):
            raise RecoveryError("Unsupported saved remote project metadata.")
        if project["id"] in remote_by_id:
            raise RecoveryError("Duplicate remote project IDs require review.")
        remote_by_id[project["id"]] = project
    assignments, skipped = {}, []
    for thread_id, assignment in source.get("thread-project-assignments", {}).items():
        kind, project_id = assignment.get("projectKind"), assignment.get("projectId")
        reason = None
        if thread_id in target.get("projectless-thread-ids", []):
            reason = "currently_projectless"
        elif assignment.get("projectOrigin") == "chatgpt":
            reason = "cloud_membership"
        elif kind == "local" and project_id in validated:
            if thread_id not in db_threads:
                reason = "local_thread_absent"
        elif kind == "remote" and project_id in remote_by_id:
            if assignment.get("hostId", remote_by_id[project_id]["hostId"]) != remote_by_id[project_id]["hostId"]:
                reason = "remote_host_mismatch"
        else:
            reason = "unvalidated_project"
        if reason:
            skipped.append({"thread_id": thread_id, "reason": reason})
        else:
            assignments[thread_id] = copy.deepcopy(assignment)
    project_ids = set(local) | set(remote_by_id)
    order = list(dict.fromkeys(p for p in source.get("project-order", []) if p in project_ids))
    order.extend(p for p in list(local) + list(remote_by_id) if p not in order)
    patch = {"local-projects": copy.deepcopy(local), "remote-projects": copy.deepcopy(remote),
             MAP_KEY: mappings, "project-order": order, "thread-project-assignments": assignments}
    for key in ("project-appearances", "sidebar-project-thread-orders"):
        values = {k: v for k, v in source.get(key, {}).items() if k in project_ids}
        if values:
            patch[key] = {**copy.deepcopy(values), **target.get(key, {})}
    hosts = {tid: host for tid, host in source.get("thread-project-membership-host-ids", {}).items()
             if tid in assignments}
    if hosts:
        patch["thread-project-membership-host-ids"] = {**hosts, **target.get("thread-project-membership-host-ids", {})}
    pins = [p for p in source.get("pinned-project-ids", []) if p in project_ids]
    if pins:
        patch["pinned-project-ids"] = list(dict.fromkeys(target.get("pinned-project-ids", []) + pins))
    summary = {"local_projects": len(local), "remote_projects": len(remote),
               "local_memberships": sum(a["projectKind"] == "local" for a in assignments.values()),
               "saved_remote_memberships": sum(a["projectKind"] == "remote" for a in assignments.values()),
               "skipped_memberships": len(skipped)}
    return patch, summary, skipped


def prepare(home: Path, work_dir: Path, source_number: int):
    _, state = read_state(home)
    target_owner = state[ACCOUNT_KEY].get("owner")
    if not target_owner:
        raise RecoveryError("Sign in to the destination account before preparing a plan.")
    candidates = sources(state)
    if not 1 <= source_number <= len(candidates):
        raise RecoveryError("Select a source number from diagnose.")
    source_owner, source = candidates[source_number - 1]
    projects, threads = read_database(home)
    patch, summary, skipped = build_patch(state, source, projects, threads)
    work_dir.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=work_dir))
    plan = {"format_version": 1, "tool_version": VERSION, "codex_home": str(home),
            "target_owner": target_owner, "source_owner": source_owner,
            "source_fingerprint": source_fingerprint(source), "patch": patch,
            "summary": summary, "skipped": skipped}
    path = run / "restore-plan.json"
    atomic_write(path, encode(plan))
    return path, summary


def desktop_pids():
    if os.name != "nt":
        raise RecoveryError("Applying recovery is supported on Windows only.")
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid:
            if entry.szExeFile.casefold() in ("chatgpt.exe", "codex.exe", "codex-desktop.exe"):
                found.append(entry.th32ProcessID)
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return found


def backup_database(home: Path, output: Path):
    source = sqlite3.connect((home / DB_NAME).as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(output)
    try:
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryError("The database backup failed validation.")
    finally:
        destination.close()
        source.close()


def apply_plan(home: Path, plan_path: Path, process_check=desktop_pids):
    if process_check():
        raise RecoveryError("Fully quit ChatGPT/Codex before applying recovery.")
    plan = json.loads(plan_path.read_bytes())
    if plan.get("format_version") != 1 or Path(plan.get("codex_home", "")).resolve() != home:
        raise RecoveryError("This plan does not match the selected Codex home or format.")
    raw, state = read_state(home)
    original = copy.deepcopy(state)
    account = state[ACCOUNT_KEY]
    source = account["inactive"].get(plan["source_owner"], {}).get("globals")
    if source is None or source_fingerprint(source) != plan["source_fingerprint"]:
        raise RecoveryError("The saved source changed; prepare a fresh plan.")
    target = state
    stashed = False
    if account.get("owner") != plan["target_owner"]:
        if account.get("owner") is None and plan["target_owner"] in account["inactive"]:
            target = account["inactive"][plan["target_owner"]]["globals"]
            stashed = True
        else:
            raise RecoveryError("The destination account changed; no write was made.")
    projects, threads = read_database(home)
    patch, summary, _ = build_patch(target, source, projects, threads)
    if fingerprint(patch) != fingerprint(plan["patch"]):
        raise RecoveryError("The reviewed patch no longer matches current data.")
    backup = Path(tempfile.mkdtemp(prefix="backup-", dir=plan_path.parent))
    (backup / STATE_NAME).write_bytes(raw)
    fallback = home / (STATE_NAME + ".bak")
    if fallback.is_symlink():
        raise RecoveryError("A symbolic-link backup state needs manual review.")
    if fallback.exists():
        (backup / fallback.name).write_bytes(fallback.read_bytes())
    backup_database(home, backup / DB_NAME)
    target.update(patch)
    if not stashed and account != original[ACCOUNT_KEY]:
        raise RecoveryError("Account-state preservation check failed.")
    if stashed:
        for owner, saved in original[ACCOUNT_KEY]["inactive"].items():
            if owner != plan["target_owner"] and account["inactive"][owner] != saved:
                raise RecoveryError("Another account state would be modified.")
    if process_check() or (home / STATE_NAME).read_bytes() != raw:
        raise RecoveryError("The app restarted or state changed during backup; no write was made.")
    updated = encode(state)
    atomic_write(home / STATE_NAME, updated)
    if (home / STATE_NAME).read_bytes() != updated:
        raise RecoveryError("Post-write verification failed; original backups are retained.")
    fallback_synced = True
    try:
        atomic_write(fallback, updated)
    except OSError:
        # The primary write succeeded: do not falsely report that nothing changed.
        fallback_synced = False
    report = {"stage": "restored", **summary, "backup_directory": str(backup),
              "live_database_modified": False, "fallback_state_updated": fallback_synced,
              "sidebar_verification_pending": True}
    atomic_write(plan_path.parent / "restore-status.json", encode(report))
    return report


def diagnose(home: Path):
    _, state = read_state(home)
    projects, threads = read_database(home)
    return {"active_local_projects": len(state.get("local-projects", {})),
            "active_remote_projects": len(state.get("remote-projects", [])),
            "active_memberships": len(state.get("thread-project-assignments", {})),
            "database_projects": len(projects), "database_threads": len(threads),
            "sources": [{"source": i, "local_projects": len(g.get("local-projects", {})),
                         "remote_projects": len(g.get("remote-projects", [])),
                         "saved_memberships": len(g.get("thread-project-assignments", {}))}
                        for i, (_, g) in enumerate(sources(state), 1)]}


def wait_and_apply(home, plan, wait_seconds, reopen=None):
    if wait_seconds < 0:
        raise RecoveryError("Wait duration must not be negative.")
    if reopen is not None and (reopen.suffix.lower() != ".exe" or not reopen.is_file()):
        raise RecoveryError("--reopen must name an existing Windows app executable.")
    deadline = time.monotonic() + wait_seconds
    while desktop_pids():
        if time.monotonic() >= deadline:
            raise RecoveryError("Timed out waiting for ChatGPT/Codex to exit; no write was made.")
        time.sleep(1)
    if wait_seconds:
        time.sleep(2)
    report = apply_plan(home, plan)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if reopen is not None:
        os.startfile(str(reopen))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--work-dir", type=Path, default=Path(__file__).resolve().parent / ".recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("diagnose", help="Read-only inspection, without private account identifiers")
    prepare_parser = commands.add_parser("prepare", help="Validate and save a local recovery plan")
    prepare_parser.add_argument("--source", type=int, required=True)
    apply_parser = commands.add_parser("apply", help="Apply a reviewed plan after the app exits")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--wait-seconds", type=int, default=0)
    apply_parser.add_argument("--reopen", type=Path)
    commands.add_parser("interactive", help="Select a saved source, preview, then wait for app exit")
    args = parser.parse_args(argv)
    home, work = args.codex_home.expanduser().resolve(), args.work_dir.expanduser().resolve()
    try:
        if args.command == "diagnose":
            print(json.dumps(diagnose(home), ensure_ascii=False, indent=2))
        elif args.command == "prepare":
            plan, summary = prepare(home, work, args.source)
            print(json.dumps({"plan": str(plan), **summary}, ensure_ascii=False, indent=2))
        elif args.command == "apply":
            wait_and_apply(home, args.plan.resolve(), args.wait_seconds,
                           args.reopen.resolve() if args.reopen else None)
        else:
            data = diagnose(home)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if not data["sources"]:
                raise RecoveryError("No saved source profile is available.")
            selection = input("Source number / 源账号编号: ").strip()
            plan, summary = prepare(home, work, int(selection))
            print(json.dumps({"plan": str(plan), **summary}, ensure_ascii=False, indent=2))
            choice = input("Restore into the CURRENT account? / 恢复到当前账号？[y/N]: ")
            if choice.strip().lower() != "y":
                print("Cancelled; Codex state was not modified. / 已取消，未修改 Codex 状态。")
                return 0
            print("Keep this terminal open. Fully quit ChatGPT/Codex within 30 minutes.\n"
                  "保留此窗口，在 30 分钟内通过 文件 → 退出 ChatGPT 完全退出应用。", flush=True)
            wait_and_apply(home, plan, 1800)
            print("Recovery saved. Reopen the desktop app to verify. / 恢复已写入，请重新打开应用核对。")
        return 0
    except (RecoveryError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"Recovery stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
