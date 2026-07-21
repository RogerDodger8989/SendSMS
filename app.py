import os
import sys
import re
import time
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
import requests
from datetime import datetime
import json
import threading
import logging
import traceback

# When running as a PyInstaller .exe, resources are in sys._MEIPASS
if getattr(sys, 'frozen', False):
    _BASE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Setup Debug Logging ---
debug_logger = logging.getLogger("BrotherPrint")
debug_logger.setLevel(logging.DEBUG)
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'print_debug.log')
try:
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)
    fh = logging.FileHandler(_log_path, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    debug_logger.addHandler(fh)
except:
    pass

_versions_logged = False

def _log_versions():
    global _versions_logged
    if _versions_logged:
        return
    _versions_logged = True
    try:
        import PIL
        pil_ver = PIL.__version__
    except Exception:
        pil_ver = "EJ INSTALLERAD"
    try:
        import brother_ql
        bql_ver = getattr(brother_ql, '__version__', None)
        if not bql_ver:
            import importlib.metadata
            bql_ver = importlib.metadata.version('brother-ql')
    except Exception:
        bql_ver = "EJ INSTALLERAD"
    debug_logger.info("=== SYSTEMINFORMATION ===")
    debug_logger.info(f"Python:      {sys.version}")
    debug_logger.info(f"PIL/Pillow:  {pil_ver}")
    debug_logger.info(f"brother_ql: {bql_ver}")
    debug_logger.info(f"OS:          {sys.platform}")
    debug_logger.info("=========================")

from dotenv import load_dotenv
import socket

# Cache: (model, label_size) → (width_px, height_px) expected by brother_ql
_label_dots_cache = {}

def _get_label_dots(model_name, label_size_str):
    """Ask brother_ql what pixel dimensions it expects for this model+label.
    Sends a 1×1 dummy image and parses the expected size from the ValueError.
    This avoids hardcoded values that differ between printer models."""
    cache_key = (model_name, label_size_str)
    if cache_key in _label_dots_cache:
        return _label_dots_cache[cache_key]

    result = None
    try:
        from PIL import Image
        from brother_ql.conversion import convert
        from brother_ql.raster import BrotherQLRaster
        qlr = BrotherQLRaster(model_name)
        dummy = Image.new('RGB', (1, 1), color='white')
        try:
            convert(qlr=qlr, images=[dummy], label=label_size_str,
                    rotate='auto', threshold=70, dither=False, compress=False,
                    red=False, dpi_600=False, hq=True, align='center')
        except ValueError as ve:
            m = re.search(r'Expecting: \((\d+), (\d+)\)', str(ve))
            if m:
                result = (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass

    if result is None:
        # Fallback hardcoded values
        result = {
            '17x54':  (566, 165),
            '29x90':  (991, 306),
            '39x90':  (991, 413),
            '62x29':  (696, 271),
            '62x100': (1109, 696),
        }.get(label_size_str, (566, 165))

    _label_dots_cache[cache_key] = result
    debug_logger.info(f"Label-dimensioner för {model_name}+{label_size_str}: {result}")
    return result

load_dotenv()

# --- SMS Proxy: master credentials & FOSSBilling config (set in .env) ---
_PROXY_ELKS_USERNAME = os.environ.get("ELKS_USERNAME", "")
_PROXY_ELKS_PASSWORD = os.environ.get("ELKS_PASSWORD", "")
_FOSSBILLING_URL = os.environ.get("FOSSBILLING_URL", "").rstrip("/")


def _validate_fossbilling_license(license_key):
    """Returns (valid: bool, reason: str).
    Calls FOSSBilling's built-in guest API (no auth required):
      GET {FOSSBILLING_URL}/api/guest/service/license?license={key}
    FOSSBilling wraps the response in {"result": {...}, "error": null}.
    A license is considered valid when result is non-null and status == "active"."""
    if not _FOSSBILLING_URL:
        return False, "FOSSBILLING_URL inte konfigurerad på servern"
    if not license_key:
        return False, "Licensnyckel saknas"

    url = f"{_FOSSBILLING_URL}/api/guest/service/license"
    try:
        resp = requests.get(url, params={"license": license_key},
                            headers={"Accept": "application/json"}, timeout=10)
    except requests.Timeout:
        return False, "Timeout vid kontakt med licensserver"
    except Exception as e:
        return False, f"Kunde inte nå licensservern: {e}"

    try:
        data = resp.json()
    except Exception:
        return False, f"Ogiltigt svar från licensservern (HTTP {resp.status_code})"

    # FOSSBilling guest API: {"result": {...} | null, "error": {"message": ...} | null}
    result = data.get("result")
    error  = data.get("error")

    if result and isinstance(result, dict):
        status = result.get("status", "")
        if status == "active":
            return True, "OK"
        return False, f"Licensen är inte aktiv (status: {status or 'okänd'})"

    if error and isinstance(error, dict):
        return False, error.get("message", "Ogiltig licens")

    # FOSSBilling may return HTTP 4xx directly for unknown keys
    if resp.status_code in (401, 403, 404):
        return False, "Ogiltig eller utgången licens"

    return False, f"Licensserver svarade med HTTP {resp.status_code}"


# DB path: defaults to ./data (works locally and in Docker with WORKDIR /app)
DB_DIR = os.environ.get("DB_DIR", "./data")
DB_PATH = os.path.join(DB_DIR, "sms_logg.db")
SETTINGS_PATH = os.path.join(DB_DIR, "settings.db")

_PASSWORD_MASK = "__masked__"

# Brute force protection: max 5 failed PIN attempts, 15 min lockout
_login_attempts = defaultdict(list)
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 900


def get_db_connection():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_settings_connection():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(SETTINGS_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_or_create_secret_key():
    key_file = os.path.join(DB_DIR, '.secret_key')
    try:
        with open(key_file, 'r') as f:
            key = f.read().strip()
            if key:
                return key
    except (FileNotFoundError, IOError):
        pass
    key = secrets.token_hex(32)
    os.makedirs(DB_DIR, exist_ok=True)
    try:
        with open(key_file, 'w') as f:
            f.write(key)
    except IOError:
        pass
    return key


app = Flask(
    __name__,
    template_folder=os.path.join(_BASE_DIR, 'templates'),
    static_folder=os.path.join(_BASE_DIR, 'static'),
)
app.secret_key = os.environ.get("SECRET_KEY") or get_or_create_secret_key()


def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sms_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            article TEXT,
            amount TEXT,
            message TEXT NOT NULL,
            api_id TEXT,
            status TEXT NOT NULL,
            timestamp DATETIME
        )
    ''')
    try:
        conn.execute("ALTER TABLE sms_log ADD COLUMN picked_up INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE sms_log ADD COLUMN label_info TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute('''
        CREATE TABLE IF NOT EXISTS license_credits (
            license_key TEXT PRIMARY KEY,
            credits     INTEGER NOT NULL DEFAULT 0,
            created_at  DATETIME NOT NULL,
            updated_at  DATETIME NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT    NOT NULL,
            change      INTEGER NOT NULL,
            reason      TEXT,
            note        TEXT,
            timestamp   DATETIME NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def init_settings_db():
    conn = get_settings_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            text TEXT NOT NULL
        )
    ''')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('elks_username', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('elks_password', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('elks_sender', 'Butiken')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_mode', 'false')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('app_pin', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('purge_days', '90')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('brother_ip', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('brother_model', 'QL-810W')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('brother_label_size', '17x54')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('brother_enabled', 'false')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proxy_url', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('license_key', '')")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parts_discount REAL DEFAULT 0,
            other_discount REAL DEFAULT 0
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            default_margin REAL DEFAULT 0,
            default_profit REAL DEFAULT 0
        )
    ''')

    cursor.execute("SELECT count(*) FROM templates")
    if cursor.fetchone()[0] == 0:
        default_templates = [
            ("Klar för hämtning", "Hej! Din vara är nu klar för hämtning. Välkommen!"),
            ("Försenad", "Hej! Tyvärr är varan lite försenad. Vi hör av oss så fort den är klar. Mvh Butiken"),
            ("Påminnelse", "Hej! Vi vill påminna om att din vara finns redo att hämtas ut. Välkommen!")
        ]
        cursor.executemany("INSERT INTO templates (name, text) VALUES (?, ?)", default_templates)
    conn.commit()
    conn.close()


def purge_old_records():
    purge_days_val = get_setting("purge_days", "90")
    purge_days = int(purge_days_val) if purge_days_val.isdigit() else 90
    cutoff_date = datetime.now() - timedelta(days=purge_days)
    cutoff_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    conn.execute('DELETE FROM sms_log WHERE timestamp < ?', (cutoff_str,))
    conn.commit()
    conn.close()


init_db()
init_settings_db()

def format_phone_for_print(phone):
    if not phone: return ""
    p = phone.replace(" ", "").replace("-", "")
    if p.startswith("+46"):
        p = "0" + p[3:]
    elif p.startswith("46") and len(p) > 8:
        p = "0" + p[2:]
        
    if p.startswith("07") and len(p) == 10:
        return f"{p[:3]}-{p[3:6]} {p[6:8]} {p[8:]}"
    elif p.startswith("08") and len(p) == 9:
        return f"{p[:2]}-{p[2:5]} {p[5:7]} {p[7:]}"
    elif p.startswith("0") and len(p) >= 9:
        return f"{p[:3]}-{p[3:6]} {p[6:]}"
    elif p.startswith("0"):
        # Just put a hyphen after area code (approx 3 or 4 chars) if we don't know
        return f"{p[:3]}-{p[3:]}"
    return p

def print_brother_label(ip_address, model, label_size, phone_number, name, timestamp, label_info=""):
    try:
        _log_versions()
        phone_number = format_phone_for_print(phone_number)
        debug_logger.info("=" * 50)
        debug_logger.info("STARTAR UTSKRIFT")
        debug_logger.info(f"  Skrivare/IP:   '{ip_address}'")
        debug_logger.info(f"  Modell:        '{model}'")
        debug_logger.info(f"  Etikett:       '{label_size}'")
        debug_logger.info(f"  Telefon:       '{phone_number}'")
        debug_logger.info(f"  Namn:          '{name}'")
        debug_logger.info(f"  Timestamp:     '{timestamp}'")
        debug_logger.info(f"  Extra info:    '{label_info}'")

        if not ip_address or not model or not label_size:
            debug_logger.error("FEL: Saknar skrivare, modell eller etikettstorlek!")
            return False, "IP, modell eller etikett-storlek saknas"

        date_str = timestamp.split(' ')[0] if timestamp else ""
        time_str = timestamp.split(' ')[1] if timestamp and ' ' in timestamp else ""

        # Load font mappings
        try:
            with open('data/fonts.json', 'r', encoding='utf-8') as f:
                font_settings = json.load(f)
            debug_logger.debug("fonts.json laddad OK")
        except Exception as e:
            debug_logger.warning(f"Kunde inte ladda fonts.json ({e}) — använder Arial")
            font_settings = {"family": "Arial", "sizes": {"title": 42, "body": 24, "footer": 18}}

        font_family = font_settings.get("family", "Arial")
        font_sizes = font_settings.get("sizes", {"title": 42, "body": 24, "footer": 18})

        try:
            from PIL import Image, ImageDraw, ImageFont
            debug_logger.debug("PIL importerad OK")
        except ImportError as e:
            debug_logger.error(f"PIL (Pillow) saknas: {e}")
            return False, f"Bibliotek saknas: {e}"

        canvas_size = _get_label_dots(model, label_size)
        debug_logger.info(f"  Canvas (WxH):  {canvas_size[0]}x{canvas_size[1]} px (etikett '{label_size}', modell '{model}')")

        img = Image.new('RGB', canvas_size, color='white')
        d = ImageDraw.Draw(img)

        font_path = f"c:/Windows/Fonts/{font_family.lower()}.ttf"
        try:
            large_size = int(canvas_size[1] * 0.30)
            med_size = int(canvas_size[1] * 0.22)
            font_large = ImageFont.truetype(font_path, large_size)
            font_medium = ImageFont.truetype(font_path, med_size)
            debug_logger.debug(f"Font laddad: '{font_path}' (stor={large_size}pt, medium={med_size}pt)")
        except IOError:
            debug_logger.warning(f"Font '{font_path}' saknas — använder standardfont")
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()

        if name:
            y_offset = int(canvas_size[1] * 0.05)
            y_step = int(canvas_size[1] * 0.32)
            d.text((20, y_offset), name, fill='black', font=font_large)
            d.text((20, y_offset + y_step), phone_number, fill='black', font=font_medium)
            d.text((int(canvas_size[0]*0.65), y_offset + y_step), f"{date_str} {time_str}", fill='black', font=font_medium)
            if label_info:
                d.text((20, y_offset + 2 * y_step), label_info, fill='black', font=font_medium)
        else:
            y_offset = int(canvas_size[1] * 0.15)
            y_step = int(canvas_size[1] * 0.35)
            d.text((20, y_offset), phone_number, fill='black', font=font_large)
            d.text((int(canvas_size[0]*0.65), y_offset), f"{date_str} {time_str}", fill='black', font=font_medium)
            if label_info:
                d.text((20, y_offset + y_step), label_info, fill='black', font=font_large)

        # Rotate 90° only for portrait labels (height > width in mm).
        # Landscape labels like 62x29 stay as-is — brother_ql expects landscape.
        try:
            lw_mm, lh_mm = int(label_size.split('x')[0]), int(label_size.split('x')[1])
            should_rotate = lh_mm > lw_mm
        except Exception:
            should_rotate = True
        debug_logger.info(f"  Rotation:      {'JA (porträtt-etikett)' if should_rotate else 'NEJ (landskap-etikett)'}")
        if should_rotate:
            img = img.transpose(Image.ROTATE_90)
        debug_logger.info(f"  Bildstorlek efter rotation: {img.size[0]}x{img.size[1]} px")

        try:
            from brother_ql.conversion import convert
            from brother_ql.raster import BrotherQLRaster
            debug_logger.debug("brother_ql importerad OK")
        except ImportError as e:
            debug_logger.error(f"brother_ql saknas: {e}")
            return False, f"brother_ql saknas: {e}"

        debug_logger.info(f"  Konverterar till raster (modell={model}, label={label_size}, rotate='auto')...")
        qlr = BrotherQLRaster(model)
        instructions = convert(
            qlr=qlr,
            images=[img],
            label=label_size,
            rotate='auto',
            threshold=70,
            dither=False,
            compress=False,
            red=False,
            dpi_600=False,
            hq=True,
            align='center'
        )
        debug_logger.info(f"  Raster klart: {len(instructions)} bytes")

        printer_name = ip_address.strip()
        is_ip = ("." in printer_name and not printer_name.startswith("\\\\") and "brother" not in printer_name.lower()) or printer_name.startswith("tcp://")
        debug_logger.info(f"  Backend:       {'NÄTVERK (TCP/IP)' if is_ip else 'WINDOWS (win32print)'}")

        if is_ip:
            debug_logger.info(f"  Skickar till nätverksskrivare: {printer_name}")
            from brother_ql.backends.helpers import send
            if not printer_name.startswith('tcp://'):
                printer_name = f'tcp://{printer_name}'
            send(instructions=instructions, printer_identifier=printer_name, backend_identifier='network', blocking=True)
            debug_logger.info("  Skickat via nätverk — KLART!")
        else:
            debug_logger.info(f"  Skickar via win32print till: '{printer_name}'")
            try:
                import win32print
            except ImportError as e:
                debug_logger.error(f"win32print saknas: {e}")
                return False, "win32print saknas — ange skrivarens IP-adress istället"

            # Logga alla tillgängliga Windows-skrivare för felsökning
            try:
                all_printers = [p[2] for p in win32print.EnumPrinters(
                    win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
                debug_logger.info(f"  Tillgängliga skrivare i Windows: {all_printers}")
                if printer_name not in all_printers:
                    debug_logger.warning(f"  VARNING: '{printer_name}' finns EJ i skrivarlistan ovan!")
            except Exception as ep:
                debug_logger.warning(f"  Kunde inte lista skrivare: {ep}")

            try:
                hprinter = win32print.OpenPrinter(printer_name)
                debug_logger.debug(f"  Skrivar-handle: {hprinter}")
            except Exception as e:
                debug_logger.error(f"  KAN INTE ÖPPNA skrivaren '{printer_name}': {e}\n{traceback.format_exc()}")
                return False, f"Hittade inte skrivaren '{printer_name}' i Windows"

            try:
                debug_logger.debug("  Startar utskriftsjobb (RAW)...")
                win32print.StartDocPrinter(hprinter, 1, ("Etikett - SendSMS", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hprinter)
                    written = win32print.WritePrinter(hprinter, instructions)
                    debug_logger.info(f"  WritePrinter: {written} bytes skrivna till kön")
                    win32print.EndPagePrinter(hprinter)
                finally:
                    win32print.EndDocPrinter(hprinter)
                    debug_logger.info("  Utskriftsjobb inlagt i Windows-kön — KLART!")
            finally:
                win32print.ClosePrinter(hprinter)
                debug_logger.debug("Skrivar-handle stängd.")
        
        debug_logger.info("UTSKRIFT KLAR")
        debug_logger.info("=" * 50)
        return True, "Utskrift skickad"
    except Exception as e:
        debug_logger.error(f"KRITISKT FEL i print_brother_label:\n{traceback.format_exc()}")
        debug_logger.info("=" * 50)
        return False, str(e)


def sanitize_phone_number(number):
    # Remove spaces, dashes, parentheses
    clean_num = re.sub(r'[\s\-\(\)]', '', number)
    # Already E.164
    if clean_num.startswith('+'):
        return clean_num
    # 00-prefixed international (e.g. 0046736562525)
    if clean_num.startswith('00') and len(clean_num) > 4:
        return '+' + clean_num[2:]
    # Swedish numbers: 07x mobiles, 08x Stockholm, 0x landlines — replace leading 0 with +46
    if clean_num.startswith('0') and len(clean_num) >= 7:
        return '+46' + clean_num[1:]
    return clean_num


def get_setting(key, default=""):
    conn = get_settings_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def deduct_credit(license_key):
    """Atomically deducts 1 credit from the license. Returns (success, remaining).
    BEGIN IMMEDIATE holds a write-lock for the duration, preventing double-spend."""
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT credits FROM license_credits WHERE license_key = ?", (license_key,)
        ).fetchone()
        if not row or row["credits"] <= 0:
            conn.rollback()
            return False, (row["credits"] if row else 0)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "UPDATE license_credits SET credits = credits - 1, updated_at = ? "
            "WHERE license_key = ? AND credits > 0",
            (now, license_key)
        )
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            conn.rollback()
            return False, 0
        remaining = conn.execute(
            "SELECT credits FROM license_credits WHERE license_key = ?", (license_key,)
        ).fetchone()["credits"]
        conn.execute(
            "INSERT INTO credit_transactions (license_key, change, reason, note, timestamp) "
            "VALUES (?, -1, 'sms_sent', NULL, ?)",
            (license_key, now)
        )
        conn.commit()
        return True, remaining
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_credits(license_key, amount, reason, note=""):
    """Adds credits to a license key, creating the row if needed. Returns credits_after."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    conn.execute(
        "INSERT OR IGNORE INTO license_credits (license_key, credits, created_at, updated_at) "
        "VALUES (?, 0, ?, ?)",
        (license_key, now, now)
    )
    conn.execute(
        "UPDATE license_credits SET credits = credits + ?, updated_at = ? WHERE license_key = ?",
        (amount, now, license_key)
    )
    credits_after = conn.execute(
        "SELECT credits FROM license_credits WHERE license_key = ?", (license_key,)
    ).fetchone()["credits"]
    conn.execute(
        "INSERT INTO credit_transactions (license_key, change, reason, note, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (license_key, amount, reason, note or None, now)
    )
    conn.commit()
    conn.close()
    return credits_after


@app.before_request
def require_login():
    # These endpoints carry their own authentication (license key, admin key, webhook secret)
    _public = {'index', 'auth_status', 'login', 'static',
               'proxy_send_sms', 'proxy_credits_balance',
               'admin_credits_add', 'admin_credits_balance',
               'webhook_fossbilling'}
    if request.endpoint in _public:
        return
    app_pin = get_setting("app_pin", "")
    if app_pin and not session.get("logged_in"):
        return jsonify({"error": "Kräver inloggning. Ladda om sidan."}), 401


@app.route('/', methods=['GET'])
def index():
    purge_old_records()
    return render_template('index.html')


@app.route('/api/server-info', methods=['GET'])
def server_info():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"
    return jsonify({"ip": local_ip, "port": 5000})


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    app_pin = get_setting("app_pin", "")
    if not app_pin:
        return jsonify({"locked": True, "setup_needed": True})
    if session.get("logged_in"):
        return jsonify({"locked": False, "setup_needed": False})
    return jsonify({"locked": True, "setup_needed": False})


@app.route('/api/auth/login', methods=['POST'])
def login():
    ip = request.remote_addr
    now = time.time()
    # Clean expired attempts
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOCKOUT_SECONDS]

    if len(_login_attempts[ip]) >= _MAX_ATTEMPTS:
        remaining = int(_LOCKOUT_SECONDS - (now - _login_attempts[ip][0]))
        mins = max(1, remaining // 60)
        return jsonify({"success": False, "error": f"För många försök. Vänta {mins} minut(er)."}), 429

    data = request.json
    pin = data.get('pin', '').strip()

    app_pin = get_setting("app_pin", "")
    master_pin = os.environ.get("MASTER_PIN", "")

    if not app_pin:
        if not pin:
            return jsonify({"success": False, "error": "PIN får inte vara tom"}), 400
        conn = get_settings_connection()
        conn.execute("UPDATE settings SET value = ? WHERE key = 'app_pin'", (pin,))
        conn.commit()
        conn.close()
        session["logged_in"] = True
        return jsonify({"success": True})

    if pin == app_pin or (master_pin and pin == master_pin):
        _login_attempts[ip] = []
        session["logged_in"] = True
        return jsonify({"success": True})

    _login_attempts[ip].append(now)
    remaining_attempts = _MAX_ATTEMPTS - len(_login_attempts[ip])
    return jsonify({"success": False, "error": f"Fel PIN-kod. {remaining_attempts} försök kvar."}), 401


@app.route('/api/auth/change-pin', methods=['POST'])
def change_pin():
    data = request.json
    old_pin = data.get('old_pin', '')
    new_pin = data.get('new_pin', '')

    app_pin = get_setting("app_pin", "")
    master_pin = os.environ.get("MASTER_PIN", "")

    if app_pin and old_pin != app_pin and old_pin != master_pin:
        return jsonify({"success": False, "error": "Gammal PIN stämmer inte"}), 401

    conn = get_settings_connection()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'app_pin'", (new_pin,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/history', methods=['GET'])
def get_history():
    search_query = request.args.get('q', '').strip()
    conn = get_db_connection()
    
    query = '''
        SELECT id, phone_number, article, message, status, timestamp, picked_up, label_info
        FROM sms_log
    '''
    params = []
    
    if search_query:
        query += " WHERE phone_number LIKE ? OR article LIKE ? OR message LIKE ?"
        params = [f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"]
        
    query += " ORDER BY id DESC LIMIT 50"
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    history = []
    for r in rows:
        history.append({
            "id": r['id'],
            "phone_number": r['phone_number'],
            "article": r['article'],
            "message": r['message'],
            "status": r['status'],
            "timestamp": r['timestamp'],
            "picked_up": r['picked_up'],
            "label_info": r['label_info'] if 'label_info' in r.keys() else ""
        })
    return jsonify(history)


@app.route('/api/history/<int:id>/pickup', methods=['POST'])
def toggle_pickup(id):
    conn = get_db_connection()
    row = conn.execute("SELECT picked_up FROM sms_log WHERE id = ?", (id,)).fetchone()
    if row:
        new_val = 1 if row['picked_up'] == 0 else 0
        conn.execute("UPDATE sms_log SET picked_up = ? WHERE id = ?", (new_val, id))
        conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/templates', methods=['GET', 'POST'])
def handle_templates():
    conn = get_settings_connection()
    if request.method == 'GET':
        templates = conn.execute("SELECT * FROM templates ORDER BY id").fetchall()
        conn.close()
        return jsonify([dict(row) for row in templates])

    elif request.method == 'POST':
        data = request.json
        name = data.get('name')
        text = data.get('text')
        template_id = data.get('id')

        if not name or not text:
            return jsonify({"success": False, "error": "Namn och text krävs."}), 400

        if template_id:
            conn.execute("UPDATE templates SET name = ?, text = ? WHERE id = ?", (name, text, template_id))
        else:
            conn.execute("INSERT INTO templates (name, text) VALUES (?, ?)", (name, text))

        conn.commit()
        conn.close()
        return jsonify({"success": True})


@app.route('/api/templates/<int:id>', methods=['DELETE'])
def delete_template(id):
    conn = get_settings_connection()
    conn.execute("DELETE FROM templates WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/settings', methods=['GET', 'POST'])
def handle_settings():
    if request.method == 'GET':
        pwd = get_setting("elks_password")
        settings = {
            "elks_username": get_setting("elks_username"),
            "elks_password": _PASSWORD_MASK if pwd else "",
            "elks_sender": get_setting("elks_sender", "Butiken"),
            "test_mode": get_setting("test_mode", "false"),
            "purge_days": get_setting("purge_days", "90"),
            "brother_ip": get_setting("brother_ip", ""),
            "brother_model": get_setting("brother_model", "QL-810W"),
            "brother_label_size": get_setting("brother_label_size", "17x54"),
            "brother_enabled": get_setting("brother_enabled", "false"),
            "proxy_url": get_setting("proxy_url", ""),
            "license_key": get_setting("license_key", ""),
        }
        return jsonify(settings)

    elif request.method == 'POST':
        data = request.json
        sender = data.get('elks_sender', '')
        if len(sender) > 11:
            return jsonify({"success": False, "error": "Avsändaren får max vara 11 tecken"}), 400
        conn = get_settings_connection()
        saveable = ['elks_username', 'elks_password', 'elks_sender', 'test_mode', 'purge_days',
                    'brother_ip', 'brother_model', 'brother_label_size', 'brother_enabled',
                    'proxy_url', 'license_key']
        for key in saveable:
            if key not in data:
                continue
            if key == 'elks_password' and data[key] == _PASSWORD_MASK:
                continue  # Password unchanged — don't overwrite
            value = str(data[key]).lower() if key in ['test_mode', 'brother_enabled'] else data[key]
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        conn.commit()
        conn.close()
        return jsonify({"success": True})


@app.route('/api/suppliers', methods=['GET', 'POST'])
def handle_suppliers():
    conn = get_settings_connection()
    if request.method == 'GET':
        rows = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        conn.close()
        return jsonify({"success": False, "error": "Namn krävs"}), 400
    if data.get('id'):
        conn.execute("UPDATE suppliers SET name=?, parts_discount=?, other_discount=? WHERE id=?",
                     (name, data.get('parts_discount', 0), data.get('other_discount', 0), data['id']))
    else:
        conn.execute("INSERT INTO suppliers (name, parts_discount, other_discount) VALUES (?, ?, ?)",
                     (name, data.get('parts_discount', 0), data.get('other_discount', 0)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/suppliers/<int:id>', methods=['DELETE'])
def delete_supplier(id):
    conn = get_settings_connection()
    conn.execute("DELETE FROM suppliers WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/customers', methods=['GET', 'POST'])
def handle_customers():
    conn = get_settings_connection()
    if request.method == 'GET':
        rows = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        conn.close()
        return jsonify({"success": False, "error": "Namn krävs"}), 400
    if data.get('id'):
        conn.execute("UPDATE customers SET name=?, default_margin=?, default_profit=? WHERE id=?",
                     (name, data.get('default_margin', 0), data.get('default_profit', 0), data['id']))
    else:
        conn.execute("INSERT INTO customers (name, default_margin, default_profit) VALUES (?, ?, ?)",
                     (name, data.get('default_margin', 0), data.get('default_profit', 0)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/customers/<int:id>', methods=['DELETE'])
def delete_customer(id):
    conn = get_settings_connection()
    conn.execute("DELETE FROM customers WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/balance', methods=['GET'])
def get_balance():
    elks_username = get_setting("elks_username")
    elks_password = get_setting("elks_password")

    if not elks_username or not elks_password:
        return jsonify({"success": False, "error": "Saknar inloggning"})

    try:
        resp = requests.get("https://api.46elks.com/a1/Me", auth=(elks_username, elks_password), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            balance_raw = data.get('balance', 0)
            currency = data.get('currency', 'SEK')
            balance = balance_raw / 10000.0
            return jsonify({"success": True, "balance": balance, "currency": currency})
        return jsonify({"success": False, "error": "Kunde inte hämta saldo"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/send-sms', methods=['POST'])
def send_sms():
    data = request.json

    phone_number = data.get('phone_number', '')
    article = data.get('article', '')
    label_info = data.get('label_info', '')
    amount = data.get('amount', '')
    message = data.get('message', '')
    print_label = data.get('print_label', True)

    if not phone_number or not message:
        return jsonify({"success": False, "error": "Telefonnummer och meddelande är obligatoriskt."}), 400

    sanitized_number = sanitize_phone_number(phone_number)

    elks_sender   = get_setting("elks_sender", "Butiken")
    test_mode     = get_setting("test_mode", "false") == "true"
    proxy_url     = get_setting("proxy_url", "").rstrip("/")
    license_key   = get_setting("license_key", "")

    status = "Failed"
    api_id = ""
    error_msg = None

    if test_mode:
        status = "Övningsläge (Ej skickat)"
    elif proxy_url and license_key:
        # --- Proxy mode: forward through the centralized SMS proxy ---
        try:
            response = requests.post(
                f"{proxy_url}/api/v1/send-sms",
                json={"license_key": license_key, "to": sanitized_number,
                      "message": message, "sender": elks_sender},
                timeout=15,
            )
            if response.status_code == 200:
                resp_data = response.json()
                status = resp_data.get('status', 'created')
                api_id = resp_data.get('id', '')
            else:
                try:
                    err_body = response.json()
                    error_msg = err_body.get('error', f"HTTP {response.status_code}")
                except Exception:
                    error_msg = f"Proxy HTTP {response.status_code}: {response.text[:200]}"
                status = f"Failed: {error_msg}"
        except Exception as e:
            error_msg = f"Kunde inte nå proxyn: {e}"
            status = f"Failed: {error_msg}"
    else:
        # --- Direct mode: call 46elks with locally stored credentials ---
        elks_username = get_setting("elks_username")
        elks_password = get_setting("elks_password")
        if elks_username and elks_password:
            try:
                response = requests.post(
                    "https://api.46elks.com/a1/sms",
                    data={"from": elks_sender, "to": sanitized_number, "message": message},
                    auth=(elks_username, elks_password),
                    timeout=10,
                )
                if response.status_code == 200:
                    resp_data = response.json()
                    status = resp_data.get('status', 'Sent')
                    api_id = resp_data.get('id', '')
                else:
                    error_msg = f"API Fel {response.status_code}: {response.text}"
                    status = f"Failed: {error_msg}"
            except Exception as e:
                error_msg = f"Request Exception: {str(e)}"
                status = f"Failed: {error_msg}"
        else:
            error_msg = "Varken proxy-URL/licensnyckel eller 46elks-uppgifter är konfigurerade."
            status = f"Failed: {error_msg}"

    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO sms_log (phone_number, article, label_info, message, status, api_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sanitized_number, article, label_info, message, status, api_id, current_time)
    )
    conn.commit()
    conn.close()

    if not error_msg and print_label:
        brother_enabled = get_setting("brother_enabled", "false") == "true"
        brother_ip = get_setting("brother_ip", "")
        brother_model = get_setting("brother_model", "")
        brother_label = get_setting("brother_label_size", "17x54")
        if brother_enabled and brother_ip and brother_model:
            threading.Thread(target=print_brother_label, args=(brother_ip, brother_model, brother_label, sanitized_number, article, current_time, label_info)).start()

    if error_msg:
        return jsonify({"success": False, "error": error_msg}), 500

    return jsonify({"success": True, "status": status})

@app.route('/api/test-brother', methods=['POST'])
def test_brother():
    data = request.json
    ip = data.get('ip', '')
    model = data.get('model', '')
    label_size = data.get('label_size', '17x54')
    if not ip or not model:
        return jsonify({"success": False, "error": "Skrivarnamn eller modell saknas"}), 400
    
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    success, msg = print_brother_label(ip, model, label_size, "070 123 45 67", "Test Testsson", current_time, "Hylla A1")
    
    if success:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": msg}), 500

@app.route('/api/reprint', methods=['POST'])
def reprint():
    data = request.json
    phone = data.get('phone_number')
    article = data.get('article', '')
    label_info = data.get('label_info', '')
    timestamp = data.get('timestamp')
    
    brother_ip = get_setting("brother_ip", "")
    brother_model = get_setting("brother_model", "")
    brother_label = get_setting("brother_label_size", "17x54")
    
    if not brother_ip or not brother_model:
        return jsonify({"success": False, "error": "Skrivaren är inte konfigurerad i inställningarna"}), 400
        
    success, msg = print_brother_label(brother_ip, brother_model, brother_label, phone, article, timestamp, label_info)
    
    if success:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": msg}), 500

@app.route('/api/print-custom-label', methods=['POST'])
def print_custom_label():
    import base64, io as _io
    data = request.json
    image_b64 = data.get('image', '')
    label_size = data.get('label_size', '')

    if not image_b64:
        return jsonify({"success": False, "error": "Ingen bild"}), 400

    brother_ip    = get_setting("brother_ip", "")
    brother_model = get_setting("brother_model", "")
    if not label_size:
        label_size = get_setting("brother_label_size", "17x54")

    if not brother_ip or not brother_model:
        return jsonify({"success": False, "error": "Skrivaren är inte konfigurerad i inställningarna"}), 400

    try:
        from PIL import Image
        target = _get_label_dots(brother_model, label_size)

        img_data = base64.b64decode(image_b64)
        img = Image.open(_io.BytesIO(img_data)).convert('RGB')
        img = img.resize(target, Image.LANCZOS)
        try:
            lw, lh = int(label_size.split('x')[0]), int(label_size.split('x')[1])
            if lh > lw:
                img = img.transpose(Image.ROTATE_90)
        except Exception:
            img = img.transpose(Image.ROTATE_90)

        from brother_ql.conversion import convert
        from brother_ql.raster import BrotherQLRaster
        qlr = BrotherQLRaster(brother_model)
        instructions = convert(
            qlr=qlr, images=[img], label=label_size,
            rotate='0', threshold=70, dither=False,
            compress=False, red=False, dpi_600=False, hq=True, align='center'
        )

        printer_name = brother_ip.strip()
        is_ip = ("." in printer_name and not printer_name.startswith("\\\\") and "brother" not in printer_name.lower()) or printer_name.startswith("tcp://")

        if is_ip:
            from brother_ql.backends.helpers import send
            if not printer_name.startswith('tcp://'):
                printer_name = f'tcp://{printer_name}'
            send(instructions=instructions, printer_identifier=printer_name, backend_identifier='network', blocking=True)
        else:
            import win32print
            hprinter = win32print.OpenPrinter(printer_name)
            try:
                win32print.StartDocPrinter(hprinter, 1, ("Etikett - SendSMS", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hprinter)
                    win32print.WritePrinter(hprinter, instructions)
                    win32print.EndPagePrinter(hprinter)
                finally:
                    win32print.EndDocPrinter(hprinter)
            finally:
                win32print.ClosePrinter(hprinter)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/v1/send-sms', methods=['POST'])
def proxy_send_sms():
    """Centralized SMS proxy endpoint.

    Expects JSON:
        license_key  (str, required) — active FOSSBilling license key
        to           (str, required) — recipient phone number (E.164 or Swedish local format)
        message      (str, required) — SMS text
        sender       (str, optional) — sender name/number (max 11 chars); falls back to "Butiken"

    Validates the license against FOSSBilling's public guest API, then forwards to 46elks
    using the master credentials (ELKS_USERNAME / ELKS_PASSWORD env vars).
    Returns the 46elks API response or an appropriate HTTP error.
    """
    data = request.get_json(silent=True) or {}

    license_key = (data.get("license_key") or "").strip()
    to_number   = (data.get("to") or "").strip()
    message     = (data.get("message") or "").strip()
    sender      = (data.get("sender") or "Butiken").strip()[:11]

    if not to_number or not message:
        return jsonify({"error": "Fälten 'to' och 'message' är obligatoriska"}), 400

    # --- 1. License validation ---
    valid, reason = _validate_fossbilling_license(license_key)
    if not valid:
        return jsonify({"error": f"Obehörig: {reason}"}), 401

    # --- 2. Credit check (atomic deduction — released only if 46elks succeeds) ---
    credit_ok, remaining = deduct_credit(license_key)
    if not credit_ok:
        return jsonify({"error": "Inga krediter kvar", "credits_remaining": 0}), 402

    # --- 3. Master credentials check ---
    if not _PROXY_ELKS_USERNAME or not _PROXY_ELKS_PASSWORD:
        # Roll back the deducted credit — server misconfiguration, not client fault
        add_credits(license_key, 1, "rollback", "server misconfiguration")
        return jsonify({"error": "SMS-tjänsten är inte konfigurerad (saknar 46elks-uppgifter)"}), 503

    sanitized = sanitize_phone_number(to_number)
    try:
        resp = requests.post(
            "https://api.46elks.com/a1/sms",
            data={"from": sender, "to": sanitized, "message": message},
            auth=(_PROXY_ELKS_USERNAME, _PROXY_ELKS_PASSWORD),
            timeout=15,
        )
    except requests.Timeout:
        add_credits(license_key, 1, "rollback", "46elks timeout")
        return jsonify({"error": "Timeout vid anrop till 46elks"}), 504
    except Exception as e:
        add_credits(license_key, 1, "rollback", f"46elks exception: {e}")
        return jsonify({"error": f"SMS-sändning misslyckades: {e}"}), 502

    try:
        elks_data = resp.json()
    except Exception:
        elks_data = {"raw": resp.text}

    if resp.status_code == 200:
        # --- 4. Log successful send ---
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            conn = get_db_connection()
            conn.execute(
                "INSERT INTO sms_log (phone_number, article, label_info, message, status, api_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sanitized, "proxy", license_key[:12] + "...", message, elks_data.get("status", "created"), elks_data.get("id", ""), current_time)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({**elks_data, "credits_remaining": remaining}), 200

    # 46elks returned an error — refund the credit
    add_credits(license_key, 1, "rollback", f"46elks HTTP {resp.status_code}")
    return jsonify({"error": f"46elks returnerade HTTP {resp.status_code}", "details": elks_data}), resp.status_code


@app.route('/api/admin/credits/add', methods=['POST'])
def admin_credits_add():
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key or request.headers.get("X-Admin-Key") != admin_key:
        return jsonify({"error": "Ogiltig admin-nyckel"}), 401
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip()
    try:
        amount = int(data.get("credits", 0))
    except (TypeError, ValueError):
        amount = 0
    note = (data.get("note") or "").strip()
    if not license_key or amount <= 0:
        return jsonify({"error": "license_key och credits (>0) krävs"}), 400
    credits_after = add_credits(license_key, amount, "admin_add", note)
    return jsonify({"success": True, "license_key": license_key, "credits_after": credits_after})


@app.route('/api/admin/credits/balance', methods=['GET'])
def admin_credits_balance():
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key or request.headers.get("X-Admin-Key") != admin_key:
        return jsonify({"error": "Ogiltig admin-nyckel"}), 401
    license_key = (request.args.get("license_key") or "").strip()
    if not license_key:
        return jsonify({"error": "license_key krävs som query-parameter"}), 400
    conn = get_db_connection()
    row = conn.execute(
        "SELECT credits FROM license_credits WHERE license_key = ?", (license_key,)
    ).fetchone()
    txs = conn.execute(
        "SELECT change, reason, note, timestamp FROM credit_transactions "
        "WHERE license_key = ? ORDER BY id DESC LIMIT 20",
        (license_key,)
    ).fetchall()
    conn.close()
    return jsonify({
        "license_key": license_key,
        "credits": row["credits"] if row else 0,
        "transactions": [dict(t) for t in txs],
    })


@app.route('/api/v1/credits/balance', methods=['GET'])
def proxy_credits_balance():
    license_key = (request.args.get("license_key") or "").strip()
    if not license_key:
        return jsonify({"error": "license_key krävs som query-parameter"}), 400
    valid, reason = _validate_fossbilling_license(license_key)
    if not valid:
        return jsonify({"error": f"Obehörig: {reason}"}), 401
    conn = get_db_connection()
    row = conn.execute(
        "SELECT credits FROM license_credits WHERE license_key = ?", (license_key,)
    ).fetchone()
    conn.close()
    return jsonify({"credits_remaining": row["credits"] if row else 0})


@app.route('/api/webhook/fossbilling', methods=['POST'])
def webhook_fossbilling():
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    if not webhook_secret or request.headers.get("X-Webhook-Secret") != webhook_secret:
        return jsonify({"error": "Ogiltig webhook-hemlighet"}), 401
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license") or "").strip()
    try:
        amount = int(data.get("credits", 0))
    except (TypeError, ValueError):
        amount = 0
    note = (data.get("note") or "").strip()
    if not license_key or amount <= 0:
        return jsonify({"error": "Fälten 'license' och 'credits' (>0) krävs"}), 400
    credits_after = add_credits(license_key, amount, "webhook_add", note)
    return jsonify({"success": True, "license_key": license_key, "credits_after": credits_after})


@app.route('/api/diagnostics', methods=['GET'])
def get_diagnostics():
    import platform

    # Library versions
    try:
        import PIL
        pil_ver = PIL.__version__
    except Exception:
        pil_ver = "EJ INSTALLERAD"
    try:
        import brother_ql
        bql_ver = getattr(brother_ql, '__version__', None)
        if not bql_ver:
            import importlib.metadata
            bql_ver = importlib.metadata.version('brother-ql')
    except Exception:
        bql_ver = "EJ INSTALLERAD"

    # Windows printers
    printers = []
    try:
        import win32print
        for p in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS):
            printers.append(p[2])
    except Exception as e:
        printers = [f"(Kunde inte hämta skrivarlista: {e})"]

    # Current printer settings
    settings = {
        "brother_enabled": get_setting("brother_enabled", "false"),
        "brother_ip": get_setting("brother_ip", ""),
        "brother_model": get_setting("brother_model", ""),
        "brother_label_size": get_setting("brother_label_size", "17x54"),
    }

    # Last 200 lines of log
    log_content = ""
    try:
        with open(_log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        log_content = "".join(lines[-200:])
    except FileNotFoundError:
        log_content = "(Ingen logg hittades — ingen utskrift har testats ännu)"
    except Exception as e:
        log_content = f"(Fel vid läsning av logg: {e})"

    return jsonify({
        "python": sys.version,
        "pil": pil_ver,
        "brother_ql": bql_ver,
        "os": platform.platform(),
        "printers": printers,
        "settings": settings,
        "log": log_content,
    })


@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    try:
        open(_log_path, 'w', encoding='utf-8').close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    import webbrowser
    def _open_browser():
        time.sleep(1.5)
        webbrowser.open('http://127.0.0.1:5000')
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
