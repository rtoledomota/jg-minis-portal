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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290') # Apenas números, sem +55 ou espaços
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025_dev_key_fallback')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

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
    # Tabela de Reservas (com quantity, sem date)
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
def load_thumbnails_from_db():
    thumbnails = []
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT service, quantity FROM stock ORDER BY service")
    stock_data = {row[0]: row[1] for row in c.fetchall()}
    conn.close()

    if gc:
        try:
            sheet = gc.open("BASE DE DADOS JG").sheet1
            records = sheet.get_all_records()
            if not records:
                raise Exception("Planilha vazia - adicione dados nas linhas 2+")

            # Processa até 12 itens (ou mais, se necessário)
            for record in records[1:13]: # records[1:] para todas as linhas
                service = record.get('NOME DA MINIATURA', 'Miniatura Desconhecida')
                marca = record.get('MARCA/FABRICANTE', '')
                obs = record.get('OBSERVAÇÕES', '')
                previsao_chegada = record.get('PREVISÃO DE CHEGADA', '')
                
                # Pega a quantidade do DB de estoque, se existir, senão usa 0
                quantity = stock_data.get(service, 0) 

                description = f"{marca} - {obs}".strip(' - ')
                thumbnail_url = record.get('IMAGEM', LOGO_URL)
                price_raw = record.get('VALOR', '')
                price_str = str(price_raw) if price_raw is not None else ''
                price = price_str.replace('R$ ', '').replace(',', '.') if price_str else '0'
                
                thumbnails.append({
                    'service': service,
                    'description': description or 'Descrição disponível',
                    'thumbnail_url': thumbnail_url,
                    'price': price,
                    'quantity': int(quantity),
                    'marca': marca,
                    'previsao_chegada': previsao_chegada
                })
            logger.info(f"Carregados {len(thumbnails)} thumbnails da planilha e estoque DB")
        except Exception as e:
            logger.error(f"Erro ao carregar planilha/estoque: {e}")
            thumbnails = [{'service': 'Fallback', 'description': 'Serviço em manutenção. Contate-nos!', 'thumbnail_url': LOGO_URL, 'price': '0', 'quantity': 0, 'marca': '', 'previsao_chegada': ''}]
    else:
        thumbnails = [{'service': 'Sem Sheets', 'description': 'Configure GOOGLE_SHEETS_CREDENTIALS', 'thumbnail_url': LOGO_URL, 'price': 'Consultar', 'quantity': 0, 'marca': '', 'previsao_chegada': ''}]
    return thumbnails

# --- Templates HTML (como strings Jinja2) ---

INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2 - Serviços</title>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 20px; background: #f8f9fa; color: #333; }
        header { text-align: center; padding: 20px; background: #004085; color: white; } /* Azul mais escuro */
        img.logo { width: 150px; height: auto; margin: 10px; }
        .thumbnails { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; padding: 20px; }
        .thumbnail { background: white; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); padding: 15px; text-align: center; transition: transform 0.2s; }
        .thumbnail:hover { transform: scale(1.05); }
        .thumbnail img { width: 100%; height: 150px; object-fit: cover; border-radius: 8px; }
        .thumbnail h3 { margin: 10px 0; color: #007bff; }
        .thumbnail p { margin: 5px 0; }
        nav { text-align: center; padding: 10px; background: #e9ecef; }
        nav a { margin: 0 15px; color: #007bff; text-decoration: none; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 10px; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        footer { text-align: center; padding: 10px; background: #343a40; color: white; margin-top: 40px; }
        .reserve-button { display: inline-block; padding: 8px 15px; margin-top: 10px; border-radius: 5px; text-decoration: none; font-weight: bold; }
        .reserve-button.available { background: #28a745; color: white; }
        .reserve-button.whatsapp { background: #ffc107; color: black; }
        @media (max-width: 600px) { .thumbnails { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" class="logo" onerror="this.src='{{ logo_url }}'">
        <h1>Bem-vindo ao JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        <a href="{{ url_for('reservar') }}">Reservar Miniaturas</a>
        {% if not session.user_id %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% endif %}
        {% if session.user_id %}
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
            {% if thumb.quantity > 0 %}
                <a href="{{ url_for('reserve_single', service=thumb.service) }}" class="reserve-button available">Reservar</a>
            {% else %}
                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá! Tenho interesse na miniatura {{ thumb.service }} e gostaria de entrar na fila de espera." target="_blank" class="reserve-button whatsapp">Fila de Espera (WhatsApp)</a>
            {% endif %}
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
            <input type="tel" name="phone" placeholder="Telefone (apenas números)" required pattern="[0-9]{10,11}" title="Telefone deve conter 10 ou 11 dígitos numéricos">
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
    <title>Reservar {{ service }} - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .form-container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 350px; text-align: center; }
        img { max-width: 100%; height: 200px; object-fit: cover; border-radius: 8px; margin-bottom: 15px; }
        input[type="number"] { width: calc(100% - 22px); padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #218838; }
        .whatsapp-button { background: #ffc107; color: black; margin-top: 10px; }
        .whatsapp-button:hover { background: #e0a800; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="form-container">
        <h2>Reservar {{ service }}</h2>
        <img src="{{ thumbnail_url }}" alt="{{ service }}" onerror="this.src='{{ logo_url }}'">
        <p>Disponível: {{ quantity }}</p>
        <p>Preço: R$ {{ price }}</p>

        {% if not session.user_id %}
        <div class="flash flash-error">
            <p>Faça <a href="{{ url_for('login') }}">login</a> para reservar.</p>
        </div>
        {% elif quantity > 0 %}
        <form method="POST">
            <input type="hidden" name="service" value="{{ service }}">
            <label for="quantity_to_reserve">Quantidade:</label>
            <input type="number" name="quantity_to_reserve" id="quantity_to_reserve" min="1" max="{{ quantity }}" value="1" required>
            <button type="submit">Confirmar Reserva</button>
        </form>
        {% else %}
        <p>Estoque esgotado para esta miniatura.</p>
        <a href="https://wa.me/{{ whatsapp_number }}?text=Olá! Tenho interesse na miniatura {{ service }} e gostaria de entrar na fila de espera." target="_blank" class="whatsapp-button button">Fila de Espera (WhatsApp)</a>
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
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        .filters { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-bottom: 20px; padding: 15px; background: #e9ecef; border-radius: 8px; }
        .filters label { align-self: center; font-weight: bold; }
        .filters select, .filters input[type="text"] { padding: 8px; border: 1px solid #ddd; border-radius: 5px; }
        .filters button { padding: 8px 15px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .filters button:hover { background: #0056b3; }
        .miniature-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }
        .miniature-item { background: #f8f9fa; border: 1px solid #ddd; border-radius: 8px; padding: 15px; text-align: center; }
        .miniature-item img { max-width: 100%; height: 120px; object-fit: cover; border-radius: 5px; margin-bottom: 10px; }
        .miniature-item h3 { margin: 5px 0; font-size: 1.1em; }
        .miniature-item p { margin: 3px 0; font-size: 0.9em; }
        .miniature-item input[type="checkbox"] { margin-right: 5px; }
        .miniature-item input[type="number"] { width: 60px; padding: 5px; border: 1px solid #ccc; border-radius: 4px; text-align: center; }
        .submit-reservations { text-align: center; margin-top: 30px; }
        .submit-reservations button { padding: 12px 25px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 1.1em; font-weight: bold; }
        .submit-reservations button:hover { background: #218838; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .whatsapp-button { display: inline-block; padding: 8px 15px; margin-top: 10px; border-radius: 5px; text-decoration: none; font-weight: bold; background: #ffc107; color: black; }
        .whatsapp-button:hover { background: #e0a800; }
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
                <input type="checkbox" id="filter_available" name="available" value="true" {% if request.args.get('available') == 'true' %}checked{% endif %} onchange="document.getElementById('filter-form').submit()">

                <label for="sort_by">Ordenar por:</label>
                <select id="sort_by" name="sort_by" onchange="document.getElementById('filter-form').submit()">
                    <option value="">Padrão</option>
                    <option value="name_asc" {% if request.args.get('sort_by') == 'name_asc' %}selected{% endif %}>Nome (A-Z)</option>
                    <option value="name_desc" {% if request.args.get('sort_by') == 'name_desc' %}selected{% endif %}>Nome (Z-A)</option>
                    <option value="price_asc" {% if request.args.get('sort_by') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                    <option value="price_desc" {% if request.args.get('sort_by') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                    <option value="quantity_asc" {% if request.args.get('sort_by') == 'quantity_asc' %}selected{% endif %}>Estoque (Menor)</option>
                    <option value="quantity_desc" {% if request.args.get('sort_by') == 'quantity_desc' %}selected{% endif %}>Estoque (Maior)</option>
                    <option value="previsao_chegada_asc" {% if request.args.get('sort_by') == 'previsao_chegada_asc' %}selected{% endif %}>Previsão Chegada (Antiga)</option>
                    <option value="previsao_chegada_desc" {% if request.args.get('sort_by') == 'previsao_chegada_desc' %}selected{% endif %}>Previsão Chegada (Recente)</option>
                </select>

                <label for="filter_marca">Marca:</label>
                <input type="text" id="filter_marca" name="marca" placeholder="Filtrar por marca" value="{{ request.args.get('marca', '') }}">
                
                <button type="submit">Aplicar Filtros</button>
            </form>
        </div>

        <form method="POST">
            <div class="miniature-list">
                {% for thumb in thumbnails %}
                <div class="miniature-item">
                    <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
                    <h3>
                        {% if thumb.quantity > 0 %}
                            <input type="checkbox" name="selected_services" value="{{ thumb.service }}" id="service_{{ loop.index }}">
                        {% endif %}
                        <label for="service_{{ loop.index }}">{{ thumb.service }}</label>
                    </h3>
                    <p>{{ thumb.description or 'Descrição disponível' }}</p>
                    <p>Preço: R$ {{ thumb.price or 'Consultar' }}</p>
                    <p>Disponível: {{ thumb.quantity }}</p>
                    {% if thumb.previsao_chegada %}
                        <p>Previsão: {{ thumb.previsao_chegada }}</p>
                    {% endif %}

                    {% if thumb.quantity > 0 %}
                        <label for="quantity_{{ loop.index }}">Qtd:</label>
                        <input type="number" name="quantity_{{ thumb.service }}" id="quantity_{{ loop.index }}" min="1" max="{{ thumb.quantity }}" value="1" {% if not session.user_id %}disabled{% endif %}>
                    {% else %}
                        <a href="https://wa.me/{{ whatsapp_number }}?text=Olá! Tenho interesse na miniatura {{ thumb.service }} e gostaria de entrar na fila de espera." target="_blank" class="whatsapp-button">Fila de Espera (WhatsApp)</a>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
            <div class="submit-reservations">
                <button type="submit" {% if not session.user_id %}disabled{% endif %}>Confirmar Reservas Selecionadas</button>
            </div>
        </form>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p style="text-align: center; margin-top: 20px;"><a href="{{ url_for('index') }}">Voltar ao Home</a></p>
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
        <p><strong>Nome:</strong> {{ session.name }}</p>
        <p><strong>Email:</strong> {{ session.email }}</p>
        <p><strong>Telefone:</strong> {{ session.phone }}</p>
        <p><strong>Data de Cadastro:</strong> {{ data_cadastro }}</p>
        <h3>Minhas Reservas:</h3>
        <ul>
            {% for res in reservations %}
            <li class="{{ 'approved' if res.status == 'approved' else 'denied' if res.status == 'denied' else 'pending' }}">
                <strong>Miniatura:</strong> {{ res.service }} | <strong>Quantidade:</strong> {{ res.quantity }} | <strong>Status:</strong> {{ res.status.title() }}
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
        h2, h3 { text-align: center; color: #333; margin-bottom: 20px; }
        .stats { display: flex; justify-content: space-around; margin: 20px 0; }
        .stat-box { background: #e9ecef; padding: 20px; border-radius: 10px; text-align: center; flex: 1; margin: 0 10px; }
        .actions { margin-left: 10px; }
        button { padding: 5px 10px; margin: 0 5px; border: none; border-radius: 3px; cursor: pointer; }
        .approve { background: #28a745; color: white; }
        .deny { background: #dc3545; color: white; }
        .backup { background: #17a2b8; color: white; }
        .sync { background: #ffc107; color: black; }
        input[type="text"], input[type="number"], input[type="url"], select, input[type="date"] { padding: 8px; border: 1px solid #ddd; border-radius: 5px; margin-bottom: 10px; }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; background: #e9ecef; margin: 10px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        li.pending { background: #fff3cd; }
        li.approved { background: #d4edda; }
        li.denied { background: #f8d7da; }
        .filters, .form-section { margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; }
        .filters form, .form-section form { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
        .form-section form input, .form-section form select { flex: 1 1 auto; min-width: 150px; }
        .form-section form button { flex: 0 0 auto; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
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
            <button onclick="window.location.href='/backup'" class="backup">Backup DB (JSON)</button>
            <button onclick="window.location.href='/export_csv'" class="backup">Export Reservas (CSV)</button>
            <form method="POST" style="display: inline;">
                <button type="submit" name="action" value="sync_stock" class="sync">Sync Estoque da Planilha</button>
            </form>
        </div>

        <div class="form-section">
            <h3>Inserir Nova Miniatura</h3>
            <form method="POST">
                <input type="hidden" name="action" value="create_miniature">
                <input type="text" name="service_name" placeholder="Nome da Miniatura" required>
                <input type="text" name="marca" placeholder="Marca/Fabricante" required>
                <input type="text" name="obs" placeholder="Observações">
                <input type="number" name="price" placeholder="Preço (ex: 25.00)" step="0.01" required>
                <input type="number" name="quantity" placeholder="Quantidade Inicial" min="0" required>
                <input type="url" name="image_url" placeholder="URL da Imagem" required>
                <input type="text" name="previsao_chegada" placeholder="Previsão de Chegada (opcional)">
                <button type="submit">Adicionar Miniatura</button>
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
        <h3>Usuários Cadastrados ({{ filtered_users|length }})</h3>
        <ul>
            {% for user in filtered_users %}
            <li>
                <span>{{ user.name }} ({{ user.email }}) - Tel: {{ user.phone }} - Role: {{ user.role }} - Cadastrado: {{ user.data_cadastro }}</span>
                <span class="actions">
                    {% if user.role != 'admin' %}
                    <a href="?promote_user={{ user.id }}" onclick="return confirm('Promover {{ user.name }} para admin?')">Promover</a>
                    <a href="?demote_user={{ user.id }}" onclick="return confirm('Rebaixar {{ user.name }} para user?')">Rebaixar</a>
                    <a href="?delete_user={{ user.id }}" onclick="return confirm('Deletar {{ user.name }} e suas reservas?')">Deletar</a>
                    {% endif %}
                </span>
            </li>
            {% endfor %}
        </ul>

        <div class="filters">
            <h3>Filtros Reservas</h3>
            <form method="GET">
                <input type="text" name="res_search" placeholder="Miniatura/Email do Usuário" value="{{ request.args.get('res_search', '') }}">
                <select name="res_status">
                    <option value="">Todos Status</option>
                    <option value="pending" {% if request.args.get('res_status') == 'pending' %}selected{% endif %}>Pending</option>
                    <option value="approved" {% if request.args.get('res_status') == 'approved' %}selected{% endif %}>Approved</option>
                    <option value="denied" {% if request.args.get('res_status') == 'denied' %}selected{% endif %}>Denied</option>
                </select>
                <button type="submit">Filtrar Reservas</button>
            </form>
        </div>
        <h3>Reservas Pendentes ({{ filtered_pending|length }})</h3>
        <ul>
            {% for res in filtered_pending %}
            <li class="pending">
                <span>ID {{ res.id }}: {{ res.service }} (Qtd: {{ res.quantity }}) por {{ res.user_email }}</span>
                <span class="actions">
                    <form method="POST" style="display: inline;">
                        <input type="hidden" name="action" value="approve">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        <button type="submit" class="approve">Aprovar</button>
                    </form>
                    <form method="POST" style="display: inline;">
                        <input type="hidden" name="action" value="deny">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        <input type="text" name="reason" placeholder="Motivo" required style="width: 100px; padding: 2px;">
                        <button type="submit" class="deny">Rejeitar</button>
                    </form>
                    <a href="?delete_res={{ res.id }}" onclick="return confirm('Deletar reserva ID {{ res.id }}?')">Deletar</a>
                </span>
            </li>
            {% endfor %}
        </ul>
        <h3>Todas as Reservas ({{ filtered_all_reservations|length }})</h3>
        <ul>
            {% for res in filtered_all_reservations %}
            <li class="{{ res.status }}">
                <span>ID {{ res.id }}: {{ res.service }} (Qtd: {{ res.quantity }}) por {{ res.user_email }} (Status: {{ res.status.title() }})</span>
                {% if res.denied_reason %}<span> - Motivo: {{ res.denied_reason }}</span>{% endif %}
                <span class="actions">
                    <a href="?delete_res={{ res.id }}" onclick="return confirm('Deletar reserva ID {{ res.id }}?')">Deletar</a>
                </span>
            </li>
            {% endfor %}
        </ul>

        <div class="form-section">
            <h3>Inserir Nova Reserva</h3>
            <form method="POST">
                <input type="hidden" name="action" value="create_reservation">
                <select name="user_id" required>
                    <option value="">Selecione Usuário</option>
                    {% for user in users_for_res %}
                    <option value="{{ user.id }}">{{ user.name }} ({{ user.email }})</option>
                    {% endfor %}
                </select>
                <select name="service" required>
                    <option value="">Selecione Miniatura</option>
                    {% for thumb in thumbnails_for_res %}
                    <option value="{{ thumb.service }}" data-quantity="{{ thumb.quantity }}">{{ thumb.service }} (Estoque: {{ thumb.quantity }})</option>
                    {% endfor %}
                </select>
                <input type="number" name="quantity" placeholder="Quantidade" min="1" required>
                <select name="status">
                    <option value="pending">Pendente</option>
                    <option value="approved">Aprovada</option>
                    <option value="denied">Rejeitada</option>
                </select>
                <input type="text" name="denied_reason" placeholder="Motivo (se rejeitada)">
                <button type="submit">Criar Reserva</button>
            </form>
        </div>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}" style="margin: 20px 0;">
                    {{ message }}
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p style="text-align: center; margin-top: 20px;"><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout Admin</a></p>
    </div>
</body>
</html>
'''

# --- Rotas da Aplicação ---

@app.route('/', methods=['GET'])
def index():
    thumbnails = load_thumbnails_from_db()
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
        c.execute("SELECT id, name, email, phone, password, role FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        if user and bcrypt.check_password_hash(user[4], password): # user[4] é a senha
            session['user_id'] = user[0]
            session['name'] = user[1]
            session['email'] = user[2]
            session['phone'] = user[3]
            session['role'] = user[5]
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
            flash('Telefone inválido. Use apenas números (10 ou 11 dígitos).', 'error')
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

@app.route('/reserve_single', methods=['GET', 'POST'])
def reserve_single():
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))

    service_name = request.args.get('service')
    if not service_name:
        flash('Serviço não especificado.', 'error')
        return redirect(url_for('index'))

    thumbnails = load_thumbnails_from_db()
    selected_thumb = next((t for t in thumbnails if t['service'] == service_name), None)

    if not selected_thumb:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('index'))

    if request.method == 'POST':
        quantity_to_reserve = int(request.form.get('quantity_to_reserve', 0))
        
        if quantity_to_reserve <= 0:
            flash('Quantidade inválida.', 'error')
            return redirect(url_for('reserve_single', service=service_name))

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT quantity FROM stock WHERE service = ?", (service_name,))
        current_stock = c.fetchone()
        conn.close()

        if not current_stock or current_stock[0] < quantity_to_reserve:
            flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock[0] if current_stock else 0}.', 'error')
            return redirect(url_for('reserve_single', service=service_name))

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            # Cria a reserva
            c.execute("INSERT INTO reservations (user_id, service, quantity, status) VALUES (?, ?, ?, ?)",
                      (session['user_id'], service_name, quantity_to_reserve, 'pending'))
            
            # Decrementa o estoque
            c.execute("UPDATE stock SET quantity = quantity - ? WHERE service = ?", (quantity_to_reserve, service_name))
            conn.commit()
            logger.info(f"Reserva de {quantity_to_reserve}x {service_name} criada por user {session['user_id']}")
            flash(f'Reserva de {quantity_to_reserve}x {service_name} realizada! Aguarde aprovação.', 'success')
            return redirect(url_for('profile'))
        except Exception as e:
            conn.rollback()
            logger.error(f"Erro ao criar reserva individual: {e}")
            flash('Erro ao realizar reserva. Tente novamente.', 'error')
        finally:
            conn.close()

    return render_template_string(RESERVE_SINGLE_HTML, 
                                  service=selected_thumb['service'],
                                  thumbnail_url=selected_thumb['thumbnail_url'],
                                  quantity=selected_thumb['quantity'],
                                  price=selected_thumb['price'],
                                  logo_url=LOGO_URL,
                                  whatsapp_number=WHATSAPP_NUMBER)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))

    thumbnails = load_thumbnails_from_db()
    
    # --- Lógica de Filtros ---
    filtered_thumbnails = thumbnails
    
    # Filtro por disponíveis
    if request.args.get('available') == 'true':
        filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]

    # Filtro por marca
    marca_filter = request.args.get('marca', '').strip().lower()
    if marca_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]

    # Ordenação
    sort_by = request.args.get('sort_by', '')
    if sort_by == 'name_asc':
        filtered_thumbnails.sort(key=lambda x: x['service'].lower())
    elif sort_by == 'name_desc':
        filtered_thumbnails.sort(key=lambda x: x['service'].lower(), reverse=True)
    elif sort_by == 'price_asc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')))
    elif sort_by == 'price_desc':
        filtered_thumbnails.sort(key=lambda x: float(x['price'].replace(',', '.')), reverse=True)
    elif sort_by == 'quantity_asc':
        filtered_thumbnails.sort(key=lambda x: x['quantity'])
    elif sort_by == 'quantity_desc':
        filtered_thumbnails.sort(key=lambda x: x['quantity'], reverse=True)
    elif sort_by == 'previsao_chegada_asc':
        filtered_thumbnails.sort(key=lambda x: x['previsao_chegada'] if x['previsao_chegada'] else 'ZZZ') # Vazios no final
    elif sort_by == 'previsao_chegada_desc':
        filtered_thumbnails.sort(key=lambda x: x['previsao_chegada'] if x['previsao_chegada'] else 'AAA', reverse=True) # Vazios no final

    if request.method == 'POST':
        selected_services = request.form.getlist('selected_services')
        if not selected_services:
            flash('Nenhuma miniatura selecionada para reserva.', 'error')
            return redirect(url_for('reservar'))

        reservations_to_create = []
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            for service_name in selected_services:
                quantity_key = f'quantity_{service_name}'
                quantity_to_reserve = int(request.form.get(quantity_key, 0))

                if quantity_to_reserve <= 0:
                    flash(f'Quantidade inválida para {service_name}.', 'error')
                    conn.rollback()
                    return redirect(url_for('reservar'))

                c.execute("SELECT quantity FROM stock WHERE service = ?", (service_name,))
                current_stock = c.fetchone()

                if not current_stock or current_stock[0] < quantity_to_reserve:
                    flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock[0] if current_stock else 0}.', 'error')
                    conn.rollback()
                    return redirect(url_for('reservar'))
                
                reservations_to_create.append((session['user_id'], service_name, quantity_to_reserve, 'pending'))
                c.execute("UPDATE stock SET quantity = quantity - ? WHERE service = ?", (quantity_to_reserve, service_name))
            
            # Insere todas as reservas após verificar o estoque de todas
            for res in reservations_to_create:
                c.execute("INSERT INTO reservations (user_id, service, quantity, status) VALUES (?, ?, ?, ?)", res)
            
            conn.commit()
            logger.info(f"{len(reservations_to_create)} reservas criadas por user {session['user_id']}")
            flash(f'{len(reservations_to_create)} reserva(s) realizada(s)! Aguarde aprovação.', 'success')
            return redirect(url_for('profile'))
        except Exception as e:
            conn.rollback()
            logger.error(f"Erro ao criar múltiplas reservas: {e}")
            flash('Erro ao realizar reservas. Tente novamente.', 'error')
        finally:
            conn.close()

    return render_template_string(RESERVAR_HTML, 
                                  thumbnails=filtered_thumbnails, 
                                  logo_url=LOGO_URL,
                                  whatsapp_number=WHATSAPP_NUMBER)

@app.route('/profile', methods=['GET'])
def profile():
    if 'user_id' not in session:
        flash('Faça login para ver perfil.', 'error')
        return redirect(url_for('login'))
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT data_cadastro FROM users WHERE id = ?", (session['user_id'],))
    user_data = c.fetchone()
    data_cadastro = user_data[0] if user_data else 'Desconhecida'
    c.execute("""
        SELECT r.id, r.service, r.quantity, r.status, r.denied_reason 
        FROM reservations r 
        WHERE r.user_id = ? 
        ORDER BY r.created_at DESC
    """, (session['user_id'],))
    reservations = c.fetchall()
    conn.close()
    return render_template_string(PROFILE_HTML, data_cadastro=data_cadastro, reservations=reservations)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado. Apenas para administradores.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # --- Lógica de POST (Ações do Admin) ---
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'sync_stock':
            thumbnails_from_sheet = load_thumbnails_from_db() # Recarrega da planilha para pegar os dados mais recentes
            for thumb in thumbnails_from_sheet:
                c.execute("INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)", (thumb['service'], thumb['quantity']))
            conn.commit()
            flash('Estoque sincronizado da planilha!', 'success')
            logger.info("Estoque sincronizado via admin.")
        
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
        
        elif action == 'create_miniature':
            service_name = request.form.get('service_name')
            marca = request.form.get('marca')
            obs = request.form.get('obs')
            price = request.form.get('price')
            quantity = request.form.get('quantity')
            image_url = request.form.get('image_url')
            previsao_chegada = request.form.get('previsao_chegada')

            if not all([service_name, marca, price, quantity, image_url]):
                flash('Preencha todos os campos obrigatórios para a miniatura.', 'error')
            else:
                try:
                    price_float = float(price)
                    quantity_int = int(quantity)
                    # Insere ou atualiza na tabela stock
                    c.execute("INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)", (service_name, quantity_int))
                    conn.commit()
                    flash(f'Miniatura "{service_name}" adicionada/atualizada no estoque.', 'success')
                    logger.info(f"Admin {session['email']} adicionou/atualizou miniatura: {service_name}")
                except ValueError:
                    flash('Preço e Quantidade devem ser números válidos.', 'error')
                except Exception as e:
                    flash(f'Erro ao adicionar miniatura: {e}', 'error')
                    logger.error(f"Erro ao adicionar miniatura: {e}")

        elif action == 'create_reservation':
            user_id = request.form.get('user_id')
            service = request.form.get('service')
            quantity = request.form.get('quantity')
            status = request.form.get('status', 'pending')
            denied_reason = request.form.get('denied_reason', '')

            if not all([user_id, service, quantity]):
                flash('Preencha todos os campos obrigatórios para a reserva.', 'error')
            else:
                try:
                    quantity_int = int(quantity)
                    if quantity_int <= 0:
                        flash('Quantidade deve ser maior que zero.', 'error')
                        raise ValueError("Quantidade inválida")

                    # Verifica estoque antes de criar
                    c.execute("SELECT quantity FROM stock WHERE service = ?", (service,))
                    current_stock = c.fetchone()
                    if not current_stock or current_stock[0] < quantity_int:
                        flash(f'Estoque insuficiente para {service}. Disponível: {current_stock[0] if current_stock else 0}.', 'error')
                        raise ValueError("Estoque insuficiente")

                    c.execute("INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)",
                              (user_id, service, quantity_int, status, denied_reason))
                    
                    # Decrementa estoque se a reserva for aprovada ou pendente (assumindo que pendente já tira do estoque)
                    if status in ['pending', 'approved']:
                        c.execute("UPDATE stock SET quantity = quantity - ? WHERE service = ?", (quantity_int, service))
                    
                    conn.commit()
                    flash('Nova reserva criada!', 'success')
                    logger.info(f"Admin {session['email']} criou reserva para user {user_id}, service {service}, qty {quantity_int}")
                except ValueError as ve:
                    flash(f'Erro: {ve}', 'error')
                except Exception as e:
                    flash(f'Erro ao criar reserva: {e}', 'error')
                    logger.error(f"Erro ao criar reserva: {e}")

        # --- Lógica de GET (Ações de URL) ---
        if 'promote_user' in request.args:
            user_id = int(request.args['promote_user'])
            c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
            conn.commit()
            flash('Usuário promovido para admin.', 'success')
        elif 'demote_user' in request.args:
            user_id = int(request.args['demote_user'])
            c.execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id,))
            conn.commit()
            flash('Usuário rebaixado para user.', 'success')
        elif 'delete_user' in request.args:
            user_id = int(request.args['delete_user'])
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            c.execute("DELETE FROM reservations WHERE user_id = ?", (user_id,))
            conn.commit()
            flash('Usuário e suas reservas deletados.', 'success')
        elif 'delete_res' in request.args:
            res_id = int(request.args['delete_res'])
            c.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
            conn.commit()
            flash('Reserva deletada.', 'success')

        # Redireciona para GET para limpar os parâmetros POST/GET da URL
        return redirect(url_for('admin'))

    # --- Lógica de GET (Carregar Dados e Filtros) ---
    
    # Usuários com filtro
    user_search = request.args.get('user_search', '').strip()
    user_role = request.args.get('user_role', '').strip()
    user_query = "SELECT id, name, email, phone, role, data_cadastro FROM users WHERE 1=1"
    user_params = []
    if user_search:
        user_query += " AND (name LIKE ? OR email LIKE ? OR phone LIKE ?)"
        user_params.extend([f"%{user_search}%", f"%{user_search}%", f"%{user_search}%"])
    if user_role:
        user_query += " AND role = ?"
        user_params.append(user_role)
    c.execute(user_query + " ORDER BY data_cadastro DESC", user_params)
    users = c.fetchall()
    filtered_users = [{'id': u[0], 'name': u[1], 'email': u[2], 'phone': u[3], 'role': u[4], 'data_cadastro': u[5]} for u in users]

    # Reservas com filtro
    res_search = request.args.get('res_search', '').strip()
    res_status = request.args.get('res_status', '').strip()
    res_query = """
        SELECT r.id, r.service, r.quantity, r.status, r.denied_reason, u.email as user_email 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        WHERE 1=1
    """
    res_params = []
    if res_search:
        res_query += " AND (r.service LIKE ? OR u.email LIKE ?)"
        res_params.extend([f"%{res_search}%", f"%{res_search}%"])
    if res_status:
        res_query += " AND r.status = ?"
        res_params.append(res_status)
    
    c.execute(res_query + " ORDER BY r.created_at DESC", res_params)
    all_reservations_raw = c.fetchall()
    filtered_all_reservations = [{'id': r[0], 'service': r[1], 'quantity': r[2], 'status': r[3], 'denied_reason': r[4], 'user_email': r[5]} for r in all_reservations_raw]
    
    pending_reservations = [res for res in filtered_all_reservations if res['status'] == 'pending']
    
    # Dados para formulários de nova reserva/miniatura
    users_for_res = [{'id': u[0], 'name': u[1], 'email': u[2]} for u in c.execute("SELECT id, name, email FROM users ORDER BY name").fetchall()]
    thumbnails_for_res = load_thumbnails_from_db() # Usa a função que carrega do DB de estoque

    conn.close()
    
    return render_template_string(ADMIN_HTML, 
                                  users=filtered_users, 
                                  filtered_users=filtered_users, # Passa filtrados para o template
                                  pending_reservations=pending_reservations, 
                                  filtered_pending=pending_reservations, # Passa filtrados
                                  all_reservations=filtered_all_reservations,
                                  filtered_all_reservations=filtered_all_reservations, # Passa filtrados
                                  thumbnails_for_res=thumbnails_for_res,
                                  users_for_res=users_for_res,
                                  tomorrow=(date.today() + timedelta(days=1)).isoformat())

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

# --- Rotas de Backup e Export ---
@app.route('/backup')
def backup_db():
    if session.get('role') != 'admin':
        abort(403)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Fetch all data from users, reservations, and stock tables
    c.execute("SELECT * FROM users")
    users = c.fetchall()
    c.execute("SELECT * FROM reservations")
    reservations = c.fetchall()
    c.execute("SELECT * FROM stock")
    stock = c.fetchall()
    conn.close()
    
    # Convert to list of dictionaries for JSON export
    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(zip(['id', 'name', 'email', 'phone', 'password', 'role', 'data_cadastro'], user)) for user in users],
        'reservations': [dict(zip(['id', 'user_id', 'service', 'quantity', 'status', 'approved_by', 'denied_reason', 'created_at'], res)) for res in reservations],
        'stock': [dict(zip(['id', 'service', 'quantity', 'last_sync'], s)) for s in stock]
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

# --- Error Handlers ---
@app.errorhandler(403)
def forbidden_error(error):
    return render_template_string('<h1>403 - Acesso Negado</h1><p>Você não tem permissão para acessar esta página.</p><p><a href="/">Voltar ao Home</a></p>'), 403

@app.errorhandler(404)
def not_found_error(error):
    return render_template_string('<h1>404 - Página Não Encontrada</h1><p>A página que você procura não existe.</p><p><a href="/">Voltar ao Home</a></p>'), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Erro interno 500: {error}")
    return render_template_string('<h1>500 - Erro Interno do Servidor</h1><p>Algo deu errado. Por favor, tente novamente mais tarde.</p><p><a href="/">Voltar ao Home</a></p>'), 500

# --- Execução da Aplicação ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = '0.0.0.0'
    app.run(host=host, port=port, debug=False)
    logger.info(f"App rodando em {host}:{port}")
