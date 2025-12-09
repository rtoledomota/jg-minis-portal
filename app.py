import os
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, jsonify, abort
from flask_bcrypt import Bcrypt
import gspread
from google.oauth2.service_account import Credentials
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025_dev_key_fallback')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290') # Default WhatsApp number

app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# Gspread Initialization
gc = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        # Added 'drive' scope for gspread.open() to work reliably
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

# Helper Functions
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row # Allows accessing columns by name
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  role TEXT DEFAULT 'user',
                  data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
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
    # New table for local stock management, synced from Google Sheets
    c.execute('''CREATE TABLE IF NOT EXISTS miniatura_stock
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  service TEXT UNIQUE NOT NULL,
                  description TEXT,
                  thumbnail_url TEXT,
                  price REAL,
                  quantity INTEGER DEFAULT 0,
                  max_reservas_por_usuario INTEGER DEFAULT 1,
                  previsao_chegada TEXT,
                  data_insercao TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # Create admin user if not exists
    c.execute("SELECT id FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (email, password, role) VALUES ('admin@jgminis.com.br', ?, 'admin')", (hashed_password,))
        logger.info("Usuário admin criado no DB")
    conn.commit()
    conn.close()

init_db()

def sync_stock_from_sheets():
    if not gc:
        logger.error("gspread não autorizado. Não é possível sincronizar estoque.")
        return False

    conn = get_db_connection()
    c = conn.cursor()
    try:
        sheet = gc.open("BASE DE DADOS JG").sheet1
        records = sheet.get_all_records()
        if not records:
            logger.warning("Planilha 'BASE DE DADOS JG' vazia. Nenhum estoque para sincronizar.")
            return False

        # Clear existing stock to avoid duplicates and reflect current sheet state
        c.execute("DELETE FROM miniatura_stock")
        
        for record in records:
            service = record.get('NOME DA MINIATURA', '').strip()
            if not service: # Skip rows without a service name
                continue

            description = f"{record.get('MARCA/FABRICANTE', '')} - {record.get('OBSERVAÇÕES', '')}".strip(' - ')
            thumbnail_url = record.get('IMAGEM', LOGO_URL)
            
            price_raw = record.get('VALOR', '')
            price_str = str(price_raw).replace('R$ ', '').replace(',', '.') if price_raw is not None else '0'
            try:
                price = float(price_str)
            except ValueError:
                price = 0.0 # Default to 0 if price is invalid

            quantity = int(record.get('QUANTIDADE DISPONIVEL', 0))
            max_reservas_por_usuario = int(record.get('MAX_RESERVAS_POR_USUARIO', 1))
            previsao_chegada = record.get('PREVISÃO DE CHEGADA', '')
            
            # Insert or update miniatura_stock
            c.execute("""
                INSERT OR REPLACE INTO miniatura_stock 
                (service, description, thumbnail_url, price, quantity, max_reservas_por_usuario, previsao_chegada)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (service, description, thumbnail_url, price, quantity, max_reservas_por_usuario, previsao_chegada))
        
        conn.commit()
        logger.info(f"Estoque sincronizado com sucesso. {len(records)} itens processados.")
        return True
    except gspread.exceptions.APIError as e:
        logger.error(f"Erro API Google ao sincronizar estoque: {e}")
        flash(f"Erro API Google ao sincronizar estoque: {e}", 'error')
        return False
    except Exception as e:
        logger.error(f"Erro inesperado ao sincronizar estoque: {e}")
        flash(f"Erro inesperado ao sincronizar estoque: {e}", 'error')
        return False
    finally:
        conn.close()

def load_thumbnails_from_db(filters=None):
    conn = get_db_connection()
    c = conn.cursor()
    query = "SELECT * FROM miniatura_stock WHERE 1=1"
    params = []

    if filters:
        if filters.get('disponiveis') == 'true':
            query += " AND quantity > 0"
        if filters.get('marca'):
            query += " AND description LIKE ?"
            params.append(f"%{filters['marca']}%")
        if filters.get('previsao_chegada'):
            query += " AND previsao_chegada LIKE ?"
            params.append(f"%{filters['previsao_chegada']}%")
        if filters.get('search_term'):
            query += " AND (service LIKE ? OR description LIKE ?)"
            params.append(f"%{filters['search_term']}%")
            params.append(f"%{filters['search_term']}%")

        if filters.get('order_by') == 'data_insercao_asc':
            query += " ORDER BY data_insercao ASC"
        elif filters.get('order_by') == 'data_insercao_desc':
            query += " ORDER BY data_insercao DESC"
        elif filters.get('order_by') == 'previsao_chegada_asc':
            query += " ORDER BY previsao_chegada ASC"
        elif filters.get('order_by') == 'previsao_chegada_desc':
            query += " ORDER BY previsao_chegada DESC"
        elif filters.get('order_by') == 'price_asc':
            query += " ORDER BY price ASC"
        elif filters.get('order_by') == 'price_desc':
            query += " ORDER BY price DESC"
        else:
            query += " ORDER BY service ASC" # Default order

    c.execute(query, params)
    thumbnails_raw = c.fetchall()
    conn.close()

    thumbnails = []
    for thumb_raw in thumbnails_raw:
        thumbnails.append({
            'id': thumb_raw['id'],
            'service': thumb_raw['service'],
            'description': thumb_raw['description'],
            'thumbnail_url': thumb_raw['thumbnail_url'],
            'price': f"{thumb_raw['price']:.2f}".replace('.', ','), # Format to R$ X,XX
            'quantity': thumb_raw['quantity'],
            'max_reservas_por_usuario': thumb_raw['max_reservas_por_usuario'],
            'previsao_chegada': thumb_raw['previsao_chegada'],
            'data_insercao': thumb_raw['data_insercao']
        })
    return thumbnails

# HTML Templates (Inline)
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
        .whatsapp-button { background-color: #25D366; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-weight: bold; display: inline-block; margin-top: 10px; }
        .whatsapp-button:hover { background-color: #1DA851; }
        .reserve-button { background-color: #28a745; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-weight: bold; display: inline-block; margin-top: 10px; }
        .reserve-button:hover { background-color: #218838; }
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
                <a href="{{ url_for('reservar') }}" class="reserve-button">Reservar Agora</a>
            {% else %}
                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá!%20Gostaria%20de%20entrar%20na%20fila%20de%20espera%20para%20{{ thumb.service | urlencode }}." target="_blank" class="whatsapp-button">Fila de Espera (WhatsApp)</a>
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
        body { font-family: Arial, sans-serif; background: #f8f9fa; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        .filters { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 20px; padding: 15px; background: #e9ecef; border-radius: 8px; }
        .filters label { font-weight: bold; margin-right: 5px; }
        .filters select, .filters input[type="text"] { padding: 8px; border: 1px solid #ddd; border-radius: 5px; }
        .filters button { padding: 8px 15px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .filters button:hover { background: #0056b3; }
        .miniature-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }
        .miniature-item { background: #f1f1f1; padding: 15px; border-radius: 8px; border: 1px solid #ddd; display: flex; flex-direction: column; }
        .miniature-item img { max-width: 100%; height: 120px; object-fit: cover; border-radius: 5px; margin-bottom: 10px; }
        .miniature-item h3 { margin: 0 0 5px; color: #007bff; font-size: 1.1em; }
        .miniature-item p { margin: 0 0 5px; font-size: 0.9em; }
        .miniature-item .price { font-weight: bold; color: #28a745; }
        .miniature-item .quantity { font-size: 0.85em; color: #666; }
        .miniature-item .reserve-options { margin-top: 10px; }
        .miniature-item input[type="checkbox"] { margin-right: 8px; transform: scale(1.2); }
        .miniature-item input[type="date"] { width: calc(100% - 10px); padding: 8px; border: 1px solid #ddd; border-radius: 5px; margin-top: 5px; }
        .submit-button { padding: 12px 20px; background: #ffc107; color: black; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; font-weight: bold; margin-top: 30px; width: 100%; }
        .submit-button:hover { background: #e0a800; }
        .whatsapp-button { background-color: #25D366; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-weight: bold; display: inline-block; margin-top: 10px; width: 100%; box-sizing: border-box; text-align: center; }
        .whatsapp-button:hover { background-color: #1DA851; }
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
        <form method="GET" class="filters">
            <label for="disponiveis">Disponíveis:</label>
            <select name="disponiveis" id="disponiveis">
                <option value="">Todos</option>
                <option value="true" {% if request.args.get('disponiveis') == 'true' %}selected{% endif %}>Sim</option>
            </select>

            <label for="marca">Marca:</label>
            <input type="text" name="marca" id="marca" value="{{ request.args.get('marca', '') }}" placeholder="Filtrar por marca">

            <label for="previsao_chegada">Previsão Chegada:</label>
            <input type="text" name="previsao_chegada" id="previsao_chegada" value="{{ request.args.get('previsao_chegada', '') }}" placeholder="Filtrar por previsão">
            
            <label for="order_by">Ordenar por:</label>
            <select name="order_by" id="order_by">
                <option value="service_asc" {% if request.args.get('order_by') == 'service_asc' %}selected{% endif %}>Nome (A-Z)</option>
                <option value="price_asc" {% if request.args.get('order_by') == 'price_asc' %}selected{% endif %}>Preço (Menor)</option>
                <option value="price_desc" {% if request.args.get('order_by') == 'price_desc' %}selected{% endif %}>Preço (Maior)</option>
                <option value="data_insercao_desc" {% if request.args.get('order_by') == 'data_insercao_desc' %}selected{% endif %}>Data Inserção (Recente)</option>
                <option value="data_insercao_asc" {% if request.args.get('order_by') == 'data_insercao_asc' %}selected{% endif %}>Data Inserção (Antiga)</option>
                <option value="previsao_chegada_asc" {% if request.args.get('order_by') == 'previsao_chegada_asc' %}selected{% endif %}>Previsão Chegada (Crescente)</option>
            </select>

            <button type="submit">Aplicar Filtros</button>
        </form>

        <form method="POST">
            <div class="miniature-list">
                {% for thumb in thumbnails %}
                <div class="miniature-item">
                    <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}" onerror="this.src='{{ logo_url }}'">
                    <h3>{{ thumb.service }}</h3>
                    <p>{{ thumb.description or 'Descrição disponível' }}</p>
                    <p class="price">Preço: R$ {{ thumb.price or 'Consultar' }}</p>
                    <p class="quantity">Disponível: {{ thumb.quantity }}</p>
                    <p class="quantity">Previsão Chegada: {{ thumb.previsao_chegada or 'N/A' }}</p>
                    <div class="reserve-options">
                        {% if thumb.quantity > 0 %}
                            <label>
                                <input type="checkbox" name="selected_items" value="{{ thumb.id }}"> Selecionar
                            </label>
                            <input type="date" name="date_{{ thumb.id }}" min="{{ tomorrow }}">
                        {% else %}
                            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá!%20Gostaria%20de%20entrar%20na%20fila%20de%20espera%20para%20{{ thumb.service | urlencode }}." target="_blank" class="whatsapp-button">Fila de Espera (WhatsApp)</a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
                {% if not thumbnails %}
                <p>Nenhuma miniatura encontrada com os filtros aplicados.</p>
                {% endif %}
            </div>
            {% if thumbnails %}
            <button type="submit" class="submit-button">Adicionar à Reserva</button>
            {% endif %}
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
        <p style="text-align: center; margin-top: 20px;"><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('profile') }}">Minhas Reservas</a></p>
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
        body { font-family: Arial, sans-serif; background: #f8f9fa; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        ul { list-style: none; padding: 0; }
        li { padding: 12px; background: #e9ecef; margin: 10px 0; border-radius: 5px; border-left: 5px solid; }
        li.approved { border-color: #28a745; background: #d4edda; }
        li.denied { border-color: #dc3545; background: #f8d7da; }
        li.pending { border-color: #ffc107; background: #fff3cd; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
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
            <li class="{{ res.status }}">
                <strong>Miniatura:</strong> {{ res.service }} <br>
                <strong>Data:</strong> {{ res.date }} <br>
                <strong>Status:</strong> {{ res.status.title() }}
                {% if res.denied_reason %} <br><em>Motivo rejeitado: {{ res.denied_reason }}</em>{% endif %}
            </li>
            {% endfor %}
            {% if not reservations %}
            <li>Nenhuma reserva encontrada. <a href="{{ url_for('reservar') }}">Faça uma agora!</a></li>
            {% endif %}
        </ul>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">
                    {{ message }}
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p style="text-align: center; margin-top: 20px;"><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout</a></p>
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
        body { font-family: Arial, sans-serif; background: #f8f9fa; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2, h3 { text-align: center; color: #333; margin-bottom: 20px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-box { background: #e9ecef; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
        .stat-box.users { background: #007bff; color: white; }
        .stat-box.pending { background: #ffc107; color: black; }
        .stat-box.low-stock { background: #dc3545; color: white; }
        .section { margin-bottom: 40px; padding: 20px; border: 1px solid #eee; border-radius: 8px; background: #fdfdfd; }
        .section h3 { margin-top: 0; border-bottom: 1px solid #eee; padding-bottom: 10px; margin-bottom: 20px; }
        ul { list-style: none; padding: 0; }
        li { padding: 12px; background: #f1f1f1; margin: 8px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        li.pending { background: #fff3cd; }
        li.approved { background: #d4edda; }
        li.denied { background: #f8d7da; }
        .actions { margin-left: 10px; display: flex; gap: 5px; flex-wrap: wrap; }
        button, .button-link { padding: 6px 10px; border: none; border-radius: 3px; cursor: pointer; font-size: 0.9em; text-decoration: none; text-align: center; display: inline-block; }
        .approve { background: #28a745; color: white; }
        .deny { background: #dc3545; color: white; }
        .delete { background: #6c757d; color: white; }
        .promote { background: #007bff; color: white; }
        .demote { background: #ffc107; color: black; }
        input[type="text"], input[type="email"], input[type="date"], select { padding: 8px; border: 1px solid #ddd; border-radius: 5px; margin-right: 10px; }
        .form-inline { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 15px; }
        .form-inline button { margin-left: 0; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .sync-button { background: #6f42c1; color: white; padding: 10px 15px; margin-bottom: 20px; display: block; width: fit-content; margin-left: auto; margin-right: auto; }
        .sync-button:hover { background: #5a2e9e; }
        .table-responsive { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
        @media (max-width: 768px) {
            .form-inline { flex-direction: column; align-items: flex-start; }
            .form-inline input, .form-inline select, .form-inline button { width: 100%; margin-right: 0; margin-bottom: 10px; }
            .actions { margin-left: 0; margin-top: 10px; width: 100%; justify-content: flex-start; }
            li { flex-direction: column; align-items: flex-start; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>Painel Admin</h2>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}" style="margin: 20px 0;">
                    {{ message }}
                </div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <div class="stats-grid">
            <div class="stat-box users">Total Usuários: {{ total_users }}</div>
            <div class="stat-box pending">Reservas Pendentes: {{ pending_reservations_count }}</div>
            <div class="stat-box low-stock">Itens com Estoque Baixo: {{ low_stock_items_count }}</div>
        </div>

        <form method="POST" action="{{ url_for('admin') }}" class="form-inline">
            <input type="hidden" name="action" value="sync_stock">
            <button type="submit" class="sync-button">Sincronizar Estoque da Planilha</button>
        </form>

        <div class="section">
            <h3>Gerenciar Usuários</h3>
            <form method="GET" class="form-inline">
                <input type="text" name="user_search" placeholder="Buscar por email" value="{{ request.args.get('user_search', '') }}">
                <select name="user_role_filter">
                    <option value="">Todos os Roles</option>
                    <option value="user" {% if request.args.get('user_role_filter') == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if request.args.get('user_role_filter') == 'admin' %}selected{% endif %}>Admin</option>
                </select>
                <button type="submit">Filtrar Usuários</button>
            </form>
            <ul>
                {% for user in users %}
                <li>
                    <span>{{ user.email }} (ID: {{ user.id }}) - Role: {{ user.role }} - Cadastrado: {{ user.data_cadastro }}</span>
                    <span class="actions">
                        {% if user.role == 'user' %}
                            <form method="POST" style="display: inline;">
                                <input type="hidden" name="action" value="promote_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="promote">Promover Admin</button>
                            </form>
                        {% elif user.role == 'admin' and user.id != session.user_id %}
                            <form method="POST" style="display: inline;">
                                <input type="hidden" name="action" value="demote_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="demote">Rebaixar User</button>
                            </form>
                        {% endif %}
                        {% if user.id != session.user_id %}
                            <form method="POST" style="display: inline;" onsubmit="return confirm('Tem certeza que deseja deletar este usuário e todas as suas reservas?');">
                                <input type="hidden" name="action" value="delete_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="delete">Deletar Usuário</button>
                            </form>
                        {% endif %}
                    </span>
                </li>
                {% endfor %}
            </ul>
        </div>

        <div class="section">
            <h3>Gerenciar Reservas</h3>
            <form method="GET" class="form-inline">
                <input type="text" name="res_search" placeholder="Buscar por miniatura/email" value="{{ request.args.get('res_search', '') }}">
                <select name="res_status_filter">
                    <option value="">Todos os Status</option>
                    <option value="pending" {% if request.args.get('res_status_filter') == 'pending' %}selected{% endif %}>Pendente</option>
                    <option value="approved" {% if request.args.get('res_status_filter') == 'approved' %}selected{% endif %}>Aprovada</option>
                    <option value="denied" {% if request.args.get('res_status_filter') == 'denied' %}selected{% endif %}>Rejeitada</option>
                </select>
                <button type="submit">Filtrar Reservas</button>
            </form>
            <ul>
                {% for res in all_reservations %}
                <li class="{{ res.status }}">
                    <span>ID {{ res.id }}: <strong>{{ res.service }}</strong> por {{ res.user_email }} em {{ res.date }} (Status: {{ res.status.title() }})</span>
                    {% if res.denied_reason %}<span> - Motivo: {{ res.denied_reason }}</span>{% endif %}
                    <span class="actions">
                        {% if res.status == 'pending' %}
                            <form method="POST" style="display: inline;">
                                <input type="hidden" name="action" value="approve_reservation">
                                <input type="hidden" name="res_id" value="{{ res.id }}">
                                <button type="submit" class="approve">Aprovar</button>
                            </form>
                            <form method="POST" style="display: inline;">
                                <input type="hidden" name="action" value="deny_reservation">
                                <input type="hidden" name="res_id" value="{{ res.id }}">
                                <input type="text" name="reason" placeholder="Motivo" required style="width: 100px; padding: 2px;">
                                <button type="submit" class="deny">Rejeitar</button>
                            </form>
                        {% endif %}
                        <form method="POST" style="display: inline;" onsubmit="return confirm('Tem certeza que deseja deletar esta reserva?');">
                            <input type="hidden" name="action" value="delete_reservation">
                            <input type="hidden" name="res_id" value="{{ res.id }}">
                            <button type="submit" class="delete">Deletar</button>
                        </form>
                    </span>
                </li>
                {% endfor %}
            </ul>
        </div>

        <div class="section">
            <h3>Miniaturas em Estoque</h3>
            <form method="GET" class="form-inline">
                <input type="text" name="miniatura_search" placeholder="Buscar por nome/marca" value="{{ request.args.get('miniatura_search', '') }}">
                <button type="submit">Buscar Miniatura</button>
            </form>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Nome</th>
                            <th>Marca/Descrição</th>
                            <th>Preço</th>
                            <th>Estoque</th>
                            <th>Previsão Chegada</th>
                            <th>Data Inserção</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for miniatura in all_miniatures %}
                        <tr>
                            <td>{{ miniatura.service }}</td>
                            <td>{{ miniatura.description }}</td>
                            <td>R$ {{ miniatura.price }}</td>
                            <td>{{ miniatura.quantity }}</td>
                            <td>{{ miniatura.previsao_chegada or 'N/A' }}</td>
                            <td>{{ miniatura.data_insercao }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="section">
            <h3>Inserir Nova Reserva (Admin)</h3>
            <form method="POST">
                <input type="hidden" name="action" value="create_reservation_admin">
                <div class="form-inline" style="margin-bottom: 15px;">
                    <label for="admin_user_id">Usuário:</label>
                    <select name="admin_user_id" id="admin_user_id" required>
                        <option value="">Selecione um usuário</option>
                        {% for user in users_for_select %}
                        <option value="{{ user.id }}">{{ user.email }}</option>
                        {% endfor %}
                    </select>

                    <label for="admin_service_id">Miniatura:</label>
                    <select name="admin_service_id" id="admin_service_id" required>
                        <option value="">Selecione uma miniatura</option>
                        {% for miniatura in all_miniatures %}
                        <option value="{{ miniatura.id }}">{{ miniatura.service }} (Estoque: {{ miniatura.quantity }})</option>
                        {% endfor %}
                    </select>

                    <label for="admin_date">Data:</label>
                    <input type="date" name="admin_date" id="admin_date" required min="{{ tomorrow }}">
                </div>
                <button type="submit" class="approve" style="width: 100%;">Criar Reserva</button>
            </form>
        </div>

        <p style="text-align: center; margin-top: 20px;"><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout Admin</a></p>
    </div>
    <script>
        const today = new Date().toISOString().split('T')[0];
        document.getElementById('admin_date').setAttribute('min', today);
    </script>
</body>
</html>
'''

# Routes
@app.route('/', methods=['GET'])
def index():
    thumbnails = load_thumbnails_from_db(filters={'disponiveis': 'true'}) # Show only available on home
    return render_template_string(INDEX_HTML, logo_url=LOGO_URL, thumbnails=thumbnails, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        if not is_valid_email(email):
            flash('Email inválido.', 'error')
            return render_template_string(LOGIN_HTML)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, email, password, role FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        
        if user and bcrypt.check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['email'] = user['email']
            session['role'] = user['role']
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
        conn = get_db_connection()
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

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    
    # Apply filters from GET request
    filters = {
        'disponiveis': request.args.get('disponiveis'),
        'marca': request.args.get('marca'),
        'previsao_chegada': request.args.get('previsao_chegada'),
        'order_by': request.args.get('order_by')
    }
    thumbnails = load_thumbnails_from_db(filters=filters)

    if request.method == 'POST':
        selected_item_ids = request.form.getlist('selected_items')
        if not selected_item_ids:
            flash('Nenhuma miniatura selecionada para reserva.', 'error')
            return redirect(url_for('reservar'))

        conn = get_db_connection()
        c = conn.cursor()
        reservations_made = 0
        
        for item_id in selected_item_ids:
            selected_date = request.form.get(f'date_{item_id}')
            if not selected_date:
                flash(f'Data não selecionada para a miniatura ID {item_id}.', 'error')
                conn.close()
                return redirect(url_for('reservar'))
            
            if selected_date < date.today().isoformat():
                flash(f'A data {selected_date} para a miniatura ID {item_id} deve ser futura.', 'error')
                conn.close()
                return redirect(url_for('reservar'))
            
            c.execute("SELECT service, quantity, max_reservas_por_usuario FROM miniatura_stock WHERE id = ?", (item_id,))
            miniatura_data = c.fetchone()

            if not miniatura_data:
                flash(f'Miniatura ID {item_id} não encontrada.', 'error')
                continue

            service_name = miniatura_data['service']
            current_quantity = miniatura_data['quantity']
            max_reservas = miniatura_data['max_reservas_por_usuario']

            if current_quantity <= 0:
                flash(f'Miniatura "{service_name}" está esgotada e não pode ser reservada.', 'error')
                continue
            
            # Check user's existing reservations for this item
            c.execute("SELECT COUNT(*) FROM reservations WHERE user_id = ? AND service = ? AND status != 'denied'", 
                      (session['user_id'], service_name))
            user_reservations_for_item = c.fetchone()[0]

            if user_reservations_for_item >= max_reservas:
                flash(f'Você já atingiu o limite de {max_reservas} reservas para "{service_name}".', 'error')
                continue

            try:
                c.execute("INSERT INTO reservations (user_id, service, date) VALUES (?, ?, ?)",
                          (session['user_id'], service_name, selected_date))
                c.execute("UPDATE miniatura_stock SET quantity = quantity - 1 WHERE id = ?", (item_id,))
                reservations_made += 1
                logger.info(f"Reserva criada para '{service_name}' por user {session['user_id']}")
            except Exception as e:
                logger.error(f"Erro ao criar reserva para {service_name}: {e}")
                flash(f'Erro ao reservar "{service_name}".', 'error')
        
        conn.commit()
        conn.close()

        if reservations_made > 0:
            flash(f'{reservations_made} reserva(s) realizada(s)! Aguarde aprovação.', 'success')
            return redirect(url_for('profile'))
        else:
            flash('Nenhuma reserva foi concluída.', 'error')
            return redirect(url_for('reservar'))

    return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/profile', methods=['GET'])
def profile():
    if 'user_id' not in session:
        flash('Faça login para ver perfil.', 'error')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT data_cadastro FROM users WHERE id = ?", (session['user_id'],))
    user_data = c.fetchone()
    data_cadastro = user_data['data_cadastro'] if user_data else 'Desconhecida'
    
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

    conn = get_db_connection()
    c = conn.cursor()

    # Handle POST actions
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'sync_stock':
            if sync_stock_from_sheets():
                flash('Estoque sincronizado com sucesso da planilha!', 'success')
            else:
                flash('Falha ao sincronizar estoque. Verifique logs.', 'error')
        
        elif action == 'approve_reservation':
            res_id = request.form.get('res_id')
            c.execute("UPDATE reservations SET status = 'approved', approved_by = ? WHERE id = ?", (session['user_id'], res_id))
            conn.commit()
            flash('Reserva aprovada.', 'success')
            logger.info(f"Admin {session['email']} aprovou reserva {res_id}")
        
        elif action == 'deny_reservation':
            res_id = request.form.get('res_id')
            reason = request.form.get('reason', 'Motivo não especificado')
            c.execute("UPDATE reservations SET status = 'denied', denied_reason = ? WHERE id = ?", (reason, res_id))
            conn.commit()
            flash('Reserva rejeitada.', 'success')
            logger.info(f"Admin {session['email']} rejeitou reserva {res_id}: {reason}")
        
        elif action == 'delete_reservation':
            res_id = request.form.get('res_id')
            c.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
            conn.commit()
            flash('Reserva deletada.', 'success')
            logger.info(f"Admin {session['email']} deletou reserva {res_id}")

        elif action == 'promote_user':
            user_id = request.form.get('user_id')
            c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
            conn.commit()
            flash('Usuário promovido a admin.', 'success')
            logger.info(f"Admin {session['email']} promoveu user {user_id}")
        
        elif action == 'demote_user':
            user_id = request.form.get('user_id')
            if int(user_id) == session['user_id']:
                flash('Você não pode rebaixar a si mesmo.', 'error')
            else:
                c.execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id,))
                conn.commit()
                flash('Usuário rebaixado para user.', 'success')
                logger.info(f"Admin {session['email']} rebaixou user {user_id}")
        
        elif action == 'delete_user':
            user_id = request.form.get('user_id')
            if int(user_id) == session['user_id']:
                flash('Você não pode deletar a si mesmo.', 'error')
            else:
                c.execute("DELETE FROM reservations WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.commit()
                flash('Usuário e suas reservas deletados.', 'success')
                logger.info(f"Admin {session['email']} deletou user {user_id}")
        
        elif action == 'create_reservation_admin':
            admin_user_id = request.form.get('admin_user_id')
            admin_service_id = request.form.get('admin_service_id')
            admin_date = request.form.get('admin_date')

            if not all([admin_user_id, admin_service_id, admin_date]):
                flash('Todos os campos para nova reserva são obrigatórios.', 'error')
            elif admin_date < date.today().isoformat():
                flash('A data da reserva deve ser futura.', 'error')
            else:
                c.execute("SELECT service, quantity FROM miniatura_stock WHERE id = ?", (admin_service_id,))
                miniatura_data = c.fetchone()
                if not miniatura_data or miniatura_data['quantity'] <= 0:
                    flash('Miniatura selecionada está esgotada ou não existe.', 'error')
                else:
                    try:
                        c.execute("INSERT INTO reservations (user_id, service, date, status) VALUES (?, ?, ?, 'pending')",
                                  (admin_user_id, miniatura_data['service'], admin_date))
                        c.execute("UPDATE miniatura_stock SET quantity = quantity - 1 WHERE id = ?", (admin_service_id,))
                        conn.commit()
                        flash('Reserva criada pelo admin com sucesso!', 'success')
                        logger.info(f"Admin {session['email']} criou reserva para user {admin_user_id} e miniatura {miniatura_data['service']}")
                    except Exception as e:
                        flash(f'Erro ao criar reserva pelo admin: {e}', 'error')
                        logger.error(f"Erro ao criar reserva pelo admin: {e}")
        
        # Redirect to GET to clear form data and re-render with updated info
        return redirect(url_for('admin'))

    # Fetch data for GET request (display)
    # Stats
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM reservations WHERE status = 'pending'")
    pending_reservations_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM miniatura_stock WHERE quantity <= 5") # Low stock threshold
    low_stock_items_count = c.fetchone()[0]

    # Users
    user_search_term = request.args.get('user_search', '')
    user_role_filter = request.args.get('user_role_filter', '')
    user_query = "SELECT id, email, role, data_cadastro FROM users WHERE 1=1"
    user_params = []
    if user_search_term:
        user_query += " AND email LIKE ?"
        user_params.append(f"%{user_search_term}%")
    if user_role_filter:
        user_query += " AND role = ?"
        user_params.append(user_role_filter)
    user_query += " ORDER BY data_cadastro DESC"
    c.execute(user_query, user_params)
    users = c.fetchall()
    users_for_select = c.execute("SELECT id, email FROM users ORDER BY email ASC").fetchall() # For admin reservation form

    # Reservations
    res_search_term = request.args.get('res_search', '')
    res_status_filter = request.args.get('res_status_filter', '')
    res_query = """
        SELECT r.id, r.service, r.date, r.status, r.denied_reason, u.email as user_email 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        WHERE 1=1
    """
    res_params = []
    if res_search_term:
        res_query += " AND (r.service LIKE ? OR u.email LIKE ?)"
        res_params.append(f"%{res_search_term}%")
        res_params.append(f"%{res_search_term}%")
    if res_status_filter:
        res_query += " AND r.status = ?"
        res_params.append(res_status_filter)
    res_query += " ORDER BY r.created_at DESC"
    c.execute(res_query, res_params)
    all_reservations = c.fetchall()

    # Miniatures (Stock)
    miniatura_search_term = request.args.get('miniatura_search', '')
    miniatura_query = "SELECT id, service, description, price, quantity, previsao_chegada, data_insercao FROM miniatura_stock WHERE 1=1"
    miniatura_params = []
    if miniatura_search_term:
        miniatura_query += " AND (service LIKE ? OR description LIKE ?)"
        miniatura_params.append(f"%{miniatura_search_term}%")
        miniatura_params.append(f"%{miniatura_search_term}%")
    miniatura_query += " ORDER BY service ASC"
    c.execute(miniatura_query, miniatura_params)
    all_miniatures = c.fetchall()

    conn.close()
    
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    return render_template_string(ADMIN_HTML, 
                                  total_users=total_users,
                                  pending_reservations_count=pending_reservations_count,
                                  low_stock_items_count=low_stock_items_count,
                                  users=users,
                                  users_for_select=users_for_select,
                                  all_reservations=all_reservations,
                                  all_miniatures=all_miniatures,
                                  tomorrow=tomorrow)

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
