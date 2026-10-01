#!/usr/bin/env python3
"""
Obsidian → NotebookLM Sync (multi-vault, Google Docs output)

For every vault listed in vaults.json:
  1. Merges each top-level topic folder into ONE Google Doc (auto-splits into
     "(v2)", "(v3)"... if it exceeds Google Docs' size limit).
  2. Merges the vault structure map + every standalone root note into a
     SINGLE "Root Notes" Doc, structure first, so a new root note or a new
     root-note edit never requires touching NotebookLM.
  3. Embeds all new images directly into ONE "Images" Doc per vault (with a
     path annotation above each), instead of a PDF per topic — so images get
     NotebookLM's native auto-refresh too. Rolls over to "(v2)" after 150
     images. See docs_sync.py for the privacy trade-off this involves.

Whenever a brand-new Doc is created (first run, or a size/count rollover),
that Doc needs to be added as a NotebookLM source by hand — logged to
sync.log AND appended to ACTION_NEEDED.md, since pythonw has no console.

Only one sync process may run at a time (Windows named mutex). Two processes
would interleave Doc body replacements (delete + insert are separate API
calls) and race on .doc_ids.json.

Usage:
    python obsidian_sync.py --auth   # one-time Google login (opens a browser)
    python obsidian_sync.py          # one-time sync of every vault
    python obsidian_sync.py --watch  # keep running, sync on changes
"""

import os
import sys
import time
import json
import re
import ctypes
import argparse
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

from docs_sync import DocsClient, get_credentials, DIVIDER

# ── CONFIG ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
VAULTS_CONFIG_PATH = SCRIPT_DIR / "vaults.json"
LOG_FILE = SCRIPT_DIR / "sync.log"

IGNORE = {".obsidian", ".trash", "templates", "attachments", ".git"}
IGNORE_FILES = {"home"}
IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "webp")

DEBOUNCE_SECONDS = 5

# ── LOGGING ───────────────────────────────────────────────────────────────────

logger = logging.getLogger("obsidian_sync")
logger.setLevel(logging.INFO)
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=1, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)
if sys.stdout is not None:  # pythonw has no stdout
    logger.addHandler(logging.StreamHandler(sys.stdout))

SYNC_LOCK = threading.Lock()


# ── SINGLE INSTANCE ───────────────────────────────────────────────────────────

_MUTEX_HANDLE = None  # module-level so the handle lives as long as the process


