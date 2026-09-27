#!/usr/bin/env python3
"""
Obsidian → NotebookLM Vault Sync (Unified Text, Structure, & Image PDF Sync)

Features:
  1. Merges Obsidian notes into topic-based Markdown files for NotebookLM.
  2. Generates Vault_Structure.md to map the original vault structure for AI queries.
  3. Extracts embedded images (![[image.png]] / ![alt](image.png)), tracks synced 
     images using .synced_images.json, and appends minimal multi-image PDFs per topic.

Usage:
    python obsidian_sync.py          # one-time sync
    python obsidian_sync.py --watch  # auto-sync on file changes
"""

import os
import sys
import time
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# PDF Libraries
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from pypdf import PdfWriter, PdfReader

# ── CONFIGURE PATHS ───────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()

load_dotenv(SCRIPT_DIR / ".env")

main_vault_env = os.getenv("MAIN_VAULT")
nlm_vault_env = os.getenv("NLM_VAULT")

if not main_vault_env or not nlm_vault_env:
    print("Error: MAIN_VAULT and NLM_VAULT must be configured in .env")
    print("Copy .env.example to .env and set both paths.")
    sys.exit(1)

MAIN_VAULT = Path(main_vault_env)
NLM_VAULT = Path(nlm_vault_env)

STATE_FILE  = SCRIPT_DIR / ".synced_images.json"

# Folders/files in the main vault root to ignore
IGNORE = {".obsidian", ".trash", "templates", "attachments", ".git"}

# Standalone .md files in the root to ignore (no extension needed)
IGNORE_FILES = {"home"}

# ── IMAGE STATE TRACKING ──────────────────────────────────────────────────────

