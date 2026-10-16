"""
watcher.py – Watches a folder for new PDF files from the ScanSnap.
"""
import time, os, traceback
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import longpaths

# Document types we ingest: PDFs, images (phone photos / scans), and Word docs.
SUPPORTED_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".docx")


def _wait_until_stable(path, timeout=30):
    """Wait until the file's size stops changing (two identical non-zero
    readings 1s apart), up to `timeout` seconds. A multi-page scan takes the
    ScanSnap several seconds to write; the old fixed 2s pause let a
    half-written PDF through to the classifier."""
    last = -1
    for _ in range(timeout):
        try:
            size = longpaths.getsize(path)
        except OSError:
            return False   # vanished (e.g. scanner writing under a temp name)
        if size > 0 and size == last:
            return True
        last = size
        time.sleep(1)
    return last > 0


class ScanHandler(FileSystemEventHandler):
    def __init__(self, callback, folder=None):
        self.callback = callback
        self.folder = os.path.normcase(os.path.normpath(folder)) if folder else None
        self._seen = set()

    def on_created(self, event):
        if not event.is_directory:
            self._ingest(event.src_path)

    def on_moved(self, event):
        # A file RENAMED into place is just as new to us as a created one:
        # ScanSnap finishes some scans by renaming a temp file, and staff
        # rename garbage-named scans by hand (report #42) — neither fires
        # on_created, so without this the file sat invisible until the next
        # restart sweep.
        if not event.is_directory:
            self._ingest(getattr(event, "dest_path", "") or "")

    def _ingest(self, path):
        if not (path and path.lower().endswith(SUPPORTED_EXTS) and path not in self._seen):
            return
        # Only files directly in the watched folder: a move INTO a subfolder
        # (_Blank/_Duplicates/staged renames headed elsewhere) is an exit, not
        # an arrival, and must not be re-ingested.
        if self.folder and os.path.normcase(os.path.normpath(os.path.dirname(path))) != self.folder:
            return
        if len(self._seen) > 5000:   # bound memory on a long-running server
            self._seen.clear()
        self._seen.add(path)
        # Nothing may escape this handler: an exception here propagates into
        # watchdog's dispatch thread and kills the observer — the app then looks
        # healthy but silently stops ingesting.
        try:
            if _wait_until_stable(path):
                print(f"[Watcher] New scan detected: {path}")
                self.callback(path)
        except Exception:
            print(f"[Watcher] Error handling {path}:\n{traceback.format_exc()}")


def start_watcher(folder: str, callback):
    """Watch `folder` for new documents. Runs until interrupted.

    Resilient to the folder being unavailable — at boot (service starts before a
    network drive is mounted, as after the 6/29 power outage) or mid-run (the
    share drops): instead of letting this daemon thread die and silently ending
    all ingest, it retries every 60s and restarts a dead observer. Files that
    arrive while the folder is unwatched are caught by the app's startup sweep
    or the Check for new items button."""
    handler = ScanHandler(callback, folder)   # reused across restarts, so _seen persists
    while True:
        observer = Observer()
        try:
            os.makedirs(folder, exist_ok=True)
            observer.schedule(handler, folder, recursive=False)
            observer.start()
            print(f"[Watcher] Monitoring: {folder}")
        except Exception as e:
            print(f"[Watcher] Cannot watch {folder} ({e}); retrying in 60s.")
            time.sleep(60)
            continue
        try:
            while observer.is_alive():
                time.sleep(1)
            print(f"[Watcher] Observer for {folder} stopped unexpectedly; restarting in 60s.")
        except (KeyboardInterrupt, SystemExit):
            observer.stop()
            observer.join()
            return
        observer.stop()
        try:
            observer.join(5)
        except Exception:
            pass
        time.sleep(60)
