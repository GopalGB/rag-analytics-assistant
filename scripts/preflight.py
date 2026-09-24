"""Pre-deployment check: is this configuration safe and complete for where it's going?

    python scripts/preflight.py                          # local Mac install (reads .env)
    python scripts/preflight.py --target docker
    python scripts/preflight.py --target public-demo     # hosted demo (run with the host's env vars set)

Prints PASS / WARN / FAIL per check and exits 1 if anything FAILs. It never prints secret values.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.security.output_filter import _LEAK_PATTERNS  # noqa: E402

CLOUD_KEYS = ("anthropic", "openai", "gemini", "openrouter", "azure_openai", "groq", "mistral", "deepseek",
              "together", "xai")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, message: str, level: str = "FAIL") -> None:
        tag = "PASS" if ok else level
        self.failed += (not ok) and level == "FAIL"
        print(f"  [{tag}] {message}")


def tracked_files_with_secrets() -> list[str]:
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except Exception:
        return []
    hits = []
    patterns = [p for p in _LEAK_PATTERNS if "SECURITY DIRECTIVE" not in p.pattern and "api[_-]?key" not in p.pattern]
    for name in files:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 1_000_000 or name.startswith("tests/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(p.search(text) for p in patterns):
            hits.append(name)
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["local", "docker", "public-demo"], default="local")
    target = ap.parse_args().target
    s = Settings()
    r = Report()
    print(f"Preflight for target: {target}")

    print("Secrets")
    leaked = tracked_files_with_secrets()
    r.check(not leaked, "no credential-shaped strings in tracked files" + (f" (found in: {', '.join(leaked)})" if leaked else ""))
    env = ROOT / ".env"
    if env.exists():
        mode = stat.S_IMODE(env.stat().st_mode)
        r.check(mode & 0o077 == 0, f".env is readable only by its owner (mode {oct(mode)}; run chmod 600 .env)", "WARN")
    tracked_env = subprocess.run(["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    r.check(not tracked_env, ".env is not committed")
    if s.app_api_key:
        r.check(len(s.app_api_key) >= 24, "APP_API_KEY is at least 24 characters (use: python -c \"import secrets; "
                "print(secrets.token_urlsafe(32))\")")

    print("Configuration")
    for w in s.warnings():
        r.check(False, w, "WARN")
    cloud = [k for k in CLOUD_KEYS if getattr(s, f"{k}_api_key", None)]
    if cloud and not s.allow_cloud_ai:
        r.check(False, f"cloud keys set ({', '.join(cloud)}) but ALLOW_CLOUD_AI is false: they won't be used", "WARN")

    if target in ("docker", "public-demo"):
        r.check(bool(s.app_api_key), "APP_API_KEY is set (requests from outside the machine must carry it)")
        r.check(not s.trust_loopback, "TRUST_LOOPBACK=false (behind a proxy every request looks local)")

    if target == "public-demo":
        hosts = set(s.allowed_host_list())
        r.check(s.public_demo, "PUBLIC_DEMO=true (read-only, no conversation memory)")
        r.check(s.db_path == ":memory:", "DB_PATH=:memory: (serverless filesystems are read-only)")
        r.check(str(s.storage_dir).startswith("/tmp"), "STORAGE_DIR is under /tmp (the only writable path)")
        r.check(not s.auto_reindex, "AUTO_REINDEX=false (no file watcher on a serverless function)")
        r.check(bool(hosts - LOCAL_HOSTS) and "*" not in hosts, "ALLOWED_HOSTS lists the public host name(s)")
        r.check(bool(cloud) and s.allow_cloud_ai, "a hosted model is configured (provider key + ALLOW_CLOUD_AI=true)")
        bundled = (ROOT / "data" / "sample").resolve()
        r.check(Path(s.data_dir).resolve() == bundled,
                "DATA_DIR is the bundled synthetic/public sample (never deploy company files to the demo)")
        r.check(bool(s.allowed_origin_list()), "ALLOWED_ORIGINS names the site that serves the UI (if proxied)", "WARN")
    else:
        r.check(not s.public_demo, "PUBLIC_DEMO is off for a private install", "WARN")

    print("Result: " + ("FAIL" if r.failed else "OK") + f" ({r.failed} failing check(s))")
    sys.exit(1 if r.failed else 0)


if __name__ == "__main__":
    if "--help" not in sys.argv and not os.environ.get("PREFLIGHT_NO_DOTENV"):
        os.chdir(ROOT)  # Settings reads .env from the working directory
    main()
