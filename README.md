# iFetch

Recursively download public web folders into a ZIP. Runs entirely on your own Windows PC.

Enter a public HTTP/HTTPS directory URL (e.g. `https://www.lpude.in/SLMs/`). iFetch scans it and all
subfolders, downloads the publicly linked files, keeps the folder structure, and gives you one ZIP.

## Setup (Windows)

1. **Install Python 3.10+** from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**).
2. **Open PowerShell** in the `ifetch` folder (Shift + right-click inside the folder -> "Open PowerShell window here").
3. **Create a virtual environment:**
   ```powershell
   python -m venv .venv
   ```
4. **Activate it:**
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks scripts, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
5. **Install dependencies:**
   ```powershell
   pip install -r requirements.txt
   ```
6. **Run:**
   ```powershell
   python main.py
   ```
7. **Open** http://127.0.0.1:8000 in your browser. Press `Ctrl+C` in PowerShell to stop.

## How it works

- `POST /api/download` validates the URL and starts a background job (returns a job id).
- `GET /api/status/{id}` is polled by the page to show progress.
- When finished, the ZIP is saved to your **Downloads** folder (e.g. `C:\Users\YourName\Downloads\SLMs.zip`); temp files are deleted. Existing files are never overwritten (`SLMs (1).zip`).

(The job/polling design is what allows live progress; a single blocking request could not report it.)

## Limits (edit at the top of `main.py`)

| Setting | Default |
|---|---|
| Max files | 500 |
| Max folders | 500 |
| Max total size | 2 GB |
| Max folder depth | 8 |
| Request timeout | 30 s (10 s connect) |

## Safety

- Only `http`/`https`; localhost, private and internal IPs are blocked (also checked on every redirect).
- Stays inside the starting folder; same domain only; `../` paths and unsafe filenames are rejected.
- Only follows links the server publicly exposes. No logins, no bypassing access controls.
- The server listens on `127.0.0.1` only.
- Known limit: the app expects Apache/nginx-style HTML listings with `<a href>` links. Pages that build
  listings with JavaScript won't work. Files that fail individually are skipped and shown as warnings.

Please only download content you are allowed to copy.

## Troubleshooting

- *"No directory listing found"*: the URL isn't a folder index page.
- *HTTP 403/404/500*: the server refused, or the path doesn't exist.
- *"Download limit reached"*: raise the limits in `main.py`.

## Future ideas (V2, not implemented)

Folder/file selection before downloading, pause/resume, retry failed files, download speed display,
concurrent downloads, resume interrupted downloads, multiple URLs, filtering by extension,
estimated ZIP size, dark mode, download history.
