import os
import json
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
from flask_bcrypt import Bcrypt

# Configuração de Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Inicialização do Flask App
app = Flask(__name__)

# Variáveis de Ambiente e Configurações
# SECRET_KEY para sessões Flask (essencial para segurança)
app.secret_key = os.environ.get('SECRET_KEY', 'uma_chave_secreta_padrao_muito_segura_para_dev')

# URL do Logo (pode ser um path local ou URL externa)
LOGO_URL = os.environ.get('LOGO_URL', '/static/logo.png')

# Diretório de persistência para o banco de dados e backups locais
PERSISTENCE_DIR = os.environ.get('PERSISTENCE_DIR', '/tmp/JG_MINIS_PERSIST_v4')
os.makedirs(PERSISTENCE_DIR, exist_ok=True)
DB_FILE = os.path.join(PERSISTENCE_DIR, 'database.db')
BACKUP_FILE = os.path.join(PERSISTENCE_DIR, 'backup_v4.json')

# Configurações do Google Sheets
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
GOOGLE_SHEETS_ID = os.environ.get('GOOGLE_SHEETS_ID')
BACKUP_SHEETS_ID = os.environ.get('BACKUP_SHEETS_ID')

# Configurações de Email
EMAIL_SENDER = os.environ.get('EMAIL_SENDER')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD') # Use App Password para Gmail
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))

# Inicialização do Bcrypt
bcrypt = Bcrypt(app)

# --- Funções de Banco de Dados (SQLite) ---
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            miniatura_id TEXT NOT NULL,
            miniatura_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            reservation_date TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    conn.commit()

    # Cria usuário admin padrão se não existir
    cursor.execute("SELECT * FROM users WHERE email = ?", ('admin@jgminis.com.br',))
    admin_user = cursor.fetchone()
    if not admin_user:
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        cursor.execute("INSERT INTO users (username, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)",
                       ('admin', 'admin@jgminis.com.br', '11999999999', hashed_password, 1))
        conn.commit()
        logging.info("Usuário admin padrão criado.")
    conn.close()
    logging.info("Banco de dados inicializado/verificado.")

# --- Funções de Backup (Google Sheets) ---
def get_gspread_client():
    try:
        if not GOOGLE_SHEETS_CREDENTIALS:
            logging.error("Variável de ambiente GOOGLE_SHEETS_CREDENTIALS não definida.")
            return None
        
        creds_json = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logging.error(f"Erro ao inicializar gspread client: {e}")
        return None

def backup_to_google_sheets(data, sheet_id, worksheet_name):
    try:
        client = get_gspread_client()
        if not client:
            logging.error("Não foi possível obter o cliente gspread. Backup para Sheets falhou.")
            return False

        sheet = client.open_by_key(sheet_id)
        worksheet = sheet.worksheet(worksheet_name)
        
        # Obter cabeçalhos existentes
        existing_headers = worksheet.row_values(1)
        
        # Se a planilha estiver vazia ou os cabeçalhos não corresponderem, adicionar/atualizar
        if not existing_headers or set(data.keys()) != set(existing_headers):
            headers = list(data.keys())
            worksheet.update('A1', [headers])
            logging.info(f"Cabeçalhos atualizados na planilha '{worksheet_name}'.")
        
        # Adicionar nova linha
        worksheet.append_row(list(data.values()))
        logging.info(f"✅ Backup criado para '{worksheet_name}' no Google Sheets.")
        return True
    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Planilha com ID '{sheet_id}' não encontrada. Verifique o GOOGLE_SHEETS_ID.")
        return False
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Worksheet '{worksheet_name}' não encontrada na planilha. Verifique o nome.")
        return False
    except Exception as e:
        logging.error(f"Erro ao fazer backup para Google Sheets: {e}")
        return False