def load_synced_log() -> dict:
    """Loads state tracking log from .synced_images.json."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_synced_log(log_data: dict):
    """Saves updated state log to .synced_images.json."""
    STATE_FILE.write_text(json.dumps(log_data, indent=2), encoding="utf-8")

# ── SKELETON GENERATOR ────────────────────────────────────────────────────────

def generate_vault_skeleton(main_vault: Path, output_file: Path) -> None:
    """
    Scans the Obsidian vault directory structure and outputs a tree-view
    Markdown file for AI-assisted note organization queries.
    """
    lines = ["# Obsidian Vault Directory Skeleton\n"]
    
    for root, dirs, files in os.walk(main_vault):
        dirs[:] = [d for d in dirs if d not in IGNORE and not d.startswith('.')]
        
        rel_path = Path(root).relative_to(main_vault)
        if rel_path == Path('.'):
            depth = 0
            lines.append(f"- **{main_vault.name}/**")
        else:
            depth = len(rel_path.parts)
            indent = "  " * depth
            lines.append(f"{indent}- **{rel_path.name}/**")
            
        file_indent = "  " * (depth + 1)
        md_files = [f for f in files if f.endswith('.md')]
        for f in sorted(md_files):
            lines.append(f"{file_indent}- {f}")
            
    output_file.write_text("\n".join(lines), encoding="utf-8")

# ── IMAGE SYNC & PDF GENERATION ───────────────────────────────────────────────

def find_images_in_topic(topic_folder: Path, main_vault: Path) -> list[dict]:
    """
    Scans all .md files inside a top-level topic directory for image links.
    Returns: [{"img_name": "...", "rel_path": "..."}, ...]
    """
    image_items = []
    wiki_pattern = re.compile(r'!\[\[(.*?\.(?:png|jpg|jpeg|webp))\]\]', re.IGNORECASE)
    md_pattern = re.compile(r'!\[.*?\]\((.*?\.(?:png|jpg|jpeg|webp))\)', re.IGNORECASE)

    for md_file in topic_folder.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        rel_path = md_file.relative_to(main_vault).as_posix()

        for match in wiki_pattern.findall(content):
            image_items.append({"img_name": Path(match).name, "rel_path": rel_path})

        for match in md_pattern.findall(content):
            image_items.append({"img_name": Path(match).name, "rel_path": rel_path})

    return image_items


def build_minimal_temp_pdf(images_meta: list[dict], temp_pdf_path: Path):
    """
    Renders stacked images with a minimal gray path header above each figure.
    """
    doc = SimpleDocTemplate(
        str(temp_pdf_path),
        pagesize=A4,
        rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36
    )
    styles = getSampleStyleSheet()
    path_style = ParagraphStyle(
        'MinimalPath',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor("#4A5568"),
        spaceBefore=8,
        spaceAfter=4
    )

    story = []
    for item in images_meta:
        story.append(Paragraph(f"<b>Path:</b> {item['rel_path']}", path_style))
        try:
            rl_img = RLImage(str(item["full_path"]))
            max_width = 500
            max_height = 400

            w_ratio = max_width / float(rl_img.drawWidth)
            h_ratio = max_height / float(rl_img.drawHeight)
            scale = min(w_ratio, h_ratio, 1.0)

            rl_img.drawWidth *= scale
            rl_img.drawHeight *= scale

            story.append(rl_img)
            story.append(Spacer(1, 10))
        except Exception as e:
            story.append(Paragraph(f"<i>[Error rendering image: {e}]</i>", path_style))

    doc.build(story)


def append_pdf_pages(existing_pdf: Path, new_pdf: Path, output_pdf: Path):
    """Appends new PDF pages to an existing PDF file."""
    writer = PdfWriter()
    if existing_pdf.exists():
        reader_existing = PdfReader(existing_pdf)
        for page in reader_existing.pages:
            writer.add_page(page)

    reader_new = PdfReader(new_pdf)
    for page in reader_new.pages:
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)


def sync_images_for_topic(topic_folder: Path, main_vault: Path, nlm_vault: Path, synced_log: dict) -> bool:
    """
    Scans, filters, builds, and appends new images to the topic's _Images.pdf source.
    """

    attachment_locations = [
        main_vault / "attachments",
        main_vault
    ]
    
    found_images = find_images_in_topic(topic_folder, main_vault)
    new_items = []

    for item in found_images:
        img_name = item["img_name"]
        if img_name in synced_log:
            continue

        # Locate image file path on disk
        img_file_path = None
        for loc in attachment_locations:
            candidate = loc / img_name
            if candidate.exists():
                img_file_path = candidate
                break

        if not img_file_path:
            # Fallback search across the vault
            glob_search = list(main_vault.rglob(img_name))
            if glob_search:
                img_file_path = glob_search[0]

        if img_file_path and img_file_path.exists():
            item["full_path"] = img_file_path
            new_items.append(item)

    if not new_items:
        return False

    topic_name = topic_folder.name
    target_pdf = nlm_vault / f"{topic_name}_Images.pdf"
    temp_pdf = nlm_vault / f"temp_{topic_name}.pdf"

    build_minimal_temp_pdf(new_items, temp_pdf)
    append_pdf_pages(target_pdf, temp_pdf, target_pdf)

    if temp_pdf.exists():
        temp_pdf.unlink()

    # Log new images to prevent duplicate processing
    for item in new_items:
        synced_log[item["img_name"]] = {
            "topic": topic_name,
            "note_path": item["rel_path"],
            "synced_at": datetime.now().isoformat()
        }

    return True

# ── HELPER FUNCTIONS ──────────────────────────────────────────────────────────

def collect_files(folder: Path) -> list[Path]:
    """Recursively collect all .md files under a folder, sorted by path."""
    return sorted(folder.rglob("*.md"))


def build_merged(folder: Path) -> str:
    """Concatenate all .md files in a folder into one string with dividers."""
    files = collect_files(folder)
    if not files:
        return ""

    parts = []
    for f in files:
        rel = f.relative_to(folder)
        try:
            content = f.read_text(encoding="utf-8").strip()
        except Exception as e:
            content = f"[Error reading file: {e}]"

        header = f"# {folder.name} / {rel.as_posix()}\n"
        parts.append(f"{header}\n{content}")

    divider = "\n\n" + "─" * 60 + "\n\n"
    return divider.join(parts)


def sync_once(main_vault: Path, nlm_vault: Path) -> dict[str, str]:
    """
    Sync top-level folders, generate structural tree, and append new images to PDF sources.
    Returns a dict of {topic_name: "created" | "updated" | "unchanged"}.
    """
    nlm_vault.mkdir(parents=True, exist_ok=True)
    results = {}
    synced_log = load_synced_log()

    # 1. Process top-level directories
    for item in main_vault.iterdir():
        if item.is_dir() and item.name not in IGNORE and not item.name.startswith("."):
            content = build_merged(item)
            out_file = nlm_vault / f"{item.name}.md"

            if not content:
                results[item.name] = "empty (skipped)"
                continue

            # Process Markdown text
            if out_file.exists():
                old = out_file.read_text(encoding="utf-8")
                if old == content:
                    status = "unchanged"
                else:
                    status = "updated"
            else:
                status = "created"

            out_file.write_text(content, encoding="utf-8")

            # Process Images into PDF for this topic
            img_added = sync_images_for_topic(item, main_vault, nlm_vault, synced_log)
            if img_added and status == "unchanged":
                status = "updated (images added)"

            results[item.name] = status

    # Save state log if images were updated
    save_synced_log(synced_log)

    # 2. Process standalone .md files in the root
    for item in main_vault.glob("*.md"):
        if item.stem.lower() in IGNORE_FILES:
            continue

        out_file = nlm_vault / item.name
        try:
            content = item.read_text(encoding="utf-8")
        except Exception:
            continue

        if out_file.exists():
            old = out_file.read_text(encoding="utf-8")
            if old == content:
                results[item.stem] = "unchanged"
                continue
            status = "updated"
        else:
            status = "created"

        out_file.write_text(content, encoding="utf-8")
        results[item.stem] = status

    # 3. Generate Vault Structure Directory Map
    skeleton_path = nlm_vault / "Vault_Structure.md"
    generate_vault_skeleton(main_vault, skeleton_path)

    return results


def print_results(results: dict[str, str]):
    icons = {
        "created": "✚",
        "updated": "↺",
        "updated (images added)": "🖼️",
        "unchanged": "·",
        "empty (skipped)": "–"
    }
    for topic, status in results.items():
        icon = icons.get(status, "?")
        print(f"  {icon} {topic} [{status}]")

# ── WATCH MODE ────────────────────────────────────────────────────────────────

def watch(main_vault: Path, nlm_vault: Path):
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("watchdog not installed. Run: pip install watchdog")
        sys.exit(1)

    class SyncHandler(FileSystemEventHandler):
        def __init__(self):
            self.last_sync = 0
            self.cooldown = 2  # seconds debounce

        def on_any_event(self, event):
            path = Path(event.src_path)
            for part in path.parts:
                if part in IGNORE or part.startswith("."):
                    return

            now = time.time()
            if now - self.last_sync > self.cooldown:
                self.last_sync = now
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] Change detected ({event.event_type}: {path.name})")
                res = sync_once(main_vault, nlm_vault)
                print_results(res)

    event_handler = SyncHandler()
    observer = Observer()
    observer.schedule(event_handler, str(main_vault), recursive=True)
    observer.start()

    print(f"Watching: {main_vault}")
    print(f"Target:   {nlm_vault}")
    print("Press Ctrl+C to stop.\n")

    print("[Initial sync]")
    res = sync_once(main_vault, nlm_vault)
    print_results(res)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nStopped watcher.")
    observer.join()

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Obsidian main vault → NotebookLM vault")
    parser.add_argument("--watch", action="store_true", help="Watch for changes and auto-sync")
    parser.add_argument("--main", type=Path, default=MAIN_VAULT, help="Path to main vault")
    parser.add_argument("--nlm", type=Path, default=NLM_VAULT, help="Path to NotebookLM vault")
    args = parser.parse_args()

    if not args.main.exists():
        print(f"Error: Main vault not found at: {args.main}")
        sys.exit(1)

    if args.watch:
        watch(args.main, args.nlm)
    else:
        print("Running one-time sync...")
        res = sync_once(args.main, args.nlm)
        print_results(res)
        print("Done!")

if __name__ == "__main__":
    main()