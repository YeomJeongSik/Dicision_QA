"""Preview and stage a new plugin without modifying an existing UE project file."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from qa_mock import Rejected

PLUGIN = "GameQABridge"
MANIFEST = ".qa-install.json"
STAGE = ".unrealautoqa-transaction"


def check_regular(path):
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise Rejected("PROJECT_REPARSE_POINT")
    return info


def file_hash(path):
    if not stat.S_ISREG(check_regular(path).st_mode):
        raise Rejected("PROJECT_NONREGULAR_FILE")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hashes(root, *, skip_plugin_outputs=False):
    if not stat.S_ISDIR(check_regular(root).st_mode):
        raise Rejected("PROJECT_NOT_DIRECTORY")
    files = {}
    def fail(error):
        raise error

    for folder, dirs, names in os.walk(root, onerror=fail):
        base = Path(folder)
        if (skip_plugin_outputs and any(name.lower().endswith(".uplugin") for name in names)
                and any(name.lower() == "source" for name in dirs)):
            dirs[:] = [name for name in dirs if name.lower() not in
                       {"binaries", "intermediate", "saved", "deriveddatacache"}]
        for name in dirs:
            check_regular(base / name)
        for name in names:
            path = base / name
            files[path.relative_to(root).as_posix()] = file_hash(path)
    return files


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def project_input_hashes(project):
    if project.suffix.lower() != ".uproject":
        raise Rejected("UPROJECT_REQUIRED")
    files = {project.name: file_hash(project)}
    for name in ("Source", "Content", "Config", "Plugins"):
        folder = project.parent / name
        if os.path.lexists(folder):
            hashes = tree_hashes(folder, skip_plugin_outputs=name == "Plugins")
            files.update({f"{name}/{path}": digest for path, digest in hashes.items()})
    return files


def project_fingerprint(project, engine_build):
    files = project_input_hashes(project)
    return {"engine_build": engine_build, "fingerprint": canonical_hash([engine_build, files]),
            "file_count": len(files)}


def copy_project_inputs(project, destination):
    files = project_input_hashes(project)
    destination.mkdir(parents=True)
    for relative, digest in files.items():
        source = project.parent / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        check_regular(source)
        shutil.copyfile(source, target)
        if file_hash(target) != digest:
            raise Rejected("BUILD_INPUT_CHANGED_DURING_BUILD")
    return {"file_count": len(files)}


def plugin_preview(project, source=None):
    if project.suffix.lower() != ".uproject":
        raise Rejected("UPROJECT_REQUIRED")
    project_sha256 = file_hash(project)
    files = {}
    metadata = {}
    if source is not None:
        files = tree_hashes(source)
        descriptor = f"{PLUGIN}.uplugin"
        if descriptor not in files or MANIFEST in files:
            raise Rejected("INVALID_PLUGIN_SOURCE")
        metadata = json.loads((source / descriptor).read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise Rejected("INVALID_PLUGIN_SOURCE")
    plugins = project.parent.resolve() / "Plugins"
    if os.path.lexists(plugins):
        check_regular(plugins)
    target = plugins / PLUGIN
    operation = "install_new_plugin" if source is not None else None
    plan = {"project_canonical_path": str(project.resolve()), "project_sha256": project_sha256,
            "target": str(target), "plugin_version": metadata.get("VersionName", metadata.get("Version")),
            "file_hashes_before": {}, "file_hashes_after": files, "backup_location": None,
            "approved_operations": []}
    if os.path.lexists(target):
        existing = tree_hashes(target)
        if MANIFEST not in existing:
            raise Rejected("PLUGIN_DESTINATION_CONFLICT")
        installed = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
        del existing[MANIFEST]
        if (not isinstance(installed, dict) or installed.get("target") != str(target)
                or installed.get("file_hashes_after") != existing):
            raise Rejected("PLUGIN_DESTINATION_CONFLICT")
        plan["file_hashes_before"] = existing
        if source is None:
            operation = "remove_plugin"
        elif existing != files:
            operation = "upgrade_plugin"
        else:
            operation = None
        if operation:
            plan["backup_location"] = str(target.parent.parent / STAGE / "backup")
    if operation:
        plan["approved_operations"] = [operation]
    return {**plan, "approval": canonical_hash(plan)}


@contextmanager
def project_lock(root):
    path = root / ".unrealautoqa.lock"
    if os.path.lexists(path):
        check_regular(path)
    with path.open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Rejected("PLUGIN_TRANSACTION_BUSY") from None
        yield


def recover_stage(root, target):
    stage = root / STAGE
    if not os.path.lexists(stage):
        return False
    files = tree_hashes(stage)
    journal = stage / "journal.json"
    if "journal.json" not in files:
        if set(p.name for p in stage.iterdir()) - {"journal.tmp"}:
            raise Rejected("PLUGIN_STAGE_CONFLICT")
        shutil.rmtree(stage)
        return True
    record = json.loads(journal.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("target") != str(target):
        raise Rejected("PLUGIN_STAGE_CONFLICT")
    if set(p.name for p in stage.iterdir()) - {"journal.json", "new", "backup", "committed"}:
        raise Rejected("PLUGIN_STAGE_CONFLICT")
    backup = stage / "backup"
    if backup.exists() and "committed" not in files:
        if not os.path.lexists(target):
            backup.rename(target)
        else:
            marker = target / MANIFEST
            check_regular(target)
            if not marker.exists() or MANIFEST not in tree_hashes(target):
                raise Rejected("PLUGIN_RECOVERY_CONFLICT")
            installed = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(installed, dict) or installed.get("installation_id") != record.get("installation_id"):
                raise Rejected("PLUGIN_RECOVERY_CONFLICT")
    shutil.rmtree(stage)
    return True


def install_plugin(project, source, approval, cancelled=lambda: False):
    if project.suffix.lower() != ".uproject":
        raise Rejected("UPROJECT_REQUIRED")
    file_hash(project)
    root = project.parent.resolve()
    target = root / "Plugins" / PLUGIN
    with project_lock(root):
        if os.path.lexists(target.parent):
            check_regular(target.parent)
        recover_stage(root, target)
        plan = plugin_preview(project, source)
        if plan["approval"] != approval:
            raise Rejected("PLUGIN_APPROVAL_MISMATCH")
        if not plan["approved_operations"]:
            return plan
        target.parent.mkdir(exist_ok=True)
        manifest = {**plan, "installation_id": uuid4().hex}
        stage = root / STAGE
        stage.mkdir()
        journal = stage / "journal.tmp"
        journal.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        journal.replace(stage / "journal.json")
        try:
            staged = stage / "new"
            if source is not None:
                staged.mkdir()
                for name in plan["file_hashes_after"]:
                    if cancelled():
                        raise Rejected("PLUGIN_INSTALL_CANCELLED")
                    destination = staged / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    origin = source / name
                    check_regular(origin)
                    shutil.copyfile(origin, destination)
                if tree_hashes(staged) != plan["file_hashes_after"]:
                    raise Rejected("PLUGIN_INPUT_CHANGED")
                (staged / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if cancelled():
                raise Rejected("PLUGIN_INSTALL_CANCELLED")
            if plugin_preview(project, source)["approval"] != approval:
                raise Rejected("PLUGIN_INPUT_CHANGED")
            if plan["file_hashes_before"]:
                target.rename(stage / "backup")
            if source is not None:
                staged.rename(target)
            (stage / "committed").touch()
        finally:
            recover_stage(root, target)
        return manifest


def recover_project(project):
    if project.suffix.lower() != ".uproject":
        raise Rejected("UPROJECT_REQUIRED")
    file_hash(project)
    root = project.parent.resolve()
    target = root / "Plugins" / PLUGIN
    with project_lock(root):
        if os.path.lexists(target.parent):
            check_regular(target.parent)
        return {"recovered": recover_stage(root, target)}


def local_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1000 or "\x00" in value:
        raise Rejected("LOCAL_PATH_REQUIRED")
    path = Path(value)
    if not path.is_absolute() or path.drive.startswith(("\\", "/")):
        raise Rejected("LOCAL_PATH_REQUIRED")
    return path


def project_candidates():
    home = Path.home()
    root = home / "Documents" / "Unreal Projects"
    return [str(path) for path in sorted(root.glob("*/*.uproject"))[:50] if path.is_file()]


def inspect_project(project, script):
    if project.suffix.lower() != ".uproject" or not stat.S_ISREG(check_regular(project).st_mode):
        raise Rejected("UPROJECT_REQUIRED")
    descriptor = json.loads(project.read_text(encoding="utf-8-sig"))
    if not isinstance(descriptor, dict):
        raise Rejected("INVALID_UPROJECT")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-Project", str(project)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise Rejected("DOCTOR_TIMEOUT") from None
    if result.returncode:
        raise Rejected("DOCTOR_FAILED")
    diagnostics = json.loads(result.stdout.decode("utf-8-sig"))
    return {"status": "NEEDS_USER_ACTION" if diagnostics["missing"] else "PREVIEW_CHANGES",
            "engine_association": descriptor.get("EngineAssociation"),
            "project_type": "C++" if descriptor.get("Modules") else "Blueprint",
            "diagnostics": diagnostics}
