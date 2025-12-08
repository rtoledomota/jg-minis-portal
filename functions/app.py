import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, jsonify
from flask_bcrypt import Bcrypt
import gspread
from google.oauth2.service_account import Credentials
import smtplib
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart

# Configurações de ambiente
LOGO_URL = os.environ.get('LOGO_URL', 'https://via.placeholder.com/150x50?text=JG+MINIS')
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')  # JSON string minificado
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025')
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')  # SQLite path para Railway

# Inicializa Flask e Bcrypt
app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# Configuração gspread com auth moderna (sem oauth2client)
gc = None
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        print("gspread auth sucesso")  # Log para debug
    except Exception as e:
        print(f"Erro na auth gspread: {e}")  # Fallback sem Sheets
        gc = None
else:
    print("GOOGLE_SHEETS_CREDENTIALS ausente - usando fallback")  # Log
    gc = None

# Inicialização do banco SQLite
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Tabela users
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  role TEXT DEFAULT 'user')''')
    # Tabela reservations
    c.execute('''CREATE TABLE IF NOT EXISTS reservations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  service TEXT,
                  date TEXT,
                  status TEXT DEFAULT 'pending',
                  FOREIGN KEY (user_id) REFERENCES users (id))''')
    # Admin user default se não existir
    c.execute("SELECT * FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (email, password, role) VALUES ('admin@jgminis.com.br', ?, 'admin')", (hashed_password,))
        print("Admin user criado")  # Log
    conn.commit()
    conn.close()

init_db()

# Templates HTML inline (simples, para demo - expanda se precisar)
INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>JG MINIS v4.2</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; margin: 20px; background: #f4f4f4; }
        h1 { color: #333; }
        img { width: 150px; height: 50px; margin: 10px; }
        .thumbnails { display: flex; justify-content: center; flex-wrap: wrap; }
        .thumbnail { margin: 10px; padding: 10px; background: white; border-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        a { margin: 0 10px; color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .flash { color: red; }
    </style>
</head>
<body>
    <h1>Bem-vindo ao JG MINIS v4.2</h1>
    <img src="{{ logo_url }}" alt="Logo JG MINIS">
    <div class="thumbnails">
        {% for thumb in thumbnails %}
        <div class="thumbnail">
            <h3>{{ thumb.service }}</h3>
            <img src="{{ thumb.thumbnail }}" alt="{{ thumb.service }}" style="width: 100px; height: 100px;">
        </div>
        {% endfor %}
    </div>
    <p><a href="/login">Login</a> | <a href="/register">Registrar</a> | <a href="/admin">Admin</a> | <a href="/reservar">Reservar Serviço</a></p>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <div class="flash">
                {% for message in messages %}
                    <p>{{ message }}</p>
                {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
</body>
</html>
'''

LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Login - JG MINIS</title>
    <style>body { font-family: Arial; text-align: center; margin: 50px; }</style>
