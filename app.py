import os
import re
import json
import csv
import io
import logging
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import gspread
from google.oauth2.service_account import Credentials

# --- 1. Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_jg_minis_v4_2_production')

# --- 3. Environment Variables ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290') # Just numbers, no + or spaces
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

# --- 4. Google Sheets Setup ---
gc = None
sheet = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        SHEET_NAME = 'BASE DE DADOS JG'
        sheet = gc.open(SHEET_NAME).sheet1
        logging.info('gspread auth bem-sucedida')
    except Exception as e:
        logging.error(f'Erro na autenticação gspread ou ao abrir planilha: {e}')
else:
    logging.warning('GOOGLE_SHEETS_CREDENTIALS não definida - usando fallback sem Sheets')

# --- 5. Validation Functions ---
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    return phone.isdigit() and 10 <= len(phone) <= 11 # 10-11 digits for Brazilian numbers

# --- 6. Database Initialization ---
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # Users table with name, email, phone
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Reservations table with quantity, no date
    c.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        service TEXT NOT NULL,
        quantity INTEGER DEFAULT 1,
        status TEXT DEFAULT 'pending',
        approved_by INTEGER,
        denied_reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (approved_by) REFERENCES users(id)
    )''')

    # Stock table for real-time quantity management
    c.execute('''CREATE TABLE IF NOT EXISTS stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service TEXT UNIQUE NOT NULL,
        quantity INTEGER DEFAULT 0,
        last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Waiting list table
    c.execute('''CREATE TABLE IF NOT EXISTS waiting_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        service TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # Create admin user if not exists
    c.execute('SELECT id FROM users WHERE email = ?', ('admin@jgminis.com.br',))
    if not c.fetchone():
        hashed_pw = generate_password_hash('admin123')
        c.execute('INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)', 
                  ('Admin', 'admin@jgminis.com.br', '11999999999', hashed_pw, 'admin'))
        logging.info('Usuário admin criado no DB')

    # Initial stock sync from Google Sheet if stock table is empty
    c.execute('SELECT COUNT(*) FROM stock')
    if c.fetchone()[0] == 0 and sheet:
        try:
            records = sheet.get_all_records()
            for record in records[1:]:  # Skip header row
                service = record.get('NOME DA MINIATURA', '')
                qty = record.get('QUANTIDADE DISPONIVEL', 0)
                if service:
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
            logging.info('Estoque inicial sincronizado da planilha para o DB')
        except Exception as e:
            logging.error(f'Erro na sincronização inicial do estoque: {e}')
    
    conn.commit()
    conn.close()

init_db()

# --- 7. Helper Function to Load Thumbnails (from DB stock + Sheet data) ---
def load_thumbnails():
    thumbnails = []
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Get stock quantities from DB
    c.execute("SELECT service, quantity FROM stock ORDER BY service")
    stock_data = {row[0]: row[1] for row in c.fetchall()}
    
    # Get other details from Google Sheet
    if sheet:
        try:
            records = sheet.get_all_records()
            if not records:
                logging.warning("Planilha vazia - thumbnails fallback")
                return [{'service': 'Fallback', 'quantity': 0, 'image': LOGO_URL, 'price': '0', 'obs': 'Adicione dados na planilha', 'marca': '', 'previsao': ''}]
            
            for record in records: # Process all records, not just first 12, for filtering
                service = record.get('NOME DA MINIATURA', '')
                if not service: continue # Skip empty service names
                
                marca = record.get('MARCA/FABRICANTE', '')
                obs = record.get('OBSERVAÇÕES', '')
                image = record.get('IMAGEM', LOGO_URL)
                price_raw = record.get('VALOR', 0)
                previsao = record.get('PREVISÃO DE CHEGADA', '')
                
                # Format price
                price_str = str(price_raw) if price_raw is not None else '0'
                price = price_str.replace('R$ ', '').replace(',', '.') # Ensure dot as decimal separator
                try:
                    price = float(price)
                except ValueError:
                    price = 0.0
                
                # Get quantity from DB stock, fallback to 0 if not found
                quantity = stock_data.get(service, 0) 

                thumbnails.append({
                    'service': service,
                    'marca': marca,
                    'obs': obs,
                    'image': image,
                    'price': f"{price:.2f}".replace('.', ','), # Format for display
                    'quantity': quantity,
                    'previsao': previsao
                })
            logging.info(f'Carregados {len(thumbnails)} thumbnails da planilha (stock sync: {len(stock_data)} itens)')
            
        except Exception as e:
            logging.error(f'Erro ao carregar thumbnails da planilha: {e}')
            thumbnails = [{'service': 'Erro de Carregamento', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': str(e), 'marca': '', 'previsao': ''}]
    else:
        thumbnails = [{'service': 'Sem Integração Sheets', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': 'Configure GOOGLE_SHEETS_CREDENTIALS', 'marca': '', 'previsao': ''}]
    
    conn.close()
    return thumbnails

# --- 8. HTML Templates (as Jinja2 strings) ---

INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2 - Home</title>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
        header { background-color: #004085; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        header img { height: 60px; vertical-align: middle; margin-right: 15px; }
        header h1 { display: inline-block; margin: 0; font-size: 2em; }
        nav { background-color: #e9ecef; padding: 10px 20px; text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        nav a { color: #007bff; text-decoration: none; margin: 0 15px; font-weight: bold; transition: color 0.3s; }
        nav a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { padding: 10px 20px; margin-top: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 25px; padding: 25px; max-width: 1200px; margin: 20px auto; }
        .thumbnail { background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; text-align: center; transition: transform 0.3s ease, box-shadow 0.3s ease; }
        .thumbnail:hover { transform: translateY(-5px); box-shadow: 0 6px 16px rgba(0,0,0,0.12); }
        .thumbnail img { width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #eee; }
        .thumbnail-content { padding: 15px; }
        .thumbnail h3 { font-size: 1.3em; color: #007bff; margin-top: 0; margin-bottom: 8px; }
        .thumbnail p { font-size: 0.95em; color: #555; margin-bottom: 5px; line-height: 1.4; }
        .thumbnail .price { font-size: 1.1em; font-weight: bold; color: #28a745; margin-top: 10px; }
        .thumbnail .quantity { font-size: 0.9em; color: #6c757d; margin-bottom: 15px; }
        .action-buttons { display: flex; justify-content: center; gap: 10px; margin-top: 15px; }
        .btn { display: inline-block; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; transition: background-color 0.3s ease, color 0.3s ease; }
        .btn-reserve { background-color: #28a745; color: white; border: none; }
        .btn-reserve:hover { background-color: #218838; }
        .btn-whatsapp { background-color: #25D366; color: white; border: none; }
        .btn-whatsapp:hover { background-color: #1DA851; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 20px; margin-top: 40px; font-size: 0.9em; }
        @media (max-width: 768px) {
            header h1 { font-size: 1.8em; }
            nav a { margin: 0 10px; }
            .grid-container { grid-template-columns: 1fr; padding: 15px; }
            .thumbnail img { height: 150px; }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS">
        <h1>JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        {% if not session.get('user_id') %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% else %}
            <a href="{{ url_for('reservar') }}">Reservar Miniaturas</a>
            {% if session.get('role') == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
            <a href="{{ url_for('profile') }}">Meu Perfil</a>
            <a href="{{ url_for('logout') }}">Logout</a>
        {% endif %}
    </nav>
    <div class="flash-messages">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </div>
    <main class="grid-container">
        {% for thumb in thumbnails %}
            <div class="thumbnail">
                <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                <div class="thumbnail-content">
                    <h3>{{ thumb.service }}</h3>
                    <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
                    <p class="price">R$ {{ thumb.price }}</p>
                    <p class="quantity">Disponível: {{ thumb.quantity }}</p>
                    <div class="action-buttons">
                        <a href="{{ url_for('reserve_single', service=thumb.service) }}" class="btn btn-reserve">Reservar Agora</a>
                        {% if thumb.quantity == 0 %}
                            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="btn btn-whatsapp" target="_blank">Entrar em Contato</a>
                        {% endif %}
                    </div>
                </div>
            </div>
        {% endfor %}
        {% if not thumbnails %}
            <div class="thumbnail" style="grid-column: 1 / -1;">
                <div class="thumbnail-content">
                    <h3>Nenhuma miniatura disponível</h3>
                    <p>Verifique a planilha ou o estoque.</p>
                </div>
            </div>
        {% endif %}
    </main>
    <footer>
        <p>&copy; {{ datetime.now().year }} JG MINIS. Todos os direitos reservados.</p>
    </footer>
</body>
</html>
'''

REGISTER_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Registrar - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .register-container { background-color: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        .register-container h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        .register-container form { display: flex; flex-direction: column; gap: 15px; }
        .register-container label { text-align: left; font-weight: bold; color: #555; }
        .register-container input[type="text"],
        .register-container input[type="email"],
        .register-container input[type="password"] {
            padding: 12px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; width: 100%; box-sizing: border-box;
        }
        .register-container button {
            background-color: #28a745; color: white; padding: 12px 20px; border: none; border-radius: 5px;
            font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease;
        }
        .register-container button:hover { background-color: #218838; }
        .register-container p { margin-top: 20px; font-size: 0.95em; }
        .register-container a { color: #007bff; text-decoration: none; transition: color 0.3s ease; }
        .register-container a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { margin-top: 15px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="register-container">
        <h1>Registrar</h1>
        <div class="flash-messages">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </div>
        <form method="post">
            <label for="name">Nome:</label>
            <input type="text" id="name" name="name" required value="{{ request.form.name if request.method == 'POST' else '' }}">
            <label for="email">Email:</label>
            <input type="email" id="email" name="email" required value="{{ request.form.email if request.method == 'POST' else '' }}">
            <label for="phone">Telefone:</label>
            <input type="text" id="phone" name="phone" required placeholder="Apenas números (DDD+Número)" value="{{ request.form.phone if request.method == 'POST' else '' }}">
            <label for="password">Senha (mín. 6 caracteres):</label>
            <input type="password" id="password" name="password" required minlength="6">
            <button type="submit">Registrar</button>
        </form>
        <p>Já tem uma conta? <a href="{{ url_for('login') }}">Fazer Login</a></p>
        <p><a href="{{ url_for('index') }}">Voltar para Home</a></p>
    </div>
</body>
</html>
'''

LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-container { background-color: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        .login-container h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        .login-container form { display: flex; flex-direction: column; gap: 15px; }
        .login-container label { text-align: left; font-weight: bold; color: #555; }
        .login-container input[type="email"],
        .login-container input[type="password"] {
            padding: 12px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; width: 100%; box-sizing: border-box;
        }
        .login-container button {
            background-color: #007bff; color: white; padding: 12px 20px; border: none; border-radius: 5px;
            font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease;
        }
        .login-container button:hover { background-color: #0056b3; }
        .login-container p { margin-top: 20px; font-size: 0.95em; }
        .login-container a { color: #28a745; text-decoration: none; transition: color 0.3s ease; }
        .login-container a:hover { color: #218838; text-decoration: underline; }
        .flash-messages { margin-top: 15px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Login</h1>
        <div class="flash-messages">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </div>
        <form method="post">
            <label for="email">Email:</label>
            <input type="email" id="email" name="email" required value="{{ request.form.email if request.method == 'POST' else '' }}">
            <label for="password">Senha:</label>
            <input type="password" id="password" name="password" required>
            <button type="submit">Entrar</button>
        </form>
        <p>Não tem uma conta? <a href="{{ url_for('register') }}">Registrar</a></p>
        <p><a href="{{ url_for('index') }}">Voltar para Home</a></p>
    </div>
</body>
</html>
'''

RESERVE_SINGLE_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reservar {{ thumb.service }} - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .reserve-container { background-color: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 500px; text-align: center; }
        .reserve-container h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        .reserve-container img { max-width: 80%; height: auto; border-radius: 8px; margin-bottom: 20px; }
        .reserve-container p { font-size: 1em; color: #555; margin-bottom: 10px; }
        .reserve-container .price { font-size: 1.2em; font-weight: bold; color: #28a745; margin-bottom: 15px; }
        .reserve-container form { display: flex; flex-direction: column; gap: 15px; align-items: center; }
        .reserve-container label { font-weight: bold; color: #555; }
        .reserve-container input[type="number"] {
            padding: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; width: 80px; text-align: center;
        }
        .reserve-container button {
            background-color: #007bff; color: white; padding: 12px 25px; border: none; border-radius: 5px;
            font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease; margin-top: 10px;
        }
        .reserve-container button:hover { background-color: #0056b3; }
        .btn-whatsapp { background-color: #25D366; color: white; padding: 12px 25px; border: none; border-radius: 5px;
            font-size: 1.1em; cursor: pointer; text-decoration: none; display: inline-block; margin-top: 15px;
            transition: background-color 0.3s ease; }
        .btn-whatsapp:hover { background-color: #1DA851; }
        .flash-messages { margin-top: 15px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .back-link { margin-top: 25px; font-size: 0.95em; }
        .back-link a { color: #007bff; text-decoration: none; transition: color 0.3s ease; }
        .back-link a:hover { color: #0056b3; text-decoration: underline; }
    </style>
</head>
<body>
    <div class="reserve-container">
        <h1>Reservar {{ thumb.service }}</h1>
        <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
        <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
        <p class="price">R$ {{ thumb.price }}</p>
        <p>Disponível: {{ current_stock }}</p>
        <div class="flash-messages">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </div>
        {% if current_stock > 0 %}
            <form method="post">
                <label for="quantity">Quantidade:</label>
                <input type="number" id="quantity" name="quantity" min="1" max="{{ current_stock }}" value="1" required>
                <button type="submit">Confirmar Reserva</button>
            </form>
        {% else %}
            <p>Estoque indisponível para esta miniatura.</p>
            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="btn-whatsapp" target="_blank">Entrar em Contato</a>
        {% endif %}
        <p class="back-link"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
    </div>
</body>
</html>
'''

RESERVAR_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reservar Múltiplas Miniaturas - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 0; }
        header { background-color: #004085; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        header h1 { margin: 0; font-size: 2em; }
        nav { background-color: #e9ecef; padding: 10px 20px; text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        nav a { color: #007bff; text-decoration: none; margin: 0 15px; font-weight: bold; transition: color 0.3s; }
        nav a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { padding: 10px 20px; margin-top: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .container { max-width: 1200px; margin: 20px auto; padding: 20px; background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        .filter-form { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 30px; padding: 15px; border: 1px solid #eee; border-radius: 8px; background-color: #f9f9f9; }
        .filter-form label { display: flex; align-items: center; gap: 5px; font-weight: bold; color: #555; }
        .filter-form input[type="text"], .filter-form select { padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.9em; }
        .filter-form button { background-color: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; transition: background-color 0.3s ease; }
        .filter-form button:hover { background-color: #0056b3; }
        .reservation-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }
        .reservation-item { border: 1px solid #ddd; border-radius: 8px; overflow: hidden; background-color: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
        .reservation-item img { width: 100%; height: 150px; object-fit: cover; border-bottom: 1px solid #eee; }
        .item-content { padding: 15px; text-align: center; }
        .item-content h3 { font-size: 1.2em; color: #007bff; margin-top: 0; margin-bottom: 8px; }
        .item-content p { font-size: 0.9em; color: #555; margin-bottom: 5px; }
        .item-content .price { font-weight: bold; color: #28a745; }
        .item-content .quantity-input { display: flex; justify-content: center; align-items: center; gap: 5px; margin-top: 10px; }
        .item-content .quantity-input input[type="number"] { width: 60px; padding: 5px; border: 1px solid #ccc; border-radius: 4px; text-align: center; font-size: 0.9em; }
        .item-content .checkbox-label { display: flex; align-items: center; justify-content: center; gap: 5px; margin-top: 10px; font-weight: bold; color: #333; }
        .item-content .btn-whatsapp { background-color: #25D366; color: white; padding: 8px 15px; border: none; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 0.9em; margin-top: 10px; display: inline-block; transition: background-color 0.3s ease; }
        .item-content .btn-whatsapp:hover { background-color: #1DA851; }
        .submit-all-btn { background-color: #28a745; color: white; padding: 15px 30px; border: none; border-radius: 8px; font-size: 1.2em; cursor: pointer; transition: background-color 0.3s ease; display: block; width: fit-content; margin: 30px auto 0; }
        .submit-all-btn:hover { background-color: #218838; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 20px; margin-top: 40px; font-size: 0.9em; }
        @media (max-width: 768px) {
            .filter-form { flex-direction: column; align-items: stretch; }
            .filter-form label { justify-content: space-between; }
            .reservation-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header>
        <h1>Reservar Múltiplas Miniaturas</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        {% if not session.get('user_id') %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% else %}
            <a href="{{ url_for('reservar') }}">Reservar Miniaturas</a>
            {% if session.get('role') == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
            <a href="{{ url_for('profile') }}">Meu Perfil</a>
            <a href="{{ url_for('logout') }}">Logout</a>
        {% endif %}
    </nav>
    <div class="flash-messages">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </div>
    <div class="container">
        <form method="get" class="filter-form">
            <label>Disponíveis: <input type="checkbox" name="available" value="1" {% if request.args.get('available') == '1' %}checked{% endif %}></label>
            <label>Ordenar por:
                <select name="order_by">
                    <option value="">Padrão</option>
                    <option value="service_asc" {% if request.args.get('order_by') == 'service_asc' %}selected{% endif %}>Nome (A-Z)</option>
                    <option value="service_desc" {% if request.args.get('order_by') == 'service_desc' %}selected{% endif %}>Nome (Z-A)</option>
                    <option value="price_asc" {% if request.args.get('order_by') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                    <option value="price_desc" {% if request.args.get('order_by') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                    <option value="previsao_asc" {% if request.args.get('order_by') == 'previsao_asc' %}selected{% endif %}>Previsão (Antiga)</option>
                    <option value="previsao_desc" {% if request.args.get('order_by') == 'previsao_desc' %}selected{% endif %}>Previsão (Recente)</option>
                </select>
            </label>
            <label>Marca: <input type="text" name="marca" value="{{ request.args.get('marca', '') }}" placeholder="Filtrar por marca"></label>
            <button type="submit">Aplicar Filtros</button>
            <button type="button" onclick="window.location.href='{{ url_for('reservar') }}'">Limpar Filtros</button>
        </form>

        <form method="post">
            <div class="reservation-grid">
                {% for thumb in thumbnails %}
                    <div class="reservation-item">
                        <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                        <div class="item-content">
                            <h3>{{ thumb.service }}</h3>
                            <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
                            <p class="price">R$ {{ thumb.price }}</p>
                            <p>Disponível: {{ thumb.quantity }}</p>
                            {% if thumb.quantity > 0 %}
                                <label class="checkbox-label">
                                    <input type="checkbox" name="services" value="{{ thumb.service }}" onchange="toggleQuantityInput(this)"> Selecionar
                                </label>
                                <div class="quantity-input" id="qty-{{ thumb.service | replace(' ', '-') }}" style="display:none;">
                                    <label for="quantity_{{ thumb.service }}">Qtd:</label>
                                    <input type="number" id="quantity_{{ thumb.service }}" name="quantity_{{ thumb.service }}" min="1" max="{{ thumb.quantity }}" value="1">
                                </div>
                            {% else %}
                                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="btn-whatsapp" target="_blank">Entrar em Contato</a>
                            {% endif %}
                        </div>
                    </div>
                {% endfor %}
            </div>
            {% if thumbnails %}
                <button type="submit" class="submit-all-btn">Confirmar Reservas Selecionadas</button>
            {% else %}
                <p style="text-align: center; margin-top: 30px;">Nenhuma miniatura encontrada com os filtros aplicados.</p>
            {% endif %}
        </form>
    </div>
    <script>
        function toggleQuantityInput(checkbox) {
            const service = checkbox.value;
            const qtyInputDiv = document.getElementById(`qty-${service.replace(/ /g, '-')}`);
            if (checkbox.checked) {
                qtyInputDiv.style.display = 'flex';
            } else {
                qtyInputDiv.style.display = 'none';
                qtyInputDiv.querySelector('input').value = 1; // Reset quantity
            }
        }
        // Initialize state based on current form values (e.g., after a failed submission)
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('input[name="services"]').forEach(checkbox => {
                if (checkbox.checked) {
                    toggleQuantityInput(checkbox);
                }
            });
        });
    </script>
    <footer>
        <p>&copy; {{ datetime.now().year }} JG MINIS. Todos os direitos reservados.</p>
    </footer>
</body>
</html>
'''

PROFILE_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meu Perfil - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 0; }
        header { background-color: #004085; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        header h1 { margin: 0; font-size: 2em; }
        nav { background-color: #e9ecef; padding: 10px 20px; text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        nav a { color: #007bff; text-decoration: none; margin: 0 15px; font-weight: bold; transition: color 0.3s; }
        nav a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { padding: 10px 20px; margin-top: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .container { max-width: 900px; margin: 20px auto; padding: 20px; background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 30px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .user-info p { font-size: 1em; margin-bottom: 8px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; font-size: 0.95em; }
        th { background-color: #f2f2f2; font-weight: bold; color: #333; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        tr:hover { background-color: #f1f1f1; }
        .no-reservations { text-align: center; color: #6c757d; margin-top: 20px; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 20px; margin-top: 40px; font-size: 0.9em; }
    </style>
</head>
<body>
    <header>
        <h1>Meu Perfil</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        {% if not session.get('user_id') %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% else %}
            <a href="{{ url_for('reservar') }}">Reservar Miniaturas</a>
            {% if session.get('role') == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
            <a href="{{ url_for('profile') }}">Meu Perfil</a>
            <a href="{{ url_for('logout') }}">Logout</a>
        {% endif %}
    </nav>
    <div class="flash-messages">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </div>
    <div class="container">
        <h2>Minhas Informações</h2>
        <div class="user-info">
            <p><strong>Nome:</strong> {{ user.name }}</p>
            <p><strong>Email:</strong> {{ user.email }}</p>
            <p><strong>Telefone:</strong> {{ user.phone }}</p>
            <p><strong>Membro desde:</strong> {{ user.data_cadastro }}</p>
        </div>

        <h2>Minhas Reservas</h2>
        {% if reservations %}
            <table>
                <thead>
                    <tr>
                        <th>Miniatura</th>
                        <th>Quantidade</th>
                        <th>Status</th>
                        <th>Data da Reserva</th>
                    </tr>
                </thead>
                <tbody>
                    {% for res in reservations %}
                        <tr>
                            <td>{{ res.service }}</td>
                            <td>{{ res.quantity }}</td>
                            <td>{{ res.status }}</td>
                            <td>{{ res.created_at }}</td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <p class="no-reservations">Você ainda não fez nenhuma reserva.</p>
        {% endif %}
    </div>
    <footer>
        <p>&copy; {{ datetime.now().year }} JG MINIS. Todos os direitos reservados.</p>
    </footer>
</body>
</html>
'''

ADMIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Admin - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 0; }
        header { background-color: #004085; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        header h1 { margin: 0; font-size: 2em; }
        nav { background-color: #e9ecef; padding: 10px 20px; text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        nav a { color: #007bff; text-decoration: none; margin: 0 15px; font-weight: bold; transition: color 0.3s; }
        nav a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { padding: 10px 20px; margin-top: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .container { max-width: 1200px; margin: 20px auto; padding: 20px; background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 30px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background-color: #f9f9f9; border-radius: 8px; padding: 20px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
        .stat-card h3 { color: #007bff; margin-top: 0; margin-bottom: 10px; font-size: 1.2em; }
        .stat-card p { font-size: 1.8em; font-weight: bold; color: #333; margin: 0; }
        .admin-actions { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 30px; }
        .admin-actions .btn { background-color: #6c757d; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; transition: background-color 0.3s ease; }
        .admin-actions .btn:hover { background-color: #5a6268; }
        .admin-actions .btn-primary { background-color: #007bff; } .admin-actions .btn-primary:hover { background-color: #0056b3; }
        .admin-actions .btn-success { background-color: #28a745; } .admin-actions .btn-success:hover { background-color: #218838; }
        .admin-actions .btn-warning { background-color: #ffc107; color: #333; } .admin-actions .btn-warning:hover { background-color: #e0a800; }
        .admin-actions .btn-danger { background-color: #dc3545; } .admin-actions .btn-danger:hover { background-color: #c82333; }
        .filter-form { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 20px; padding: 15px; border: 1px solid #eee; border-radius: 8px; background-color: #f9f9f9; }
        .filter-form label { display: flex; align-items: center; gap: 5px; font-weight: bold; color: #555; }
        .filter-form input[type="text"], .filter-form select { padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.9em; }
        .filter-form button { background-color: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; transition: background-color 0.3s ease; }
        .filter-form button:hover { background-color: #0056b3; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; font-size: 0.9em; }
        th { background-color: #f2f2f2; font-weight: bold; color: #333; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        tr:hover { background-color: #f1f1f1; }
        .action-form { display: inline-block; margin: 0 5px; }
        .action-form button { padding: 8px 12px; border-radius: 5px; border: none; cursor: pointer; font-size: 0.85em; }
        .action-form .btn-approve { background-color: #28a745; color: white; }
        .action-form .btn-deny { background-color: #dc3545; color: white; }
        .action-form .btn-delete { background-color: #6c757d; color: white; }
        .action-form .btn-promote { background-color: #007bff; color: white; }
        .action-form .btn-demote { background-color: #ffc107; color: #333; }
        .form-section { background-color: #f9f9f9; border-radius: 8px; padding: 20px; margin-top: 30px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
        .form-section h3 { color: #007bff; margin-top: 0; margin-bottom: 20px; text-align: center; }
        .form-section form { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        .form-section form label { font-weight: bold; color: #555; display: block; margin-bottom: 5px; }
        .form-section form input[type="text"],
        .form-section form input[type="number"],
        .form-section form input[type="url"],
        .form-section form select {
            padding: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.95em; width: 100%; box-sizing: border-box;
        }
        .form-section form button {
            background-color: #28a745; color: white; padding: 12px 20px; border: none; border-radius: 5px;
            font-size: 1em; cursor: pointer; transition: background-color 0.3s ease; grid-column: 1 / -1; margin-top: 10px;
        }
        .form-section form button:hover { background-color: #218838; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 20px; margin-top: 40px; font-size: 0.9em; }
        @media (max-width: 768px) {
            .stats-grid, .filter-form, .form-section form { grid-template-columns: 1fr; }
            .admin-actions { flex-direction: column; align-items: stretch; }
            .admin-actions .btn { width: 100%; }
        }
    </style>
</head>
<body>
    <header>
        <h1>Painel Administrativo</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        <a href="{{ url_for('reservar') }}">Reservar Miniaturas</a>
        <a href="{{ url_for('admin') }}">Admin</a>
        <a href="{{ url_for('profile') }}">Meu Perfil</a>
        <a href="{{ url_for('logout') }}">Logout</a>
    </nav>
    <div class="flash-messages">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </div>
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card"><h3>Total Usuários</h3><p>{{ stats.users }}</p></div>
            <div class="stat-card"><h3>Reservas Pendentes</h3><p>{{ stats.pending }}</p></div>
            <div class="stat-card"><h3>Total Reservas</h3><p>{{ stats.total_res }}</p></div>
            <div class="stat-card"><h3>Fila de Espera</h3><p>{{ stats.waiting }}</p></div>
        </div>

        <div class="admin-actions">
            <form method="post" class="action-form">
                <input type="hidden" name="action" value="sync_stock">
                <button type="submit" class="btn btn-warning">Sincronizar Estoque da Planilha</button>
            </form>
            <a href="{{ url_for('backup_db') }}" class="btn btn-primary">Backup DB (JSON)</a>
            <a href="{{ url_for('export_csv') }}" class="btn btn-primary">Export Reservas (CSV)</a>
        </div>

        <h2>Gerenciar Usuários</h2>
        <form method="get" class="filter-form">
            <label>Email: <input type="text" name="user_email_filter" value="{{ request.args.get('user_email_filter', '') }}" placeholder="Filtrar por email"></label>
            <label>Role:
                <select name="user_role_filter">
                    <option value="">Todos</option>
                    <option value="user" {% if request.args.get('user_role_filter') == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if request.args.get('user_role_filter') == 'admin' %}selected{% endif %}>Admin</option>
                </select>
            </label>
            <button type="submit">Filtrar Usuários</button>
            <button type="button" onclick="window.location.href='{{ url_for('admin') }}'">Limpar Filtros</button>
        </form>
        <table>
            <thead>
                <tr><th>ID</th><th>Nome</th><th>Email</th><th>Telefone</th><th>Role</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for user in users %}
                    <tr>
                        <td>{{ user.id }}</td>
                        <td>{{ user.name }}</td>
                        <td>{{ user.email }}</td>
                        <td>{{ user.phone }}</td>
                        <td>{{ user.role }}</td>
                        <td>
                            {% if user.id != session.get('user_id') %} {# Cannot modify self #}
                                <form method="post" class="action-form">
                                    <input type="hidden" name="action" value="promote_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-promote">Promover</button>
                                </form>
                                <form method="post" class="action-form">
                                    <input type="hidden" name="action" value="demote_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-demote">Rebaixar</button>
                                </form>
                                <form method="post" class="action-form" onsubmit="return confirm('Tem certeza que deseja deletar este usuário e suas reservas?');">
                                    <input type="hidden" name="action" value="delete_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-delete">Deletar</button>
                                </form>
                            {% else %}
                                (Você)
                            {% endif %}
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Gerenciar Reservas</h2>
        <form method="get" class="filter-form">
            <label>Miniatura/Email: <input type="text" name="res_search_filter" value="{{ request.args.get('res_search_filter', '') }}" placeholder="Miniatura ou Email"></label>
            <label>Status:
                <select name="res_status_filter">
                    <option value="">Todos</option>
                    <option value="pending" {% if request.args.get('res_status_filter') == 'pending' %}selected{% endif %}>Pending</option>
                    <option value="approved" {% if request.args.get('res_status_filter') == 'approved' %}selected{% endif %}>Approved</option>
                    <option value="denied" {% if request.args.get('res_status_filter') == 'denied' %}selected{% endif %}>Denied</option>
                </select>
            </label>
            <button type="submit">Filtrar Reservas</button>
            <button type="button" onclick="window.location.href='{{ url_for('admin') }}'">Limpar Filtros</button>
        </form>
        <table>
            <thead>
                <tr><th>ID</th><th>Usuário</th><th>Miniatura</th><th>Qtd</th><th>Status</th><th>Motivo Negação</th><th>Data Criação</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for res in reservations %}
                    <tr>
                        <td>{{ res.id }}</td>
                        <td>{{ res.user_email }}</td>
                        <td>{{ res.service }}</td>
                        <td>{{ res.quantity }}</td>
                        <td>{{ res.status }}</td>
                        <td>{{ res.denied_reason if res.denied_reason else '-' }}</td>
                        <td>{{ res.created_at }}</td>
                        <td>
                            {% if res.status == 'pending' %}
                                <form method="post" class="action-form">
                                    <input type="hidden" name="action" value="approve_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <button type="submit" class="btn-approve">Aprovar</button>
                                </form>
                                <form method="post" class="action-form">
                                    <input type="hidden" name="action" value="deny_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <input type="text" name="reason" placeholder="Motivo" required style="width: 80px;">
                                    <button type="submit" class="btn-deny">Negar</button>
                                </form>
                            {% endif %}
                            <form method="post" class="action-form" onsubmit="return confirm('Tem certeza que deseja deletar esta reserva? O estoque será restaurado.');">
                                <input type="hidden" name="action" value="delete_res">
                                <input type="hidden" name="res_id" value="{{ res.id }}">
                                <button type="submit" class="btn-delete">Deletar</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Fila de Espera</h2>
        <table>
            <thead>
                <tr><th>ID</th><th>Usuário</th><th>Miniatura</th><th>Data Entrada</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for item in waiting_list %}
                    <tr>
                        <td>{{ item.id }}</td>
                        <td>{{ item.user_email }}</td>
                        <td>{{ item.service }}</td>
                        <td>{{ item.created_at }}</td>
                        <td>
                            <form method="post" class="action-form" onsubmit="return confirm('Tem certeza que deseja remover este item da fila?');">
                                <input type="hidden" name="action" value="delete_waiting_item">
                                <input type="hidden" name="item_id" value="{{ item.id }}">
                                <button type="submit" class="btn-delete">Remover</button>
                            </form>
                            {# Optional: Add a button to notify user via simulated log #}
                            <form method="post" class="action-form">
                                <input type="hidden" name="action" value="notify_waiting_user">
                                <input type="hidden" name="item_id" value="{{ item.id }}">
                                <button type="submit" class="btn btn-primary">Notificar</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="form-section">
            <h3>Inserir Nova Miniatura (Adiciona ao Estoque)</h3>
            <form method="post">
                <input type="hidden" name="action" value="insert_miniature">
                <label>Nome da Miniatura: <input type="text" name="service" required></label>
                <label>Marca/Fabricante: <input type="text" name="marca" required></label>
                <label>Observações: <input type="text" name="obs"></label>
                <label>Preço: <input type="number" name="price" step="0.01" required></label>
                <label>Quantidade Inicial: <input type="number" name="quantity" min="0" required></label>
                <label>URL da Imagem: <input type="url" name="image" required></label>
                <label>Previsão de Chegada: <input type="text" name="previsao"></label>
                <button type="submit">Adicionar Miniatura</button>
            </form>
        </div>

        <div class="form-section">
            <h3>Inserir Nova Reserva (Manual)</h3>
            <form method="post">
                <input type="hidden" name="action" value="insert_reservation">
                <label>Usuário:
                    <select name="user_id" required>
                        <option value="">Selecione um Usuário</option>
                        {% for u in all_users_for_select %}
                            <option value="{{ u.id }}">{{ u.name }} ({{ u.email }})</option>
                        {% endfor %}
                    </select>
                </label>
                <label>Miniatura:
                    <select name="service" required>
                        <option value="">Selecione uma Miniatura</option>
                        {% for s in all_services_for_select %}
                            <option value="{{ s.service }}">{{ s.service }} (Estoque: {{ s.quantity }})</option>
                        {% endfor %}
                    </select>
                </label>
                <label>Quantidade: <input type="number" name="quantity" min="1" required></label>
                <label>Status:
                    <select name="status">
                        <option value="pending">Pending</option>
                        <option value="approved">Approved</option>
                        <option value="denied">Denied</option>
                    </select>
                </label>
                <label>Motivo (se negado): <input type="text" name="reason"></label>
                <button type="submit">Criar Reserva</button>
            </form>
        </div>
    </div>
    <footer>
        <p>&copy; {{ datetime.now().year }} JG MINIS. Todos os direitos reservados.</p>
    </footer>
</body>
</html>
'''

# --- 9. Flask Routes ---

@app.route('/')
def index():
    thumbnails = load_thumbnails()
    return render_template_string(INDEX_HTML, thumbnails=thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, datetime=datetime)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')

        if not name:
            flash('Nome é obrigatório.', 'error')
            return render_template_string(REGISTER_HTML, request=request, datetime=datetime)
        if not is_valid_email(email):
            flash('Email inválido.', 'error')
            return render_template_string(REGISTER_HTML, request=request, datetime=datetime)
        if not is_valid_phone(phone):
            flash('Telefone inválido (apenas números, 10 ou 11 dígitos).', 'error')
            return render_template_string(REGISTER_HTML, request=request, datetime=datetime)
        if len(password) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML, request=request, datetime=datetime)
        
        hashed_pw = generate_password_hash(password)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)', (name, email, phone, hashed_pw))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Este email já está cadastrado.', 'error')
        except Exception as e:
            logging.error(f'Erro ao registrar usuário: {e}')
            flash('Ocorreu um erro ao registrar. Tente novamente.', 'error')
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML, request=request, datetime=datetime)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT id, password, role, email FROM users WHERE email = ?', (email,))
        user_data = c.fetchone()
        conn.close()

        if user_data and check_password_hash(user_data[1], password):
            session['user_id'] = user_data[0]
            session['role'] = user_data[2]
            session['email'] = user_data[3]
            logging.info(f'Login bem-sucedido para {email}')
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Email ou senha inválidos.', 'error')
    return render_template_string(LOGIN_HTML, request=request, datetime=datetime)

@app.route('/logout')
def logout():
    logging.info(f'Logout de {session.get("email", "usuário desconhecido")}')
    session.clear()
    flash('Você foi desconectado.', 'success')
    return redirect(url_for('index'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Você precisa estar logado para acessar esta página.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Get user info
    c.execute('SELECT name, email, phone, data_cadastro FROM users WHERE id = ?', (session['user_id'],))
    user = c.fetchone()
    user_dict = {'name': user[0], 'email': user[1], 'phone': user[2], 'data_cadastro': user[3]}

    # Get user's reservations
    c.execute('SELECT service, quantity, status, created_at FROM reservations WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
    reservations = [{'service': r[0], 'quantity': r[1], 'status': r[2], 'created_at': r[3]} for r in c.fetchall()]
    
    conn.close()
    return render_template_string(PROFILE_HTML, user=user_dict, reservations=reservations, session=session, url_for=url_for, datetime=datetime)

@app.route('/reserve_single/<service>', methods=['GET', 'POST'])
def reserve_single(service):
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Get thumbnail details
    thumbnails = load_thumbnails()
    thumb = next((t for t in thumbnails if t['service'] == service), None)
    if not thumb:
        flash('Miniatura não encontrada.', 'error')
        conn.close()
        return redirect(url_for('index'))
    
    # Get current stock from DB
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service,))
    stock_row = c.fetchone()
    current_stock = stock_row[0] if stock_row else 0
    
    if request.method == 'POST':
        quantity_to_reserve = int(request.form.get('quantity', 0))
        
        if quantity_to_reserve <= 0:
            flash('A quantidade deve ser pelo menos 1.', 'error')
        elif quantity_to_reserve > current_stock:
            flash(f'Quantidade indisponível. Restam apenas {current_stock} unidades.', 'error')
        else:
            try:
                c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)',
                          (session['user_id'], service, quantity_to_reserve))
                c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity_to_reserve, service))
                conn.commit()
                flash(f'{quantity_to_reserve} unidade(s) de "{service}" reservada(s) com sucesso!', 'success')
                return redirect(url_for('profile'))
            except Exception as e:
                logging.error(f'Erro ao criar reserva individual: {e}')
                flash('Ocorreu um erro ao processar sua reserva. Tente novamente.', 'error')
        
    conn.close()
    return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, current_stock=current_stock, 
                                  logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, session=session, url_for=url_for, datetime=datetime)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar.', 'error')
        return redirect(url_for('login'))
    
    all_thumbnails = load_thumbnails()
    filtered_thumbnails = all_thumbnails

    # --- Apply Filters ---
    available_only = request.args.get('available') == '1'
    order_by = request.args.get('order_by', '')
    marca_filter = request.args.get('marca', '').strip().lower()

    if available_only:
        filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
    
    if marca_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]

    # --- Apply Sorting ---
    if order_by == 'service_asc':
        filtered_thumbnails.sort(key=lambda x: x['service'])
    elif order_by == 'service_desc':
        filtered_thumbnails.sort(key=lambda x: x['service'], reverse=True)
    elif order_by == 'price_asc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')))
    elif order_by == 'price_desc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')), reverse=True)
    elif order_by == 'previsao_asc':
        # Sort by date, empty strings last
        filtered_thumbnails.sort(key=lambda x: (x['previsao'] == '', x['previsao']))
    elif order_by == 'previsao_desc':
        # Sort by date, empty strings first
        filtered_thumbnails.sort(key=lambda x: (x['previsao'] != '', x['previsao']), reverse=True)

    if request.method == 'POST':
        services_selected = request.form.getlist('services')
        reservations_made = 0
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        for service_name in services_selected:
            quantity_key = f'quantity_{service_name}'
            quantity_to_reserve = int(request.form.get(quantity_key, 0))
            
            if quantity_to_reserve <= 0:
                flash(f'Quantidade inválida para "{service_name}".', 'error')
                continue

            # Get current stock from DB
            c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name,))
            stock_row = c.fetchone()
            current_stock = stock_row[0] if stock_row else 0

            if quantity_to_reserve > current_stock:
                flash(f'Estoque insuficiente para "{service_name}". Disponível: {current_stock}.', 'error')
            else:
                try:
                    c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)',
                              (session['user_id'], service_name, quantity_to_reserve))
                    c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity_to_reserve, service_name))
                    conn.commit()
                    reservations_made += 1
                except Exception as e:
                    logging.error(f'Erro ao criar reserva múltipla para {service_name}: {e}')
                    flash(f'Erro ao reservar "{service_name}".', 'error')
        
        conn.close()
        if reservations_made > 0:
            flash(f'{reservations_made} reserva(s) realizada(s) com sucesso!', 'success')
            return redirect(url_for('profile'))
        else:
            flash('Nenhuma reserva foi feita.', 'error')

    return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL, 
                                  whatsapp_number=WHATSAPP_NUMBER, session=session, url_for=url_for, datetime=datetime, request=request)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado. Você não tem permissão de administrador.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    if request.method == 'POST':
        action = request.form.get('action')
        
        # --- User Management Actions ---
        if action == 'promote_user':
            user_id = request.form.get('user_id')
            c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
            flash(f'Usuário {user_id} promovido a admin.', 'success')
        elif action == 'demote_user':
            user_id = request.form.get('user_id')
            c.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
            flash(f'Usuário {user_id} rebaixado para user.', 'success')
        elif action == 'delete_user':
            user_id = request.form.get('user_id')
            # Delete associated reservations and waiting list entries first
            c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,))
            c.execute('DELETE FROM waiting_list WHERE user_id = ?', (user_id,))
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            flash(f'Usuário {user_id} e suas reservas/fila deletados.', 'success')
        
        # --- Reservation Management Actions ---
        elif action == 'approve_res':
            res_id = request.form.get('res_id')
            c.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
            flash(f'Reserva {res_id} aprovada.', 'success')
        elif action == 'deny_res':
            res_id = request.form.get('res_id')
            reason = request.form.get('reason', 'Motivo não especificado')
            c.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
            flash(f'Reserva {res_id} negada.', 'success')
        elif action == 'delete_res':
            res_id = request.form.get('res_id')
            # Get quantity from reservation to restore stock
            c.execute('SELECT service, quantity FROM reservations WHERE id = ?', (res_id,))
            res_data = c.fetchone()
            if res_data:
                service, quantity = res_data
                c.execute('UPDATE stock SET quantity = quantity + ? WHERE service = ?', (quantity, service))
                flash(f'Estoque de "{service}" restaurado em {quantity} unidades.', 'success')
            c.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
            flash(f'Reserva {res_id} deletada.', 'success')
        
        # --- Miniature Management Actions ---
        elif action == 'insert_miniature':
            service = request.form.get('service', '').strip()
            marca = request.form.get('marca', '').strip()
            obs = request.form.get('obs', '').strip()
            price = float(request.form.get('price', 0))
            quantity = int(request.form.get('quantity', 0))
            image = request.form.get('image', '').strip()
            previsao = request.form.get('previsao', '').strip()

            if not service or not marca or price <= 0 or quantity < 0 or not image:
                flash('Preencha todos os campos obrigatórios para a miniatura.', 'error')
            else:
                try:
                    # Insert/Update into stock DB
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, quantity))
                    # Note: This does NOT update the Google Sheet. Sheet is primary source for other details.
                    flash(f'Miniatura "{service}" adicionada/atualizada no estoque.', 'success')
                except Exception as e:
                    logging.error(f'Erro ao inserir miniatura: {e}')
                    flash('Erro ao adicionar miniatura. Tente novamente.', 'error')

        # --- Manual Reservation Insertion ---
        elif action == 'insert_reservation':
            user_id = request.form.get('user_id')
            service = request.form.get('service')
            quantity = int(request.form.get('quantity', 0))
            status = request.form.get('status', 'pending')
            reason = request.form.get('reason', '').strip()

            if not user_id or not service or quantity <= 0:
                flash('Preencha todos os campos obrigatórios para a reserva manual.', 'error')
            else:
                try:
                    # Check stock if approved
                    if status == 'approved':
                        c.execute('SELECT quantity FROM stock WHERE service = ?', (service,))
                        current_stock = c.fetchone()
                        if not current_stock or current_stock[0] < quantity:
                            flash(f'Estoque insuficiente para "{service}" para aprovar a reserva.', 'error')
                            conn.rollback() # Rollback any changes
                            conn.close()
                            return redirect(url_for('admin'))
                        c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service))
                    
                    c.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)',
                              (user_id, service, quantity, status, reason if status == 'denied' else None))
                    conn.commit()
                    flash(f'Reserva manual para "{service}" criada com sucesso!', 'success')
                except Exception as e:
                    logging.error(f'Erro ao inserir reserva manual: {e}')
                    flash('Erro ao criar reserva manual. Tente novamente.', 'error')

        # --- Stock Synchronization ---
        elif action == 'sync_stock':
            if sheet:
                try:
                    records = sheet.get_all_records()
                    for record in records:
                        service = record.get('NOME DA MINIATURA', '')
                        qty = record.get('QUANTIDADE DISPONIVEL', 0)
                        if service:
                            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
                    conn.commit()
                    flash('Estoque sincronizado da planilha para o DB!', 'success')
                except Exception as e:
                    logging.error(f'Erro na sincronização do estoque: {e}')
                    flash('Erro na sincronização do estoque. Verifique a planilha e as permissões.', 'error')
            else:
                flash('Integração com Google Sheets não configurada.', 'error')
        
        # --- Waiting List Actions ---
        elif action == 'delete_waiting_item':
            item_id = request.form.get('item_id')
            c.execute('DELETE FROM waiting_list WHERE id = ?', (item_id,))
            flash(f'Item da fila de espera {item_id} removido.', 'success')
        elif action == 'notify_waiting_user':
            item_id = request.form.get('item_id')
            c.execute('SELECT u.email, wl.service FROM waiting_list wl JOIN users u ON wl.user_id = u.id WHERE wl.id = ?', (item_id,))
            notification_data = c.fetchone()
            if notification_data:
                user_email, service_name = notification_data
                logging.info(f'Notificação simulada para {user_email}: Miniatura "{service_name}" está disponível!')
                flash(f'Notificação simulada enviada para {user_email} sobre "{service_name}".', 'success')
            else:
                flash('Item da fila de espera não encontrado para notificação.', 'error')

        conn.commit() # Commit any pending changes
        return redirect(url_for('admin')) # Redirect to clear POST data

    # --- Admin Dashboard Data Loading (GET request) ---
    
    # Stats
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_res_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations')
    total_res_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM waiting_list')
    waiting_list_count = c.fetchone()[0]
    stats = {'users': users_count, 'pending': pending_res_count, 'total_res': total_res_count, 'waiting': waiting_list_count}

    # User Filtering
    user_email_filter = request.args.get('user_email_filter', '').strip().lower()
    user_role_filter = request.args.get('user_role_filter', '').strip()
    user_query = 'SELECT id, name, email, phone, role, data_cadastro FROM users WHERE 1=1'
    user_params = []
    if user_email_filter:
        user_query += ' AND email LIKE ?'
        user_params.append(f'%{user_email_filter}%')
    if user_role_filter:
        user_query += ' AND role = ?'
        user_params.append(user_role_filter)
    c.execute(user_query, user_params)
    users = [{'id': r[0], 'name': r[1], 'email': r[2], 'phone': r[3], 'role': r[4], 'data_cadastro': r[5]} for r in c.fetchall()]

    # Reservation Filtering
    res_search_filter = request.args.get('res_search_filter', '').strip().lower()
    res_status_filter = request.args.get('res_status_filter', '').strip()
    res_query = '''
        SELECT r.id, u.email, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
        FROM reservations r JOIN users u ON r.user_id = u.id WHERE 1=1
    '''
    res_params = []
    if res_search_filter:
        res_query += ' AND (u.email LIKE ? OR r.service LIKE ?)'
        res_params.extend([f'%{res_search_filter}%', f'%{res_search_filter}%'])
    if res_status_filter:
        res_query += ' AND r.status = ?'
        res_params.append(res_status_filter)
    res_query += ' ORDER BY r.created_at DESC'
    c.execute(res_query, res_params)
    reservations = [{'id': r[0], 'user_email': r[1], 'service': r[2], 'quantity': r[3], 
                     'status': r[4], 'denied_reason': r[5], 'created_at': r[6]} for r in c.fetchall()]

    # Waiting List
    c.execute('SELECT wl.id, u.email, wl.service, wl.created_at FROM waiting_list wl JOIN users u ON wl.user_id = u.id ORDER BY wl.created_at ASC')
    waiting_list = [{'id': r[0], 'user_email': r[1], 'service': r[2], 'created_at': r[3]} for r in c.fetchall()]

    # Data for "Insert New Reservation" form
    all_users_for_select = c.execute('SELECT id, name, email FROM users ORDER BY name').fetchall()
    all_users_for_select = [{'id': r[0], 'name': r[1], 'email': r[2]} for r in all_users_for_select]
    
    all_services_for_select = c.execute('SELECT service, quantity FROM stock ORDER BY service').fetchall()
    all_services_for_select = [{'service': r[0], 'quantity': r[1]} for r in all_services_for_select]

    conn.close()
    return render_template_string(ADMIN_HTML, stats=stats, users=users, reservations=reservations, 
                                  waiting_list=waiting_list, all_users_for_select=all_users_for_select, 
                                  all_services_for_select=all_services_for_select, session=session, url_for=url_for, datetime=datetime, request=request)

