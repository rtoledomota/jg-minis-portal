import os
import json
import sqlite3
import logging
import csv
from datetime import datetime, date, timedelta
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, jsonify, abort, send_file, Response
from flask_bcrypt import Bcrypt
import gspread
from google.oauth2.service_account import Credentials
import re
import io

# --- Configuração de Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')  # Apenas números, sem +55 ou espaços
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025_dev_key_fallback')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

# --- Inicialização do Flask App ---
app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# --- Integração Google Sheets ---
gc = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        logger.info("gspread auth bem-sucedida")
    except Exception as e:
        logger.error(f"Erro na autenticação gspread: {e}")
        gc = None
else:
    logger.warning("GOOGLE_SHEETS_CREDENTIALS não definida - usando fallback sem Sheets")
    gc = None

# --- Funções de Validação ---
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    # Apenas números, 10 a 11 dígitos (com DDD)
    pattern = r'^\d{10,11}$'
    return re.match(pattern, phone) is not None

# --- Inicialização do Banco de Dados ---
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Tabela de Usuários (com name e phone)
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  email TEXT UNIQUE NOT NULL,
                  phone TEXT NOT NULL,
                  password TEXT NOT NULL,
                  role TEXT DEFAULT 'user',
                  data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Tabela de Reservas (com quantity)
    c.execute('''CREATE TABLE IF NOT EXISTS reservations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  service TEXT NOT NULL,
                  quantity INTEGER DEFAULT 1,
                  status TEXT DEFAULT 'pending',
                  approved_by INTEGER,
                  denied_reason TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id),
                  FOREIGN KEY (approved_by) REFERENCES users (id))''')
    # Tabela de Estoque (para controle em tempo real)
    c.execute('''CREATE TABLE IF NOT EXISTS stock
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  service TEXT UNIQUE NOT NULL,
                  quantity INTEGER DEFAULT 0,
                  price TEXT DEFAULT '0',
                  marca TEXT DEFAULT '',
                  obs TEXT DEFAULT '',
                  thumbnail_url TEXT DEFAULT '',
                  previsao_chegada TEXT DEFAULT '',
                  last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # Cria usuário admin padrão se não existir
    c.execute("SELECT id FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)",
                  ('Admin', 'admin@jgminis.com.br', '11999999999', hashed_password, 'admin'))
        logger.info("Usuário admin criado no DB")
    conn.commit()
    conn.close()

init_db()

# --- Carregar Miniaturas (do DB de Estoque, não direto da planilha) ---
def load_thumbnails():
    thumbnails = []
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT service, quantity, price, marca, obs, thumbnail_url, previsao_chegada FROM stock ORDER BY service")
    stock_records = c.fetchall()
    conn.close()

    for record in stock_records:
        service, quantity, price, marca, obs, thumbnail_url, previsao_chegada = record
        description = f"{marca} - {obs}".strip(' - ')
        thumbnails.append({
            'service': service,
            'description': description or 'Descrição disponível',
            'thumbnail_url': thumbnail_url or LOGO_URL,
            'price': price,
            'quantity': quantity,
            'marca': marca,
            'previsao_chegada': previsao_chegada
        })
    logger.info(f"Carregados {len(thumbnails)} thumbnails do DB de estoque")
    return thumbnails

# --- Templates HTML (Inline Jinja2) ---

INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2 - Serviços</title>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 20px; background: #f8f9fa; color: #333; }
        header { text-align: center; padding: 20px; background: #004085; color: white; }
        img.logo { width: 150px; height: auto; margin: 10px; }
        .thumbnails { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; padding: 20px; }
        .thumbnail { background: white; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); padding: 15px; text-align: center; transition: transform 0.2s; }
        .thumbnail:hover { transform: scale(1.05); }
        .thumbnail img { width: 100%; height: 150px; object-fit: cover; border-radius: 8px; }
        .thumbnail h3 { margin: 10px 0; color: #007bff; }
        .thumbnail p { margin: 5px 0; }
        .buttons { display: flex; justify-content: center; gap: 10px; margin-top: 10px; }
        .reservar-btn { background: #28a745; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-weight: bold; }
        .reservar-btn:hover { background: #218838; }
        .whatsapp-btn { background: #25D366; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-weight: bold; }
        .whatsapp-btn:hover { background: #1DA851; }
        nav { text-align: center; padding: 10px; background: #e9ecef; }
        nav a { margin: 0 15px; color: #007bff; text-decoration: none; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 10px; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        footer { text-align: center; padding: 10px; background: #343a40; color: white; margin-top: 40px; }
        @media (max-width: 600px) { .thumbnails { grid-template-columns: 1fr; } .buttons { flex-direction: column; } }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" class="logo" onerror="this.src='{{ logo_url }}'">
        <h1>Bem-vindo ao JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        {% if not session.user_id %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% endif %}
        {% if session.user_id %}
            <a href="{{ url_for('reservar') }}">Reservar Miniaturas</a>
            {% if session.role == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
            <a href="{{ url_for('profile') }}">Meu Perfil</a>
            <a href="{{ url_for('logout') }}">Logout</a>
        {% endif %}
    </nav>
    <main class="thumbnails">
        {% for thumb in thumbnails %}
        <div class="thumbnail">
            <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
            <h3>{{ thumb.service }}</h3>
            <p>{{ thumb.description or 'Descrição disponível' }}</p>
            <p>Preço: R$ {{ thumb.price or 'Consultar' }}</p>
            <p>Disponível: {{ thumb.quantity }}</p>
            <div class="buttons">
                <a href="{{ url_for('reserve_single', service=thumb.service) }}" class="reservar-btn">Reservar Agora</a>
                {% if thumb.quantity == 0 %}
                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, estou na fila de espera para {{ thumb.service }}. Meu email: {{ session.email if session.user_id else 'anônimo' }}" class="whatsapp-btn" target="_blank">Fila WhatsApp</a>
                {% endif %}
            </div>
        </div>
        {% endfor %}
        {% if not thumbnails %}
        <div class="thumbnail">
            <p>Serviços em manutenção. Contate-nos!</p>
        </div>
        {% endif %}
    </main>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
            <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">
                {{ message }}
            </div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <footer>
        <p>&copy; 2025 JG MINIS - Todos os direitos reservados</p>
    </footer>
</body>
</html>
'''

LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .form-container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 300px; text-align: center; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #0056b3; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="form-container">
        <h2>Login</h2>
        <form method="POST">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Senha" required>
            <button type="submit">Entrar</button>
        </form>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-error">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('register') }}">Não tem conta? Registrar</a></p>
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a></p>
    </div>
</body>
</html>
'''

REGISTER_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Registrar - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .form-container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 300px; text-align: center; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #218838; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="form-container">
        <h2>Registrar Usuário</h2>
        <form method="POST">
            <input type="text" name="name" placeholder="Nome Completo" required>
            <input type="email" name="email" placeholder="Email" required>
            <input type="tel" name="phone" placeholder="Telefone (apenas números)" pattern="[0-9]{10,11}" required>
            <input type="password" name="password" placeholder="Senha (mín. 6 chars)" required minlength="6">
            <button type="submit">Registrar</button>
        </form>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('login') }}">Já tem conta? Fazer Login</a></p>
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a></p>
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
    <title>Reservar {{ thumb.service }} - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .form-container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 350px; text-align: center; }
        img { max-width: 100%; height: 150px; object-fit: cover; border-radius: 8px; margin-bottom: 15px; }
        input[type="number"] { width: calc(100% - 22px); padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #218838; }
        .whatsapp-btn { background: #25D366; color: white; padding: 10px; border-radius: 5px; text-decoration: none; font-weight: bold; display: block; margin-top: 10px; }
        .whatsapp-btn:hover { background: #1DA851; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="form-container">
        <h2>Reservar {{ thumb.service }}</h2>
        <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}">
        <p>Preço: R$ {{ thumb.price }}</p>
        <p>Disponível: {{ current_stock }}</p>
        {% if current_stock > 0 %}
        <form method="POST">
            <input type="hidden" name="service" value="{{ thumb.service }}">
            <label for="quantity">Quantidade:</label>
            <input type="number" id="quantity" name="quantity" value="1" min="1" max="{{ current_stock }}" required>
            <button type="submit">Confirmar Reserva</button>
        </form>
        {% else %}
        <p>Estoque esgotado para {{ thumb.service }}.</p>
        <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, estou na fila de espera para {{ thumb.service }}. Meu email: {{ session.email if session.user_id else 'anônimo' }}" class="whatsapp-btn" target="_blank">Fila WhatsApp</a>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a></p>
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
    <title>Reservar Miniaturas - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; }
        .filters { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; justify-content: center; }
        .filters label { margin-right: 5px; }
        .filters select, .filters input[type="text"] { padding: 8px; border: 1px solid #ddd; border-radius: 5px; }
        .filters button { padding: 8px 15px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .filters button:hover { background: #0056b3; }
        .miniature-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 20px; }
        .miniature-item { background: #f0f0f0; padding: 15px; border-radius: 8px; text-align: center; }
        .miniature-item img { max-width: 100%; height: 100px; object-fit: cover; border-radius: 5px; margin-bottom: 10px; }
        .miniature-item h4 { margin: 5px 0; }
        .miniature-item p { margin: 2px 0; font-size: 0.9em; }
        .miniature-item input[type="checkbox"] { margin-right: 5px; }
        .miniature-item input[type="number"] { width: 60px; padding: 5px; border: 1px solid #ccc; border-radius: 3px; text-align: center; }
        .whatsapp-btn { background: #25D366; color: white; padding: 5px 10px; border-radius: 5px; text-decoration: none; font-weight: bold; display: inline-block; margin-left: 5px; font-size: 0.8em; }
        .whatsapp-btn:hover { background: #1DA851; }
        .submit-all-btn { display: block; width: 100%; padding: 12px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; margin-top: 20px; }
        .submit-all-btn:hover { background: #218838; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Reservar Miniaturas</h2>
        {% if not session.user_id %}
        <div class="flash flash-error">
            <p>Faça <a href="{{ url_for('login') }}">login</a> para reservar.</p>
        </div>
        {% else %}
        <div class="filters">
            <form method="GET" id="filter-form">
                <label for="filter_available">Disponíveis:</label>
                <input type="checkbox" id="filter_available" name="available" value="1" {% if request.args.get('available') == '1' %}checked{% endif %} onchange="this.form.submit()">
                
                <label for="sort_by">Ordenar por:</label>
                <select id="sort_by" name="sort_by" onchange="this.form.submit()">
                    <option value="service" {% if request.args.get('sort_by') == 'service' %}selected{% endif %}>Nome</option>
                    <option value="previsao_chegada" {% if request.args.get('sort_by') == 'previsao_chegada' %}selected{% endif %}>Previsão Chegada</option>
                </select>

                <label for="filter_marca">Marca:</label>
                <input type="text" id="filter_marca" name="marca" placeholder="Filtrar por marca" value="{{ request.args.get('marca', '') }}" onchange="this.form.submit()">
                
                <button type="submit">Aplicar Filtros</button>
            </form>
        </div>

        <form method="POST">
            <div class="miniature-list">
                {% for thumb in thumbnails %}
                <div class="miniature-item">
                    <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}">
                    <h4>{{ thumb.service }}</h4>
                    <p>Marca: {{ thumb.marca }}</p>
                    <p>Preço: R$ {{ thumb.price }}</p>
                    <p>Disponível: {{ thumb.quantity }}</p>
                    {% if thumb.quantity > 0 %}
                        <input type="checkbox" name="selected_services" value="{{ thumb.service }}" id="service_{{ loop.index }}">
                        <label for="service_{{ loop.index }}">Reservar</label>
                        <input type="number" name="quantity_{{ thumb.service }}" value="1" min="1" max="{{ thumb.quantity }}" {% if thumb.quantity == 0 %}disabled{% endif %}>
                    {% else %}
                        <p>Esgotado</p>
                        <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, estou na fila de espera para {{ thumb.service }}. Meu email: {{ session.email if session.user_id else 'anônimo' }}" class="whatsapp-btn" target="_blank">Fila WhatsApp</a>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
            <button type="submit" class="submit-all-btn">Confirmar Reservas Selecionadas</button>
        </form>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a></p>
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
    <title>Meu Perfil - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; background: #e9ecef; margin: 10px 0; border-radius: 5px; }
        li.approved { background: #d4edda; }
        li.denied { background: #f8d7da; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Meu Perfil</h2>
        <p><strong>Nome:</strong> {{ user_data.name }}</p>
        <p><strong>Email:</strong> {{ user_data.email }}</p>
        <p><strong>Telefone:</strong> {{ user_data.phone }}</p>
        <p><strong>Data de Cadastro:</strong> {{ user_data.data_cadastro }}</p>
        <h3>Minhas Reservas:</h3>
        <ul>
            {% for res in reservations %}
            <li class="{{ 'approved' if res.status == 'approved' else 'denied' if res.status == 'denied' else 'pending' }}">
                {% if res.denied_reason %} | <em>Motivo rejeitado: {{ res.denied_reason }}</em>{% endif %}
            </li>
            {% endfor %}
            {% if not reservations %}
            <li>Nenhuma reserva encontrada. <a href="{{ url_for('reservar') }}">Faça uma agora!</a></li>
            {% endif %}
        </ul>
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout</a></p>
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
    <title>Admin - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2, h3 { text-align: center; color: #333; }
        .stats { display: flex; justify-content: space-around; margin: 20px 0; }
        .stat-box { background: #e9ecef; padding: 20px; border-radius: 10px; text-align: center; flex: 1; margin: 0 10px; }
        .actions { margin-left: 10px; }
        button, .btn-link { padding: 8px 12px; margin: 0 5px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; }
        .approve { background: #28a745; color: white; }
        .deny { background: #dc3545; color: white; }
        .backup { background: #17a2b8; color: white; }
        .sync { background: #ffc107; color: black; }
        .promote { background: #007bff; color: white; }
        .demote { background: #6c757d; color: white; }
        .delete { background: #dc3545; color: white; }
        input[type="text"], input[type="number"], input[type="email"], input[type="url"], select, input[type="date"] { padding: 8px; margin: 5px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; width: 100%; max-width: 250px; }
        .form-group { margin-bottom: 10px; }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; background: #e9ecef; margin: 10px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        li span { margin-right: 10px; }
        .filters { margin: 20px 0; text-align: center; display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
        .filters form { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
        .new-form-section { background: #f8f9fa; padding: 20px; border-radius: 10px; margin: 20px 0; }
        .new-form-section form { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
        .new-form-section form button { max-width: 200px; margin-top: 10px; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
        .table-actions { display: flex; gap: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Painel Admin</h2>
        <div class="stats">
            <div class="stat-box">
                <h3>Usuários</h3>
                <p>{{ users|length }}</p>
            </div>
            <div class="stat-box">
                <h3>Reservas Pendentes</h3>
                <p>{{ pending_reservations|length }}</p>
            </div>
            <div class="stat-box">
                <h3>Total Reservas</h3>
                <p>{{ all_reservations|length }}</p>
            </div>
        </div>
        <div class="actions" style="text-align: center; margin: 20px 0;">
            <a href="{{ url_for('backup_db') }}" class="btn-link backup">Backup DB (JSON)</a>
            <a href="{{ url_for('export_csv') }}" class="btn-link backup">Export Reservas (CSV)</a>
            <form method="POST" style="display: inline;">
                <button type="submit" name="action" value="sync_stock" class="sync">Sync Estoque da Planilha</button>
            </form>
        </div>

        <div class="new-form-section">
            <h3>Inserir Nova Miniatura</h3>
            <form method="POST">
                <input type="hidden" name="action" value="add_miniature">
                <input type="text" name="service" placeholder="Nome da Miniatura" required>
                <input type="text" name="marca" placeholder="Marca/Fabricante" required>
                <input type="text" name="obs" placeholder="Observações">
                <input type="text" name="previsao_chegada" placeholder="Previsão de Chegada">
                <input type="number" name="quantity" placeholder="Quantidade Inicial" min="0" required>
                <input type="text" name="price" placeholder="Preço (ex: 25.00)" required>
                <input type="url" name="thumbnail_url" placeholder="URL da Imagem">
                <button type="submit">Adicionar Miniatura</button>
            </form>
        </div>

        <div class="new-form-section">
            <h3>Inserir Nova Reserva</h3>
            <form method="POST">
                <input type="hidden" name="action" value="create_res">
                <select name="user_id" required>
                    <option value="">Selecione Usuário</option>
                    {% for user in users %}
                    <option value="{{ user.id }}">{{ user.email }}</option>
                    {% endfor %}
                </select>
                <select name="service" required>
                    <option value="">Selecione Miniatura</option>
                    {% for thumb in thumbnails %}
                    <option value="{{ thumb.service }}" data-quantity="{{ thumb.quantity }}">{{ thumb.service }} (Estoque: {{ thumb.quantity }})</option>
                    {% endfor %}
                </select>
                <input type="number" name="quantity" placeholder="Quantidade" min="1" required>
                <select name="status">
                    <option value="pending">Pendente</option>
                    <option value="approved">Aprovada</option>
                    <option value="denied">Rejeitada</option>
                </select>
                <input type="text" name="reason" placeholder="Motivo (opcional)">
                <button type="submit">Criar Reserva</button>
            </form>
        </div>

        <div class="filters">
            <h3>Filtros Usuários</h3>
            <form method="GET">
                <input type="text" name="user_search" placeholder="Nome/Email/Telefone" value="{{ request.args.get('user_search', '') }}">
                <select name="user_role">
                    <option value="">Todos Roles</option>
                    <option value="user" {% if request.args.get('user_role') == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if request.args.get('user_role') == 'admin' %}selected{% endif %}>Admin</option>
                </select>
                <button type="submit">Filtrar Usuários</button>
            </form>
        </div>
        <h3>Usuários ({{ filtered_users|length }})</h3>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Nome</th>
                    <th>Email</th>
                    <th>Telefone</th>
                    <th>Role</th>
                    <th>Cadastro</th>
                    <th>Ações</th>
                </tr>
            </thead>
            <tbody>
                {% for user in filtered_users %}
                <tr>
                    <td>{{ user.id }}</td>
                    <td>{{ user.name }}</td>
                    <td>{{ user.email }}</td>
                    <td>{{ user.phone }}</td>
                    <td>{{ user.role }}</td>
                    <td>{{ user.data_cadastro }}</td>
                    <td class="table-actions">
                        {% if user.role != 'admin' %}
                        <a href="{{ url_for('admin', promote_=user.id) }}" class="btn-link promote">Promover</a>
                        <a href="{{ url_for('admin', demote_=user.id) }}" class="btn-link demote" onclick="return confirm('Rebaixar este usuário?')">Rebaixar</a>
                        <a href="{{ url_for('admin', delete_user_=user.id) }}" class="btn-link delete" onclick="return confirm('Deletar este usuário e todas as suas reservas?')">Deletar</a>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="filters">
            <h3>Filtros Reservas</h3>
            <form method="GET">
                <input type="text" name="res_search" placeholder="Miniatura/Email do Usuário" value="{{ request.args.get('res_search', '') }}">
                <select name="res_status">
                    <option value="">Todos Status</option>
                    <option value="pending" {% if request.args.get('res_status') == 'pending' %}selected{% endif %}>Pendente</option>
                    <option value="approved" {% if request.args.get('res_status') == 'approved' %}selected{% endif %}>Aprovada</option>
                    <option value="denied" {% if request.args.get('res_status') == 'denied' %}selected{% endif %}>Rejeitada</option>
                </select>
                <button type="submit">Filtrar Reservas</button>
            </form>
        </div>
        <h3>Reservas Pendentes ({{ filtered_pending|length }})</h3>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Miniatura</th>
                    <th>Quantidade</th>
                    <th>Usuário</th>
                    <th>Data Criação</th>
                    <th>Ações</th>
                </tr>
            </thead>
            <tbody>
                {% for res in filtered_pending %}
                <tr>
                    <td>{{ res.id }}</td>
                    <td>{{ res.service }}</td>
                    <td>{{ res.quantity }}</td>
                    <td>{{ res.user_email }}</td>
                    <td>{{ res.created_at }}</td>
                    <td class="table-actions">
                        <form method="POST" style="display: inline;">
                            <input type="hidden" name="action" value="approve">
                            <input type="hidden" name="res_id" value="{{ res.id }}">
                            <button type="submit" class="approve">Aprovar</button>
                        </form>
                        <form method="POST" style="display: inline;">
                            <input type="hidden" name="action" value="deny">
                            <input type="hidden" name="res_id" value="{{ res.id }}">
                            <input type="text" name="reason" placeholder="Motivo" required style="width: 100px;">
                            <button type="submit" class="deny">Rejeitar</button>
                        </form>
                        <a href="{{ url_for('admin', delete_res_=res.id) }}" class="btn-link delete" onclick="return confirm('Deletar esta reserva?')">Deletar</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <h3>Todas as Reservas ({{ filtered_all_reservations|length }})</h3>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Miniatura</th>
                    <th>Quantidade</th>
                    <th>Usuário</th>
                    <th>Status</th>
                    <th>Motivo Rejeição</th>
                    <th>Data Criação</th>
                    <th>Ações</th>
                </tr>
            </thead>
            <tbody>
                {% for res in filtered_all_reservations %}
                <tr class="{{ res.status }}">
                    <td>{{ res.id }}</td>
                    <td>{{ res.service }}</td>
                    <td>{{ res.quantity }}</td>
                    <td>{{ res.user_email }}</td>
                    <td>{{ res.status.title() }}</td>
                    <td>{{ res.denied_reason or '-' }}</td>
                    <td>{{ res.created_at }}</td>
                    <td class="table-actions">
                        <a href="{{ url_for('admin', delete_res_=res.id) }}" class="btn-link delete" onclick="return confirm('Deletar esta reserva?')">Deletar</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}" style="margin: 20px 0;">
                    {{ message }}
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout Admin</a></p>
    </div>
</body>
</html>
'''

# --- Rotas ---
@app.route('/', methods=['GET'])
def index():
    thumbnails = load_thumbnails()
    return render_template_string(INDEX_HTML, logo_url=LOGO_URL, thumbnails=thumbnails, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        if not is_valid_email(email):
            flash('Email inválido.', 'error')
            return render_template_string(LOGIN_HTML)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT id, email, password, role, name, phone FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        if user and bcrypt.check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['email'] = user[1]
            session['role'] = user[3]
            session['name'] = user[4]
            session['phone'] = user[5]
            logger.info(f"Login bem-sucedido para {email}")
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Email ou senha incorretos.', 'error')
            logger.warning(f"Falha de login para {email}")
    return render_template_string(LOGIN_HTML)

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
            flash('Telefone inválido (apenas números, 10 ou 11 dígitos).', 'error')
            return render_template_string(REGISTER_HTML)
        if len(password) < 6:
            flash('Senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML)

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)", (name, email, phone, hashed_password))
            conn.commit()
            logger.info(f"Registro bem-sucedido para {email}")
            flash('Registro realizado! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email já cadastrado. Faça login.', 'error')
        except Exception as e:
            logger.error(f"Erro no registro: {e}")
            flash('Erro interno. Tente novamente.', 'error')
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML)

@app.route('/reserve_single/<service>', methods=['GET', 'POST'])
def reserve_single(service):
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT service, quantity, price, marca, obs, thumbnail_url, previsao_chegada FROM stock WHERE service = ?", (service,))
    stock_item = c.fetchone()
    conn.close()

    if not stock_item:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('index'))

    thumb = {
        'service': stock_item[0],
        'quantity': stock_item[1],
        'price': stock_item[2],
        'marca': stock_item[3],
        'obs': stock_item[4],
        'thumbnail_url': stock_item[5],
        'previsao_chegada': stock_item[6]
    }
    current_stock = thumb['quantity']

    if request.method == 'POST':
        try:
            quantity_to_reserve = int(request.form['quantity'])
        except ValueError:
            flash('Quantidade inválida.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, current_stock=current_stock, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

        if quantity_to_reserve <= 0 or quantity_to_reserve > current_stock:
            flash('Quantidade inválida ou insuficiente no estoque.', 'error')
            return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, current_stock=current_stock, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)",
                      (session['user_id'], service, quantity_to_reserve))
            c.execute("UPDATE stock SET quantity = quantity - ? WHERE service = ?", (quantity_to_reserve, service))
            conn.commit()
            flash(f'{quantity_to_reserve} {service} reservada(s) com sucesso!', 'success')
            return redirect(url_for('profile'))
        except Exception as e:
            conn.rollback()
            logger.error(f"Erro ao reservar miniatura {service}: {e}")
            flash('Erro ao processar reserva. Tente novamente.', 'error')
        finally:
            conn.close()

    return render_template_string(RESERVE_SINGLE_HTML, thumb=thumb, current_stock=current_stock, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))

    all_thumbnails = load_thumbnails()
    filtered_thumbnails = all_thumbnails

    # --- Lógica de Filtros ---
    available_filter = request.args.get('available') == '1'
    sort_by = request.args.get('sort_by', 'service')
    marca_filter = request.args.get('marca', '').strip().lower()

    if available_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
    
    if marca_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]

    if sort_by == 'previsao_chegada':
        filtered_thumbnails.sort(key=lambda x: x.get('previsao_chegada', ''))
    else: # Default to service
        filtered_thumbnails.sort(key=lambda x: x.get('service', ''))

    if request.method == 'POST':
        selected_services = request.form.getlist('selected_services')
        if not selected_services:
            flash('Nenhuma miniatura selecionada.', 'error')
            return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        reservations_made = 0
        try:
            for service_name in selected_services:
                quantity_input_name = f'quantity_{service_name}'
                try:
                    quantity_to_reserve = int(request.form.get(quantity_input_name, 1))
                except ValueError:
                    flash(f'Quantidade inválida para {service_name}.', 'error')
                    continue

                c.execute("SELECT quantity FROM stock WHERE service = ?", (service_name,))
                stock_row = c.fetchone()
                current_stock = stock_row[0] if stock_row else 0

                if quantity_to_reserve <= 0 or quantity_to_reserve > current_stock:
                    flash(f'Quantidade inválida ou insuficiente para {service_name}.', 'error')
                    continue

                c.execute("INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)",
                          (session['user_id'], service_name, quantity_to_reserve))
                c.execute("UPDATE stock SET quantity = quantity - ? WHERE service = ?", (quantity_to_reserve, service_name))
                reservations_made += 1
            
            conn.commit()
            if reservations_made > 0:
                flash(f'{reservations_made} reserva(s) realizada(s) com sucesso!', 'success')
                return redirect(url_for('profile'))
            else:
                flash('Nenhuma reserva foi concluída.', 'error')

        except Exception as e:
            conn.rollback()
            logger.error(f"Erro ao processar múltiplas reservas: {e}")
            flash('Erro ao processar reservas. Tente novamente.', 'error')
        finally:
            conn.close()

    return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/profile', methods=['GET'])
