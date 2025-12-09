import os
import json
import csv
import sqlite3
import bcrypt
import gspread
from flask import Flask, request, session, redirect, url_for, flash, render_template_string, send_file, make_response
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from io import StringIO, BytesIO
import logging

# Configuração do logging
logging.basicConfig(level=logging.INFO)

# Variáveis de ambiente
LOGO_URL = os.getenv('LOGO_URL', 'https://example.com/logo.png')
GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS', 'credentials.json')
WHATSAPP_NUMBER = os.getenv('WHATSAPP_NUMBER', '5511999999999')
SECRET_KEY = os.getenv('SECRET_KEY', 'supersecretkey')
DATABASE = os.getenv('DATABASE', 'jgminis.db')
PORT = int(os.getenv('PORT', 5000))

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Funções de validação
def is_valid_email(email):
    import re
    return re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email)

def is_valid_phone(phone):
    import re
    return re.match(r'^\d{10,11}$', phone)

# Conexão com o banco de dados
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# Inicialização do banco de dados
def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            approved_by INTEGER,
            denied_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (approved_by) REFERENCES users (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT UNIQUE NOT NULL,
            quantity INTEGER DEFAULT 0,
            last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS waiting_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, service)
        )
    ''')
    # Criar usuário admin se não existir
    cursor.execute('SELECT * FROM users WHERE email = ?', ('admin@jgminis.com.br',))
    if not cursor.fetchone():
        hashed = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt())
        cursor.execute('INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)', 
                       ('Admin', 'admin@jgminis.com.br', '00000000000', hashed.decode('utf-8'), 'admin'))
    # Sincronizar estoque inicial se vazio
    cursor.execute('SELECT COUNT(*) FROM stock')
    if cursor.fetchone()[0] == 0:
        sync_stock_from_sheet()
    conn.commit()
    conn.close()

# Sincronização do estoque com Google Sheets
def sync_stock_from_sheet():
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS, scope)
        client = gspread.authorize(creds)
        sheet = client.open('BASE DE DADOS JG').sheet1
        records = sheet.get_all_records()
        conn = get_db()
        cursor = conn.cursor()
        for record in records:
            service = record.get('NOME DA MINIATURA', '').strip().lower()
            if service:
                quantity = int(record.get('QUANTIDADE DISPONIVEL', 0))
                cursor.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', 
                               (service, quantity))
        conn.commit()
        conn.close()
        logging.info('Estoque sincronizado com sucesso.')
    except Exception as e:
        logging.error(f'Erro ao sincronizar estoque: {e}')

# Carregar thumbnails
def load_thumbnails():
    thumbnails = []
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS, scope)
        client = gspread.authorize(creds)
        sheet = client.open('BASE DE DADOS JG').sheet1
        records = sheet.get_all_records()
        conn = get_db()
        cursor = conn.cursor()
        for record in records:
            service = record.get('NOME DA MINIATURA', '').strip().lower()
            if service:
                cursor.execute('SELECT quantity FROM stock WHERE service = ?', (service,))
                stock_row = cursor.fetchone()
                quantity = stock_row['quantity'] if stock_row else 0
                logging.info(f'Lookup para {service}: quantidade {quantity}')
                thumbnails.append({
                    'service': record.get('NOME DA MINIATURA', ''),
                    'marca': record.get('MARCA/FABRICANTE', ''),
                    'obs': record.get('OBSERVAÇÕES', ''),
                    'image': record.get('IMAGEM', ''),
                    'price': record.get('VALOR', 0),
                    'quantity': quantity,
                    'previsao': record.get('PREVISÃO DE CHEGADA', '')
                })
        conn.close()
    except Exception as e:
        logging.error(f'Erro ao carregar thumbnails: {e}')
        # Fallback: thumbnails vazios
    return thumbnails

# Templates como strings Jinja2
INDEX_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>JG MINIS v4.2</title>
    <style>
        body { font-family: Arial, sans-serif; }
        .thumbnail { border: 1px solid #ccc; padding: 10px; margin: 10px; display: inline-block; width: 200px; }
        .esgotado { filter: grayscale(100%); }
        .esgotado .tag { background: red; color: white; padding: 5px; }
        .action-buttons { display: flex; gap: 10px; }
    </style>
</head>
<body>
    <img src="{{ logo_url }}" alt="Logo">
    {% if session.user_id %}
        <a href="/profile">Perfil</a> | <a href="/reservar">Reservar</a> | <a href="/logout">Logout</a>
        {% if session.role == 'admin' %}<a href="/admin">Admin</a>{% endif %}
    {% else %}
        <a href="/register">Registrar</a> | <a href="/login">Login</a>
    {% endif %}
    <h1>JG MINIS - Miniaturas</h1>
    <div class="grid">
        {% for thumb in thumbnails %}
            <div class="thumbnail {% if thumb.quantity == 0 %}esgotado{% endif %}">
                <img src="{{ thumb.image }}" alt="{{ thumb.service }}">
                <h3>{{ thumb.service }}</h3>
                <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
                <p>R$ {{ thumb.price }}</p>
                <p>Quantidade: {{ thumb.quantity }}</p>
                {% if thumb.quantity == 0 %}
                    <div class="tag">ESGOTADO</div>
                    <div class="action-buttons">
                        <a href="/add_waiting_list/{{ thumb.service }}">Fila de Espera</a>
                        <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.email or 'anônimo' }}">Entrar em Contato</a>
                    </div>
                {% else %}
                    <a href="/reserve_single/{{ thumb.service }}">Reservar Agora</a>
                {% endif %}
            </div>
        {% endfor %}
    </div>
</body>
</html>
'''

