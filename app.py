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

# Configurações de logging para Railway (debug em produção)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configurações de ambiente (com fallbacks para desenvolvimento local)
LOGO_URL = os.environ.get('LOGO_URL', 'https://via.placeholder.com/150x50?text=JG+MINIS+Logo')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')  # JSON string minificado
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025_dev_key_fallback')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')  # SQLite path para Railway/Heroku

# Inicializa Flask e Bcrypt
app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# Configuração gspread com auth moderna (google-auth)
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

# Função para validar email com regex
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

# Inicialização do banco SQLite
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
                  status TEXT DEFAULT 'pending',
                  approved_by INTEGER,  -- ID admin que aprovou
                  denied_reason TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id),
                  FOREIGN KEY (approved_by) REFERENCES users (id))''')
    # Admin user default
    c.execute("SELECT id FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (email, password, role) VALUES ('admin@jgminis.com.br', ?, 'admin')", (hashed_password,))
        logger.info("Usuário admin criado no DB")
    conn.commit()
    conn.close()

init_db()

# Função para carregar thumbnails da planilha "BASE DE DADOS JG"
def load_thumbnails():
    thumbnails = []
    if gc:
        try:
            sheet = gc.open("BASE DE DADOS JG").sheet1  # Nome da planilha
            records = sheet.get_all_records()  # Pega todas as linhas como dict
            for record in records:  # Pega todos os itens da planilha
                # Mapeamento das colunas da planilha para o formato do app
                service = record.get('NOME DA MINIATURA', 'Miniatura Desconhecida')
                
                # Combina MARCA/FABRICANTE e OBSERVAÇÕES para a descrição
                marca_fabricante = record.get('MARCA/FABRICANTE', '').strip()
                observacoes = record.get('OBSERVAÇÕES', '').strip()
                description_parts = [part for part in [marca_fabricante, observacoes] if part]
                description = " - ".join(description_parts) if description_parts else 'Descrição não disponível'
                
                thumbnail_url = record.get('IMAGEM', LOGO_URL)  # URL da imagem
                
                # Formata o preço
                price_raw = str(record.get('VALOR', '')).strip()
                price = price_raw.replace('R$', '').replace(',', '.').strip() if price_raw else 'Consultar'
                
                thumbnails.append({
                    'service': service,
                    'description': description,
                    'thumbnail_url': thumbnail_url,
                    'price': price
                })
            logger.info(f"Carregados {len(thumbnails)} thumbnails da planilha BASE DE DADOS JG")
        except Exception as e:
            logger.error(f"Erro ao carregar planilha: {e}")
            # Fallback se houver erro no Sheets
            thumbnails = [{'service': 'Fallback', 'description': 'Serviço em manutenção', 'thumbnail_url': LOGO_URL, 'price': '0,00'}]
    else:
        # Fallback se GOOGLE_SHEETS_CREDENTIALS não estiver configurado
        thumbnails = [{'service': 'Sem Integração', 'description': 'Configure Sheets para mais detalhes', 'thumbnail_url': LOGO_URL, 'price': '0,00'}]
    return thumbnails

# --- Templates HTML Inline (com CSS responsivo) ---

INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS v4.2 - Serviços</title>
    <style>
        body { font-family: 'Arial', sans-serif; margin: 0; padding: 20px; background: #f8f9fa; color: #333; }
        header { text-align: center; padding: 20px; background: #007bff; color: white; }
        img.logo { max-width: 150px; height: auto; margin: 10px; }
        .thumbnails { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); 
            gap: 20px; 
            padding: 20px; 
            max-width: 1200px; /* Limita largura para PC */
            margin: 0 auto; /* Centraliza */
        }
        .thumbnail { 
            background: white; 
            border-radius: 10px; 
            box-shadow: 0 4px 8px rgba(0,0,0,0.1); 
            padding: 15px; 
            text-align: center; 
            transition: transform 0.2s; 
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }
        .thumbnail:hover { transform: scale(1.05); }
        .thumbnail img { 
            width: 100%; 
            height: 150px; 
            object-fit: cover; 
            border-radius: 8px; 
            margin-bottom: 10px;
        }
        .thumbnail h3 { margin: 10px 0; color: #007bff; }
        .thumbnail p { margin: 5px 0; flex-grow: 1; }
        nav { text-align: center; padding: 10px; background: #e9ecef; }
        nav a { margin: 0 15px; color: #007bff; text-decoration: none; font-weight: bold; }
        nav a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 10px; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        footer { text-align: center; padding: 10px; background: #343a40; color: white; margin-top: 40px; }
        @media (max-width: 600px) { 
            .thumbnails { grid-template-columns: 1fr; padding: 10px; } 
            body { padding: 0; }
        }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo JG MINIS" class="logo">
        <h1>Bem-vindo ao JG MINIS v4.2</h1>
    </header>
    <nav>
        <a href="{{ url_for('index') }}">Home</a>
        {% if not session.user_id %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Registrar</a>
        {% endif %}
        {% if session.user_id %}
            <a href="{{ url_for('reservar') }}">Reservar Serviço</a>
            {% if session.role == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
            <a href="{{ url_for('profile') }}">Meu Perfil</a>
            <a href="{{ url_for('logout') }}">Logout</a>
        {% endif %}
    </nav>
    <main class="thumbnails">
        {% for thumb in thumbnails %}
        <div class="thumbnail">
            <img src="{{ thumb.thumbnail_url or logo_url }}" alt="{{ thumb.service }}">
            <h3>{{ thumb.service }}</h3>
            <p>{{ thumb.description or 'Descrição disponível' }}</p>
            <p>Preço: R$ {{ thumb.price or 'Consultar' }}</p>
            <a href="{{ url_for('reservar') }}" style="color: #28a745; font-weight: bold;">Reservar Agora</a>
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
    <title>Reservar Serviço - JG MINIS v4.2</title>
    <style>
        body { font-family: Arial; background: #f8f9fa; padding: 20px; }
        .container { max-width: 500px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; }
        form { display: flex; flex-direction: column; }
        label { margin: 10px 0 5px; font-weight: bold; }
        input, select { padding: 10px; border: 1px solid #ddd; border-radius: 5px; margin-bottom: 15px; }
        button { padding: 12px; background: #ffc107; color: black; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; font-weight: bold; }
        button:hover { background: #e0a800; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .services-list { margin: 20px 0; }
        .service-option { padding: 10px; background: #e9ecef; margin: 5px 0; border-radius: 5px; cursor: pointer; }
        .service-option:hover { background: #dee2e6; }
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
            <label for="service">Miniatura:</label>
            <select name="service" id="service" required>
                <option value="">Selecione uma miniatura</option>
                {% for thumb in thumbnails %}
                <option value="{{ thumb.service }}">{{ thumb.service }} - R$ {{ thumb.price }}</option>
                {% endfor %}
            </select>
            <label for="date">Data (apenas datas futuras):</label>
            <input type="date" name="date" id="date" required min="{{ tomorrow }}">
            <button type="submit">Confirmar Reserva</button>
        </form>
        <div class="services-list">
            <h3>Miniaturas Disponíveis:</h3>
            {% for thumb in thumbnails %}
            <div class="service-option">
                <strong>{{ thumb.service }}</strong> - {{ thumb.description }} - R$ {{ thumb.price }}
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                <div class="flash flash-{{ 'success' if category == 'success' else 'error' }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <p><a href="{{ url_for('index') }}">Voltar ao Home</a> | <a href="{{ url_for('profile') }}">Minhas Reservas</a></p>
    </div>
    <script>
        const today = new Date().toISOString().split('T')[0];
        document.getElementById('date').setAttribute('min', today);
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
        .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        h2, h3 { text-align: center; color: #333; }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; background: #e9ecef; margin: 10px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; }
        li.pending { background: #fff3cd; }
        li.approved { background: #d4edda; }
        li.denied { background: #f8d7da; }
        .actions { margin-left: 10px; }
        button { padding: 5px 10px; margin: 0 5px; border: none; border-radius: 3px; cursor: pointer; }
        .approve { background: #28a745; color: white; }
        .deny { background: #dc3545; color: white; }
        input[type="text"] { width: 100px; padding: 2px; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; }
        .flash-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .flash-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Painel Admin</h2>
        <h3>Usuários Cadastrados ({{ users|length }})</h3>
        <ul>
            {% for user in users %}
            <li>
                <span>{{ user.email }} - Role: {{ user.role }} - Cadastrado: {{ user.data_cadastro }}</span>
                {% if user.role != 'admin' %}
                <span class="actions">
                    <a href="?demote_{{ user.id }}" onclick="return confirm('Rebaixar user?')">Demote</a>
                </span>
                {% endif %}
            </li>
            {% endfor %}
        </ul>
        <h3>Reservas Pendentes ({{ pending_reservations|length }})</h3>
        <ul>
            {% for res in pending_reservations %}
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
                </span>
            </li>
            {% endfor %}
        </ul>
        <h3>Todas as Reservas</h3>
        <ul>
            {% for res in all_reservations %}
            <li class="{{ res.status }}">
                <span>ID {{ res.id }}: {{ res.service }} por {{ res.user_email }} em {{ res.date }} (Status: {{ res.status.title() }})</span>
                {% if res.denied_reason %}<span> - Motivo: {{ res.denied_reason }}</span>{% endif %}
            </li>
            {% endfor %}
        </ul>
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

@app.route('/', methods=['GET'])
def index():
    thumbnails = load_thumbnails()
    return render_template_string(INDEX_HTML, logo_url=LOGO_URL, thumbnails=thumbnails)

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
    thumbnails = load_thumbnails()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if request.method == 'POST':
        service = request.form['service']
        selected_date = request.form['date']
        if selected_date <= date.today().isoformat():
            flash('Data deve ser futura.', 'error')
            return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT INTO reservations (user_id, service, date) VALUES (?, ?, ?)",
                  (session['user_id'], service, selected_date))
        res_id = c.lastrowid
        conn.commit()
        conn.close()
        logger.info(f"Reserva criada ID {res_id} por user {session['user_id']}")
        flash('Reserva realizada! Aguarde aprovação.', 'success')
        return redirect(url_for('profile'))
    return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, tomorrow=tomorrow)

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
    c.execute("SELECT id, email, role, data_cadastro FROM users ORDER BY data_cadastro DESC")
    users = c.fetchall()
    c.execute("""
        SELECT r.id, r.service, r.date, r.user_id, u.email as user_email 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        WHERE r.status = 'pending' 
        ORDER BY r.created_at DESC
    """)
    pending_reservations = c.fetchall()
    c.execute("""
        SELECT r.id, r.service, r.date, r.status, r.denied_reason, u.email as user_email 
        FROM reservations r 
        JOIN users u ON r.user_id = u.id 
        ORDER BY r.created_at DESC
    """)
    all_reservations = c.fetchall()
    if request.method == 'POST':
        action = request.form.get('action')
        res_id = request.form.get('res_id')
        if action == 'approve' and res_id:
            c.execute("UPDATE reservations SET status = 'approved', approved_by = ? WHERE id = ?", (session['user_id'], res_id))
            conn.commit()
            flash('Reserva aprovada.', 'success')
            logger.info(f"Admin {session['email']} aprovou reserva {res_id}")
        elif action == 'deny' and res_id:
            reason = request.form.get('reason', 'Motivo não especificado')
            c.execute("UPDATE reservations SET status = 'denied', denied_reason = ? WHERE id = ?", (reason, res_id))
            conn.commit()
            flash('Reserva rejeitada.', 'success')
            logger.info(f"Admin {session['email']} rejeitou reserva {res_id}: {reason}")
        elif 'demote_' in request.args:
            user_id_to_demote = int(request.args['demote_'][7:])
            if user_id_to_demote != session['user_id']:
                c.execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id_to_demote,))
                conn.commit()
                flash(f'Usuário rebaixado para user.', 'success')
    conn.close()
    return render_template_string(ADMIN_HTML, users=users, pending_reservations=pending_reservations, all_reservations=all_reservations)

@app.route('/logout', methods=['GET'])
def logout():
    if 'user_id' in session:
        logger.info(f"Logout de {session['email']}")
    session.clear()
    flash('Logout realizado com sucesso!', 'success')
    return redirect(url_for('index'))

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
