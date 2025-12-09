import os
import re
import json
import csv
import io
import logging
from datetime import datetime
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import gspread
from google.oauth2.service_account import Credentials

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default_secret_key')

# Environment variables
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
gc = None
sheet = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)
        SHEET_NAME = 'BASE DE DADOS JG'
        sheet = gc.open(SHEET_NAME).sheet1
        logging.info("gspread auth bem-sucedida")
    except Exception as e:
        logging.error(f'Erro na autenticação ou ao abrir planilha: {e}')
else:
    logging.warning("GOOGLE_SHEETS_CREDENTIALS não definida - usando fallback sem Sheets")

# Database initialization
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        service TEXT NOT NULL,
        quantity INTEGER DEFAULT 1,
        status TEXT DEFAULT 'pending',
        approved_by INTEGER,
        denied_reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (approved_by) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service TEXT UNIQUE NOT NULL,
        quantity INTEGER DEFAULT 0,
        last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Create admin user if not exists
    c.execute('SELECT id FROM users WHERE email = ?', ('admin@jgminis.com.br',))
    if not c.fetchone():
        hashed_pw = generate_password_hash('admin123')
        c.execute('INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)', 
                  ('Admin', 'admin@jgminis.com.br', '11999999999', hashed_pw, 'admin'))
        logging.info("Usuário admin criado no DB")
    
    # Initial stock sync if stock table is empty
    c.execute('SELECT COUNT(*) FROM stock')
    if c.fetchone()[0] == 0 and sheet:
        try:
            records = sheet.get_all_records()
            for record in records[1:]:  # Skip header
                service = record.get('NOME DA MINIATURA', '')
                qty = record.get('QUANTIDADE DISPONIVEL', 0)
                if service:
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
            logging.info('Estoque inicial sincronizado da planilha para o DB.')
        except Exception as e:
            logging.error(f'Erro na sincronização inicial do estoque: {e}')
    conn.commit()
    conn.close()

init_db()

# Validation functions
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    return phone.isdigit() and 10 <= len(phone) <= 11

# Load thumbnails from Google Sheets and DB stock
def load_thumbnails():
    thumbnails = []
    if sheet:
        try:
            records = sheet.get_all_records()
            if not records:
                logging.warning("Planilha vazia - nenhum thumbnail carregado.")
                return [] # Retorna lista vazia se a planilha estiver vazia

            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            
            # Pega os primeiros 12 registros da planilha (ou todos se menos de 12)
            for record in records[1:13]:  # records[1:] para todas as linhas de dados
                service = record.get('NOME DA MINIATURA', '')
                if not service: # Pula registros sem nome de miniatura
                    continue

                marca = record.get('MARCA/FABRICANTE', '')
                obs = record.get('OBSERVAÇÕES', '')
                image = record.get('IMAGEM', LOGO_URL) # Fallback para LOGO_URL se imagem vazia
                price = record.get('VALOR', 0)
                previsao = record.get('PREVISÃO DE CHEGADA', '')
                
                # Obtém a quantidade do DB de estoque
                c.execute('SELECT quantity FROM stock WHERE service = ?', (service,))
                db_qty = c.fetchone()
                quantity = db_qty[0] if db_qty else 0 # Se não estiver no stock DB, assume 0
                
                thumbnails.append({
                    'service': service,
                    'marca': marca,
                    'obs': obs,
                    'image': image,
                    'price': price,
                    'quantity': quantity,
                    'previsao': previsao
                })
            conn.close()
            logging.info(f'Carregados {len(thumbnails)} thumbnails da planilha e DB de estoque.')
        except Exception as e:
            logging.error(f'Erro ao carregar thumbnails: {e}')
            thumbnails = []  # Fallback para lista vazia em caso de erro
    else:
        logging.warning("Sheet não inicializada, thumbnails não carregados.")
    return thumbnails

