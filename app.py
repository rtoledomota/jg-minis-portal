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
        .thumbnail { background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; text-align: center; transition: transform 0.3s ease, box-shadow 0.3s ease; position: relative; }
        .thumbnail:hover { transform: translateY(-5px); box-shadow: 0 6px 16px rgba(0,0,0,0.12); }
        .thumbnail img { width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #eee; }
        .thumbnail.esgotado { opacity: 0.7; filter: grayscale(100%); }
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
        .btn-waiting { background-color: #ffc107; color: #212529; border: none; }
        .btn-waiting:hover { background-color: #e0a800; }
        .btn-contact { background-color: #25D366; color: white; border: none; }
        .btn-contact:hover { background-color: #1DA851; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 20px; margin-top: 40px; font-size: 0.9em; }
        @media (max-width: 768px) {
            header h1 { font-size: 1.8em; }
            nav a { margin: 0 10px; }
            .grid-container { grid-template-columns: 1fr; padding: 15px; }
            .thumbnail img { height: 150px; }
            .action-buttons { flex-direction: column; }
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
    <title>Registrar - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .register-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        form { display: flex; flex-direction: column; gap: 15px; }
        label { text-align: left; font-weight: bold; color: #555; }
        input[type="text"], input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 1em; }
        button { background-color: #28a745; color: white; padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.1em; font-weight: bold; transition: background-color 0.3s ease; margin-top: 15px; }
        button:hover { background-color: #218838; }
        .flash-messages { margin-top: 20px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .back-link { display: block; margin-top: 20px; color: #007bff; text-decoration: none; font-weight: bold; }
        .back-link:hover { text-decoration: underline; }
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
            <input type="text" id="name" name="name" required>
            <label for="email">Email:</label>
            <input type="email" id="email" name="email" required>
            <label for="phone">Telefone:</label>
            <input type="text" id="phone" name="phone" required pattern="[0-9]{10,11}" title="Telefone deve ter 10 ou 11 dígitos numéricos">
            <label for="password">Senha (mín. 6 caracteres):</label>
            <input type="password" id="password" name="password" required minlength="6">
            <button type="submit">Registrar</button>
        </form>
        <a href="{{ url_for('login') }}" class="back-link">Já tem conta? Fazer Login</a>
        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Início</a>
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
        .login-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        form { display: flex; flex-direction: column; gap: 15px; }
        label { text-align: left; font-weight: bold; color: #555; }
        input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 1em; }
        button { background-color: #007bff; color: white; padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.1em; font-weight: bold; transition: background-color 0.3s ease; margin-top: 15px; }
        button:hover { background-color: #0056b3; }
        .flash-messages { margin-top: 20px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .link-group { display: flex; justify-content: space-between; margin-top: 20px; }
        .link-group a { color: #007bff; text-decoration: none; font-weight: bold; }
        .link-group a:hover { text-decoration: underline; }
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
            <input type="email" id="email" name="email" required>
            <label for="password">Senha:</label>
            <input type="password" id="password" name="password" required>
            <button type="submit">Entrar</button>
        </form>
        <div class="link-group">
            <a href="{{ url_for('register') }}">Não tem conta? Registrar</a>
            <a href="{{ url_for('index') }}">Voltar ao Início</a>
        </div>
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
    <title>Reservar Miniaturas - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 900px; margin: 20px auto; }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        .filters { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 30px; padding: 15px; border: 1px solid #eee; border-radius: 8px; background-color: #f9f9f9; }
        .filters label { display: flex; align-items: center; gap: 5px; font-weight: bold; color: #555; }
        .filters input[type="checkbox"] { transform: scale(1.2); }
        .filters input[type="text"], .filters select { padding: 8px; border: 1px solid #ddd; border-radius: 5px; font-size: 0.9em; }
        .filters button { background-color: #007bff; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .filters button:hover { background-color: #0056b3; }
        .miniature-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }
        .miniature-item { background-color: #f9f9f9; border: 1px solid #eee; border-radius: 8px; padding: 15px; text-align: center; position: relative; }
        .miniature-item img { max-width: 100%; height: 120px; object-fit: cover; border-radius: 5px; margin-bottom: 10px; }
        .miniature-item h3 { font-size: 1.1em; color: #007bff; margin-top: 0; margin-bottom: 5px; }
        .miniature-item p { font-size: 0.9em; color: #666; margin-bottom: 5px; }
        .miniature-item input[type="checkbox"] { position: absolute; top: 10px; left: 10px; transform: scale(1.3); }
        .miniature-item input[type="number"] { width: 80px; padding: 5px; border: 1px solid #ddd; border-radius: 5px; text-align: center; margin-top: 10px; }
        .submit-btn { background-color: #28a745; color: white; padding: 12px 25px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.1em; font-weight: bold; transition: background-color 0.3s ease; display: block; width: fit-content; margin: 30px auto 0; }
        .submit-btn:hover { background-color: #218838; }
        .flash-messages { margin-top: 20px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .back-link { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; font-weight: bold; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Reservar Miniaturas</h1>
        <div class="flash-messages">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </div>
        <form method="get" class="filters">
            <label>
                <input type="checkbox" name="available" value="true" {% if request.args.get('available') == 'true' %}checked{% endif %}>
                Disponíveis
            </label>
            <label>
                Ordenar por:
                <select name="order_by">
                    <option value="">Padrão</option>
                    <option value="service_asc" {% if request.args.get('order_by') == 'service_asc' %}selected{% endif %}>Nome (A-Z)</option>
                    <option value="service_desc" {% if request.args.get('order_by') == 'service_desc' %}selected{% endif %}>Nome (Z-A)</option>
                    <option value="price_asc" {% if request.args.get('order_by') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                    <option value="price_desc" {% if request.args.get('order_by') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                </select>
            </label>
            <label>
                Previsão de Chegada:
                <input type="text" name="previsao_filter" value="{{ request.args.get('previsao_filter', '') }}" placeholder="Ex: 2025-01">
            </label>
            <label>
                Marca:
                <input type="text" name="marca_filter" value="{{ request.args.get('marca_filter', '') }}" placeholder="Ex: Hot Wheels">
            </label>
            <button type="submit">Aplicar Filtros</button>
        </form>

        <form method="post">
            <div class="miniature-list">
                {% for thumb in thumbnails %}
                    <div class="miniature-item">
                        <input type="checkbox" name="services" value="{{ thumb.service }}" id="service-{{ loop.index }}">
                        <label for="service-{{ loop.index }}">
                            <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                            <h3>{{ thumb.service }}</h3>
                            <p>{{ thumb.marca }}</p>
                            <p>R$ {{ thumb.price }}</p>
                            <p>Disponível: {{ thumb.quantity }}</p>
                            {% if thumb.quantity > 0 %}
                                <input type="number" name="quantity_{{ thumb.service }}" min="1" max="{{ thumb.quantity }}" value="1">
                            {% else %}
                                <p style="color: red; font-weight: bold;">Esgotado</p>
                            {% endif %}
                        </label>
                    </div>
                {% endfor %}
            </div>
            <button type="submit" class="submit-btn">Reservar Selecionadas</button>
        </form>
        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Início</a>
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
    <title>Reservar {{ service }} - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .reserve-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        form { display: flex; flex-direction: column; gap: 15px; }
        label { text-align: left; font-weight: bold; color: #555; }
        input[type="number"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 1em; }
        button { background-color: #28a745; color: white; padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.1em; font-weight: bold; transition: background-color 0.3s ease; margin-top: 15px; }
        button:hover { background-color: #218838; }
        .flash-messages { margin-top: 20px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .back-link { display: block; margin-top: 20px; color: #007bff; text-decoration: none; font-weight: bold; }
        .back-link:hover { text-decoration: underline; }
        .whatsapp-link { display: block; margin-top: 10px; color: #25D366; text-decoration: none; font-weight: bold; }
        .whatsapp-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="reserve-container">
        <h1>Reservar {{ service }}</h1>
        <div class="flash-messages">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </div>
        {% if max_quantity > 0 %}
            <form method="post">
                <label for="quantity">Quantidade (Disponível: {{ max_quantity }}):</label>
                <input type="number" id="quantity" name="quantity" min="1" max="{{ max_quantity }}" value="1" required>
                <button type="submit">Confirmar Reserva</button>
            </form>
        {% else %}
            <p style="color: red; font-weight: bold;">Esta miniatura está esgotada.</p>
            <a href="{{ url_for('add_waiting_list', service=service) }}" class="btn btn-waiting">Fila de Espera</a>
            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="whatsapp-link" target="_blank">Entrar em Contato via WhatsApp</a>
        {% endif %}
        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Início</a>
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
    <title>Meu Perfil - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 800px; margin: 20px auto; }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        p { font-size: 1em; line-height: 1.6; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; font-size: 0.95em; }
        th { background-color: #f0f0f0; font-weight: bold; }
        .no-reservations { text-align: center; color: #666; margin-top: 20px; }
        .back-link { display: block; text-align: center; margin-top: 30px; color: #007bff; text-decoration: none; font-weight: bold; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Meu Perfil</h1>
        <h2>Minhas Informações</h2>
        <p><strong>Nome:</strong> {{ user.name }}</p>
        <p><strong>Email:</strong> {{ user.email }}</p>
        <p><strong>Telefone:</strong> {{ user.phone }}</p>
        <p><strong>Membro desde:</strong> {{ user.data_cadastro }}</p>

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
            <p class="no-reservations">Você ainda não fez nenhuma reserva.</p>
        {% endif %}
        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Início</a>
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
    <title>Admin - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 1200px; margin: 20px auto; }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background-color: #e9f5ff; padding: 20px; border-radius: 8px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
        .stat-card h3 { color: #004085; margin-top: 0; font-size: 1.2em; }
        .stat-card p { font-size: 1.8em; font-weight: bold; color: #007bff; margin: 0; }
        .admin-section { margin-bottom: 40px; padding: 20px; border: 1px solid #eee; border-radius: 8px; background-color: #f9f9f9; }
        .admin-section form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 15px; }
        .admin-section label { font-weight: bold; color: #555; }
        .admin-section input[type="text"], .admin-section input[type="number"], .admin-section input[type="url"], .admin-section select { padding: 8px; border: 1px solid #ddd; border-radius: 5px; font-size: 0.9em; flex-grow: 1; min-width: 150px; }
        .admin-section button { background-color: #007bff; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .admin-section button:hover { background-color: #0056b3; }
        .admin-section .action-btn { background-color: #28a745; margin-left: 5px; }
        .admin-section .action-btn.deny { background-color: #dc3545; }
        .admin-section .action-btn.delete { background-color: #6c757d; }
        .admin-section .action-btn.promote { background-color: #ffc107; color: #212529; }
        .admin-section .action-btn:hover { opacity: 0.9; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; font-size: 0.9em; }
        th { background-color: #f0f0f0; font-weight: bold; }
        .flash-messages { margin-top: 20px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .back-link { display: block; text-align: center; margin-top: 30px; color: #007bff; text-decoration: none; font-weight: bold; }
        .back-link:hover { text-decoration: underline; }
        .form-row { display: flex; flex-wrap: wrap; gap: 10px; width: 100%; }
        .form-row > div { flex: 1; min-width: 200px; }
        .form-row label { display: block; margin-bottom: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Painel Administrativo</h1>
        <div class="flash-messages">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <h3>Total de Usuários</h3>
                <p>{{ stats.users }}</p>
            </div>
            <div class="stat-card">
                <h3>Reservas Pendentes</h3>
                <p>{{ stats.pending }}</p>
            </div>
            <div class="stat-card">
                <h3>Total de Reservas</h3>
                <p>{{ stats.total_res }}</p>
            </div>
            <div class="stat-card">
                <h3>Fila de Espera</h3>
                <p>{{ stats.waiting }}</p>
            </div>
        </div>

        <div class="admin-section">
            <h2>Ferramentas de Sistema</h2>
            <form method="post" style="justify-content: center;">
                <button type="submit" name="action" value="sync_stock" class="action-btn">Sincronizar Estoque da Planilha</button>
                <button type="button" onclick="location.href='{{ url_for('backup') }}'" class="action-btn">Backup DB (JSON)</button>
                <button type="button" onclick="location.href='{{ url_for('export_csv') }}'" class="action-btn">Exportar Reservas (CSV)</button>
            </form>
        </div>

        <div class="admin-section">
            <h2>Gerenciar Usuários</h2>
            <form method="get">
                <label>Email: <input type="text" name="user_email_filter" value="{{ request.args.get('user_email_filter', '') }}"></label>
                <label>Role: 
                    <select name="user_role_filter">
                        <option value="">Todos</option>
                        <option value="user" {% if request.args.get('user_role_filter') == 'user' %}selected{% endif %}>User</option>
                        <option value="admin" {% if request.args.get('user_role_filter') == 'admin' %}selected{% endif %}>Admin</option>
                    </select>
                </label>
                <button type="submit">Filtrar Usuários</button>
            </form>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Nome</th>
                        <th>Email</th>
                        <th>Telefone</th>
                        <th>Role</th>
                        <th>Ações</th>
                    </tr>
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
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="promote_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="action-btn promote">Promover</button>
                                </form>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="demote_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="action-btn">Rebaixar</button>
                                </form>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="delete_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="action-btn delete" onclick="return confirm('Tem certeza que deseja deletar este usuário e todas as suas reservas?')">Deletar</button>
                                </form>
                            </td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="admin-section">
            <h2>Gerenciar Reservas Pendentes</h2>
            <form method="get">
                <label>Serviço: <input type="text" name="res_service_filter" value="{{ request.args.get('res_service_filter', '') }}"></label>
                <label>Email Usuário: <input type="text" name="res_email_filter" value="{{ request.args.get('res_email_filter', '') }}"></label>
                <button type="submit">Filtrar Reservas</button>
            </form>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Usuário</th>
                        <th>Serviço</th>
                        <th>Quantidade</th>
                        <th>Data Criação</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for res in pending_reservations %}
                        <tr>
                            <td>{{ res.id }}</td>
                            <td>{{ res.user_name }} ({{ res.user_email }})</td>
                            <td>{{ res.service }}</td>
                            <td>{{ res.quantity }}</td>
                            <td>{{ res.created_at }}</td>
                            <td>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="approve_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <button type="submit" class="action-btn">Aprovar</button>
                                </form>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="deny_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <input type="text" name="reason" placeholder="Motivo da recusa" required style="width: 120px;">
                                    <button type="submit" class="action-btn deny">Recusar</button>
                                </form>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="delete_reservation">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <button type="submit" class="action-btn delete" onclick="return confirm('Tem certeza que deseja deletar esta reserva? O estoque será restaurado.')">Deletar</button>
                                </form>
                            </td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="admin-section">
            <h2>Fila de Espera</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Usuário</th>
                        <th>Serviço</th>
                        <th>Data Entrada</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for wl in waiting_list %}
                        <tr>
                            <td>{{ wl.id }}</td>
                            <td>{{ wl.user_name }} ({{ wl.user_email }})</td>
                            <td>{{ wl.service }}</td>
                            <td>{{ wl.created_at }}</td>
                            <td>
                                <form method="post" style="display:inline;">
                                    <input type="hidden" name="action" value="delete_waiting_entry">
                                    <input type="hidden" name="wl_id" value="{{ wl.id }}">
                                    <button type="submit" class="action-btn delete" onclick="return confirm('Remover da fila de espera?')">Remover</button>
                                </form>
                            </td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="admin-section">
            <h2>Inserir Nova Miniatura (no Estoque)</h2>
            <form method="post">
                <input type="hidden" name="action" value="insert_miniature">
                <div class="form-row">
                    <div><label>Serviço (Nome): <input type="text" name="service" required></label></div>
                    <div><label>Marca/Fabricante: <input type="text" name="marca" required></label></div>
                    <div><label>Observações: <input type="text" name="obs"></label></div>
                    <div><label>Preço: <input type="number" name="price" step="0.01" required></label></div>
                    <div><label>Quantidade Inicial: <input type="number" name="quantity" min="0" required></label></div>
                    <div><label>URL da Imagem: <input type="url" name="image" required></label></div>
                </div>
                <button type="submit" style="margin-top: 15px;">Adicionar Miniatura</button>
            </form>
        </div>

        <div class="admin-section">
            <h2>Inserir Nova Reserva Manualmente</h2>
            <form method="post">
                <input type="hidden" name="action" value="insert_reservation_manual">
                <div class="form-row">
                    <div>
                        <label>Usuário:</label>
                        <select name="user_id" required>
                            <option value="">Selecione um usuário</option>
                            {% for user in all_users %}<option value="{{ user.id }}">{{ user.name }} ({{ user.email }})</option>{% endfor %}
                        </select>
                    </div>
                    <div>
                        <label>Serviço:</label>
                        <select name="service" required>
                            <option value="">Selecione um serviço</option>
                            {% for service_name in all_services %}<option value="{{ service_name }}">{{ service_name }}</option>{% endfor %}
                        </select>
                    </div>
                    <div><label>Quantidade: <input type="number" name="quantity" min="1" required></label></div>
                    <div>
                        <label>Status:</label>
                        <select name="status">
                            <option value="pending">Pendente</option>
                            <option value="approved">Aprovada</option>
                            <option value="denied">Recusada</option>
                        </select>
                    </div>
                    <div><label>Motivo (se recusada): <input type="text" name="reason"></label></div>
                </div>
                <button type="submit" style="margin-top: 15px;">Criar Reserva</button>
            </form>
        </div>

        <a href="{{ url_for('index') }}" class="back-link">Voltar ao Início</a>
    </div>
</body>
</html>
'''

# --- 9. Routes ---

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
            flash('Telefone inválido (10 ou 11 dígitos numéricos).', 'error')
            return render_template_string(REGISTER_HTML)
        if len(password) < 6:
            flash('Senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML)
        
        hashed_pw = generate_password_hash(password).decode('utf-8')
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)', 
                      (name, email, phone, hashed_pw))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email já cadastrado.', 'error')
        except Exception as e:
            logging.error(f'Erro no registro: {e}')
            flash('Erro interno ao registrar.', 'error')
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
        c.execute('SELECT id, password, role, email FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        conn.close()
        
        if user and check_password_hash(user[1], password):
            session['user_id'] = user[0]
            session['role'] = user[2]
            session['email'] = user[3]
            logging.info(f'Usuário {email} logado com sucesso.')
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Credenciais inválidas. Verifique seu email e senha.', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    user_email = session.get('email', 'usuário desconhecido')
    session.clear()
    flash('Você foi desconectado.', 'success')
    logging.info(f'Usuário {user_email} desconectado.')
    return redirect(url_for('index'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Você precisa estar logado para acessar esta página.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute('SELECT name, email, phone, data_cadastro FROM users WHERE id = ?', (session['user_id'],))
    user_data = c.fetchone()
    
    c.execute('SELECT service, quantity, status, created_at FROM reservations WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
    reservations = c.fetchall()
    
    conn.close()
    
    user_dict = {
        'name': user_data[0],
        'email': user_data[1],
        'phone': user_data[2],
        'data_cadastro': user_data[3]
    }
    
    reservations_list = []
    for res in reservations:
        reservations_list.append({
            'service': res[0],
            'quantity': res[1],
            'status': res[2],
            'created_at': res[3]
        })
        
    return render_template_string(PROFILE_HTML, user=user_dict, reservations=reservations_list)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar miniaturas.', 'error')
        return redirect(url_for('login'))
    
    all_thumbnails = load_thumbnails()
    
    if request.method == 'POST':
        selected_services = request.form.getlist('services')
        if not selected_services:
            flash('Nenhuma miniatura selecionada para reserva.', 'error')
            return redirect(url_for('reservar'))
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        successful_reservations = 0
        
        for service_name in selected_services:
            quantity_str = request.form.get(f'quantity_{service_name}', '0')
            try:
                quantity = int(quantity_str)
            except ValueError:
                flash(f'Quantidade inválida para {service_name}.', 'error')
                continue
            
            if quantity <= 0:
                flash(f'Quantidade para {service_name} deve ser maior que zero.', 'error')
                continue
            
            c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name.lower(),))
            stock_row = c.fetchone()
            current_stock = stock_row[0] if stock_row else 0
            
            if current_stock >= quantity:
                c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                          (session['user_id'], service_name, quantity))
                c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service_name.lower()))
                successful_reservations += 1
            else:
                flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock}.', 'error')
        
        conn.commit()
        conn.close()
        
        if successful_reservations > 0:
            flash(f'{successful_reservations} reserva(s) realizada(s) com sucesso!', 'success')
        return redirect(url_for('profile'))
    
    # GET request - apply filters
    filtered_thumbnails = all_thumbnails
    
    available_filter = request.args.get('available') == 'true'
    order_by = request.args.get('order_by', '')
    previsao_filter = request.args.get('previsao_filter', '').strip().lower()
    marca_filter = request.args.get('marca_filter', '').strip().lower()

    if available_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
    
    if previsao_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if previsao_filter in t['previsao'].lower()]
        
    if marca_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]

    if order_by == 'service_asc':
        filtered_thumbnails.sort(key=lambda x: x['service'])
    elif order_by == 'service_desc':
        filtered_thumbnails.sort(key=lambda x: x['service'], reverse=True)
    elif order_by == 'price_asc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')))
    elif order_by == 'price_desc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')), reverse=True)

    return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL)

@app.route('/reserve_single', methods=['GET', 'POST'])
def reserve_single():
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar miniaturas.', 'error')
        return redirect(url_for('login'))
    
    service_name = request.args.get('service')
    if not service_name:
        flash('Serviço não especificado.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name.lower(),))
    stock_row = c.fetchone()
    max_quantity = stock_row[0] if stock_row else 0
    conn.close()
    
    if request.method == 'POST':
        try:
            quantity = int(request.form['quantity'])
        except ValueError:
            flash('Quantidade inválida.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, service=service_name, max_quantity=max_quantity, whatsapp_number=WHATSAPP_NUMBER, session=session)
        
        if quantity <= 0:
            flash('A quantidade deve ser maior que zero.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, service=service_name, max_quantity=max_quantity, whatsapp_number=WHATSAPP_NUMBER, session=session)
            
        if quantity <= max_quantity:
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                      (session['user_id'], service_name, quantity))
            c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service_name.lower()))
            conn.commit()
            conn.close()
            flash(f'{quantity} unidade(s) de {service_name} reservada(s) com sucesso!', 'success')
            return redirect(url_for('profile'))
        else:
            flash(f'Quantidade solicitada ({quantity}) excede o estoque disponível ({max_quantity}).', 'error')
    
    return render_template_string(RESERVE_SINGLE_HTML, service=service_name, max_quantity=max_quantity, whatsapp_number=WHATSAPP_NUMBER, session=session)

@app.route('/add_waiting_list/<service>')
def add_waiting_list(service):
    if 'user_id' not in session:
        flash('Você precisa estar logado para entrar na fila de espera.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], service))
        conn.commit()
        flash(f'Você foi adicionado à fila de espera para "{service}".', 'success')
    except sqlite3.IntegrityError:
        flash(f'Você já está na fila de espera para "{service}".', 'error')
    except Exception as e:
        logging.error(f'Erro ao adicionar à fila de espera: {e}')
        flash('Erro ao adicionar à fila de espera.', 'error')
    finally:
        conn.close()
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado. Você não tem permissão de administrador.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'promote_user':
            user_id = request.form['user_id']
            c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
            flash('Usuário promovido a administrador.', 'success')
        elif action == 'demote_user':
            user_id = request.form['user_id']
            c.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
            flash('Usuário rebaixado para usuário comum.', 'success')
        elif action == 'delete_user':
            user_id = request.form['user_id']
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,))
            c.execute('DELETE FROM waiting_list WHERE user_id = ?', (user_id,))
            flash('Usuário e todas as suas reservas/entradas na fila deletados.', 'success')
        elif action == 'approve_res':
            res_id = request.form['res_id']
            c.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
            flash('Reserva aprovada.', 'success')
        elif action == 'deny_res':
            res_id = request.form['res_id']
            reason = request.form.get('reason', 'Motivo não especificado.')
            c.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
            flash('Reserva recusada.', 'success')
        elif action == 'delete_reservation':
            res_id = request.form['res_id']
            c.execute('SELECT service, quantity FROM reservations WHERE id = ?', (res_id,))
            res_data = c.fetchone()
            if res_data:
                service_name = res_data[0].lower()
                quantity_reserved = res_data[1]
                c.execute('UPDATE stock SET quantity = quantity + ? WHERE service = ?', (quantity_reserved, service_name))
                flash(f'Reserva deletada e {quantity_reserved} unidade(s) de "{res_data[0]}" restaurada(s) ao estoque.', 'success')
            c.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
        elif action == 'delete_waiting_entry':
            wl_id = request.form['wl_id']
            c.execute('DELETE FROM waiting_list WHERE id = ?', (wl_id,))
            flash('Entrada da fila de espera removida.', 'success')
        elif action == 'insert_miniature':
            service = request.form['service'].strip()
            marca = request.form['marca'].strip()
            obs = request.form['obs'].strip()
            price = float(request.form['price'])
            quantity = int(request.form['quantity'])
            image = request.form['image'].strip()
            
            # Insert/Update into stock table
            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service.lower(), quantity))
            # Note: Other fields (marca, obs, price, image) are not stored in DB stock table, only in Google Sheet.
            # For full persistence of these fields, a separate 'miniatures' table would be needed.
            flash(f'Miniatura "{service}" adicionada/atualizada no estoque.', 'success')
        elif action == 'insert_reservation_manual':
            user_id = request.form['user_id']
            service = request.form['service'].strip()
            quantity = int(request.form['quantity'])
            status = request.form['status']
            reason = request.form.get('reason', '')
            
            if quantity <= 0:
                flash('Quantidade deve ser maior que zero.', 'error')
            else:
                c.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)', 
                               (user_id, service, quantity, status, reason))
                if status == 'approved':
                    c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
                flash(f'Reserva manual para "{service}" criada.', 'success')
        elif action == 'sync_stock':
            try:
                records = sheet.get_all_records()
                for record in records:
                    service = record.get('NOME DA MINIATURA', '').strip().lower()
                    qty = record.get('QUANTIDADE DISPONIVEL', 0)
                    if service:
                        c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
                flash('Estoque sincronizado da planilha para o DB.', 'success')
                logging.info('Estoque sincronizado via admin.')
            except Exception as e:
                logging.error(f'Erro ao sincronizar estoque via admin: {e}')
                flash('Erro ao sincronizar estoque.', 'error')
        
        conn.commit()
        return redirect(url_for('admin')) # Redirect after POST to prevent re-submission

    # --- GET request logic ---
    # Stats
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations')
    total_reservations = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM waiting_list')
    waiting_count = c.fetchone()[0]
    stats = {'users': users_count, 'pending': pending_count, 'total_res': total_reservations, 'waiting': waiting_count}

    # User Filters
    user_email_filter = request.args.get('user_email_filter', '').strip()
    user_role_filter = request.args.get('user_role_filter', '').strip()
    query_users = 'SELECT id, name, email, phone, role FROM users WHERE 1=1'
    params_users = []
    if user_email_filter:
        query_users += ' AND email LIKE ?'
        params_users.append(f'%{user_email_filter}%')
    if user_role_filter:
        query_users += ' AND role = ?'
        params_users.append(user_role_filter)
    users = c.execute(query_users, params_users).fetchall()

    # Reservation Filters
    res_service_filter = request.args.get('res_service_filter', '').strip()
    res_email_filter = request.args.get('res_email_filter', '').strip()
    query_pending_res = '''
        SELECT r.id, u.name as user_name, u.email as user_email, r.service, r.quantity, r.created_at 
        FROM reservations r JOIN users u ON r.user_id = u.id 
        WHERE r.status = "pending"
    '''
    params_pending_res = []
    if res_service_filter:
        query_pending_res += ' AND r.service LIKE ?'
        params_pending_res.append(f'%{res_service_filter}%')
    if res_email_filter:
        query_pending_res += ' AND u.email LIKE ?'
        params_pending_res.append(f'%{res_email_filter}%')
    pending_reservations = c.execute(query_pending_res, params_pending_res).fetchall()

    # Waiting List
    query_waiting_list = '''
        SELECT wl.id, u.name as user_name, u.email as user_email, wl.service, wl.created_at 
        FROM waiting_list wl JOIN users u ON wl.user_id = u.id
    '''
    waiting_list = c.execute(query_waiting_list).fetchall()

    # Data for manual inserts
    all_users = c.execute('SELECT id, name, email FROM users ORDER BY name').fetchall()
    all_services = sorted(list(set([t['service'] for t in load_thumbnails()]))) # Get unique service names from thumbnails

    conn.close()
    
    return render_template_string(ADMIN_HTML, stats=stats, users=users, pending_reservations=pending_reservations, 
                                  all_users=all_users, all_services=all_services, waiting_list=waiting_list)

@app.route('/backup')
def backup():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row # Ensure rows are dict-like
    c = conn.cursor()
    
    users = c.execute('SELECT * FROM users').fetchall()
    reservations = c.execute('SELECT * FROM reservations').fetchall()
    stock = c.execute('SELECT * FROM stock').fetchall()
    waiting_list = c.execute('SELECT * FROM waiting_list').fetchall()
    
    conn.close()
    
    data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(row) for row in users],
        'reservations': [dict(row) for row in reservations],
        'stock': [dict(row) for row in stock],
        'waiting_list': [dict(row) for row in waiting_list]
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
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('''SELECT r.id, u.name as user_name, u.email, r.service, r.quantity, r.status, r.created_at, r.denied_reason 
                 FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC''')
    rows = c.fetchall()
    conn.close()
    
    si = io.StringIO()
    writer = csv.writer(si)
    
    writer.writerow(['ID Reserva', 'Nome Usuário', 'Email Usuário', 'Serviço', 'Quantidade', 'Status', 'Data Criação', 'Motivo Recusa'])
    for row in rows:
        writer.writerow([row['id'], row['user_name'], row['email'], row['service'], row['quantity'], row['status'], row['created_at'], row['denied_reason']])
    
    output = io.BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name=f'jgminis_reservations_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')

@app.route('/sync_stock')
def sync_stock_route():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    
    sync_stock_from_sheet()
    flash('Estoque sincronizado da planilha para o DB.', 'success')
    return redirect(url_for('admin'))

@app.errorhandler(404)
def page_not_found(e):
    flash('A página que você tentou acessar não foi encontrada.', 'error')
    return redirect(url_for('index')), 404

@app.errorhandler(500)
def internal_error(e):
    logging.error(f'Erro interno do servidor: {e}')
    flash('Ocorreu um erro interno no servidor. Tente novamente mais tarde.', 'error')
    return redirect(url_for('index')), 500

@app.route('/favicon.ico')
def favicon():
    return '', 204 # No favicon, return 204 No Content

if __name__ == '__main__':
    # init_db() # Already called once at module level
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
