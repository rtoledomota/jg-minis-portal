import os
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, abort
from flask_bcrypt import Bcrypt
import gspread
from google.oauth2.service_account import Credentials
import smtplib
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart
import re

# Configurações de logging para Railway (debug em produção)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configurações de ambiente (com fallbacks para desenvolvimento local)
LOGO_URL = os.environ.get('LOGO_URL', 'https://via.placeholder.com/150x50?text=JG+MINIS+Logo')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')  # JSON string minificado
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025_dev_key_fallback')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')  # SQLite path para Railway/Heroku
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASS = os.environ.get('SMTP_PASS')

# Inicializa Flask e Bcrypt
app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# Configuração gspread com autenticação moderna (google-auth)
gc = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.readonly']
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

# Inicialização do banco de dados SQLite
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Tabela users
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  role TEXT DEFAULT 'user',
                  data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Tabela reservations
    c.execute('''CREATE TABLE IF NOT EXISTS reservations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  service TEXT NOT NULL,
                  date TEXT NOT NULL,  -- Formato YYYY-MM-DD
                  status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'denied'
                  approved_by INTEGER,  -- ID do admin que aprovou
                  denied_reason TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id))''')
    # Cria usuário admin padrão se não existir
    c.execute("SELECT id FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (email, password, role) VALUES ('admin@jgminis.com.br', ?, 'admin')", (hashed_password,))
        logger.info("Usuário admin padrão criado no DB")
    conn.commit()
    conn.close()

init_db()

# Função para enviar email
def send_email(to_email, subject, body):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP_USER ou SMTP_PASS ausente - email não enviado")
        return False
    msg = MimeMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MimeText(body, 'plain'))
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        text = msg.as_string()
        server.sendmail(SMTP_USER, to_email, text)
        server.quit()
        logger.info(f"Email enviado para {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Erro ao enviar email para {to_email}: {e}")
        return False