</head>
<body>
    <h2>Login</h2>
    <form method="POST">
        <p>Email: <input type="email" name="email" required style="padding: 5px;"></p>
        <p>Senha: <input type="password" name="password" required style="padding: 5px;"></p>
        <p><input type="submit" value="Entrar" style="padding: 10px; background: #007bff; color: white; border: none;"></p>
    </form>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <div style="color: red;">
                {% for message in messages %}
                    <p>{{ message }}</p>
                {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    <p><a href="/">Voltar ao Home</a></p>
</body>
</html>
'''

REGISTER_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Registrar - JG MINIS</title>
    <style>body { font-family: Arial; text-align: center; margin: 50px; }</style>
</head>
<body>
    <h2>Registrar Usuário</h2>
    <form method="POST">
        <p>Email: <input type="email" name="email" required style="padding: 5px;"></p>
        <p>Senha: <input type="password" name="password" required style="padding: 5px;"></p>
        <p><input type="submit" value="Registrar" style="padding: 10px; background: #28a745; color: white; border: none;"></p>
    </form>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <div style="color: red;">
                {% for message in messages %}
                    <p>{{ message }}</p>
                {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    <p><a href="/login">Já tem conta? Login</a> | <a href="/">Home</a></p>
</body>
</html>
'''

RESERVAR_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Reservar - JG MINIS</title>
    <style>body { font-family: Arial; text-align: center; margin: 50px; }</style>
</head>
<body>
    <h2>Reservar Serviço</h2>
    {% if 'user_id' in session %}
    <form method="POST">
        <p>Serviço: <input type="text" name="service" required style="padding: 5px;" placeholder="Ex: Corte de Cabelo"></p>
        <p>Data: <input type="date" name="date" required style="padding: 5px;"></p>
        <p><input type="submit" value="Reservar" style="padding: 10px; background: #ffc107; color: black; border: none;"></p>
    </form>
    {% else %}
    <p>Faça <a href="/login">login</a> para reservar.</p>
    {% endif %}
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <div style="color: green;">
                {% for message in messages %}
                    <p>{{ message }}</p>
                {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    <p><a href="/">Voltar ao Home</a></p>
</body>
</html>
'''

ADMIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Admin - JG MINIS</title>
    <style>body { font-family: Arial; text-align: center; margin: 20px; } ul { list-style: none; }</style>
</head>
<body>
    <h2>Painel Admin</h2>
    <h3>Usuários Cadastrados</h3>
    <ul>
        {% for user in users %}
        <li>{{ user.email }} - Role: {{ user.role }}</li>
        {% endfor %}
    </ul>
    <h3>Reservas Pendentes</h3>
    <ul>
        {% for res in reservations %}
        <li>ID {{ res.id }}: {{ res.service }} em {{ res.date }} (Status: {{ res.status }})</li>
        {% endfor %}
    </ul>
    <p><a href="/">Sair para Home</a></p>
</body>
</html>
'''

# Função para enviar email (opcional, use vars env para SMTP)
def send_email(user_id, subject, body):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        user_email = result[0]
        smtp_server = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
        smtp_port = int(os.environ.get('SMTP_PORT', 587))
        smtp_user = os.environ.get('SMTP_USER')
        smtp_pass = os.environ.get('SMTP_PASS')
        if smtp_user and smtp_pass:
            msg = MimeMultipart()
            msg['From'] = smtp_user
            msg['To'] = user_email
            msg['Subject'] = subject
            msg.attach(MimeText(body, 'plain'))
            try:
                server = smtplib.SMTP(smtp_server, smtp_port)
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, user_email, msg.as_string())
                server.quit()
                print(f"Email enviado para {user_email}")  # Log
            except Exception as e:
                print(f"Erro email: {e}")

# Rotas
@app.route('/', methods=['GET'])
def index():
    thumbnails = []
    if gc:
        try:
            sheet = gc.open("JG Minis Sheet").sheet1  # Nome da planilha Google - ajuste se necessário
            records = sheet.get_all_records()
            for record in records[:5]:  # Primeiras 5 miniaturas
                thumbnails.append({
                    'service': record.get('service', 'Serviço Desconhecido'),
                    'thumbnail': record.get('thumbnail', LOGO_URL)
                })
        except Exception as e:
            print(f"Erro Sheets: {e}")
            thumbnails = [{'service': 'Fallback Serviço', 'thumbnail': LOGO_URL}]
    else:
        thumbnails = [{'service': 'Sem Integração Sheets', 'thumbnail': LOGO_URL}]
    return render_template_string(INDEX_HTML, logo_url=LOGO_URL, thumbnails=thumbnails)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        if user and bcrypt.check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['role'] = user[3]
            session['email'] = user[1]
            flash('Login realizado com sucesso!')
            return redirect(url_for('index'))
        else:
            flash('Email ou senha inválidos.')
    return render_template_string(LOGIN_HTML)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed_password))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email já cadastrado.')
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        flash('Faça login para reservar.')
        return redirect(url_for('login'))
    if request.method == 'POST':
        service = request.form['service']
        date = request.form['date']
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT INTO reservations (user_id, service, date) VALUES (?, ?, ?)",
                  (session['user_id'], service, date))
        conn.commit()
        conn.close()
        flash('Reserva realizada com sucesso!')
        send_email(session['user_id'], 'Reserva Confirmada', f'Serviço: {service} em {date}')
        return redirect(url_for('index'))
    return render_template_string(RESERVAR_HTML)

@app.route('/admin', methods=['GET'])
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado. Apenas admin.')
        return redirect(url_for('index'))
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT id, email, role FROM users")
    users = c.fetchall()
    c.execute("SELECT * FROM reservations")
    reservations = c.fetchall()
    conn.close()
    return render_template_string(ADMIN_HTML, users=users, reservations=reservations)

@app.route('/logout', methods=['GET'])
def logout():
    session.clear()
    flash('Logout realizado.')
    return redirect(url_for('index'))

# Para produção no Railway (Gunicorn usa isso)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
