"""
scan_connector.py – Klick Klack scan connector (one per office workstation).

Reports this computer's ScanSnap status to the Klick Klack server every ~20s so
the /scan page can show whether THIS machine is ready and walk the user through
whatever's missing (reports #23/#18/#13; see scanflow.py + SCANNER_DESIGN.md).

It reports; it never scans, moves files, or touches the database. The actual
document path is: ScanSnap "Klick Klack" profile saves into the watched
Unfiled Scans share, and the server ingests from there.

Run from the shared folder with the bundled interpreter (no installs):
    \\\\LACKSSERVER2021\\File-Cabinet\\IntakeSystem\\python\\pythonw.exe
    \\\\LACKSSERVER2021\\File-Cabinet\\IntakeSystem\\scan_connector.py
    (always the UNC path — drive-letter mappings differ per desk)
"Connect My Scanner.bat" starts it and adds a Startup-folder shortcut so it
comes back at every sign-in. Standard library only — workstations have nothing
pip-installed. Reads app_url + connector_token from the same shared config.json
the server uses (workstations already see that file; it holds no more than
they can already read).

A one-line status file is kept at %LOCALAPPDATA%\\KlickKlack\\connector_status.txt
for humans debugging a machine.
"""
import ctypes
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

VERSION = "1.2"            # 1.2: command channel — folder dialog + open-Explorer
MIRROR_MAX_PER_BEAT = 25   # keep each heartbeat snappy; the rest follow next beat
BEAT_SECS = 20
CMD_POLL_SECS = 2.5        # command pickup latency; a dict lookup server-side
CMD_POLL_ERR_SECS = 15     # back off when the server is unreachable
LOCK_PORT = 47653          # single-instance guard, one connector per machine
CREATE_NO_WINDOW = 0x08000000

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_XML = os.path.join(os.environ.get("APPDATA", ""),
                           "PFU", "ScanSnap Home", "Managed", "Profiles",
                           "PfuSsHome.xml")
STATUS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "KlickKlack")