# Templates HTML inline (detalhados com CSS responsivo)
INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2 - Serviços</title>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 0; background: #f8f9fa; color: #333; }
        header { text-align: center; padding: 20px; background: #007bff; color: white; }
        img.logo { max-width: 150px; height: auto; margin: 10px; }
        h1 { margin: 0; }
        .thumbnails { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; padding: 20px; max-width: 1200px; margin: 0 auto; }
        .thumbnail { background: white; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); padding: 15px; text-align: center; transition: transform 0.2s; }
        .thumbnail:hover { transform: translateY(-5px); }
        .thumbnail img { width: 100%; height: 150px; object-fit: cover; border-radius: 8px; margin-bottom: 10px; }
        .thumbnail h3 { margin: 10px 0; color: #007bff; }
        .thumbnail p { margin: 5px 0; font-size: 0.9em; color: #666; }
        .thumbnail .price { font-weight: bold; color: #28a745; font-size: 1.1em; }
        .thumbnail a { display: inline-block; margin-top: 10px; padding: 8px 15px; background: #28a745; color: white; text-decoration: none; border-radius: 5px; }
        .thumbnail a:hover { background: #218838; }
        nav { text-align: center; padding: 10px; background: #e9ecef; border-bottom: 1px solid #dee2e6; }
        nav a { margin: 0 15px; color: #007bff; text-decoration: none; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 10px auto; border-radius: 5px; text-align: center; max-width: 800px; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        footer { text-align: center; padding: 20px; background: #343a40; color: white; margin-top: 40px; }
        @media (max-width: 600px) { .thumbnails { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" class="logo">
        <h1>Bem-vindo ao JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        {% if session.user_id %}
            <a href="{{ url_for('profile') }}">Meu Perfil</a>
            <a href="{{ url_for('reservar') }}">Reservar Serviço</a>
            {% if session.role == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
            <a href="{{ url_for('logout') }}">Logout</a>
        {% else %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% endif %}
    </nav>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
            <div class="flash flash-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <main class="thumbnails">
        {% for thumb in thumbnails %}
        <div class="thumbnail">
            <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}">
            <h3>{{ thumb.service }}</h3>
            <p>{{ thumb.description or 'Descrição não disponível' }}</p>
            <p class="price">R$ {{ thumb.price or 'Consultar' }}</p>
            <a href="{{ url_for('reservar') }}">Reservar Agora</a>
        </div>
        {% endfor %}
        {% if not thumbnails %}
        <div class="thumbnail">
            <p>Nenhum serviço disponível no momento. Por favor, volte mais tarde ou entre em contato.</p>
        </div>
        {% endif %}
    </main>
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
        h2 { color: #333; margin-bottom: 20px; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #0056b3; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        p { margin-top: 15px; }
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
        h2 { color: #333; margin-bottom: 20px; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #218838; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        p { margin-top: 15px; }
    </style>
</head>
<body>
    <div class="form-container">
        <h2>Registrar Usuário</h2>
        <form method="POST">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Senha (mín. 6 caracteres)" required minlength="6">
            <button type="submit">Registrar</button>
        </form>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ category }}">{{ message }}</div>
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
    <title>Reservar Serviço - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; padding: 20px; }
        .container { max-width: 500px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        form { display: flex; flex-direction: column; }
        label { margin: 10px 0 5px; font-weight: bold; color: #555; }
        input, select { padding: 10px; border: 1px solid #ddd; border-radius: 5px; margin-bottom: 15px; font-size: 1em; }
        button { padding: 12px; background: #ffc107; color: black; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; font-weight: bold; }
        button:hover { background: #e0a800; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .services-list { margin-top: 30px; border-top: 1px solid #eee; padding-top: 20px; }
        .service-option { padding: 10px; background: #e9ecef; margin: 8px 0; border-radius: 5px; cursor: pointer; text-align: left; }
        .service-option:hover { background: #dee2e6; }
        .service-option strong { color: #007bff; }
        .service-option .price { float: right; font-weight: bold; color: #28a745; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Reservar Serviço</h2>
        {% if not session.user_id %}
        <div class="flash flash-error">
            <p>Faça <a href="{{ url_for('login') }}">login</a> para reservar.</p>
        </div>
        {% else %}
        <form method="POST">
            <label for="service">Serviço:</label>
            <select name="service" id="service" required>
                <option value="">Selecione um serviço</option>
                {% for thumb in thumbnails %}
                <option value="{{ thumb.service }}">{{ thumb.service }} - R$ {{ thumb.price }}</option>
                {% endfor %}
            </select>
            <label for="date">Data (apenas datas futuras):</label>
            <input type="date" name="date" id="date" required min="{{ tomorrow }}">
            <button type="submit">Confirmar Reserva</button>
        </form>
        <div class="services-list">
            <h3>Serviços Disponíveis:</h3>
            {% for thumb in thumbnails %}
            <div class="service-option">
                <strong>{{ thumb.service }}</strong> - {{ thumb.description }} <span class="price">R$ {{ thumb.price }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('profile') }}">Minhas Reservas</a></p>
    </div>
    <script>
        // Bloqueia datas passadas
        const today = new Date();
        const tomorrow = new Date(today);
        tomorrow.setDate(tomorrow.getDate() + 1);
        document.getElementById('date').setAttribute('min', tomorrow.toISOString().split('T')[0]);
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
        .container { max-width: 700px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        p { margin-bottom: 10px; }
        ul { list-style: none; padding: 0; }
        li { padding: 12px; margin: 10px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; }
        li.approved { background: #d4edda; color: #155724; }
        li.denied { background: #f8d7da; color: #721c24; }
        li.pending { background: #fff3cd; color: #856404; }
        li span { flex-grow: 1; }
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
            <li class="{{ res.status }}">
                <span><strong>Serviço:</strong> {{ res.service }} | <strong>Data:</strong> {{ res.date }} | <strong>Status:</strong> {{ res.status.title() }}</span>
                {% if res.denied_reason %} <br><em>Motivo da Rejeição: {{ res.denied_reason }}</em>{% endif %}
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
        .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2, h3 { text-align: center; color: #333; margin-bottom: 20px; }
        ul { list-style: none; padding: 0; }
        li { padding: 12px; margin: 10px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        li.pending { background: #fff3cd; color: #856404; }
        li.approved { background: #d4edda; color: #155724; }
        li.denied { background: #f8d7da; color: #721c24; }
        .actions { display: flex; gap: 5px; margin-top: 5px; }
        button { padding: 6px 12px; border: none; border-radius: 3px; cursor: pointer; font-size: 0.9em; }
        .approve { background: #28a745; color: white; }
        .deny { background: #dc3545; color: white; }
        .demote { background: #ffc107; color: black; }
        input[type="text"] { padding: 5px; border: 1px solid #ddd; border-radius: 3px; width: 120px; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 20px auto; border-radius: 5px; text-align: center; max-width: 800px; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        @media (max-width: 768px) {
            li { flex-direction: column; align-items: flex-start; }
            .actions { margin-top: 10px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>Painel Admin</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <h3>Usuários Cadastrados ({{ users|length }})</h3>
        <ul>
            {% for user in users %}
            <li>
                <span><strong>{{ user.email }}</strong> - Role: {{ user.role }} - Cadastrado: {{ user.data_cadastro }}</span>
                {% if user.role != 'admin' %}
                <span class="actions">
                    <form method="POST" style="display: inline;">
                        <input type="hidden" name="action" value="demote_user">
                        <input type="hidden" name="user_id" value="{{ user.id }}">
                        <button type="submit" class="demote">Rebaixar para User</button>
                    </form>
                </span>
                {% endif %}
            </li>
            {% endfor %}
        </ul>
        <h3>Reservas Pendentes ({{ pending_reservations|length }})</h3>
        <ul>
            {% for res in pending_reservations %}
            <li class="pending">
                <span><strong>ID {{ res.id }}:</strong> {{ res.service }} por {{ res.user_email }} em {{ res.date }}</span>
                <span class="actions">
                    <form method="POST" style="display: inline;">
                        <input type="hidden" name="action" value="approve">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        <button type="submit" class="approve">Aprovar</button>
                    </form>
                    <form method="POST" style="display: inline;">
                        <input type="hidden" name="action" value="deny">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        <input type="text" name="reason" placeholder="Motivo rejeição" required>
                        <button type="submit" class="deny">Rejeitar</button>
                    </form>
                </span>
            </li>
            {% endfor %}
        </ul>
        <h3>Todas as Reservas ({{ all_reservations|length }})</h3>
        <ul>
            {% for res in all_reservations %}
            <li class="{{ res.status }}">
                <span><strong>ID {{ res.id }}:</strong> {{ res.service }} por {{ res.user_email }} em {{ res.date }} (Status: {{ res.status.title() }})</span>
                {% if res.denied_reason %}<span> - Motivo: {{ res.denied_reason }}</span>{% endif %}
            </li>
            {% endfor %}
        </ul>
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('logout') }}">Logout Admin</a></p>
    </div>
</body>
</html>
'''

# Rotas da aplicação
@app.route('/', methods=['GET'])
def index():
    thumbnails = []
    if gc:
        try:
            sheet = gc.open("JG Minis Sheet").sheet1  # Nome da planilha Google - ajuste se necessário
            records = sheet.get_all_records()
            for record in records[:6]:  # Exibe até 6 serviços
                thumbnails.append({
                    'service': record.get('service', 'Serviço Desconhecido'),
                    'description': record.get('description', 'Descrição não disponível'),
                    'thumbnail_url': record.get('thumbnail_url', LOGO_URL),
                    'price': record.get('price', 'Consultar')
                })
            logger.info(f"Carregados {len(thumbnails)} thumbnails do Google Sheets")
        except Exception as e:
            logger.error(f"Erro ao carregar dados do Google Sheets: {e}")
            flash('Não foi possível carregar os serviços. Tente novamente mais tarde.', 'error')
            thumbnails = [{'service': 'Serviço de Exemplo', 'description': 'Serviço em manutenção', 'thumbnail_url': LOGO_URL, 'price': 'R$ 0'}]
    else:
        thumbnails = [{'service': 'Serviço de Exemplo', 'description': 'Configure GOOGLE_SHEETS_CREDENTIALS para ver mais', 'thumbnail_url': LOGO_URL, 'price': 'Consultar'}]
    return render_template_string(INDEX_HTML, logo_url=LOGO_URL, thumbnails=thumbnails, session=session)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        if not is_valid_email(email):
            flash('Email inválido.', 'error')
            return render_template_string(LOGIN_HTML)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT id, email, password, role FROM users WHERE email = ?", (email,))
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
    if 'user_id' in session:
        return redirect(url_for('index'))
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
            user_id = c.lastrowid
            conn.close()
            logger.info(f"Registro bem-sucedido para {email}")
            flash('Registro realizado com sucesso! Verifique seu email.', 'success')
            send_email(email, 'Bem-vindo ao JG MINIS', f'Obrigado por se registrar, {email}! Seu login está pronto.')
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
    
    thumbnails = []  # Reusa do index para opções de serviço
    if gc:
        try:
            sheet = gc.open("JG Minis Sheet").sheet1
            records = sheet.get_all_records()
            for record in records:
                thumbnails.append({
                    'service': record.get('service', ''),
                    'description': record.get('description', ''),
                    'thumbnail_url': record.get('thumbnail_url', LOGO_URL),
                    'price': record.get('price', '')
                })
        except Exception as e:
            logger.error(f"Erro ao carregar thumbnails para reservar: {e}")
            flash('Não foi possível carregar a lista de serviços.', 'error')
    
    tomorrow = (date.today() + timedelta(days=1)).isoformat()  # Data mínima para reserva (amanhã)

    if request.method == 'POST':
        service = request.form['service']
        selected_date_str = request.form['date']
        
        if not service or not selected_date_str:
            flash('Por favor, preencha todos os campos.', 'error')
            return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow)

        try:
            selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
            if selected_date <= date.today():
                flash('A data da reserva deve ser futura.', 'error')
                return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow)
        except ValueError:
            flash('Formato de data inválido.', 'error')
            return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow)

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT INTO reservations (user_id, service, date) VALUES (?, ?, ?)",
                  (session['user_id'], service, selected_date_str))
        res_id = c.lastrowid
        conn.commit()
        conn.close()
        logger.info(f"Reserva criada ID {res_id} por user {session['user_id']} para {service} em {selected_date_str}")
        flash('Reserva realizada com sucesso! Aguarde aprovação.', 'success')
        send_email(session['email'], 'Reserva Recebida', f'Sua reserva para {service} em {selected_date_str} foi enviada para aprovação.')
        return redirect(url_for('profile'))
    return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow)

@app.route('/profile', methods=['GET'])
def profile():
    if 'user_id' not in session:
        flash('Faça login para ver seu perfil.', 'error')
        return redirect(url_for('login'))
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT data_cadastro FROM users WHERE id = ?", (session['user_id'],))
    user_data = c.fetchone()
    data_cadastro = user_data[0] if user_data else 'Desconhecida'
    
    c.execute("""
        SELECT id, service, date, status, denied_reason 
        FROM reservations 
        WHERE user_id = ? 
        ORDER BY created_at DESC
    """, (session['user_id'],))
    reservations = [{'id': r[0], 'service': r[1], 'date': r[2], 'status': r[3], 'denied_reason': r[4]} for r in c.fetchall()]
    conn.close()
    return render_template_string(PROFILE_HTML, data_cadastro=data_cadastro, reservations=reservations, session=session)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado. Apenas para administradores.', 'error')
        return redirect(url_for('index'))
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # Handle POST actions (approve/deny/demote)
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'approve':
            res_id = request.form.get('res_id')
            if res_id:
                c.execute("UPDATE reservations SET status = 'approved', approved_by = ? WHERE id = ?", (session['user_id'], res_id))
                conn.commit()
                flash('Reserva aprovada.', 'success')
                logger.info(f"Admin {session['email']} aprovou reserva {res_id}")
                # Envia email ao user
                c.execute("SELECT u.email, r.service, r.date FROM reservations r JOIN users u ON r.user_id = u.id WHERE r.id = ?", (res_id,))
                user_email, service, res_date = c.fetchone()
                send_email(user_email, 'Sua Reserva Foi Aprovada!', f'Sua reserva para {service} em {res_date} foi aprovada! Aguardamos você.')
            else:
                flash('ID da reserva inválido.', 'error')
        
        elif action == 'deny':
            res_id = request.form.get('res_id')
            reason = request.form.get('reason', 'Motivo não especificado')
            if res_id:
                c.execute("UPDATE reservations SET status = 'denied', denied_reason = ? WHERE id = ?", (reason, res_id))
                conn.commit()
                flash('Reserva rejeitada.', 'success')
                logger.info(f"Admin {session['email']} rejeitou reserva {res_id}: {reason}")
                # Envia email ao user
                c.execute("SELECT u.email, r.service, r.date FROM reservations r JOIN users u ON r.user_id = u.id WHERE r.id = ?", (res_id,))
                user_email, service, res_date = c.fetchone()
                send_email(user_email, 'Sua Reserva Foi Rejeitada', f'Sua reserva para {service} em {res_date} foi rejeitada. Motivo: {reason}.')
            else:
                flash('ID da reserva inválido.', 'error')
        
        elif action == 'demote_user':
            user_id_to_demote = request.form.get('user_id')
            if user_id_to_demote and int(user_id_to_demote) != session['user_id']: # Admin não pode rebaixar a si mesmo
                c.execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id_to_demote,))
                conn.commit()
                flash(f'Usuário {user_id_to_demote} rebaixado para user.', 'success')
                logger.info(f"Admin {session['email']} rebaixou user {user_id_to_demote}")
            else:
                flash('Não é possível rebaixar este usuário.', 'error')
        
        return redirect(url_for('admin')) # Redireciona para evitar reenvio de formulário

    # Fetch data for GET request
    c.execute("SELECT id, email, role, data_cadastro FROM users ORDER BY data_cadastro DESC")
    users_data = [{'id': u[0], 'email': u[1], 'role': u[2], 'data_cadastro': u[3]} for u in c.fetchall()]
    
    c.execute("""
        SELECT r.id, r.service, r.date, r.user_id, u.email as user_email 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        WHERE r.status = 'pending' 
        ORDER BY r.created_at DESC
    """)
    pending_reservations_data = [{'id': r[0], 'service': r[1], 'date': r[2], 'user_id': r[3], 'user_email': r[4]} for r in c.fetchall()]
    
    c.execute("""
        SELECT r.id, r.service, r.date, r.status, r.denied_reason, u.email as user_email 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        ORDER BY r.created_at DESC
    """)
    all_reservations_data = [{'id': r[0], 'service': r[1], 'date': r[2], 'status': r[3], 'denied_reason': r[4], 'user_email': r[5]} for r in c.fetchall()]
    
    conn.close()
    return render_template_string(ADMIN_HTML, users=users_data, pending_reservations=pending_reservations_data, all_reservations=all_reservations_data, session=session)

@app.route('/logout', methods=['GET'])
def logout():
    if 'user_id' in session:
        logger.info(f"Logout de {session['email']}")
    session.clear()
    flash('Logout realizado com sucesso!', 'success')
    return redirect(url_for('index'))

# Tratadores de erro
@app.errorhandler(404)
def not_found_error(error):
    logger.warning(f"404 Not Found: {request.path}")
    return render_template_string('''
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head><meta charset="UTF-8"><title>404 Não Encontrado</title>
        <style>body { font-family: Arial; text-align: center; margin-top: 50px; }</style></head>
        <body><h1>404 - Página Não Encontrada</h1><p>A página que você procura não existe.</p><p><a href="/">Voltar ao Home</a></p></body>
        </html>
    '''), 404

@app.errorhandler(500)
def internal_error(error):
    logger.exception(f"Erro interno 500: {error}") # Loga a exceção completa
    return render_template_string('''
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head><meta charset="UTF-8"><title>500 Erro Interno</title>
        <style>body { font-family: Arial; text-align: center; margin-top: 50px; }</style></head>
        <body><h1>500 - Erro Interno do Servidor</h1><p>Algo deu errado. Por favor, tente novamente mais tarde.</p><p><a href="/">Voltar ao Home</a></p></body>
        </html>
    '''), 500

# Para produção no Railway (Gunicorn usa isso)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = '0.0.0.0'
    logger.info(f"Iniciando Flask app em {host}:{port}")
    app.run(host=host, port=port, debug=False)
