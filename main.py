"""
FolderFetch - recursively download a public web folder into a ZIP.

Run with:  python main.py   then open http://127.0.0.1:8000

How it works (V1, deliberately simple and sequential):
  1. POST /api/download   -> validates the URL and starts a background job, returns a job id
  2. GET  /api/status/ID  -> the page polls this to show progress
  3. When finished, the ZIP is saved into your Windows Downloads folder
"""

import asyncio
import ipaddress
import re
import shutil
import socket
import tempfile
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import httpx
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

# ----------------------------------------------------------------------------
# Limits - change these if you need to
# ----------------------------------------------------------------------------
MAX_FILES = 500                       # max number of files per job
MAX_FOLDERS = 500                     # max number of folders to scan
MAX_TOTAL_BYTES = 2 * 1024**3         # 2 GB total download size
MAX_DEPTH = 8                         # how many folder levels deep to go
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
JOB_MAX_AGE_SECONDS = 60 * 60         # unfetched results are deleted after 1 hour

BASE_DIR = Path(__file__).parent
WORK_ROOT = Path(tempfile.gettempdir()) / "folderfetch"
DOWNLOADS_DIR = Path.home() / "Downloads"   # finished ZIPs are saved here
JOBS: dict[str, dict] = {}


class FolderFetchError(Exception):
    """An error whose message is safe and useful to show to the user."""


