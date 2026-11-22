# Keep or Delete with PhotoStructure

Pick photos. Ask PhotoStructure. Keep or delete.

Windows GUI against a **local** [PhotoStructure for Node](https://photostructure.com/server/photostructure-for-node/) library. It opens files and folders in Explorer and sends deletes to the Recycle Bin, so it is Windows-only.

This project was vibe-coded. Fork it, sell it, rewrite it, or ignore it.

License: [MIT](LICENSE). Commercial use, private forks, and closed-source products that include this code are allowed. Keep the copyright notice.

This is not an official PhotoStructure project.

## What it does

- Add folders or files, then **Ask PhotoStructure**.
- PhotoStructure `info` hashes each file; `list --dump --where` finds library rows with the same hashes.
- **Match rotated** (default) uses canonical `pHash0` / `rHash0` / `wHash0` (or upright plain hashes). **Match exact only** uses non-zero plain hashes.
- Optional library sync before hashing, progress while scanning and matching, retry of error rows.
- Start / stop / sync / update the local PhotoStructure web server (localhost).
- Optional image preview (Pillow). Double-click a file or folder cell to open it or show it in Explorer.
- Delete matched **evaluated** files to the Recycle Bin (not library files unless those paths were also in the evaluate set).

It does not write the PhotoStructure library database or edit PhotoStructure settings.

## Privacy

Everything stays on your machine.

- Reads `%APPDATA%\PhotoStructure\settings.toml` for `httpPort` and `libraryDir`.
- Talks to `http://127.0.0.1:<port>/` only.
- Network use is npm/npx when you **Update** PhotoStructure (`photostructure@stable`).
- Paths and hashes appear in the UI and log; they are not uploaded by this tool.

Do not commit your photo library, `settings.toml`, or `.photostructure` folders to Github.

## Requirements

- Windows
- [Node.js](https://nodejs.org/)
- [PhotoStructure for Node](https://photostructure.com/server/photostructure-for-node/)
- A PhotoStructure library (the app can start the local web server, too)
- Python 3.10+ including Tkinter, which ships with the official Python 3 Windows installer.

Python packages: see [requirements.txt](requirements.txt). Preview needs Pillow; the rest of the app runs without it.

## Run

Install the optional preview dependency, then start the `.py` with Python 3:

```text
pip install -r requirements.txt
python keep-or-delete-with-photostructure.py
```

`keep-or-delete-with-photostructure.cmd` is a launcher shortcut. Copy it to the Desktop, Downloads, or anywhere else. It finds `keep-or-delete-with-photostructure.py` next to the `.cmd`, then in `%USERPROFILE%\keep-or-delete-with-photostructure\`. It prefers `pythonw.exe` so no console window stays open.

If auto-discovery fails, set `PHOTOSTRUCTURE` to a `photostructure` executable or `photostructure.js`. Otherwise the tool uses `photostructure` on `PATH`, then the newest `%LOCALAPPDATA%\npm-cache\_npx\*\node_modules\photostructure\photostructure.js`.

## Evaluate notes

- Skips directories named `.photostructure` and files named `thumbs.db`, `desktop.ini`, `.ds_store`.
- No size or extension filter: a tiny JPEG or a text file can still appear, then error if PhotoStructure cannot hash it.
- A no-match only means that file’s hashes are not in the library. It does not prove the library scan is complete.
- A yellow banner can appear when the newest PhotoStructure sync report lists skipped or failed items. Open System Activity from that banner.

Stop the web server from this app before **Update**, so `npx` is not replacing files in use.

## Disclaimer

No warranty. Hash matching can miss near-duplicates and can match unrelated images. Confirm before you delete anything. PhotoStructure is a trademark of its owner.
