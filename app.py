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
            logging.info('gspread auth bem-sucedida - planilha aberta')
        except json.JSONDecodeError as e:
            logging.error(f'Erro no JSON das creds: {e} - Verifique GOOGLE_SHEETS_CREDENTIALS (uma linha só)')
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
app.secret_key = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_jg_minis_v4_3_3_stable')

# --- 3. Environment Variables ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')  # Just numbers, no + or spaces
DATABASE = os.environ.get('DATABASE', '/tmp/jgminis.db')

# --- 4. Database Helper Functions (Preservation Guaranteed) ---
def get_db_connection():
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logging.error(f'Erro na conexão DB: {e}')
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        logging.error('Falha ao obter conexão DB para init_db. Abortando inicialização.')
        return
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
                # Assuming first row is header, skip it if records has more than 1 row
                start_row = 1 if len(records) > 0 and 'NOME DA MINIATURA' in records[0] else 0
                for record in records[start_row:]:
                    service = record.get('NOME DA MINIATURA', '').strip().lower()
                    qty = int(record.get('QUANTIDADE DISPONÍVEL', 0) or 0)
                    if service:
                        c.execute('INSERT OR IGNORE INTO stock (service, quantity) VALUES (?, ?)', (service, qty))
                conn.commit()
                logging.info('Stock inicial sincronizado do Google Sheets (preservando dados existentes)')
            except Exception as e:
                logging.error(f'Erro no sync inicial de stock: {e}')
        elif not sheet:
            logging.warning('Google Sheets não disponível para sync inicial de stock.')

        conn.commit()
        logging.info('DB inicializado sem perda de dados - tabelas preservadas')
    except Exception as e:
        logging.error(f'Erro no init_db: {e}')
        conn.rollback()
    finally:
        conn.close()

# Call init_db() globally to ensure it runs on app startup (Gunicorn)
init_db()

# --- 5. Validation Functions ---
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    cleaned = re.sub(r'[^\d]', '', phone)  # Remove non-digits
    return cleaned.isdigit() and 10 &lt;= len(cleaned) &lt;= 11

def normalize_service_name(name):
    return name.strip().lower()  # Case-insensitive normalization

# --- 6. Stock Management ---
def get_stock(service):
    service_norm = normalize_service_name(service)
    conn = get_db_connection()
    if not conn: return 0
    c = conn.cursor()
    try:
        c.execute('SELECT quantity FROM stock WHERE service = ?', (service_norm,))
        result = c.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logging.error(f'Erro ao obter stock para {service_norm}: {e}')
        return 0
    finally:
        conn.close()