REGISTER_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Registrar</title>
</head>
<body>
    <h1>Registrar</h1>
    <form method="post">
        Nome: <input type="text" name="name" required><br>
        Email: <input type="email" name="email" required><br>
        Telefone: <input type="text" name="phone" required><br>
        Senha: <input type="password" name="password" required minlength="6"><br>
        <input type="submit" value="Registrar">
    </form>
    <a href="/">Voltar</a>
</body>
</html>
'''

LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Login</title>
</head>
<body>
    <h1>Login</h1>
    <form method="post">
        Email: <input type="email" name="email" required><br>
        Senha: <input type="password" name="password" required><br>
        <input type="submit" value="Login">
    </form>
    <a href="/">Voltar</a>
</body>
</html>
'''

RESERVAR_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Reservar</title>
</head>
<body>
    <h1>Reservar Miniaturas</h1>
    <form method="post">
        Disponíveis: <input type="checkbox" name="available" checked><br>
        Ordem por data: <input type="checkbox" name="order_by_date"><br>
        Busca previsão: <input type="text" name="previsao_search"><br>
        Busca marca: <input type="text" name="marca_search"><br>
        {% for thumb in thumbnails %}
            {% if not available or thumb.quantity > 0 %}
                <input type="checkbox" name="services" value="{{ thumb.service }}"> {{ thumb.service }} - Quantidade: <input type="number" name="quantity_{{ thumb.service }}" min="1" max="{{ thumb.quantity }}" value="1"><br>
            {% endif %}
        {% endfor %}
        <input type="submit" value="Reservar">
    </form>
    <a href="/">Voltar</a>
</body>
</html>
'''

RESERVE_SINGLE_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Reservar {{ service }}</title>
</head>
<body>
    <h1>Reservar {{ service }}</h1>
    <form method="post">
        Quantidade: <input type="number" name="quantity" min="1" max="{{ max_quantity }}" required><br>
        <input type="submit" value="Reservar">
    </form>
    <a href="/">Voltar</a>
</body>
</html>
'''

PROFILE_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Perfil</title>
</head>
<body>
    <h1>Perfil</h1>
    <table border="1">
        <tr><th>Serviço</th><th>Quantidade</th><th>Status</th><th>Data</th></tr>
        {% for res in reservations %}
            <tr><td>{{ res.service }}</td><td>{{ res.quantity }}</td><td>{{ res.status }}</td><td>{{ res.created_at }}</td></tr>
        {% endfor %}
    </table>
    <a href="/">Voltar</a>
