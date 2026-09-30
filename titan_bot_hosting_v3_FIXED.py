# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║                 TITAN BOT HOSTING v2.0                     ║
║        Personal Telegram Bot Hosting Controller             ║
╚══════════════════════════════════════════════════════════════╝

Environment variables:
    HOST_BOT_TOKEN   = Telegram token of the hosting controller
    OWNER_ID         = Telegram numeric user ID of the owner

Optional:
    MAX_UPLOAD_MB    = 50
    MAX_LOG_LINES    = 300
    AUTO_RESTART     = 1
    RESTART_DELAY    = 5
    PIP_TIMEOUT      = 600
    MAX_PROJECTS     = 30
    MAX_ZIP_FILES    = 5000
    MAX_EXTRACT_MB   = 200

Required package:
    pip install pyTelegramBotAPI
"""

import os
import re
import sys
import json
import time
import shutil
import signal
import zipfile
import threading
import subprocess
from pathlib import Path
from datetime import datetime

import telebot
from telebot import types


# ============================================================
# CONFIG
# ============================================================

BASE = Path(__file__).resolve().parent
DATA = BASE / "hosting_data"
PROJECTS = DATA / "projects"
LOGS = DATA / "logs"
DB_FILE = DATA / "hosting.json"

for directory in (DATA, PROJECTS, LOGS):
    directory.mkdir(parents=True, exist_ok=True)

TOKEN = "8806896770:AAF95ai1N2-jaCdhQmMwK95oJX1ELEEFsDE"
OWNER_ID = 8447733447

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 50)
MAX_LOG_LINES = env_int("MAX_LOG_LINES", 300)
RESTART_DELAY = env_int("RESTART_DELAY", 5)
PIP_TIMEOUT = env_int("PIP_TIMEOUT", 600)
MAX_PROJECTS = env_int("MAX_PROJECTS", 30)
MAX_ZIP_FILES = env_int("MAX_ZIP_FILES", 5000)
MAX_EXTRACT_MB = env_int("MAX_EXTRACT_MB", 200)
AUTO_RESTART = os.getenv("AUTO_RESTART", "1").strip() == "1"

if not TOKEN:
    raise RuntimeError("HOST_BOT_TOKEN is not set.")

if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID is not set or invalid.")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ============================================================
# RUNTIME STATE
# ============================================================

lock = threading.RLock()

# pid -> subprocess.Popen
processes = {}

# project metadata loaded from JSON
project_meta = {}

# owner ids currently waiting for an upload
upload_waiting = set()

# project -> generation number
# Used to prevent old process monitors from restarting newer processes.
restart_generation = {}

# project -> last process start time
started_at = {}

# Per-project operation locks prevent double-click races.
project_locks = {}

# ============================================================
# DATABASE
# ============================================================

def save_db():
    with lock:
        tmp = DB_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(project_meta, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        tmp.replace(DB_FILE)


def load_db():
    global project_meta

    if not DB_FILE.exists():
        project_meta = {}
        return

    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            project_meta = data
        else:
            project_meta = {}
    except Exception:
        project_meta = {}


load_db()

# ============================================================
# SECURITY / HELPERS
# ============================================================

def owner_only(message):
    return getattr(message.from_user, "id", 0) == OWNER_ID


def safe_name(name, default="project"):
    name = Path(str(name)).name
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    name = name.strip("._")
    return name[:80] or default


def project_id_from_name(name):
    stem = Path(name).stem
    pid = safe_name(stem, "bot").lower()
    pid = re.sub(r"[^a-z0-9_-]+", "_", pid)
    return pid[:40] or "bot"


def unique_project_id(base_pid):
    candidate = base_pid
    counter = 2

    while candidate in project_meta or project_dir(candidate).exists():
        candidate = f"{base_pid}_{counter}"
        counter += 1

    return candidate


def project_dir(pid):
    return PROJECTS / pid


def get_project_lock(pid):
    with lock:
        return project_locks.setdefault(pid, threading.RLock())


def log_file(pid):
    return LOGS / f"{pid}.log"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def format_duration(seconds):
    if seconds is None:
        return "—"

    try:
        seconds = max(0, int(seconds))
    except Exception:
        return "—"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []

    if days:
        parts.append(f"{days}ي")
    if hours:
        parts.append(f"{hours}س")
    if minutes:
        parts.append(f"{minutes}د")

    parts.append(f"{seconds}ث")

    return " ".join(parts)


def append_log(pid, line):
    try:
        path = log_file(pid)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with path.open("a", encoding="utf-8", errors="replace") as file:
            file.write(f"[{stamp}] {str(line).rstrip()}\n")

        # Keep the log file under control.
        try:
            lines = path.read_text(
                encoding="utf-8",
                errors="replace"
            ).splitlines()

            max_keep = max(MAX_LOG_LINES * 4, 100)

            if len(lines) > max_keep:
                path.write_text(
                    "\n".join(lines[-max_keep:]) + "\n",
                    encoding="utf-8"
                )
        except Exception:
            pass

    except Exception as exc:
        print("[LOG ERROR]", exc)


def read_logs(pid, limit=MAX_LOG_LINES):
    path = log_file(pid)

    if not path.exists():
        return "لا توجد سجلات حتى الآن."

    try:
        lines = path.read_text(
            encoding="utf-8",
            errors="replace"
        ).splitlines()

        return "\n".join(lines[-limit:]) or "لا توجد سجلات حتى الآن."

    except Exception as exc:
        return f"تعذر قراءة السجل: {exc}"


def process_alive(pid):
    proc = processes.get(pid)

    if not proc:
        return False

    try:
        alive = proc.poll() is None
        if not alive:
            with lock:
                if processes.get(pid) is proc:
                    processes.pop(pid, None)
        return alive
    except Exception:
        return False


def project_state(pid):
    meta = project_meta.get(pid, {})
    if process_alive(pid):
        return "🟢", "شغال"
    if meta.get("enabled", False):
        return "🟡", "متوقف — Auto Restart مفعّل"
    return "🔴", "متوقف"


def set_project_enabled(pid, enabled):
    with lock:
        project_meta.setdefault(pid, {})["enabled"] = bool(enabled)
        project_meta[pid]["updated_at"] = now_iso()
        restart_generation[pid] = restart_generation.get(pid, 0) + 1
        save_db()


def get_project_size(folder):
    total = 0

    try:
        for path in folder.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass

    return total


def human_size(size):
    try:
        size = float(size)
    except Exception:
        return "0 B"

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} PB"


# ============================================================
# PROJECT ENTRY
# ============================================================

def find_entry(folder):
    meta = project_meta.get(folder.name, {})
    configured = meta.get("entry")

    if configured:
        candidate = (folder / configured).resolve()

        try:
            candidate.relative_to(folder.resolve())
        except ValueError:
            candidate = None

        if (
            candidate
            and candidate.is_file()
            and candidate.suffix.lower() == ".py"
        ):
            return candidate

    preferred = (
        "main.py",
        "bot.py",
        "app.py",
        "run.py",
        "start.py",
        "index.py",
    )

    for name in preferred:
        candidate = folder / name
        if candidate.is_file():
            return candidate

    py_files = [
        p for p in folder.glob("*.py")
        if p.name != "__init__.py"
    ]

    return py_files[0] if len(py_files) == 1 else None


def list_python_entries(folder):
    result = []

    try:
        for path in folder.rglob("*.py"):
            if ".venv" not in path.parts:
                result.append(path.relative_to(folder))
    except Exception:
        pass

    return sorted(result)


# ============================================================
# ZIP SECURITY
# ============================================================

def safe_extract_zip(zip_path, destination):
    destination = destination.resolve()

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()

        if len(members) > MAX_ZIP_FILES:
            raise RuntimeError(
                f"الـZIP يحتوي على أكثر من {MAX_ZIP_FILES} ملف."
            )

        total_bytes = 0
        max_bytes = MAX_EXTRACT_MB * 1024 * 1024

        for info in members:
            filename = info.filename

            if not filename:
                continue

            total_bytes += max(0, info.file_size)

            if total_bytes > max_bytes:
                raise RuntimeError(
                    f"حجم المشروع بعد الفك أكبر من {MAX_EXTRACT_MB}MB."
                )

            target = (destination / filename).resolve()

            try:
                target.relative_to(destination)
            except ValueError:
                raise RuntimeError(
                    "تم رفض ZIP يحتوي على مسار ملفات غير آمن."
                )

            mode = info.external_attr >> 16

            # Reject symbolic links.
            if (mode & 0o170000) == 0o120000:
                raise RuntimeError(
                    "الروابط الرمزية داخل ZIP غير مسموحة."
                )

        archive.extractall(destination)


def flatten_single_folder(folder):
    try:
        items = list(folder.iterdir())
    except Exception:
        return

    if len(items) != 1 or not items[0].is_dir():
        return

    child = items[0]

    # Do not flatten a virtual environment or suspicious directory.
    if child.name == ".venv":
        return

    temp = folder.parent / (
        f".flatten_{folder.name}_{int(time.time() * 1000)}"
    )

    child.rename(temp)

    try:
        for item in list(temp.iterdir()):
            target = folder / item.name

            if target.exists():
                if target.is_dir() and item.is_dir():
                    shutil.copytree(
                        item,
                        target,
                        dirs_exist_ok=True
                    )
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)

                    shutil.move(str(item), str(target))
            else:
                shutil.move(str(item), str(target))

    finally:
        shutil.rmtree(temp, ignore_errors=True)


# ============================================================
# PYTHON ENVIRONMENT / PACKAGES
# ============================================================

def venv_python(folder):
    if os.name == "nt":
        return folder / ".venv" / "Scripts" / "python.exe"

    return folder / ".venv" / "bin" / "python"


def create_venv(folder):
    vp = venv_python(folder)

    if vp.exists():
        return vp

    append_log(
        folder.name,
        "Creating isolated virtual environment..."
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            str(folder / ".venv"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        output = (
            result.stderr
            or result.stdout
            or "Unknown venv error"
        )

        raise RuntimeError(
            "فشل إنشاء البيئة الافتراضية:\n"
            + output[-1800:]
        )

    return vp


MODULE_TO_PACKAGE = {
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "requests": "requests",
    "aiohttp": "aiohttp",
    "bs4": "beautifulsoup4",
    "pymongo": "pymongo",
    "sqlalchemy": "SQLAlchemy",
    "flask": "Flask",
    "django": "Django",
    "numpy": "numpy",
    "pandas": "pandas",
    "PIL": "Pillow",
    "pyrogram": "pyrogram",
    "telethon": "telethon",
    "aiogram": "aiogram",
    "discord": "discord.py",
    "cv2": "opencv-python",
    "matplotlib": "matplotlib",
    "scipy": "scipy",
    "sklearn": "scikit-learn",
    "qrcode": "qrcode[pil]",
    "yt_dlp": "yt-dlp",
    "youtube_dl": "youtube_dl",
    "colorama": "colorama",
    "pyfiglet": "pyfiglet",
    "termcolor": "termcolor",
    "tqdm": "tqdm",
    "pyperclip": "pyperclip",
    "cryptography": "cryptography",
    "Crypto": "pycryptodome",
    "psutil": "psutil",
    "pytz": "pytz",
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
    "lxml": "lxml",
    "dateutil": "python-dateutil",
    "websocket": "websocket-client",
    "websockets": "websockets",
    "jwt": "PyJWT",
    "openpyxl": "openpyxl",
    "pydantic": "pydantic",
    "rich": "rich",
    "loguru": "loguru",
    "faker": "Faker",
    "httpx": "httpx",
}


def detect_imports(entry):
    found = set()

    try:
        root = entry.parent
        py_files = [
            p for p in root.rglob("*.py")
            if ".venv" not in p.parts
        ]
    except Exception:
        py_files = [entry]

    for py_file in py_files:
        try:
            text = py_file.read_text(
                encoding="utf-8",
                errors="ignore"
            )
        except Exception:
            continue

        # import foo
        # from foo import bar
        for match in re.finditer(
            r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)",
            text,
            re.MULTILINE,
        ):
            module = match.group(1)

            if module in MODULE_TO_PACKAGE:
                found.add(MODULE_TO_PACKAGE[module])

    return sorted(found)


def run_pip(vp, args, timeout=PIP_TIMEOUT):
    command = [
        str(vp),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
    ] + args

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    output = (
        (result.stdout or "")
        + "\n"
        + (result.stderr or "")
    ).strip()

    return result.returncode, output


def install_requirements(pid, folder, entry):
    vp = create_venv(folder)

    # Upgrade pip quietly.
    try:
        rc, output = run_pip(
            vp,
            ["--upgrade", "pip"],
            timeout=min(PIP_TIMEOUT, 300),
        )
        append_log(
            pid,
            "pip upgrade:\n" + output[-2500:]
        )
    except Exception as exc:
        append_log(pid, f"pip upgrade skipped: {exc}")

    req = folder / "requirements.txt"

    if req.exists():
        append_log(pid, "Installing requirements.txt...")

        rc, output = run_pip(
            vp,
            ["-r", str(req)]
        )

        append_log(pid, output[-7000:])

        if rc != 0:
            raise RuntimeError(
                "فشل تثبيت requirements.txt. راجع السجل."
            )

    detected = detect_imports(entry)

    if detected:
        append_log(
            pid,
            "Checking detected packages: "
            + ", ".join(detected)
        )

        rc, output = run_pip(vp, detected)

        append_log(pid, output[-7000:])

        if rc != 0:
            raise RuntimeError(
                "فشل تثبيت بعض المكتبات المكتشفة. راجع السجل."
            )
    elif not req.exists():
        append_log(
            pid,
            "No external requirements detected."
        )


# ============================================================
# PROCESS CONTROL
# ============================================================

def kill_process(pid):
    proc = processes.get(pid)

    if not proc:
        return False

    try:
        if proc.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(
                        os.getpgid(proc.pid),
                        signal.SIGTERM
                    )
                except ProcessLookupError:
                    pass

                try:
                    proc.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(
                            os.getpgid(proc.pid),
                            signal.SIGKILL
                        )
                    except ProcessLookupError:
                        pass

            else:
                proc.terminate()

                try:
                    proc.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    proc.kill()

        return True

    except Exception as exc:
        append_log(pid, f"Stop error: {exc}")
        return False


def stream_output(pid, proc):
    try:
        if proc.stdout is None:
            append_log(pid, "stdout is unavailable.")
            return

        for line in iter(proc.stdout.readline, ""):
            if line:
                append_log(pid, line)

        proc.stdout.close()

    except Exception as exc:
        append_log(pid, f"Log reader error: {exc}")


def monitor_process(pid, proc, generation):
    return_code = proc.wait()

    with lock:
        if processes.get(pid) is proc:
            processes.pop(pid, None)

        current_generation = restart_generation.get(pid, 0)
        enabled = project_meta.get(pid, {}).get(
            "enabled",
            False
        )

    append_log(
        pid,
        f"Process exited with code {return_code}"
    )

    if (
        return_code != 0
        and AUTO_RESTART
        and enabled
        and generation == current_generation
    ):
        append_log(
            pid,
            f"Auto restart in {RESTART_DELAY}s..."
        )

        time.sleep(max(1, RESTART_DELAY))

        with lock:
            still_enabled = project_meta.get(
                pid,
                {}
            ).get("enabled", False)

            still_current = (
                restart_generation.get(pid, 0)
                == generation
            )

            already_running = process_alive(pid)

        if (
            still_enabled
            and still_current
            and not already_running
        ):
            try:
                start_project(pid, auto=True)

            except Exception as exc:
                append_log(
                    pid,
                    f"Auto restart failed: {exc}"
                )


def start_project(pid, auto=False):
    folder = project_dir(pid)

    if not folder.exists():
        raise RuntimeError("المشروع غير موجود.")

    if process_alive(pid):
        return True, "البوت شغال بالفعل."

    entry = find_entry(folder)

    if not entry:
        entries = list_python_entries(folder)

        if len(entries) > 1:
            raise RuntimeError(
                "فيه أكتر من ملف Python ومفيش ملف تشغيل محدد.\n"
                "استخدم main.py / bot.py أو ارفع المشروع مرة تانية."
            )

        raise RuntimeError(
            "مش لاقي ملف تشغيل Python."
        )

    # Syntax check before installing packages.
    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "py_compile",
            str(entry),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if check.returncode != 0:
        error = (
            check.stderr
            or check.stdout
            or "Syntax error"
        )[-3500:]

        append_log(pid, error)

        raise RuntimeError(
            "فيه SyntaxError في ملف التشغيل:\n"
            + error
        )

    install_requirements(
        pid,
        folder,
        entry
    )

    vp = venv_python(folder)

    if not vp.exists():
        raise RuntimeError(
            "ملف Python الخاص بالبيئة الافتراضية غير موجود."
        )

    env = os.environ.copy()

    env["PYTHONUNBUFFERED"] = "1"

    # Never leak hosting controller secrets.
    env.pop("HOST_BOT_TOKEN", None)
    env.pop("OWNER_ID", None)

    with lock:
        generation = (
            restart_generation.get(pid, 0)
            + 1
        )

        restart_generation[pid] = generation

        project_meta.setdefault(pid, {})

        project_meta[pid]["entry"] = str(
            entry.relative_to(folder)
        )

        project_meta[pid]["enabled"] = True
        project_meta[pid]["updated_at"] = now_iso()

        save_db()

    append_log(
        pid,
        f"Starting: {entry.name}"
    )

    try:
        proc = subprocess.Popen(
            [
                str(vp),
                "-u",
                str(entry),
            ],
            cwd=str(folder),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=(
                os.name == "posix"
            ),
        )

    except Exception:
        with lock:
            restart_generation[pid] = (
                generation + 1
            )
        raise

    with lock:
        processes[pid] = proc
        started_at[pid] = time.time()

    threading.Thread(
        target=stream_output,
        args=(pid, proc),
        daemon=True,
    ).start()

    threading.Thread(
        target=monitor_process,
        args=(
            pid,
            proc,
            generation,
        ),
        daemon=True,
    ).start()

    return True, (
        f"تم تشغيل {pid}."
        if not auto
        else f"تمت إعادة تشغيل {pid} تلقائيًا."
    )


def stop_project(pid, disable=True):
    if disable:
        with lock:
            project_meta.setdefault(
                pid,
                {}
            )["enabled"] = False

            restart_generation[pid] = (
                restart_generation.get(pid, 0)
                + 1
            )

            save_db()

    stopped = kill_process(pid)

    if stopped:
        append_log(
            pid,
            "Stopped by owner."
        )

    return stopped


def restart_project(pid):
    with lock:
        project_meta.setdefault(
            pid,
            {}
        )["enabled"] = True

        restart_generation[pid] = (
            restart_generation.get(pid, 0)
            + 1
        )

        save_db()

    kill_process(pid)

    append_log(
        pid,
        "Manual restart requested."
    )

    time.sleep(1)

    return start_project(pid)


def restore_enabled_projects():
    # Small delay gives the hosting controller time to initialize.
    time.sleep(2)

    for pid, meta in list(project_meta.items()):
        if not meta.get("enabled", False):
            continue

        try:
            start_project(pid, auto=True)

            append_log(
                pid,
                "Restored after hosting controller restart."
            )

        except Exception as exc:
            append_log(
                pid,
                f"Restore failed: {exc}"
            )


def shutdown_all():
    # Temporary shutdown. Projects remain enabled in DB.
    for pid in list(processes):
        try:
            kill_process(pid)
        except Exception:
            pass


def main_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🤖 إدارة البوتات", callback_data="bots:0"),
        types.InlineKeyboardButton("📤 رفع بوت", callback_data="upload"),
    )
    keyboard.add(
        types.InlineKeyboardButton("📊 لوحة الحالة", callback_data="status"),
        types.InlineKeyboardButton("🛑 إيقاف الكل", callback_data="stop_all"),
    )
    keyboard.add(
        types.InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="home"),
    )
    return keyboard


# ============================================================
# UI / CONTROL HELPERS
# ============================================================

def esc(value):
    return telebot.util.escape(str(value))


def truncate_log(text, limit=3500):
    text = str(text or "")
    return text[-limit:] if len(text) > limit else text


def project_button_label(pid):
    icon, state = project_state(pid)
    return f"{icon} {pid} — {state}"


def projects_keyboard(page=0, per_page=8):
    pids = sorted(project_meta)
    total = len(pids)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))

    keyboard = types.InlineKeyboardMarkup(row_width=1)

    start = page * per_page
    for pid in pids[start:start + per_page]:
        keyboard.add(types.InlineKeyboardButton(
            project_button_label(pid),
            callback_data=f"project:{pid}"
        ))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ السابق", callback_data=f"bots:{page-1}"))
    nav.append(types.InlineKeyboardButton(
        f"📄 {page+1}/{pages}", callback_data="noop"
    ))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("التالي ▶️", callback_data=f"bots:{page+1}"))
    keyboard.row(*nav)

    keyboard.row(
        types.InlineKeyboardButton("🔄 تحديث", callback_data=f"bots:{page}"),
        types.InlineKeyboardButton("🏠 الرئيسية", callback_data="home"),
    )
    return keyboard


def project_keyboard(pid):
    running = process_alive(pid)
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    if running:
        keyboard.add(
            types.InlineKeyboardButton("🛑 إيقاف", callback_data=f"stop:{pid}"),
            types.InlineKeyboardButton("🔄 إعادة تشغيل", callback_data=f"restart:{pid}"),
        )
    else:
        keyboard.add(
            types.InlineKeyboardButton("🚀 تشغيل", callback_data=f"start:{pid}"),
            types.InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete:{pid}"),
        )

    keyboard.add(
        types.InlineKeyboardButton("📜 السجل", callback_data=f"logs:{pid}"),
        types.InlineKeyboardButton("ℹ️ التفاصيل", callback_data=f"info:{pid}"),
    )

    keyboard.add(
        types.InlineKeyboardButton("📄 ملف التشغيل", callback_data=f"entry:{pid}"),
        types.InlineKeyboardButton(
            "⏱ إيقاف تلقائي" if project_meta.get(pid, {}).get("enabled") else "🔁 تفعيل Auto Restart",
            callback_data=f"toggle:{pid}",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton("⬅️ كل البوتات", callback_data="bots:0"),
        types.InlineKeyboardButton("🏠 الرئيسية", callback_data="home"),
    )
    return keyboard


def dashboard_text():
    running = sum(process_alive(pid) for pid in project_meta)
    total = len(project_meta)
    stopped = max(0, total - running)

    return (
        "╔════════════════════════════════╗\n"
        "║      🚀 <b>TITAN HOSTING</b>       ║\n"
        "║        <i>Control Center</i>         ║\n"
        "╚════════════════════════════════╝\n\n"
        f"🟢 <b>Online:</b> <code>{running}</code>\n"
        f"🔴 <b>Offline:</b> <code>{stopped}</code>\n"
        f"📦 <b>Projects:</b> <code>{total}/{MAX_PROJECTS}</code>\n"
        f"🔁 <b>Global Auto Restart:</b> <code>{'ON' if AUTO_RESTART else 'OFF'}</code>\n\n"
        "🎛️ <b>اختار من لوحة التحكم:</b>"
    )


def bot_list_text():
    if not project_meta:
        return (
            "🤖 <b>البوتات المستضافة</b>\n\n"
            "📭 مفيش بوتات مضافة لسه.\n"
            "اضغط «📤 رفع بوت» لإضافة أول مشروع."
        )

    running = sum(process_alive(pid) for pid in project_meta)
    return (
        "🤖 <b>BOT MANAGER</b>\n\n"
        f"📦 الإجمالي: <b>{len(project_meta)}</b>\n"
        f"🟢 شغال: <b>{running}</b>\n"
        f"🔴 متوقف: <b>{len(project_meta)-running}</b>\n\n"
        "اختار بوت لإدارة التشغيل والإعدادات والـLogs:"
    )


def project_info_text(pid):
    meta = project_meta.get(pid, {})
    folder = project_dir(pid)
    icon, state = project_state(pid)
    started = started_at.get(pid)
    uptime = format_duration(time.time() - started) if process_alive(pid) and started else "—"

    size = human_size(get_project_size(folder))
    entries = list_python_entries(folder)
    entry = meta.get("entry", "تلقائي")
    proc = processes.get(pid)
    pid_os = getattr(proc, "pid", "—")

    return (
        f"🤖 <b>{esc(pid)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{icon} <b>الحالة:</b> {esc(state)}\n"
        f"📄 <b>Entry:</b> <code>{esc(entry)}</code>\n"
        f"📁 <b>الحجم:</b> <code>{size}</code>\n"
        f"⏱ <b>Uptime:</b> <code>{uptime}</code>\n"
        f"🧩 <b>Python Files:</b> <code>{len(entries)}</code>\n"
        f"🆔 <b>Process PID:</b> <code>{pid_os}</code>\n"
        f"🔁 <b>Auto Restart:</b> <code>{'ON' if meta.get('enabled') else 'OFF'}</code>\n"
        f"📅 <b>Created:</b> <code>{esc(meta.get('created_at','—'))}</code>\n"
        f"🕐 <b>Updated:</b> <code>{esc(meta.get('updated_at','—'))}</code>"
    )


def entry_keyboard(pid):
    folder = project_dir(pid)
    entries = list_python_entries(folder)
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for entry in entries[:30]:
        selected = project_meta.get(pid, {}).get("entry") == str(entry)
        keyboard.add(types.InlineKeyboardButton(
            ("✅ " if selected else "📄 ") + str(entry),
            callback_data=f"setentry:{pid}:{entry}"
        ))

    keyboard.add(
        types.InlineKeyboardButton("⬅️ رجوع", callback_data=f"project:{pid}")
    )
    return keyboard


def delete_confirm_keyboard(pid):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🗑 نعم، احذف", callback_data=f"delete:{pid}"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data=f"project:{pid}"),
    )
    return keyboard


# ============================================================

# ============================================================
# COMMANDS
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not owner_only(message):
        return

    bot.send_message(
        message.chat.id,
        dashboard_text(),
        reply_markup=main_keyboard()
    )


@bot.message_handler(commands=["bots"])
def cmd_bots(message):
    if not owner_only(message):
        return

    bot.send_message(
        message.chat.id,
        bot_list_text(),
        reply_markup=projects_keyboard(0)
    )


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not owner_only(message):
        return

    bot.send_message(
        message.chat.id,
        dashboard_text(),
        reply_markup=main_keyboard()
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    if not owner_only(message):
        return

    bot.send_message(
        message.chat.id,
        "🧭 <b>TITAN HOSTING — Help</b>\n\n"
        "/start — لوحة التحكم\n"
        "/bots — إدارة البوتات\n"
        "/status — الحالة\n"
        "/cancel — إلغاء وضع الرفع\n"
        "/stopall — إيقاف كل البوتات\n"
        "/logs اسم_البوت — عرض السجل\n\n"
        "📦 رفع المشاريع: .py / .zip"
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if not owner_only(message):
        return

    upload_waiting.discard(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "✅ تم إلغاء وضع رفع الملفات."
    )


@bot.message_handler(commands=["stopall"])
def cmd_stopall(message):
    if not owner_only(message):
        return

    count = 0
    for pid in list(project_meta):
        if process_alive(pid):
            stop_project(pid)
            count += 1

    bot.send_message(
        message.chat.id,
        f"🛑 تم إيقاف <b>{count}</b> بوت."
    )


@bot.message_handler(commands=["logs"])
def cmd_logs(message):
    if not owner_only(message):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        bot.reply_to(
            message,
            "اكتب:\n<code>/logs اسم_البوت</code>"
        )
        return

    pid = parts[1].strip()

    if pid not in project_meta:
        bot.reply_to(
            message,
            "❌ البوت غير موجود."
        )
        return

    log_text = truncate_log(read_logs(pid), 3500)

    bot.send_message(
        message.chat.id,
        f"📜 <b>آخر Logs — {esc(pid)}</b>\n\n"
        f"<pre>{esc(log_text)}</pre>"
    )


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ غير مصرح.", show_alert=True)
        return

    data = call.data or ""

    try:
        if data == "noop":
            bot.answer_callback_query(call.id)
            return

        if data == "home":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                dashboard_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_keyboard()
            )
            return

        if data == "upload":
            upload_waiting.add(call.from_user.id)
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                "📤 <b>وضع رفع المشروع</b>\n\n"
                "ابعت ملف <code>.py</code> أو <code>.zip</code> الآن.\n"
                "يفضل وجود <code>requirements.txt</code>.\n\n"
                "❌ /cancel للإلغاء."
            )
            return

        if data.startswith("bots:"):
            page = int(data.split(":", 1)[1])
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                bot_list_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=projects_keyboard(page)
            )
            return

        if data == "bots":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                bot_list_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=projects_keyboard(0)
            )
            return

        if data == "status":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                dashboard_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_keyboard()
            )
            return

        if data == "stop_all":
            count = 0
            for pid in list(project_meta):
                if process_alive(pid):
                    stop_project(pid)
                    count += 1

            bot.answer_callback_query(call.id, f"🛑 تم إيقاف {count}")
            bot.edit_message_text(
                dashboard_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_keyboard()
            )
            return

        if data.startswith("project:"):
            pid = data.split(":", 1)[1]
            if pid not in project_meta:
                bot.answer_callback_query(call.id, "❌ البوت غير موجود.", show_alert=True)
                return

            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                project_info_text(pid),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=project_keyboard(pid)
            )
            return

        if data.startswith("entry:"):
            pid = data.split(":", 1)[1]
            if pid not in project_meta:
                bot.answer_callback_query(call.id, "❌ البوت غير موجود.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                f"📄 <b>اختيار ملف التشغيل</b>\n\n🤖 <code>{esc(pid)}</code>\n"
                "اختار ملف Python اللي يبدأ منه البوت:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=entry_keyboard(pid)
            )
            return

        if data.startswith("setentry:"):
            _, pid, entry = data.split(":", 2)
            if pid not in project_meta:
                bot.answer_callback_query(call.id, "❌ البوت غير موجود.", show_alert=True)
                return

            candidate = (project_dir(pid) / entry).resolve()
            try:
                candidate.relative_to(project_dir(pid).resolve())
            except ValueError:
                raise RuntimeError("مسار ملف التشغيل غير آمن.")

            if not candidate.is_file() or candidate.suffix.lower() != ".py":
                raise RuntimeError("ملف التشغيل غير صالح.")

            with get_project_lock(pid):
                project_meta[pid]["entry"] = str(candidate.relative_to(project_dir(pid)))
                project_meta[pid]["updated_at"] = now_iso()
                save_db()

            bot.answer_callback_query(call.id, "✅ تم تحديد ملف التشغيل.")
            bot.edit_message_text(
                project_info_text(pid),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=project_keyboard(pid)
            )
            return

        try:
            action, pid = data.split(":", 1)
        except ValueError:
            bot.answer_callback_query(call.id, "❌ طلب غير صالح.", show_alert=True)
            return

        if pid not in project_meta:
            bot.answer_callback_query(call.id, "❌ البوت غير موجود.", show_alert=True)
            return

        if action == "start":
            start_project(pid)
            bot.answer_callback_query(call.id, "🚀 تم التشغيل.")

        elif action == "stop":
            stop_project(pid)
            bot.answer_callback_query(call.id, "🛑 تم الإيقاف.")

        elif action == "restart":
            restart_project(pid)
            bot.answer_callback_query(call.id, "🔄 تمت إعادة التشغيل.")

        elif action == "toggle":
            current = project_meta.get(pid, {}).get("enabled", False)
            set_project_enabled(pid, not current)
            bot.answer_callback_query(
                call.id,
                "🔁 Auto Restart ON" if not current else "⏹ Auto Restart OFF"
            )

        elif action == "logs":
            text = truncate_log(read_logs(pid), 3500)
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                f"📜 <b>آخر Logs — {esc(pid)}</b>\n\n"
                f"<pre>{esc(text)}</pre>"
            )
            return

        elif action == "info":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                project_info_text(pid),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=project_keyboard(pid)
            )
            return

        elif action == "confirm_delete":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "⚠️ <b>تأكيد حذف المشروع</b>\n\n"
                f"🤖 <code>{esc(pid)}</code>\n"
                "سيتم حذف ملفات المشروع والـLogs والبيانات المرتبطة به.\n\n"
                "<b>العملية لا يمكن التراجع عنها.</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=delete_confirm_keyboard(pid)
            )
            return

        elif action == "delete":
            with get_project_lock(pid):
                stop_project(pid)
                shutil.rmtree(project_dir(pid), ignore_errors=True)
                log_file(pid).unlink(missing_ok=True)

                with lock:
                    project_meta.pop(pid, None)
                    processes.pop(pid, None)
                    started_at.pop(pid, None)
                    restart_generation.pop(pid, None)
                    project_locks.pop(pid, None)
                    save_db()

            bot.answer_callback_query(call.id, "🗑 تم الحذف.")
            bot.edit_message_text(
                bot_list_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=projects_keyboard(0)
            )
            return

        else:
            bot.answer_callback_query(call.id, "❌ عملية غير معروفة.", show_alert=True)
            return

        # Refresh the selected project after mutations.
        bot.edit_message_text(
            project_info_text(pid),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=project_keyboard(pid)
        )

    except Exception as exc:
        append_log(locals().get("pid", "controller"), f"CONTROL ERROR: {exc}")
        try:
            bot.answer_callback_query(call.id, "❌ حصل خطأ.", show_alert=True)
        except Exception:
            pass

        try:
            bot.edit_message_text(
                "❌ <b>فشل تنفيذ العملية</b>\n\n"
                f"<pre>{esc(str(exc)[-2800:])}</pre>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=main_keyboard()
            )
        except Exception:
            pass


# ============================================================
# UPLOAD
# ============================================================

@bot.message_handler(content_types=["document"])
def receive_document(message):
    if not owner_only(message):
        return

    if message.from_user.id not in upload_waiting:
        bot.reply_to(
            message,
            "اضغط «📤 رفع بوت» الأول."
        )
        return

    upload_waiting.discard(
        message.from_user.id
    )

    name = (
        message.document.file_name
        or "project"
    )

    lower = name.lower()

    if not (
        lower.endswith(".py")
        or lower.endswith(".zip")
    ):
        bot.reply_to(
            message,
            "❌ المسموح فقط .py أو .zip"
        )
        return

    size = (
        getattr(
            message.document,
            "file_size",
            0
        )
        or 0
    )

    if size > MAX_UPLOAD_MB * 1024 * 1024:
        bot.reply_to(
            message,
            f"❌ الملف أكبر من {MAX_UPLOAD_MB}MB."
        )
        return

    if len(project_meta) >= MAX_PROJECTS:
        bot.reply_to(
            message,
            f"❌ وصلت للحد الأقصى "
            f"({MAX_PROJECTS} مشاريع)."
        )
        return

    status_message = bot.reply_to(
        message,
        "📥 جاري تحميل وتجهيز المشروع..."
    )

    temp = DATA / (
        f".upload_{message.from_user.id}_"
        f"{int(time.time() * 1000)}"
    )

    temp.mkdir(
        parents=True,
        exist_ok=True
    )

    pid = None

    try:
        info = bot.get_file(
            message.document.file_id
        )

        raw = bot.download_file(
            info.file_path
        )

        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            raise RuntimeError(
                "حجم الملف أكبر من الحد المسموح."
            )

        uploaded = (
            temp
            / safe_name(name)
        )

        uploaded.write_bytes(raw)

        base_pid = project_id_from_name(
            name
        )

        pid = unique_project_id(
            base_pid
        )

        folder = project_dir(pid)

        folder.mkdir(
            parents=True,
            exist_ok=True
        )

        if lower.endswith(".zip"):
            safe_extract_zip(
                uploaded,
                folder
            )

            flatten_single_folder(
                folder
            )

        else:
            shutil.copy2(
                uploaded,
                folder / safe_name(name)
            )

        entry = find_entry(folder)

        if not entry:
            entries = list_python_entries(
                folder
            )

            if len(entries) > 1:
                raise RuntimeError(
                    "المشروع يحتوي على أكثر من ملف Python "
                    "ومفيش Entry واضح.\n"
                    "سمّي ملف التشغيل main.py أو bot.py."
                )

            raise RuntimeError(
                "مش قادر أحدد ملف التشغيل."
            )

        rel = str(
            entry.relative_to(folder)
        )

        project_meta[pid] = {
            "name": name,
            "entry": rel,
            "enabled": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }

        save_db()

        bot.edit_message_text(
            "🔧 <b>تم تجهيز المشروع</b>\n\n"
            f"📦 <code>{pid}</code>\n"
            f"📄 <code>{rel}</code>\n\n"
            "📚 جاري إنشاء البيئة وتثبيت المكتبات...",
            message.chat.id,
            status_message.message_id
        )

        start_project(pid)

        bot.edit_message_text(
            "╔══════════════════════════════╗\n"
            "║       ✅ <b>BOT ONLINE</b>       ║\n"
            "╚══════════════════════════════╝\n\n"
            f"📦 المشروع: <code>{pid}</code>\n"
            f"📄 التشغيل: <code>{rel}</code>\n"
            "🟢 الحالة: <b>شغال</b>\n"
            "🔁 Auto Restart: <b>مفعل</b>",
            message.chat.id,
            status_message.message_id,
            reply_markup=project_keyboard(pid)
        )

    except Exception as exc:
        if pid:
            project_meta.setdefault(
                pid,
                {}
            )["enabled"] = False

            restart_generation[pid] = (
                restart_generation.get(pid, 0)
                + 1
            )

            save_db()

            append_log(
                pid,
                f"UPLOAD/START ERROR: {exc}"
            )

        if pid and project_dir(pid).exists():
            # Keep the failed project and its logs
            # so the owner can inspect the problem.
            pass

        bot.edit_message_text(
            "❌ <b>فشل تجهيز أو تشغيل البوت</b>\n\n"
            f"<code>{telebot.util.escape(str(exc)[-3000:])}</code>",
            message.chat.id,
            status_message.message_id
        )

    finally:
        shutil.rmtree(
            temp,
            ignore_errors=True
        )


# ============================================================
# POLLING
# ============================================================

def polling_loop():
    """
    Keep the hosting controller alive if Telegram/network
    temporarily fails.
    """

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=60,
                skip_pending=True
            )

        except telebot.apihelper.ApiTelegramException as exc:
            print(
                f"[HOST] Telegram API error: {exc}. "
                "Retrying in 10s..."
            )

            time.sleep(10)

        except Exception as exc:
            print(
                f"[HOST] Polling error: {exc}. "
                "Retrying in 5s..."
            )

            time.sleep(5)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 64)
    print("🚀 TITAN BOT HOSTING v2.0")
    print(f"📁 Projects: {PROJECTS}")
    print(f"📁 Logs: {LOGS}")
    print(f"👤 Owner ID: {OWNER_ID}")
    print(f"🔁 Auto Restart: {AUTO_RESTART}")
    print(f"📦 Max Projects: {MAX_PROJECTS}")
    print("=" * 64)

    try:
        threading.Thread(
            target=restore_enabled_projects,
            daemon=True
        ).start()

        polling_loop()

    except KeyboardInterrupt:
        print("\n[HOST] Shutdown requested.")

    finally:
        shutdown_all()
        print("[HOST] All hosted processes stopped.")
