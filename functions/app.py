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

# Fix para vars de ambiente
LOGO_URL = os.environ.get('LOGO_URL', 'https://via.placeholder.com/150x50?text=JG+MINIS')  # Default logo
GOOGLE_SHEETS_CREDENTIALS = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')  # JSON string para gspread
SECRET_KEY = os.environ.get('SECRET_KEY', 'jgminis_v4_secret_2025')  # Default para sessions
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')  # SQLite path

# Init Flask e Bcrypt
app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)

# Gspread setup
if GOOGLE_SHEETS_CREDENTIALS:
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_SHEETS_CREDENTIALS))
    gc = gspread.authorize(creds)
else:
    gc = None  # Fallback sem Sheets

# DB setup
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT, role TEXT DEFAULT 'user')''')
    c.execute('''CREATE TABLE IF NOT EXISTS reservations
                 (id INTEGER PRIMARY KEY, user_id INTEGER, service TEXT, date TEXT, status TEXT DEFAULT 'pending')''')
    # Admin user se não existir
    c.execute("SELECT * FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (email, password, role) VALUES ('admin@jgminis.com.br', ?, 'admin')", (hashed,))
    conn.commit()
    conn.close()

init_db()

# Templates HTML inline (para Pages/Railway sem arquivos externos)
INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>JG MINIS v4.2</title>
    <style>body { font-family: Arial; text-align: center; } img { width: 150px; }</style>
</head>
<body>
    <h1>Bem-vindo ao JG MINIS v4.2</h1>
    <img src="{{ logo_url }}" alt="Logo">
    <p>Miniauras de serviços: {{ thumbnails }}</p>
    <a href="/login">Login</a> | <a href="/register">Registrar</a> | <a href="/admin">Admin</a> | <a href="/reservar">Reservar</a>
</body>
</html>
'''

LOGIN_HTML = '''
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
    <h2>Login</h2>
    <form method="POST">
        Email: <input type="email" name="email" required><br>
        Senha: <input type="password" name="password" required><br>
        <input type="submit" value="Entrar">
    </form>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <ul>
                {% for message in messages %}
                    <li>{{ message }}</li>
                {% endfor %}
            </ul>
        {% endif %}
    {% endwith %}
</body>
</html>
'''

# Rotas
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def catch_all(path):
    if request.method == 'POST':
        # Handle forms (login, register, reservar)
        if path == 'login':
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
                return redirect(url_for('index'))
            else:
                flash('Credenciais inválidas')
                return render_template_string(LOGIN_HTML)
        elif path == 'register':
            # Lógica register (hash password, insert DB)
            email = request.form['email']
            password = bcrypt.generate_password_hash(request.form['password']).decode('utf-8')
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            try:
                c.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, password))
                conn.commit()
                flash('Usuário registrado')
            except sqlite3.IntegrityError:
                flash('Email já existe')
            conn.close()
            return render_template_string(LOGIN_HTML)  # Reuse for register form
        elif path == 'reservar':
            if 'user_id' in session:
                service = request.form['service']
                date = request.form['date']
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("INSERT INTO reservations (user_id, service, date) VALUES (?, ?, ?)", (session['user_id'], service, date))
                conn.commit()
                conn.close()
                flash('Reserva feita')
                # Email notification
                send_email(session['user_id'], 'Reserva confirmada', f'Serviço: {service} em {date}')
            else:
                flash('Faça login para reservar')
            return redirect(url_for('index'))
        elif path == 'admin':
            if session.get('role') == 'admin':
                # Lógica admin (list users/reservations)
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("SELECT * FROM reservations")
                reservations = c.fetchall()
                conn.close()
                return f"<h2>Admin - Reservas:</h2><ul>{''.join([f'<li>ID {r[0]}: {r[2]} em {r[3]}</li>' for r in reservations])}</ul>"
            else:
                flash('Acesso negado')
                return redirect(url_for('index'))
    else:
        # GET requests
        thumbnails = []
        if gc:
            try:
                sheet = gc.open("JG Minis Sheet").sheet1  # Nome da planilha
                thumbnails = sheet.get_all_records()[:5]  # Primeiras 5 miniaturas
            except:
                thumbnails = [{'service': 'Serviço 1', 'thumbnail': LOGO_URL}]  # Fallback
        else:
            thumbnails = [{'service': 'Serviço Default', 'thumbnail': LOGO_URL}]
        thumbnails_str = ', '.join([t['service'] for t in thumbnails])
        return render_template_string(INDEX_HTML, logo_url=LOGO_URL, thumbnails=thumbnails_str)

def send_email(user_id, subject, body):
    # Lógica email (SMTP, ex: Gmail)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE id = ?", (user_id,))
    email = c.fetchone()[0]
    conn.close()
    # Exemplo SMTP (use vars env para SMTP_SERVER, USER, PASS)
    smtp_server = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('SMTP_PORT', 587))
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    if smtp_user and smtp_pass:
        msg = MimeMultipart()
        msg['From'] = smtp_user
        msg['To'] = email
        msg['Subject'] = subject
        msg.attach(MimeText(body, 'plain'))
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, email, msg.as_string())
        server.quit()

# Handler para Cloudflare Workers/Pages
async def handler(request):
    # Simula Flask request
    from flask import request as flask_request
    flask_request.environ = request.env
    flask_request.method = request.method
    flask_request.path = request.path
    flask_request.args = request.params
    # Chama app
    with app.test_request_context(path=request.path, method=request.method):
        return app.test_client().get(request.path)

# Para Railway (use app.run sem handler)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