def load_miniaturas_from_sheets():
    try:
        client = get_gspread_client()
        if not client:
            logging.warning("Não foi possível obter o cliente gspread. Usando dados padrão ou vazios para miniaturas.")
            return []

        sheet = client.open_by_key(GOOGLE_SHEETS_ID)
        worksheet = sheet.worksheet('Miniaturas') # Assumindo que as miniaturas estão em uma aba chamada 'Miniaturas'
        data = worksheet.get_all_records()
        logging.info("Miniaturas carregadas do Google Sheets com sucesso.")
        return data
    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Planilha com ID '{GOOGLE_SHEETS_ID}' não encontrada. Verifique o GOOGLE_SHEETS_ID.")
        return []
    except gspread.exceptions.WorksheetNotFound:
        logging.error("Worksheet 'Miniaturas' não encontrada na planilha. Verifique o nome.")
        return []
    except Exception as e:
        logging.error(f"Erro ao carregar miniaturas do Google Sheets: {e}")
        return []

# --- Funções de Email ---
def send_email(to_email, subject, body):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        logging.error("Variáveis de ambiente EMAIL_SENDER ou EMAIL_PASSWORD não definidas. Email não enviado.")
        return False

    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        text = msg.as_string()
        server.sendmail(EMAIL_SENDER, to_email, text)
        server.quit()
        logging.info(f"Email enviado para {to_email} com sucesso.")
        return True
    except Exception as e:
        logging.error(f"Erro ao enviar email para {to_email}: {e}")
        return False

# --- Rotas do Aplicativo ---