# --- HTML Templates (Inline Jinja2) ---
INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
        header { background-color: #004085; color: white; padding: 15px 0; text-align: center; }
        header img { height: 50px; vertical-align: middle; margin-right: 10px; }
        header h1 { display: inline-block; vertical-align: middle; margin: 0; }
        nav { background-color: #e9ecef; padding: 10px 0; text-align: center; border-bottom: 1px solid #ddd; }
        nav a { color: #007bff; text-decoration: none; padding: 0 15px; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px auto; max-width: 800px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border-color: #f5c6cb; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 25px; padding: 25px; max-width: 1200px; margin: 20px auto; }
        .thumbnail { background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); transition: transform 0.2s; }
        .thumbnail:hover { transform: translateY(-5px); }
        .thumbnail img { max-width: 100%; height: 180px; object-fit: cover; border-radius: 4px; margin-bottom: 10px; }
        .thumbnail h3 { color: #007bff; margin: 10px 0; font-size: 1.3em; }
        .thumbnail p { margin: 5px 0; font-size: 0.95em; }
        .thumbnail .price { font-weight: bold; color: #28a745; font-size: 1.1em; }
        .thumbnail .quantity { color: #6c757d; }
        .buttons-container { display: flex; justify-content: center; align-items: center; gap: 10px; margin-top: 15px; }
        .btn { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; font-size: 0.9em; transition: background-color 0.2s; }
        .btn:hover { background-color: #0056b3; }
        .btn-whatsapp { background-color: #25D366; }
        .btn-whatsapp:hover { background-color: #1DA851; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 0; margin-top: 30px; }
        @media (max-width: 768px) {
            .grid { grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); padding: 15px; }
            nav a { padding: 0 10px; }
        }
        @media (max-width: 480px) {
            .grid { grid-template-columns: 1fr; padding: 10px; }
            .buttons-container { flex-direction: column; gap: 8px; }
            .btn { width: 80%; }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" onerror="this.src='{{ logo_url }}'">
        <h1>JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="/">Home</a>
        {% if session.user_id %}
            <a href="/reservar">Reservar Múltiplas</a>
            <a href="/profile">Meu Perfil</a>
            {% if session.role == 'admin' %}<a href="/admin">Admin</a>{% endif %}
            <a href="/logout">Logout</a>
        {% else %}
            <a href="/login">Login</a>
            <a href="/register">Registrar</a>
        {% endif %}
    </nav>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <ul class="flash-messages">
                {% for category, message in messages %}
                    <li class="flash-{{ category }}">{{ message }}</li>
                {% endfor %}
            </ul>
        {% endif %}
    {% endwith %}
    <div class="grid">
        {% for thumb in thumbnails %}
            <div class="thumbnail">
                <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
                <h3>{{ thumb.service }}</h3>
                <p>{{ thumb.obs }}</p>
                <p class="price">R$ {{ "%.2f"|format(thumb.price|float) }}</p>
                <p class="quantity">Disponível: {{ thumb.quantity }}</p>
                <div class="buttons-container">
                    <a href="/reserve_single?service={{ thumb.service }}" class="btn">Reservar Agora</a>
                    {% if thumb.quantity == 0 %}
                        <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de entrar na fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" target="_blank" class="btn btn-whatsapp">Fila WhatsApp</a>
                    {% endif %}
                </div>
            </div>
        {% else %}
            <p style="grid-column: 1 / -1; text-align: center;">Nenhuma miniatura disponível no momento. Tente novamente mais tarde ou contate o administrador.</p>
        {% endfor %}
    </div>
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
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .register-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 20px; }
        .form-group { margin-bottom: 15px; text-align: left; }
        .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
        .form-group input[type="text"],
        .form-group input[type="email"],
        .form-group input[type="password"] {
            width: calc(100% - 20px);
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 1em;
        }
        button {
            background-color: #28a745;
            color: white;
            padding: 12px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1.1em;
            width: 100%;
            transition: background-color 0.2s;
            margin-top: 10px;
        }
        button:hover { background-color: #218838; }
        .link-text { margin-top: 20px; font-size: 0.9em; }
        .link-text a { color: #007bff; text-decoration: none; }
        .link-text a:hover { text-decoration: underline; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px 0; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; text-align: center; }
    </style>
</head>
<body>
    <div class="register-container">
        <h1>Registrar</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <ul class="flash-messages">
                    {% for category, message in messages %}
                        <li class="flash-{{ category }}">{{ message }}</li>
                    {% endfor %}
                </ul>
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
        <p class="link-text"><a href="/login">Já tem conta? Fazer Login</a></p>
        <p class="link-text"><a href="/">Voltar para Home</a></p>
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
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #004085; margin-bottom: 20px; }
        .form-group { margin-bottom: 15px; text-align: left; }
        .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
        .form-group input[type="email"],
        .form-group input[type="password"] {
            width: calc(100% - 20px);
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 1em;
        }
        button {
            background-color: #007bff;
            color: white;
            padding: 12px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1.1em;
            width: 100%;
            transition: background-color 0.2s;
            margin-top: 10px;
        }
        button:hover { background-color: #0056b3; }
        .link-text { margin-top: 20px; font-size: 0.9em; }
        .link-text a { color: #007bff; text-decoration: none; }
        .link-text a:hover { text-decoration: underline; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px 0; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; text-align: center; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Login</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <ul class="flash-messages">
                    {% for category, message in messages %}
                        <li class="flash-{{ category }}">{{ message }}</li>
                    {% endfor %}
                </ul>
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
        <p class="link-text"><a href="/register">Não tem conta? Registrar</a></p>
        <p class="link-text"><a href="/">Voltar para Home</a></p>
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
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .reserve-container { background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 100%; max-width: 500px; text-align: center; }
        h1 { color: #004085; margin-bottom: 20px; }
        .item-details img { max-width: 100%; height: 200px; object-fit: cover; border-radius: 4px; margin-bottom: 15px; }
        .item-details p { margin: 5px 0; font-size: 1em; }
        .item-details .price { font-weight: bold; color: #28a745; font-size: 1.1em; }
        .item-details .quantity-available { color: #6c757d; margin-bottom: 20px; }
        .form-group { margin-bottom: 20px; text-align: left; }
        .form-group label { display: block; margin-bottom: 8px; font-weight: bold; font-size: 1.1em; }
        .form-group input[type="number"] {
            width: calc(100% - 20px);
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 1em;
            text-align: center;
        }
        button {
            background-color: #007bff;
            color: white;
            padding: 12px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1.1em;
            width: 100%;
            transition: background-color 0.2s;
            margin-top: 10px;
        }
        button:hover { background-color: #0056b3; }
        .whatsapp-link {
            display: inline-block;
            background-color: #25D366;
            color: white;
            padding: 10px 15px;
            border-radius: 5px;
            text-decoration: none;
            font-weight: bold;
            margin-top: 20px;
            transition: background-color 0.2s;
        }
        .whatsapp-link:hover { background-color: #1DA851; }
        .link-text { margin-top: 20px; font-size: 0.9em; }
        .link-text a { color: #007bff; text-decoration: none; }
        .link-text a:hover { text-decoration: underline; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px 0; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; text-align: center; }
    </style>
</head>
<body>
    <div class="reserve-container">
        <h1>Reservar {{ thumb.service }}</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <ul class="flash-messages">
                    {% for category, message in messages %}
                        <li class="flash-{{ category }}">{{ message }}</li>
                    {% endfor %}
                </ul>
            {% endif %}
        {% endwith %}
        <div class="item-details">
            <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
            <p>{{ thumb.obs }}</p>
            <p class="price">Preço: R$ {{ "%.2f"|format(thumb.price|float) }}</p>
            <p class="quantity-available">Disponível: {{ thumb.quantity }}</p>
        </div>
        {% if thumb.quantity > 0 %}
            <form method="post">
                <div class="form-group">
                    <label for="quantity">Quantidade a reservar:</label>
                    <input type="number" id="quantity" name="quantity" min="1" max="{{ thumb.quantity }}" value="1" required>
                </div>
                <button type="submit">Confirmar Reserva</button>
            </form>
        {% else %}
            <p>Estoque indisponível para esta miniatura.</p>
            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de entrar na fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" target="_blank" class="whatsapp-link">Entrar na Fila de Espera (WhatsApp)</a>
        {% endif %}
        <p class="link-text"><a href="/">Voltar para Home</a></p>
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
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 0; }
        header { background-color: #004085; color: white; padding: 15px 0; text-align: center; }
        header img { height: 50px; vertical-align: middle; margin-right: 10px; }
        header h1 { display: inline-block; vertical-align: middle; margin: 0; }
        nav { background-color: #e9ecef; padding: 10px 0; text-align: center; border-bottom: 1px solid #ddd; }
        nav a { color: #007bff; text-decoration: none; padding: 0 15px; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .container { max-width: 1200px; margin: 20px auto; padding: 20px; background-color: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        .filters { display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 30px; padding: 15px; background-color: #f8f9fa; border-radius: 5px; }
        .filters label { font-weight: bold; margin-right: 5px; }
        .filters input[type="text"], .filters select { padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
        .filters button { background-color: #007bff; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.2s; }
        .filters button:hover { background-color: #0056b3; }
        .thumbnail-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 20px; }
        .thumbnail-item { border: 1px solid #eee; border-radius: 8px; padding: 15px; text-align: center; background-color: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .thumbnail-item img { max-width: 100%; height: 150px; object-fit: cover; border-radius: 4px; margin-bottom: 10px; }
        .thumbnail-item h3 { font-size: 1.1em; color: #007bff; margin-bottom: 5px; }
        .thumbnail-item p { font-size: 0.9em; margin: 3px 0; }
        .thumbnail-item .price { font-weight: bold; color: #28a745; }
        .thumbnail-item .quantity-available { color: #6c757d; margin-bottom: 10px; }
        .thumbnail-item input[type="checkbox"] { margin-right: 8px; transform: scale(1.2); }
        .thumbnail-item input[type="number"] { width: 80px; padding: 5px; border: 1px solid #ddd; border-radius: 4px; text-align: center; margin-top: 10px; }
        .action-buttons { text-align: center; margin-top: 30px; }
        .action-buttons button { background-color: #28a745; color: white; padding: 12px 25px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.1em; transition: background-color 0.2s; }
        .action-buttons button:hover { background-color: #218838; }
        .whatsapp-link { display: inline-block; background-color: #25D366; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-weight: bold; margin-top: 10px; transition: background-color 0.2s; font-size: 0.9em; }
        .whatsapp-link:hover { background-color: #1DA851; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px auto; max-width: 800px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border-color: #f5c6cb; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 0; margin-top: 30px; }
        @media (max-width: 768px) {
            .filters { flex-direction: column; align-items: stretch; }
            .filters input, .filters select, .filters button { width: 100%; margin-bottom: 10px; }
            .thumbnail-list { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" onerror="this.src='{{ logo_url }}'">
        <h1>JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="/">Home</a>
        {% if session.user_id %}
            <a href="/reservar">Reservar Múltiplas</a>
            <a href="/profile">Meu Perfil</a>
            {% if session.role == 'admin' %}<a href="/admin">Admin</a>{% endif %}
            <a href="/logout">Logout</a>
        {% else %}
            <a href="/login">Login</a>
            <a href="/register">Registrar</a>
        {% endif %}
    </nav>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <ul class="flash-messages">
                {% for category, message in messages %}
                    <li class="flash-{{ category }}">{{ message }}</li>
                {% endfor %}
            </ul>
        {% endif %}
    {% endwith %}
    <div class="container">
        <h1>Reservar Múltiplas Miniaturas</h1>
        <form method="get" class="filters">
            <label>
                <input type="checkbox" name="available" value="1" {% if request.args.get('available') %}checked{% endif %}> Apenas Disponíveis
            </label>
            <label>Ordenar por:
                <select name="order">
                    <option value="">Nenhum</option>
                    <option value="service_asc" {% if request.args.get('order') == 'service_asc' %}selected{% endif %}>Nome (A-Z)</option>
                    <option value="service_desc" {% if request.args.get('order') == 'service_desc' %}selected{% endif %}>Nome (Z-A)</option>
                    <option value="price_asc" {% if request.args.get('order') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                    <option value="price_desc" {% if request.args.get('order') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                </select>
            </label>
            <label>Previsão de Chegada:
                <input type="text" name="previsao" value="{{ request.args.get('previsao', '') }}" placeholder="Ex: 2024-12-31">
            </label>
            <label>Marca:
                <input type="text" name="marca" value="{{ request.args.get('marca', '') }}" placeholder="Ex: Reaper">
            </label>
            <button type="submit">Aplicar Filtros</button>
            <a href="/reservar" class="btn" style="background-color: #6c757d;">Limpar Filtros</a>
        </form>

        <form method="post">
            <div class="thumbnail-list">
                {% for thumb in thumbnails %}
                    <div class="thumbnail-item">
                        <input type="checkbox" name="selected_services" value="{{ thumb.service }}" id="service_{{ loop.index }}">
                        <label for="service_{{ loop.index }}">
                            <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
                            <h3>{{ thumb.service }}</h3>
                            <p>{{ thumb.obs }}</p>
                            <p class="price">R$ {{ "%.2f"|format(thumb.price|float) }}</p>
                            <p class="quantity-available">Disponível: {{ thumb.quantity }}</p>
                        </label>
                        {% if thumb.quantity > 0 %}
                            <input type="number" name="quantity_{{ thumb.service }}" min="0" max="{{ thumb.quantity }}" value="0" {% if not session.user_id %}disabled{% endif %}>
                        {% else %}
                            <p>Esgotado</p>
                            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de entrar na fila de espera para {{ thumb.service }}. Meu email: {{ session.get('email', 'anônimo') }}" target="_blank" class="whatsapp-link">Fila WhatsApp</a>
                        {% endif %}
                    </div>
                {% else %}
                    <p style="grid-column: 1 / -1; text-align: center;">Nenhuma miniatura encontrada com os filtros aplicados.</p>
                {% endfor %}
            </div>
            {% if thumbnails %}
                <div class="action-buttons">
                    <button type="submit" {% if not session.user_id %}disabled title="Faça login para reservar"{% endif %}>Reservar Selecionadas</button>
                </div>
            {% endif %}
        </form>
    </div>
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
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 0; }
        header { background-color: #004085; color: white; padding: 15px 0; text-align: center; }
        header img { height: 50px; vertical-align: middle; margin-right: 10px; }
        header h1 { display: inline-block; vertical-align: middle; margin: 0; }
        nav { background-color: #e9ecef; padding: 10px 0; text-align: center; border-bottom: 1px solid #ddd; }
        nav a { color: #007bff; text-decoration: none; padding: 0 15px; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .container { max-width: 900px; margin: 20px auto; padding: 20px; background-color: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .user-info p { margin: 5px 0; font-size: 1.1em; }
        .user-info span { font-weight: bold; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #f8f9fa; color: #333; font-weight: bold; }
        tr:nth-child(even) { background-color: #f2f2f2; }
        .status-pending { color: orange; font-weight: bold; }
        .status-approved { color: green; font-weight: bold; }
        .status-denied { color: red; font-weight: bold; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px auto; max-width: 800px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border-color: #f5c6cb; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 0; margin-top: 30px; }
        @media (max-width: 768px) {
            .container { padding: 15px; }
            table, thead, tbody, th, td, tr { display: block; }
            thead tr { position: absolute; top: -9999px; left: -9999px; }
            tr { border: 1px solid #ccc; margin-bottom: 10px; }
            td { border: none; border-bottom: 1px solid #eee; position: relative; padding-left: 50%; text-align: right; }
            td:before { position: absolute; top: 6px; left: 6px; width: 45%; padding-right: 10px; white-space: nowrap; content: attr(data-label); font-weight: bold; text-align: left; }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" onerror="this.src='{{ logo_url }}'">
        <h1>JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="/">Home</a>
        {% if session.user_id %}
            <a href="/reservar">Reservar Múltiplas</a>
            <a href="/profile">Meu Perfil</a>
            {% if session.role == 'admin' %}<a href="/admin">Admin</a>{% endif %}
            <a href="/logout">Logout</a>
        {% else %}
            <a href="/login">Login</a>
            <a href="/register">Registrar</a>
        {% endif %}
    </nav>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <ul class="flash-messages">
                {% for category, message in messages %}
                    <li class="flash-{{ category }}">{{ message }}</li>
                {% endfor %}
            </ul>
        {% endif %}
    {% endwith %}
    <div class="container">
        <h1>Meu Perfil</h1>
        <div class="user-info">
            <h2>Dados do Usuário</h2>
            <p><span>Nome:</span> {{ user.name }}</p>
            <p><span>Email:</span> {{ user.email }}</p>
            <p><span>Telefone:</span> {{ user.phone }}</p>
            <p><span>Membro desde:</span> {{ user.data_cadastro }}</p>
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
                        <th>Motivo (se negado)</th>
                    </tr>
                </thead>
                <tbody>
                    {% for res in reservations %}
                        <tr>
                            <td data-label="Serviço">{{ res.service }}</td>
                            <td data-label="Quantidade">{{ res.quantity }}</td>
                            <td data-label="Status" class="status-{{ res.status }}">{{ res.status|capitalize }}</td>
                            <td data-label="Data da Reserva">{{ res.created_at }}</td>
                            <td data-label="Motivo">{{ res.denied_reason or 'N/A' }}</td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <p>Você ainda não possui reservas.</p>
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
        body { font-family: Arial, sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 0; }
        header { background-color: #004085; color: white; padding: 15px 0; text-align: center; }
        header img { height: 50px; vertical-align: middle; margin-right: 10px; }
        header h1 { display: inline-block; vertical-align: middle; margin: 0; }
        nav { background-color: #e9ecef; padding: 10px 0; text-align: center; border-bottom: 1px solid #ddd; }
        nav a { color: #007bff; text-decoration: none; padding: 0 15px; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .container { max-width: 1200px; margin: 20px auto; padding: 20px; background-color: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background-color: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .stat-card h3 { margin: 0 0 10px 0; color: #333; font-size: 1.2em; }
        .stat-card p { font-size: 1.8em; font-weight: bold; color: #007bff; margin: 0; }
        .admin-actions-bar { display: flex; flex-wrap: wrap; justify-content: center; gap: 15px; margin-bottom: 30px; padding: 15px; background-color: #f8f9fa; border-radius: 5px; }
        .admin-actions-bar button, .admin-actions-bar a {
            background-color: #6c757d; color: white; padding: 10px 15px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; font-size: 0.9em; transition: background-color 0.2s;
        }
        .admin-actions-bar button:hover, .admin-actions-bar a:hover { background-color: #5a6268; }
        .admin-actions-bar .btn-sync { background-color: #ffc107; color: #212529; }
        .admin-actions-bar .btn-sync:hover { background-color: #e0a800; }
        .admin-actions-bar .btn-backup { background-color: #17a2b8; }
        .admin-actions-bar .btn-backup:hover { background-color: #138496; }
        .filters-form { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; padding: 15px; background-color: #f8f9fa; border-radius: 5px; }
        .filters-form label { font-weight: bold; margin-right: 5px; }
        .filters-form input[type="text"], .filters-form select { padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
        .filters-form button { background-color: #007bff; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.2s; }
        .filters-form button:hover { background-color: #0056b3; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #f8f9fa; color: #333; font-weight: bold; }
        tr:nth-child(even) { background-color: #f2f2f2; }
        .action-buttons-table { display: flex; gap: 5px; }
        .action-buttons-table button, .action-buttons-table a {
            padding: 6px 10px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 0.85em; transition: background-color 0.2s; color: white;
        }
        .btn-promote { background-color: #28a745; } .btn-promote:hover { background-color: #218838; }
        .btn-demote { background-color: #ffc107; color: #212529; } .btn-demote:hover { background-color: #e0a800; }
        .btn-delete { background-color: #dc3545; } .btn-delete:hover { background-color: #c82333; }
        .btn-approve { background-color: #28a745; } .btn-approve:hover { background-color: #218838; }
        .btn-deny { background-color: #dc3545; } .btn-deny:hover { background-color: #c82333; }
        .form-section { background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin-top: 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .form-section form { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; margin-top: 15px; }
        .form-section label { display: block; margin-bottom: 5px; font-weight: bold; }
        .form-section input[type="text"], .form-section input[type="number"], .form-section input[type="url"], .form-section select {
            width: calc(100% - 20px); padding: 8px; border: 1px solid #ddd; border-radius: 4px;
        }
        .form-section button { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.2s; margin-top: 10px; }
        .form-section button:hover { background-color: #0056b3; }
        .flash-messages { list-style: none; padding: 10px; margin: 10px auto; max-width: 800px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border-color: #f5c6cb; }
        footer { background-color: #343a40; color: white; text-align: center; padding: 15px 0; margin-top: 30px; }
        @media (max-width: 768px) {
            .stats-grid { grid-template-columns: 1fr; }
            .admin-actions-bar, .filters-form { flex-direction: column; align-items: stretch; }
            .admin-actions-bar button, .admin-actions-bar a, .filters-form input, .filters-form select, .filters-form button { width: 100%; margin-bottom: 10px; }
            table, thead, tbody, th, td, tr { display: block; }
            thead tr { position: absolute; top: -9999px; left: -9999px; }
            tr { border: 1px solid #ccc; margin-bottom: 10px; }
            td { border: none; border-bottom: 1px solid #eee; position: relative; padding-left: 50%; text-align: right; }
            td:before { position: absolute; top: 6px; left: 6px; width: 45%; padding-right: 10px; white-space: nowrap; content: attr(data-label); font-weight: bold; text-align: left; }
            .form-section form { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" onerror="this.src='{{ logo_url }}'">
        <h1>JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="/">Home</a>
        {% if session.user_id %}
            <a href="/reservar">Reservar Múltiplas</a>
            <a href="/profile">Meu Perfil</a>
            {% if session.role == 'admin' %}<a href="/admin">Admin</a>{% endif %}
            <a href="/logout">Logout</a>
        {% else %}
            <a href="/login">Login</a>
            <a href="/register">Registrar</a>
        {% endif %}
    </nav>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <ul class="flash-messages">
                {% for category, message in messages %}
                    <li class="flash-{{ category }}">{{ message }}</li>
                {% endfor %}
            </ul>
        {% endif %}
    {% endwith %}
    <div class="container">
        <h1>Painel Administrativo</h1>

        <h2>Estatísticas Rápidas</h2>
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
        </div>

        <div class="admin-actions-bar">
            <form method="post" style="display:inline;">
                <input type="hidden" name="action" value="sync_stock">
                <button type="submit" class="btn-sync">Sincronizar Estoque da Planilha</button>
            </form>
            <a href="/backup" class="btn-backup">Backup DB (JSON)</a>
            <a href="/export_csv" class="btn-backup">Exportar Reservas (CSV)</a>
        </div>

        <h2>Gerenciar Usuários</h2>
        <form method="get" class="filters-form">
            <label>Filtrar Email: <input type="text" name="user_filter" value="{{ request.args.get('user_filter', '') }}" placeholder="Email"></label>
            <label>Filtrar Role:
                <select name="role">
                    <option value="">Todos</option>
                    <option value="user" {% if request.args.get('role') == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if request.args.get('role') == 'admin' %}selected{% endif %}>Admin</option>
                </select>
            </label>
            <button type="submit">Aplicar Filtros</button>
            <a href="/admin" class="btn" style="background-color: #6c757d;">Limpar Filtros</a>
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
                        <td data-label="ID">{{ user.id }}</td>
                        <td data-label="Nome">{{ user.name }}</td>
                        <td data-label="Email">{{ user.email }}</td>
                        <td data-label="Telefone">{{ user.phone }}</td>
                        <td data-label="Role">{{ user.role }}</td>
                        <td data-label="Ações">
                            <div class="action-buttons-table">
                                {% if user.role != 'admin' %}
                                    <form method="post" style="display:inline;">
                                        <input type="hidden" name="action" value="promote_user">
                                        <input type="hidden" name="user_id" value="{{ user.id }}">
                                        <button type="submit" class="btn-promote">Promover</button>
                                    </form>
                                {% endif %}
                                {% if user.role == 'admin' and user.email != 'admin@jgminis.com.br' %} {# Não rebaixa o admin principal #}
                                    <form method="post" style="display:inline;">
                                        <input type="hidden" name="action" value="demote_user">
                                        <input type="hidden" name="user_id" value="{{ user.id }}">
                                        <button type="submit" class="btn-demote">Rebaixar</button>
                                    </form>
                                {% endif %}
                                {% if user.email != 'admin@jgminis.com.br' %} {# Não deleta o admin principal #}
                                    <form method="post" style="display:inline;" onsubmit="return confirm('Tem certeza que deseja deletar este usuário e todas as suas reservas?');">
                                        <input type="hidden" name="action" value="delete_user">
                                        <input type="hidden" name="user_id" value="{{ user.id }}">
                                        <button type="submit" class="btn-delete">Deletar</button>
                                    </form>
                                {% endif %}
                            </div>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Gerenciar Reservas</h2>
        <form method="get" class="filters-form">
            <label>Filtrar Serviço/Email: <input type="text" name="res_filter" value="{{ request.args.get('res_filter', '') }}" placeholder="Serviço ou Email"></label>
            <label>Filtrar Status:
                <select name="status">
                    <option value="">Todos</option>
                    <option value="pending" {% if request.args.get('status') == 'pending' %}selected{% endif %}>Pendente</option>
                    <option value="approved" {% if request.args.get('status') == 'approved' %}selected{% endif %}>Aprovada</option>
                    <option value="denied" {% if request.args.get('status') == 'denied' %}selected{% endif %}>Negada</option>
                </select>
            </label>
            <button type="submit">Aplicar Filtros</button>
            <a href="/admin" class="btn" style="background-color: #6c757d;">Limpar Filtros</a>
        </form>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Usuário</th>
                    <th>Serviço</th>
                    <th>Quantidade</th>
                    <th>Status</th>
                    <th>Data</th>
                    <th>Motivo</th>
                    <th>Ações</th>
                </tr>
            </thead>
            <tbody>
                {% for res in reservations %}
                    <tr>
                        <td data-label="ID">{{ res.id }}</td>
                        <td data-label="Usuário">{{ res.user_email }}</td>
                        <td data-label="Serviço">{{ res.service }}</td>
                        <td data-label="Quantidade">{{ res.quantity }}</td>
                        <td data-label="Status" class="status-{{ res.status }}">{{ res.status|capitalize }}</td>
                        <td data-label="Data">{{ res.created_at }}</td>
                        <td data-label="Motivo">{{ res.denied_reason or 'N/A' }}</td>
                        <td data-label="Ações">
                            <div class="action-buttons-table">
                                {% if res.status == 'pending' %}
                                    <form method="post" style="display:inline;">
                                        <input type="hidden" name="action" value="approve_res">
                                        <input type="hidden" name="res_id" value="{{ res.id }}">
                                        <button type="submit" class="btn-approve">Aprovar</button>
                                    </form>
                                    <form method="post" style="display:inline;">
                                        <input type="hidden" name="action" value="deny_res">
                                        <input type="hidden" name="res_id" value="{{ res.id }}">
                                        <input type="text" name="reason" placeholder="Motivo" required style="width: 100px;">
                                        <button type="submit" class="btn-deny">Negar</button>
                                    </form>
                                {% endif %}
                                <form method="post" style="display:inline;" onsubmit="return confirm('Tem certeza que deseja deletar esta reserva?');">
                                    <input type="hidden" name="action" value="delete_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <button type="submit" class="btn-delete">Deletar</button>
                                </form>
                            </div>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="form-section">
            <h2>Inserir Nova Miniatura</h2>
            <form method="post">
                <input type="hidden" name="action" value="insert_miniature">
                <div>
                    <label for="new_service">Nome do Serviço:</label>
                    <input type="text" id="new_service" name="service" required>
                </div>
                <div>
                    <label for="new_marca">Marca/Fabricante:</label>
                    <input type="text" id="new_marca" name="marca" required>
                </div>
                <div>
                    <label for="new_obs">Observações:</label>
                    <input type="text" id="new_obs" name="obs">
                </div>
                <div>
                    <label for="new_price">Preço:</label>
                    <input type="number" id="new_price" name="price" step="0.01" required>
                </div>
                <div>
                    <label for="new_quantity">Quantidade Inicial:</label>
                    <input type="number" id="new_quantity" name="quantity" min="0" required>
                </div>
                <div>
                    <label for="new_image">URL da Imagem:</label>
                    <input type="url" id="new_image" name="image" required>
                </div>
                <button type="submit">Adicionar Miniatura</button>
            </form>
        </div>

        <div class="form-section">
            <h2>Inserir Nova Reserva</h2>
            <form method="post">
                <input type="hidden" name="action" value="insert_reservation">
                <div>
                    <label for="res_user_id">Usuário:</label>
                    <select id="res_user_id" name="user_id" required>
                        <option value="">Selecione um Usuário</option>
                        {% for u in all_users %}
                            <option value="{{ u.id }}">{{ u.email }} (ID: {{ u.id }})</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label for="res_service">Serviço:</label>
                    <select id="res_service" name="service" required>
                        <option value="">Selecione um Serviço</option>
                        {% for s in all_services %}
                            <option value="{{ s }}">{{ s }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label for="res_quantity">Quantidade:</label>
                    <input type="number" id="res_quantity" name="quantity" min="1" required>
                </div>
                <div>
                    <label for="res_status">Status:</label>
                    <select id="res_status" name="status">
                        <option value="pending">Pendente</option>
                        <option value="approved">Aprovada</option>
                        <option value="denied">Negada</option>
                    </select>
                </div>
                <div>
                    <label for="res_reason">Motivo (se negada):</label>
                    <input type="text" id="res_reason" name="reason">
                </div>
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

# --- Rotas da Aplicação ---
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
            flash('O nome é obrigatório.', 'error')
            return render_template_string(REGISTER_HTML)
        if not is_valid_email(email):
            flash('Email inválido. Por favor, insira um email válido.', 'error')
            return render_template_string(REGISTER_HTML)
        if not is_valid_phone(phone):
            flash('Telefone inválido. Deve conter 10 ou 11 dígitos numéricos.', 'error')
            return render_template_string(REGISTER_HTML)
        if len(password) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML)
        
        hashed_pw = generate_password_hash(password)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)', (name, email, phone, hashed_pw))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Este email já está cadastrado. Por favor, use outro ou faça login.', 'error')
        except Exception as e:
            logging.error(f"Erro ao registrar usuário: {e}")
            flash('Ocorreu um erro interno ao tentar registrar. Tente novamente.', 'error')
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
        c.execute('SELECT id, name, email, password, role FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[3], password): # user[3] é a senha hash
            session['user_id'] = user[0]
            session['name'] = user[1]
            session['email'] = user[2]
            session['role'] = user[4]
            logging.info(f'Login bem-sucedido para {email}')
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Credenciais inválidas. Verifique seu email e senha.', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    if 'user_id' in session:
        logging.info(f'Logout de {session["email"]}')
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
    
    # Obter dados do usuário logado
    c.execute('SELECT name, email, phone, data_cadastro FROM users WHERE id = ?', (session['user_id'],))
    user_data = c.fetchone()
    user = {
        'name': user_data[0],
        'email': user_data[1],
        'phone': user_data[2],
        'data_cadastro': user_data[3]
    }
    
    # Obter reservas do usuário
    c.execute('SELECT service, quantity, status, created_at, denied_reason FROM reservations WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
    reservations = []
    for res in c.fetchall():
        reservations.append({
            'service': res[0],
            'quantity': res[1],
            'status': res[2],
            'created_at': res[3],
            'denied_reason': res[4]
        })
    
    conn.close()
    return render_template_string(PROFILE_HTML, user=user, reservations=reservations, logo_url=LOGO_URL, datetime=datetime)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Você precisa estar logado para reservar miniaturas.', 'error')
        return redirect(url_for('login'))

    all_thumbnails = load_thumbnails()
    filtered_thumbnails = all_thumbnails

    if request.method == 'GET':
        # Aplicar filtros
        available = request.args.get('available')
        order = request.args.get('order')
        previsao_filter = request.args.get('previsao', '').lower()
        marca_filter = request.args.get('marca', '').lower()

        if available:
            filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
        
        if previsao_filter:
            filtered_thumbnails = [t for t in filtered_thumbnails if previsao_filter in t['previsao'].lower()]
        
        if marca_filter:
            filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]

        # Aplicar ordenação
        if order == 'service_asc':
            filtered_thumbnails.sort(key=lambda x: x['service'])
        elif order == 'service_desc':
            filtered_thumbnails.sort(key=lambda x: x['service'], reverse=True)
        elif order == 'price_asc':
            filtered_thumbnails.sort(key=lambda x: float(x['price']))
        elif order == 'price_desc':
            filtered_thumbnails.sort(key=lambda x: float(x['price']), reverse=True)

        return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, datetime=datetime)
    
    elif request.method == 'POST':
        selected_services = request.form.getlist('selected_services')
        reservations_made = 0
        
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        for service_name in selected_services:
            quantity_key = f'quantity_{service_name}'
            quantity_str = request.form.get(quantity_key, '0')
            
            try:
                quantity = int(quantity_str)
            except ValueError:
                flash(f'Quantidade inválida para {service_name}.', 'error')
                continue

            if quantity <= 0:
                continue

            # Verificar estoque atual
            c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name,))
            stock_row = c.fetchone()
            current_stock = stock_row[0] if stock_row else 0

            if quantity > current_stock:
                flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock}.', 'error')
                continue
            
            # Realizar reserva e atualizar estoque
            c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                      (session['user_id'], service_name, quantity))
            c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service_name))
            reservations_made += 1
            
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

    thumbnails = load_thumbnails()
    thumb = next((t for t in thumbnails if t['service'] == service_name), None)

    if not thumb:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('index'))

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name,))
    stock_row = c.fetchone()
    current_stock = stock_row[0] if stock_row else 0
    conn.close()

    if request.method == 'POST':
        quantity_str = request.form.get('quantity', '0')
        try:
            quantity = int(quantity_str)
        except ValueError:
            flash('Quantidade inválida.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, current_stock=current_stock, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

        if quantity <= 0:
            flash('A quantidade a reservar deve ser maior que zero.', 'error')
        elif quantity > current_stock:
            flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock}.', 'error')
        else:
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)',
                      (session['user_id'], service_name, quantity))
            c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service_name))
            conn.commit()
            conn.close()
            flash(f'{quantity} unidade(s) de {service_name} reservada(s) com sucesso!', 'success')
            return redirect(url_for('profile'))
            
    return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, current_stock=current_stock, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

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
            c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,)) # Deleta reservas primeiro
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            flash('Usuário e suas reservas deletados.', 'success')
        elif action == 'approve_res':
            res_id = request.form['res_id']
            c.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
            flash('Reserva aprovada.', 'success')
        elif action == 'deny_res':
            res_id = request.form['res_id']
            reason = request.form.get('reason', 'Motivo não especificado')
            c.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
            flash('Reserva negada.', 'success')
        elif action == 'delete_res':
            res_id = request.form['res_id']
            c.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
            flash('Reserva deletada.', 'success')
        elif action == 'insert_miniature':
            service = request.form['service'].strip()
            marca = request.form['marca'].strip()
            obs = request.form['obs'].strip()
            price = request.form['price']
            quantity = request.form['quantity']
            image = request.form['image'].strip()

            if not all([service, marca, price, quantity, image]):
                flash('Todos os campos da miniatura são obrigatórios.', 'error')
            else:
                try:
                    price = float(price)
                    quantity = int(quantity)
                    if quantity < 0: raise ValueError("Quantidade negativa")
                    
                    # Insere/Atualiza no stock DB
                    c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, quantity))
                    flash(f'Miniatura "{service}" adicionada/atualizada no estoque.', 'success')
                except ValueError:
                    flash('Preço e Quantidade devem ser números válidos.', 'error')
                except Exception as e:
                    logging.error(f"Erro ao inserir miniatura: {e}")
                    flash('Erro ao adicionar miniatura.', 'error')

        elif action == 'insert_reservation':
            user_id = request.form['user_id']
            service = request.form['service']
            quantity = request.form['quantity']
            status = request.form['status']
            reason = request.form.get('reason', '')

            if not all([user_id, service, quantity]):
                flash('Usuário, Serviço e Quantidade são obrigatórios para a reserva.', 'error')
            else:
                try:
                    quantity = int(quantity)
                    if quantity <= 0: raise ValueError("Quantidade deve ser positiva")

                    # Verificar estoque antes de inserir reserva aprovada
                    if status == 'approved':
                        c.execute('SELECT quantity FROM stock WHERE service = ?', (service,))
                        stock_row = c.fetchone()
                        current_stock = stock_row[0] if stock_row else 0
                        if quantity > current_stock:
                            flash(f'Estoque insuficiente para {service}. Disponível: {current_stock}.', 'error')
                            conn.commit() # Commit para salvar outras ações, se houver
                            return redirect(url_for('admin'))

                    c.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)', 
                              (user_id, service, quantity, status, reason))
                    
                    # Decrementar estoque se a reserva for aprovada
                    if status == 'approved':
                        c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service))
                    
                    flash('Nova reserva criada com sucesso!', 'success')
                except ValueError:
                    flash('Quantidade deve ser um número válido e positivo.', 'error')
                except Exception as e:
                    logging.error(f"Erro ao inserir reserva: {e}")
                    flash('Erro ao criar reserva.', 'error')

        elif action == 'sync_stock':
            if sheet:
                try:
                    records = sheet.get_all_records()
                    for record in records[1:]: # Ignora a linha de cabeçalho
                        service = record.get('NOME DA MINIATURA', '')
                        qty = record.get('QUANTIDADE DISPONIVEL', 0)
                        if service:
                            c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
                    flash('Estoque sincronizado da planilha para o DB!', 'success')
                    logging.info('Estoque sincronizado via admin.')
                except Exception as e:
                    logging.error(f'Erro ao sincronizar estoque da planilha: {e}')
                    flash('Erro na sincronização do estoque. Verifique a planilha e as permissões.', 'error')
            else:
                flash('Planilha não configurada ou inacessível para sincronização.', 'error')
        
        conn.commit() # Commit final para todas as ações POST
        return redirect(url_for('admin')) # Redireciona para GET após POST

    # --- Lógica GET para exibir o painel ---
    # Estatísticas
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations')
    total_res_count = c.fetchone()[0]
    stats = {'users': users_count, 'pending': pending_count, 'total_res': total_res_count}

    # Filtros de Usuários
    user_filter = request.args.get('user_filter', '').strip().lower()
    role_filter = request.args.get('role', '').strip().lower()
    
    user_query = 'SELECT id, name, email, phone, role FROM users WHERE 1=1'
    user_params = []
    if user_filter:
        user_query += ' AND (name LIKE ? OR email LIKE ?)'
        user_params.extend([f'%{user_filter}%', f'%{user_filter}%'])
    if role_filter:
        user_query += ' AND role = ?'
        user_params.append(role_filter)
    users = c.execute(user_query, user_params).fetchall()
    
    # Filtros de Reservas
    res_filter = request.args.get('res_filter', '').strip().lower()
    status_filter = request.args.get('status', '').strip().lower()

    res_query = '''
        SELECT r.id, u.email, r.service, r.quantity, r.status, r.created_at, r.denied_reason 
        FROM reservations r JOIN users u ON r.user_id = u.id 
        WHERE 1=1
    '''
    res_params = []
    if res_filter:
        res_query += ' AND (r.service LIKE ? OR u.email LIKE ?)'
        res_params.extend([f'%{res_filter}%', f'%{res_filter}%'])
    if status_filter:
        res_query += ' AND r.status = ?'
        res_params.append(status_filter)
    res_query += ' ORDER BY r.created_at DESC'
    reservations = c.execute(res_query, res_params).fetchall()

    # Dados para formulários de inserção
    all_users_for_select = c.execute('SELECT id, email FROM users ORDER BY email').fetchall()
    all_services_for_select = [t['service'] for t in load_thumbnails()] # Pega serviços do stock DB via load_thumbnails

    conn.close()
    return render_template_string(ADMIN_HTML, 
                                  stats=stats, 
                                  users=users, 
                                  reservations=reservations, 
                                  all_users=all_users_for_select, 
                                  all_services=all_services_for_select, 
                                  logo_url=LOGO_URL, 
                                  datetime=datetime,
                                  request=request) # Passa request para manter filtros na URL

@app.route('/backup')
def backup():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        abort(403)
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Fetch all data from tables
    users = c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users').fetchall()
    reservations = c.execute('SELECT id, user_id, service, quantity, status, approved_by, denied_reason, created_at FROM reservations').fetchall()
    stock = c.execute('SELECT id, service, quantity, last_sync FROM stock').fetchall()
    
    conn.close()
    
    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(zip(['id', 'name', 'email', 'phone', 'role', 'data_cadastro'], row)) for row in users],
        'reservations': [dict(zip(['id', 'user_id', 'service', 'quantity', 'status', 'approved_by', 'denied_reason', 'created_at'], row)) for row in reservations],
        'stock': [dict(zip(['id', 'service', 'quantity', 'last_sync'], row)) for row in stock]
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
        flash('Acesso negado.', 'error')
        abort(403)
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Fetch reservations with user details
    rows = c.execute('''
        SELECT 
            r.id, 
            u.name AS user_name, 
            u.email AS user_email, 
            u.phone AS user_phone,
            r.service, 
            r.quantity, 
            r.status, 
            r.created_at, 
            r.denied_reason 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id
        ORDER BY r.created_at DESC
    ''').fetchall()
    
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['ID Reserva', 'Nome Usuário', 'Email Usuário', 'Telefone Usuário', 'Serviço', 'Quantidade', 'Status', 'Data Reserva', 'Motivo Negado'])
    writer.writerows(rows)
    
    output.seek(0)
    
    buffer = io.BytesIO()
    buffer.write(output.getvalue().encode('utf-8'))
    buffer.seek(0)
    
    filename = f"jgminis_reservas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='text/csv')

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.errorhandler(403)
def forbidden_error(e):
    flash('Você não tem permissão para acessar esta página.', 'error')
    return redirect(url_for('index')), 403

@app.errorhandler(404)
def page_not_found(e):
    flash('A página que você tentou acessar não foi encontrada.', 'error')
    return redirect(url_for('index')), 404

@app.errorhandler(500)
def internal_server_error(e):
    logging.error(f'Erro interno do servidor: {e}', exc_info=True)
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return redirect(url_for('index')), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
