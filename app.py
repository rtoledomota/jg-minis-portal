import os
import sqlite3
import bcrypt
import logging
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # Change this in production

# Environment variables
LOGO_URL = os.getenv('LOGO_URL', 'https://example.com/logo.png')
WHATSAPP_NUMBER = os.getenv('WHATSAPP_NUMBER', '+5511999999999')
GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS', 'path/to/credentials.json')

# Logging
logging.basicConfig(level=logging.DEBUG)

# Database
DB_PATH = 'jgminis.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            image_url TEXT,
            data_insercao TEXT,
            previsao_chegada TEXT,
            marca TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stock_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (stock_id) REFERENCES stock (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS waiting_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stock_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (stock_id) REFERENCES stock (id)
        )
    ''')
    conn.commit()
    # Sync initial stock if empty
    cursor.execute('SELECT COUNT(*) FROM stock')
    if cursor.fetchone()[0] == 0:
        sync_stock_from_sheet()
    conn.close()

def sync_stock_from_sheet():
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS, scope)
        client = gspread.authorize(creds)
        sheet = client.open('JG Minis Stock').sheet1  # Assuming sheet name
        records = sheet.get_all_records()
        conn = get_db()
        cursor = conn.cursor()
        for record in records:
            name = record.get('name', '').strip().lower()
            quantity = int(record.get('quantity', 0))
            image_url = record.get('image_url', '')
            data_insercao = record.get('data_insercao', '')
            previsao_chegada = record.get('previsao_chegada', '')
            marca = record.get('marca', '')
            cursor.execute('INSERT OR REPLACE INTO stock (name, quantity, image_url, data_insercao, previsao_chegada, marca) VALUES (?, ?, ?, ?, ?, ?)', 
                           (name, quantity, image_url, data_insercao, previsao_chegada, marca))
        conn.commit()
        conn.close()
        logging.info('Stock synced from Google Sheets')
    except Exception as e:
        logging.error(f'Error syncing stock: {e}')

def load_thumbnails():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM stock')
    thumbnails = cursor.fetchall()
    conn.close()
    return thumbnails

# Templates
base_template = '''
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>JG Minis Portal de Reservas</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; }
        header { background-color: #333; color: white; padding: 10px; text-align: center; }
        header img { height: 50px; }
        nav { background-color: #f4f4f4; padding: 10px; }
        nav a { margin: 0 15px; text-decoration: none; color: #333; font-size: 1.2em; }
        .container { padding: 20px; }
        .thumbnail { display: inline-block; margin: 10px; border: 1px solid #ccc; padding: 10px; }
        .thumbnail img { width: 150px; height: 150px; }
        .esgotado { filter: grayscale(100%); position: relative; }
        .esgotado::after { content: "ESGOTADO"; position: absolute; top: 10px; left: 10px; background: red; color: white; padding: 5px; }
        .reserve-btn { background: blue; color: white; padding: 10px; text-decoration: none; }
        .waiting-btn { background: yellow; color: black; padding: 10px; text-decoration: none; }
        .contact-btn { background: green; color: white; padding: 10px; text-decoration: none; }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="Logo">
        <h1>JG Minis Portal de Reservas</h1>
    </header>
    <nav>
        <a href="/">Home</a>
        {% if session.get('user_id') %}
            <a href="/profile">Perfil</a>
            <a href="/logout">Logout</a>
            {% if session.get('role') == 'admin' %}
                <a href="/admin">Admin</a>
            {% endif %}
        {% else %}
            <a href="/login">Login</a>
            <a href="/register">Registrar</a>
        {% endif %}
    </nav>
    <div class="container">
        {% block content %}{% endblock %}
    </div>
</body>
</html>
'''

home_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Miniaturas Disponíveis</h2>
<div>
    {% for item in thumbnails %}
        <div class="thumbnail{% if item.quantity == 0 %} esgotado{% endif %}">
            <img src="{{ item.image_url }}" alt="{{ item.name }}">
            <p>{{ item.name }}</p>
            <p>Quantidade: {{ item.quantity }}</p>
            {% if item.quantity > 0 %}
                <a href="/reserve/{{ item.id }}" class="reserve-btn">Reservar Agora</a>
            {% else %}
                <a href="#" onclick="addToWaitingList({{ item.id }})" class="waiting-btn">Fila de Espera</a>
                <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, estou interessado em {{ item.name }}" class="contact-btn">Entrar em Contato</a>
            {% endif %}
        </div>
    {% endfor %}
</div>
<script>
function addToWaitingList(stockId) {
    fetch('/add_to_waiting_list', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stock_id: stockId })
    }).then(response => {
        if (response.ok) {
            alert('Adicionado à fila de espera');
        } else {
            alert('Erro');
        }
    });
}
</script>
{% endblock %}
'''

register_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Registrar</h2>
<form method="post">
    <label>Nome: <input type="text" name="name" required></label><br>
    <label>Email: <input type="email" name="email" required></label><br>
    <label>Telefone: <input type="tel" name="phone" required></label><br>
    <label>Senha: <input type="password" name="password" required></label><br>
    <button type="submit">Registrar</button>
</form>
{% endblock %}
'''

login_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Login</h2>
<form method="post">
    <label>Email: <input type="email" name="email" required></label><br>
    <label>Senha: <input type="password" name="password" required></label><br>
    <button type="submit">Login</button>
</form>
{% endblock %}
'''

profile_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Perfil</h2>
<h3>Suas Reservas</h3>
<ul>
    {% for res in reservations %}
        <li>{{ res.name }} - Quantidade: {{ res.quantity }} - Status: {{ res.status }}</li>
    {% endfor %}
</ul>
{% endblock %}
'''

reserve_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Reservar {{ item.name }}</h2>
<img src="{{ item.image_url }}" alt="{{ item.name }}">
<form method="post">
    <label>Quantidade: <input type="number" name="quantity" min="1" max="{{ item.quantity }}" required></label><br>
    <button type="submit">Reservar</button>
</form>
{% endblock %}
'''

reserve_multiple_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Reservar Múltiplas</h2>
<form method="post">
    <label>Filtrar por: <input type="text" name="filter" placeholder="Disponível, Data Inserção, etc."></label><br>
    {% for item in items %}
        <div>
            <input type="checkbox" name="selected" value="{{ item.id }}">
            <img src="{{ item.image_url }}" width="100" alt="{{ item.name }}">
            <p>{{ item.name }}</p>
            <input type="number" name="quantity_{{ item.id }}" min="0" max="{{ item.quantity }}">
        </div>
    {% endfor %}
    <button type="submit">Reservar Selecionadas</button>
</form>
{% endblock %}
'''

admin_template = '''
{% extends "base.html" %}
{% block content %}
<h2>Painel Admin</h2>
<h3>Estatísticas</h3>
<p>Total Usuários: {{ stats.users }}</p>
<p>Total Reservas: {{ stats.reservations }}</p>
<p>Total Estoque: {{ stats.stock }}</p>

<h3>Gerenciar Usuários</h3>
<form method="post" action="/admin/user_action">
    <label>Filtro: <input type="text" name="filter"></label><br>
    {% for user in users %}
        <div>
            <input type="checkbox" name="selected_users" value="{{ user.id }}">
            {{ user.name }} - {{ user.email }} - {{ user.role }}
        </div>
    {% endfor %}
    <button name="action" value="promote">Promover</button>
    <button name="action" value="demote">Rebaixar</button>
    <button name="action" value="delete">Deletar</button>
</form>

<h3>Gerenciar Reservas</h3>
<form method="post" action="/admin/reservation_action">
    {% for res in reservations %}
        <div>
            <input type="checkbox" name="selected_res" value="{{ res.id }}">
            {{ res.name }} - {{ res.quantity }} - {{ res.status }}
            <input type="text" name="reason_{{ res.id }}" placeholder="Razão">
        </div>
    {% endfor %}
    <button name="action" value="approve">Aprovar</button>
    <button name="action" value="deny">Negar</button>
    <button name="action" value="delete">Deletar</button>
</form>

<h3>Inserir Nova Miniatura</h3>
<form method="post" action="/admin/insert_stock">
    <label>Nome: <input type="text" name="name" required></label><br>
    <label>Quantidade: <input type="number" name="quantity" required></label><br>
    <label>Imagem URL: <input type="url" name="image_url"></label><br>
    <label>Data Inserção: <input type="date" name="data_insercao"></label><br>
    <label>Previsão Chegada: <input type="date" name="previsao_chegada"></label><br>
    <label>Marca: <input type="text" name="marca"></label><br>
    <button type="submit">Inserir</button>
</form>

<h3>Inserir Nova Reserva</h3>
<form method="post" action="/admin/insert_reservation">
    <label>User ID: <input type="number" name="user_id" required></label><br>
    <label>Stock ID: <input type="number" name="stock_id" required></label><br>
    <label>Quantidade: <input type="number" name="quantity" required></label><br>
    <button type="submit">Inserir</button>
</form>

<h3>Sincronizar Estoque</h3>
<form method="post" action="/admin/sync_stock">
    <button type="submit">Sincronizar</button>
</form>

<h3>Backups</h3>
<a href="/admin/backup_json">Download JSON</a>
<a href="/admin/backup_csv">Download CSV</a>

<h3>Fila de Espera</h3>
<ul>
    {% for wl in waiting_list %}
        <li>{{ wl.name }} - <a href="/admin/delete_waiting/{{ wl.id }}">Deletar</a></li>
    {% endfor %}
</ul>
{% endblock %}
'''

# Routes
@app.route('/')
def home():
    try:
        thumbnails = load_thumbnails()
        return render_template_string(base_template + home_template, thumbnails=thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)
    except Exception as e:
        logging.error(f'Error in home: {e}')
        return 'Internal Server Error', 500

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            name = request.form['name']
            email = request.form['email']
            phone = request.form['phone']
            password = request.form['password']
            if not name or not email or not phone:
                flash('Nome, email e telefone são obrigatórios')
                return redirect(url_for('register'))
            password_hash = generate_password_hash(password)
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (name, email, phone, password_hash) VALUES (?, ?, ?, ?)', (name, email, phone, password_hash))
            conn.commit()
            conn.close()
            flash('Registrado com sucesso')
            return redirect(url_for('login'))
        except Exception as e:
            logging.error(f'Error in register: {e}')
            flash('Erro no registro')
            return redirect(url_for('register'))
    return render_template_string(base_template + register_template, logo_url=LOGO_URL)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            email = request.form['email']
            password = request.form['password']
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
            user = cursor.fetchone()
            conn.close()
            if user and check_password_hash(user['password_hash'], password):
                session['user_id'] = user['id']
                session['role'] = user['role']
                return redirect(url_for('home'))
            else:
                flash('Credenciais inválidas')
        except Exception as e:
            logging.error(f'Error in login: {e}')
            flash('Erro no login')
    return render_template_string(base_template + login_template, logo_url=LOGO_URL)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT r.quantity, r.status, s.name FROM reservations r
            JOIN stock s ON r.stock_id = s.id
            WHERE r.user_id = ?
        ''', (session['user_id'],))
        reservations = cursor.fetchall()
        conn.close()
        return render_template_string(base_template + profile_template, reservations=reservations, logo_url=LOGO_URL)
    except Exception as e:
        logging.error(f'Error in profile: {e}')
        return 'Internal Server Error', 500

@app.route('/reserve/<int:stock_id>', methods=['GET', 'POST'])
def reserve(stock_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM stock WHERE id = ?', (stock_id,))
        item = cursor.fetchone()
        if not item:
            return 'Item não encontrado', 404
        if request.method == 'POST':
            quantity = int(request.form['quantity'])
            if quantity > item['quantity']:
                flash('Quantidade excede estoque')
                return redirect(url_for('reserve', stock_id=stock_id))
            cursor.execute('INSERT INTO reservations (user_id, stock_id, quantity) VALUES (?, ?, ?)', (session['user_id'], stock_id, quantity))
            cursor.execute('UPDATE stock SET quantity = quantity - ? WHERE id = ?', (quantity, stock_id))
            conn.commit()
            conn.close()
            flash('Reserva feita')
            return redirect(url_for('profile'))
        conn.close()
        return render_template_string(base_template + reserve_template, item=item, logo_url=LOGO_URL)
    except Exception as e:
        logging.error(f'Error in reserve: {e}')
        return 'Internal Server Error', 500

@app.route('/reserve_multiple', methods=['GET', 'POST'])
def reserve_multiple():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        conn = get_db()
        cursor = conn.cursor()
        filter_text = request.args.get('filter', '')
        query = 'SELECT * FROM stock'
        if filter_text:
            query += ' WHERE name LIKE ? OR data_insercao LIKE ? OR previsao_chegada LIKE ? OR marca LIKE ?'
            cursor.execute(query, ('%' + filter_text + '%', '%' + filter_text + '%', '%' + filter_text + '%', '%' + filter_text + '%'))
        else:
            cursor.execute(query)
        items = cursor.fetchall()
        if request.method == 'POST':
            selected = request.form.getlist('selected')
            for stock_id in selected:
                quantity = int(request.form.get(f'quantity_{stock_id}', 0))
                if quantity > 0:
                    cursor.execute('SELECT quantity FROM stock WHERE id = ?', (stock_id,))
                    stock_qty = cursor.fetchone()['quantity']
                    if quantity <= stock_qty:
                        cursor.execute('INSERT INTO reservations (user_id, stock_id, quantity) VALUES (?, ?, ?)', (session['user_id'], stock_id, quantity))
                        cursor.execute('UPDATE stock SET quantity = quantity - ? WHERE id = ?', (quantity, stock_id))
            conn.commit()
            conn.close()
            flash('Reservas feitas')
            return redirect(url_for('profile'))
        conn.close()
        return render_template_string(base_template + reserve_multiple_template, items=items, logo_url=LOGO_URL)
    except Exception as e:
        logging.error(f'Error in reserve_multiple: {e}')
        return 'Internal Server Error', 500

@app.route('/add_to_waiting_list', methods=['POST'])
def add_to_waiting_list():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    try:
        data = request.get_json()
        stock_id = data['stock_id']
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO waiting_list (user_id, stock_id) VALUES (?, ?)', (session['user_id'], stock_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f'Error in add_to_waiting_list: {e}')
        return jsonify({'error': 'Internal error'}), 500

@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        users_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM reservations')
        res_count = cursor.fetchone()[0]
        cursor.execute('SELECT SUM(quantity) FROM stock')
        stock_count = cursor.fetchone()[0] or 0
        stats = {'users': users_count, 'reservations': res_count, 'stock': stock_count}
        cursor.execute('SELECT * FROM users')
        users = cursor.fetchall()
        cursor.execute('''
            SELECT r.id, r.quantity, r.status, s.name FROM reservations r
            JOIN stock s ON r.stock_id = s.id
        ''')
        reservations = cursor.fetchall()
        cursor.execute('''
            SELECT wl.id, u.name, s.name FROM waiting_list wl
            JOIN users u ON wl.user_id = u.id
            JOIN stock s ON wl.stock_id = s.id
        ''')
        waiting_list = cursor.fetchall()
        conn.close()
        return render_template_string(base_template + admin_template, stats=stats, users=users, reservations=reservations, waiting_list=waiting_list, logo_url=LOGO_URL)
    except Exception as e:
        logging.error(f'Error in admin: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/user_action', methods=['POST'])
def admin_user_action():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        action = request.form['action']
        selected_users = request.form.getlist('selected_users')
        conn = get_db()
        cursor = conn.cursor()
        for user_id in selected_users:
            if action == 'promote':
                cursor.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
            elif action == 'demote':
                cursor.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
            elif action == 'delete':
                cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()
        return redirect(url_for('admin'))
    except Exception as e:
        logging.error(f'Error in admin_user_action: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/reservation_action', methods=['POST'])
def admin_reservation_action():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        action = request.form['action']
        selected_res = request.form.getlist('selected_res')
        conn = get_db()
        cursor = conn.cursor()
        for res_id in selected_res:
            reason = request.form.get(f'reason_{res_id}', '')
            if action == 'approve':
                cursor.execute('UPDATE reservations SET status = "approved" WHERE id = ?', (res_id,))
            elif action == 'deny':
                cursor.execute('UPDATE reservations SET status = "denied", reason = ? WHERE id = ?', (reason, res_id))
            elif action == 'delete':
                cursor.execute('SELECT stock_id, quantity FROM reservations WHERE id = ?', (res_id,))
                res = cursor.fetchone()
                cursor.execute('UPDATE stock SET quantity = quantity + ? WHERE id = ?', (res['quantity'], res['stock_id']))
                cursor.execute('DELETE FROM reservations WHERE id = ?', (res_id,))
        conn.commit()
        conn.close()
        return redirect(url_for('admin'))
    except Exception as e:
        logging.error(f'Error in admin_reservation_action: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/insert_stock', methods=['POST'])
def admin_insert_stock():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        name = request.form['name'].strip().lower()
        quantity = int(request.form['quantity'])
        image_url = request.form.get('image_url', '')
        data_insercao = request.form.get('data_insercao', '')
        previsao_chegada = request.form.get('previsao_chegada', '')
        marca = request.form.get('marca', '')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO stock (name, quantity, image_url, data_insercao, previsao_chegada, marca) VALUES (?, ?, ?, ?, ?, ?)', 
                       (name, quantity, image_url, data_insercao, previsao_chegada, marca))
        conn.commit()
        conn.close()
        return redirect(url_for('admin'))
    except Exception as e:
        logging.error(f'Error in admin_insert_stock: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/insert_reservation', methods=['POST'])
def admin_insert_reservation():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        user_id = int(request.form['user_id'])
        stock_id = int(request.form['stock_id'])
        quantity = int(request.form['quantity'])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO reservations (user_id, stock_id, quantity) VALUES (?, ?, ?)', (user_id, stock_id, quantity))
        cursor.execute('UPDATE stock SET quantity = quantity - ? WHERE id = ?', (quantity, stock_id))
        conn.commit()
        conn.close()
        return redirect(url_for('admin'))
    except Exception as e:
        logging.error(f'Error in admin_insert_reservation: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/sync_stock', methods=['POST'])
def admin_sync_stock():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        sync_stock_from_sheet()
        return redirect(url_for('admin'))
    except Exception as e:
        logging.error(f'Error in admin_sync_stock: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/backup_json')
def admin_backup_json():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users')
        users = [dict(row) for row in cursor.fetchall()]
        cursor.execute('SELECT * FROM stock')
        stock = [dict(row) for row in cursor.fetchall()]
        cursor.execute('SELECT * FROM reservations')
        reservations = [dict(row) for row in cursor.fetchall()]
        cursor.execute('SELECT * FROM waiting_list')
        waiting_list = [dict(row) for row in cursor.fetchall()]
        conn.close()
        data = {'users': users, 'stock': stock, 'reservations': reservations, 'waiting_list': waiting_list}
        return jsonify(data)
    except Exception as e:
        logging.error(f'Error in admin_backup_json: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/backup_csv')
def admin_backup_csv():
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        import csv
        from io import StringIO
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM reservations')
        reservations = cursor.fetchall()
        conn.close()
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'User ID', 'Stock ID', 'Quantity', 'Status', 'Reason', 'Created At'])
        for res in reservations:
            writer.writerow([res['id'], res['user_id'], res['stock_id'], res['quantity'], res['status'], res['reason'], res['created_at']])
        output.seek(0)
        return output.getvalue(), {'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename=reservations.csv'}
    except Exception as e:
        logging.error(f'Error in admin_backup_csv: {e}')
        return 'Internal Server Error', 500

@app.route('/admin/delete_waiting/<int:wl_id>')
def admin_delete_waiting(wl_id):
    if session.get('role') != 'admin':
        return 'Acesso negado', 403
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM waiting_list WHERE id = ?', (wl_id,))
        conn.commit()
        conn.close()
        return redirect(url_for('admin'))
    except Exception as e:
        logging.error(f'Error in admin_delete_waiting: {e}')
        return 'Internal Server Error', 500

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8080, debug=True)
