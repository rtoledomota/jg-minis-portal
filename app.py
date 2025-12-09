import os
import re
import json
import csv
import io
import logging
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, send_file, abort, make_response
from werkzeug.security import generate_password_hash, check_password_hash
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
app.secret_key = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_jg_minis_v4_3_production_stable')

# --- 3. Environment Variables ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')  # Just numbers, no + or spaces
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

# --- 4. Database Helper Functions (Preservation Guaranteed) ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # Users table - IF NOT EXISTS preserves existing data
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # Reservations table - IF NOT EXISTS preserves existing data
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

        # Stock table - IF NOT EXISTS preserves existing data
        c.execute('''CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT UNIQUE NOT NULL,
            quantity INTEGER DEFAULT 0,
            last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # Waiting list table - IF NOT EXISTS preserves existing data
        c.execute('''CREATE TABLE IF NOT EXISTS waiting_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')

        # Create admin user ONLY if not exists (preserves existing admins)
        c.execute('SELECT id FROM users WHERE email = ?', ('admin@jgminis.com.br',))
        if not c.fetchone():
            hashed_pw = generate_password_hash('admin123')
            c.execute('INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)', 
                      ('Admin', 'admin@jgminis.com.br', '11999999999', hashed_pw, 'admin'))
            logging.info('Usuário admin criado no DB (primeira vez)')

        # Initial stock sync ONLY if stock table is empty (preserves existing stock)
        c.execute('SELECT COUNT(*) FROM stock')
        if c.fetchone()[0] == 0 and sheet:
            try:
                records = sheet.get_all_records()
                for record in records[1:]:  # Skip header
                    service = normalize_service_name(record.get('NOME DA MINIATURA', ''))
                    qty = int(record.get('QUANTIDADE DISPONÍVEL', 0) or 0)
                    if service:
                        c.execute('INSERT OR IGNORE INTO stock (service, quantity) VALUES (?, ?)', (service, qty))
                conn.commit()
                logging.info('Stock inicial sincronizado do Google Sheets (preservando dados existentes)')
            except Exception as e:
                logging.error(f'Erro no sync inicial de stock: {e}')

        conn.commit()
        logging.info('DB inicializado sem perda de dados - tabelas preservadas')
    except Exception as e:
        logging.error(f'Erro no init_db: {e}')
        conn.rollback()
    finally:
        conn.close()

# --- 5. Validation Functions ---
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    cleaned = re.sub(r'[^\d]', '', phone)  # Remove non-digits
    return cleaned.isdigit() and 10 <= len(cleaned) <= 11

def normalize_service_name(name):
    return name.strip().lower()  # Case-insensitive normalization

# --- 6. Stock Management ---
def get_stock(service):
    service_norm = normalize_service_name(service)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT quantity FROM stock WHERE service = ?', (service_norm,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def update_stock(service, delta):
    service_norm = normalize_service_name(service)
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('UPDATE stock SET quantity = quantity + ?, last_sync = CURRENT_TIMESTAMP WHERE service = ?', (delta, service_norm))
        if c.rowcount == 0:
            c.execute('INSERT INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service_norm, max(0, delta)))
        conn.commit()
        logging.info(f'Stock atualizado para {service_norm}: delta {delta}')
    except Exception as e:
        logging.error(f'Erro ao atualizar stock: {e}')
        conn.rollback()
    finally:
        conn.close()

def sync_stock_from_sheet():
    if not sheet:
        flash('Integração com Google Sheets indisponível - usando dados locais.')
        return False
    try:
        records = sheet.get_all_records()
        conn = get_db_connection()
        c = conn.cursor()
        for record in records[1:]:  # Skip header
            service = normalize_service_name(record.get('NOME DA MINIATURA', ''))
            qty = int(record.get('QUANTIDADE DISPONÍVEL', 0) or 0)
            if service:
                c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
        conn.commit()
        conn.close()
        logging.info('Stock sincronizado com sucesso do Google Sheets')
        flash('Stock sincronizado com Google Sheets!')
        return True
    except Exception as e:
        logging.error(f'Erro no sync de stock: {e}')
        flash('Erro ao sincronizar stock. Usando dados locais.')
        return False

# --- 7. User Management ---
def create_user(name, email, phone, password):
    if not all([name, email, phone, password]):
        return False, 'Todos os campos são obrigatórios.'
    if not is_valid_email(email):
        return False, 'Email inválido.'
    if not is_valid_phone(phone):
        return False, 'Telefone inválido (10-11 dígitos).'
    if len(password) < 6:
        return False, 'Senha deve ter pelo menos 6 caracteres.'
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # Check if email exists (preserves data)
        c.execute('SELECT id FROM users WHERE email = ?', (email,))
        if c.fetchone():
            conn.close()
            return False, 'Email já cadastrado.'
        
        hashed_pw = generate_password_hash(password)
        c.execute('INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)', 
                  (name, email, phone, hashed_pw, 'user'))
        conn.commit()
        logging.info(f'Usuário criado: {email}')
        conn.close()
        return True, 'Cadastro realizado com sucesso!'
    except Exception as e:
        logging.error(f'Erro ao criar usuário: {e}')
        conn.rollback()
        conn.close()
        return False, 'Erro interno no cadastro. Tente novamente.'

def authenticate_user(email, password):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, name, password, role FROM users WHERE email = ?', (email,))
    user = c.fetchone()
    conn.close()
    if user and check_password_hash(user['password'], password):
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['role'] = user['role']
        logging.info(f'Login bem-sucedido: {email}')
        return True
    logging.warning(f'Falha no login: {email}')
    return False

def get_user_by_id(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def promote_to_admin(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # Verify user exists first
        c.execute('SELECT id FROM users WHERE id = ?', (user_id,))
        if not c.fetchone():
            conn.close()
            return False, 'Usuário não encontrado.'
        
        c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
        if c.rowcount > 0:
            conn.commit()
            logging.info(f'Usuário {user_id} promovido a admin')
            conn.close()
            return True, 'Usuário promovido a admin com sucesso!'
        else:
            conn.close()
            return False, 'Erro ao promover usuário.'
    except Exception as e:
        logging.error(f'Erro ao promover admin: {e}')
        conn.rollback()
        conn.close()
        return False, 'Erro interno na promoção.'

# --- 8. Reservation Management ---
def create_reservation(user_id, service, quantity=1):
    service_norm = normalize_service_name(service)
    stock = get_stock(service_norm)
    if stock < quantity:
        # Add to waiting list instead
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('INSERT INTO waiting_list (user_id, service) VALUES (?, ?)', (user_id, service_norm))
            conn.commit()
            logging.info(f'Usuário {user_id} adicionado à fila para {service_norm}')
            conn.close()
            return False, f'Estoque insuficiente ({stock} disponível). Você foi adicionado à fila de espera!'
        except Exception as e:
            logging.error(f'Erro na fila de espera: {e}')
            conn.rollback()
            conn.close()
            return False, 'Erro ao adicionar à fila de espera.'
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO reservations (user_id, service, quantity, status) VALUES (?, ?, ?, ?)', 
                  (user_id, service_norm, quantity, 'pending'))
        reservation_id = c.lastrowid
        # Update stock
        update_stock(service_norm, -quantity)
        conn.commit()
        logging.info(f'Reserva criada: ID {reservation_id} para {service_norm}, qty {quantity}')
        conn.close()
        return True, f'Reserva #{reservation_id} criada com sucesso para {quantity} unidade(s)!'
    except Exception as e:
        logging.error(f'Erro ao criar reserva: {e}')
        conn.rollback()
        conn.close()
        # Revert stock if needed (but since insert failed, no need)
        return False, 'Erro interno na reserva. Tente novamente.'

def confirm_reservation(reservation_id, admin_id):
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('UPDATE reservations SET status = "confirmed", approved_by = ? WHERE id = ? AND status = "pending"', 
                  (admin_id, reservation_id))
        if c.rowcount > 0:
            conn.commit()
            logging.info(f'Reserva {reservation_id} confirmada por admin {admin_id}')
            conn.close()
            return True, 'Reserva confirmada!'
        else:
            conn.close()
            return False, 'Reserva não encontrada ou já processada.'
    except Exception as e:
        logging.error(f'Erro ao confirmar reserva: {e}')
        conn.rollback()
        conn.close()
        return False, 'Erro interno na confirmação.'

def reject_reservation(reservation_id, admin_id, reason):
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('UPDATE reservations SET status = "rejected", approved_by = ?, denied_reason = ? WHERE id = ? AND status = "pending"', 
                  (admin_id, reason, reservation_id))
        if c.rowcount > 0:
            # Revert stock
            c.execute('SELECT service, quantity FROM reservations WHERE id = ?', (reservation_id,))
            res = c.fetchone()
            if res:
                update_stock(res['service'], res['quantity'])
            conn.commit()
            logging.info(f'Reserva {reservation_id} rejeitada por admin {admin_id}: {reason}')
            conn.close()
            return True, 'Reserva rejeitada e estoque revertido!'
        else:
            conn.close()
            return False, 'Reserva não encontrada ou já processada.'
    except Exception as e:
        logging.error(f'Erro ao rejeitar reserva: {e}')
        conn.rollback()
        conn.close()
        return False, 'Erro interno na rejeição.'

# --- 9. Get All Data for Home/Admin ---
def get_all_minis():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT service, quantity FROM stock ORDER BY service')
    minis = c.fetchall()
    conn.close()
    return minis

def get_all_reservations():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT r.id, r.service, r.quantity, r.status, r.created_at, u.name as user_name, a.name as admin_name
        FROM reservations r
        JOIN users u ON r.user_id = u.id
        LEFT JOIN users a ON r.approved_by = a.id
        ORDER BY r.created_at DESC
    ''')
    reservations = c.fetchall()
    conn.close()
    return reservations

def get_all_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users ORDER BY data_cadastro DESC')
    users = c.fetchall()
    conn.close()
    return users

def get_waiting_list():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT wl.id, wl.service, wl.created_at, u.name as user_name
        FROM waiting_list wl
        JOIN users u ON wl.user_id = u.id
        ORDER BY wl.created_at ASC
    ''')
    waiting = c.fetchall()
    conn.close()
    return waiting

# --- 10. HTML Templates (Inline for Simplicity) ---
HOME_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG Minis Portal de Reservas</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f4f4f4; color: #333; }
        header { text-align: center; margin-bottom: 30px; }
        .logo { max-width: 200px; height: auto; }
        h1 { color: #333; font-size: 2em; }
        .mini-container { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 20px; }
        .mini-card { background: white; border-radius: 10px; padding: 15px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }
        .mini-card.esgotado { opacity: 0.6; filter: grayscale(100%); }
        .mini-card.esgotado .btn-reservar { background: #ccc; cursor: not-allowed; }
        .mini-name { font-weight: bold; margin-bottom: 10px; color: #333 !important; }
        .stock { color: #28a745; font-weight: bold; }
        .btn { display: inline-block; padding: 10px 15px; margin: 5px; text-decoration: none; border-radius: 5px; color: white; }
        .btn-reservar { background: #007bff; }
        .btn-fila { background: #ffc107; color: #000; }
        .btn-contato { background: #28a745; }
        .btn-logout { background: #dc3545; float: right; }
        nav { margin-bottom: 20px; }
        nav a { margin: 0 10px; font-size: 1.2em; text-decoration: none; color: #333; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .flash.success { background: #d4edda; color: #155724; }
        .flash.error { background: #f8d7da; color: #721c24; }
    </style>
</head>
<body>
    <header>
        <img src="{{ logo_url }}" alt="JG Minis Logo" class="logo">
        <h1>JG Minis Portal de Reservas</h1>
    </header>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    {% if session.user_id %}
        <nav>
            <a href="/">Home</a>
            {% if session.role == 'admin' %}
                <a href="/admin">Admin</a>
            {% endif %}
            <a href="/logout" class="btn btn-logout">Logout</a>
        </nav>
        <p>Bem-vindo, {{ session.user_name }}!</p>
    {% else %}
        <nav>
            <a href="/login">Login</a>
            <a href="/register">Cadastro</a>
        </nav>
    {% endif %}
    <div class="mini-container">
        {% for mini in minis %}
            <div class="mini-card {% if mini['quantity'] == 0 %}esgotado{% endif %}">
                <div class="mini-name">{{ mini['service'].title() }}</div>
                <div class="stock">Estoque: {{ mini['quantity'] }}</div>
                {% if mini['quantity'] > 0 %}
                    <a href="/reserve/{{ mini['service'] }}" class="btn btn-reservar">Reservar Agora</a>
                {% else %}
                    <a href="/waiting/{{ mini['service'] }}" class="btn btn-fila">Fila de Espera</a>
                    <a href="https://wa.me/{{ whatsapp_number }}" class="btn btn-contato">Entrar em Contato</a>
                    <p>ESGOTADO</p>
                {% endif %}
            </div>
        {% endfor %}
    </div>
</body>
</html>
'''

RESERVE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reservar {{ service.title() }} - JG Minis</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; background: #f4f4f4; color: #333; }
        h1 { color: #333; }
        form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        label { display: block; margin: 10px 0 5px; font-weight: bold; color: #333 !important; }
        input, select { width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .flash.success { background: #d4edda; color: #155724; }
        .flash.error { background: #f8d7da; color: #721c24; }
        img { max-width: 100%; height: auto; margin: 20px 0; } /* Imagem na reserva */
    </style>
</head>
<body>
    <h1>Reservar {{ service.title() }}</h1>
    <img src="https://i.imgur.com/example-mini.jpg" alt="{{ service.title() }}"> <!-- Placeholder para imagem da miniatura -->
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    {% if not session.user_id %}
        <p>Faça <a href="/login">login</a> para reservar.</p>
    {% else %}
        <form method="POST">
            <label for="quantity">Quantidade (máx. {{ stock }}):</label>
            <select name="quantity" id="quantity">
                {% for q in range(1, stock + 1) %}
                    <option value="{{ q }}">{{ q }}</option>
                {% endfor %}
            </select>
            <button type="submit" class="btn">Confirmar Reserva</button>
        </form>
        <a href="/">Voltar à Home</a>
    {% endif %}
</body>
</html>
'''

ADMIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin - JG Minis</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f4f4f4; color: #333 !important; } /* Texto visível: cor #333 */
        h1, h2 { color: #333 !important; }
        table { width: 100%; border-collapse: collapse; margin: 20px 0; background: white; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; color: #333 !important; } /* Texto visível em tabelas */
        th { background: #f8f9fa; }
        .btn { padding: 5px 10px; margin: 2px; border-radius: 3px; text-decoration: none; color: white; }
        .btn-confirm { background: #28a745; }
        .btn-reject { background: #dc3545; }
        .btn-promote { background: #ffc107; color: #000; }
        .btn-sync { background: #007bff; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; color: #333 !important; }
        .flash.success { background: #d4edda; }
        .flash.error { background: #f8d7da; }
        form { background: white; padding: 15px; border-radius: 5px; margin: 10px 0; }
        input, select, textarea { width: 100%; padding: 5px; margin: 5px 0; }
    </style>
</head>
<body>
    <h1>Painel Admin - JG Minis</h1>
    <a href="/" class="btn">Home</a> <a href="/logout" class="btn" style="background: #dc3545;">Logout</a>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <h2>Resumo</h2>

    <h2>Gerenciar Usuários</h2>
    <table>
        <tr><th>ID</th><th>Nome</th><th>Email</th><th>Telefone</th><th>Role</th><th>Data Cadastro</th><th>Ações</th></tr>
        {% for user in users %}
            <tr>
                <td>{{ user['id'] }}</td>
                <td style="color: #333 !important;">{{ user['name'] }}</td>
                <td style="color: #333 !important;">{{ user['email'] }}</td>
                <td style="color: #333 !important;">{{ user['phone'] }}</td>
                <td>{{ user['role'] }}</td>
                <td>{{ user['data_cadastro'] }}</td>
                <td>
                    {% if user['role'] != 'admin' %}
                        <a href="/admin/promote/{{ user['id'] }}" class="btn btn-promote">Promover Admin</a>
                    {% endif %}
                </td>
            </tr>
        {% endfor %}
    </table>

    <h2>Gerenciar Reservas</h2>
    <table>
        <tr><th>ID</th><th>Serviço</th><th>Usuário</th><th>Quantidade</th><th>Status</th><th>Data</th><th>Ações</th></tr>
        {% for res in reservations %}
            <tr>
                <td>{{ res['id'] }}</td>
                <td style="color: #333 !important;">{{ res['service'].title() }}</td>
                <td style="color: #333 !important;">{{ res['user_name'] }}</td>
                <td>{{ res['quantity'] }}</td>
                <td>{{ res['status'] }}</td>
                <td>{{ res['created_at'] }}</td>
                <td>
                    {% if res['status'] == 'pending' %}
                        <a href="/admin/confirm/{{ res['id'] }}" class="btn btn-confirm">Confirmar Reserva</a>
                        <form method="POST" action="/admin/reject/{{ res['id'] }}" style="display: inline;">
                            <input type="text" name="reason" placeholder="Motivo da rejeição" required>
                            <button type="submit" class="btn btn-reject">Rejeitar Reserva</button>
                        </form>
                    {% endif %}
                </td>
            </tr>
        {% endfor %}
    </table>

    <h2>Fila de Espera</h2>
    <table>
        <tr><th>ID</th><th>Serviço</th><th>Usuário</th><th>Data</th></tr>
        {% for wait in waiting %}
            <tr>
                <td>{{ wait['id'] }}</td>
                <td style="color: #333 !important;">{{ wait['service'].title() }}</td>
                <td style="color: #333 !important;">{{ wait['user_name'] }}</td>
                <td>{{ wait['created_at'] }}</td>
            </tr>
        {% endfor %}
    </table>

    <h2>Ações Admin</h2>
    <form method="POST" action="/admin/sync">
        <button type="submit" class="btn btn-sync">Sincronizar Stock</button>
    </form>
    <a href="/admin/backup/json" class="btn">Backup JSON</a>
    <a href="/admin/backup/csv" class="btn">Backup CSV</a>

    <h2>Inserir Nova Miniatura/Estoque</h2>
    <form method="POST" action="/admin/insert_mini">
        <input type="text" name="service" placeholder="Nome da Miniatura" required>
        <input type="number" name="quantity" placeholder="Quantidade" required>
        <button type="submit" class="btn">Inserir</button>
    </form>

    <h2>Inserir Nova Reserva (Teste)</h2>
    <form method="POST" action="/admin/insert_reservation">
        <input type="number" name="user_id" placeholder="ID Usuário" required>
        <input type="text" name="service" placeholder="Serviço" required>
        <input type="number" name="quantity" value="1" required>
        <button type="submit" class="btn">Inserir Reserva</button>
    </form>
</body>
</html>
'''

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - JG Minis</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 400px; margin: 100px auto; padding: 20px; background: #f4f4f4; color: #333; }
        form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #007bff; color: white; padding: 10px; width: 100%; border: none; border-radius: 5px; cursor: pointer; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .flash.error { background: #f8d7da; color: #721c24; }
    </style>
</head>
<body>
    <h2>Login</h2>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <form method="POST">
        <input type="email" name="email" placeholder="Email" required>
        <input type="password" name="password" placeholder="Senha" required>
        <button type="submit" class="btn">Entrar</button>
    </form>
    <p><a href="/register">Não tem conta? Cadastre-se</a></p>
</body>
</html>
'''

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cadastro - JG Minis</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 400px; margin: 100px auto; padding: 20px; background: #f4f4f4; color: #333; }
        form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #28a745; color: white; padding: 10px; width: 100%; border: none; border-radius: 5px; cursor: pointer; }
        .flash { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .flash.success { background: #d4edda; color: #155724; }
        .flash.error { background: #f8d7da; color: #721c24; }
    </style>
</head>
<body>
    <h2>Cadastro</h2>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <form method="POST">
        <input type="text" name="name" placeholder="Nome Completo" required>
        <input type="email" name="email" placeholder="Email" required>
        <input type="text" name="phone" placeholder="Telefone (ex: 11999999999)" required>
        <input type="password" name="password" placeholder="Senha (mín. 6 chars)" required>
        <button type="submit" class="btn">Cadastrar</button>
    </form>
    <p><a href="/login">Já tem conta? Faça login</a></p>
</body>
</html>
'''

WAITING_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fila de Espera - {{ service.title() }} - JG Minis</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; background: #f4f4f4; color: #333; }
        h1 { color: #333; }
        .btn { background: #ffc107; color: #000; padding: 10px 20px; text-decoration: none; border-radius: 5px; }
    </style>
</head>
<body>
    <h1>Fila de Espera para {{ service.title() }}</h1>
    <p>Você foi adicionado à fila. Entraremos em contato quando houver estoque!</p>
    <a href="/" class="btn">Voltar à Home</a>
</body>
</html>
'''

# --- 11. Routes ---
@app.before_first_request
def before_first_request():
    init_db()

@app.route('/')
def home():
    minis = get_all_minis()
    total_stock = sum(m['quantity'] for m in minis)
    return render_template_string(HOME_TEMPLATE, minis=minis, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER, total_stock=total_stock)

@app.route('/reserve/<service>', methods=['GET', 'POST'])
def reserve(service):
    service_norm = normalize_service_name(service)
    stock = get_stock(service_norm)
    if stock == 0:
        flash('Estoque esgotado. Redirecionando para fila de espera...', 'error')
        logging.info(f'Redirect de reserva falhou para {service_norm} - estoque 0')
        return redirect(url_for('waiting', service=service_norm))
    
    if not session.get('user_id'):
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        quantity = int(request.form.get('quantity', 1))
        if quantity > stock:
            flash(f'Quantidade inválida. Máximo: {stock}', 'error')
            return render_template_string(RESERVE_TEMPLATE, service=service, stock=stock)
        
        success, msg = create_reservation(session['user_id'], service, quantity)
        flash(msg, 'success' if success else 'error')
        if success:
            logging.info(f'Reserva processada com sucesso para {service_norm}')
            return redirect(url_for('home'))
        else:
            logging.warning(f'Falha na reserva para {service_norm}: {msg}')
    
    # GET: Show form
    return render_template_string(RESERVE_TEMPLATE, service=service, stock=stock)

@app.route('/waiting/<service>')
def waiting(service):
    if not session.get('user_id'):
        flash('Faça login para entrar na fila.', 'error')
        return redirect(url_for('login'))
    
    # Add to waiting list (idempotent - won't duplicate)
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], normalize_service_name(service)))
        conn.commit()
        logging.info(f'Adicionado à fila: usuário {session["user_id"]} para {service}')
    except Exception as e:
        logging.error(f'Erro na fila: {e}')
    finally:
        conn.close()
    
    return render_template_string(WAITING_TEMPLATE, service=service)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        if authenticate_user(email, password):
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('home'))
        else:
            flash('Email ou senha incorretos.', 'error')
            logging.warning(f'Tentativa de login falha: {email}')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        success, msg = create_user(name, email, phone, password)
        flash(msg, 'success' if success else 'error')
        if success:
            return redirect(url_for('login'))
    
    return render_template_string(REGISTER_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    flash('Logout realizado.', 'success')
    return redirect(url_for('home'))

# --- 12. Admin Routes ---
@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    users = get_all_users()
    reservations = get_all_reservations()
    pending_res = [r for r in reservations if r['status'] == 'pending']
    waiting = get_waiting_list()
    minis = get_all_minis()
    total_stock = sum(m['quantity'] for m in minis)
    
    return render_template_string(ADMIN_TEMPLATE, users=users, reservations=reservations, pending_res=pending_res, waiting=waiting, total_stock=total_stock)

@app.route('/admin/promote/<int:user_id>', methods=['GET'])
def admin_promote(user_id):
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    success, msg = promote_to_admin(user_id)
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('admin'))

@app.route('/admin/confirm/<int:res_id>', methods=['GET'])
def admin_confirm(res_id):
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    success, msg = confirm_reservation(res_id, session['user_id'])
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('admin'))

@app.route('/admin/reject/<int:res_id>', methods=['POST'])
def admin_reject(res_id):
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    reason = request.form.get('reason', 'Motivo não especificado')
    success, msg = reject_reservation(res_id, session['user_id'], reason)
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('admin'))

@app.route('/admin/sync', methods=['POST'])
def admin_sync():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    sync_stock_from_sheet()
    return redirect(url_for('admin'))

@app.route('/admin/insert_mini', methods=['POST'])
def admin_insert_mini():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    service = request.form['service']
    quantity = int(request.form['quantity'])
    service_norm = normalize_service_name(service)
    update_stock(service_norm, quantity)  # Inserts or updates
    flash(f'Miniatura "{service}" inserida/atualizada com {quantity} unidades.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/insert_reservation', methods=['POST'])
def admin_insert_reservation():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    user_id = int(request.form['user_id'])
    service = request.form['service']
    quantity = int(request.form['quantity'])
    success, msg = create_reservation(user_id, service, quantity)
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('admin'))

@app.route('/admin/backup/json')
def admin_backup_json():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users')
    users_data = [dict(row) for row in c.fetchall()]
    c.execute('SELECT * FROM reservations')
    res_data = [dict(row) for row in c.fetchall()]
    c.execute('SELECT * FROM stock')
    stock_data = [dict(row) for row in c.fetchall()]
    conn.close()
    
    backup = {'users': users_data, 'reservations': res_data, 'stock': stock_data, 'timestamp': datetime.now().isoformat()}
    return send_file(io.BytesIO(json.dumps(backup, indent=2, ensure_ascii=False).encode('utf-8')), 
                     mimetype='application/json', as_attachment=True, download_name=f'jgminis_backup_{datetime.now().strftime("%Y%m%d")}.json')

@app.route('/admin/backup/csv')
def admin_backup_csv():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Users CSV
    writer.writerow(['USERS'])
    c.execute('SELECT * FROM users')
    writer.writerows([list(row) for row in c.fetchall()])
    writer.writerow([])  # Empty row
    
    # Reservations CSV
    writer.writerow(['RESERVATIONS'])
    c.execute('SELECT * FROM reservations')
    writer.writerows([list(row) for row in c.fetchall()])
    writer.writerow([])  # Empty row
    
    # Stock CSV
    writer.writerow(['STOCK'])
    c.execute('SELECT * FROM stock')
    writer.writerows([list(row) for row in c.fetchall()])
    
    conn.close()
    
    return send_file(io.BytesIO(output.getvalue().encode('utf-8')), 
                     mimetype='text/csv', as_attachment=True, download_name=f'jgminis_backup_{datetime.now().strftime("%Y%m%d")}.csv')

# --- 13. Error Handlers (Prevent 500s) ---
@app.errorhandler(404)
def not_found(e):
    return render_template_string('<h1>404 - Página Não Encontrada</h1><a href="/">Voltar</a>'), 404

@app.errorhandler(500)
def internal_error(e):
    logging.error(f'Erro 500: {e}')
    return 'Erro interno no servidor. Tente novamente.', 500

if __name__ == '__main__':
    init_db()  # Ensure DB on startup without loss
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
