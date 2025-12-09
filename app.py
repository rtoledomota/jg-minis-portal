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

# Configuração de Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variáveis de Configuração (do ambiente ou fallback)
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025_dev_key_fallback')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# Autenticação gspread para Google Sheets
gc = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        # Scopes para Sheets e Drive (necessário para gspread.open)
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

# Função para validar formato de email
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

# Inicialização do Banco de Dados SQLite
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Tabela de Usuários
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  role TEXT DEFAULT 'user',
                  data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Tabela de Reservas
    c.execute('''CREATE TABLE IF NOT EXISTS reservations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  service TEXT NOT NULL,
                  date TEXT NOT NULL,
                  status TEXT DEFAULT 'pending',
                  approved_by INTEGER,
                  denied_reason TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id),
                  FOREIGN KEY (approved_by) REFERENCES users (id))''')
    # Tabela de Estoque (para controle interno, sincronizado com Sheets)
    c.execute('''CREATE TABLE IF NOT EXISTS stock
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  service TEXT UNIQUE NOT NULL,
                  quantity INTEGER DEFAULT 0,
                  last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Cria usuário admin padrão se não existir
    c.execute("SELECT id FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (email, password, role) VALUES ('admin@jgminis.com.br', ?, 'admin')", (hashed_password,))
        logger.info("Usuário admin criado no DB")
    conn.commit()
    conn.close()

init_db()

# Função para carregar miniaturas da planilha Google Sheets
def load_thumbnails():
    thumbnails = []
    if gc:
        try:
            sheet = gc.open("BASE DE DADOS JG").sheet1
            records = sheet.get_all_records()
            if not records:
                raise Exception("Planilha vazia - adicione dados nas linhas 2+")

            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()

            # Carrega até 12 itens (linhas 2-13) para exibição inicial. Mude para records[1:] para todas.
            for record in records[1:13]:
                service = record.get('NOME DA MINIATURA', 'Miniatura Desconhecida')
                marca = record.get('MARCA/FABRICANTE', '')
                obs = record.get('OBSERVAÇÕES', '')
                previsao_chegada = record.get('PREVISÃO DE CHEGADA', '')
                
                # Pega a quantidade do DB de estoque, se existir, senão da planilha
                c.execute("SELECT quantity FROM stock WHERE service = ?", (service,))
                db_quantity = c.fetchone()
                quantity = db_quantity[0] if db_quantity else record.get('QUANTIDADE DISPONIVEL', 0)

                description = f"{marca} - {obs}".strip(' - ')
                thumbnail_url = record.get('IMAGEM', LOGO_URL)
                price_raw = record.get('VALOR', '')
                
                # Fix: Converte para str antes de replace (handle int/float/None)
                price_str = str(price_raw) if price_raw is not None else ''
                price = price_str.replace('R$ ', '').replace(',', '.') if price_str else '0'
                
                thumbnails.append({
                    'service': service,
                    'description': description or 'Descrição disponível',
                    'thumbnail_url': thumbnail_url,
                    'price': price,
                    'quantity': int(quantity) if quantity else 0,
                    'marca': marca,
                    'previsao_chegada': previsao_chegada
                })
            conn.close()
            logger.info(f"Carregados {len(thumbnails)} thumbnails da planilha")
        except Exception as e:
            logger.error(f"Erro ao carregar planilha: {e}")
            thumbnails = [{'service': 'Fallback', 'description': 'Serviço em manutenção. Contate-nos!', 'thumbnail_url': LOGO_URL, 'price': '0', 'quantity': 0, 'marca': '', 'previsao_chegada': ''}]
    else:
        thumbnails = [{'service': 'Sem Sheets', 'description': 'Configure GOOGLE_SHEETS_CREDENTIALS', 'thumbnail_url': LOGO_URL, 'price': 'Consultar', 'quantity': 0, 'marca': '', 'previsao_chegada': ''}]
    return thumbnails

# --- Templates HTML Inline ---

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
        .whatsapp-link { display: inline-block; padding: 8px 12px; background-color: #25D366; color: white; border-radius: 5px; text-decoration: none; margin-top: 10px; }
        .whatsapp-link:hover { background-color: #1DA851; }
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
            {% if thumb.quantity > 0 %}
                <a href="{{ url_for('reservar') }}" style="color: #28a745; font-weight: bold;">Reservar Agora</a>
            {% else %}
                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá! Estou na fila de espera para a miniatura: {{ thumb.service }}" target="_blank" class="whatsapp-link">Fila de Espera (WhatsApp)</a>
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
            <input type="email" name="email" placeholder="Email" required>
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
        .filters { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-bottom: 20px; }
        .filters label { margin-right: 5px; }
        .filters select, .filters input[type="text"] { padding: 8px; border-radius: 5px; border: 1px solid #ddd; }
        .filters button { padding: 8px 15px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .filters button:hover { background: #0056b3; }
        .miniature-list { margin-top: 20px; }
        .miniature-item { display: flex; align-items: center; background: #e9ecef; padding: 10px; margin-bottom: 10px; border-radius: 5px; }
        .miniature-item input[type="checkbox"] { margin-right: 15px; transform: scale(1.5); }
        .miniature-item img { width: 80px; height: 80px; object-fit: cover; border-radius: 5px; margin-right: 15px; }
        .miniature-details { flex-grow: 1; }
        .miniature-details h3 { margin: 0 0 5px; color: #007bff; }
        .miniature-details p { margin: 0; font-size: 0.9em; }
        .miniature-actions { display: flex; flex-direction: column; align-items: flex-end; }
        .miniature-actions input[type="date"] { margin-top: 5px; padding: 8px; border-radius: 5px; border: 1px solid #ddd; }
        .whatsapp-link { display: inline-block; padding: 5px 10px; background-color: #25D366; color: white; border-radius: 5px; text-decoration: none; margin-top: 5px; font-size: 0.9em; }
        .whatsapp-link:hover { background-color: #1DA851; }
        .submit-button { width: 100%; padding: 12px; background: #ffc107; color: black; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; font-weight: bold; margin-top: 20px; }
        .submit-button:hover { background: #e0a800; }
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
            <form method="GET" id="filterForm">
                <label for="disponiveis">Disponíveis:</label>
                <input type="checkbox" id="disponiveis" name="disponiveis" value="true" {% if request.args.get('disponiveis') == 'true' %}checked{% endif %}>
                
                <label for="sort_by">Ordenar por:</label>
                <select name="sort_by" id="sort_by">
                    <option value="data_insercao" {% if request.args.get('sort_by') == 'data_insercao' %}selected{% endif %}>Data Inserção</option>
                    <option value="previsao_chegada" {% if request.args.get('sort_by') == 'previsao_chegada' %}selected{% endif %}>Previsão Chegada</option>
                </select>
                
                <label for="marca">Marca:</label>
                <input type="text" id="marca" name="marca" placeholder="Filtrar por marca" value="{{ request.args.get('marca', '') }}">
                
                <button type="submit">Aplicar Filtros</button>
            </form>
        </div>

        <form method="POST">
            <div class="miniature-list">
                {% for thumb in thumbnails %}
                <div class="miniature-item">
                    {% if thumb.quantity > 0 %}
                        <input type="checkbox" name="selected_services" value="{{ thumb.service }}" id="service_{{ loop.index }}">
                    {% else %}
                        <input type="checkbox" disabled title="Esgotado">
                    {% endif %}
                    <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
                    <div class="miniature-details">
                        <h3>{{ thumb.service }}</h3>
                        <p>{{ thumb.description or 'Descrição disponível' }}</p>
                        <p>Preço: R$ {{ thumb.price or 'Consultar' }} | Disponível: {{ thumb.quantity }}</p>
                        {% if thumb.previsao_chegada %}
                            <p>Previsão de Chegada: {{ thumb.previsao_chegada }}</p>
                        {% endif %}
                    </div>
                    <div class="miniature-actions">
                        {% if thumb.quantity > 0 %}
                            <label for="date_{{ loop.index }}">Data:</label>
                            <input type="date" name="date_{{ thumb.service }}" id="date_{{ loop.index }}" required min="{{ tomorrow }}">
                        {% else %}
                            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá! Estou na fila de espera para a miniatura: {{ thumb.service }}" target="_blank" class="whatsapp-link">Fila de Espera (WhatsApp)</a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            <button type="submit" class="submit-button">Confirmar Reservas Selecionadas</button>
        </form>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">
                    {{ message }}
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('profile') }}">Minhas Reservas</a></p>
    </div>
    <script>
        const today = new Date().toISOString().split('T')[0];
        document.querySelectorAll('input[type="date"]').forEach(input => {
            input.setAttribute('min', today);
        });
    </script>
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
        <p><strong>Email:</strong> {{ session.email }}</p>
        <p><strong>Data de Cadastro:</strong> {{ data_cadastro }}</p>
        <h3>Minhas Reservas:</h3>
        <ul>
            {% for res in reservations %}
            <li class="{{ 'approved' if res.status == 'approved' else 'denied' if res.status == 'denied' else 'pending' }}">
                <strong>Miniatura:</strong> {{ res.service }} | <strong>Data:</strong> {{ res.date }} | <strong>Status:</strong> {{ res.status.title() }}
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
        .actions-bar { text-align: center; margin: 20px 0; }
        button { padding: 8px 15px; margin: 0 5px; border: none; border-radius: 5px; cursor: pointer; }
        .approve { background: #28a745; color: white; }
        .deny { background: #dc3545; color: white; }
        .delete { background: #ffc107; color: black; }
        .backup { background: #17a2b8; color: white; }
        .sync { background: #6c757d; color: white; }
        input[type="text"], select, input[type="date"] { padding: 8px; border: 1px solid #ddd; border-radius: 5px; margin: 5px; }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; background: #e9ecef; margin: 10px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        li.pending { background: #fff3cd; }
        li.approved { background: #d4edda; }
        li.denied { background: #f8d7da; }
        .filters { margin: 20px 0; text-align: center; display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; }
        .new-reservation-form { background: #f8f9fa; padding: 20px; border-radius: 10px; margin: 20px 0; }
        .new-reservation-form form { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; }
        .flash-error { background: #f8d7da; color: #721c24; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .table-section { margin-top: 30px; }
        .table-section h3 { margin-bottom: 15px; }
        .table-section ul { border: 1px solid #ddd; border-radius: 5px; }
        .table-section li:last-child { margin-bottom: 0; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Painel Admin</h2>
        <div class="stats">
            <div class="stat-box">
                <h3>Usuários</h3>
                <p>{{ total_users }}</p>
            </div>
            <div class="stat-box">
                <h3>Reservas Pendentes</h3>
                <p>{{ total_pending_reservations }}</p>
            </div>
            <div class="stat-box">
                <h3>Total Reservas</h3>
                <p>{{ total_reservations }}</p>
            </div>
        </div>
        <div class="actions-bar">
            <button onclick="window.location.href='/backup'" class="backup">Backup DB (JSON)</button>
            <button onclick="window.location.href='/export_csv'" class="backup">Export Reservas (CSV)</button>
            <form method="POST" style="display: inline;">
                <button type="submit" name="action" value="sync_stock" class="sync">Sync Estoque da Planilha</button>
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

        <div class="table-section">
            <h3>Usuários</h3>
            <div class="filters">
                <form method="GET" action="{{ url_for('admin') }}">
                    <input type="text" name="user_search" placeholder="Email" value="{{ request.args.get('user_search', '') }}">
                    <select name="user_role">
                        <option value="">Todos Roles</option>
                        <option value="user" {% if request.args.get('user_role') == 'user' %}selected{% endif %}>User</option>
                        <option value="admin" {% if request.args.get('user_role') == 'admin' %}selected{% endif %}>Admin</option>
                    </select>
                    <button type="submit">Filtrar</button>
                </form>
            </div>
            <ul>
                {% for user in filtered_users %}
                <li>
                    <span>{{ user.email }} - Role: {{ user.role }} - Cadastrado: {{ user.data_cadastro }}</span>
                    <span class="actions">
                        {% if user.role != 'admin' %}
                        <a href="{{ url_for('admin', promote_user=user.id) }}" class="approve">Promover</a>
                        <a href="{{ url_for('admin', demote_user=user.id) }}" class="deny" onclick="return confirm('Rebaixar usuário {{ user.email }}?')">Rebaixar</a>
                        <a href="{{ url_for('admin', delete_user=user.id) }}" class="delete" onclick="return confirm('Deletar usuário {{ user.email }} e todas as suas reservas?')">Deletar</a>
                        {% endif %}
                    </span>
                </li>
                {% endfor %}
                {% if not filtered_users %}<li>Nenhum usuário encontrado.</li>{% endif %}
            </ul>
        </div>

        <div class="table-section">
            <h3>Reservas Pendentes</h3>
            <div class="filters">
                <form method="GET" action="{{ url_for('admin') }}">
                    <input type="text" name="res_pending_search" placeholder="Serviço/Email" value="{{ request.args.get('res_pending_search', '') }}">
                    <button type="submit">Filtrar</button>
                </form>
            </div>
            <ul>
                {% for res in filtered_pending_reservations %}
                <li class="pending">
                    <span>ID {{ res.id }}: {{ res.service }} por {{ res.user_email }} em {{ res.date }}</span>
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
                        <a href="{{ url_for('admin', delete_res=res.id) }}" class="delete" onclick="return confirm('Deletar reserva {{ res.id }}?')">Deletar</a>
                    </span>
                </li>
                {% endfor %}
                {% if not filtered_pending_reservations %}<li>Nenhuma reserva pendente encontrada.</li>{% endif %}
            </ul>
        </div>

        <div class="new-reservation-form">
            <h3>Inserir Nova Reserva</h3>
            <form method="POST">
                <select name="user_id" required>
                    <option value="">Selecione Usuário</option>
                    {% for user in all_users %}
                    <option value="{{ user.id }}">{{ user.email }}</option>
                    {% endfor %}
                </select>
                <select name="service" required>
                    <option value="">Selecione Miniatura</option>
                    {% for thumb in all_thumbnails %}
                    <option value="{{ thumb.service }}" data-quantity="{{ thumb.quantity }}">{{ thumb.service }} (Estoque: {{ thumb.quantity }})</option>
                    {% endfor %}
                </select>
                <input type="date" name="date" required min="{{ tomorrow }}">
                <select name="status">
                    <option value="pending">Pendente</option>
                    <option value="approved">Aprovada</option>
                    <option value="denied">Rejeitada</option>
                </select>
                <input type="text" name="reason" placeholder="Motivo (se rejeitada)">
                <button type="submit" name="action" value="create_res">Criar Reserva</button>
            </form>
        </div>

        <div class="table-section">
            <h3>Todas as Reservas</h3>
            <div class="filters">
                <form method="GET" action="{{ url_for('admin') }}">
                    <input type="text" name="res_all_search" placeholder="Serviço/Email" value="{{ request.args.get('res_all_search', '') }}">
                    <select name="res_all_status">
                        <option value="">Todos Status</option>
                        <option value="pending" {% if request.args.get('res_all_status') == 'pending' %}selected{% endif %}>Pendente</option>
                        <option value="approved" {% if request.args.get('res_all_status') == 'approved' %}selected{% endif %}>Aprovada</option>
                        <option value="denied" {% if request.args.get('res_all_status') == 'denied' %}selected{% endif %}>Rejeitada</option>
                    </select>
                    <button type="submit">Filtrar</button>
                </form>
            </div>
            <ul>
                {% for res in filtered_all_reservations %}
                <li class="{{ res.status }}">
                    <span>ID {{ res.id }}: {{ res.service }} por {{ res.user_email }} em {{ res.date }} (Status: {{ res.status.title() }})</span>
                    {% if res.denied_reason %}<span> - Motivo: {{ res.denied_reason }}</span>{% endif %}
                    <span class="actions">
                        <a href="{{ url_for('admin', delete_res=res.id) }}" class="delete" onclick="return confirm('Deletar reserva {{ res.id }}?')">Deletar</a>
                    </span>
                </li>
                {% endfor %}
                {% if not filtered_all_reservations %}<li>Nenhuma reserva encontrada.</li>{% endif %}
            </ul>
        </div>
        <p style="text-align: center; margin-top: 30px;"><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout Admin</a></p>
    </div>
</body>
</html>
'''

# --- Rotas Flask ---

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
        c.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        if user and bcrypt.check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['email'] = user[1]
            session['role'] = user[3]
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
        email = request.form['email'].strip().lower()
        password = request.form['password']
        if not is_valid_email(email):
            flash('Email inválido.', 'error')
            return render_template_string(REGISTER_HTML)
        if len(password) < 6:
            flash('Senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template_string(REGISTER_HTML)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed_password))
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

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))

    all_thumbnails = load_thumbnails()
    filtered_thumbnails = all_thumbnails

    # Lógica de Filtros (GET)
    disponiveis = request.args.get('disponiveis') == 'true'
    sort_by = request.args.get('sort_by', 'data_insercao')
    marca_filter = request.args.get('marca', '').lower()

    if disponiveis:
        filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
    
    if marca_filter:
        filtered_thumbnails = [t for t in filtered_thumbnails if marca_filter in t['marca'].lower()]

    if sort_by == 'previsao_chegada':
        # Ordena por previsão de chegada (vazios por último)
        filtered_thumbnails.sort(key=lambda x: (x['previsao_chegada'] == '', x['previsao_chegada']))
    else: # default 'data_insercao' (não temos data de inserção na thumb, então ordena por nome)
        filtered_thumbnails.sort(key=lambda x: x['service'])

    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    if request.method == 'POST':
        selected_services = request.form.getlist('selected_services')
        if not selected_services:
            flash('Selecione pelo menos uma miniatura para reservar.', 'error')
            return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, tomorrow=tomorrow, whatsapp_number=WHATSAPP_NUMBER)

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        reservations_made = 0
        
        for service_name in selected_services:
            selected_date = request.form.get(f'date_{service_name}')
            if not selected_date:
                flash(f'Data não selecionada para {service_name}.', 'error')
                conn.close()
                return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, tomorrow=tomorrow, whatsapp_number=WHATSAPP_NUMBER)
            
            if selected_date <= date.today().isoformat():
                flash(f'Data para {service_name} deve ser futura.', 'error')
                conn.close()
                return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, tomorrow=tomorrow, whatsapp_number=WHATSAPP_NUMBER)
            
            # Verifica estoque antes de reservar
            c.execute("SELECT quantity FROM stock WHERE service = ?", (service_name,))
            current_quantity = c.fetchone()
            if not current_quantity or current_quantity[0] <= 0:
                flash(f'Miniatura "{service_name}" esgotada. Não foi possível reservar.', 'error')
                continue # Pula para a próxima miniatura selecionada
            
            try:
                c.execute("INSERT INTO reservations (user_id, service, date) VALUES (?, ?, ?)",
                          (session['user_id'], service_name, selected_date))
                c.execute("UPDATE stock SET quantity = quantity - 1 WHERE service = ?", (service_name,))
                reservations_made += 1
                logger.info(f"Reserva criada para {service_name} por user {session['user_id']}")
            except Exception as e:
                logger.error(f"Erro ao criar reserva para {service_name}: {e}")
                flash(f'Erro ao reservar "{service_name}". Tente novamente.', 'error')
        
        conn.commit()
        conn.close()

        if reservations_made > 0:
            flash(f'{reservations_made} reserva(s) realizada(s)! Aguarde aprovação.', 'success')
            return redirect(url_for('profile'))
        else:
            flash('Nenhuma reserva foi realizada.', 'error')
            return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, tomorrow=tomorrow, whatsapp_number=WHATSAPP_NUMBER)

    return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, tomorrow=tomorrow, whatsapp_number=WHATSAPP_NUMBER)

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
        SELECT r.id, r.service, r.date, r.status, r.denied_reason 
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

    # --- Processar Ações POST ---
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'sync_stock':
            all_thumbnails_from_sheet = load_thumbnails() # Carrega do Sheets
            for thumb in all_thumbnails_from_sheet:
                c.execute("INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)", (thumb['service'], thumb['quantity']))
            conn.commit()
            flash('Estoque sincronizado da planilha!', 'success')
            logger.info("Estoque sincronizado via Admin.")
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
        elif action == 'create_res':
            user_id = request.form.get('user_id')
            service = request.form.get('service')
            res_date = request.form.get('date')
            status = request.form.get('status', 'pending')
            reason = request.form.get('reason', '')
            
            if user_id and service and res_date:
                if res_date <= date.today().isoformat():
                    flash('Data da reserva deve ser futura.', 'error')
                else:
                    c.execute("INSERT INTO reservations (user_id, service, date, status, denied_reason) VALUES (?, ?, ?, ?, ?)", (user_id, service, res_date, status, reason))
                    conn.commit()
                    # Decrementa estoque se a reserva for aprovada
                    if status == 'approved':
                        c.execute("UPDATE stock SET quantity = quantity - 1 WHERE service = ?", (service,))
                        conn.commit()
                    flash('Nova reserva criada!', 'success')
                    logger.info(f"Admin {session['email']} criou reserva para {service} (User ID: {user_id}) com status {status}.")
            else:
                flash('Preencha todos os campos obrigatórios para criar a reserva.', 'error')
        
        # Redireciona para GET para limpar o formulário POST
        return redirect(url_for('admin'))

    # --- Processar Ações GET (Promote/Demote/Delete) ---
    if 'promote_user' in request.args:
        user_id = request.args.get('promote_user')
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
        conn.commit()
        flash('Usuário promovido a admin.', 'success')
        logger.info(f"Admin {session['email']} promoveu user {user_id}.")
        return redirect(url_for('admin'))
    if 'demote_user' in request.args:
        user_id = request.args.get('demote_user')
        if int(user_id) == session['user_id']:
            flash('Você não pode rebaixar a si mesmo.', 'error')
        else:
            c.execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id,))
            conn.commit()
            flash('Usuário rebaixado para user.', 'success')
            logger.info(f"Admin {session['email']} rebaixou user {user_id}.")
        return redirect(url_for('admin'))
    if 'delete_user' in request.args:
        user_id = request.args.get('delete_user')
        if int(user_id) == session['user_id']:
            flash('Você não pode deletar a si mesmo.', 'error')
        else:
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            c.execute("DELETE FROM reservations WHERE user_id = ?", (user_id,)) # Deleta reservas do usuário
            conn.commit()
            flash('Usuário e suas reservas deletados.', 'success')
            logger.info(f"Admin {session['email']} deletou user {user_id}.")
        return redirect(url_for('admin'))
    if 'delete_res' in request.args:
        res_id = request.args.get('delete_res')
        c.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
        conn.commit()
        flash('Reserva deletada.', 'success')
        logger.info(f"Admin {session['email']} deletou reserva {res_id}.")
        return redirect(url_for('admin'))

    # --- Carregar Dados e Aplicar Filtros (GET) ---
    
    # Stats
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM reservations WHERE status = 'pending'")
    total_pending_reservations = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM reservations")
    total_reservations = c.fetchone()[0]

    # Usuários
    user_search = request.args.get('user_search', '')
    user_role = request.args.get('user_role', '')
    user_query = "SELECT id, email, role, data_cadastro FROM users WHERE 1=1"
    user_params = []
    if user_search:
        user_query += " AND email LIKE ?"
        user_params.append(f"%{user_search}%")
    if user_role:
        user_query += " AND role = ?"
        user_params.append(user_role)
    c.execute(user_query, user_params)
    filtered_users = c.fetchall()
    all_users = c.execute("SELECT id, email FROM users ORDER BY email").fetchall() # Para o form de nova reserva

    # Reservas Pendentes
    res_pending_search = request.args.get('res_pending_search', '')
    pending_query = """
        SELECT r.id, r.service, r.date, r.user_id, u.email as user_email 
        FROM reservations r JOIN users u ON r.user_id = u.id 
        WHERE r.status = 'pending'
    """
    pending_params = []
    if res_pending_search:
        pending_query += " AND (r.service LIKE ? OR u.email LIKE ?)"
        pending_params.extend([f"%{res_pending_search}%", f"%{res_pending_search}%"])
    pending_query += " ORDER BY r.created_at DESC"
    c.execute(pending_query, pending_params)
    filtered_pending_reservations = c.fetchall()

    # Todas as Reservas
    res_all_search = request.args.get('res_all_search', '')
    res_all_status = request.args.get('res_all_status', '')
    all_res_query = """
        SELECT r.id, r.service, r.date, r.status, r.denied_reason, u.email as user_email 
        FROM reservations r JOIN users u ON r.user_id = u.id 
        WHERE 1=1
    """
    all_res_params = []
    if res_all_search:
        all_res_query += " AND (r.service LIKE ? OR u.email LIKE ?)"
        all_res_params.extend([f"%{res_all_search}%", f"%{res_all_search}%"])
    if res_all_status:
        all_res_query += " AND r.status = ?"
        all_res_params.append(res_all_status)
    all_res_query += " ORDER BY r.created_at DESC"
    c.execute(all_res_query, all_res_params)
    filtered_all_reservations = c.fetchall()

    # Miniaturas para o form de nova reserva
    all_thumbnails = load_thumbnails()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    conn.close()
    return render_template_string(ADMIN_HTML, 
                                  total_users=total_users,
                                  total_pending_reservations=total_pending_reservations,
                                  total_reservations=total_reservations,
                                  filtered_users=filtered_users, 
                                  all_users=all_users,
                                  filtered_pending_reservations=filtered_pending_reservations, 
                                  filtered_all_reservations=filtered_all_reservations,
                                  all_thumbnails=all_thumbnails, 
                                  tomorrow=tomorrow)

