# Obsidian → NotebookLM Sync

Python tool that syncs one or more Obsidian vaults to native **Google Docs**, so NotebookLM picks up changes automatically. Uploaded Markdown/PDF sources don't refresh; Google Docs sources do.

## What it produces (per vault)

| Output | Contents |
|---|---|
| One Doc per top-level topic folder | All `.md` files in the folder, merged |
| `Root Notes` Doc | Vault structure map + every standalone root note |
| `Images` Doc | Every embedded image, with the source note path above each |

- Docs over ~900k characters are split automatically into `(v2)`, `(v3)`, etc.
- The Images Doc rolls over to a new part after 150 images.
- Unchanged content is detected by SHA-256 and skipped.
- Folders/files in `.obsidian`, `.trash`, `templates`, `attachments`, `.git`, and the root note `home` are ignored.

## Setup

```bash
pip install -r requirements.txt
```

1. Create OAuth credentials (Desktop app) in Google Cloud with the Docs and Drive APIs enabled, and save them as `credentials.json` next to the scripts.
2. Create `vaults.json` (see `vaults.json.example`):

```json
[
  {
    "name": "MyVault1",
    "main": "C:\\Path\\To\\Your\\Obsidian\\MyVault1",
    "drive_folder_id": "YOUR_GOOGLE_DRIVE_FOLDER_ID"
  }
]
```

- `name` is required and is used as the Doc-ID key prefix. **Don't rename it after the first sync**, or new Docs will be created.
- `drive_folder_id` is optional. If omitted, Docs are created in the root of My Drive.

3. Authenticate once (opens a browser):

```bash
python obsidian_sync.py --auth
```

## Running

```bash
python obsidian_sync.py           # one-time sync of all vaults
python obsidian_sync.py --watch   # initial sync, then sync on file changes
```

Only one instance can run at a time (Windows named mutex), because concurrent runs would corrupt Doc updates and `.doc_ids.json`.

## Adding new Docs to NotebookLM

NotebookLM can't be automated, so each **newly created** Doc (first run, or a size/image rollover) must be added once as a source via the Drive picker. After that, content updates flow through automatically.

New Docs are logged to `sync.log`, appended to `ACTION_NEEDED.md`, and (if `win11toast` is installed) shown as a Windows toast. If a split Doc shrinks and a part is no longer needed, that part is replaced with a placeholder note rather than deleted, so the NotebookLM source doesn't break. You can remove that source manually.

## Windows startup

`run_sync.vbs` starts `--watch` silently at login:

```vbscript
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

python = "C:\Users\userX\venvs\obsidian-notebooklm-sync\Scripts\pythonw.exe"
script = "H:\My Drive\Scripts\Obsidian-NotebookLM Sync\obsidian_sync.py"

' Wait up to 5 minutes for Google Drive to mount (poll every 5 s)
For i = 1 To 60
    If fso.FileExists(script) Then Exit For
    WScript.Sleep 5000
Next

If fso.FileExists(script) Then
    WScript.Sleep 5000   ' let Drive settle after the drive appears
    WshShell.Run """" & python & """ """ & script & """ --watch", 0, False
End If
```

Replace the path with your `obsidian_sync.py` location, then press `Win+R`, enter `shell:startup`, and put a shortcut to `run_sync.vbs` there.

## ⚠️ Image privacy trade-off

The Docs API can only insert images from a URL. For each new image, the tool uploads a temporary Drive file with **"anyone with the link"** access, inserts it into the Doc, then deletes the temp file immediately. The image is briefly reachable by link during that window. Don't sync vaults containing sensitive images without considering this.

## Runtime files (git-ignored)

`vaults.json`, `credentials.json`, `token.json`, `.doc_ids.json` (vault key → Doc ID map), `.synced_images_<vault>.json`, `sync.log`, `ACTION_NEEDED.md`

## Features

- Multi-vault support via `vaults.json`
- Topic folders merged into auto-splitting Google Docs
- Vault structure map included in the Root Notes Doc
- Embedded images (wiki-style and Markdown) sent to an Images Doc
- Hash-based change detection and retry with backoff on API errors
- Debounced `--watch` mode, with per-vault image state
- Self-heals if a Doc is deleted or trashed (recreates it)