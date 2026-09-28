"""
docs_sync.py — sketch of a Google Docs output path for obsidian_sync.py.

WHY THIS EXISTS
----------------
NotebookLM only auto-refreshes sources that are native Google Docs/Sheets/
Slides. Plain .md/.txt/.pdf files are frozen snapshots at the moment you add
them as a source. To get NotebookLM sources that update themselves with zero
manual re-adding, the merged topic content needs to live in an actual Google
Doc (not a .md file sitting in a Drive folder).

This module is NOT wired into obsidian_sync.py yet — it's a working sketch of
the pieces needed:
  1. OAuth against your own Google account (one-time browser consent)
  2. Finding-or-creating one Google Doc per topic
  3. Replacing that Doc's full body with the freshly merged content on each sync

SETUP (one-time, per Google account)
-------------------------------------
1. Go to https://console.cloud.google.com/ -> create a project (or reuse one).
2. Enable the "Google Docs API" and "Google Drive API" for that project.
3. Configure the OAuth consent screen (External is fine for personal use;
   add your own Google account as a test user).
4. Create credentials -> OAuth client ID -> Application type: Desktop app.
   Download the JSON, save it next to this script as `credentials.json`.
5. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
6. First run will pop a browser window for one-time consent, then cache a
   refreshable token in `token.json`. No further manual auth after that.

INTEGRATION SKETCH (once you want this wired in)
--------------------------------------------------
In vaults.json, add an optional per-vault "output": "gdoc" (default "md").
In obsidian_sync.py's sync_once(), when a topic's output mode is "gdoc",
call upsert_doc(...) below instead of out_file.write_text(content).
"""

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCRIPT_DIR = Path(__file__).parent.resolve()
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
DOC_ID_MAP_FILE = SCRIPT_DIR / ".doc_ids.json"

# Docs + Drive scopes: Docs to write content, Drive to create/find files by name
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


def get_credentials() -> Credentials:
    """Loads cached credentials, refreshing or running the OAuth flow as needed."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _load_doc_id_map() -> dict:
    if DOC_ID_MAP_FILE.exists():
        return json.loads(DOC_ID_MAP_FILE.read_text(encoding="utf-8"))
    return {}


def _save_doc_id_map(mapping: dict):
    DOC_ID_MAP_FILE.write_text(json.dumps(mapping, indent=2), encoding="utf-8")


def get_or_create_doc(drive_service, docs_service, title: str, parent_folder_id: str | None = None) -> str:
    """
    Returns the Doc ID for `title`, creating it (once) if it doesn't exist yet.
    Reuses the same Doc ID on every future sync so NotebookLM keeps tracking
    the same source instead of you re-adding a new file each time.
    """
    doc_map = _load_doc_id_map()

    if title in doc_map:
        return doc_map[title]

    body = {"title": title}
    doc = docs_service.documents().create(body=body).execute()
    doc_id = doc["documentId"]

    if parent_folder_id:
        drive_service.files().update(
            fileId=doc_id,
            addParents=parent_folder_id,
            fields="id, parents",
        ).execute()

    doc_map[title] = doc_id
    _save_doc_id_map(doc_map)
    return doc_id


def update_doc_content(docs_service, doc_id: str, new_text: str):
    """
    Replaces the entire body of a Google Doc with new_text. Simplest correct
    approach given the content is fully regenerated from Obsidian on every
    sync anyway — no need for incremental diffing.
    """
    doc = docs_service.documents().get(documentId=doc_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"]

    requests = []
    # Google Docs won't let you delete the very last newline; stop one short.
    if end_index > 1:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index - 1}
            }
        })

    requests.append({
        "insertText": {
            "location": {"index": 1},
            "text": new_text,
        }
    })

    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()


def upsert_doc(title: str, content: str, parent_folder_id: str | None = None):
    """
    High-level entry point: given a topic title and its freshly merged
    Markdown content, create the Doc on first run or update it in place on
    every subsequent run. This is what obsidian_sync.py would call per topic
    once "output": "gdoc" is wired in.
    """
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    docs_service = build("docs", "v1", credentials=creds)

    doc_id = get_or_create_doc(drive_service, docs_service, title, parent_folder_id)
    update_doc_content(docs_service, doc_id, content)
    return doc_id


if __name__ == "__main__":
    # Quick manual test: python docs_sync.py
    # Creates/updates a Doc called "Docs Sync Test" with a timestamped body.
    from datetime import datetime
    test_id = upsert_doc(
        "Docs Sync Test",
        f"Last updated: {datetime.now().isoformat()}\n\nThis Doc is managed by docs_sync.py.",
    )
    print(f"Doc ID: {test_id}")
    print(f"https://docs.google.com/document/d/{test_id}/edit")