def update_stock(service, delta):
    service_norm = normalize_service_name(service)
    conn = get_db_connection()
    if not conn: return
    c = conn.cursor()
    try:
        c.execute('UPDATE stock SET quantity = quantity + ?, last_sync = CURRENT_TIMESTAMP WHERE service = ?', (delta, service_norm))
        if c.rowcount == 0: # If service not found, insert it
            c.execute('INSERT INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service_norm, max(0, delta)))
        conn.commit()
        logging.info(f'Stock atualizado para {service_norm}: delta {delta}')
    except Exception as e:
        logging.error(f'Erro ao atualizar stock para {service_norm}: {e}')
        conn.rollback()
    finally:
        conn.close()

def sync_stock_from_sheet():
    if not sheet:
        flash('Integração com Google Sheets indisponível - usando dados locais.', 'error')
        return False
    try:
        records = sheet.get_all_records()
        conn = get_db_connection()
        if not conn: return False
        c = conn.cursor()
        # Assuming first row is header, skip it if records has more than 1 row
        start_row = 1 if len(records) > 0 and 'NOME DA MINIATURA' in records[0] else 0
        for record in records[start_row:]:
            service = normalize_service_name(record.get('NOME DA MINIATURA', ''))
            qty = int(record.get('QUANTIDADE DISPONÍVEL', 0) or 0)
            if service:
                c.execute('INSERT OR REPLACE INTO stock (service, quantity, last_sync) VALUES (?, ?, CURRENT_TIMESTAMP)', (service, qty))
        conn.commit()
        conn.close()
        logging.info('Stock sincronizado com sucesso do Google Sheets')
        flash('Stock sincronizado com Google Sheets!', 'success')
        return True
    except Exception as e:
        logging.error(f'Erro no sync de stock: {e}')
        flash('Erro ao sincronizar stock. Usando dados locais.', 'error')
        return False

# --- 7. User Management ---
def create_user(name, email, phone, password):
    if not all([name, email, phone, password]):
        return False, 'Todos os campos são obrigatórios.'
    if not is_valid_email(email):
        return False, 'Email inválido.'
    if not is_valid_phone(phone):
        return False, 'Telefone inválido (10-11 dígitos).'
    if len(password) &lt; 6:
        return False, 'Senha deve ter pelo menos 6 caracteres.'
    
    conn = get_db_connection()
    if not conn: return False, 'Erro interno no DB.'
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
        return True, 'Cadastro realizado com sucesso!'
    except Exception as e:
        logging.error(f'Erro ao criar usuário: {e}')
        conn.rollback()
        return False, 'Erro interno no cadastro. Tente novamente.'
    finally:
        conn.close()

def authenticate_user(email, password):
    conn = get_db_connection()
    if not conn: return False
    c = conn.cursor()
    try:
        c.execute('SELECT id, name, password, role FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['role'] = user['role']
            logging.info(f'Login bem-sucedido: {email}')
            return True
        logging.warning(f'Falha no login: {email}')
        return False
    except Exception as e:
        logging.error(f'Erro na autenticação: {e}')
        return False
    finally:
        conn.close()

def get_user_by_id(user_id):
    conn = get_db_connection()
    if not conn: return None
    c = conn.cursor()
    try:
        c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = c.fetchone()
        return user
    except Exception as e:
        logging.error(f'Erro ao obter usuário {user_id}: {e}')
        return None
    finally:
        conn.close()

def promote_to_admin(user_id):
    conn = get_db_connection()
    if not conn: return False, 'Erro interno no DB.'
    c = conn.cursor()
    try:
        # Verify user exists first
        c.execute('SELECT id FROM users WHERE id = ?', (user_id,))
        if not c.fetchone():
            return False, 'Usuário não encontrado.'
        
        c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
        if c.rowcount > 0:
            conn.commit()
            logging.info(f'Usuário {user_id} promovido a admin')
            return True, 'Usuário promovido a admin com sucesso!'
        else:
            return False, 'Erro ao promover usuário.'
    except Exception as e:
        logging.error(f'Erro ao promover admin: {e}')
        conn.rollback()
        return False, 'Erro interno na promoção.'
    finally:
        conn.close()

# --- 8. Reservation Management ---
def create_reservation(user_id, service, quantity=1):
    service_norm = normalize_service_name(service)
    stock = get_stock(service_norm)
    if stock &lt; quantity:
        # Add to waiting list instead
        conn = get_db_connection()
        if not conn: return False, 'Erro interno no DB.'
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO waiting_list (user_id, service) VALUES (?, ?)', (user_id, service_norm))
            conn.commit()
            logging.info(f'Usuário {user_id} adicionado à fila para {service_norm}')
            return False, f'Estoque insuficiente ({stock} disponível). Você foi adicionado à fila de espera!'
        except Exception as e:
            logging.error(f'Erro na fila de espera: {e}')
            conn.rollback()
            return False, 'Erro ao adicionar à fila de espera.'
        finally:
            conn.close()
    
    conn = get_db_connection()
    if not conn: return False, 'Erro interno no DB.'
    c = conn.cursor()
    try:
        c.execute('INSERT INTO reservations (user_id, service, quantity, status) VALUES (?, ?, ?, ?)', 
                  (user_id, service_norm, quantity, 'pending'))
        reservation_id = c.lastrowid
        # Update stock
        update_stock(service_norm, -quantity)
        conn.commit()
        logging.info(f'Reserva criada: ID {reservation_id} para {service_norm}, qty {quantity}')
        return True, f'Reserva #{reservation_id} criada com sucesso para {quantity} unidade(s)!'
    except Exception as e:
        logging.error(f'Erro ao criar reserva: {e}')
        conn.rollback()
        # Revert stock if needed (but since insert failed, no need)
        return False, 'Erro interno na reserva. Tente novamente.'
    finally:
        conn.close()

def confirm_reservation(reservation_id, admin_id):
    conn = get_db_connection()
    if not conn: return False, 'Erro interno no DB.'
    c = conn.cursor()
    try:
        c.execute('UPDATE reservations SET status = "confirmed", approved_by = ? WHERE id = ? AND status = "pending"', 
                  (admin_id, reservation_id))
        if c.rowcount > 0:
            conn.commit()
            logging.info(f'Reserva {reservation_id} confirmada por admin {admin_id}')
            return True, 'Reserva confirmada!'
        else:
            return False, 'Reserva não encontrada ou já processada.'
    except Exception as e:
        logging.error(f'Erro ao confirmar reserva: {e}')
        conn.rollback()
        return False, 'Erro interno na confirmação.'
    finally:
        conn.close()

def reject_reservation(reservation_id, admin_id, reason):
    conn = get_db_connection()
    if not conn: return False, 'Erro interno no DB.'
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
            return True, 'Reserva rejeitada e estoque revertido!'
        else:
            return False, 'Reserva não encontrada ou já processada.'
    except Exception as e:
        logging.error(f'Erro ao rejeitar reserva: {e}')
        conn.rollback()
        return False, 'Erro interno na rejeição.'
    finally:
        conn.close()

# --- 9. Get All Data for Home/Admin ---
def get_all_minis_data_for_display():
    conn = get_db_connection()
    if not conn: return []
    c = conn.cursor()
    thumbnails = []
    try:
        # Get stock quantities from DB (service in lower case)
        c.execute("SELECT service, quantity FROM stock ORDER BY service")
        stock_data = {row['service']: row['quantity'] for row in c.fetchall()}
        
        # Get other details from Google Sheet
        if sheet:
            records = sheet.get_all_records()
            if not records:
                logging.warning("Planilha vazia - thumbnails fallback")
                return [{'service': 'Fallback', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': 'Adicione dados na planilha', 'marca': '', 'previsao': ''}]
            
            # Assuming first row is header, skip it if records has more than 1 row
            start_row = 1 if len(records) > 0 and 'NOME DA MINIATURA' in records[0] else 0
            for record in records[start_row:]:
                service_raw = record.get('NOME DA MINIATURA', '').strip()
                if not service_raw: continue
                
                service_lower = normalize_service_name(service_raw)
                
                marca = record.get('MARCA/FABRICANTE', '')
                obs = record.get('OBSERVAÇÕES', '')
                image = record.get('IMAGEM', LOGO_URL)
                price_raw = record.get('VALOR', 0)
                previsao = record.get('PREVISÃO DE CHEGADA', '')
                
                price_str = str(price_raw) if price_raw is not None else '0'
                price = price_str.replace('R$ ', '').replace(',', '.')
                try:
                    price = float(price)
                except ValueError:
                    price = 0.0
                
                quantity = stock_data.get(service_lower, 0) 
                
                thumbnails.append({
                    'service': service_raw,
                    'marca': marca,
                    'obs': obs,
                    'image': image,
                    'price': f"{price:.2f}".replace('.', ','),
                    'quantity': quantity,
                    'previsao': previsao
                })
            logging.info(f'Carregados {len(thumbnails)} thumbnails da planilha')
        else:
            # Fallback thumbnails if no sheet
            for service_name, qty in stock_data.items():
                thumbnails.append({
                    'service': service_name.title(),
                    'marca': 'N/A',
                    'obs': 'Dados do DB',
                    'image': LOGO_URL,
                    'price': '0,00',
                    'quantity': qty,
                    'previsao': 'N/A'
                })
            logging.warning('Usando fallback thumbnails - Google Sheets não disponível')
        
        return thumbnails
    except Exception as e:
        logging.error(f'Erro ao carregar thumbnails: {e}')
        return [{'service': 'Erro de Carregamento', 'quantity': 0, 'image': LOGO_URL, 'price': '0,00', 'obs': str(e), 'marca': '', 'previsao': ''}]
    finally:
        conn.close()

def get_all_reservations():
    conn = get_db_connection()
    if not conn: return []
    c = conn.cursor()
    try:
        c.execute('''
            SELECT r.id, r.service, r.quantity, r.status, r.created_at, u.name as user_name, a.name as admin_name, r.denied_reason
            FROM reservations r
            JOIN users u ON r.user_id = u.id
            LEFT JOIN users a ON r.approved_by = a.id
            ORDER BY r.created_at DESC
        ''')
        reservations = c.fetchall()
        return reservations
    except Exception as e:
        logging.error(f'Erro ao obter reservas: {e}')
        return []
    finally:
        conn.close()

def get_all_users():
    conn = get_db_connection()
    if not conn: return []
    c = conn.cursor()
    try:
        c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users ORDER BY data_cadastro DESC')
        users = c.fetchall()
        return users
    except Exception as e:
        logging.error(f'Erro ao obter usuários: {e}')
        return []
    finally:
        conn.close()

def get_waiting_list():
    conn = get_db_connection()
    if not conn: return []
    c = conn.cursor()
    try:
        c.execute('''
            SELECT wl.id, wl.service, wl.created_at, u.name as user_name
            FROM waiting_list wl
            JOIN users u ON wl.user_id = u.id
            ORDER BY wl.created_at ASC
        ''')
        waiting = c.fetchall()
        return waiting
    except Exception as e:
        logging.error(f'Erro ao obter fila de espera: {e}')
        return []
    finally:
        conn.close()

# --- 10. HTML Templates (Inline for Simplicity) ---
HOME_TEMPLATE = '''
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
        .btn-reserve { background-color: #007bff; color: white; border: none; }
        .btn-reserve:hover { background-color: #0056b3; }
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
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-messages">
            {% for category, message in messages %}
                <div class="flash-{{ category }}">{{ message }}</div>
            {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    <nav>
        <a href="/">Home</a>
        {% if session.get('user_id') %}
            {% if session.get('role') == 'admin' %}
                <a href="/admin">Admin</a>
            {% endif %}
            <a href="/logout" style="float: right;">Logout ({{ session.get('user_name', 'Usuário') }})</a>
        {% else %}
            <a href="/login">Login</a>
            <a href="/register">Cadastro</a>
        {% endif %}
    </nav>
    <div class="grid-container">
        {% for thumb in thumbnails %}
            <div class="thumbnail {% if thumb.quantity == 0 %}esgotado{% endif %}">
                <img src="{{ thumb.image }}" alt="{{ thumb.service }}">
                {% if thumb.quantity == 0 %}<div class="esgotado-tag">ESGOTADO</div>{% endif %}
                <div class="thumbnail-content">
                    <h3>{{ thumb.service }}</h3>
                    <p>{{ thumb.marca }} - {{ thumb.obs }}</p>
                    <p class="price">R$ {{ thumb.price }}</p>
                    <p class="quantity">Disponível: {{ thumb.quantity }}</p>
                    <div class="action-buttons">
                        {% if thumb.quantity == 0 %}
                            <a href="/waiting/{{ thumb.service }}" class="btn btn-waiting">Fila de Espera</a>
                            <a href="https://wa.me/{{ whatsapp_number }}?text=Olá, gostaria de saber sobre a fila de espera para {{ thumb.service }}. Meu email: {{ session.get('user_name', 'anônimo') }} ({{ session.get('user_email', 'sem email') }})" class="btn btn-contact">Entrar em Contato</a>
                        {% else %}
                            <a href="/reserve/{{ thumb.service }}" class="btn btn-reserve">Reservar Agora</a>
                        {% endif %}
                    </div>
                </div>
            </div>
        {% endfor %}
    </div>
    <footer>
        <p>&copy; 2025 JG Minis Portal de Reservas. Todos os direitos reservados.</p>
    </footer>
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
        h1 { color: #333; text-align: center; margin-bottom: 20px; }
        .flash-messages { padding: 10px 0; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .mini-details { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }
        .mini-details img { max-width: 80%; height: auto; border-radius: 5px; margin-bottom: 15px; }
        .mini-details p { margin: 5px 0; font-size: 1.1em; }
        .mini-details .price { font-weight: bold; color: #28a745; }
        form { margin-top: 20px; }
        label { display: block; margin: 10px 0 5px; font-weight: bold; color: #333; }
        select { width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .btn:hover { background-color: #0056b3; }
        .back-link { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>Reservar {{ service.title() }}</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-messages">
            {% for category, message in messages %}
                <div class="flash-{{ category }}">{{ message }}</div>
            {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    <div class="mini-details">
        <img src="{{ image_url }}" alt="{{ service.title() }}">
        <p><strong>Marca:</strong> {{ marca }}</p>
        <p><strong>Observações:</strong> {{ obs }}</p>
        <p class="price"><strong>Preço:</strong> R$ {{ price }}</p>
        <p><strong>Disponível:</strong> {{ stock }} unidade(s)</p>
    </div>
    {% if not session.get('user_id') %}
        <p style="text-align: center; margin-top: 20px;">Faça <a href="/login">login</a> para reservar.</p>
    {% else %}
        <form method="POST">
            <label for="quantity">Quantidade:</label>
            <select name="quantity" id="quantity">
                {% for q in range(1, stock + 1) %}
                    <option value="{{ q }}">{{ q }}</option>
                {% endfor %}
            </select>
            <button type="submit" class="btn">Confirmar Reserva</button>
        </form>
        <a href="/" class="back-link">Voltar à Home</a>
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
        body { font-family: Arial, sans-serif; margin: 20px; background: #f4f4f4; color: #333; }
        h1, h2 { color: #333; margin-top: 25px; margin-bottom: 15px; }
        .flash-messages { padding: 10px 0; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        table { width: 100%; border-collapse: collapse; margin: 20px 0; background: white; box-shadow: 0 2px 5px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; color: #333; }
        th { background: #e9ecef; font-weight: bold; }
        .btn { display: inline-block; padding: 8px 12px; margin: 4px; border-radius: 5px; text-decoration: none; font-weight: bold; transition: background-color 0.3s ease; border: none; cursor: pointer; color: white; }
        .btn-home { background: #007bff; } .btn-home:hover { background: #0056b3; }
        .btn-logout { background: #dc3545; } .btn-logout:hover { background: #c82333; }
        .btn-confirm { background: #28a745; } .btn-confirm:hover { background: #218838; }
        .btn-reject { background: #dc3545; } .btn-reject:hover { background: #c82333; }
        .btn-promote { background: #ffc107; color: #212529; } .btn-promote:hover { background: #e0a800; }
        .btn-sync { background: #17a2b8; } .btn-sync:hover { background: #138496; }
        .btn-backup { background: #6c757d; } .btn-backup:hover { background: #5a6268; }
        .btn-insert { background: #007bff; } .btn-insert:hover { background: #0056b3; }
        form { background: white; padding: 20px; border-radius: 8px; margin: 15px 0; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
        input[type="text"], input[type="number"], select, textarea { width: calc(100% - 22px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 5px; }
        button[type="submit"] { padding: 10px 15px; border-radius: 5px; border: none; background: #007bff; color: white; cursor: pointer; font-weight: bold; }
        .action-form { display: inline-block; margin: 0; }
    </style>
</head>
<body>
    <h1>Painel Admin - JG Minis</h1>
    <a href="/" class="btn btn-home">Home</a> 
    <a href="/logout" class="btn btn-logout">Logout ({{ session.get('user_name', 'Admin') }})</a>

    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-messages">
            {% for category, message in messages %}
                <div class="flash-{{ category }}">{{ message }}</div>
            {% endfor %}
            </div>
        {% endif %}
    {% endwith %}

    <h2>Gerenciar Usuários</h2>
    <table>
        <thead>
            <tr><th>ID</th><th>Nome</th><th>Email</th><th>Telefone</th><th>Role</th><th>Data Cadastro</th><th>Ações</th></tr>
        </thead>
        <tbody>
            {% for user in users %}
                <tr>
                    <td>{{ user['id'] }}</td>
                    <td>{{ user['name'] }}</td>
                    <td>{{ user['email'] }}</td>
                    <td>{{ user['phone'] }}</td>
                    <td>{{ user['role'] }}</td>
                    <td>{{ user['data_cadastro'] }}</td>
                    <td>
                        {% if user['role'] != 'admin' %}
                            <a href="/admin/promote/{{ user['id'] }}" class="btn btn-promote">Promover Admin</a>
                        {% endif %}
                    </td>
                </tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Gerenciar Reservas</h2>
    <table>
        <thead>
            <tr><th>ID</th><th>Serviço</th><th>Usuário</th><th>Quantidade</th><th>Status</th><th>Data</th><th>Razão Rejeição</th><th>Ações</th></tr>
        </thead>
        <tbody>
            {% for res in reservations %}
                <tr>
                    <td>{{ res['id'] }}</td>
                    <td>{{ res['service'].title() }}</td>
                    <td>{{ res['user_name'] }}</td>
                    <td>{{ res['quantity'] }}</td>
                    <td>{{ res['status'] }}</td>
                    <td>{{ res['created_at'] }}</td>
                    <td>{{ res['denied_reason'] if res['denied_reason'] else 'N/A' }}</td>
                    <td>
                        {% if res['status'] == 'pending' %}
                            <a href="/admin/confirm/{{ res['id'] }}" class="btn btn-confirm">Confirmar Reserva</a>
                            <form method="POST" action="/admin/reject/{{ res['id'] }}" class="action-form">
                                <input type="text" name="reason" placeholder="Motivo da rejeição" required style="width: auto; margin-right: 5px;">
                                <button type="submit" class="btn btn-reject">Rejeitar Reserva</button>
                            </form>
                        {% endif %}
                    </td>
                </tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Fila de Espera</h2>
    <table>
        <thead>
            <tr><th>ID</th><th>Serviço</th><th>Usuário</th><th>Data</th></tr>
        </thead>
        <tbody>
            {% for wait in waiting %}
                <tr>
                    <td>{{ wait['id'] }}</td>
                    <td>{{ wait['service'].title() }}</td>
                    <td>{{ wait['user_name'] }}</td>
                    <td>{{ wait['created_at'] }}</td>
                </tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Ações Admin</h2>
    <form method="POST" action="/admin/sync" class="action-form">
        <button type="submit" class="btn btn-sync">Sincronizar Stock (Google Sheets)</button>
    </form>
    <a href="/admin/backup/json" class="btn btn-backup">Backup JSON</a>
    <a href="/admin/backup/csv" class="btn btn-backup">Backup CSV</a>

    <h2>Inserir Nova Miniatura/Estoque</h2>
    <form method="POST" action="/admin/insert_mini">
        <input type="text" name="service" placeholder="Nome da Miniatura" required>
        <input type="number" name="quantity" placeholder="Quantidade" required>
        <button type="submit" class="btn btn-insert">Inserir/Atualizar</button>
    </form>

    <h2>Inserir Nova Reserva (Teste)</h2>
    <form method="POST" action="/admin/insert_reservation">
        <input type="number" name="user_id" placeholder="ID Usuário" required>
        <input type="text" name="service" placeholder="Nome da Miniatura" required>
        <input type="number" name="quantity" value="1" required>
        <button type="submit" class="btn btn-insert">Inserir Reserva</button>
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
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        input { width: calc(100% - 22px); padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #007bff; color: white; padding: 10px; width: 100%; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .btn:hover { background-color: #0056b3; }
        .flash-messages { padding: 10px 0; text-align: center; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        p { text-align: center; margin-top: 15px; }
        p a { color: #007bff; text-decoration: none; }
        p a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h2>Login</h2>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-messages">
            {% for category, message in messages %}
                <div class="flash-{{ category }}">{{ message }}</div>
            {% endfor %}
            </div>
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
        h2 { text-align: center; color: #333; margin-bottom: 20px; }
        form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        input { width: calc(100% - 22px); padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #28a745; color: white; padding: 10px; width: 100%; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; transition: background-color 0.3s ease; }
        .btn:hover { background-color: #218838; }
        .flash-messages { padding: 10px 0; text-align: center; }
        .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 5px; padding: 8px; margin-bottom: 10px; }
        p { text-align: center; margin-top: 15px; }
        p a { color: #007bff; text-decoration: none; }
        p a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h2>Cadastro</h2>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-messages">
            {% for category, message in messages %}
                <div class="flash-{{ category }}">{{ message }}</div>
            {% endfor %}
            </div>
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
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; background: #f4f4f4; color: #333; text-align: center; }
        h1 { color: #333; margin-bottom: 20px; }
        p { font-size: 1.1em; margin-bottom: 30px; }
        .btn { background: #ffc107; color: #212529; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; transition: background-color 0.3s ease; }
        .btn:hover { background-color: #e0a800; }
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
@app.route('/')
def home():
    try:
        thumbnails = get_all_minis_data_for_display()
        return render_template_string(HOME_TEMPLATE, thumbnails=thumbnails, logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER)
    except Exception as e:
        logging.error(f'Erro na home: {e}')
        flash('Erro ao carregar a página inicial. Tente novamente mais tarde.', 'error')
        return render_template_string(HOME_TEMPLATE, thumbnails=[], logo_url=LOGO_URL, whatsapp_number=WHATSAPP_NUMBER), 500

@app.route('/reserve/<service>', methods=['GET', 'POST'])
def reserve(service):
    service_norm = normalize_service_name(service)
    
    # Get full details for display
    mini_details = next((item for item in get_all_minis_data_for_display() if normalize_service_name(item['service']) == service_norm), None)
    if not mini_details:
        flash(f'Miniatura "{service}" não encontrada.', 'error')
        return redirect(url_for('home'))

    stock = mini_details['quantity']
    image_url = mini_details['image']
    marca = mini_details['marca']
    obs = mini_details['obs']
    price = mini_details['price']

    if stock == 0:
        flash(f'Estoque esgotado para "{service}". Você foi adicionado à fila de espera.', 'error')
        logging.info(f'Redirect de reserva para fila de espera para {service_norm} - estoque 0')
        return redirect(url_for('waiting', service=service_norm))
    
    if not session.get('user_id'):
        flash('Faça login para reservar.', 'error')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            quantity = int(request.form.get('quantity', 1))
            if quantity &lt;= 0:
                flash('Quantidade inválida.', 'error')
                return render_template_string(RESERVE_TEMPLATE, service=service, stock=stock, image_url=image_url, marca=marca, obs=obs, price=price)
            if quantity > stock:
                flash(f'Quantidade excede o estoque disponível. Máximo: {stock}', 'error')
                return render_template_string(RESERVE_TEMPLATE, service=service, stock=stock, image_url=image_url, marca=marca, obs=obs, price=price)
            
            success, msg = create_reservation(session['user_id'], service, quantity)
            flash(msg, 'success' if success else 'error')
            if success:
                logging.info(f'Reserva processada com sucesso para {service_norm}')
                return redirect(url_for('home'))
            else:
                logging.warning(f'Falha na reserva para {service_norm}: {msg}')
        except ValueError:
            flash('Quantidade deve ser um número válido.', 'error')
        except Exception as e:
            logging.error(f'Erro inesperado na reserva: {e}')
            flash('Ocorreu um erro inesperado ao processar sua reserva.', 'error')
    
    return render_template_string(RESERVE_TEMPLATE, service=service, stock=stock, image_url=image_url, marca=marca, obs=obs, price=price)

@app.route('/waiting/<service>')
def waiting(service):
    service_norm = normalize_service_name(service)
    if not session.get('user_id'):
        flash('Faça login para entrar na fila.', 'error')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    if not conn:
        flash('Erro interno no DB. Não foi possível adicionar à fila.', 'error')
        return redirect(url_for('home'))
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO waiting_list (user_id, service) VALUES (?, ?)', (session['user_id'], service_norm))
        conn.commit()
        logging.info(f'Adicionado à fila: usuário {session["user_id"]} para {service_norm}')
        flash(f'Você foi adicionado à fila de espera para "{service.title()}".', 'success')
    except Exception as e:
        logging.error(f'Erro na fila de espera: {e}')
        flash('Erro ao adicionar à fila de espera.', 'error')
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
    waiting = get_waiting_list()
    
    return render_template_string(ADMIN_TEMPLATE, users=users, reservations=reservations, waiting=waiting)

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
    try:
        quantity = int(request.form['quantity'])
        if quantity &lt; 0:
            flash('Quantidade não pode ser negativa.', 'error')
            return redirect(url_for('admin'))
        service_norm = normalize_service_name(service)
        update_stock(service_norm, quantity - get_stock(service_norm)) # Adjust stock to new quantity
        flash(f'Miniatura "{service}" inserida/atualizada com {quantity} unidades.', 'success')
    except ValueError:
        flash('Quantidade deve ser um número válido.', 'error')
    except Exception as e:
        logging.error(f'Erro ao inserir/atualizar miniatura: {e}')
        flash('Erro ao inserir/atualizar miniatura.', 'error')
    return redirect(url_for('admin'))

@app.route('/admin/insert_reservation', methods=['POST'])
def admin_insert_reservation():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    try:
        user_id = int(request.form['user_id'])
        service = request.form['service']
        quantity = int(request.form['quantity'])
        if quantity &lt;= 0:
            flash('Quantidade deve ser positiva.', 'error')
            return redirect(url_for('admin'))
        
        # Check if user exists
        if not get_user_by_id(user_id):
            flash(f'Usuário com ID {user_id} não encontrado.', 'error')
            return redirect(url_for('admin'))

        success, msg = create_reservation(user_id, service, quantity)
        flash(msg, 'success' if success else 'error')
    except ValueError:
        flash('ID do usuário e quantidade devem ser números válidos.', 'error')
    except Exception as e:
        logging.error(f'Erro ao inserir reserva via admin: {e}')
        flash('Erro ao inserir reserva.', 'error')
    return redirect(url_for('admin'))

@app.route('/admin/backup/json')
def admin_backup_json():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    conn = get_db_connection()
    if not conn:
        flash('Erro interno no DB para backup.', 'error')
        return redirect(url_for('admin'))
    c = conn.cursor()
    try:
        c.execute('SELECT * FROM users')
        users_data = [dict(row) for row in c.fetchall()]
        c.execute('SELECT * FROM reservations')
        res_data = [dict(row) for row in c.fetchall()]
        c.execute('SELECT * FROM stock')
        stock_data = [dict(row) for row in c.fetchall()]
        c.execute('SELECT * FROM waiting_list')
        waiting_data = [dict(row) for row in c.fetchall()]
        
        backup = {'users': users_data, 'reservations': res_data, 'stock': stock_data, 'waiting_list': waiting_data, 'timestamp': datetime.now().isoformat()}
        return send_file(io.BytesIO(json.dumps(backup, indent=2, ensure_ascii=False).encode('utf-8')), 
                         mimetype='application/json', as_attachment=True, download_name=f'jgminis_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
    except Exception as e:
        logging.error(f'Erro ao gerar backup JSON: {e}')
        flash('Erro ao gerar backup JSON.', 'error')
        return redirect(url_for('admin'))
    finally:
        conn.close()

@app.route('/admin/backup/csv')
def admin_backup_csv():
    if session.get('role') != 'admin':
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    conn = get_db_connection()
    if not conn:
        flash('Erro interno no DB para backup CSV.', 'error')
        return redirect(url_for('admin'))
    c = conn.cursor()
    
    try:
        # Users CSV
        writer.writerow(['USERS'])
        c.execute('SELECT id, name, email, phone, role, data_cadastro FROM users')
        writer.writerow([description[0] for description in c.description]) # Header
        writer.writerows([list(row) for row in c.fetchall()])
        writer.writerow([])  # Empty row
        
        # Reservations CSV
        writer.writerow(['RESERVATIONS'])
        c.execute('SELECT id, user_id, service, quantity, status, approved_by, denied_reason, created_at FROM reservations')
        writer.writerow([description[0] for description in c.description]) # Header
        writer.writerows([list(row) for row in c.fetchall()])
        writer.writerow([])  # Empty row
        
        # Stock CSV
        writer.writerow(['STOCK'])
        c.execute('SELECT id, service, quantity, last_sync FROM stock')
        writer.writerow([description[0] for description in c.description]) # Header
        writer.writerows([list(row) for row in c.fetchall()])
        writer.writerow([]) # Empty row

        # Waiting List CSV
        writer.writerow(['WAITING_LIST'])
        c.execute('SELECT id, user_id, service, created_at FROM waiting_list')
        writer.writerow([description[0] for description in c.description]) # Header
        writer.writerows([list(row) for row in c.fetchall()])
        
        return send_file(io.BytesIO(output.getvalue().encode('utf-8')), 
                         mimetype='text/csv', as_attachment=True, download_name=f'jgminis_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
    except Exception as e:
        logging.error(f'Erro ao gerar backup CSV: {e}')
        flash('Erro ao gerar backup CSV.', 'error')
        return redirect(url_for('admin'))
    finally:
        conn.close()

# --- 13. Error Handlers (Prevent 500s) ---
@app.errorhandler(404)
def not_found(e):
    logging.warning(f'404 Not Found: {request.url}')
    flash('Página não encontrada.', 'error')
    return redirect(url_for('home')), 404

@app.errorhandler(500)
def internal_error(e):
    logging.critical(f'Erro 500 Interno: {e}', exc_info=True)
    flash('Ocorreu um erro interno no servidor. Por favor, tente novamente mais tarde.', 'error')
    return redirect(url_for('home')), 500

if __name__ == '__main__':
    # When running locally, ensure DB is initialized
    # In Railway, init_db() is called globally when the module is imported by Gunicorn
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
