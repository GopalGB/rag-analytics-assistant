"""Back up and restore the assistant's data and state.

    python scripts/backup.py create [--out backups/] [--include-env] [--no-data]
    python scripts/backup.py verify backups/assistant-backup-<ts>.tar.gz
    python scripts/backup.py restore backups/assistant-backup-<ts>.tar.gz [--force]

A backup is a .tar.gz containing:
    data/      the documents folder (DATA_DIR), unless --no-data
    storage/   reviews, approvals, activity log, database, OAuth tokens (caches are skipped: rebuildable)
    manifest.json  file list with SHA-256 checksums, app version, and the activity-log integrity result

The activity log's hash chain is verified before backing up and again on restore. `.env` holds API
keys and is only included with --include-env; keep such backups encrypted (e.g. on a FileVault disk).
Restore refuses to overwrite existing state unless --force, and verifies every checksum first.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import __version__  # noqa: E402
from app.audit import AuditLog  # noqa: E402
from app.config import Settings  # noqa: E402

# Rebuildable caches under STORAGE_DIR only. DATA_DIR is the source of truth and is copied in full,
# including any folder that happens to be called "cache".
STORAGE_SKIP = {"cache"}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _files(root: Path, skip_top: set[str] | frozenset[str] = frozenset()) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.relative_to(root).parts[0] not in skip_top)


def create(out_dir: Path, include_env: bool, include_data: bool, settings: Settings) -> Path:
    storage, data = Path(settings.storage_dir), Path(settings.data_dir)
    audit = AuditLog(storage / "audit.jsonl").verify() if (storage / "audit.jsonl").exists() else {"ok": True, "entries": 0}
    if not audit["ok"]:
        print(f"WARNING: activity log integrity check failed at entry {audit.get('broken_at')}; backing up anyway.")
    entries: list[tuple[str, Path]] = [(f"storage/{p.relative_to(storage).as_posix()}", p) for p in _files(storage, STORAGE_SKIP)]
    if include_data:
        entries += [(f"data/{p.relative_to(data).as_posix()}", p) for p in _files(data)]
    if include_env and (ROOT / ".env").exists():
        entries.append((".env", ROOT / ".env"))
    manifest = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app_version": __version__,
        "activity_log": audit,
        "includes_env": include_env,
        "files": {name: {"sha256": _sha(p), "bytes": p.stat().st_size} for name, p in entries},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"assistant-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        raw = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for name, p in entries:
            tar.add(p, arcname=name)
    target.chmod(0o600)
    print(f"Backup written: {target} ({len(entries)} files, activity log {'OK' if audit['ok'] else 'BROKEN'})")
    return target


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise SystemExit(f"Refusing unsafe archive member: {m.name}")
        members.append(m)
    return members


def verify(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        members = {m.name: m for m in _safe_members(tar)}
        if "manifest.json" not in members:
            raise SystemExit("Not a backup made by this tool: manifest.json is missing.")
        manifest = json.loads(tar.extractfile(members["manifest.json"]).read())
        # Every file in the archive must be listed (and checksummed) in the manifest, and vice versa:
        # an extra, unlisted member would otherwise be restored without any check.
        files = {n for n, m in members.items() if m.isfile() and n != "manifest.json"}
        extra = sorted(files - set(manifest["files"]))
        if extra:
            raise SystemExit(f"Archive contains files not listed in its manifest: {extra[:10]}")
        bad = []
        for name, meta in manifest["files"].items():
            f = tar.extractfile(members[name]) if name in members else None
            if f is None or hashlib.sha256(f.read()).hexdigest() != meta["sha256"]:
                bad.append(name)
    if bad:
        raise SystemExit(f"Checksum mismatch or missing files: {bad[:10]}")
    print(f"Backup OK: {len(manifest['files'])} files verified (created {manifest['created']}, v{manifest['app_version']}).")
    return manifest


def _destination(name: str, storage: Path, data: Path) -> Path | None:
    if name.startswith("storage/"):
        return storage / name[len("storage/"):]
    if name.startswith("data/"):
        return data / name[len("data/"):]
    if name == ".env":
        return ROOT / ".env"
    return None


def restore(archive: Path, force: bool, settings: Settings) -> None:
    manifest = verify(archive)
    storage, data = Path(settings.storage_dir), Path(settings.data_dir)
    with tarfile.open(archive, "r:gz") as tar:
        plan = [(m, dest) for m in _safe_members(tar)
                if m.isfile() and m.name != "manifest.json" and (dest := _destination(m.name, storage, data))]
        # Check every destination before writing anything, so a refusal never leaves a half-restored state.
        existing = [str(dest) for _, dest in plan if dest.exists()]
        if not force and (any(storage.glob("*")) or existing):
            where = f"{storage} is not empty" if any(storage.glob("*")) else f"{len(existing)} file(s) already exist"
            raise SystemExit(f"{where} (e.g. {(existing or [str(storage)])[0]}). "
                             "Stop the app and re-run with --force to overwrite.")
        for m, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tar.extractfile(m).read())
            if m.name in (".env",) or "secrets" in m.name:
                dest.chmod(0o600)
    audit = AuditLog(storage / "audit.jsonl").verify() if (storage / "audit.jsonl").exists() else {"ok": True}
    print(f"Restored {len(manifest['files'])} files. Activity log integrity: {'OK' if audit['ok'] else 'BROKEN'}. "
          "Start the app to rebuild the search index.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--out", default="backups")
    c.add_argument("--include-env", action="store_true")
    c.add_argument("--no-data", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("archive")
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("--force", action="store_true")
    args = ap.parse_args()
    settings = Settings()
    if args.cmd == "create":
        create(Path(args.out), args.include_env, not args.no_data, settings)
    elif args.cmd == "verify":
        verify(Path(args.archive))
    else:
        restore(Path(args.archive), args.force, settings)


if __name__ == "__main__":
    main()