@app.route('/add_to_waiting_list', methods=['POST'])
def add_to_waiting_list():
    if 'user_id' not in session:
        flash('Você precisa estar logado para entrar na fila de espera.', 'error')
        return redirect(url_for('login'))
    
    service = request.form.get('service')
    if not service:
        flash('Miniatura não especificada.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    try:
        # Check if user is already in waiting list for this service
        c.execute('SELECT id FROM waiting_list WHERE user_id = ? AND service = ?', (session['user_id'], service))
        if c.fetchone():
            flash(f'Você já está na fila de espera para "{service}".', 'info')
        else:
            c.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], service))
            conn.commit()
            flash(f'Você foi adicionado à fila de espera para "{service}"!', 'success')
    except Exception as e:
        logging.error(f'Erro ao adicionar à fila de espera: {e}')
        flash('Ocorreu um erro ao adicionar à fila de espera. Tente novamente.', 'error')
    finally:
        conn.close()
    
    return redirect(url_for('index')) # Redirect back to home or relevant page

@app.route('/backup_db')
def backup_db():
    if session.get('role') != 'admin':
        abort(403) # Forbidden
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # Fetch all data from tables
    users = c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users').fetchall()
    reservations = c.execute('SELECT id, user_id, service, quantity, status, approved_by, denied_reason, created_at FROM reservations').fetchall()
    stock = c.execute('SELECT id, service, quantity, last_sync FROM stock').fetchall()
    waiting_list = c.execute('SELECT id, user_id, service, created_at FROM waiting_list').fetchall()
    
    conn.close()

    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(zip(['id', 'name', 'email', 'phone', 'role', 'data_cadastro'], row)) for row in users],
        'reservations': [dict(zip(['id', 'user_id', 'service', 'quantity', 'status', 'approved_by', 'denied_reason', 'created_at'], row)) for row in reservations],
        'stock': [dict(zip(['id', 'service', 'quantity', 'last_sync'], row)) for row in stock],
        'waiting_list': [dict(zip(['id', 'user_id', 'service', 'created_at'], row)) for row in waiting_list]
    }
    
    json_data = json.dumps(backup_data, indent=4, ensure_ascii=False)
    
    buffer = io.BytesIO()
    buffer.write(json_data.encode('utf-8'))
    buffer.seek(0)
    
    filename = f"jgminis_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/json')