def load_shared_config():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def probe_hardware():
    """(scanner_model or "", scansnap_running) via plain system tools —
    pnputil for the device (PFU's USB vendor id is 04C5), tasklist for the
    ScanSnap Home resident processes (they own the scanner's Scan button).
    Deliberately NO powershell here: a hidden powershell.exe loop spawned from
    pythonw every 20s is exactly the shape antivirus heuristics flag (VIPRE
    raised a popup on this office's machines the day this shipped)."""
    scanner = ""
    try:
        out = subprocess.run(["pnputil", "/enum-devices", "/connected"],
                             capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout
        desc, is_pfu = "", False
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Instance ID:"):
                is_pfu = "VID_04C5" in s.upper()
                desc = ""
            elif is_pfu and s.startswith("Device Description:"):
                desc = s.split(":", 1)[1].strip()
            elif is_pfu and s.startswith("Status:"):
                if "started" in s.lower() and desc:
                    scanner = desc
                    break
        # Localized Windows / older pnputil field names: connected + listed
        # is still a solid "it's plugged in", just without the model string.
        if not scanner and "VID_04C5" in out.upper():
            scanner = "ScanSnap"
    except Exception:
        pass
    running = False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout.lower()
        running = any(n in out for n in ("pfuss", "sshtasktray"))
    except Exception:
        pass
    return scanner, running


def profile_ok():
    """Does ScanSnap Home on this machine have a 'Klick Klack' profile?
    Best-effort read of the profiles XML — never blocks a heartbeat."""
    try:
        with open(PROFILE_XML, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    names = re.findall(r"<ProfileName[^>]*>([^<]*)</ProfileName>", text)
    return any("klick" in n.lower() for n in names)


def maybe_mirror(mir):
    r"""Copy new files from this desk's server-side Scan Archive down to the
    local Desktop\SCANS folder (the server names both sides in its heartbeat
    reply; nothing is mirrored unless config.json's scan_copy_mirrors says so).

    One-way and add-only: never deletes anything, and a file the user later
    deletes locally STAYS deleted — a done-list under %LOCALAPPDATA%\KlickKlack
    remembers what was already delivered. Standard library only, like the rest
    of this connector."""
    if not mir:
        return 0
    src = mir.get("src") or ""
    dest = os.path.expandvars(mir.get("dest") or "")
    if not (src and dest and os.path.isdir(src)):
        return 0
    os.makedirs(dest, exist_ok=True)
    done_file = os.path.join(STATUS_DIR, "mirrored.txt")
    done = set()
    try:
        with open(done_file, "r", encoding="utf-8") as f:
            done = {ln.rstrip("\n") for ln in f if ln.strip()}
    except OSError:
        pass
    copied = 0
    try:
        for dirpath, _dirs, files in os.walk(src):
            rel_root = os.path.relpath(dirpath, src)
            for name in files:
                rel = name if rel_root == "." else os.path.join(rel_root, name)
                if rel in done:
                    continue
                target = os.path.join(dest, name)
                base, ext = os.path.splitext(name)
                c = 1
                while os.path.exists(target):   # same name from another year
                    target = os.path.join(dest, "{} ({}){}".format(base, c, ext))
                    c += 1
                shutil.copy2(os.path.join(dirpath, name), target)
                done.add(rel)
                copied += 1
                if copied >= MIRROR_MAX_PER_BEAT:
                    raise StopIteration
    except StopIteration:
        pass
    except Exception:
        pass                                    # a broken share must not kill beats
    if copied:
        try:
            os.makedirs(STATUS_DIR, exist_ok=True)
            with open(done_file, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(done)) + "\n")
        except OSError:
            pass
    return copied


# ── The command channel (server-side: desk_commands.py; reports #34 + #22) ───
# The browser can't pop a native dialog or File Explorer; we can. Commands are
# queued on the server by the staff pages and collected here every ~2.5s —
# still outbound-only, so no listener, no firewall prompt, nothing new for
# antivirus to dislike (plain in-process urllib, no subprocess loop).

_dialog_busy = threading.Lock()   # one folder dialog at a time per desk


def _explorer_args(path, select):
    """The explorer.exe invocation for an open_explorer command — pure, tested.
    /select, takes the target as part of the SAME argument (Explorer's own
    quirk) and highlights the file inside its folder."""
    if select:
        return ["explorer", "/select," + path]
    return ["explorer", path]


def _alt_nudge():
    """Windows won't let a background process throw a window in front of the
    user (foreground lock). A synthetic ALT tap is the well-worn exception
    that lifts it just long enough for our dialog to appear on top instead of
    blinking in the taskbar."""
    try:
        KEYEVENTF_KEYUP = 0x0002
        VK_MENU = 0x12
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _pick_folder_modern(initial):
    """The real Explorer-style folder chooser: IFileOpenDialog with
    FOS_PICKFOLDERS — the exact dialog every upload button opens, in folder
    mode. The bundled embeddable Python has no tkinter, so this is raw ctypes
    COM: create the dialog, flip it to folder mode, seat it at `initial`,
    Show, read the picked IShellItem back as a filesystem path.

    Returns (status, path): ("done", path) | ("cancelled", "") — anything
    else raises and the caller falls back to the classic dialog."""
    from ctypes import POINTER, byref, c_int, c_uint, c_ulong, c_void_p, c_wchar_p
    from ctypes import HRESULT, oledll, windll

    CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
    IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
    IID_IShellItem = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
    CLSCTX_INPROC_SERVER = 1
    FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM = 0x20, 0x40
    SIGDN_FILESYSPATH = 0x80058000
    HR_CANCELLED = -2147023673          # HRESULT of ERROR_CANCELLED

    class GUID(ctypes.Structure):
        _fields_ = [("d1", c_ulong), ("d2", ctypes.c_ushort),
                    ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        def __init__(self, s):
            super().__init__()
            oledll.ole32.CLSIDFromString(c_wchar_p(s), byref(self))

    def vtbl_call(obj, index, restype, *argtypes_and_args):
        """Call slot `index` of a COM object's vtable."""
        n = len(argtypes_and_args) // 2
        argtypes = argtypes_and_args[:n]
        args = argtypes_and_args[n:]
        vtable = ctypes.cast(obj, POINTER(c_void_p)).contents.value
        slot = ctypes.cast(vtable + index * ctypes.sizeof(c_void_p),
                           POINTER(c_void_p)).contents.value
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        return proto(slot)(obj, *args)

    ole32 = oledll.ole32
    ole32.CoInitializeEx(None, 2)       # COINIT_APARTMENTTHREADED
    try:
        dialog = c_void_p()
        ole32.CoCreateInstance(byref(GUID(CLSID_FileOpenDialog)), None,
                               CLSCTX_INPROC_SERVER,
                               byref(GUID(IID_IFileOpenDialog)), byref(dialog))
        try:
            # IFileDialog vtable: 9=SetOptions, 10=GetOptions, 12=SetFolder,
            # 17=SetTitle, 3=IModalWindow::Show, 20=GetResult, 2=Release.
            opts = c_uint()
            vtbl_call(dialog, 10, HRESULT, POINTER(c_uint), byref(opts))
            vtbl_call(dialog, 9, HRESULT, c_uint,
                      opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)
            vtbl_call(dialog, 17, HRESULT, c_wchar_p,
                      "Choose a folder for Klick Klack")
            if initial:
                item = c_void_p()
                try:
                    windll.shell32.SHCreateItemFromParsingName(
                        c_wchar_p(initial), None,
                        byref(GUID(IID_IShellItem)), byref(item))
                except OSError:
                    item = None          # stale/unreachable start dir: open anywhere
                if item:
                    vtbl_call(dialog, 12, HRESULT, c_void_p, item)
                    vtbl_call(item, 2, c_ulong)                    # Release
            _alt_nudge()
            proto = ctypes.WINFUNCTYPE(c_int, c_void_p, c_void_p)
            vtable = ctypes.cast(dialog, POINTER(c_void_p)).contents.value
            show = ctypes.cast(vtable + 3 * ctypes.sizeof(c_void_p),
                               POINTER(c_void_p)).contents.value
            hr = proto(show)(dialog, None)
            if hr == HR_CANCELLED:
                return "cancelled", ""
            if hr != 0:
                raise OSError(f"IFileDialog.Show HRESULT {hr:#010x}")
            result = c_void_p()
            vtbl_call(dialog, 20, HRESULT, POINTER(c_void_p), byref(result))
            try:
                name = c_wchar_p()
                vtbl_call(result, 5, HRESULT, c_uint, POINTER(c_wchar_p),
                          SIGDN_FILESYSPATH, byref(name))   # IShellItem 5=GetDisplayName
                path = name.value or ""
                ole32.CoTaskMemFree(name)
                return ("done", path) if path else ("cancelled", "")
            finally:
                vtbl_call(result, 2, c_ulong)                      # Release
        finally:
            vtbl_call(dialog, 2, c_ulong)                          # Release
    finally:
        ole32.CoUninitialize()


def _pick_folder_classic(initial):
    """SHBrowseForFolder — the old tree dialog. Only ever the fallback when
    the modern one fails; works on anything that can spell Windows."""
    from ctypes import byref, c_void_p, c_wchar_p, windll

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [("hwndOwner", c_void_p), ("pidlRoot", c_void_p),
                    ("pszDisplayName", c_wchar_p), ("lpszTitle", c_wchar_p),
                    ("ulFlags", ctypes.c_uint), ("lpfn", c_void_p),
                    ("lParam", c_void_p), ("iImage", ctypes.c_int)]

    # Declare the pointer-sized return/args or 64-bit ctypes truncates them
    # to c_int and the process crashes on the second call.
    windll.shell32.SHBrowseForFolderW.restype = c_void_p
    windll.shell32.SHGetPathFromIDListW.argtypes = [c_void_p, c_wchar_p]
    windll.ole32.CoTaskMemFree.argtypes = [c_void_p]
    windll.ole32.CoInitialize(None)
    try:
        bi = BROWSEINFO()
        bi.lpszTitle = "Choose a folder for Klick Klack"
        bi.ulFlags = 0x0041          # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
        _alt_nudge()
        pidl = windll.shell32.SHBrowseForFolderW(byref(bi))
        if not pidl:
            return "cancelled", ""
        buf = ctypes.create_unicode_buffer(1024)
        ok = windll.shell32.SHGetPathFromIDListW(pidl, buf)
        windll.ole32.CoTaskMemFree(pidl)
        return ("done", buf.value) if ok and buf.value else ("cancelled", "")
    finally:
        windll.ole32.CoUninitialize()


def _post(app_url, path, payload):
    req = urllib.request.Request(
        app_url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def _run_pick_folder(app_url, token, cmd):
    """Worker thread: the dialog blocks for as long as the person browses."""
    if not _dialog_busy.acquire(blocking=False):
        status, path = "error", ""       # a dialog is already open on this desk
    else:
        try:
            try:
                status, path = _pick_folder_modern(cmd.get("initial") or "")
            except Exception:
                try:
                    status, path = _pick_folder_classic(cmd.get("initial") or "")
                except Exception:
                    status, path = "error", ""
        finally:
            _dialog_busy.release()
    try:
        _post(app_url, "/api/connector/command_result",
              {"token": token, "id": cmd.get("id"), "status": status,
               "path": path})
    except Exception:
        pass                             # result lost -> the page times out honestly


def _handle_command(app_url, token, cmd):
    kind = cmd.get("type")
    if kind == "pick_folder":
        threading.Thread(target=_run_pick_folder,
                         args=(app_url, token, cmd), daemon=True).start()
    elif kind == "open_explorer":
        path = str(cmd.get("path") or "")
        if path.startswith("\\\\"):      # only ever UNC — never local paths
            try:
                subprocess.Popen(_explorer_args(path, bool(cmd.get("select"))))
            except Exception:
                pass
    # unknown types: a newer server than connector — ignore quietly


def command_loop():
    """Second daemon thread beside the heartbeat: collect queued commands for
    this desk. Short interval is fine — it's one tiny in-process HTTP GET-
    shaped POST, not a spawned process (the VIPRE lesson is about the latter)."""
    while True:
        wait = CMD_POLL_SECS
        try:
            cfg = load_shared_config()
            app_url = (cfg.get("app_url") or "http://192.168.10.240:5050").rstrip("/")
            token = cfg.get("connector_token") or ""
            body = _post(app_url, "/api/connector/commands",
                         {"token": token, "machine": socket.gethostname()})
            for cmd in body.get("commands") or []:
                _handle_command(app_url, token, cmd)
            wait = float(body.get("poll_seconds") or CMD_POLL_SECS)
        except Exception:
            wait = CMD_POLL_ERR_SECS
        time.sleep(wait)


def write_status(line):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(os.path.join(STATUS_DIR, "connector_status.txt"), "w",
                  encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + line + "\n")
    except OSError:
        pass


def main():
    # Second instance (double-clicked twice, plus the Startup shortcut)? The
    # port is taken — quietly leave the first one to it.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return

    machine = socket.gethostname()
    user = getpass.getuser()
    # The command channel (reports #34/#22) runs beside the heartbeat so a
    # folder dialog sitting open never stops the status beats.
    threading.Thread(target=command_loop, daemon=True).start()
    while True:
        try:
            cfg = load_shared_config()
            app_url = (cfg.get("app_url") or "http://192.168.10.240:5050").rstrip("/")
            token = cfg.get("connector_token") or ""
            scanner, running = probe_hardware()
            payload = {
                "token": token,
                "machine": machine,
                "user": user,
                "scanner_present": bool(scanner),
                "scanner_model": scanner,
                "scansnap_running": running,
                "profile_ok": profile_ok(),
                "version": VERSION,
            }
            req = urllib.request.Request(
                app_url + "/api/connector/heartbeat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            mirrored = maybe_mirror(body.get("mirror"))
            write_status(f"ok  scanner={scanner or 'none'}  "
                         f"scansnap_running={running}  profile={payload['profile_ok']}"
                         + (f"  mirrored={mirrored}" if mirrored else ""))
        except Exception as e:                    # noqa: BLE001 — must never die
            write_status(f"error  {type(e).__name__}: {e}")
        time.sleep(BEAT_SECS)


if __name__ == "__main__":
    main()
