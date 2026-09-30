# Google Docs output layer for obsidian_sync.py

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from PIL import Image as PILImage

logger = logging.getLogger("obsidian_sync.docs")

SCRIPT_DIR = Path(__file__).parent.resolve()
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
DOC_ID_MAP_FILE = SCRIPT_DIR / ".doc_ids.json"
ACTION_FILE = SCRIPT_DIR / "ACTION_NEEDED.md"

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

CHUNK_CHARS = 40_000
MAX_DOC_CHARS = 1_000_000
TARGET_PART_CHARS = 900_000     # split threshold, kept well under the hard limit
DIVIDER = "\n\n" + "─" * 60 + "\n\n"   # must match the one build_merged() uses

IMAGES_PER_DOC = 150            # conservative rollover point for the Images doc
MAX_IMAGE_WIDTH_PT = 400.0
EMU_PER_PT = 12700


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────

def notify(message: str):
    """Logs + appends to ACTION_NEEDED.md + (if installed) fires a Windows toast."""
    logger.warning(message)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(ACTION_FILE, "a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {message}\n")
    try:
        from win11toast import notify as _toast  # optional soft dependency
        _toast("Obsidian sync needs attention", message)
    except Exception:
        pass


# ── AUTH ──────────────────────────────────────────────────────────────────────