def acquire_single_instance(name: str = "ObsidianNotebookLMSync") -> bool:
    """
    True if we're the only instance. Uses a Windows named mutex, which the OS
    releases automatically when the process exits or crashes (no stale lock
    file to clean up, nothing written into the synced Drive folder).
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]

    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        logger.warning("Could not create instance mutex (error %s) - continuing without guard.",
                       ctypes.get_last_error())
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False

    _MUTEX_HANDLE = handle
    return True


# ── VAULT CONFIG ──────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def load_vaults(config_path: Path) -> list[dict]:
    """
    Each vaults.json entry:
        name             (required) also the Doc-ID key prefix — don't rename after first sync
        main             (required) Obsidian vault path
        drive_folder_id  (optional) Drive folder where this vault's Docs are created
    """
    if not config_path.exists():
        logger.error("vaults config not found at %s (copy vaults.json.example)", config_path)
        sys.exit(1)

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Could not parse %s: %s", config_path, e)
        sys.exit(1)

    vaults, seen_slugs = [], set()
    for i, entry in enumerate(raw):
        try:
            name, main = entry["name"], Path(entry["main"])
        except KeyError as e:
            logger.error("vaults.json entry #%d is missing %s", i, e)
            sys.exit(1)

        slug = slugify(name)
        if slug in seen_slugs:
            logger.error("Duplicate vault name (after slugify): '%s'", name)
            sys.exit(1)
        seen_slugs.add(slug)

        folder_id = entry.get("drive_folder_id") or None
        if not folder_id:
            logger.warning("Vault '%s' has no drive_folder_id — Docs go to My Drive root.", name)

        vaults.append({
            "name": name,
            "slug": slug,
            "main": main,
            "drive_folder_id": folder_id,
            "state_file": SCRIPT_DIR / f".synced_images_{slug}.json",
        })
    return vaults


# ── IMAGE STATE (per vault) ───────────────────────────────────────────────────

def load_synced_log(state_file: Path) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_synced_log(state_file: Path, log_data: dict):
    state_file.write_text(json.dumps(log_data, indent=2), encoding="utf-8")


# ── VAULT STRUCTURE ───────────────────────────────────────────────────────────

def build_vault_skeleton(main_vault: Path) -> str:
    lines = ["# Vault Structure\n"]
    for root, dirs, files in os.walk(main_vault):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE and not d.startswith("."))
        rel_path = Path(root).relative_to(main_vault)
        if rel_path == Path("."):
            depth = 0
            lines.append(f"- **{main_vault.name}/**")
        else:
            depth = len(rel_path.parts)
            lines.append(f"{'  ' * depth}- **{rel_path.name}/**")
        file_indent = "  " * (depth + 1)
        for f in sorted(f for f in files if f.endswith(".md")):
            lines.append(f"{file_indent}- {f}")
    return "\n".join(lines)


def build_root_bundle(main_vault: Path) -> str:
    """Vault structure (first, for quick AI access) + every standalone root note, one Doc."""
    parts = [build_vault_skeleton(main_vault)]

    for item in sorted(main_vault.glob("*.md")):
        if item.stem.lower() in IGNORE_FILES:
            continue
        try:
            content = item.read_text(encoding="utf-8").strip()
        except Exception as e:
            content = f"[Error reading file: {e}]"
        parts.append(f"# {item.stem}\n\n{content}")

    return DIVIDER.join(parts)


# ── MERGING (topic folders) ───────────────────────────────────────────────────

def build_merged(folder: Path) -> str:
    files = sorted(folder.rglob("*.md"))
    if not files:
        return ""
    parts = []
    for f in files:
        rel = f.relative_to(folder)
        try:
            content = f.read_text(encoding="utf-8").strip()
        except Exception as e:
            content = f"[Error reading file: {e}]"
        parts.append(f"# {folder.name} / {rel.as_posix()}\n\n{content}")
    return DIVIDER.join(parts)


# ── IMAGE DISCOVERY (vault-wide) ──────────────────────────────────────────────

def find_new_images(main_vault: Path, synced_log: dict) -> list[dict]:
    """Scans every .md in the vault for embedded images, returns unseen ones with a resolved path."""
    ext_group = "|".join(IMAGE_EXTENSIONS)
    wiki_pattern = re.compile(rf"!\[\[(.*?\.(?:{ext_group}))\]\]", re.IGNORECASE)
    md_pattern = re.compile(rf"!\[.*?\]\((.*?\.(?:{ext_group}))\)", re.IGNORECASE)
    attachment_locations = [main_vault / "attachments", main_vault]

    seen_names = set()
    new_items = []
    for md_file in main_vault.rglob("*.md"):
        if any(part in IGNORE or part.startswith(".") for part in md_file.relative_to(main_vault).parts[:-1]):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        rel_note_path = md_file.relative_to(main_vault).as_posix()
        matches = wiki_pattern.findall(content) + md_pattern.findall(content)
        for match in matches:
            img_name = Path(match).name
            if img_name in synced_log or img_name in seen_names:
                continue
            seen_names.add(img_name)

            img_path = None
            for loc in attachment_locations:
                candidate = loc / img_name
                if candidate.exists():
                    img_path = candidate
                    break
            if not img_path:
                found = list(main_vault.rglob(img_name))
                if found:
                    img_path = found[0]

            if img_path and img_path.exists():
                new_items.append({"img_name": img_name, "rel_path": rel_note_path, "full_path": img_path})

    return new_items


# ── CORE SYNC (one vault) ─────────────────────────────────────────────────────

def sync_once(vault: dict, docs: DocsClient) -> dict[str, str]:
    main, slug, folder_id = vault["main"], vault["slug"], vault["drive_folder_id"]
    results = {}

    # 1. Topic folders → one multipart Doc each
    for item in sorted(main.iterdir()):
        if not item.is_dir() or item.name in IGNORE or item.name.startswith("."):
            continue
        content = build_merged(item)
        if not content:
            results[item.name] = "empty (skipped)"
            continue
        try:
            results[item.name] = docs.upsert_multipart(
                f"{slug}:topic/{item.name}", item.name, content, folder_id)
        except Exception:
            logger.exception("Doc sync failed for %s / %s", vault["name"], item.name)
            results[item.name] = "error (see sync.log)"

    # 2. Root notes + vault structure → one multipart Doc
    try:
        results["Root Notes"] = docs.upsert_multipart(
            f"{slug}:root", "Root Notes", build_root_bundle(main), folder_id)
    except Exception:
        logger.exception("Root bundle sync failed for %s", vault["name"])
        results["Root Notes"] = "error (see sync.log)"

    # 3. Images → append new ones to the shared Images Doc
    synced_log = load_synced_log(vault["state_file"])
    new_images = find_new_images(main, synced_log)
    if new_images:
        try:
            embedded = docs.append_images(f"{slug}:images", "Images", new_images, folder_id)
        except Exception:
            logger.exception("Image sync failed for %s", vault["name"])
            embedded = []
        for item in new_images:
            if item["img_name"] in embedded:
                synced_log[item["img_name"]] = {
                    "rel_path": item["rel_path"], "synced_at": datetime.now().isoformat()
                }
        save_synced_log(vault["state_file"], synced_log)
        results["Images"] = f"{len(embedded)}/{len(new_images)} embedded" if embedded else "0 embedded (see sync.log)"
    else:
        results["Images"] = "unchanged"

    return results


def sync_vault_safely(vault: dict, docs: DocsClient) -> dict[str, str]:
    with SYNC_LOCK:
        try:
            return sync_once(vault, docs)
        except Exception:
            logger.exception("Sync crashed for vault '%s'", vault["name"])
            return {"(vault)": "error (see sync.log)"}


def sync_all_vaults(vaults: list[dict], docs: DocsClient) -> dict[str, dict[str, str]]:
    all_results = {}
    for v in vaults:
        if not v["main"].exists():
            logger.warning("Skipping '%s': main vault not found at %s", v["name"], v["main"])
            continue
        all_results[v["name"]] = sync_vault_safely(v, docs)
    return all_results


def print_results(all_results: dict[str, dict[str, str]]):
    icons = {"created": "✚", "updated": "↺", "unchanged": "·", "empty (skipped)": "–"}
    for vault_name, results in all_results.items():
        logger.info("[%s]", vault_name)
        for name, status in results.items():
            icon = icons.get(status, "✗" if str(status).startswith("error") else "?")
            logger.info("  %s %s [%s]", icon, name, status)


# ── WATCH MODE ────────────────────────────────────────────────────────────────

def watch(vaults: list[dict], docs: DocsClient):
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        logger.error("watchdog not installed. Run: pip install watchdog")
        sys.exit(1)

    class VaultSyncHandler(FileSystemEventHandler):
        def __init__(self, vault: dict):
            self.vault = vault
            self._timer = None
            self._timer_lock = threading.Lock()

        def on_any_event(self, event):
            path = Path(event.src_path)
            if any(part in IGNORE or part.startswith(".") for part in path.parts):
                return
            if event.is_directory and event.event_type == "modified":
                return
            with self._timer_lock:
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(DEBOUNCE_SECONDS, self._run, args=(event.event_type, path.name))
                self._timer.daemon = True
                self._timer.start()

        def _run(self, event_type: str, name: str):
            logger.info("Change detected in '%s' (%s: %s)", self.vault["name"], event_type, name)
            print_results({self.vault["name"]: sync_vault_safely(self.vault, docs)})

    observer = Observer()
    watching = 0
    for v in vaults:
        if not v["main"].exists():
            logger.warning("Not watching '%s': %s not found", v["name"], v["main"])
            continue
        observer.schedule(VaultSyncHandler(v), str(v["main"]), recursive=True)
        logger.info("Watching [%s] %s", v["name"], v["main"])
        watching += 1

    if not watching:
        logger.error("No valid vaults to watch. Check vaults.json.")
        sys.exit(1)

    logger.info("[Initial sync — all vaults]")
    print_results(sync_all_vaults(vaults, docs))

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logger.info("Stopped watcher.")
    observer.join()


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Obsidian vaults → Google Docs for NotebookLM")
    parser.add_argument("--watch", action="store_true", help="Keep running and sync on changes")
    parser.add_argument("--auth", action="store_true", help="One-time Google login, then exit")
    args = parser.parse_args()

    if args.auth:
        get_credentials(interactive=True)
        logger.info("Authenticated. token.json saved — you can now run without --auth.")
        return

    # Must come before anything that touches Docs or .doc_ids.json.
    if not acquire_single_instance():
        logger.error("Another obsidian_sync instance is already running - exiting.")
        sys.exit(1)

    vaults = load_vaults(VAULTS_CONFIG_PATH)

    try:
        docs = DocsClient()
    except Exception as e:
        logger.error("Google Docs client failed to start: %s", e)
        sys.exit(1)

    if args.watch:
        watch(vaults, docs)
    else:
        logger.info("Running one-time sync for all configured vaults...")
        print_results(sync_all_vaults(vaults, docs))
        logger.info("Done!")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)