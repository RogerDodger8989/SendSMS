import os
import re
import time
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
import requests
from dotenv import load_dotenv
import socket
import threading

load_dotenv()

# DB path: defaults to ./data (works locally and in Docker with WORKDIR /app)
DB_DIR = os.environ.get("DB_DIR", "./data")
DB_PATH = os.path.join(DB_DIR, "sms_logg.db")

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

    try:
        cursor.execute("ALTER TABLE sms_log ADD COLUMN label_info TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

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
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = 'purge_days'").fetchone()
    purge_days = int(row['value']) if row and row['value'].isdigit() else 90
    cutoff_date = datetime.now() - timedelta(days=purge_days)
    cutoff_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('DELETE FROM sms_log WHERE timestamp < ?', (cutoff_str,))
    conn.commit()
    conn.close()


init_db()

def print_brother_label(ip_address, model, label_size, phone_number, name, timestamp, label_info=""):
    try:
        if not ip_address or not model or not label_size:
            return False, "IP, modell eller etikett-storlek saknas"
            
        date_str = timestamp.split(' ')[0] if timestamp else ""
        
        from PIL import Image, ImageDraw, ImageFont
        
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
            font_large = ImageFont.truetype("arial.ttf", large_size)
            font_medium = ImageFont.truetype("arial.ttf", med_size)
        except:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()

        if name:
            y_offset = int(canvas_size[1] * 0.05)
            y_step = int(canvas_size[1] * 0.32)
            d.text((20, y_offset), name, fill='black', font=font_large)
            d.text((20, y_offset + y_step), phone_number, fill='black', font=font_medium)
            d.text((int(canvas_size[0]*0.65), y_offset + y_step), date_str, fill='black', font=font_medium)
            if label_info:
                d.text((20, y_offset + 2 * y_step), label_info, fill='black', font=font_medium)
        else:
            y_offset = int(canvas_size[1] * 0.15)
            y_step = int(canvas_size[1] * 0.35)
            d.text((20, y_offset), phone_number, fill='black', font=font_large)
            d.text((int(canvas_size[0]*0.65), y_offset), date_str, fill='black', font=font_medium)
            if label_info:
                d.text((20, y_offset + y_step), label_info, fill='black', font=font_large)

        img = img.transpose(Image.ROTATE_90)
        
        from brother_ql.conversion import convert
        from brother_ql.raster import BrotherQLRaster
        from brother_ql.backends.helpers import send
        
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
        
        send(instructions=instructions, printer_identifier=f'tcp://{ip_address}', backend_identifier='network', blocking=True)
        return True, "Utskrift skickad"
    except Exception as e:
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
    conn = get_db_connection()
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
        conn = get_db_connection()
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

    conn = get_db_connection()
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
    conn = get_db_connection()
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
    conn = get_db_connection()
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
        conn = get_db_connection()
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

    if status in ['Sent', 'Delivered', 'Övningsläge (Ej skickat)'] and print_label:
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
        return jsonify({"success": False, "error": "IP-adress eller modell saknas"}), 400
    
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