def get_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:
            logger.warning("Token refresh failed: %s", e)

    if not interactive:
        raise RuntimeError(
            "No valid Google token. Run once in a terminal: python obsidian_sync.py --auth"
        )

    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(f"credentials.json not found at {CREDENTIALS_FILE} - see README setup.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ── API HELPERS ───────────────────────────────────────────────────────────────

def _execute(request, retries: int = 5):
    delay = 2
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            transient = e.resp.status in (429, 500, 502, 503, 504)
            if transient and attempt < retries - 1:
                logger.warning("API %s, retrying in %ss", e.resp.status, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── SPLITTING ─────────────────────────────────────────────────────────────────

def split_into_parts(content: str, divider: str = DIVIDER, limit: int = TARGET_PART_CHARS) -> list[str]:

    if len(content) <= limit:
        return [content]

    sections = content.split(divider)
    parts, current = [], ""

    for section in sections:
        piece = section if not current else divider + section
        if current and len(current) + len(piece) > limit:
            parts.append(current)
            current = section
        elif len(section) > limit:
            # a single note is itself oversized - hard-split it, no clean way around it
            if current:
                parts.append(current)
                current = ""
            for i in range(0, len(section), limit):
                parts.append(section[i:i + limit])
        else:
            current = current + piece if current else section

    if current:
        parts.append(current)

    return parts


# ── CLIENT ────────────────────────────────────────────────────────────────────

class DocsClient:
    def __init__(self):
        creds = get_credentials(interactive=False)
        self.docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.map, self.parts, self.image_counts = self._load_map()

    # -- persistence ------------------------------------------------------------

    def _load_map(self):
        if not DOC_ID_MAP_FILE.exists():
            return {}, {}, {}
        try:
            data = json.loads(DOC_ID_MAP_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"{DOC_ID_MAP_FILE.name} is corrupt ({e}). Restore it from backup "
                "or delete it (this will create fresh Docs for everything)."
            )
        if "docs" in data or "parts" in data or "image_counts" in data:
            return data.get("docs", {}), data.get("parts", {}), data.get("image_counts", {})
        return data, {}, {}  # legacy flat format from the pre-multipart version

    def _save_map(self):
        tmp = DOC_ID_MAP_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"docs": self.map, "parts": self.parts, "image_counts": self.image_counts}, indent=2
        ), encoding="utf-8")
        tmp.replace(DOC_ID_MAP_FILE)

    # -- low-level doc ops --------------------------------------------------

    def _doc_is_usable(self, doc_id: str) -> bool:
        try:
            f = _execute(self.drive.files().get(fileId=doc_id, fields="id,trashed"))
            return not f.get("trashed", False)
        except HttpError as e:
            if e.resp.status == 404:
                return False
            raise

    def _create_doc(self, title: str, folder_id: str | None) -> str:
        body = {"name": title, "mimeType": "application/vnd.google-apps.document"}
        if folder_id:
            body["parents"] = [folder_id]
        return _execute(self.drive.files().create(body=body, fields="id"))["id"]

    def _ensure_doc(self, key: str, title: str, folder_id: str | None) -> tuple[str, bool]:
        """Returns (doc_id, was_created)."""
        entry = self.map.get(key)
        if entry and self._doc_is_usable(entry["doc_id"]):
            return entry["doc_id"], False

        if entry:
            logger.warning("Doc for '%s' was deleted/trashed — recreating.", key)

        doc_id = self._create_doc(title, folder_id)
        self.map[key] = {
            "doc_id": doc_id, "title": title,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        return doc_id, True

    def _replace_body(self, doc_id: str, text: str):
        doc = _execute(self.docs.documents().get(documentId=doc_id, fields="body/content/endIndex"))
        end_index = doc["body"]["content"][-1]["endIndex"]

        if end_index - 1 > 1:
            _execute(self.docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}}}]},
            ))

        chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
        for chunk in reversed(chunks):
            _execute(self.docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": chunk}}]},
            ))

    # -- single-doc upsert (unchanged behaviour, used per part) --------------

    def upsert(self, key: str, title: str, content: str, folder_id: str | None = None) -> str:
        content = content.replace("\x00", "")
        if len(content) > MAX_DOC_CHARS:
            raise ValueError(f"{key}: {len(content):,} chars exceeds the Google Docs limit.")

        digest = _sha256(content)
        entry = self.map.get(key)
        if entry and entry.get("sha256") == digest and self._doc_is_usable(entry["doc_id"]):
            return "unchanged"

        doc_id, created = self._ensure_doc(key, title, folder_id)
        self._replace_body(doc_id, content)
        self.map[key]["sha256"] = digest
        self.map[key]["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_map()
        return "created" if created else "updated"

    # -- multipart upsert (topics / root bundle) ------------------------------

    def upsert_multipart(self, base_key: str, base_title: str, content: str,
                          folder_id: str | None = None) -> str:
        """
        Splits content across N Docs if needed. Returns the status of part 1
        ("created"/"updated"/"unchanged") — good enough for the results table;
        rollover/stale events are reported via notify(), not the return value.
        """
        parts = split_into_parts(content)
        prev_count = self.parts.get(base_key, 0)
        first_status = None

        for i, part_content in enumerate(parts):
            key = base_key if i == 0 else f"{base_key}#{i + 1}"
            title = base_title if i == 0 else f"{base_title} (v{i + 1})"
            is_new_key = key not in self.map
            status = self.upsert(key, title, part_content, folder_id)
            if i == 0:
                first_status = status
            if is_new_key and status == "created":
                notify(f"New Doc created: '{title}'. Add it as a NotebookLM source (Drive picker).")

        # don't delete (breaks the NotebookLM source)
        for j in range(len(parts), prev_count):
            old_key = base_key if j == 0 else f"{base_key}#{j + 1}"
            old_entry = self.map.get(old_key)
            if old_entry:
                note = (f"[This part is no longer needed — '{base_title}' now fits in "
                        f"fewer parts. You can remove this source from NotebookLM.]")
                self.upsert(old_key, old_entry["title"], note, folder_id)
                notify(f"'{old_entry['title']}' is now empty — safe to remove from NotebookLM.")

        self.parts[base_key] = len(parts)
        self._save_map()
        return first_status

    # -- images ----------------------------------------------------------------

    def _upload_temp_public(self, image_path: Path) -> str:
        """Uploads image_path to Drive with link-view access; returns its file ID."""
        media = MediaFileUpload(str(image_path), resumable=False)
        f = _execute(self.drive.files().create(
            body={"name": f"_tmp_sync_{image_path.name}"}, media_body=media, fields="id"))
        file_id = f["id"]
        _execute(self.drive.permissions().create(
            fileId=file_id, body={"role": "reader", "type": "anyone"}))
        return file_id

    def _append_one_image(self, doc_id: str, rel_path: str, image_path: Path):
        temp_id = self._upload_temp_public(image_path)
        try:
            uri = f"https://drive.google.com/uc?export=view&id={temp_id}"

            with PILImage.open(image_path) as im:
                w, h = im.size
            width_pt = min(MAX_IMAGE_WIDTH_PT, float(w))
            height_pt = width_pt * (float(h) / float(w)) if w else width_pt
            width_emu = int(width_pt * EMU_PER_PT)
            height_emu = int(height_pt * EMU_PER_PT)

            doc = _execute(self.docs.documents().get(documentId=doc_id, fields="body/content(endIndex)"))
            end_index = doc["body"]["content"][-1]["endIndex"]
            insert_at = max(end_index - 1, 1)
            header = f"Path: {rel_path}\n"

            _execute(self.docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
                {"insertText": {"location": {"index": insert_at}, "text": header}},
                {"insertInlineImage": {
                    "location": {"index": insert_at + len(header)},
                    "uri": uri,
                    "objectSize": {
                        "width": {"magnitude": width_emu, "unit": "EMU"},
                        "height": {"magnitude": height_emu, "unit": "EMU"},
                    },
                }},
                {"insertText": {"location": {"index": insert_at + len(header) + 1}, "text": "\n\n"}},
            ]}))
        finally:
            try:
                _execute(self.drive.files().delete(fileId=temp_id))
            except Exception:
                logger.warning("Could not delete temp image upload %s (file id %s)", image_path.name, temp_id)

    def append_images(self, base_key: str, base_title: str, items: list[dict],
                       folder_id: str | None = None) -> list[str]:

        if not items:
            return []

        part = self.parts.get(base_key, 1) or 1
        key = base_key if part == 1 else f"{base_key}#{part}"
        succeeded = []

        for item in items:
            count = self.image_counts.get(key, 0)
            if count >= IMAGES_PER_DOC:
                part += 1
                key = f"{base_key}#{part}"
                title = f"{base_title} (v{part})"
                notify(f"'{base_title}' image doc is full — started '{title}'. "
                       f"Add it as a NotebookLM source.")
            title = base_title if part == 1 else f"{base_title} (v{part})"

            doc_id, created = self._ensure_doc(key, title, folder_id)
            if created:
                notify(f"New Doc created: '{title}'. Add it as a NotebookLM source (Drive picker).")

            try:
                self._append_one_image(doc_id, item["rel_path"], item["full_path"])
                self.image_counts[key] = self.image_counts.get(key, 0) + 1
                succeeded.append(item["img_name"])
            except Exception:
                logger.exception("Failed to embed image %s — will retry next sync", item["img_name"])

        self.parts[base_key] = part
        self._save_map()
        return succeeded