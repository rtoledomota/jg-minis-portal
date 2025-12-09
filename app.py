import os
import re
import json
import csv
import io
import logging
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, send_file, abort, make_response
from werkzeug.security import generate_password_hash, check_password_hash # Standard Flask password hashing
import sqlite3

# Fallback for gspread - use only if available
GSPREAD_AVAILABLE = False
gc = None
sheet = None
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
    # Setup gspread
    GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
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
except ImportError:
    logging.warning('gspread não instalado - usando fallback sem Google Sheets')

# --- 1. Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_jg_minis_v4_2_production')

# --- 3. Environment Variables ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')  # Just numbers, no + or spaces
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

# --- 5. Validation Functions ---
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    return phone.isdigit() and 10 <= len(phone) <= 11  # 10-11 digits for Brazilian numbers

# --- 6. Database Initialization ---
def init_db():
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()

        # Users table with name, email, phone, and 'password' column
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT NOT NULL, -- Corrected column name
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
            hashed_pw = generate_password_hash('admin123') # Using werkzeug.security
            c.execute('INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)', # Using 'password'
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
        logging.info('Banco de dados inicializado com sucesso')
    except Exception as e:
        logging.error(f'Erro ao inicializar banco de dados: {e}')

init_db()

# --- 7. Helper Function to Load Thumbnails (from DB stock + Sheet data) ---
def load_thumbnails():
    thumbnails = []
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        # Get stock quantities from DB (service in lower case)
        c.execute("SELECT service, quantity FROM stock ORDER BY service")
        # Store service names in lowercase for case-insensitive lookup
        stock_data = {row[0]: row[1] for row in c.fetchall()}
        
        # Get other details from Google Sheet
        if sheet:
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
        else:
            # Fallback thumbnails if no sheet
            thumbnails = [{'service': 'Sem Integração com Planilha', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': 'Configure GOOGLE_SHEETS_CREDENTIALS para ver miniaturas.', 'marca': '', 'previsao': ''}]
            logging.warning('Usando fallback thumbnails - Google Sheets não disponível')
        
        conn.close()
    except Exception as e:
        logging.error(f'Erro ao carregar thumbnails: {e}')
        thumbnails = [{'service': 'Erro de Carregamento', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': str(e), 'marca': '', 'previsao': ''}]
    
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
        nav a { color: #007bff; text-decoration: none; margin: 0 15px; font-weight: bold; font-size: 1.2em; transition: color 0.3s; }
        nav a:hover { color: #0056b3; text-decoration: underline; }
        .flash-messages { padding: 10px 20px; margin-top: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 25px; padding: 25px; max-width: 1200px; margin: 20px auto; }
        .thumbnail { background-color: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; text-align: center; transition: transform 0.3s ease, box-shadow 0.3s ease; position: relative; }
        .thumbnail:hover { transform: translateY(-5px); box-shadow: 0 6px 16px rgba(0,0,0,0.12); }
        .thumbnail img { width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #eee; }
        .thumbnail.esgotado { opacity: 0.7; filter: grayscale(100%); }
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
        .btn-contact { background-color: #25D366; color: white; border: none; } /* Verde WhatsApp */
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
        <h1>JG Minis Portal de Reservas</h1>
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
        <p>&copy; {{ datetime.now().year }} JG Minis. Todos os direitos reservados.</p>
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
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 500px; margin: 50px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        form div { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="text"], input[type="email"], input[type="tel"], input[type="password"] {
            width: calc(100% - 22px);
            padding: 10px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 1em;
        }
        button {
            width: 100%;
            padding: 12px;
            background-color: #28a745;
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 1.1em;
            font-weight: bold;
            cursor: pointer;
            transition: background-color 0.3s ease;
        }
        button:hover { background-color: #218838; }
        .link { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; }
        .link:hover { text-decoration: underline; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Registrar</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post">
            <div>
                <label for="name">Nome:</label>
                <input type="text" id="name" name="name" required>
            </div>
            <div>
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div>
                <label for="phone">Telefone:</label>
                <input type="tel" id="phone" name="phone" pattern="[0-9]{10,11}" placeholder="Ex: 11987654321" required>
            </div>
            <div>
                <label for="password">Senha (mínimo 6 caracteres):</label>
                <input type="password" id="password" name="password" minlength="6" required>
            </div>
            <button type="submit">Registrar</button>
        </form>
        <a href="{{ url_for('login') }}" class="link">Já tem uma conta? Faça Login</a>
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
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 500px; margin: 50px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        form div { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="email"], input[type="password"] {
            width: calc(100% - 22px);
            padding: 10px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 1em;
        }
        button {
            width: 100%;
            padding: 12px;
            background-color: #007bff;
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 1.1em;
            font-weight: bold;
            cursor: pointer;
            transition: background-color 0.3s ease;
        }
        button:hover { background-color: #0056b3; }
        .link { display: block; text-align: center; margin-top: 20px; color: #28a745; text-decoration: none; }
        .link:hover { text-decoration: underline; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Login</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post">
            <div>
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div>
                <label for="password">Senha:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit">Entrar</button>
        </form>
        <a href="{{ url_for('register') }}" class="link">Não tem uma conta? Registre-se</a>
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
    <title>Reservar {{ service }} - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 600px; margin: 50px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); text-align: center; }
        h1 { color: #004085; margin-bottom: 20px; }
        .miniature-image { max-width: 250px; height: auto; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        p { font-size: 1.1em; margin-bottom: 10px; }
        form { margin-top: 20px; }
        label { display: block; margin-bottom: 10px; font-weight: bold; font-size: 1.1em; }
        input[type="number"] {
            width: 100px;
            padding: 8px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 1em;
            text-align: center;
        }
        .btn {
            padding: 10px 20px;
            border-radius: 5px;
            text-decoration: none;
            font-weight: bold;
            transition: background-color 0.3s ease, color 0.3s ease;
            margin-top: 20px;
            display: inline-block;
        }
        .btn-reserve { background-color: #28a745; color: white; border: none; }
        .btn-reserve:hover { background-color: #218838; }
        .btn-waiting { background-color: #ffc107; color: #212529; border: none; margin-left: 10px; }
        .btn-waiting:hover { background-color: #e0a800; }
        .btn-contact { background-color: #25D366; color: white; border: none; margin-left: 10px; }
        .btn-contact:hover { background-color: #1DA851; }
        .action-buttons { margin-top: 20px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Reservar {{ service }}</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <img src="{{ image_url }}" alt="{{ service }}" class="miniature-image" onerror="this.onerror=null;this.src='{{ logo_url }}';">
        <p>Disponível: {{ max_quantity }}</p>
        {% if max_quantity > 0 %}
            <form method="post">
                <label for="quantity">Quantidade:</label>
                <input type="number" id="quantity" name="quantity" min="1" max="{{ max_quantity }}" value="1" required><br><br>
                <button type="submit" class="btn btn-reserve">Confirmar Reserva</button>
            </form>
        {% else %}
            <p>Esta miniatura está esgotada no momento.</p>
            <div class="action-buttons">
                <a href="{{ url_for('add_waiting_list', service=service) }}" class="btn btn-waiting">Fila de Espera</a>
                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ service }}. Meu email: {{ session.get('email', 'anônimo') }}" class="btn btn-contact" target="_blank">Entrar em Contato</a>
            </div>
        {% endif %}
        <p style="margin-top: 30px;"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
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
    <title>Reservar Múltiplas - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: 50px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        .filter-form { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 30px; padding: 15px; border: 1px solid #eee; border-radius: 8px; background-color: #f9f9f9; }
        .filter-form label { font-weight: bold; display: flex; align-items: center; gap: 5px; }
        .filter-form input[type="text"], .filter-form select { padding: 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9em; }
        .filter-form button { padding: 8px 15px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; transition: background-color 0.3s ease; }
        .filter-form button:hover { background-color: #0056b3; }
        .miniature-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; }
        .miniature-item { border: 1px solid #ddd; padding: 15px; border-radius: 8px; background-color: #fff; box-shadow: 0 2px 5px rgba(0,0,0,0.05); text-align: center; }
        .miniature-item img { max-width: 100px; height: auto; margin-bottom: 10px; border-radius: 4px; }
        .miniature-item h3 { font-size: 1.1em; color: #333; margin-top: 0; margin-bottom: 5px; }
        .miniature-item p { font-size: 0.9em; color: #666; margin-bottom: 5px; }
        .miniature-item input[type="number"] { width: 60px; padding: 5px; border: 1px solid #ccc; border-radius: 4px; text-align: center; }
        .submit-button { width: 100%; padding: 12px; background-color: #28a745; color: white; border: none; border-radius: 4px; font-size: 1.1em; font-weight: bold; cursor: pointer; transition: background-color 0.3s ease; margin-top: 30px; }
        .submit-button:hover { background-color: #218838; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Reservar Múltiplas Miniaturas</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="get" class="filter-form">
            <label>
                <input type="checkbox" name="available" value="1" {% if request.args.get('available') == '1' %}checked{% endif %}> Disponíveis
            </label>
            <label>
                Ordenar por data:
                <select name="order">
                    <option value="">Nenhum</option>
                    <option value="asc" {% if request.args.get('order') == 'asc' %}selected{% endif %}>Ascendente</option>
                    <option value="desc" {% if request.args.get('order') == 'desc' %}selected{% endif %}>Descendente</option>
                </select>
            </label>
            <label>
                Previsão:
                <input type="text" name="previsao" value="{{ request.args.get('previsao', '') }}">
            </label>
            <label>
                Marca:
                <input type="text" name="marca" value="{{ request.args.get('marca', '') }}">
            </label>
            <button type="submit">Filtrar</button>
        </form>

        <form method="post">
            <div class="miniature-list">
                {% for thumb in thumbnails %}
                    <div class="miniature-item">
                        <input type="checkbox" name="services" value="{{ thumb.service }}" id="checkbox_{{ loop.index }}">
                        <label for="checkbox_{{ loop.index }}">
                            <img src="{{ thumb.image }}" alt="{{ thumb.service }}" onerror="this.onerror=null;this.src='{{ logo_url }}';">
                            <h3>{{ thumb.service }}</h3>
                            <p>Marca: {{ thumb.marca }}</p>
                            <p>Disponível: {{ thumb.quantity }}</p>
                        </label>
                        <label for="quantity_{{ loop.index }}">Quantidade:</label>
                        <input type="number" id="quantity_{{ loop.index }}" name="quantity_{{ thumb.service }}" min="0" max="{{ thumb.quantity }}" value="0">
                    </div>
                {% endfor %}
            </div>
            <button type="submit" class="submit-button">Reservar Selecionadas</button>
        </form>
        <p style="margin-top: 30px; text-align: center;"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
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
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: 50px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 20px; margin-bottom: 15px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #f2f2f2; font-weight: bold; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Meu Perfil</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
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
        <p style="margin-top: 30px;"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
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
    <title>Admin - JG Minis Portal de Reservas</title>
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: 50px auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h1 { color: #004085; text-align: center; margin-bottom: 30px; }
        h2 { color: #007bff; margin-top: 30px; margin-bottom: 15px; border-bottom: 2px solid #eee; padding-bottom: 5px; }
        .stats { display: flex; justify-content: space-around; flex-wrap: wrap; gap: 20px; margin-bottom: 30px; }
        .stat-box { background-color: #e9ecef; padding: 15px; border-radius: 8px; text-align: center; flex: 1; min-width: 150px; }
        .stat-box strong { display: block; font-size: 1.5em; color: #004085; }
        .filter-form { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 30px; padding: 15px; border: 1px solid #eee; border-radius: 8px; background-color: #f9f9f9; }
        .filter-form label { font-weight: bold; display: flex; align-items: center; gap: 5px; }
        .filter-form input[type="text"], .filter-form select { padding: 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9em; }
        .filter-form button { padding: 8px 15px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; transition: background-color 0.3s ease; }
        .filter-form button:hover { background-color: #0056b3; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; font-size: 0.9em; }
        th { background-color: #f2f2f2; font-weight: bold; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        form.inline-form { display: inline-block; margin-left: 5px; }
        form.inline-form button { padding: 5px 10px; font-size: 0.85em; border-radius: 4px; cursor: pointer; transition: background-color 0.3s ease; }
        .btn-promote { background-color: #28a745; color: white; border: none; } .btn-promote:hover { background-color: #218838; }
        .btn-demote { background-color: #ffc107; color: #212529; border: none; } .btn-demote:hover { background-color: #e0a800; }
        .btn-delete { background-color: #dc3545; color: white; border: none; } .btn-delete:hover { background-color: #c82333; }
        .btn-approve { background-color: #28a745; color: white; border: none; } .btn-approve:hover { background-color: #218838; }
        .btn-deny { background-color: #ffc107; color: #212529; border: none; } .btn-deny:hover { background-color: #e0a800; }
        .form-section { background-color: #f9f9f9; padding: 20px; border-radius: 8px; margin-top: 30px; }
        .form-section label { display: block; margin-bottom: 8px; font-weight: bold; }
        .form-section input[type="text"], .form-section input[type="number"], .form-section input[type="url"], .form-section select {
            width: calc(100% - 22px); padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 1em; margin-bottom: 15px;
        }
        .form-section button { padding: 10px 20px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; transition: background-color 0.3s ease; }
        .form-section button:hover { background-color: #0056b3; }
        .admin-actions { display: flex; flex-wrap: wrap; gap: 15px; margin-top: 30px; }
        .admin-actions button { padding: 10px 20px; background-color: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; transition: background-color 0.3s ease; }
        .admin-actions button:hover { background-color: #5a6268; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Painel Administrativo</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <h2>Estatísticas</h2>
        <div class="stats">
            <div class="stat-box"><strong>{{ stats.users }}</strong> Usuários</div>
            <div class="stat-box"><strong>{{ stats.pending }}</strong> Reservas Pendentes</div>
            <div class="stat-box"><strong>{{ stats.total_res }}</strong> Total de Reservas</div>
        </div>

        <h2>Filtros</h2>
        <form method="get" class="filter-form">
            <label>Email Usuário: <input type="text" name="user_email_filter" value="{{ request.args.get('user_email_filter', '') }}"></label>
            <label>Role:
                <select name="user_role_filter">
                    <option value="">Todas</option>
                    <option value="user" {% if request.args.get('user_role_filter') == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if request.args.get('user_role_filter') == 'admin' %}selected{% endif %}>Admin</option>
                </select>
            </label>
            <label>Serviço Reserva: <input type="text" name="res_service_filter" value="{{ request.args.get('res_service_filter', '') }}"></label>
            <label>Status Reserva:
                <select name="res_status_filter">
                    <option value="">Todos</option>
                    <option value="pending" {% if request.args.get('res_status_filter') == 'pending' %}selected{% endif %}>Pendente</option>
                    <option value="approved" {% if request.args.get('res_status_filter') == 'approved' %}selected{% endif %}>Aprovada</option>
                    <option value="denied" {% if request.args.get('res_status_filter') == 'denied' %}selected{% endif %}>Negada</option>
                </select>
            </label>
            <button type="submit">Aplicar Filtros</button>
        </form>

        <h2>Gerenciamento de Usuários</h2>
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
                            <form method="post" action="{{ url_for('admin') }}" class="inline-form">
                                <input type="hidden" name="action" value="promote_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="btn-promote">Promover</button>
                            </form>
                            <form method="post" action="{{ url_for('admin') }}" class="inline-form">
                                <input type="hidden" name="action" value="demote_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="btn-demote">Rebaixar</button>
                            </form>
                            <form method="post" action="{{ url_for('admin') }}" class="inline-form" onsubmit="return confirm('Tem certeza que deseja deletar este usuário?');">
                                <input type="hidden" name="action" value="delete_user">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <button type="submit" class="btn-delete">Deletar</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>Gerenciamento de Reservas</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Usuário</th>
                    <th>Serviço</th>
                    <th>Quantidade</th>
                    <th>Status</th>
                    <th>Data</th>
                    <th>Ações</th>
                </tr>
            </thead>
            <tbody>
                {% for res in reservations %}
                    <tr>
                        <td>{{ res.id }}</td>
                        <td>{{ res.user_email }}</td>
                        <td>{{ res.service }}</td>
                        <td>{{ res.quantity }}</td>
                        <td>{{ res.status }}</td>
                        <td>{{ res.created_at }}</td>
                        <td>
                            {% if res.status == 'pending' %}
                                <form method="post" action="{{ url_for('admin') }}" class="inline-form">
                                    <input type="hidden" name="action" value="approve_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <button type="submit" class="btn-approve">Aprovar</button>
                                </form>
                                <form method="post" action="{{ url_for('admin') }}" class="inline-form">
                                    <input type="hidden" name="action" value="deny_res">
                                    <input type="hidden" name="res_id" value="{{ res.id }}">
                                    <input type="text" name="reason" placeholder="Motivo" style="width: 80px;">
                                    <button type="submit" class="btn-deny">Negar</button>
                                </form>
                            {% endif %}
                            <form method="post" action="{{ url_for('admin') }}" class="inline-form" onsubmit="return confirm('Tem certeza que deseja deletar esta reserva? O estoque será restaurado.');">
                                <input type="hidden" name="action" value="delete_res">
                                <input type="hidden" name="res_id" value="{{ res.id }}">
                                <button type="submit" class="btn-delete">Deletar</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="form-section">
            <h2>Inserir Nova Miniatura</h2>
            <form method="post" action="{{ url_for('admin') }}">
                <input type="hidden" name="action" value="insert_miniature">
                <label>Serviço: <input type="text" name="service" required></label>
                <label>Marca: <input type="text" name="marca" required></label>
                <label>Observações: <input type="text" name="obs"></label>
                <label>Preço: <input type="number" name="price" step="0.01" required></label>
                <label>Quantidade Inicial: <input type="number" name="quantity" required></label>
                <label>Imagem URL: <input type="url" name="image" required></label>
                <button type="submit">Inserir Miniatura</button>
            </form>
        </div>

        <div class="form-section">
            <h2>Inserir Nova Reserva</h2>
            <form method="post" action="{{ url_for('admin') }}">
                <input type="hidden" name="action" value="insert_reservation">
                <label>Usuário:
                    <select name="user_id" required>
                        {% for u in all_users %}<option value="{{ u.id }}">{{ u.email }}</option>{% endfor %}
                    </select>
                </label>
                <label>Serviço:
                    <select name="service" required>
                        {% for s in all_services %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
                    </select>
                </label>
                <label>Quantidade: <input type="number" name="quantity" min="1" required></label>
                <label>Status:
                    <select name="status">
                        <option value="pending">Pendente</option>
                        <option value="approved">Aprovada</option>
                        <option value="denied">Negada</option>
                    </select>
                </label>
                <label>Motivo (se negada): <input type="text" name="reason"></label>
                <button type="submit">Inserir Reserva</button>
            </form>
        </div>

        <h2>Ações Administrativas</h2>
        <div class="admin-actions">
            <form method="post" action="{{ url_for('admin') }}">
                <input type="hidden" name="action" value="sync_stock">
                <button type="submit">Sincronizar Estoque da Planilha</button>
            </form>
            <button onclick="window.location.href='{{ url_for('backup') }}'">Backup JSON Completo</button>
            <button onclick="window.location.href='{{ url_for('export_csv') }}'">Exportar Reservas CSV</button>
        </div>

        <h2>Fila de Espera</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Usuário</th>
                    <th>Serviço</th>
                    <th>Data de Entrada</th>
                    <th>Ações</th>
                </tr>
            </thead>
            <tbody>
                {% for wl in waiting_list %}
                    <tr>
                        <td>{{ wl.id }}</td>
                        <td>{{ wl.user_email }}</td>
                        <td>{{ wl.service }}</td>
                        <td>{{ wl.created_at }}</td>
                        <td>
                            <form method="post" action="{{ url_for('admin') }}" class="inline-form" onsubmit="return confirm('Tem certeza que deseja remover este item da fila de espera?');">
                                <input type="hidden" name="action" value="delete_waiting">
                                <input type="hidden" name="wl_id" value="{{ wl.id }}">
                                <button type="submit" class="btn-delete">Remover</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>
        <p style="margin-top: 30px; text-align: center;"><a href="{{ url_for('index') }}">Voltar para Home</a></p>
    </div>
</body>
</html>
'''

# --- Rotas ---
@app.route('/')
def index():
    try:
        thumbnails = load_thumbnails()
        return render_template_string(INDEX_HTML, thumbnails=thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, datetime=datetime)
    except Exception as e:
        logging.error(f'Erro na rota index: {e}')
        flash('Erro interno ao carregar a página inicial.', 'error')
        return render_template_string(INDEX_HTML, thumbnails=[], logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, datetime=datetime), 500

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            name = request.form['name'].strip()
            email = request.form['email'].strip().lower()
            phone = request.form['phone'].strip()
            password = request.form['password']

            # Validação robusta
            if not name:
                flash('Nome é obrigatório.', 'error')
                return redirect(url_for('register'))
            if not is_valid_email(email):
                flash('Email inválido.', 'error')
                return redirect(url_for('register'))
            if not is_valid_phone(phone):
                flash('Telefone inválido (10 ou 11 dígitos).', 'error')
                return redirect(url_for('register'))
            if len(password) < 6:
                flash('Senha deve ter pelo menos 6 caracteres.', 'error')
                return redirect(url_for('register'))

            hashed_password = generate_password_hash(password) # Usando werkzeug.security

            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)", # Coluna 'password'
                      (name, email, phone, hashed_password))
            conn.commit()
            conn.close()
            flash('Registro realizado! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email já cadastrado.', 'error')
            return redirect(url_for('register'))
        except Exception as e:
            logging.error(f"Erro no registro: {e}")
            flash('Erro interno no registro.', 'error')
            return redirect(url_for('register'))
    return render_template_string(REGISTER_HTML, logo_url=LOGO_URL)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            email = request.form['email'].strip().lower()
            password = request.form['password']
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute('SELECT id, password, role, email FROM users WHERE email = ?', (email,))
            user = c.fetchone()
            conn.close()
            if user and check_password_hash(user['password'], password): # Usando werkzeug.security
                session['user_id'] = user['id']
                session['role'] = user['role']
                session['email'] = user['email']
                flash('Login realizado com sucesso!', 'success')
                return redirect(url_for('index'))
            else:
                flash('Email ou senha inválidos.', 'error')
                return redirect(url_for('login'))
        except Exception as e:
            logging.error(f"Erro no login: {e}")
            flash('Erro interno no login.', 'error')
            return redirect(url_for('login'))
    return render_template_string(LOGIN_HTML, logo_url=LOGO_URL)

@app.route('/logout')
def logout():
    session.clear()
    flash('Você foi desconectado.', 'success')
    return redirect(url_for('index'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Faça login para acessar seu perfil.', 'error')
        return redirect(url_for('login'))
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('SELECT service, quantity, status, created_at FROM reservations WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
        reservations = c.fetchall()
        conn.close()
        return render_template_string(PROFILE_HTML, reservations=reservations, logo_url=LOGO_URL)
    except Exception as e:
        logging.error(f"Erro ao carregar perfil do usuário {session.get('user_id')}: {e}")
        flash('Erro interno ao carregar seu perfil.', 'error')
        return redirect(url_for('index'))

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Faça login para reservar miniaturas.', 'error')
        return redirect(url_for('login'))
    
    all_thumbnails = load_thumbnails()
    filtered_thumbnails = all_thumbnails

    # Aplicar filtros (GET request)
    if request.method == 'GET':
        available = request.args.get('available') == '1'
        order = request.args.get('order')
        previsao_search = request.args.get('previsao', '').strip().lower()
        marca_search = request.args.get('marca', '').strip().lower()

        if available:
            filtered_thumbnails = [t for t in filtered_thumbnails if t['quantity'] > 0]
        if previsao_search:
            filtered_thumbnails = [t for t in filtered_thumbnails if previsao_search in t['previsao'].lower()]
        if marca_search:
            filtered_thumbnails = [t for t in filtered_thumbnails if marca_search in t['marca'].lower()]
        
        if order == 'asc':
            filtered_thumbnails.sort(key=lambda x: x['previsao'] if x['previsao'] else 'ZZZ') # Sort by previsao, empty last
        elif order == 'desc':
            filtered_thumbnails.sort(key=lambda x: x['previsao'] if x['previsao'] else 'AAA', reverse=True)

    # Processar reservas (POST request)
    if request.method == 'POST':
        services_to_reserve = request.form.getlist('services')
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        reservations_made = 0
        for service_name in services_to_reserve:
            try:
                quantity_str = request.form.get(f'quantity_{service_name}', '0')
                quantity = int(quantity_str)
                
                if quantity <= 0:
                    continue

                c.execute('SELECT quantity FROM stock WHERE service = ?', (service_name.lower(),))
                stock_row = c.fetchone()
                current_stock = stock_row[0] if stock_row else 0

                if current_stock >= quantity:
                    c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)',
                              (session['user_id'], service_name, quantity))
                    c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service_name.lower()))
                    reservations_made += 1
                else:
                    flash(f'Estoque insuficiente para {service_name}. Disponível: {current_stock}.', 'error')
            except Exception as e:
                logging.error(f"Erro ao processar reserva múltipla para {service_name}: {e}")
                flash(f'Erro ao reservar {service_name}.', 'error')
        
        conn.commit()
        conn.close()
        if reservations_made > 0:
            flash(f'{reservations_made} reserva(s) realizada(s) com sucesso!', 'success')
        return redirect(url_for('profile'))
    
    return render_template_string(RESERVAR_HTML, thumbnails=filtered_thumbnails, logo_url=LOGO_URL)

@app.route('/reserve_single/<service>', methods=['GET', 'POST'])
def reserve_single(service):
    if 'user_id' not in session:
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Get stock and image_url
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),))
    stock_row = c.fetchone()
    max_quantity = stock_row[0] if stock_row else 0

    # Get image_url from sheet data (load_thumbnails provides this)
    thumbnails = load_thumbnails() # This is inefficient, but simple for now
    thumb_data = next((t for t in thumbnails if t['service'].lower() == service.lower()), None)
    image_url = thumb_data['image'] if thumb_data else LOGO_URL

    if request.method == 'POST':
        try:
            quantity = int(request.form['quantity'])
            if quantity <= 0 or quantity > max_quantity:
                flash('Quantidade inválida ou insuficiente no estoque.', 'error')
                conn.close()
                return render_template_string(RESERVE_SINGLE_HTML, service=service, max_quantity=max_quantity, image_url=image_url, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)
            
            c.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)',
                      (session['user_id'], service, quantity))
            c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
            conn.commit()
            flash('Reserva realizada com sucesso!', 'success')
            conn.close()
            return redirect(url_for('profile'))
        except Exception as e:
            logging.error(f"Erro ao processar reserva individual para {service}: {e}")
            flash('Erro ao processar sua reserva.', 'error')
            conn.close()
            return render_template_string(RESERVE_SINGLE_HTML, service=service, max_quantity=max_quantity, image_url=image_url, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)
    
    conn.close()
    return render_template_string(RESERVE_SINGLE_HTML, service=service, max_quantity=max_quantity, image_url=image_url, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/add_waiting_list/<service>')
def add_waiting_list(service):
    if 'user_id' not in session:
        flash('Faça login para entrar na fila de espera.', 'error')
        return redirect(url_for('login'))
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], service))
        conn.commit()
        flash(f'Você entrou na fila de espera para {service}! Entraremos em contato.', 'success')
    except sqlite3.IntegrityError:
        flash(f'Você já está na fila de espera para {service}.', 'info')
    except Exception as e:
        logging.error(f"Erro ao adicionar {session.get('user_id')} à fila de espera para {service}: {e}")
        flash('Erro interno ao adicionar à fila de espera.', 'error')
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

    # Process POST actions first
    if request.method == 'POST':
        action = request.form.get('action')
        try:
            if action == 'promote_user':
                user_id = request.form['user_id']
                c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
                flash(f'Usuário {user_id} promovido a admin.', 'success')
            elif action == 'demote_user':
                user_id = request.form['user_id']
                c.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
                flash(f'Usuário {user_id} rebaixado para user.', 'success')
            elif action == 'delete_user':
                user_id = request.form['user_id']
                c.execute('DELETE FROM users WHERE id = ?', (user_id,))
                flash(f'Usuário {user_id} deletado.', 'success')
            elif action == 'approve_res':
                res_id = request.form['res_id']
                c.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
                flash(f'Reserva {res_id} aprovada.', 'success')
            elif action == 'deny_res':
                res_id = request.form['res_id']
                reason = request.form.get('reason', 'Motivo não especificado')
                c.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
                flash(f'Reserva {res_id} negada.', 'success')
            elif action == 'delete_res':
                res_id = request.form['res_id']
                # Restore stock
                c.execute('SELECT service, quantity FROM reservations WHERE id = ?', (res_id,))
                res_data = c.fetchone()
                if res_data:
                    service_name = res_data[0].lower()
                    quantity_to_restore = res_data[1]
                    c.execute('UPDATE stock SET quantity = quantity + ? WHERE service = ?', (quantity_to_restore, service_name))
                    flash(f'Estoque de {service_name} restaurado em {quantity_to_restore} unidades.', 'info')
                c.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
                flash(f'Reserva {res_id} deletada.', 'success')
            elif action == 'insert_miniature':
                service = request.form['service'].strip()
                marca = request.form['marca'].strip()
                obs = request.form.get('obs', '').strip()
                price = float(request.form['price'])
                quantity = int(request.form['quantity'])
                image = request.form['image'].strip()
                # Insert into stock (service name stored lower for consistency)
                c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service.lower(), quantity))
                # Note: Other details like marca, obs, price, image are not stored in DB stock table, only in Google Sheet.
                # If you want to store them, stock table schema needs to be extended.
                flash(f'Miniatura "{service}" inserida/atualizada no estoque.', 'success')
            elif action == 'insert_reservation':
                user_id = request.form['user_id']
                service = request.form['service'].strip()
                quantity = int(request.form['quantity'])
                status = request.form['status']
                reason = request.form.get('reason', '').strip()
                
                # Check stock if status is approved
                if status == 'approved':
                    c.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),))
                    stock_row = c.fetchone()
                    current_stock = stock_row[0] if stock_row else 0
                    if current_stock < quantity:
                        flash(f'Estoque insuficiente para {service} ({current_stock} disponíveis). Reserva não inserida como aprovada.', 'error')
                        status = 'pending' # Force to pending if not enough stock
                
                c.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)',
                          (user_id, service, quantity, status, reason))
                if status == 'approved':
                    c.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
                flash(f'Reserva para {service} inserida.', 'success')
            elif action == 'sync_stock':
                if sheet:
                    try:
                        records = sheet.get_all_records()
                        for record in records:
                            service = record.get('NOME DA MINIATURA', '').strip().lower()
                            qty = record.get('QUANTIDADE DISPONIVEL', 0)
                            if service:
                                c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
                        flash('Estoque sincronizado com a planilha.', 'success')
                    except Exception as e:
                        logging.error(f'Erro na sincronização do estoque com a planilha: {e}')
                        flash('Erro na sincronização do estoque.', 'error')
                else:
                    flash('Integração com Google Sheets não configurada.', 'error')
            elif action == 'delete_waiting':
                wl_id = request.form['wl_id']
                c.execute('DELETE FROM waiting_list WHERE id = ?', (wl_id,))
                flash(f'Item da fila de espera {wl_id} removido.', 'success')
            
            conn.commit()
        except Exception as e:
            logging.error(f"Erro na ação admin '{action}': {e}")
            flash(f'Erro ao executar ação "{action}". Detalhes no log do servidor.', 'error')
        finally:
            conn.close()
            return redirect(url_for('admin')) # Redirect after POST to prevent re-submission

    # Fetch data for GET request (or after POST redirect)
    # Stats
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reservations')
    total_res = c.fetchone()[0]
    stats = {'users': users_count, 'pending': pending_count, 'total_res': total_res}

    # Filters for Users
    user_email_filter = request.args.get('user_email_filter', '').strip().lower()
    user_role_filter = request.args.get('user_role_filter', '').strip().lower()
    query_users = 'SELECT id, name, email, phone, role FROM users WHERE 1=1'
    params_users = []
    if user_email_filter:
        query_users += ' AND email LIKE ?'
        params_users.append(f'%{user_email_filter}%')
    if user_role_filter:
        query_users += ' AND role = ?'
        params_users.append(user_role_filter)
    users = c.execute(query_users, params_users).fetchall()

    # Filters for Reservations
    res_service_filter = request.args.get('res_service_filter', '').strip().lower()
    res_status_filter = request.args.get('res_status_filter', '').strip().lower()
    query_reservations = '''
        SELECT r.id, u.email AS user_email, r.service, r.quantity, r.status, r.created_at, r.denied_reason
        FROM reservations r JOIN users u ON r.user_id = u.id WHERE 1=1
    '''
    params_reservations = []
    if res_service_filter:
        query_reservations += ' AND r.service LIKE ?'
        params_reservations.append(f'%{res_service_filter}%')
    if res_status_filter:
        query_reservations += ' AND r.status = ?'
        params_reservations.append(res_status_filter)
    query_reservations += ' ORDER BY r.created_at DESC'
    reservations = c.execute(query_reservations, params_reservations).fetchall()

    # Data for Insert Forms
    c.execute('SELECT id, email FROM users ORDER BY email')
    all_users = c.fetchall()
    all_services = [thumb['service'] for thumb in load_thumbnails()] # Get all service names

    # Waiting List
    query_waiting_list = '''
        SELECT wl.id, u.email AS user_email, wl.service, wl.created_at
        FROM waiting_list wl JOIN users u ON wl.user_id = u.id ORDER BY wl.created_at DESC
    '''
    waiting_list = c.execute(query_waiting_list).fetchall()

    conn.close()
    return render_template_string(ADMIN_HTML, stats=stats, users=users, reservations=reservations, 
                                  all_users=all_users, all_services=all_services, waiting_list=waiting_list, 
                                  logo_url=LOGO_URL, datetime=datetime)

@app.route('/backup')
def backup():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users')
        users = [dict(row) for row in c.fetchall()]
        
        c.execute('SELECT id, service, quantity, last_sync FROM stock')
        stock = [dict(row) for row in c.fetchall()]
        
        c.execute('SELECT id, user_id, service, quantity, status, approved_by, denied_reason, created_at FROM reservations')
        reservations = [dict(row) for row in c.fetchall()]

        c.execute('SELECT id, user_id, service, created_at FROM waiting_list')
        waiting_list = [dict(row) for row in c.fetchall()]
        
        conn.close()
        
        data = {
            'timestamp': datetime.now().isoformat(),
            'users': users,
            'stock': stock,
            'reservations': reservations,
            'waiting_list': waiting_list
        }
        
        response = make_response(json.dumps(data, indent=4, default=str)) # default=str for datetime objects
        response.headers['Content-Disposition'] = 'attachment; filename=jgminis_backup_{}.json'.format(datetime.now().strftime('%Y%m%d%H%M%S'))
        response.headers['Content-Type'] = 'application/json'
        return response
    except Exception as e:
        logging.error(f"Erro ao gerar backup JSON: {e}")
        flash('Erro interno ao gerar backup JSON.', 'error')
        return redirect(url_for('admin'))

@app.route('/export_csv')
def export_csv():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('index'))
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''
            SELECT r.id, u.name AS user_name, u.email, r.service, r.quantity, r.status, r.created_at, r.denied_reason
            FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY r.created_at DESC
        ''')
        rows = c.fetchall()
        conn.close()
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow(['ID da Reserva', 'Nome do Usuário', 'Email do Usuário', 'Serviço', 'Quantidade', 'Status', 'Data da Reserva', 'Motivo da Negação'])
        for row in rows:
            writer.writerow([row['id'], row['user_name'], row['email'], row['service'], row['quantity'], row['status'], row['created_at'], row['denied_reason']])
        
        output.seek(0)
        
        response = make_response(output.getvalue())
        response.headers['Content-Disposition'] = 'attachment; filename=jgminis_reservations_{}.csv'.format(datetime.now().strftime('%Y%m%d%H%M%S'))
        response.headers['Content-Type'] = 'text/csv'
        return response
    except Exception as e:
        logging.error(f"Erro ao exportar CSV de reservas: {e}")
        flash('Erro interno ao exportar reservas CSV.', 'error')
        return redirect(url_for('admin'))

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.errorhandler(404)
def page_not_found(e):
    flash('A página que você tentou acessar não foi encontrada.', 'error')
    return redirect(url_for('index')), 404

@app.errorhandler(500)
def internal_error(e):
    logging.error(f'Erro interno do servidor: {e}')
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return redirect(url_for('index')), 500

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
