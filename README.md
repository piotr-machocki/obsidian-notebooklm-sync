# Obsidian → NotebookLM Sync

Python tool for syncing one or more Obsidian vaults to NotebookLM-friendly sources.

## Google Docs

NotebookLM automatically refreshes native Google Docs sources, unlike uploaded Markdown/PDF files. The project is therefore being migrated to Google Docs output.

`docs_sync.py` contains the Google Docs implementation and is currently being integrated into the main sync pipeline.

## Setup

```bash
pip install -r requirements.txt
```

Copy `vaults.json.example` to `vaults.json` and configure your vaults:

```json
[
  {
    "name": "MyVault1",
    "main": "C:\\Path\\To\\Your\\Obsidian\\MyVault1",
    "nlm": "C:\\Path\\To\\Your\\NotebookLM\\Folder"
  }
]
```

Add as many vaults as needed.

## Windows startup

`run_sync.vbs` can start `--watch` silently at Windows login.

```vbscript
Set WshShell = CreateObject("WScript.Shell")
' Waits 30 seconds for Google Drive, then runs pythonw silently
WScript.Sleep 30000
WshShell.Run "pythonw ""H:\My Drive\Scripts\obsidian_sync.py"" --watch", 0, False
```

Replace the script path with your actual `obsidian_sync.py` location.

To run it automatically:

1. Press `Win+R`
2. Enter `shell:startup`
3. Create a shortcut to `run_sync.vbs` in that folder

## Running

```bash
python obsidian_sync.py
python obsidian_sync.py --watch
```

The first command performs a one-time sync. The second watches all configured vaults for changes.

## Features

* Multi-vault support via `vaults.json`
* Topic-based note synchronization
* Vault structure generation
* Embedded image extraction and PDF generation
* Automatic syncing with `--watch`
* Per-vault synchronization state
* Google Docs output *(in progress)*

## Status

Work in progress — multi-vault support is implemented; Google Docs integration is in progress.