def profile():
    if 'user_id' not in session:
        flash('Faça login para ver perfil.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    c.execute("SELECT name, email, phone, data_cadastro FROM users WHERE id = ?", (session['user_id'],))
    user_data_row = c.fetchone()
    user_data = {
        'name': user_data_row[0],
        'email': user_data_row[1],
        'phone': user_data_row[2],
        'data_cadastro': user_data_row[3]
    } if user_data_row else {'name': 'N/A', 'email': 'N/A', 'phone': 'N/A', 'data_cadastro': 'N/A'}

    c.execute("""
        SELECT r.id, r.service, r.quantity, r.status, r.denied_reason, r.created_at
        FROM reservations r 
        WHERE r.user_id = ? 
        ORDER BY r.created_at DESC
    """, (session['user_id'],))
    reservations_raw = c.fetchall()
    conn.close()

    reservations = []
    for res in reservations_raw:
        reservations.append({
            'id': res[0],
            'service': res[1],
            'quantity': res[2],
            'status': res[3],
            'denied_reason': res[4],
            'created_at': res[5]
        })

    return render_template_string(PROFILE_HTML, user_data=user_data, reservations=reservations)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado. Apenas para administradores.', 'error')
        return redirect(url_for('index'))

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # --- Processar Ações POST ---
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'sync_stock':
            if gc:
                try:
                    sheet = gc.open("BASE DE DADOS JG").sheet1
                    records = sheet.get_all_records()
                    if not records:
                        flash("Planilha vazia - nenhum dado para sincronizar.", 'error')
                    else:
                        for record in records[1:]: # Pula o cabeçalho
                            service = record.get('NOME DA MINIATURA', '').strip()
                            quantity = record.get('QUANTIDADE DISPONIVEL', 0)
                            price_raw = record.get('VALOR', '')
                            price_str = str(price_raw) if price_raw is not None else ''
                            price = price_str.replace('R$ ', '').replace(',', '.') if price_str else '0'
                            marca = record.get('MARCA/FABRICANTE', '').strip()
                            obs = record.get('OBSERVAÇÕES', '').strip()
                            thumbnail_url = record.get('IMAGEM', LOGO_URL).strip()
                            previsao_chegada = record.get('PREVISÃO DE CHEGADA', '').strip()

                            if service: # Apenas sincroniza se o nome do serviço não for vazio
                                c.execute("INSERT OR REPLACE INTO stock (service, quantity, price, marca, obs, thumbnail_url, previsao_chegada, last_sync) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                                          (service, int(quantity), price, marca, obs, thumbnail_url, previsao_chegada))
                        conn.commit()
                        flash('Estoque sincronizado da planilha!', 'success')
                except Exception as e:
                    logger.error(f"Erro ao sincronizar estoque da planilha: {e}")
                    flash(f"Erro ao sincronizar estoque: {e}", 'error')
            else:
                flash("Integração com Google Sheets não configurada.", 'error')

        elif action == 'approve':
            res_id = request.form.get('res_id')
            c.execute("UPDATE reservations SET status = 'approved', approved_by = ? WHERE id = ?", (session['user_id'], res_id))
            conn.commit()
            flash('Reserva aprovada.', 'success')
            logger.info(f"Admin {session['email']} aprovou reserva {res_id}")

        elif action == 'deny':
            res_id = request.form.get('res_id')
            reason = request.form.get('reason', 'Motivo não especificado')
            c.execute("UPDATE reservations SET status = 'denied', denied_reason = ? WHERE id = ?", (reason, res_id))
            conn.commit()
            flash('Reserva rejeitada.', 'success')
            logger.info(f"Admin {session['email']} rejeitou reserva {res_id}: {reason}")

        elif action == 'add_miniature':
            service = request.form.get('service').strip()
            marca = request.form.get('marca').strip()
            obs = request.form.get('obs').strip()
            previsao_chegada = request.form.get('previsao_chegada').strip()
            quantity = int(request.form.get('quantity', 0))
            price = request.form.get('price').strip()
            thumbnail_url = request.form.get('thumbnail_url').strip()

            if not service or not marca or quantity < 0 or not price:
                flash('Nome, marca, quantidade e preço são obrigatórios para a miniatura.', 'error')
            else:
                try:
                    c.execute("INSERT INTO stock (service, quantity, price, marca, obs, thumbnail_url, previsao_chegada, last_sync) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                              (service, quantity, price, marca, obs, thumbnail_url, previsao_chegada))
                    conn.commit()
                    flash(f'Miniatura "{service}" adicionada ao estoque!', 'success')
                except sqlite3.IntegrityError:
                    flash(f'Miniatura "{service}" já existe no estoque.', 'error')
                except Exception as e:
                    flash(f'Erro ao adicionar miniatura: {e}', 'error')
                    logger.error(f"Erro ao adicionar miniatura: {e}")

        elif action == 'create_res':
            user_id = request.form.get('user_id')
            service = request.form.get('service')
            quantity = int(request.form.get('quantity', 1))
            status = request.form.get('status', 'pending')
            reason = request.form.get('reason', '')

            if not user_id or not service or quantity <= 0:
                flash('Usuário, miniatura e quantidade são obrigatórios.', 'error')
            else:
                try:
                    c.execute("SELECT quantity FROM stock WHERE service = ?", (service,))
                    current_stock = c.fetchone()[0] if c.fetchone() else 0

                    if quantity > current_stock and status != 'denied': # Permite criar reserva negada mesmo sem estoque
                        flash(f'Estoque insuficiente para {service}. Disponível: {current_stock}.', 'error')
                    else:
                        c.execute("INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)",
                                  (user_id, service, quantity, status, reason))
                        if status == 'approved':
                            c.execute("UPDATE stock SET quantity = quantity - ? WHERE service = ?", (quantity, service))
                        conn.commit()
                        flash('Nova reserva criada!', 'success')
                except Exception as e:
                    flash(f'Erro ao criar reserva: {e}', 'error')
                    logger.error(f"Erro ao criar reserva: {e}")

        # Redireciona para GET após POST para evitar reenvio de formulário
        return redirect(url_for('admin'))

    # --- Lógica de Filtros e Exibição GET ---

    # Usuários
    user_search = request.args.get('user_search', '').strip()
    user_role = request.args.get('user_role', '').strip()
    user_query = "SELECT id, name, email, phone, password, role, data_cadastro FROM users WHERE 1=1"
    user_params = []
    if user_search:
        user_query += " AND (name LIKE ? OR email LIKE ? OR phone LIKE ?)"
        user_params.extend([f"%{user_search}%", f"%{user_search}%", f"%{user_search}%"])
    if user_role:
        user_query += " AND role = ?"
        user_params.append(user_role)
    c.execute(user_query, user_params)
    users_raw = c.fetchall()
    users = [dict(zip(['id', 'name', 'email', 'phone', 'password', 'role', 'data_cadastro'], row)) for row in users_raw]
    filtered_users = users

    # Processar ações GET (promote/demote/delete)
    if 'promote_' in request.args:
        user_id = int(request.args['promote_'][8:])
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
        conn.commit()
        flash('Usuário promovido para admin.', 'success')
        return redirect(url_for('admin'))
    if 'demote_' in request.args:
        user_id = int(request.args['demote_'][7:])
        if user_id != session['user_id']: # Não permite rebaixar a si mesmo
            c.execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id,))
            conn.commit()
            flash('Usuário rebaixado para user.', 'success')
        else:
            flash('Você não pode rebaixar a si mesmo.', 'error')
        return redirect(url_for('admin'))
    if 'delete_user_' in request.args:
        user_id = int(request.args['delete_user_'][11:])
        if user_id != session['user_id']: # Não permite deletar a si mesmo
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            c.execute("DELETE FROM reservations WHERE user_id = ?", (user_id,))
            conn.commit()
            flash('Usuário e suas reservas deletados.', 'success')
        else:
            flash('Você não pode deletar a si mesmo.', 'error')
        return redirect(url_for('admin'))
    if 'delete_res_' in request.args:
        res_id = int(request.args['delete_res_'][10:])
        c.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
        conn.commit()
        flash('Reserva deletada.', 'success')
        return redirect(url_for('admin'))

    # Reservas
    res_search = request.args.get('res_search', '').strip()
    res_status = request.args.get('res_status', '').strip()

    reservations_query = """
        SELECT r.id, r.service, r.quantity, r.status, r.denied_reason, u.email as user_email, r.created_at
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        WHERE 1=1
    """
    reservations_params = []

    if res_search:
        reservations_query += " AND (r.service LIKE ? OR u.email LIKE ?)"
        reservations_params.extend([f"%{res_search}%", f"%{res_search}%"])
    if res_status:
        reservations_query += " AND r.status = ?"
        reservations_params.append(res_status)
    
    reservations_query += " ORDER BY r.created_at DESC"
    c.execute(reservations_query, reservations_params)
    all_reservations_raw = c.fetchall()
    
    all_reservations = []
    for res in all_reservations_raw:
        all_reservations.append({
            'id': res[0], 'service': res[1], 'quantity': res[2], 'status': res[3],
            'denied_reason': res[4], 'user_email': res[5], 'created_at': res[6]
        })
    
    filtered_all_reservations = all_reservations
    filtered_pending = [res for res in all_reservations if res['status'] == 'pending']

    # Miniaturas para formulário de nova reserva
    thumbnails = load_thumbnails() # Carrega do DB de estoque

    conn.close()
    return render_template_string(ADMIN_HTML, 
                                  users=users, 
                                  filtered_users=filtered_users,
                                  pending_reservations=filtered_pending, 
                                  filtered_pending=filtered_pending,
                                  all_reservations=all_reservations,
                                  filtered_all_reservations=filtered_all_reservations,
                                  thumbnails=thumbnails,
                                  logo_url=LOGO_URL)

@app.route('/backup')
def backup_db():
    if session.get('role') != 'admin':
        abort(403)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Fetch all data from users table
    c.execute("SELECT id, name, email, phone, password, role, data_cadastro FROM users")
    users_data = c.fetchall()
    users_columns = [description[0] for description in c.description]
    
    # Fetch all data from reservations table
    c.execute("SELECT id, user_id, service, quantity, status, approved_by, denied_reason, created_at FROM reservations")
    reservations_data = c.fetchall()
    reservations_columns = [description[0] for description in c.description]

    # Fetch all data from stock table
    c.execute("SELECT id, service, quantity, price, marca, obs, thumbnail_url, previsao_chegada, last_sync FROM stock")
    stock_data = c.fetchall()
    stock_columns = [description[0] for description in c.description]
    
    conn.close()

    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(zip(users_columns, row)) for row in users_data],
        'reservations': [dict(zip(reservations_columns, row)) for row in reservations_data],
        'stock': [dict(zip(stock_columns, row)) for row in stock_data]
    }
    
    filename = f"jgminis_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
    return Response(json.dumps(backup_data, indent=2, ensure_ascii=False).encode('utf-8'),
                    mimetype='application/json', headers={'Content-Disposition': f'attachment; filename={filename}'})