</body>
</html>
'''

ADMIN_HTML = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Admin</title>
</head>
<body>
    <h1>Admin Panel</h1>
    <p>Usuários: {{ users_count }}</p>
    <p>Reservas Pendentes: {{ pending_count }}</p>
    <p>Total Reservas: {{ total_reservations }}</p>
    <h2>Filtros</h2>
    <form method="get">
        Email: <input type="text" name="email_filter"><br>
        Role: <input type="text" name="role_filter"><br>
        Serviço: <input type="text" name="service_filter"><br>
        Status: <input type="text" name="status_filter"><br>
        <input type="submit" value="Filtrar">
    </form>
    <h2>Usuários</h2>
    <table border="1">
        <tr><th>ID</th><th>Nome</th><th>Email</th><th>Role</th><th>Ações</th></tr>
        {% for user in users %}
            <tr><td>{{ user.id }}</td><td>{{ user.name }}</td><td>{{ user.email }}</td><td>{{ user.role }}</td>
                <td>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="promote">
                        <input type="hidden" name="user_id" value="{{ user.id }}">
                        <input type="submit" value="Promover">
                    </form>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="demote">
                        <input type="hidden" name="user_id" value="{{ user.id }}">
                        <input type="submit" value="Rebaixar">
                    </form>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="delete_user">
                        <input type="hidden" name="user_id" value="{{ user.id }}">
                        <input type="submit" value="Deletar">
                    </form>
                </td>
            </tr>
        {% endfor %}
    </table>
    <h2>Reservas Pendentes</h2>
    <table border="1">
        <tr><th>ID</th><th>Usuário</th><th>Serviço</th><th>Quantidade</th><th>Ações</th></tr>
        {% for res in pending_reservations %}
            <tr><td>{{ res.id }}</td><td>{{ res.user_name }}</td><td>{{ res.service }}</td><td>{{ res.quantity }}</td>
                <td>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="approve">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        <input type="submit" value="Aprovar">
                    </form>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="deny">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        Razão: <input type="text" name="reason" required>
                        <input type="submit" value="Negar">
                    </form>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="delete_reservation">
                        <input type="hidden" name="res_id" value="{{ res.id }}">
                        <input type="submit" value="Deletar">
                    </form>
                </td>
            </tr>
        {% endfor %}
    </table>
    <h2>Inserir Miniatura</h2>
    <form method="post">
        <input type="hidden" name="action" value="insert_stock">
        Serviço: <input type="text" name="service" required><br>
        Marca: <input type="text" name="marca" required><br>
        Obs: <input type="text" name="obs"><br>
        Preço: <input type="number" name="price" step="0.01" required><br>
        Quantidade: <input type="number" name="quantity" required><br>
        Imagem: <input type="text" name="image" required><br>
        <input type="submit" value="Inserir">
    </form>
    <h2>Inserir Reserva</h2>
    <form method="post">
        <input type="hidden" name="action" value="insert_reservation">
        Usuário: <select name="user_id" required>
            {% for user in all_users %}<option value="{{ user.id }}">{{ user.name }}</option>{% endfor %}
        </select><br>
        Serviço: <input type="text" name="service" required><br>
        Quantidade: <input type="number" name="quantity" required><br>
        Status: <select name="status"><option>pending</option><option>approved</option><option>denied</option></select><br>
        Razão: <input type="text" name="reason"><br>
        <input type="submit" value="Inserir">
    </form>
    <button onclick="location.href='/backup'">Backup JSON</button>
    <button onclick="location.href='/export_csv'">Exportar CSV</button>
    <button onclick="location.href='/sync_stock'">Sincronizar Estoque</button>
    <h2>Fila de Espera</h2>
    <table border="1">
        <tr><th>ID</th><th>Usuário</th><th>Serviço</th><th>Data</th><th>Ação</th></tr>
        {% for wl in waiting_list %}
            <tr><td>{{ wl.id }}</td><td>{{ wl.user_name }}</td><td>{{ wl.service }}</td><td>{{ wl.created_at }}</td>
                <td>
                    <form method="post" style="display:inline;">
                        <input type="hidden" name="action" value="delete_waiting">
                        <input type="hidden" name="wl_id" value="{{ wl.id }}">
                        <input type="submit" value="Deletar">
                    </form>
                </td>
            </tr>
        {% endfor %}
    </table>
    <a href="/">Voltar</a>
</body>
</html>
'''

