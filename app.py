import os
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

# --- Setup Debug Logging ---
debug_logger = logging.getLogger("BrotherPrint")
debug_logger.setLevel(logging.DEBUG)
try:
    fh = logging.FileHandler('data/print_debug.log', encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    debug_logger.addHandler(fh)
except:
    pass

from dotenv import load_dotenv
import socket

load_dotenv()

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


app = Flask(__name__)
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
        phone_number = format_phone_for_print(phone_number)
        debug_logger.info(f"---- STARTAR UTSKRIFT ----")
        debug_logger.info(f"Mottagen Info -> IP/Namn: {ip_address}, Modell: {model}, Storlek: {label_size}")
        if not ip_address or not model or not label_size:
            debug_logger.error("Saknar IP, modell eller etikettstorlek!")
            return False, "IP, modell eller etikett-storlek saknas"
        
        date_str = timestamp.split(' ')[0] if timestamp else ""
        time_str = timestamp.split(' ')[1] if timestamp and ' ' in timestamp else ""
        debug_logger.debug("Förbereder bild för utskrift...")
        
        # Load font mappings
        try:
            with open('data/fonts.json', 'r', encoding='utf-8') as f:
                font_settings = json.load(f)
        except Exception as e:
            debug_logger.error(f"Kunde inte ladda fonts.json: {e}")
            font_settings = {"family": "Arial", "sizes": {"title": 42, "body": 24, "footer": 18}}
            
        font_family = font_settings.get("family", "Arial")
        font_sizes = font_settings.get("sizes", {"title": 42, "body": 24, "footer": 18})
        
        try:
            from PIL import Image, ImageDraw, ImageFont
            import win32print
        except ImportError as e:
            debug_logger.error(f"Ett bibliotek (PIL eller win32print) saknas: {e}")
            return False, f"Bibliotek saknas: {e}"

        dimensions = {
            '17x54': (566, 165),
            '29x90': (991, 306),
            '39x90': (991, 413),
            '62x29': (696, 271),
            '62x100': (1109, 696)
        }
        canvas_size = dimensions.get(label_size, (566, 165))
        
        img = Image.new('RGB', canvas_size, color='white')
        d = ImageDraw.Draw(img)
        
        try:
            large_size = int(canvas_size[1] * 0.30)
            med_size = int(canvas_size[1] * 0.22)
            font_path = f"c:/Windows/Fonts/{font_family.lower()}.ttf"
            font_large = ImageFont.truetype(font_path, large_size)
            font_medium = ImageFont.truetype(font_path, med_size)
        except IOError:
            debug_logger.warning("Kunde inte hitta vald font, använder standardfont.")
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

        img = img.transpose(Image.ROTATE_90)
        debug_logger.debug(f"Bild genererad framgångsrikt! (Storlek: {img.size})")
        
        try:
            from brother_ql.conversion import convert
            from brother_ql.raster import BrotherQLRaster
        except ImportError as e:
            debug_logger.error(f"Kunde inte importera brother_ql: {e}")
            return False, f"brother_ql saknas: {e}"
        
        debug_logger.debug("Konverterar till Brother-raster...")
        qlr = BrotherQLRaster(model)
        instructions = convert(
            qlr=qlr,
            images=[img],
            label=label_size,
            rotate='0',
            threshold=70,
            dither=False,
            compress=False,
            red=False,
            dpi_600=False,
            hq=True,
            align='center'
        )
        debug_logger.debug(f"Raster-instruktioner klara (Storlek: {len(instructions)} bytes).")
        
        printer_name = ip_address.strip()
        is_ip = ("." in printer_name and not printer_name.startswith("\\\\") and "brother" not in printer_name.lower()) or printer_name.startswith("tcp://")
        
        if is_ip:
            debug_logger.info(f"Använder nätverksbackend för IP: {printer_name}")
            from brother_ql.backends.helpers import send
            if not printer_name.startswith('tcp://'):
                printer_name = f'tcp://{printer_name}'
            debug_logger.debug("Skickar instruktioner över tcp...")
            send(instructions=instructions, printer_identifier=printer_name, backend_identifier='network', blocking=True)
            debug_logger.info("Instruktioner skickade över nätverket!")
        else:
            debug_logger.info(f"Använder Windows win32print för skrivare: {printer_name}")
            try:
                hprinter = win32print.OpenPrinter(printer_name)
                debug_logger.debug(f"Fick skrivar-handle: {hprinter}")
            except Exception as e:
                debug_logger.error(f"Kunde INTE öppna Windows-skrivaren '{printer_name}': {e}")
                return False, f"Hittade inte skrivaren '{printer_name}' i Windows"
                
            try:
                debug_logger.debug("Startar utskriftsjobb i Windows-kön...")
                win32print.StartDocPrinter(hprinter, 1, ("Etikett - SendSMS", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hprinter)
                    win32print.WritePrinter(hprinter, instructions)
                    debug_logger.debug("Rådata överförd till Windows-kön.")
                    win32print.EndPagePrinter(hprinter)
                finally:
                    win32print.EndDocPrinter(hprinter)
                    debug_logger.info("Utskriftsjobb avslutat och inlagt i kön!")
            finally:
                win32print.ClosePrinter(hprinter)
                debug_logger.debug("Skrivar-handle stängd.")
        
        debug_logger.info("---- UTSKRIFT KLAR ----")
        return True, "Utskrift skickad"
    except Exception as e:
        debug_logger.error(f"Kritiskt undantag i print_brother_label: {str(e)}\n{traceback.format_exc()}")
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


@app.before_request
def require_login():
    if request.endpoint in ['index', 'auth_status', 'login', 'static']:
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
            "brother_enabled": get_setting("brother_enabled", "false")
        }
        return jsonify(settings)

    elif request.method == 'POST':
        data = request.json
        sender = data.get('elks_sender', '')
        if len(sender) > 11:
            return jsonify({"success": False, "error": "Avsändaren får max vara 11 tecken"}), 400
        conn = get_settings_connection()
        for key in ['elks_username', 'elks_password', 'elks_sender', 'test_mode', 'purge_days', 'brother_ip', 'brother_model', 'brother_label_size', 'brother_enabled']:
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

    elks_username = get_setting("elks_username")
    elks_password = get_setting("elks_password")
    elks_sender = get_setting("elks_sender", "Butiken")
    test_mode = get_setting("test_mode", "false") == "true"

    api_url = "https://api.46elks.com/a1/sms"
    payload = {
        "from": elks_sender,
        "to": sanitized_number,
        "message": message
    }

    status = "Failed"
    api_id = ""
    error_msg = None

    if test_mode:
        status = "Övningsläge (Ej skickat)"
    elif elks_username and elks_password:
        try:
            response = requests.post(
                api_url,
                data=payload,
                auth=(elks_username, elks_password),
                timeout=10
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
        error_msg = "Saknar inloggningsuppgifter. Fyll i API-nycklar i inställningarna."
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
        dimensions = {
            '17x54': (566, 165), '29x90': (991, 306), '39x90': (991, 413),
            '62x29': (696, 271), '62x100': (1109, 696)
        }
        target = dimensions.get(label_size, (566, 165))

        img_data = base64.b64decode(image_b64)
        img = Image.open(_io.BytesIO(img_data)).convert('RGB')
        img = img.resize(target, Image.LANCZOS)
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