@app.route('/export_csv')
def export_csv():
    if session.get('role') != 'admin':
        abort(403)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT r.id, u.name, u.email, u.phone, r.service, r.quantity, r.status, r.denied_reason, r.created_at 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id
        ORDER BY r.created_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID Reserva', 'Nome Usuário', 'Email Usuário', 'Telefone Usuário', 'Miniatura', 'Quantidade', 'Status', 'Motivo Rejeição', 'Data Criação'])
    writer.writerows(rows)
    
    filename = f"jgminis_reservas_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv"
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'})

@app.route('/logout', methods=['GET'])
def logout():
    if 'user_id' in session:
        logger.info(f"Logout de {session['email']}")
    session.clear()
    flash('Logout realizado com sucesso!', 'success')
    return redirect(url_for('index'))

@app.route('/favicon.ico')
def favicon():
    return '', 204

# --- Error Handlers ---
@app.errorhandler(404)
def not_found_error(error):
    return render_template_string('<h1>404 - Página Não Encontrada</h1><p><a href="/">Voltar ao Home</a></p>'), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Erro interno 500: {error}")
    return render_template_string('<h1>500 - Erro Interno</h1><p>Algo deu errado. Tente novamente.</p><a href="/">Home</a>'), 500

# --- Execução do App ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = '0.0.0.0'
    app.run(host=host, port=port, debug=False)
    logger.info(f"App rodando em {host}:{port}")
