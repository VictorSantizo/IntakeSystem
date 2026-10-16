r"""
longpaths.py – Windows MAX_PATH survival kit for the intake pipeline.

ScanSnap's "name the file from recognized text" setting produces filenames
hundreds of characters long. On Windows, once a full path passes ~260
characters, plain os.path probes (exists/isfile/getsize) don't raise — they
just report the file missing, so the watcher, the startup sweep AND Check for new items
all silently ignored those scans (reports #42/#48; two files sat stranded in
Unfiled Scans from 7/24 to 7/29 with no error anywhere).

These helpers retry every probe with the extended-length prefix
(\\?\C:\... / \\?\UNC\server\...) that lifts the limit, and
rescue_long_name() renames an over-long file to a short safe name so the
rest of the pipeline (pdfplumber, shutil, the classifier) never has to care.
"""
import os
import re

# Rescue threshold: stay well under MAX_PATH (260) because the pipeline later
# builds staged/sidecar names off this path and needs headroom.
SAFE_PATH_LEN = 220

_UNSAFE_CHARS = re.compile(r'[^A-Za-z0-9 ._&#@$%()\[\]+,-]+')


def extended(path):
    """Extended-length form of an absolute Windows path (lifts MAX_PATH).
    Pass-through on other platforms and for already-prefixed paths."""
    if os.name != "nt" or not path or path.startswith("\\\\?\\"):
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def exists(path):
    return os.path.exists(path) or (path and os.path.exists(extended(path)))


def isfile(path):
    return os.path.isfile(path) or (path and os.path.isfile(extended(path)))


def getsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return os.path.getsize(extended(path))


def getmtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return os.path.getmtime(extended(path))


def rescue_long_name(path, max_len=SAFE_PATH_LEN):
    """If `path` is longer than max_len, rename the file to a short sanitized
    name in the same folder and return the new path; otherwise return `path`
    unchanged. The OCR-garbage original name is truncated, not preserved —
    the pipeline renames staged files to a readable classified name anyway."""
    if not path or len(path) <= max_len:
        return path
    if not isfile(path):
        return path
    folder = os.path.dirname(path)
    stem, ext = os.path.splitext(os.path.basename(path))
    stem = _UNSAFE_CHARS.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    # room for: folder + sep + stem + " (NNN)" + ext, inside max_len
    room = max_len - len(folder) - 1 - 6 - len(ext)
    stem = (stem[:room].rstrip() if room > 8 else "") or "scan"
    for n in range(1000):
        suffix = "" if n == 0 else f" ({n})"
        candidate = os.path.join(folder, f"{stem}{suffix}{ext}")
        if exists(candidate):
            continue
        try:
            os.rename(extended(path), extended(candidate))
            print(f"[LongPath] Over-long scan filename shortened to: "
                  f"{os.path.basename(candidate)}")
            return candidate
        except OSError as e:
            print(f"[LongPath] Could not shorten {os.path.basename(path)[:60]}...: {e}")
            return path
    return path