# ----------------------------------------------------------------------------
# URL / safety helpers
# ----------------------------------------------------------------------------
async def assert_public_host(url: str) -> None:
    """Refuse localhost, private networks and other non-public addresses."""
    host = urlparse(url).hostname
    if not host:
        raise FolderFetchError("Invalid URL: no host name found.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FolderFetchError(f"Connection failure: could not resolve host '{host}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FolderFetchError("Blocked: localhost and private/internal addresses are not allowed.")


def normalize_start_url(raw: str) -> str:
    raw = (raw or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise FolderFetchError("Invalid URL. Enter a full http:// or https:// address.")
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"  # treat the start URL as a directory
    return parsed._replace(path=path, query="", fragment="").geturl()


def safe_relative_path(root_name: str, raw_rest: str) -> Path | None:
    """
    Turn the part of a URL path below the start folder into a safe relative path.
    Returns None if it looks like a path-traversal attempt.
    """
    parts = [root_name] + re.split(r"[\\/]", unquote(raw_rest))
    clean = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            return None
        part = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part).rstrip(". ")  # Windows-safe
        if part:
            clean.append(part)
    return Path(*clean) if clean else None


def http_error_message(status: int, url: str) -> str:
    if status == 403:
        return f"Access denied (HTTP 403) for {url}. The folder is not publicly accessible."
    if status == 404:
        return f"Not found (HTTP 404): {url}"
    if status >= 500:
        return f"The server had an error (HTTP {status}) for {url}."
    return f"Unexpected HTTP {status} for {url}."


def translate_httpx_error(exc: Exception, url: str) -> FolderFetchError:
    if isinstance(exc, httpx.TimeoutException):
        return FolderFetchError(f"Timeout while contacting {url}.")
    if isinstance(exc, httpx.HTTPStatusError):
        return FolderFetchError(http_error_message(exc.response.status_code, url))
    if isinstance(exc, httpx.TooManyRedirects):
        return FolderFetchError(f"Too many redirects for {url}.")
    return FolderFetchError(f"Connection failure for {url}: {exc.__class__.__name__}")


# ----------------------------------------------------------------------------
# Crawling
# ----------------------------------------------------------------------------
async def fetch_listing(client: httpx.AsyncClient, url: str) -> str | None:
    """Fetch a directory page. Returns HTML text, or None if it is not HTML."""
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise translate_httpx_error(exc, url)
    if "html" not in resp.headers.get("content-type", "").lower():
        return None
    return resp.text


def extract_entries(page_url: str, html: str, start: httpx.URL, base_path: str):
    """Return (set_of_directory_urls, set_of_file_urls) found on one listing page."""
    soup = BeautifulSoup(html, "html.parser")
    dirs, files = set(), set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
            continue
        full, _ = urldefrag(urljoin(page_url, href))
        p = urlparse(full)
        if p.scheme not in ("http", "https"):
            continue
        if p.netloc.lower() != start.netloc.decode().lower():
            continue  # external domain
        if p.query:
            continue  # e.g. Apache sort links like ?C=N;O=D
        if ".." in re.split(r"[\\/]", unquote(p.path)):
            continue  # path traversal
        if not p.path.startswith(base_path) or p.path == base_path:
            continue  # outside the starting folder (this also skips "Parent Directory")
        clean = p._replace(fragment="", query="").geturl()
        (dirs if p.path.endswith("/") else files).add(clean)
    return dirs, files


async def probe_is_directory(client: httpx.AsyncClient, url: str) -> str | None:
    """
    Some servers link to folders without a trailing slash. If `url` is really a
    folder (redirects to a '/' URL, or returns an HTML page), return its folder URL.
    """
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return None
            is_html = "html" in resp.headers.get("content-type", "").lower()
            final = resp.url
            if final.path.endswith("/") or is_html:
                path = final.path if final.path.endswith("/") else final.path + "/"
                return urlparse(str(final))._replace(path=path, query="", fragment="").geturl()
    except (httpx.HTTPError, httpx.InvalidURL):
        pass
    return None


async def crawl(client, job, start_url: str):
    """Breadth-first crawl. Returns a list of (file_url, relative_path)."""
    start = httpx.URL(start_url)
    base_path = urlparse(start_url).path
    root_name = unquote([s for s in base_path.split("/") if s][-1]) if base_path.strip("/") else start.host
    seen_dirs, seen_files, found = {start_url}, set(), []
    queue = deque([(start_url, 0)])
    first_page = True
    job["folders"] = 1

    while queue:
        dir_url, depth = queue.popleft()
        job["current"] = dir_url
        try:
            html = await fetch_listing(client, dir_url)
        except FolderFetchError as exc:
            if first_page:
                raise  # the starting page must work
            job["warnings"].append(str(exc))
            continue
        if html is None:
            if first_page:
                raise FolderFetchError("No directory listing found: the URL did not return an HTML page.")
            continue

        dirs, files = extract_entries(dir_url, html, start, base_path)
        if first_page and not dirs and not files:
            raise FolderFetchError("No directory listing found at this URL (no links to files or folders).")
        first_page = False

        for f in sorted(files):
            if f in seen_files:
                continue
            seen_files.add(f)
            # No extension + no trailing slash: might actually be a folder.
            if "." not in urlparse(f).path.rsplit("/", 1)[-1]:
                as_dir = await probe_is_directory(client, f)
                if as_dir and urlparse(as_dir).path.startswith(base_path) \
                        and urlparse(as_dir).netloc.lower() == start.netloc.decode().lower():
                    dirs.add(as_dir)
                    continue
            rel = safe_relative_path(root_name, unquote(urlparse(f).path[len(base_path):]))
            if rel is None:
                continue
            if len(found) >= MAX_FILES:
                raise FolderFetchError(f"Download limit reached: more than {MAX_FILES} files found.")
            found.append((f, rel))
            job["files"] = len(found)

        for d in sorted(dirs):
            if d in seen_dirs:
                continue
            seen_dirs.add(d)
            if depth + 1 > MAX_DEPTH:
                job["warnings"].append(f"Skipped (max depth {MAX_DEPTH}): {d}")
                continue
            if len(seen_dirs) > MAX_FOLDERS:
                raise FolderFetchError(f"Download limit reached: more than {MAX_FOLDERS} folders found.")
            queue.append((d, depth + 1))
            job["folders"] = len(seen_dirs)

    return found


# ----------------------------------------------------------------------------
# Downloading and zipping
# ----------------------------------------------------------------------------
async def download_file(client, job, url: str, dest: Path, host: str) -> None:
    """Stream one file to disk, enforcing the total-size limit."""
    written = 0
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if resp.url.host != host:
                raise FolderFetchError("Redirected to another domain; file skipped.")
            length = resp.headers.get("content-length", "")
            if length.isdigit() and job["bytes"] + int(length) > MAX_TOTAL_BYTES:
                raise FolderFetchError("Download limit reached: total size limit exceeded.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    written += len(chunk)
                    if job["bytes"] + written > MAX_TOTAL_BYTES:
                        raise FolderFetchError("Download limit reached: total size limit exceeded.")
                    fh.write(chunk)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        dest.unlink(missing_ok=True)
        raise translate_httpx_error(exc, url)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    job["bytes"] += written


def make_zip(files_dir: Path, zip_path: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(files_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(files_dir).as_posix())
    except (OSError, zipfile.BadZipFile) as exc:
        raise FolderFetchError(f"ZIP creation failed: {exc}")


async def run_job(job_id: str, url: str) -> None:
    job = JOBS[job_id]
    work = WORK_ROOT / job_id
    files_dir = work / "files"
    try:
        await assert_public_host(url)
        files_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
            max_redirects=5,
            headers={"User-Agent": "FolderFetch/1.0 (local tool)"},
            event_hooks={"request": [lambda req: assert_public_host(str(req.url))]},
        ) as client:
            job.update(state="scanning", message="Scanning directory...")
            found = await crawl(client, job, url)
            if not found:
                raise FolderFetchError("No downloadable files found in this folder.")

            job.update(state="downloading", message="Downloading files...")
            host = httpx.URL(url).host
            failed = 0
            for i, (file_url, rel) in enumerate(found):
                job["current"] = rel.as_posix()
                dest = (files_dir / rel).resolve()
                if files_dir.resolve() not in dest.parents:  # final path-traversal guard
                    continue
                try:
                    await download_file(client, job, file_url, dest, host)
                except FolderFetchError as exc:
                    if "limit reached" in str(exc):
                        raise
                    failed += 1
                    job["warnings"].append(f"{rel.as_posix()}: {exc}")
                except OSError as exc:  # e.g. file/folder name clash on Windows
                    failed += 1
                    job["warnings"].append(f"{rel.as_posix()}: could not save ({exc.__class__.__name__})")
                job["done"] = i + 1
                job["percent"] = int(90 * (i + 1) / len(found))
            if failed == len(found):
                raise FolderFetchError("Every file failed to download. See details below.")

        job.update(state="zipping", message="Creating ZIP...", current="", percent=92)
        zip_path = work / f"{rel_root_name(url)}.zip"
        await asyncio.to_thread(make_zip, files_dir, zip_path)
        try:
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            final_path = unique_path(DOWNLOADS_DIR / zip_path.name)
            shutil.move(str(zip_path), str(final_path))
        except OSError as exc:
            raise FolderFetchError(f"Could not save the ZIP to {DOWNLOADS_DIR}: {exc}")
        shutil.rmtree(work, ignore_errors=True)  # delete temp files
        job.update(state="done", message="Download complete", percent=100, saved_path=str(final_path))
    except FolderFetchError as exc:
        job.update(state="error", message=str(exc))
        shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # unexpected - still report it politely
        job.update(state="error", message=f"Unexpected error: {exc.__class__.__name__}: {exc}")
        shutil.rmtree(work, ignore_errors=True)


def unique_path(path: Path) -> Path:
    """SLMs.zip -> SLMs (1).zip -> SLMs (2).zip ... so nothing is overwritten."""
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def rel_root_name(url: str) -> str:
    segs = [s for s in urlparse(url).path.split("/") if s]
    name = unquote(segs[-1]) if segs else urlparse(url).hostname
    return re.sub(r'[<>:"/\\|?*]', "_", name) or "download"


def purge_old_jobs() -> None:
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_MAX_AGE_SECONDS
                and v["state"] in ("done", "error")]:
        shutil.rmtree(WORK_ROOT / jid, ignore_errors=True)
        JOBS.pop(jid, None)


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
app = FastAPI(title="FolderFetch")


class DownloadRequest(BaseModel):
    url: str


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    """Validate the URL and start a background job."""
    try:
        url = normalize_start_url(req.url)
    except FolderFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    purge_old_jobs()
    if any(j["state"] in ("queued", "scanning", "downloading", "zipping") for j in JOBS.values()):
        raise HTTPException(status_code=409, detail="A job is already running. Please wait for it to finish.")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = dict(state="queued", message="Starting...", folders=0, files=0, done=0, bytes=0,
                        percent=0, current="", warnings=[], created=time.time())
    asyncio.create_task(run_job(job_id, url))
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return {k: v for k, v in job.items() if k not in ("zip", "created")}


@app.on_event("shutdown")
def cleanup_all() -> None:
    shutil.rmtree(WORK_ROOT, ignore_errors=True)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")

if __name__ == "__main__":
    # Bound to 127.0.0.1 so only this PC can reach the app.
    uvicorn.run(app, host="127.0.0.1", port=8000)
