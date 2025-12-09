import os
import re
import json
import csv
import io
import logging
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, send_file, abort, make_response
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
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')  # Just numbers, no + or spaces
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
    return phone.isdigit() and 10 <= len(phone) <= 11  # 10-11 digits for Brazilian numbers

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
                service = record.get('NOME DA MINIATURA', '').strip().lower()  # Normalize: trim and lower
                qty = record.get('QUANTIDADE DISPONIVEL', 0)
                if service:
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
            logging.info('Estoque inicial sincronizado da planilha para o DB (case-insensitive)')
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
    
    # Get stock quantities from DB (service in lower case)
    c.execute("SELECT service, quantity FROM stock ORDER BY service")
    # Store service names in lowercase for case-insensitive lookup
    stock_data = {row[0]: row[1] for row in c.fetchall()}
    
    # Get other details from Google Sheet
    if sheet:
        try:
            records = sheet.get_all_records()
            if not records:
                logging.warning("Planilha vazia - thumbnails fallback")
                return [{'service': 'Fallback', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': 'Adicione dados na planilha', 'marca': '', 'previsao': ''}]
            
            for record in records: # Process all records
                service_raw = record.get('NOME DA MINIATURA', '').strip()
                if not service_raw: continue # Skip empty service names
                
                service_lower = service_raw.lower()  # Normalize for lookup
                
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
                # Use normalized service name for lookup
                quantity = stock_data.get(service_lower, 0) 
                logging.info(f'Stock lookup for "{service_raw}" (normalized: "{service_lower}"): found {quantity} or 0')
                
                thumbnails.append({
                    'service': service_raw,  # Original case for display
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
    <title>JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
        header { background-color: #004085; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        header img { height: 60px; vertical-align: middle; margin-right: 15px; }
        header h1 { display: inline-block; margin: 0; font-size: 2em; }
        nav { background-color: #e9ecef; padding: 10px 20px; text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        nav a { color: #007bff; text-decoration: none; margin: 0 15px; font-weight: bold; transition: color 0.3s; font-size: 1.2em; } /* Ajuste de fonte aqui */
        nav a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { padding: 10px 20px; margin-top: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 25px; padding: 25px; max-width: 1200px; margin: 20px auto; }
        .thumbnail { background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; text-align: center; transition: transform 0.3s ease, box-shadow 0.3s ease; position: relative; }
        .thumbnail:hover { transform: translateY(-5px); box-shadow: 0 6px 16px rgba(0,0,0,0.12); }
        .thumbnail img { width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #eee; }
        .thumbnail.esgotado { opacity: 0.7; }
        .thumbnail.esgotado img { filter: grayscale(100%); }
        .esgotado-tag { position: absolute; top: 10px; right: 10px; background-color: #dc3545; color: white; padding: 5px 10px; border-radius: 20px; font-size: 0.8em; font-weight: bold; z-index: 10; }
        .thumbnail-content { padding: 15px; }
        .thumbnail h3 { font-size: 1.3em; color: #007bff; margin-top: 0; margin-bottom: 8px; }
        .thumbnail p { font-size: 0.95em; color: #555; margin-bottom: 5px; line-height: 1.4; }
        .thumbnail .price { font-size: 1.1em; font-weight: bold; color: #28a745; margin-top: 10px; }
        .thumbnail .quantity { font-size: 0.9em; color: #6c757d; margin-bottom: 15px; }
        .action-buttons { display: flex; justify-content: center; gap: 10px; margin-top: 15px; flex-wrap: wrap; }
        .btn { display: inline-block; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; transition: background-color 0.3s ease, color 0.3s ease; }
        .btn-reserve { background-color: #28a745; color: white; border: none; }
        .btn-reserve:hover { background-color: #218838; }
        .btn-waiting { background-color: #ffc107; color: #212529; border: none; } /* Amarelo */
        .btn-waiting:hover { background-color: #e0a800; }
        .btn-contact { background-color: #25D366; color: white; border: none; } /* Verde */
        .btn-contact:hover { background-color: #1DA851; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 20px; margin-top: 40px; font-size: 0.9em; }
        @media (max-width: 768px) {
            header h1 { font-size: 1.8em; }
            nav a { margin: 0 10px; font-size: 1em; }
            .grid-container { grid-template-columns: 1fr; padding: 15px; }
            .thumbnail img { height: 150px; }
            .action-buttons { flex-direction: column; }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS">
        <h1>JG Minis Portal de Reservas</h1> <!-- Título ajustado -->
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
            <div class="thumbnail{% if thumb.quantity == 0 %} esgotado{% endif %}">
                {% if thumb.quantity == 0 %}<div class="esgotado-tag">ESGOTADO</div>{% endif %}
                <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                <div class="thumbnail-content">
                    <h3>{{ thumb.service }}</h3>
                    <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
                    <p class="price">R$ {{ thumb.price }}</p>
                    <p class="quantity">Disponível: {{ thumb.quantity }}</p>
                    <div class="action-buttons">
                        {% if thumb.quantity > 0 %}
                            <a href="{{ url_for('reserve_single', service=thumb.service) }}" class="btn btn-reserve">Reservar Agora</a>
                        {% else %}
                            <a href="{{ url_for('add_waiting_list', service=thumb.service) }}" class="btn btn-waiting">Fila de Espera</a>
                            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="btn btn-contact" target="_blank">Entrar em Contato</a>
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
    <title>Registrar - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .register-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        form { display: flex; flex-direction: column; gap: 15px; }
        label { text-align: left; font-weight: bold; color: #555; }
        input[type="text"], input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 1em; }
        button { padding: 12px; background-color: #28a745; color: white; border: none; border-radius: 5px; font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease; margin-top: 15px; }
        button:hover { background-color: #218838; }
        .flash-error { color: #dc3545; margin-top: 10px; font-size: 0.9em; }
        a { color: #007bff; text-decoration: none; margin-top: 20px; display: block; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="register-container">
        <h1>Registrar</h1>
        <form method="post">
            <label for="name">Nome:</label>
            <input type="text" id="name" name="name" required>
            <label for="email">Email:</label>
            <input type="email" id="email" name="email" required>
            <label for="phone">Telefone (apenas números):</label>
            <input type="text" id="phone" name="phone" required pattern="[0-9]{10,11}" title="Telefone deve conter 10 ou 11 dígitos numéricos">
            <label for="password">Senha (mín. 6 caracteres):</label>
            <input type="password" id="password" name="password" required minlength="6">
            <button type="submit">Registrar</button>
        </form>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <p class="flash-{{ category }}">{{ message }}</p>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <a href="{{ url_for('login') }}">Já tem conta? Fazer Login</a>
        <a href="{{ url_for('index') }}">Voltar ao Home</a>
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
    <title>Login - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        form { display: flex; flex-direction: column; gap: 15px; }
        label { text-align: left; font-weight: bold; color: #555; }
        input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 1em; }
        button { padding: 12px; background-color: #007bff; color: white; border: none; border-radius: 5px; font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease; margin-top: 15px; }
        button:hover { background-color: #0056b3; }
        .flash-error { color: #dc3545; margin-top: 10px; font-size: 0.9em; }
        a { color: #007bff; text-decoration: none; margin-top: 20px; display: block; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Login</h1>
        <form method="post">
            <label for="email">Email:</label>
            <input type="email" id="email" name="email" required>
            <label for="password">Senha:</label>
            <input type="password" id="password" name="password" required>
            <button type="submit">Entrar</button>
        </form>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <p class="flash-{{ category }}">{{ message }}</p>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <a href="{{ url_for('register') }}">Não tem conta? Registrar</a>
        <a href="{{ url_for('index') }}">Voltar ao Home</a>
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
    <title>Reservar {{ thumb.service }} - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
        .reserve-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 500px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        img { max-width: 250px; height: auto; margin-bottom: 20px; border: 1px solid #eee; border-radius: 5px; }
        form { display: flex; flex-direction: column; gap: 15px; margin-top: 20px; }
        label { text-align: left; font-weight: bold; color: #555; }
        input[type="number"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 1em; text-align: center; }
        button { padding: 12px; background-color: #28a745; color: white; border: none; border-radius: 5px; font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease; margin-top: 15px; }
        button:hover { background-color: #218838; }
        .btn-whatsapp { padding: 12px; background-color: #25D366; color: white; border: none; border-radius: 5px; font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease; text-decoration: none; display: inline-block; margin-top: 15px; }
        .btn-whatsapp:hover { background-color: #1DA851; }
        .flash-error { color: #dc3545; margin-top: 10px; font-size: 0.9em; }
        a { color: #007bff; text-decoration: none; margin-top: 20px; display: block; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="reserve-container">
        <h1>Reservar {{ thumb.service }}</h1>
        <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';" style="width: 200px; height: auto;"> <!-- Imagem da miniatura -->
        {% if max_quantity > 0 %}
            <form method="post">
                <label>Quantidade disponível: {{ max_quantity }}</label>
                <label for="quantity">Quantidade a reservar:</label> 
                <input type="number" id="quantity" name="quantity" min="1" max="{{ max_quantity }}" value="1" required>
                <button type="submit">Confirmar Reserva</button>
            </form>
        {% else %}
            <p>Estoque indisponível para {{ thumb.service }}.</p>
            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="btn-whatsapp" target="_blank">Entrar em Contato</a>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <p class="flash-{{ category }}">{{ message }}</p>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <a href="{{ url_for('index') }}">Voltar ao Home</a>
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
    <title>Reservar Múltiplas Miniaturas - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; padding: 20px; }
        .container { max-width: 900px; margin: 20px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        .filters-form { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 30px; padding: 15px; background-color: #e9ecef; border-radius: 8px; }
        .filters-form label { font-weight: bold; color: #555; }
        .filters-form input[type="checkbox"] { margin-right: 5px; }
        .filters-form input[type="text"], .filters-form select { padding: 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9em; }
        .filters-form button { padding: 10px 20px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.3s ease; }
        .filters-form button:hover { background-color: #0056b3; }
        .thumbnails-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .thumbnail-item { border: 1px solid #eee; border-radius: 8px; padding: 15px; text-align: center; background-color: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
        .thumbnail-item img { max-width: 100%; height: 120px; object-fit: cover; border-radius: 4px; margin-bottom: 10px; }
        .thumbnail-item h3 { font-size: 1.1em; color: #007bff; margin-top: 0; margin-bottom: 8px; }
        .thumbnail-item p { font-size: 0.9em; color: #555; margin-bottom: 5px; }
        .thumbnail-item input[type="checkbox"] { margin-right: 8px; transform: scale(1.2); }
        .thumbnail-item input[type="number"] { width: 60px; padding: 5px; border: 1px solid #ccc; border-radius: 4px; text-align: center; font-size: 0.9em; margin-left: 5px; }
        .submit-button { padding: 12px 25px; background-color: #28a745; color: white; border: none; border-radius: 5px; font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease; display: block; width: fit-content; margin: 0 auto; }
        .submit-button:hover { background-color: #218838; }
        .flash-messages { margin-top: 20px; text-align: center; }
        .flash-error { color: #dc3545; }
        .flash-success { color: #28a745; }
        .back-link { display: block; text-align: center; margin-top: 30px; color: #007bff; text-decoration: none; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Reservar Múltiplas Miniaturas</h1>
        <form method="get" class="filters-form">
            <label>
                <input type="checkbox" name="available" value="1" {% if request.args.get('available') == '1' %}checked{% endif %}> Disponíveis
            </label>
            <label>
                Ordenar por:
                <select name="order_by">
                    <option value="">Nenhum</option>
                    <option value="service_asc" {% if request.args.get('order_by') == 'service_asc' %}selected{% endif %}>Nome (A-Z)</option>
                    <option value="service_desc" {% if request.args.get('order_by') == 'service_desc' %}selected{% endif %}>Nome (Z-A)</option>
                    <option value="price_asc" {% if request.args.get('order_by') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                    <option value="price_desc" {% if request.args.get('order_by') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                </select>
            </label>
            <label>
                Previsão de Chegada:
                <input type="text" name="previsao_search" value="{{ request.args.get('previsao_search', '') }}">
            </label>
            <label>
                Marca:
                <input type="text" name="marca_search" value="{{ request.args.get('marca_search', '') }}">
            </label>
            <button type="submit">Filtrar</button>
        </form>

        <form method="post">
            <div class="thumbnails-grid">
                {% for thumb in thumbnails %}
                    <div class="thumbnail-item">
                        <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                        <h3>{{ thumb.service }}</h3>
                        <p>{{ thumb.marca }}</p>
                        <p>R$ {{ thumb.price }}</p>
                        <p>Disponível: {{ thumb.quantity }}</p>
                        {% if thumb.quantity > 0 %}
                            <label>
                                <input type="checkbox" name="services" value="{{ thumb.service }}">
                                Qtd: <input type="number" name="quantity_{{ thumb.service }}" min="1" max="{{ thumb.quantity }}" value="1">
                            </label>
                        {% else %}
                            <p style="color: #dc3545; font-weight: bold;">ESGOTADO</p>
                            <a href="{{ url_for('add_waiting_list', service=thumb.service) }}" class="btn btn-waiting" style="font-size: 0.9em; padding: 8px 12px;">Fila de Espera</a>
                        {% endif %}
                    </div>
                {% endfor %}
            </div>
            <button type="submit" class="submit-button">Reservar Selecionadas</button>
        </form>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="flash-messages">
                    {% for category, message in messages %}
                        <p class="flash-{{ category }}">{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Home</a>
    </div>
</body>
</html>
'''

PROFILE_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meu Perfil - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; padding: 20px; }
        .container { max-width: 800px; margin: 20px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        p { margin-bottom: 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #f8f9fa; color: #333; }
        .back-link { display: block; text-align: center; margin-top: 30px; color: #007bff; text-decoration: none; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Meu Perfil</h1>
        <h2>Dados do Usuário</h2>
        <p><strong>Nome:</strong> {{ user.name }}</p>
        <p><strong>Email:</strong> {{ user.email }}</p>
        <p><strong>Telefone:</strong> {{ user.phone }}</p>
        <p><strong>Tipo de Usuário:</strong> {{ user.role }}</p>
        <p><strong>Membro Desde:</strong> {{ user.data_cadastro }}</p>

        <h2>Minhas Reservas</h2>
        {% if reservations %}
            <table>
                <thead>
                    <tr>
                        <th>Serviço</th>
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
            <p>Você ainda não fez nenhuma reserva.</p>
        {% endif %}

        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Home</a>
    </div>
</body>
</html>
'''

ADMIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Admin - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; padding: 20px; }
        .container { max-width: 1200px; margin: 20px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; text-align: center; }
        .stat-box { background-color: #e9ecef; padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
        .stat-box h3 { color: #333; margin-top: 0; font-size: 1.2em; }
        .stat-box p { font-size: 1.5em; font-weight: bold; color: #004085; margin: 0; }
        .filters-form, .action-form { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; padding: 15px; background-color: #f8f9fa; border-radius: 8px; }
        .filters-form label, .action-form label { font-weight: bold; color: #555; }
        .filters-form input[type="text"], .filters-form select, .action-form input[type="text"], .action-form select, .action-form input[type="number"], .action-form input[type="url"] { padding: 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9em; flex-grow: 1; }
        .filters-form button, .action-form button { padding: 10px 15px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.3s ease; }
        .filters-form button:hover, .action-form button:hover { background-color: #0056b3; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; font-size: 0.9em; }
        th { background-color: #f8f9fa; color: #333; }
        .action-buttons button { padding: 6px 10px; margin-right: 5px; border-radius: 4px; cursor: pointer; font-size: 0.85em; }
        .btn-promote { background-color: #28a745; color: white; border: none; }
        .btn-promote:hover { background-color: #218838; }
        .btn-demote { background-color: #ffc107; color: #212529; border: none; }
        .btn-demote:hover { background-color: #e0a800; }
        .btn-delete { background-color: #dc3545; color: white; border: none; }
        .btn-delete:hover { background-color: #c82333; }
        .btn-approve { background-color: #28a745; color: white; border: none; }
        .btn-approve:hover { background-color: #218838; }
        .btn-deny { background-color: #ffc107; color: #212529; border: none; }
        .btn-deny:hover { background-color: #e0a800; }
        .btn-backup { background-color: #17a2b8; color: white; border: none; margin-right: 10px; }
        .btn-backup:hover { background-color: #138496; }
        .btn-sync { background-color: #6c757d; color: white; border: none; }
        .btn-sync:hover { background-color: #5a6268; }
        .flash-messages { margin-top: 20px; text-align: center; }
        .flash-error { color: #dc3545; }
        .flash-success { color: #28a745; }
        .back-link { display: block; text-align: center; margin-top: 30px; color: #007bff; text-decoration: none; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Painel Admin</h1>

        <div class="stats-grid">
            <div class="stat-box">
                <h3>Usuários Registrados</h3>
                <p>{{ stats.users_count }}</p>
            </div>
            <div class="stat-box">
                <h3>Reservas Pendentes</h3>
                <p>{{ stats.pending_count }}</p>
            </div>
            <div class="stat-box">
                <h3>Total de Reservas</h3>
                <p>{{ stats.total_reservations }}</p>
            </div>
        </div>

        <h2>Ferramentas Administrativas</h2>
        <div class="action-form" style="justify-content: center;">
            <button onclick="location.href='{{ url_for('backup') }}'" class="btn-backup">Backup JSON</button>
            <button onclick="location.href='{{ url_for('export_csv') }}'" class="btn-backup">Exportar CSV</button>
            <form method="post" style="display:inline-flex; gap: 10px;">
                <input type="hidden" name="action" value="sync_stock">
                <button type="submit" class="btn-sync">Sincronizar Estoque</button>
            </form>
        </div>

        <h2>Gerenciar Usuários</h2>
        <form method="get" class="filters-form">
            <label>Email: <input type="text" name="email_filter" value="{{ request.args.get('email_filter', '') }}"></label>
            <label>Role: 
                <select name="role_filter">
                    <option value="">Todos</option>
                    <option value="user" {% if request.args.get('role_filter') == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if request.args.get('role_filter') == 'admin' %}selected{% endif %}>Admin</option>
                </select>
            </label>
            <button type="submit">Filtrar Usuários</button>
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
                        <td class="action-buttons">
                            {% if user.role != 'admin' %}
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="promote">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-promote">Promover</button>
                                </form>
                            {% endif %}
                            {% if user.role == 'admin' and user.id != session.get('user_id') %} {# Admin não pode rebaixar a si mesmo #}
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="demote">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-demote">Rebaixar</button>
                                </form>
                            {% endif %}
                            {% if user.id != session.get('user_id') %} {# Usuário não pode deletar a si mesmo #}
                                <form method="post" style="display:inline;" onsubmit="return confirm('Tem certeza que deseja deletar este usuário? Todas as reservas associadas serão removidas.');">
                                    <input type="hidden" name="action" value="delete_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-delete">Deletar</button>
                                </form>
                            {% endif %}
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Gerenciar Reservas</h2>
        <form method="get" class="filters-form">
            <label>Serviço: <input type="text" name="service_filter" value="{{ request.args.get('service_filter', '') }}"></label>
            <label>Status: 
                <select name="status_filter">
                    <option value="">Todos</option>
                    <option value="pending" {% if request.args.get('status_filter') == 'pending' %}selected{% endif %}>Pendente</option>
                    <option value="approved" {% if request.args.get('status_filter') == 'approved' %}selected{% endif %}>Aprovado</option>
                    <option value="denied" {% if request.args.get('status_filter') == 'denied' %}selected{% endif %}>Negado</option>
                </select>
            </label>
            <button type="submit">Filtrar Reservas</button>
        </form>
        <table>
            <thead>
                <tr><th>ID</th><th>Usuário</th><th>Serviço</th><th>Quantidade</th><th>Status</th><th>Data</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for res in all_reservations %}
                    <tr>
                        <td>{{ res.id }}</td>
                        <td>{{ res.user_name }} ({{ res.user_email }})</td>
                        <td>{{ res.service }}</td>
                        <td>{{ res.quantity }}</td>
                        <td>{{ res.status }} {% if res.denied_reason %}({{ res.denied_reason }}){% endif %}</td>
                        <td>{{ res.created_at }}</td>
                        <td class="action-buttons">
                            {% if res.status == 'pending' %}
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="approve">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <button type="submit" class="btn-approve">Aprovar</button>
                                </form>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="deny">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <input type="text" name="reason" placeholder="Motivo" required style="width: 80px;">
                                    <button type="submit" class="btn-deny">Negar</button>
                                </form>
                            {% endif %}
                            <form method="post" style="display:inline;" onsubmit="return confirm('Tem certeza que deseja deletar esta reserva? O estoque será restaurado.');">
                                <input type="hidden" name="action" value="delete_reservation">
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
                <tr><th>ID</th><th>Usuário</th><th>Serviço</th><th>Data de Entrada</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for wl in waiting_list %}
                    <tr>
                        <td>{{ wl.id }}</td>
                        <td>{{ wl.user_name }} ({{ wl.user_email }})</td>
                        <td>{{ wl.service }}</td>
                        <td>{{ wl.created_at }}</td>
                        <td class="action-buttons">
                            <form method="post" style="display:inline;" onsubmit="return confirm('Tem certeza que deseja remover este item da fila de espera?');">
                                <input type="hidden" name="action" value="delete_waiting">
                                <input type="hidden" name="wl_id" value="{{ wl.id }}">
                                <button type="submit" class="btn-delete">Remover</button>
                            </form>
                            {# Adicionar botão para notificar usuário, se houver sistema de notificação #}
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Inserir Nova Miniatura (Estoque)</h2>
        <form method="post" class="action-form">
            <input type="hidden" name="action" value="insert_miniature">
            <label>Serviço: <input type="text" name="service" required></label>
            <label>Marca: <input type="text" name="marca" required></label>
            <label>Observações: <input type="text" name="obs"></label>
            <label>Preço: <input type="number" name="price" step="0.01" required></label>
            <label>Quantidade Inicial: <input type="number" name="quantity" min="0" required></label>
            <label>URL da Imagem: <input type="url" name="image" required></label>
            <label>Previsão de Chegada: <input type="text" name="previsao"></label>
            <button type="submit">Adicionar Miniatura</button>
        </form>

        <h2>Inserir Nova Reserva</h2>
        <form method="post" class="action-form">
            <input type="hidden" name="action" value="insert_reservation">
            <label>Usuário: 
                <select name="user_id" required>
                    {% for user in all_users %}<option value="{{ user.id }}">{{ user.name }} ({{ user.email }})</option>{% endfor %}
                </select>
            </label>
            <label>Serviço: <input type="text" name="service" required placeholder="Nome da miniatura"></label>
            <label>Quantidade: <input type="number" name="quantity" min="1" required></label>
            <label>Status: 
                <select name="status">
                    <option value="pending">Pendente</option>
                    <option value="approved">Aprovado</option>
                    <option value="denied">Negado</option>
                </select>
            </label>
            <label>Motivo (se negado): <input type="text" name="reason"></label>
            <button type="submit">Criar Reserva</button>
        </form>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="flash-messages">
                    {% for category, message in messages %}
                        <p class="flash-{{ category }}">{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Home</a>
    </div>
</body>
</html>
'''

# --- 9. Rotas da Aplicação ---

@app.route('/')
def index():
    thumbnails = load_thumbnails()
    return render_template_string(INDEX_HTML, thumbnails=thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, datetime=datetime)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        phone = request.form['phone'].strip()
        password = request.form['password']

        if not name:
            flash('Nome é obrigatório.', 'error')
            return render_template_string(REGISTER_HTML)
        if not is_valid_email(email):
            flash('Email inválido.', 'error')
            return render_template_string(REGISTER_HTML)
        if not is_valid_phone(phone):
            flash('Telefone inválido (apenas 10 ou 11 dígitos numéricos).', 'error')
            return render_template_string(REGISTER_HTML)
        if len(password) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML)
        
        hashed_pw = generate_password_hash(password).decode('utf-8')
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)', 
                      (name, email, phone, hashed_pw))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Este email já está cadastrado.', 'error')
        except Exception as e:
            logging.error(f'Erro ao registrar usuário: {e}')
            flash('Ocorreu um erro ao registrar. Tente novamente.', 'error')
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT id, password, role, email, name FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[1], password): # user[1] is password hash
            session['user_id'] = user[0]
            session['role'] = user[2]
            session['email'] = user[3]
            session['user_name'] = user[4]
            logging.info(f'Login bem-sucedido para {email}')
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Email ou senha inválidos.', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    user_email = session.get('email', 'usuário desconhecido')
    session.clear()
    logging.info(f'Logout de {user_email}')
    flash('Você foi desconectado.', 'success')
    return redirect(url_for('index'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Faça login para acessar seu perfil.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute('SELECT name, email, phone, role, data_cadastro FROM users WHERE id = ?', (session['user_id'],))
    user_data = c.fetchone()
    
    c.execute('SELECT service, quantity, status, created_at FROM reservations WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
    reservations = c.fetchall()
    
    conn.close()
    
    # Convert row objects to dicts for easier template access
    user_dict = {
        'name': user_data[0], 'email': user_data[1], 'phone': user_data[2],
        'role': user_data[3], 'data_cadastro': user_data[4]
    }
    reservations_list = [
        {'service': r[0], 'quantity': r[1], 'status': r[2], 'created_at': r[3]}
        for r in reservations
    ]
    
    return render_template_string(PROFILE_HTML, user=user_dict, reservations=reservations_list)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Faça login para reservar miniaturas.', 'error')
        return redirect(url_for('login'))
    
    all_thumbnails = load_thumbnails()
    
    # --- Filtering Logic ---
    filtered_thumbnails = all_thumbnails
    
    available_filter = request.args.get('available') == '1'
    previsao_search = request.args.get('previsao_search', '').strip().lower()
    marca_search = request.args.get('marca_search', '').strip().lower()
    order_by = request.args.get('order_by', '')

    if available_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
    if previsao_search:
        filtered_thumbnails = [t for t in filtered_thumbnails if previsao_search in t['previsao'].lower()]
    if marca_search:
        filtered_thumbnails = [t for t in filtered_thumbnails if marca_search in t['marca'].lower()]

    if order_by == 'service_asc':
        filtered_thumbnails.sort(key=lambda x: x['service'].lower())
    elif order_by == 'service_desc':
        filtered_thumbnails.sort(key=lambda x: x['service'].lower(), reverse=True)
    elif order_by == 'price_asc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')))
    elif order_by == 'price_desc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')), reverse=True)

    if request.method == 'POST':
        services_to_reserve = request.form.getlist('services')
        successful_reservations = 0
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        for service_name in services_to_reserve:
            quantity_str = request.form.get(f'quantity_{service_name}', '1')
            try:
                quantity = int(quantity_str)
            except ValueError:
                flash(f'Quantidade inválida para {service_name}.', 'error')
                continue
            
            if quantity <= 0:
                flash(f'Quantidade para {service_name} deve ser maior que zero.', 'error')
                continue

            # Get current stock
            c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name.lower(),))
            stock_row = c.fetchone()
            current_stock = stock_row[0] if stock_row else 0

            if current_stock >= quantity:
                c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                          (session['user_id'], service_name, quantity))
                c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', 
                          (quantity, service_name.lower()))
                successful_reservations += 1
            else:
                flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock}.', 'error')
        
        conn.commit()
        conn.close()
        
        if successful_reservations > 0:
            flash(f'{successful_reservations} reserva(s) realizada(s) com sucesso!', 'success')
        if successful_reservations < len(services_to_reserve):
            flash('Algumas reservas não puderam ser concluídas devido a estoque insuficiente.', 'error')
            
        return redirect(url_for('profile'))
        
    return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/reserve_single', methods=['GET', 'POST'])
def reserve_single():
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))
    
    service_name = request.args.get('service')
    if not service_name:
        flash('Serviço não especificado.', 'error')
        return redirect(url_for('index'))

    all_thumbnails = load_thumbnails()
    thumb_obj = next((t for t in all_thumbnails if t['service'] == service_name), None)

    if not thumb_obj:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('index'))

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name.lower(),))
    stock_row = c.fetchone()
    max_quantity = stock_row[0] if stock_row else 0
    conn.close()

    if request.method == 'POST':
        quantity_str = request.form.get('quantity', '0')
        try:
            quantity = int(quantity_str)
        except ValueError:
            flash('Quantidade inválida.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb_obj, max_quantity=max_quantity, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

        if quantity <= 0 or quantity > max_quantity:
            flash(f'Quantidade inválida ou insuficiente no estoque. Disponível: {max_quantity}.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb_obj, max_quantity=max_quantity, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)',
                  (session['user_id'], service_name, quantity))
        c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', 
                  (quantity, service_name.lower()))
        conn.commit()
        conn.close()
        flash(f'{quantity} unidade(s) de {service_name} reservada(s) com sucesso!', 'success')
        return redirect(url_for('profile'))

    return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb_obj, max_quantity=max_quantity, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/add_waiting_list/<service_name>')
def add_waiting_list(service_name):
    if 'user_id' not in session:
        flash('Faça login para entrar na fila de espera.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], service_name))
        conn.commit()
        flash(f'Você foi adicionado à fila de espera para {service_name}.', 'success')
    except sqlite3.IntegrityError:
        flash(f'Você já está na fila de espera para {service_name}.', 'info')
    except Exception as e:
        logging.error(f'Erro ao adicionar à fila de espera: {e}')
        flash('Ocorreu um erro ao adicionar à fila de espera. Tente novamente.', 'error')
    finally:
        conn.close()
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # --- POST Actions ---
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'promote':
            user_id = request.form.get('user_id')
            c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
            flash('Usuário promovido para admin.', 'success')
        elif action == 'demote':
            user_id = request.form.get('user_id')
            c.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
            flash('Usuário rebaixado para user.', 'success')
        elif action == 'delete_user':
            user_id = request.form.get('user_id')
            # Delete associated reservations and waiting list entries first
            c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,))
            c.execute('DELETE FROM waiting_list WHERE user_id = ?', (user_id,))
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            flash('Usuário e dados associados deletados.', 'success')
        elif action == 'approve':
            res_id = request.form.get('res_id')
            c.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
            flash('Reserva aprovada.', 'success')
        elif action == 'deny':
            res_id = request.form.get('res_id')
            reason = request.form.get('reason', 'Motivo não especificado')
            c.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
            flash('Reserva negada.', 'success')
        elif action == 'delete_reservation':
            res_id = request.form.get('res_id')
            c.execute('SELECT service, quantity FROM reservations WHERE id = ?', (res_id,))
            res_data = c.fetchone()
            if res_data:
                service_name = res_data[0].lower() # Normalize service name
                quantity_reserved = res_data[1]
                c.execute('UPDATE stock SET quantity = quantity + ? WHERE service = ?', (quantity_reserved, service_name))
                flash(f'Estoque de {res_data[0]} restaurado em {quantity_reserved} unidades.', 'info')
            c.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
            flash('Reserva deletada.', 'success')
        elif action == 'delete_waiting':
            wl_id = request.form.get('wl_id')
            c.execute('DELETE FROM waiting_list WHERE id = ?', (wl_id,))
            flash('Item da fila de espera removido.', 'success')
        elif action == 'insert_miniature':
            service = request.form.get('service', '').strip()
            marca = request.form.get('marca', '').strip()
            obs = request.form.get('obs', '').strip()
            price_str = request.form.get('price', '0')
            quantity_str = request.form.get('quantity', '0')
            image = request.form.get('image', '').strip()
            previsao = request.form.get('previsao', '').strip()

            try:
                price = float(price_str)
                quantity = int(quantity_str)
            except ValueError:
                flash('Preço ou quantidade inválidos para a miniatura.', 'error')
                conn.commit() # Commit any previous changes
                return redirect(url_for('admin'))
            
            if not service or not marca or not image:
                flash('Nome, Marca e Imagem são obrigatórios para a miniatura.', 'error')
                conn.commit()
                return redirect(url_for('admin'))

            # Insert/Update stock
            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', 
                      (service.lower(), quantity))
            # Note: Other fields (marca, obs, price, image, previsao) are not stored in DB stock table,
            # they are primarily managed via Google Sheet. This action only updates stock.
            flash(f'Estoque para "{service}" atualizado/inserido com {quantity} unidades.', 'success')
        elif action == 'insert_reservation':
            user_id = request.form.get('user_id')
            service = request.form.get('service', '').strip()
            quantity_str = request.form.get('quantity', '0')
            status = request.form.get('status', 'pending')
            reason = request.form.get('reason', '').strip()

            try:
                quantity = int(quantity_str)
            except ValueError:
                flash('Quantidade inválida para a reserva.', 'error')
                conn.commit()
                return redirect(url_for('admin'))
            
            if not user_id or not service or quantity <= 0:
                flash('Usuário, serviço e quantidade válidos são obrigatórios para a reserva.', 'error')
                conn.commit()
                return redirect(url_for('admin'))
            
            # Check stock if status is approved
            if status == 'approved':
                c.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),))
                stock_row = c.fetchone()
                current_stock = stock_row[0] if stock_row else 0
                if current_stock < quantity:
                    flash(f'Estoque insuficiente para aprovar a reserva de {service}. Disponível: {current_stock}.', 'error')
                    conn.commit()
                    return redirect(url_for('admin'))
                c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))

            c.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)', 
                      (user_id, service, quantity, status, reason))
            flash(f'Reserva para {service} criada com sucesso.', 'success')
        elif action == 'sync_stock':
            if sheet:
                try:
                    records = sheet.get_all_records()
                    for record in records:
                        service = record.get('NOME DA MINIATURA', '').strip().lower()
                        qty = record.get('QUANTIDADE DISPONIVEL', 0)
                        if service:
                            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
                    flash('Estoque sincronizado da planilha para o DB.', 'success')
                except Exception as e:
                    logging.error(f'Erro na sincronização do estoque: {e}')
                    flash('Erro na sincronização do estoque com a planilha.', 'error')
            else:
                flash('Google Sheets não configurado.', 'error')
        
        conn.commit()
        return redirect(url_for('admin'))

    # --- GET Data for Display ---
    # Stats
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations')
    total_reservations = c.fetchone()[0]
    stats = {'users_count': users_count, 'pending_count': pending_count, 'total_reservations': total_reservations}

    # Users
    email_filter = request.args.get('email_filter', '').strip()
    role_filter = request.args.get('role_filter', '').strip()
    query_users = 'SELECT id, name, email, phone, role, data_cadastro FROM users WHERE 1=1'
    params_users = []
    if email_filter:
        query_users += ' AND email LIKE ?'
        params_users.append(f'%{email_filter}%')
    if role_filter:
        query_users += ' AND role = ?'
        params_users.append(role_filter)
    c.execute(query_users, params_users)
    users = c.fetchall()
    users_list = [
        {'id': u[0], 'name': u[1], 'email': u[2], 'phone': u[3], 'role': u[4], 'data_cadastro': u[5]}
        for u in users
    ]

    # Reservations
    service_filter = request.args.get('service_filter', '').strip()
    status_filter = request.args.get('status_filter', '').strip()
    query_reservations = '''
        SELECT r.id, u.name as user_name, u.email as user_email, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
        FROM reservations r JOIN users u ON r.user_id = u.id WHERE 1=1
    '''
    params_reservations = []
    if service_filter:
        query_reservations += ' AND r.service LIKE ?'
        params_reservations.append(f'%{service_filter}%')
    if status_filter:
        query_reservations += ' AND r.status = ?'
        params_reservations.append(status_filter)
    query_reservations += ' ORDER BY r.created_at DESC'
    c.execute(query_reservations, params_reservations)
    all_reservations = c.fetchall()
    reservations_list = [
        {'id': r[0], 'user_name': r[1], 'user_email': r[2], 'service': r[3], 'quantity': r[4], 
         'status': r[5], 'denied_reason': r[6], 'created_at': r[7]}
        for r in all_reservations
    ]

    # Waiting List
    query_waiting_list = '''
        SELECT wl.id, u.name as user_name, u.email as user_email, wl.service, wl.created_at 
        FROM waiting_list wl JOIN users u ON wl.user_id = u.id ORDER BY wl.created_at DESC
    '''
    c.execute(query_waiting_list)
    waiting_list = c.fetchall()
    waiting_list_list = [
        {'id': wl[0], 'user_name': wl[1], 'user_email': wl[2], 'service': wl[3], 'created_at': wl[4]}
        for wl in waiting_list
    ]

    # All users for insert reservation dropdown
    c.execute('SELECT id, name, email FROM users ORDER BY name')
    all_users_for_dropdown = c.fetchall()
    all_users_list = [
        {'id': u[0], 'name': u[1], 'email': u[2]}
        for u in all_users_for_dropdown
    ]

    conn.close()
    
    return render_template_string(ADMIN_HTML, 
                                  stats=stats, 
                                  users=users_list, 
                                  all_reservations=reservations_list, 
                                  waiting_list=waiting_list_list,
                                  all_users=all_users_list,
                                  request=request, # Pass request object for filter values
                                  session=session # Pass session for admin self-management logic
                                 )

@app.route('/backup')
def backup():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute('SELECT * FROM users')
    users = c.fetchall()
    c.execute('SELECT * FROM reservations')
    reservations = c.fetchall()
    c.execute('SELECT * FROM stock')
    stock = c.fetchall()
    c.execute('SELECT * FROM waiting_list')
    waiting_list = c.fetchall()
    
    conn.close()
    
    # Convert rows to dicts for JSON serialization
    users_dicts = [dict(row) for row in users]
    reservations_dicts = [dict(row) for row in reservations]
    stock_dicts = [dict(row) for row in stock]
    waiting_list_dicts = [dict(row) for row in waiting_list]

    data = {
        'timestamp': datetime.now().isoformat(),
        'users': users_dicts,
        'reservations': reservations_dicts,
        'stock': stock_dicts,
        'waiting_list': waiting_list_dicts
    }
    
    response = make_response(json.dumps(data, indent=4, ensure_ascii=False))
    response.headers['Content-Disposition'] = f'attachment; filename=jgminis_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    response.headers['Content-Type'] = 'application/json'
    return response

@app.route('/export_csv')
def export_csv():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''SELECT r.id, u.name as user_name, u.email as user_email, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
                      FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC''')
    rows = c.fetchall()
    conn.close()
    
    si = io.StringIO()
    writer = csv.writer(si)
    
    writer.writerow(['ID', 'Nome Usuário', 'Email Usuário', 'Serviço', 'Quantidade', 'Status', 'Motivo Negação', 'Data Criação'])
    for row in rows:
        writer.writerow([row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]])
    
    output = io.BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    
    return send_file(output, mimetype='text/csv', as_attachment=True, 
                     download_name=f'jgminis_reservations_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')

@app.route('/sync_stock')
def sync_stock_route(): # Renamed to avoid conflict with function name
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    if sheet:
        try:
            records = sheet.get_all_records()
            for record in records:
                service = record.get('NOME DA MINIATURA', '').strip().lower()
                qty = record.get('QUANTIDADE DISPONIVEL', 0)
                if service:
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
            conn.commit()
            flash('Estoque sincronizado da planilha para o DB.', 'success')
        except Exception as e:
            logging.error(f'Erro na sincronização do estoque: {e}')
            flash('Erro na sincronização do estoque com a planilha.', 'error')
        finally:
            conn.close()
    else:
        flash('Google Sheets não configurado.', 'error')
        conn.close()
    
    return redirect(url_for('admin'))

@app.errorhandler(404)
def page_not_found(e):
    flash('A página que você tentou acessar não foi encontrada.', 'error')
    return redirect(url_for('index')), 404

@app.errorhandler(500)
def internal_error(e):
    logging.error(f'Erro interno do servidor: {e}')
    flash('Ocorreu um erro interno no servidor. Por favor, tente novamente mais tarde.', 'error')
    return redirect(url_for('index')), 500

@app.route('/favicon.ico')
def favicon():
    return '', 204

# --- 10. Run App ---
if __name__ == '__main__':
    # init_db() # Already called once globally
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