# Rotas
@app.route('/')
def index():
    thumbnails = load_thumbnails()
    return render_template_string(INDEX_HTML, thumbnails=thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        if not is_valid_email(email) or not is_valid_phone(phone) or len(password) < 6:
            flash('Dados inválidos')
            return redirect(url_for('register'))
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)', 
                           (name, email, phone, hashed.decode('utf-8')))
            conn.commit()
            flash('Registrado com sucesso')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email já existe')
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
        user = cursor.fetchone()
        conn.close()
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['email'] = user['email']
            flash('Login realizado')
            return redirect(url_for('index'))
        flash('Credenciais inválidas')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.clear()
    flash('Logout realizado')
    return redirect(url_for('index'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM reservations WHERE user_id = ?', (session['user_id'],))
    reservations = cursor.fetchall()
    conn.close()
    return render_template_string(PROFILE_HTML, reservations=reservations)

@app.route('/reservar', methods=['GET', 'POST'])
def reservar():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    thumbnails = load_thumbnails()
    if request.method == 'POST':
        services = request.form.getlist('services')
        conn = get_db()
        cursor = conn.cursor()
        for service in services:
            quantity = int(request.form.get(f'quantity_{service}', 1))
            cursor.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),))
            stock_row = cursor.fetchone()
            if stock_row and stock_row['quantity'] >= quantity:
                cursor.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                               (session['user_id'], service, quantity))
                cursor.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
                flash(f'Reserva para {service} realizada')
            else:
                flash(f'Estoque insuficiente para {service}')
        conn.commit()
        conn.close()
        return redirect(url_for('profile'))
    available = request.args.get('available', 'true') == 'true'
    # Filtros simples (não implementados completamente)
    return render_template_string(RESERVAR_HTML, thumbnails=thumbnails, available=available)

@app.route('/reserve_single/<service>', methods=['GET', 'POST'])
def reserve_single(service):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT quantity FROM stock WHERE service = ?', (service.lower(),))
    stock_row = cursor.fetchone()
    max_quantity = stock_row['quantity'] if stock_row else 0
    conn.close()
    if request.method == 'POST':
        quantity = int(request.form['quantity'])
        if quantity <= max_quantity:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO reservations (user_id, service, quantity) VALUES (?, ?, ?)', 
                           (session['user_id'], service, quantity))
            cursor.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
            conn.commit()
            conn.close()
            flash('Reserva realizada')
            return redirect(url_for('profile'))
        flash('Quantidade inválida')
    return render_template_string(RESERVE_SINGLE_HTML, service=service, max_quantity=max_quantity)