@app.route('/logout', methods=['GET'])
def logout():
    if 'user_id' in session:
        logger.info(f"Logout de {session['email']}")
    session.clear()
    flash('Logout realizado com sucesso!', 'success')
    return redirect(url_for('index'))

@app.route('/backup')
def backup_db():
    if session.get('role') != 'admin':
        abort(403)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Exporta tabela de usuários
    c.execute("SELECT id, email, password, role, data_cadastro FROM users")
    users = c.fetchall()
    user_cols = [description[0] for description in c.description]
    
    # Exporta tabela de reservas
    c.execute("SELECT id, user_id, service, date, status, approved_by, denied_reason, created_at FROM reservations")
    reservations = c.fetchall()
    res_cols = [description[0] for description in c.description]

    # Exporta tabela de estoque
    c.execute("SELECT id, service, quantity, last_sync FROM stock")
    stock = c.fetchall()
    stock_cols = [description[0] for description in c.description]
    
    conn.close()
    
    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(zip(user_cols, user)) for user in users],
        'reservations': [dict(zip(res_cols, res)) for res in reservations],
        'stock': [dict(zip(stock_cols, s)) for s in stock]
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
    c.execute("SELECT r.id, u.email, r.service, r.date, r.status, r.denied_reason, r.created_at FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC")
    rows = c.fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'User Email', 'Service', 'Date', 'Status', 'Denied Reason', 'Created At'])
    writer.writerows(rows)
    
    filename = f"jgminis_reservas_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv"
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'})

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.errorhandler(404)
def not_found_error(error):
    return render_template_string('<h1>404 - Página Não Encontrada</h1><p><a href="/">Voltar ao Home</a></p>'), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Erro interno 500: {error}")
    return render_template_string('<h1>500 - Erro Interno</h1><p>Algo deu errado. Tente novamente.</p><a href="/">Home</a>'), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = '0.0.0.0'
    app.run(host=host, port=port, debug=False)
    logger.info(f"App rodando em {host}:{port}")
