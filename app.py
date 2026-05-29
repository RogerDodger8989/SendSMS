import os
import sqlite3
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
import requests
from dotenv import load_dotenv

# Load environment variables from .env if present (for local testing)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hemlig-standard-nyckel-for-sessions")

# Configuration
DB_DIR = os.environ.get("DB_DIR", "/app/data")
DB_PATH = os.path.join(DB_DIR, "sms_logg.db")

def get_db_connection():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
    
    # Add picked_up column if missing
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
    
    # Insert default settings
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('elks_username', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('elks_password', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('elks_sender', 'Butiken')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('test_mode', 'false')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('app_pin', '')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('purge_days', '90')")
        
    cursor.execute("SELECT count(*) FROM templates")
    if cursor.fetchone()[0] == 0:
        default_templates = [
            ("Klar för hämtning", "Hej! Din {vara} är nu klar för hämtning. Att betala: {summa} kr. Välkommen!"),
            ("Försenad", "Hej! Tyvärr är din {vara} lite försenad. Vi hör av oss så fort den är klar. Mvh Butiken"),
            ("Påminnelse", "Hej! Vi vill påminna om att din {vara} finns redo att hämtas ut. Att betala: {summa} kr. Välkommen!")
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

def sanitize_phone_number(number):
    clean_num = re.sub(r'[\s\-]', '', number)
    if clean_num.startswith('07') and len(clean_num) == 10:
        clean_num = '+46' + clean_num[1:]
    return clean_num

def get_setting(key, default=""):
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default

@app.before_request
def require_login():
    # Allow access to index, static files, and auth endpoints
    if request.endpoint in ['index', 'auth_status', 'login', 'static']:
        return
        
    # Check if a PIN is set. If so, user must be logged in for API access.
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

# --- AUTH ENDPOINTS ---
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
    data = request.json
    pin = data.get('pin', '').strip()
    
    app_pin = get_setting("app_pin", "")
    master_pin = os.environ.get("MASTER_PIN", "")
    
    if not app_pin:
        if not pin: return jsonify({"success": False, "error": "PIN får inte vara tom"}), 400
        conn = get_db_connection()
        conn.execute("UPDATE settings SET value = ? WHERE key = 'app_pin'", (pin,))
        conn.commit()
        conn.close()
        session["logged_in"] = True
        return jsonify({"success": True})
        
    if pin == app_pin or (master_pin and pin == master_pin):
        session["logged_in"] = True
        return jsonify({"success": True})
        
    return jsonify({"success": False, "error": "Fel PIN-kod"}), 401

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

# --- DATA ENDPOINTS ---
@app.route('/api/history', methods=['GET'])
def get_history():
    search_query = request.args.get('q', '').strip()
    conn = get_db_connection()
    
    if search_query:
        like_query = f"%{search_query}%"
        history = conn.execute('''
            SELECT * FROM sms_log 
            WHERE phone_number LIKE ? 
               OR article LIKE ? 
               OR amount LIKE ? 
               OR message LIKE ?
            ORDER BY timestamp DESC
        ''', (like_query, like_query, like_query, like_query)).fetchall()
    else:
        history = conn.execute('SELECT * FROM sms_log ORDER BY timestamp DESC LIMIT 100').fetchall()
        
    conn.close()
    return jsonify([dict(row) for row in history])

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
        settings = {
            "elks_username": get_setting("elks_username"),
            "elks_password": get_setting("elks_password"),
            "elks_sender": get_setting("elks_sender", "Butiken"),
            "test_mode": get_setting("test_mode", "false"),
            "purge_days": get_setting("purge_days", "90")
        }
        return jsonify(settings)
    
    elif request.method == 'POST':
        data = request.json
        conn = get_db_connection()
        for key in ['elks_username', 'elks_password', 'elks_sender', 'test_mode', 'purge_days']:
            if key in data:
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (str(data[key]).lower() if key == 'test_mode' else data[key], key))
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
            
            # 46elks returnerar saldot i 1/10000 dels valuta
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
    amount = data.get('amount', '')
    message = data.get('message', '')
    
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
    conn.execute('''
        INSERT INTO sms_log (phone_number, article, amount, message, api_id, status, timestamp, picked_up)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    ''', (sanitized_number, article, amount, message, api_id, status, current_time))
    conn.commit()
    conn.close()
    
    if error_msg:
        return jsonify({"success": False, "error": error_msg}), 500
        
    return jsonify({"success": True, "status": status})

if __name__ == '__main__':
    if not os.path.exists('data') and not os.environ.get('DB_DIR'):
        os.environ['DB_DIR'] = './data'
        DB_DIR = './data'
        DB_PATH = os.path.join(DB_DIR, "sms_logg.db")
        init_db()
    
    app.run(host='0.0.0.0', port=5000, debug=True)