@app.route('/add_waiting_list/<service>')
def add_waiting_list(service):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], service))
        conn.commit()
        flash('Adicionado à fila! Aguarde notificação.')
    except sqlite3.IntegrityError:
        flash('Já está na fila para este serviço')
    finally:
        conn.close()
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('role') != 'admin':
        return redirect(url_for('index'))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    users_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM reservations WHERE status = "pending"')
    pending_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM reservations')
    total_reservations = cursor.fetchone()[0]
    # Filtros
    email_filter = request.args.get('email_filter', '')
    role_filter = request.args.get('role_filter', '')
    service_filter = request.args.get('service_filter', '')
    status_filter = request.args.get('status_filter', '')
    query_users = 'SELECT * FROM users WHERE 1=1'
    params_users = []
    if email_filter:
        query_users += ' AND email LIKE ?'
        params_users.append(f'%{email_filter}%')
    if role_filter:
        query_users += ' AND role = ?'
        params_users.append(role_filter)
    cursor.execute(query_users, params_users)
    users = cursor.fetchall()
    query_res = '''SELECT r.*, u.name as user_name FROM reservations r JOIN users u ON r.user_id = u.id WHERE r.status = "pending"'''
    params_res = []
    if service_filter:
        query_res += ' AND r.service LIKE ?'
        params_res.append(f'%{service_filter}%')
    if status_filter:
        query_res += ' AND r.status = ?'
        params_res.append(status_filter)
    cursor.execute(query_res, params_res)
    pending_reservations = cursor.fetchall()
    cursor.execute('SELECT * FROM users')
    all_users = cursor.fetchall()
    cursor.execute('SELECT wl.*, u.name as user_name FROM waiting_list wl JOIN users u ON wl.user_id = u.id')
    waiting_list = cursor.fetchall()
    conn.close()
    if request.method == 'POST':
        action = request.form['action']
        if action == 'promote':
            user_id = request.form['user_id']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
            conn.commit()
            conn.close()
        elif action == 'demote':
            user_id = request.form['user_id']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
            conn.commit()
            conn.close()
        elif action == 'delete_user':
            user_id = request.form['user_id']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
            conn.commit()
            conn.close()
        elif action == 'approve':
            res_id = request.form['res_id']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('UPDATE reservations SET status = "approved", approved_by = ? WHERE id = ?', (session['user_id'], res_id))
            conn.commit()
            conn.close()
        elif action == 'deny':
            res_id = request.form['res_id']
            reason = request.form['reason']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('UPDATE reservations SET status = "denied", denied_reason = ? WHERE id = ?', (reason, res_id))
            conn.commit()
            conn.close()
        elif action == 'delete_reservation':
            res_id = request.form['res_id']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT service, quantity FROM reservations WHERE id = ?', (res_id,))
            res = cursor.fetchone()
            if res:
                cursor.execute('UPDATE stock SET quantity = quantity + ? WHERE service = ?', (res['quantity'], res['service'].lower()))
            cursor.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
            conn.commit()
            conn.close()
        elif action == 'insert_stock':
            service = request.form['service'].strip().lower()
            marca = request.form['marca']
            obs = request.form['obs']
            price = float(request.form['price'])
            quantity = int(request.form['quantity'])
            image = request.form['image']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO stock (service, quantity) VALUES (?, ?)', (service, quantity))
            conn.commit()
            conn.close()
            # Nota: outros campos não são armazenados no DB, apenas no sheet
        elif action == 'insert_reservation':
            user_id = request.form['user_id']
            service = request.form['service']
            quantity = int(request.form['quantity'])
            status = request.form['status']
            reason = request.form.get('reason', '')
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO reservations (user_id, service, quantity, status, denied_reason) VALUES (?, ?, ?, ?, ?)', 
                           (user_id, service, quantity, status, reason))
            if status == 'approved':
                cursor.execute('UPDATE stock SET quantity = quantity - ? WHERE service = ?', (quantity, service.lower()))
            conn.commit()
            conn.close()
        elif action == 'delete_waiting':
            wl_id = request.form['wl_id']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM waiting_list WHERE id = ?', (wl_id,))
            conn.commit()
            conn.close()
        return redirect(url_for('admin'))
    return render_template_string(ADMIN_HTML, users_count=users_count, pending_count=pending_count, total_reservations=total_reservations, 
                                  users=users, pending_reservations=pending_reservations, all_users=all_users, waiting_list=waiting_list)

@app.route('/backup')
def backup():
    if session.get('role') != 'admin':
        return redirect(url_for('index'))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users')
    users = cursor.fetchall()
    cursor.execute('SELECT * FROM reservations')
    reservations = cursor.fetchall()
    cursor.execute('SELECT * FROM stock')
    stock = cursor.fetchall()
    cursor.execute('SELECT * FROM waiting_list')
    waiting_list = cursor.fetchall()
    conn.close()
    data = {
        'timestamp': datetime.now().isoformat(),
        'users': [dict(row) for row in users],
        'reservations': [dict(row) for row in reservations],
        'stock': [dict(row) for row in stock],
        'waiting_list': [dict(row) for row in waiting_list]
    }
    response = make_response(json.dumps(data, indent=4))
    response.headers['Content-Disposition'] = 'attachment; filename=backup.json'
    response.headers['Content-Type'] = 'application/json'
    return response

@app.route('/export_csv')
def export_csv():
    if session.get('role') != 'admin':
        return redirect(url_for('index'))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''SELECT r.id, u.name as user_name, u.email, r.service, r.quantity, r.status, r.created_at 
                      FROM reservations r JOIN users u ON r.user_id = u.id''')
    rows = cursor.fetchall()
    conn.close()
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['ID', 'Nome Usuário', 'Email', 'Serviço', 'Quantidade', 'Status', 'Data Criação'])
    for row in rows:
        writer.writerow([row['id'], row['user_name'], row['email'], row['service'], row['quantity'], row['status'], row['created_at']])
    output = BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name='reservations.csv')

@app.route('/sync_stock')
def sync_stock():
    if session.get('role') != 'admin':
        return redirect(url_for('index'))
    sync_stock_from_sheet()
    flash('Estoque sincronizado')
    return redirect(url_for('admin'))

@app.errorhandler(404)
def page_not_found(e):
    flash('Página não encontrada')
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(e):
    flash('Erro interno')
    return redirect(url_for('index'))

@app.route('/favicon.ico')
def favicon():
    return '', 204

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=PORT, debug=True)
