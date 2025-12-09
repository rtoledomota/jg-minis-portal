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
                    # Normalize service name to lower for case-insensitive matching
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service.lower(), qty))
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
    # Store service names in lowercase for case-insensitive lookup
    stock_data = {row[0].lower(): row[1] for row in c.fetchall()} 
    
    # Get other details from Google Sheet
    if sheet:
        try:
            records = sheet.get_all_records()
            if not records:
                logging.warning("Planilha vazia - thumbnails fallback")
                return [{'service': 'Fallback', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': 'Adicione dados na planilha', 'marca': '', 'previsao': ''}]
            
            for record in records: # Process all records
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
                # Use normalized service name for lookup
                quantity = stock_data.get(service.lower(), 0) 

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
        .thumbnail { background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; text-align: center; transition: transform 0.3s ease, box-shadow 0.3s ease; position: relative; }
        .thumbnail:hover { transform: translateY(-5px); box-shadow: 0 6px 16px rgba(0,0,0,0.12); }
        .thumbnail img { width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #eee; transition: filter 0.3s ease; }
        .thumbnail.sold-out img { filter: grayscale(100%); }
        .sold-out-tag { position: absolute; top: 10px; left: 10px; background-color: #dc3545; color: white; padding: 5px 10px; border-radius: 5px; font-weight: bold; font-size: 0.8em; z-index: 10; }
        .thumbnail-content { padding: 15px; }
        .thumbnail h3 { font-size: 1.3em; color: #007bff; margin-top: 0; margin-bottom: 8px; }
        .thumbnail p { font-size: 0.95em; color: #555; margin-bottom: 5px; line-height: 1.4; }
        .thumbnail .price { font-size: 1.1em; font-weight: bold; color: #28a745; margin-top: 10px; }
        .thumbnail .quantity { font-size: 0.9em; color: #6c757d; margin-bottom: 15px; }
        .action-buttons { display: flex; justify-content: center; gap: 10px; margin-top: 15px; }
        .btn { display: inline-block; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; transition: background-color 0.3s ease, color 0.3s ease; }
        .btn-reserve { background-color: #28a745; color: white; border: none; }
        .btn-reserve:hover { background-color: #218838; }
        .btn-waiting-list { background-color: #ffc107; color: #333; border: none; }
        .btn-waiting-list:hover { background-color: #e0a800; }
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
            <div class="thumbnail {% if thumb.quantity == 0 %}sold-out{% endif %}">
                <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                {% if thumb.quantity == 0 %}<div class="sold-out-tag">ESGOTADO</div>{% endif %}
                <div class="thumbnail-content">
                    <h3>{{ thumb.service }}</h3>
                    <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
                    <p class="price">R$ {{ thumb.price }}</p>
                    <p class="quantity">Disponível: {{ thumb.quantity }}</p>
                    <div class="action-buttons">
                        {% if thumb.quantity > 0 %}
                            <a href="{{ url_for('reserve_single', service=thumb.service) }}" class="btn btn-reserve">Reservar Agora</a>
                        {% else %}
                            <form action="{{ url_for('add_to_waiting_list') }}" method="post" style="display:inline;">
                                <input type="hidden" name="service" value="{{ thumb.service }}">
                                <button type="submit" class="btn btn-waiting-list">FILA DE ESPERA</button>
                            </form>
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
        .form-group { margin-bottom: 15px; text-align: left; }
        label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
        input[type="text"], input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; }
        button { width: 100%; padding: 12px; background-color: #28a745; color: white; border: none; border-radius: 5px; font-size: 1.1em; font-weight: bold; cursor: pointer; transition: background-color 0.3s ease; margin-top: 20px; }
        button:hover { background-color: #218838; }
        .link-text { margin-top: 20px; font-size: 0.95em; }
        .link-text a { color: #007bff; text-decoration: none; transition: color 0.3s ease; }
        .link-text a:hover { color: #0056b3; text-decoration: underline; }
        .flash-message { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 15px; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="register-container">
        <h1>Registrar</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                <div class="flash-message">
                    {% for message in messages %}
                        <p>{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        <form method="post">
            <div class="form-group">
                <label for="name">Nome:</label>
                <input type="text" id="name" name="name" required>
            </div>
            <div class="form-group">
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div class="form-group">
                <label for="phone">Telefone (apenas números):</label>
                <input type="text" id="phone" name="phone" required pattern="[0-9]{10,11}" title="Telefone deve conter 10 ou 11 dígitos numéricos">
            </div>
            <div class="form-group">
                <label for="password">Senha (mín. 6 caracteres):</label>
                <input type="password" id="password" name="password" required minlength="6">
            </div>
            <button type="submit">Registrar</button>
        </form>
        <p class="link-text">Já tem conta? <a href="{{ url_for('login') }}">Fazer Login</a></p>
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
        .form-group { margin-bottom: 15px; text-align: left; }
        label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
        input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; }
        button { width: 100%; padding: 12px; background-color: #007bff; color: white; border: none; border-radius: 5px; font-size: 1.1em; font-weight: bold; cursor: pointer; transition: background-color 0.3s ease; margin-top: 20px; }
        button:hover { background-color: #0056b3; }
        .link-text { margin-top: 20px; font-size: 0.95em; }
        .link-text a { color: #28a745; text-decoration: none; transition: color 0.3s ease; }
        .link-text a:hover { color: #218838; text-decoration: underline; }
        .flash-message { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 15px; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Login</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                <div class="flash-message">
                    {% for message in messages %}
                        <p>{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        <form method="post">
            <div class="form-group">
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div class="form-group">
                <label for="password">Senha:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit">Entrar</button>
        </form>
        <p class="link-text">Não tem conta? <a href="{{ url_for('register') }}">Registrar</a></p>
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
        h2 { color: #007bff; margin-top: 30px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .user-info p { margin-bottom: 8px; font-size: 1.05em; }
        .user-info strong { color: #555; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #f8f8f8; color: #555; font-weight: bold; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .status-pending { color: #ffc107; font-weight: bold; }
        .status-approved { color: #28a745; font-weight: bold; }
        .status-denied { color: #dc3545; font-weight: bold; }
        .no-reservations { text-align: center; color: #6c757d; margin-top: 20px; }
        .back-link { display: block; text-align: center; margin-top: 30px; font-size: 1.05em; }
        .back-link a { color: #007bff; text-decoration: none; transition: color 0.3s ease; }
        .back-link a:hover { color: #0056b3; text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Meu Perfil</h1>
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
                        <th>Serviço</th>
                        <th>Quantidade</th>
                        <th>Status</th>
                        <th>Data da Reserva</th>
                        <th>Motivo Rejeição</th>
                    </tr>
                </thead>
                <tbody>
                    {% for res in reservations %}
                        <tr>
                            <td>{{ res.service }}</td>
                            <td>{{ res.quantity }}</td>
                            <td class="status-{{ res.status }}">{{ res.status.capitalize() }}</td>
                            <td>{{ res.created_at }}</td>
                            <td>{{ res.denied_reason if res.denied_reason else '-' }}</td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <p class="no-reservations">Você ainda não fez nenhuma reserva.</p>
        {% endif %}
        <div class="back-link">
            <a href="{{ url_for('index') }}">Voltar para Home</a>
        </div>
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
        .reserve-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 100%; max-width: 500px; text-align: center; }
        h1 { color: #004085; margin-bottom: 25px; font-size: 1.8em; }
        .thumbnail-details img { max-width: 80%; height: auto; border-radius: 5px; margin-bottom: 15px; }
        .thumbnail-details p { margin-bottom: 8px; font-size: 1.05em; }
        .thumbnail-details strong { color: #555; }
        .form-group { margin-bottom: 20px; text-align: left; }
        label { display: block; margin-bottom: 8px; font-weight: bold; color: #555; font-size: 1.1em; }
        input[type="number"] { width: calc(100% - 20px); padding: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; }
        .action-buttons { display: flex; justify-content: center; gap: 15px; margin-top: 20px; }
        .btn { padding: 12px 25px; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 1.1em; cursor: pointer; transition: background-color 0.3s ease, color 0.3s ease; border: none; }
        .btn-reserve { background-color: #28a745; color: white; }
        .btn-reserve:hover { background-color: #218838; }
        .btn-waiting-list { background-color: #ffc107; color: #333; }
        .btn-waiting-list:hover { background-color: #e0a800; }
        .back-link { margin-top: 20px; font-size: 1em; }
        .back-link a { color: #007bff; text-decoration: none; transition: color 0.3s ease; }
        .back-link a:hover { color: #0056b3; text-decoration: underline; }
        .flash-message { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 15px; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="reserve-container">
        <h1>Reservar {{ thumb.service }}</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                <div class="flash-message">
                    {% for message in messages %}
                        <p>{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        <div class="thumbnail-details">
            <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
            <p><strong>Marca:</strong> {{ thumb.marca }}</p>
            <p><strong>Observações:</strong> {{ thumb.obs }}</p>
            <p><strong>Preço:</strong> R$ {{ thumb.price }}</p>
            <p><strong>Disponível:</strong> {{ thumb.quantity }}</p>
        </div>

        {% if thumb.quantity > 0 %}
            <form method="post">
                <div class="form-group">
                    <label for="quantity">Quantidade a reservar:</label>
                    <input type="number" id="quantity" name="quantity" min="1" max="{{ thumb.quantity }}" value="1" required>
                </div>
                <div class="action-buttons">
                    <button type="submit" class="btn btn-reserve">Confirmar Reserva</button>
                </div>
            </form>
        {% else %}
            <p>Esta miniatura está esgotada no momento.</p>
            <div class="action-buttons">
                <form action="{{ url_for('add_to_waiting_list') }}" method="post" style="display:inline;">
                    <input type="hidden" name="service" value="{{ thumb.service }}">
                    <button type="submit" class="btn btn-waiting-list">FILA DE ESPERA</button>
                </form>
            </div>
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
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 1200px; margin: 20px auto; }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        .filters { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 30px; padding: 15px; background-color: #f8f8f8; border-radius: 8px; }
        .filters label { font-weight: bold; color: #555; display: flex; align-items: center; gap: 5px; }
        .filters input[type="text"], .filters select { padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.95em; }
        .filters button { padding: 8px 15px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .filters button:hover { background-color: #0056b3; }
        .miniature-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 25px; margin-top: 20px; }
        .miniature-item { background-color: #fdfdfd; border: 1px solid #eee; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); overflow: hidden; text-align: center; padding: 15px; position: relative; }
        .miniature-item.sold-out { opacity: 0.7; }
        .miniature-item img { width: 100%; height: 150px; object-fit: cover; border-radius: 5px; margin-bottom: 10px; transition: filter 0.3s ease; }
        .miniature-item.sold-out img { filter: grayscale(100%); }
        .sold-out-tag { position: absolute; top: 10px; left: 10px; background-color: #dc3545; color: white; padding: 5px 10px; border-radius: 5px; font-weight: bold; font-size: 0.8em; z-index: 10; }
        .miniature-item h3 { font-size: 1.2em; color: #007bff; margin-bottom: 5px; }
        .miniature-item p { font-size: 0.9em; color: #555; margin-bottom: 5px; }
        .miniature-item .price { font-weight: bold; color: #28a745; }
        .miniature-item .quantity-input { width: 80px; padding: 5px; border: 1px solid #ccc; border-radius: 4px; text-align: center; margin-top: 10px; }
        .action-buttons { display: flex; justify-content: center; gap: 10px; margin-top: 15px; }
        .btn { padding: 8px 15px; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 0.95em; cursor: pointer; transition: background-color 0.3s ease; border: none; }
        .btn-waiting-list { background-color: #ffc107; color: #333; }
        .btn-waiting-list:hover { background-color: #e0a800; }
        .submit-all-btn { display: block; width: fit-content; margin: 30px auto 0; padding: 12px 30px; background-color: #28a745; color: white; border: none; border-radius: 5px; font-size: 1.1em; font-weight: bold; cursor: pointer; transition: background-color 0.3s ease; }
        .submit-all-btn:hover { background-color: #218838; }
        .flash-message { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 15px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Reservar Múltiplas Miniaturas</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                <div class="flash-message">
                    {% for message in messages %}
                        <p>{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}

        <div class="filters">
            <form method="get" action="{{ url_for('reservar') }}">
                <label><input type="checkbox" name="available" value="1" {% if request.args.get('available') %}checked{% endif %}> Disponíveis</label>
                <label>Ordenar por:
                    <select name="order_by">
                        <option value="">Padrão</option>
                        <option value="service_asc" {% if request.args.get('order_by') == 'service_asc' %}selected{% endif %}>Nome (A-Z)</option>
                        <option value="service_desc" {% if request.args.get('order_by') == 'service_desc' %}selected{% endif %}>Nome (Z-A)</option>
                        <option value="price_asc" {% if request.args.get('order_by') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                        <option value="price_desc" {% if request.args.get('order_by') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                    </select>
                </label>
                <label>Previsão de Chegada: <input type="text" name="previsao" value="{{ request.args.get('previsao', '') }}"></label>
                <label>Marca: <input type="text" name="marca" value="{{ request.args.get('marca', '') }}"></label>
                <button type="submit">Filtrar</button>
            </form>
        </div>

        <form method="post" action="{{ url_for('reservar') }}">
            <div class="miniature-grid">
                {% for thumb in thumbnails %}
                    <div class="miniature-item {% if thumb.quantity == 0 %}sold-out{% endif %}">
                        <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                        {% if thumb.quantity == 0 %}<div class="sold-out-tag">ESGOTADO</div>{% endif %}
                        <h3>{{ thumb.service }}</h3>
                        <p>{{ thumb.marca }}</p>
                        <p class="price">R$ {{ thumb.price }}</p>
                        <p>Disponível: {{ thumb.quantity }}</p>
                        {% if thumb.quantity > 0 %}
                            <label for="qty_{{ loop.index }}">Reservar:</label>
                            <input type="number" id="qty_{{ loop.index }}" name="quantity_{{ thumb.service }}" min="0" max="{{ thumb.quantity }}" value="0" class="quantity-input">
                        {% else %}
                            <div class="action-buttons">
                                <form action="{{ url_for('add_to_waiting_list') }}" method="post" style="display:inline;">
                                    <input type="hidden" name="service" value="{{ thumb.service }}">
                                    <button type="submit" class="btn btn-waiting-list">FILA DE ESPERA</button>
                                </form>
                            </div>
                        {% endif %}
                    </div>
                {% endfor %}
            </div>
            {% if thumbnails %}
                <button type="submit" class="submit-all-btn">Confirmar Reservas Selecionadas</button>
            {% endif %}
        </form>
        <p style="text-align: center; margin-top: 20px;"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
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
    <title>Painel Admin - JG MINIS</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 1200px; margin: 20px auto; }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 30px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background-color: #e9f5ff; padding: 20px; border-radius: 8px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
        .stat-card h3 { color: #0056b3; margin-top: 0; margin-bottom: 10px; font-size: 1.2em; }
        .stat-card p { font-size: 1.8em; font-weight: bold; color: #007bff; margin: 0; }
        .admin-actions { text-align: center; margin-bottom: 30px; }
        .admin-actions button, .admin-actions a { display: inline-block; padding: 10px 20px; margin: 5px; border-radius: 5px; text-decoration: none; font-weight: bold; cursor: pointer; transition: background-color 0.3s ease; border: none; }
        .btn-sync { background-color: #ffc107; color: #333; }
        .btn-sync:hover { background-color: #e0a800; }
        .btn-backup { background-color: #17a2b8; color: white; }
        .btn-backup:hover { background-color: #138496; }
        .btn-export { background-color: #28a745; color: white; }
        .btn-export:hover { background-color: #218838; }
        .filters-form { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 20px; padding: 15px; background-color: #f8f8f8; border-radius: 8px; }
        .filters-form label { font-weight: bold; color: #555; display: flex; align-items: center; gap: 5px; }
        .filters-form input[type="text"], .filters-form select { padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.95em; }
        .filters-form button { padding: 8px 15px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .filters-form button:hover { background-color: #0056b3; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; font-size: 0.9em; }
        th { background-color: #f8f8f8; color: #555; font-weight: bold; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .action-form { display: inline-block; margin-right: 5px; }
        .action-form button { padding: 6px 10px; border-radius: 4px; font-size: 0.85em; cursor: pointer; border: none; }
        .btn-promote { background-color: #007bff; color: white; }
        .btn-promote:hover { background-color: #0056b3; }
        .btn-demote { background-color: #6c757d; color: white; }
        .btn-demote:hover { background-color: #5a6268; }
        .btn-delete { background-color: #dc3545; color: white; }
        .btn-delete:hover { background-color: #c82333; }
        .btn-approve { background-color: #28a745; color: white; }
        .btn-approve:hover { background-color: #218838; }
        .btn-deny { background-color: #ffc107; color: #333; }
        .btn-deny:hover { background-color: #e0a800; }
        .form-section { background-color: #fdfdfd; padding: 25px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); margin-top: 30px; }
        .form-section form { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }
        .form-section label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
        .form-section input[type="text"], .form-section input[type="number"], .form-section input[type="url"], .form-section select { width: calc(100% - 20px); padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.95em; }
        .form-section button { padding: 10px 20px; background-color: #007bff; color: white; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; transition: background-color 0.3s ease; margin-top: 15px; }
        .form-section button:hover { background-color: #0056b3; }
        .form-section .full-width { grid-column: 1 / -1; }
        .flash-message { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 15px; text-align: center; }
        .back-link { display: block; text-align: center; margin-top: 30px; font-size: 1.05em; }
        .back-link a { color: #007bff; text-decoration: none; transition: color 0.3s ease; }
        .back-link a:hover { color: #0056b3; text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Painel Administrativo</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                <div class="flash-message">
                    {% for message in messages %}
                        <p>{{ message }}</p>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}

        <div class="stats-grid">
            <div class="stat-card"><h3>Usuários Registrados</h3><p>{{ stats.users }}</p></div>
            <div class="stat-card"><h3>Reservas Pendentes</h3><p>{{ stats.pending_reservations }}</p></div>
            <div class="stat-card"><h3>Total de Reservas</h3><p>{{ stats.total_reservations }}</p></div>
            <div class="stat-card"><h3>Itens em Fila</h3><p>{{ stats.waiting_list_count }}</p></div>
        </div>

        <div class="admin-actions">
            <form method="post" style="display:inline-block;">
                <input type="hidden" name="action" value="sync_stock">
                <button type="submit" class="btn-sync">Sincronizar Estoque da Planilha</button>
            </form>
            <a href="{{ url_for('backup_db') }}" class="btn btn-backup">Backup DB (JSON)</a>
            <a href="{{ url_for('export_csv') }}" class="btn btn-export">Export Reservas (CSV)</a>
        </div>

        <h2>Gerenciar Usuários</h2>
        <div class="filters-form">
            <form method="get" action="{{ url_for('admin') }}">
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
        </div>
        <table>
            <thead>
                <tr><th>ID</th><th>Nome</th><th>Email</th><th>Telefone</th><th>Role</th><th>Membro Desde</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for user in users %}
                    <tr>
                        <td>{{ user.id }}</td>
                        <td>{{ user.name }}</td>
                        <td>{{ user.email }}</td>
                        <td>{{ user.phone }}</td>
                        <td>{{ user.role }}</td>
                        <td>{{ user.data_cadastro }}</td>
                        <td>
                            {% if user.role != 'admin' %}
                                <form method="post" class="action-form">
                                    <input type="hidden" name="action" value="promote_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-promote">Promover</button>
                                </form>
                            {% else %}
                                <form method="post" class="action-form">
                                    <input type="hidden" name="action" value="demote_user">
                                    <input type="hidden" name="user_id" value="{{ user.id }}">
                                    <button type="submit" class="btn-demote">Rebaixar</button>
                                </form>
                            {% endif %}
                            <form method="post" class="action-form" onsubmit="return confirm('Tem certeza que deseja deletar este usuário e todas as suas reservas?');">
                                <input type="hidden" name="action" value="delete_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="btn-delete">Deletar</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Gerenciar Reservas</h2>
        <div class="filters-form">
            <form method="get" action="{{ url_for('admin') }}">
                <label>Serviço/Email: <input type="text" name="res_search_filter" value="{{ request.args.get('res_search_filter', '') }}"></label>
                <label>Status:
                    <select name="res_status_filter">
                        <option value="">Todos</option>
                        <option value="pending" {% if request.args.get('res_status_filter') == 'pending' %}selected{% endif %}>Pendente</option>
                        <option value="approved" {% if request.args.get('res_status_filter') == 'approved' %}selected{% endif %}>Aprovada</option>
                        <option value="denied" {% if request.args.get('res_status_filter') == 'denied' %}selected{% endif %}>Rejeitada</option>
                    </select>
                </label>
                <button type="submit">Filtrar Reservas</button>
            </form>
        </div>
        <table>
            <thead>
                <tr><th>ID</th><th>Usuário</th><th>Serviço</th><th>Quantidade</th><th>Status</th><th>Data Reserva</th><th>Motivo Rejeição</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for res in reservations %}
                    <tr>
                        <td>{{ res.id }}</td>
                        <td>{{ res.user_email }}</td>
                        <td>{{ res.service }}</td>
                        <td>{{ res.quantity }}</td>
                        <td>{{ res.status.capitalize() }}</td>
                        <td>{{ res.created_at }}</td>
                        <td>{{ res.denied_reason if res.denied_reason else '-' }}</td>
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
                                    <button type="submit" class="btn-deny">Rejeitar</button>
                                </form>
                            {% endif %}
                            <form method="post" class="action-form" onsubmit="return confirm('Tem certeza que deseja deletar esta reserva?');">
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
                <tr><th>ID</th><th>Usuário</th><th>Serviço</th><th>Data Entrada</th><th>Ações</th></tr>
            </thead>
            <tbody>
                {% for item in waiting_list %}
                    <tr>
                        <td>{{ item.id }}</td>
                        <td>{{ item.user_email }}</td>
                        <td>{{ item.service }}</td>
                        <td>{{ item.created_at }}</td>
                        <td>
                            <form method="post" class="action-form" onsubmit="return confirm('Notificar usuário e remover da fila?');">
                                <input type="hidden" name="action" value="notify_waiting_list">
                                <input type="hidden" name="item_id" value="{{ item.id }}">
                                <button type="submit" class="btn-approve">Notificar</button>
                            </form>
                            <form method="post" class="action-form" onsubmit="return confirm('Remover da fila?');">
                                <input type="hidden" name="action" value="delete_waiting_list">
                                <input type="hidden" name="item_id" value="{{ item.id }}">
                                <button type="submit" class="btn-delete">Remover</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="form-section">
            <h2>Inserir Nova Miniatura</h2>
            <form method="post">
                <input type="hidden" name="action" value="insert_miniature">
                <label>Serviço: <input type="text" name="service" required></label>
                <label>Marca: <input type="text" name="marca" required></label>
                <label>Observações: <input type="text" name="obs"></label>
                <label>Preço: <input type="number" name="price" step="0.01" required></label>
                <label>Quantidade Inicial: <input type="number" name="quantity" required min="0"></label>
                <label>Imagem URL: <input type="url" name="image" required></label>
                <label>Previsão de Chegada: <input type="text" name="previsao"></label>
                <div class="full-width"><button type="submit">Inserir Miniatura</button></div>
            </form>
        </div>

        <div class="form-section">
            <h2>Inserir Nova Reserva</h2>
            <form method="post">
                <input type="hidden" name="action" value="insert_reservation">
                <label>Usuário:
                    <select name="user_id" required>
                        <option value="">Selecione um Usuário</option>
                        {% for u in all_users %}<option value="{{ u.id }}">{{ u.email }}</option>{% endfor %}
                    </select>
                </label>
                <label>Serviço:
                    <select name="service" required>
                        <option value="">Selecione um Serviço</option>
                        {% for s in all_services %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
                    </select>
                </label>
                <label>Quantidade: <input type="number" name="quantity" required min="1"></label>
                <label>Status:
                    <select name="status">
                        <option value="pending">Pendente</option>
                        <option value="approved">Aprovada</option>
                        <option value="denied">Rejeitada</option>
                    </select>
                </label>
                <label>Motivo (se rejeitada): <input type="text" name="reason"></label>
                <div class="full-width"><button type="submit">Criar Reserva</button></div>
            </form>
        </div>
        
        <p class="back-link"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
    </div>
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
            flash('Telefone inválido (deve conter 10 ou 11 dígitos numéricos).', 'error')
            return render_template_string(REGISTER_HTML)
        if len(password) < 6:
            flash('Senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML)
        
        hashed_pw = generate_password_hash(password)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)', (name, email, phone, hashed_pw))
            conn.commit()
            logging.info(f'Novo usuário registrado: {email}')
            flash('Registro realizado com sucesso! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email já cadastrado. Por favor, use outro email ou faça login.', 'error')
        except Exception as e:
            logging.error(f'Erro ao registrar usuário {email}: {e}')
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
        c.execute('SELECT id, password, role, email FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[1], password):
            session['user_id'] = user[0]
            session['role'] = user[2]
            session['email'] = user[3]
            logging.info(f'Login bem-sucedido para: {email}')
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Email ou senha inválidos. Tente novamente.', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    user_email = session.get('email', 'usuário desconhecido')
    session.clear()
    logging.info(f'Logout de: {user_email}')
    flash('Você foi desconectado.', 'success')
    return redirect(url_for('index'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Você precisa estar logado para acessar esta página.', 'error')
        return redirect(url_for('login'))

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Get user details
    c.execute('SELECT name, email, phone, data_cadastro FROM users WHERE id = ?', (session['user_id'],))
    user_data = c.fetchone()
    user = {
        'name': user_data[0],
        'email': user_data[1],
        'phone': user_data[2],
        'data_cadastro': user_data[3]
    }

    # Get user reservations
    c.execute('''SELECT service, quantity, status, denied_reason, created_at 
                 FROM reservations WHERE user_id = ? ORDER BY created_at DESC''', (session['user_id'],))
    reservations = []
    for res in c.fetchall():
        reservations.append({
            'service': res[0],
            'quantity': res[1],
            'status': res[2],
            'denied_reason': res[3],
            'created_at': res[4]
        })
    conn.close()
    
    return render_template_string(PROFILE_HTML, user=user, reservations=reservations)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar miniaturas.', 'error')
        return redirect(url_for('login'))

    all_thumbnails = load_thumbnails()
    
    if request.method == 'GET':
        # Apply filters
        filtered_thumbnails = all_thumbnails
        
        if request.args.get('available'):
            filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
        
        previsao_filter = request.args.get('previsao', '').lower()
        if previsao_filter:
            filtered_thumbnails = [t for t in filtered_thumbnails if previsao_filter in t['previsao'].lower()]
            
        marca_filter = request.args.get('marca', '').lower()
        if marca_filter:
            filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]
            
        order_by = request.args.get('order_by', '')
        if order_by == 'service_asc':
            filtered_thumbnails.sort(key=lambda x: x['service'].lower())
        elif order_by == 'service_desc':
            filtered_thumbnails.sort(key=lambda x: x['service'].lower(), reverse=True)
        elif order_by == 'price_asc':
            filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')))
        elif order_by == 'price_desc':
            filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')), reverse=True)

        return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)
    
    else: # POST request for reservations
        user_id = session['user_id']
        reservations_made = 0
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()

        for thumb in all_thumbnails:
            service = thumb['service']
            # Get quantity from form for this service
            quantity_key = f'quantity_{service}'
            quantity_to_reserve = int(request.form.get(quantity_key, 0))

            if quantity_to_reserve > 0:
                # Check current stock in DB
                c.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),)) # Use lower for lookup
                current_stock_row = c.fetchone()
                current_stock = current_stock_row[0] if current_stock_row else 0

                if quantity_to_reserve <= current_stock:
                    # Make reservation
                    c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                              (user_id, service, quantity_to_reserve))
                    # Update stock
                    c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', 
                              (quantity_to_reserve, service.lower())) # Use lower for update
                    reservations_made += 1
                    logging.info(f'Reserva de {quantity_to_reserve}x {service} feita por user {user_id}')
                else:
                    flash(f'Não foi possível reservar {quantity_to_reserve}x {service}. Estoque insuficiente (disponível: {current_stock}).', 'error')
        
        conn.commit()
        conn.close()

        if reservations_made > 0:
            flash(f'{reservations_made} reserva(s) realizada(s) com sucesso!', 'success')
            return redirect(url_for('profile'))
        else:
            flash('Nenhuma reserva foi feita. Verifique as quantidades e o estoque.', 'error')
            return redirect(url_for('reservar'))

@app.route('/reserve_single', methods=['GET', 'POST'])
def reserve_single():
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar miniaturas.', 'error')
        return redirect(url_for('login'))
    
    service_name = request.args.get('service')
    if not service_name:
        flash('Serviço não especificado.', 'error')
        return redirect(url_for('index'))

    # Find thumbnail details
    all_thumbnails = load_thumbnails()
    thumb = next((t for t in all_thumbnails if t['service'] == service_name), None)
    
    if not thumb:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('index'))

    # Get current stock from DB
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name.lower(),)) # Use lower for lookup
    stock_row = c.fetchone()
    current_stock = stock_row[0] if stock_row else 0
    conn.close()

    thumb['quantity'] = current_stock # Ensure UI shows current stock

    if request.method == 'POST':
        quantity_to_reserve = int(request.form.get('quantity', 0))

        if quantity_to_reserve <= 0:
            flash('Quantidade inválida. Deve ser pelo menos 1.', 'error')
        elif quantity_to_reserve > current_stock:
            flash(f'Quantidade insuficiente no estoque. Disponível: {current_stock}.', 'error')
        else:
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            try:
                c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                          (session['user_id'], service_name, quantity_to_reserve))
                c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', 
                          (quantity_to_reserve, service_name.lower())) # Use lower for update
                conn.commit()
                logging.info(f'Reserva de {quantity_to_reserve}x {service_name} feita por user {session["user_id"]}')
                flash(f'{quantity_to_reserve}x {service_name} reservada(s) com sucesso!', 'success')
                return redirect(url_for('profile'))
            except Exception as e:
                logging.error(f'Erro ao fazer reserva para {service_name}: {e}')
                flash('Ocorreu um erro ao processar sua reserva. Tente novamente.', 'error')
            finally:
                conn.close()
    
    return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/add_to_waiting_list', methods=['POST'])
def add_to_waiting_list():
    if 'user_id' not in session:
        flash('Você precisa estar logado para entrar na fila de espera.', 'error')
        return redirect(url_for('login'))
    
    service = request.form.get('service')
    user_id = session['user_id']
    user_email = session.get('email', 'anônimo')

    if not service:
        flash('Serviço não especificado para a fila de espera.', 'error')
        return redirect(url_for('index'))

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    try:
        # Check if user is already in waiting list for this service
        c.execute('SELECT id FROM waiting_list WHERE user_id = ? AND service = ?', (user_id, service))
        if c.fetchone():
            flash(f'Você já está na fila de espera para {service}.', 'info')
        else:
            c.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (user_id, service))
            conn.commit()
            logging.info(f'Usuário {user_email} adicionado à fila de espera para {service}')
            flash(f'Você foi adicionado à fila de espera para {service}!', 'success')
            # Redirect to WhatsApp with pre-filled message
            whatsapp_message = f"Olá, fui adicionado à fila de espera para {service}. Meu email é {user_email}."
            return redirect(f"https://wa.me/{WHATSAPP_NUMBER}?text={whatsapp_message}")
    except Exception as e:
        logging.error(f'Erro ao adicionar user {user_id} à fila para {service}: {e}')
        flash('Ocorreu um erro ao adicionar você à fila de espera. Tente novamente.', 'error')
    finally:
        conn.close()
    
    return redirect(url_for('index')) # Fallback redirect

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
            user_id = request.form.get('user_id')
            c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
            flash('Usuário promovido a administrador.', 'success')
        elif action == 'demote_user':
            user_id = request.form.get('user_id')
            c.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
            flash('Usuário rebaixado para user.', 'success')
        elif action == 'delete_user':
            user_id = request.form.get('user_id')
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,))
            c.execute('DELETE FROM waiting_list WHERE user_id = ?', (user_id,))
            flash('Usuário e todas as suas reservas/filas deletados.', 'success')
        elif action == 'approve_res':
            res_id = request.form.get('res_id')
            c.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
            flash('Reserva aprovada.', 'success')
        elif action == 'deny_res':
            res_id = request.form.get('res_id')
            reason = request.form.get('reason', 'Motivo não especificado.')
            c.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
            flash('Reserva rejeitada.', 'success')
        elif action == 'delete_res':
            res_id = request.form.get('res_id')
            # Get quantity from reservation before deleting to restore stock
            c.execute('SELECT service, quantity FROM reservations WHERE id = ?', (res_id,))
            res_data = c.fetchone()
            if res_data:
                service_name = res_data[0]
                quantity_reserved = res_data[1]
                c.execute('UPDATE stock SET quantity = quantity + ? WHERE service = ?', (quantity_reserved, service_name.lower())) # Restore stock
                logging.info(f'Estoque de {service_name} restaurado em {quantity_reserved} após exclusão da reserva {res_id}')
            c.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
            flash('Reserva deletada e estoque restaurado.', 'success')
        elif action == 'insert_miniature':
            service = request.form.get('service')
            marca = request.form.get('marca')
            obs = request.form.get('obs')
            price = float(request.form.get('price', 0))
            quantity = int(request.form.get('quantity', 0))
            image = request.form.get('image')
            previsao = request.form.get('previsao')
            
            # Insert into stock table (case-insensitive service name)
            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service.lower(), quantity))
            # Note: This does NOT update the Google Sheet. Sheet is primary source for other details.
            flash(f'Miniatura "{service}" adicionada/atualizada no estoque local.', 'success')
        elif action == 'insert_reservation':
            user_id = request.form.get('user_id')
            service = request.form.get('service')
            quantity = int(request.form.get('quantity', 0))
            status = request.form.get('status', 'pending')
            reason = request.form.get('reason', '')

            if not user_id or not service or quantity <= 0:
                flash('Dados inválidos para nova reserva.', 'error')
            else:
                # Check stock before inserting if status is approved
                if status == 'approved':
                    c.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),))
                    current_stock = c.fetchone()
                    if not current_stock or current_stock[0] < quantity:
                        flash(f'Estoque insuficiente para aprovar {quantity}x {service}. Disponível: {current_stock[0] if current_stock else 0}.', 'error')
                        conn.commit() # Commit any previous changes
                        return redirect(url_for('admin')) # Redirect to avoid further processing
                    c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
                
                c.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)', 
                          (user_id, service, quantity, status, reason))
                flash('Nova reserva criada.', 'success')
        elif action == 'sync_stock':
            if sheet:
                try:
                    records = sheet.get_all_records()
                    for record in records:
                        service = record.get('NOME DA MINIATURA', '')
                        qty = record.get('QUANTIDADE DISPONIVEL', 0)
                        if service:
                            # Normalize service name to lower for case-insensitive matching
                            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service.lower(), qty))
                    flash('Estoque sincronizado da planilha para o DB!', 'success')
                    logging.info('Estoque sincronizado via admin.')
                except Exception as e:
                    logging.error(f'Erro na sincronização de estoque via admin: {e}')
                    flash('Erro na sincronização do estoque.', 'error')
            else:
                flash('Planilha não configurada ou inacessível.', 'error')
        elif action == 'notify_waiting_list':
            item_id = request.form.get('item_id')
            c.execute('SELECT wl.service, u.email FROM waiting_list wl JOIN users u ON wl.user_id = u.id WHERE wl.id = ?', (item_id,))
            item = c.fetchone()
            if item:
                service_name = item[0]
                user_email = item[1]
                logging.info(f'Notificação simulada para {user_email} sobre {service_name} da fila de espera.')
                flash(f'Notificação simulada enviada para {user_email} sobre {service_name}.', 'success')
                c.execute('DELETE FROM waiting_list WHERE id = ?', (item_id,))
            else:
                flash('Item da fila de espera não encontrado.', 'error')
        elif action == 'delete_waiting_list':
            item_id = request.form.get('item_id')
            c.execute('DELETE FROM waiting_list WHERE id = ?', (item_id,))
            flash('Item removido da fila de espera.', 'success')

        conn.commit()

    # --- Fetch Data for Admin Page ---
    # Stats
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_reservations_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations')
    total_reservations_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM waiting_list')
    waiting_list_count = c.fetchone()[0]
    stats = {
        'users': users_count,
        'pending_reservations': pending_reservations_count,
        'total_reservations': total_reservations_count,
        'waiting_list_count': waiting_list_count
    }

    # Users (with filters)
    user_email_filter = request.args.get('user_email_filter', '').lower()
    user_role_filter = request.args.get('user_role_filter', '')
    user_query = 'SELECT id, name, email, phone, role, data_cadastro FROM users WHERE 1=1'
    user_params = []
    if user_email_filter:
        user_query += ' AND email LIKE ?'
        user_params.append(f'%{user_email_filter}%')
    if user_role_filter:
        user_query += ' AND role = ?'
        user_params.append(user_role_filter)
    users = c.execute(user_query, user_params).fetchall()
    users_list = []
    for u in users:
        users_list.append({
            'id': u[0], 'name': u[1], 'email': u[2], 'phone': u[3], 'role': u[4], 'data_cadastro': u[5]
        })

    # Reservations (with filters)
    res_search_filter = request.args.get('res_search_filter', '').lower()
    res_status_filter = request.args.get('res_status_filter', '')
    res_query = '''
        SELECT r.id, u.email, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
        FROM reservations r JOIN users u ON r.user_id = u.id WHERE 1=1
    '''
    res_params = []
    if res_search_filter:
        res_query += ' AND (r.service LIKE ? OR u.email LIKE ?)'
        res_params.extend([f'%{res_search_filter}%', f'%{res_search_filter}%'])
    if res_status_filter:
        res_query += ' AND r.status = ?'
        res_params.append(res_status_filter)
    res_query += ' ORDER BY r.created_at DESC'
    reservations = c.execute(res_query, res_params).fetchall()
    reservations_list = []
    for r in reservations:
        reservations_list.append({
            'id': r[0], 'user_email': r[1], 'service': r[2], 'quantity': r[3], 
            'status': r[4], 'denied_reason': r[5], 'created_at': r[6]
        })

    # Waiting List
    c.execute('''SELECT wl.id, u.email, wl.service, wl.created_at 
                 FROM waiting_list wl JOIN users u ON wl.user_id = u.id ORDER BY wl.created_at ASC''')
    waiting_list_items = []
    for item in c.fetchall():
        waiting_list_items.append({
            'id': item[0], 'user_email': item[1], 'service': item[2], 'created_at': item[3]
        })

    # Data for 'Insert New Reservation' form
    all_users = c.execute('SELECT id, email FROM users ORDER BY email').fetchall()
    all_services_raw = c.execute('SELECT service FROM stock ORDER BY service').fetchall()
    all_services = [s[0] for s in all_services_raw] # List of service names

    conn.close()
    
    return render_template_string(ADMIN_HTML, 
                                  stats=stats, 
                                  users=users_list, 
                                  reservations=reservations_list, 
                                  waiting_list=waiting_list_items,
                                  all_users=all_users, 
                                  all_services=all_services,
                                  request=request) # Pass request to access args in template

@app.route('/backup_db')
def backup_db():
    if session.get('role') != 'admin':
        abort(403)
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # Fetch all data
    users = c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users').fetchall()
    reservations = c.execute('SELECT id, user_id, service, quantity, status, approved_by, denied_reason, created_at FROM reservations').fetchall()
    stock = c.execute('SELECT id, service, quantity, last_sync FROM stock').fetchall()
    waiting_list = c.execute('SELECT id, user_id, service, created_at FROM waiting_list').fetchall()
    
    conn.close()

    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(zip(['id', 'name', 'email', 'phone', 'role', 'data_cadastro'], u)) for u in users],
        'reservations': [dict(zip(['id', 'user_id', 'service', 'quantity', 'status', 'approved_by', 'denied_reason', 'created_at'], r)) for r in reservations],
        'stock': [dict(zip(['id', 'service', 'quantity', 'last_sync'], s)) for s in stock],
        'waiting_list': [dict(zip(['id', 'user_id', 'service', 'created_at'], wl)) for wl in waiting_list]
    }

    json_data = json.dumps(backup_data, indent=4, ensure_ascii=False)
    
    buffer = io.BytesIO()
    buffer.write(json_data.encode('utf-8'))
    buffer.seek(0)
    
    filename = f"jgminis_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    logging.info(f'Backup DB gerado: {filename}')
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/json')

@app.route('/export_csv')
def export_csv():
    if session.get('role') != 'admin':
        abort(403)
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Join reservations with user details
    rows = c.execute('''
        SELECT r.id, u.name, u.email, u.phone, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
        FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC
    ''').fetchall()
    
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['ID Reserva', 'Nome Usuário', 'Email Usuário', 'Telefone Usuário', 'Serviço', 'Quantidade', 'Status', 'Motivo Rejeição', 'Data Reserva'])
    writer.writerows(rows)
    
    buffer = io.BytesIO()
    buffer.write(output.getvalue().encode('utf-8'))
    buffer.seek(0)
    
    filename = f"jgminis_reservas_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    logging.info(f'Export CSV gerado: {filename}')
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='text/csv')

@app.route('/favicon.ico')
def favicon():
    return '', 204 # No favicon

@app.errorhandler(403)
def forbidden_error(e):
    flash('Você não tem permissão para acessar esta página.', 'error')
    return redirect(url_for('index')), 403

@app.errorhandler(404)
def page_not_found(e):
    flash('A página que você tentou acessar não foi encontrada.', 'error')
    return redirect(url_for('index')), 404

@app.errorhandler(500)
def internal_error(e):
    logging.error(f'Erro interno do servidor: {e}', exc_info=True)
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return redirect(url_for('index')), 500

# --- 10. Run the Flask App ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