@app.route('/export_csv')
def export_csv():
    if session.get('role') != 'admin':
        abort(403) # Forbidden
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Fetch reservations with user info
    c.execute('''
        SELECT r.id, u.name, u.email, u.phone, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
        FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC
    ''')
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(['ID Reserva', 'Nome Usuário', 'Email Usuário', 'Telefone Usuário', 'Miniatura', 'Quantidade', 'Status', 'Motivo Negação', 'Data Criação'])
    
    # Write data rows
    writer.writerows(rows)
    
    buffer = io.BytesIO()
    buffer.write(output.getvalue().encode('utf-8'))
    buffer.seek(0)
    
    filename = f"jgminis_reservas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='text/csv')

@app.route('/favicon.ico')
def favicon():
    # Return a 204 No Content response for favicon requests
    return '', 204

# --- 10. Error Handlers ---
@app.errorhandler(404)
def page_not_found(e):
    flash('A página que você tentou acessar não foi encontrada.', 'error')
    return redirect(url_for('index')), 404

@app.errorhandler(500)
def internal_server_error(e):
    logging.error(f'Erro interno do servidor: {e}')
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return redirect(url_for('index')), 500

# --- 11. Run Application ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = '0.0.0.0'
    app.run(host=host, port=port, debug=False) # Set debug=True for development