# HTML Templates (Inline para simplificar o deploy)
INDEX_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
        .navbar { background-color: #333; color: white; padding: 1em; display: flex; justify-content: space-between; align-items: center; }
        .navbar a { color: white; text-decoration: none; margin: 0 1em; }
        .navbar a:hover { text-decoration: underline; }
        .container { width: 90%; margin: 2em auto; background-color: white; padding: 2em; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
        h1, h2 { color: #333; text-align: center; }
        .flash-message { padding: 1em; margin-bottom: 1em; border-radius: 5px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .miniatura-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5em; margin-top: 2em; }
        .miniatura-card { border: 1px solid #ddd; border-radius: 8px; overflow: hidden; background-color: #fff; box-shadow: 0 2px 5px rgba(0,0,0,0.1); transition: transform 0.2s; }
        .miniatura-card:hover { transform: translateY(-5px); }
        .miniatura-card img { width: 100%; height: 200px; object-fit: cover; }
        .miniatura-info { padding: 1em; }
        .miniatura-info h3 { margin-top: 0; color: #0056b3; }
        .miniatura-info p { font-size: 0.9em; line-height: 1.5; }
        .miniatura-info .price { font-weight: bold; color: #28a745; font-size: 1.1em; }
        .miniatura-info .stock { font-size: 0.8em; color: #666; }
        .miniatura-info .actions { margin-top: 1em; text-align: center; }
        .miniatura-info .actions a { background-color: #007bff; color: white; padding: 0.6em 1em; border-radius: 5px; text-decoration: none; font-size: 0.9em; }
        .miniatura-info .actions a:hover { background-color: #0056b3; }
        .sort-options { text-align: center; margin-bottom: 1.5em; }
        .sort-options label { margin-right: 0.5em; font-weight: bold; }
        .sort-options select { padding: 0.5em; border-radius: 5px; border: 1px solid #ccc; }
        .sort-options button { background-color: #007bff; color: white; padding: 0.5em 1em; border: none; border-radius: 5px; cursor: pointer; margin-left: 0.5em; }
        .sort-options button:hover { background-color: #0056b3; }
        .footer { text-align: center; padding: 2em; color: #666; font-size: 0.8em; }
        .logo { max-height: 40px; margin-right: 10px; }
    </style>
</head>
<body>
    <div class="navbar">
        <div>
            {% if logo_url %}<img src="{{ logo_url }}" alt="Logo" class="logo">{% endif %}
            <a href="/">JG MINIS v4.2</a>
        </div>
        <div>
            {% if 'user_id' in session %}
                <a href="/profile"><i class="fas fa-user"></i> {{ session['username'] }}</a>
                {% if session['is_admin'] %}<a href="/admin"><i class="fas fa-cogs"></i> Admin</a>{% endif %}
                <a href="/logout"><i class="fas fa-sign-out-alt"></i> Sair</a>
            {% else %}
                <a href="/login"><i class="fas fa-sign-in-alt"></i> Login</a>
                <a href="/register"><i class="fas fa-user-plus"></i> Registrar</a>
            {% endif %}
        </div>
    </div>
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-message flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <h1>Bem-vindo ao JG MINIS v4.2</h1>
        <p style="text-align: center;">Explore nossa coleção de miniaturas!</p>

        <div class="sort-options">
            <form method="GET" action="/">
                <label for="sort_by">Ordenar por:</label>
                <select name="sort_by" id="sort_by">
                    <option value="name" {% if sort_by == 'name' %}selected{% endif %}>Nome</option>
                    <option value="price" {% if sort_by == 'price' %}selected{% endif %}>Preço</option>
                    <option value="stock" {% if sort_by == 'stock' %}selected{% endif %}>Estoque</option>
                </select>
                <select name="order" id="order">
                    <option value="asc" {% if order == 'asc' %}selected{% endif %}>Crescente</option>
                    <option value="desc" {% if order == 'desc' %}selected{% endif %}>Decrescente</option>
                </select>
                <button type="submit">Ordenar</button>
            </form>
        </div>

        <div class="miniatura-grid">
            {% for miniatura in miniaturas %}
            <div class="miniatura-card">
                <img src="{{ miniatura.image_url }}" alt="{{ miniatura.name }}">
                <div class="miniatura-info">
                    <h3>{{ miniatura.name }}</h3>
                    <p>{{ miniatura.description }}</p>
                    <p class="price">Preço: R$ {{ "%.2f"|format(miniatura.price) }}</p>
                    <p class="stock">Estoque: {{ miniatura.stock }} unidades</p>
                    <div class="actions">
                        <a href="/reservar?miniatura_id={{ miniatura.id }}">Reservar</a>
                    </div>
                </div>
            </div>
            {% else %}
            <p style="grid-column: 1 / -1; text-align: center;">Nenhuma miniatura disponível no momento.</p>
            {% endfor %}
        </div>
    </div>
    <div class="footer">
        <p>&copy; 2025 JG MINIS. Todos os direitos reservados.</p>
    </div>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - JG MINIS</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-container { background-color: white; padding: 2em; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #333; margin-bottom: 1em; }
        .form-group { margin-bottom: 1em; text-align: left; }
        .form-group label { display: block; margin-bottom: 0.5em; font-weight: bold; }
        .form-group input { width: calc(100% - 20px); padding: 0.8em; border: 1px solid #ddd; border-radius: 5px; font-size: 1em; }
        .btn-submit { background-color: #007bff; color: white; padding: 0.8em 1.5em; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; width: 100%; margin-top: 1em; }
        .btn-submit:hover { background-color: #0056b3; }
        .flash-message { padding: 1em; margin-bottom: 1em; border-radius: 5px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .register-link { margin-top: 1.5em; font-size: 0.9em; }
        .register-link a { color: #007bff; text-decoration: none; }
        .register-link a:hover { text-decoration: underline; }
        .home-link { margin-top: 1em; font-size: 0.9em; }
        .home-link a { color: #666; text-decoration: none; }
        .home-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="login-container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-message flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <h1>Login</h1>
        <form method="POST" action="/login">
            <div class="form-group">
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div class="form-group">
                <label for="password">Senha:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit" class="btn-submit">Entrar</button>
        </form>
        <div class="register-link">
            Não tem uma conta? <a href="/register">Registre-se aqui</a>
        </div>
        <div class="home-link">
            <a href="/"><i class="fas fa-home"></i> Voltar para a Home</a>
        </div>
    </div>
</body>
</html>
"""

REGISTER_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Registro - JG MINIS</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .register-container { background-color: white; padding: 2em; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #333; margin-bottom: 1em; }
        .form-group { margin-bottom: 1em; text-align: left; }
        .form-group label { display: block; margin-bottom: 0.5em; font-weight: bold; }
        .form-group input { width: calc(100% - 20px); padding: 0.8em; border: 1px solid #ddd; border-radius: 5px; font-size: 1em; }
        .btn-submit { background-color: #28a745; color: white; padding: 0.8em 1.5em; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; width: 100%; margin-top: 1em; }
        .btn-submit:hover { background-color: #218838; }
        .flash-message { padding: 1em; margin-bottom: 1em; border-radius: 5px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .login-link { margin-top: 1.5em; font-size: 0.9em; }
        .login-link a { color: #007bff; text-decoration: none; }
        .login-link a:hover { text-decoration: underline; }
        .home-link { margin-top: 1em; font-size: 0.9em; }
        .home-link a { color: #666; text-decoration: none; }
        .home-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="register-container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-message flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <h1>Registrar</h1>
        <form method="POST" action="/register">
            <div class="form-group">
                <label for="username">Nome de Usuário:</label>
                <input type="text" id="username" name="username" required>
            </div>
            <div class="form-group">
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div class="form-group">
                <label for="phone">Telefone:</label>
                <input type="tel" id="phone" name="phone" placeholder="Ex: 11987654321" required>
            </div>
            <div class="form-group">
                <label for="password">Senha:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit" class="btn-submit">Registrar</button>
        </form>
        <div class="login-link">
            Já tem uma conta? <a href="/login">Faça login aqui</a>
        </div>
        <div class="home-link">
            <a href="/"><i class="fas fa-home"></i> Voltar para a Home</a>
        </div>
    </div>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin - JG MINIS</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
        .navbar { background-color: #333; color: white; padding: 1em; display: flex; justify-content: space-between; align-items: center; }
        .navbar a { color: white; text-decoration: none; margin: 0 1em; }
        .navbar a:hover { text-decoration: underline; }
        .container { width: 90%; margin: 2em auto; background-color: white; padding: 2em; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
        h1, h2 { color: #333; text-align: center; }
        .flash-message { padding: 1em; margin-bottom: 1em; border-radius: 5px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        table { width: 100%; border-collapse: collapse; margin-top: 1.5em; }
        th, td { border: 1px solid #ddd; padding: 0.8em; text-align: left; }
        th { background-color: #f2f2f2; font-weight: bold; }
        .logo { max-height: 40px; margin-right: 10px; }
    </style>
</head>
<body>
    <div class="navbar">
        <div>
            {% if logo_url %}<img src="{{ logo_url }}" alt="Logo" class="logo">{% endif %}
            <a href="/">JG MINIS v4.2</a>
        </div>
        <div>
            {% if 'user_id' in session %}
                <a href="/profile"><i class="fas fa-user"></i> {{ session['username'] }}</a>
                {% if session['is_admin'] %}<a href="/admin"><i class="fas fa-cogs"></i> Admin</a>{% endif %}
                <a href="/logout"><i class="fas fa-sign-out-alt"></i> Sair</a>
            {% else %}
                <a href="/login"><i class="fas fa-sign-in-alt"></i> Login</a>
                <a href="/register"><i class="fas fa-user-plus"></i> Registrar</a>
            {% endif %}
        </div>
    </div>
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-message flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <h1>Painel Administrativo</h1>

        <h2>Usuários</h2>
        {% if users %}
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Nome</th>
                    <th>Email</th>
                    <th>Telefone</th>
                    <th>Admin</th>
                </tr>
            </thead>
            <tbody>
                {% for user in users %}
                <tr>
                    <td>{{ user.id }}</td>
                    <td>{{ user.username }}</td>
                    <td>{{ user.email }}</td>
                    <td>{{ user.phone }}</td>
                    <td>{{ 'Sim' if user.is_admin else 'Não' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p>Nenhum usuário registrado.</p>
        {% endif %}

        <h2>Reservas</h2>
        {% if reservations %}
        <table>
            <thead>
                <tr>
                    <th>ID Reserva</th>
                    <th>ID Usuário</th>
                    <th>Nome Usuário</th>
                    <th>Email Usuário</th>
                    <th>ID Miniatura</th>
                    <th>Nome Miniatura</th>
                    <th>Quantidade</th>
                    <th>Data Reserva</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {% for res in reservations %}
                <tr>
                    <td>{{ res.id }}</td>
                    <td>{{ res.user_id }}</td>
                    <td>{{ res.username }}</td>
                    <td>{{ res.email }}</td>
                    <td>{{ res.miniatura_id }}</td>
                    <td>{{ res.miniatura_name }}</td>
                    <td>{{ res.quantity }}</td>
                    <td>{{ res.reservation_date }}</td>
                    <td>{{ res.status }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p>Nenhuma reserva realizada.</p>
        {% endif %}
    </div>
</body>
</html>
"""

RESERVAR_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reservar Miniatura - JG MINIS</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { font-family: 'Arial', sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .reserve-container { background-color: white; padding: 2em; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 100%; max-width: 500px; text-align: center; }
        h1 { color: #333; margin-bottom: 1em; }
        .form-group { margin-bottom: 1em; text-align: left; }
        .form-group label { display: block; margin-bottom: 0.5em; font-weight: bold; }
        .form-group select, .form-group input { width: calc(100% - 20px); padding: 0.8em; border: 1px solid #ddd; border-radius: 5px; font-size: 1em; }
        .btn-submit { background-color: #ffc107; color: #333; padding: 0.8em 1.5em; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; width: 100%; margin-top: 1em; }
        .btn-submit:hover { background-color: #e0a800; }
        .flash-message { padding: 1em; margin-bottom: 1em; border-radius: 5px; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .home-link { margin-top: 1em; font-size: 0.9em; }
        .home-link a { color: #666; text-decoration: none; }
        .home-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="reserve-container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-message flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <h1>Reservar Miniatura</h1>
        <form method="POST" action="/reservar">
            <div class="form-group">
                <label for="miniatura_id">Miniatura:</label>
                <select id="miniatura_id" name="miniatura_id" required>
                    {% for miniatura in miniaturas %}
                        <option value="{{ miniatura.id }}" {% if selected_miniatura_id == miniatura.id %}selected{% endif %}>
                            {{ miniatura.name }} (Estoque: {{ miniatura.stock }})
                        </option>
                    {% endfor %}
                </select>
            </div>
            <div class="form-group">
                <label for="quantity">Quantidade:</label>
                <input type="number" id="quantity" name="quantity" min="1" value="1" required>
            </div>
            <button type="submit" class="btn-submit">Confirmar Reserva</button>
        </form>
        <div class="home-link">
            <a href="/"><i class="fas fa-home"></i> Voltar para a Home</a>
        </div>
    </div>
</body>
</html>
"""

# Inicializa o DB no startup
with app.app_context():
    init_db()

@app.before_request
def before_request():
    # Carrega o usuário da sessão para todas as requisições
    if 'user_id' in session:
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        if user:
            session['username'] = user['username']
            session['is_admin'] = user['is_admin']
        else:
            session.pop('user_id', None)
            session.pop('username', None)
            session.pop('is_admin', None)

@app.route('/')
def index():
    sort_by = request.args.get('sort_by', 'name')
    order = request.args.get('order', 'asc')

    miniaturas = load_miniaturas_from_sheets()
    
    # Dados padrão se não conseguir carregar do Sheets
    if not miniaturas:
        miniaturas = [
            {'id': 'm001', 'name': 'Miniatura Clássica', 'description': 'Uma miniatura rara e detalhada.', 'price': 150.00, 'stock': 5, 'image_url': 'https://via.placeholder.com/200x200?text=Miniatura+01'},
            {'id': 'm002', 'name': 'Miniatura Moderna', 'description': 'Design arrojado e cores vibrantes.', 'price': 120.00, 'stock': 10, 'image_url': 'https://via.placeholder.com/200x200?text=Miniatura+02'},
            {'id': 'm003', 'name': 'Miniatura Futurista', 'description': 'Tecnologia e inovação em miniatura.', 'price': 200.00, 'stock': 3, 'image_url': 'https://via.placeholder.com/200x200?text=Miniatura+03'},
        ]
        logging.warning("Usando miniaturas padrão, pois não foi possível carregar do Google Sheets.")

    # Ordenação
    if sort_by == 'name':
        miniaturas.sort(key=lambda x: x['name'], reverse=(order == 'desc'))
    elif sort_by == 'price':
        miniaturas.sort(key=lambda x: x['price'], reverse=(order == 'desc'))
    elif sort_by == 'stock':
        miniaturas.sort(key=lambda x: x['stock'], reverse=(order == 'desc'))

    return render_template_string(INDEX_HTML, miniaturas=miniaturas, sort_by=sort_by, order=order, logo_url=LOGO_URL)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and bcrypt.check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = user['is_admin']
            flash('Login bem-sucedido!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Email ou senha incorretos.', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (username, email, phone, password) VALUES (?, ?, ?, ?)",
                           (username, email, phone, hashed_password))
            conn.commit()
            flash('Usuário registrado com sucesso! Faça login.', 'success')
            
            # Backup do novo usuário para Google Sheets
            user_data = {
                'ID': cursor.lastrowid,
                'Username': username,
                'Email': email,
                'Phone': phone,
                'Is Admin': 'Não',
                'Registration Date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            backup_to_google_sheets(user_data, BACKUP_SHEETS_ID, 'Users Backup')

            # Enviar email de boas-vindas
            email_subject = "Bem-vindo ao JG MINIS!"
            email_body = f"""
            <html>
            <body>
                <p>Olá {username},</p>
                <p>Seja bem-vindo(a) ao JG MINIS! Sua conta foi criada com sucesso.</p>
                <p>Você pode fazer login usando seu email: {email}</p>
                <p>Aproveite para explorar nossa coleção de miniaturas.</p>
                <p>Atenciosamente,</p>
                <p>A equipe JG MINIS</p>
            </body>
            </html>
            """
            send_email(email, email_subject, email_body)

            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email ou nome de usuário já existem.', 'error')
        except Exception as e:
            flash(f'Erro ao registrar usuário: {e}', 'error')
            logging.error(f"Erro no registro: {e}")
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML)

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.', 'success')
    return redirect(url_for('index'))

@app.route('/admin')
def admin_panel():
    if 'user_id' not in session or not session.get('is_admin'):
        flash('Acesso negado. Apenas administradores.', 'error')
        return redirect(url_for('index'))

    conn = get_db_connection()
    users = conn.execute("SELECT * FROM users").fetchall()
    
    # Obter reservas e juntar com dados do usuário para exibição
    reservations_raw = conn.execute("SELECT * FROM reservations").fetchall()
    reservations = []
    for res in reservations_raw:
        user_res = conn.execute("SELECT username, email FROM users WHERE id = ?", (res['user_id'],)).fetchone()
        if user_res:
            res_dict = dict(res)
            res_dict['username'] = user_res['username']
            res_dict['email'] = user_res['email']
            reservations.append(res_dict)
        else:
            reservations.append(dict(res)) # Adiciona mesmo sem user_res se não encontrar

    conn.close()
    return render_template_string(ADMIN_HTML, users=users, reservations=reservations, logo_url=LOGO_URL)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Você precisa estar logado para fazer uma reserva.', 'error')
        return redirect(url_for('login'))

    miniaturas = load_miniaturas_from_sheets()
    if not miniaturas:
        miniaturas = [
            {'id': 'm001', 'name': 'Miniatura Clássica', 'description': 'Uma miniatura rara e detalhada.', 'price': 150.00, 'stock': 5, 'image_url': 'https://via.placeholder.com/200x200?text=Miniatura+01'},
            {'id': 'm002', 'name': 'Miniatura Moderna', 'description': 'Design arrojado e cores vibrantes.', 'price': 120.00, 'stock': 10, 'image_url': 'https://via.placeholder.com/200x200?text=Miniatura+02'},
        ]
        logging.warning("Usando miniaturas padrão para reserva, pois não foi possível carregar do Google Sheets.")

    selected_miniatura_id = request.args.get('miniatura_id')

    if request.method == 'POST':
        miniatura_id = request.form['miniatura_id']
        quantity = int(request.form['quantity'])
        user_id = session['user_id']

        selected_miniatura = next((m for m in miniaturas if m['id'] == miniatura_id), None)

        if not selected_miniatura:
            flash('Miniatura não encontrada.', 'error')
            return redirect(url_for('reservar'))

        if quantity <= 0:
            flash('Quantidade inválida.', 'error')
            return redirect(url_for('reservar'))

        if selected_miniatura['stock'] < quantity:
            flash(f"Estoque insuficiente para {selected_miniatura['name']}. Disponível: {selected_miniatura['stock']}.", 'error')
            return redirect(url_for('reservar'))

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO reservations (user_id, miniatura_id, miniatura_name, quantity, reservation_date, status) VALUES (?, ?, ?, ?, ?, ?)",
                           (user_id, miniatura_id, selected_miniatura['name'], quantity, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'Pendente'))
            conn.commit()
            flash('Reserva realizada com sucesso!', 'success')

            # Backup da reserva para Google Sheets
            reservation_data = {
                'ID Reserva': cursor.lastrowid,
                'ID Usuário': user_id,
                'Nome Miniatura': selected_miniatura['name'],
                'ID Miniatura': miniatura_id,
                'Quantidade': quantity,
                'Data Reserva': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'Status': 'Pendente'
            }
            backup_to_google_sheets(reservation_data, BACKUP_SHEETS_ID, 'Reservations Backup')

            # Enviar email de confirmação de reserva
            user_info = conn.execute("SELECT email, username FROM users WHERE id = ?", (user_id,)).fetchone()
            if user_info:
                email_subject = "Confirmação de Reserva JG MINIS"
                email_body = f"""
                <html>
                <body>
                    <p>Olá {user_info['username']},</p>
                    <p>Sua reserva para a miniatura <b>{selected_miniatura['name']}</b> (Quantidade: {quantity}) foi realizada com sucesso!</p>
                    <p>Detalhes da reserva:</p>
                    <ul>
                        <li>Miniatura: {selected_miniatura['name']}</li>
                        <li>Quantidade: {quantity}</li>
                        <li>Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</li>
                        <li>Status: Pendente</li>
                    </ul>
                    <p>Em breve entraremos em contato para finalizar os detalhes.</p>
                    <p>Atenciosamente,</p>
                    <p>A equipe JG MINIS</p>
                </body>
                </html>
                """
                send_email(user_info['email'], email_subject, email_body)

            return redirect(url_for('index'))
        except Exception as e:
            flash(f'Erro ao processar reserva: {e}', 'error')
            logging.error(f"Erro na reserva: {e}")
        finally:
            conn.close()
    
    return render_template_string(RESERVAR_HTML, miniaturas=miniaturas, selected_miniatura_id=selected_miniatura_id)

# Bloco para rodar o app localmente (para desenvolvimento)
if __name__ == '__main__':
    logging.info(f"🚀 JG MINIS v4.2 - Inicializando aplicação...")
    logging.info(f"Diretório de persistência: {PERSISTENCE_DIR}")
    logging.info(f"Arquivo DB: {DB_FILE}")
    
    # Tenta carregar backup local se existir
    if os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE, 'r') as f:
                # Aqui você pode adicionar lógica para restaurar o DB do backup
                # Por simplicidade, este exemplo apenas loga que o arquivo existe
                logging.info(f"Arquivo de backup encontrado em {BACKUP_FILE}. (Lógica de restauração não implementada neste exemplo)")
        except Exception as e:
            logging.warning(f"Erro ao ler arquivo de backup em {BACKUP_FILE}: {e}")
    else:
        logging.warning(f"Arquivo de backup não encontrado em {BACKUP_FILE}. Não foi possível restaurar.")

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
