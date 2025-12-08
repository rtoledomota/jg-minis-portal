# Fix SQLite para Cloudflare Pages
try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

import json
from flask import Flask, request, redirect, url_for, session, render_template_string, flash, send_file
from functools import wraps
import os
import bcrypt
import re
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from io import BytesIO
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sua_chave_secreta_aqui')

# 📁 Configurações de Persistência e Caminhos
PERSIST_DIR = Path("/tmp") / "JG_MINIS_PERSIST_v4"
PERSIST_DIR.mkdir(parents=True, exist_ok=True) # Garante que o diretório existe

DB_FILE = PERSIST_DIR / "database.db"
BACKUP_FILE = PERSIST_DIR / "backup_v4.json"

# 🌐 Variáveis de Ambiente e URLs
WHATSAPP_NUMERO = os.environ.get('WHATSAPP_NUMERO', '5511999999999')
GOOGLE_SHEETS_ID = os.environ.get('GOOGLE_SHEETS_ID', '1sxlvo6j-UTB0xXuyivzWnhRuYvpJFcH2smL4ZzHTUps') # Planilha principal de miniaturas
BACKUP_SHEETS_ID = os.environ.get('BACKUP_SHEETS_ID', '1avMoEA0WddQ7dW92X2NORo-cJSwJb7cpinjsuZMMZqI') # Planilha de backup
GOOGLE_SHEETS_SHEET = 'Miniaturas'
BACKUP_RESERVAS_SHEET = 'Backups_Reservas'
BACKUP_USUARIOS_SHEET = 'Usuarios_Backup'
LOGO_URL = "https://i.imgur.com/Yp1OiWB.jpeg" # URL do logo

# 📧 Configurações de Email (com fallback para simulação se credenciais ausentes)
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', 'seu_email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', 'sua_senha_app')
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))

print(f"🚀 JG MINIS v4.2 - Iniciando aplicação com DB persistente em: {DB_FILE}")

# --- Funções de Banco de Dados e Persistência ---

def get_db_connection():
    """Retorna uma conexão com o banco de dados SQLite persistente."""
    return sqlite3.connect(str(DB_FILE))

def create_json_backup():
    """Cria um backup completo do banco de dados em formato JSON.
    Aciona backups para Excel e Google Sheets.
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        backup_data = {
            'timestamp': datetime.now().isoformat(),
            'users': c.execute("SELECT * FROM users").fetchall(),
            'miniaturas': c.execute("SELECT * FROM miniaturas").fetchall(),
            'reservations': c.execute("SELECT * FROM reservations").fetchall(),
            'waitlist': c.execute("SELECT * FROM waitlist").fetchall()
        }
        
        with open(str(BACKUP_FILE), 'w') as f:
            json.dump(backup_data, f, default=str)
        
        conn.close()
        print(f"💾 JSON Backup criado: {len(backup_data['reservations'])} reservas")
        
        # Aciona backups extras
        export_to_excel_reservas() # Gera um buffer de Excel (não salva em disco)
        backup_to_google() # Envia para Google Sheets
        sync_to_sheets() # Sincroniza com a planilha principal de miniaturas
        return True
    except Exception as e:
        print(f"⚠️ Erro no JSON backup: {e}")
        return False

def restore_backup():
    """Restaura o banco de dados a partir do último backup JSON na inicialização.
    Preserva o usuário admin.
    """
    if BACKUP_FILE.exists():
        try:
            with open(str(BACKUP_FILE), 'r') as f:
                backup_data = json.load(f)
            
            conn = get_db_connection()
            c = conn.cursor()
            
            # Limpa tabelas, mas preserva o admin
            c.execute("DELETE FROM waitlist")
            c.execute("DELETE FROM reservations")
            c.execute("DELETE FROM miniaturas")
            c.execute("DELETE FROM users WHERE email != 'admin@jgminis.com.br'")
            
            # Restaura dados
            for user in backup_data.get('users', []):
                if user[2] != 'admin@jgminis.com.br': # Não sobrescreve o admin
                    c.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?)", user)
            
            for mini in backup_data.get('miniaturas', []):
                # Compatibilidade: Adiciona 'created_at' se o backup for antigo
                if len(mini) == 8:
                    mini = (*mini, datetime.now().isoformat())
                c.execute("INSERT OR REPLACE INTO miniaturas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", mini)
            
            for res in backup_data.get('reservations', []):
                c.execute("INSERT OR REPLACE INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?)", res)
            
            for wl in backup_data.get('waitlist', []):
                c.execute("INSERT OR REPLACE INTO waitlist VALUES (?, ?, ?, ?, ?, ?)", wl)
            
            conn.commit()
            conn.close()
            
            num_reservas = len(backup_data.get('reservations', []))
            print(f"✅ Restaurado: {num_reservas} reservas do backup")
            
            # Remove o backup após restauração para evitar restaurações duplicadas
            BACKUP_FILE.unlink()
            return True
        except Exception as e:
            print(f"⚠️ Erro na restauração: {e}")
            return False
    return False

def init_db():
    """Inicializa o esquema do banco de dados, criando tabelas se não existirem."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT, password TEXT NOT NULL, is_admin INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS miniaturas (id INTEGER PRIMARY KEY AUTOINCREMENT, image_url TEXT NOT NULL, name TEXT NOT NULL, arrival_date TEXT, stock INTEGER DEFAULT 0, price REAL DEFAULT 0.0, observations TEXT, max_reservations_per_user INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reservations (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, miniatura_id INTEGER NOT NULL, quantity INTEGER NOT NULL, reservation_date TEXT NOT NULL, status TEXT DEFAULT 'confirmed', confirmacao_enviada INTEGER DEFAULT 0, FOREIGN KEY (user_id) REFERENCES users(id), FOREIGN KEY (miniatura_id) REFERENCES miniaturas(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS waitlist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, miniatura_id INTEGER NOT NULL, email TEXT NOT NULL, notification_sent INTEGER DEFAULT 0, request_date TEXT NOT NULL, FOREIGN KEY (user_id) REFERENCES users(id), FOREIGN KEY (miniatura_id) REFERENCES miniaturas(id))''')
    conn.commit()
    conn.close()
    print("✅ BD inicializado com persistência e created_at para ordenação")

def load_initial_data():
    """Carrega dados iniciais (usuário admin e usuário de teste) se não existirem."""
    conn = get_db_connection()
    c = conn.cursor()
    
    # Adiciona usuário admin
    c.execute("SELECT * FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)", ('Admin', 'admin@jgminis.com.br', '5511999999999', hashed_password, 1))
        print("✅ Usuário admin adicionado.")
    
    # Adiciona usuário de teste
    c.execute("SELECT * FROM users WHERE email = 'usuario@example.com'")
    if not c.fetchone():
        hashed_password = bcrypt.hashpw('usuario123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)", ('Usuário Teste', 'usuario@example.com', '5511988888888', hashed_password, 0))
        print("✅ Usuário teste adicionado.")
    
    conn.commit()
    conn.close()

# --- Funções de Google Sheets ---

def get_google_sheets():
    """Autentica e retorna um cliente gspread para acesso ao Google Sheets."""
    try:
        credentials_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS', '{}')
        credentials_dict = json.loads(credentials_json)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
        gc = gspread.authorize(credentials)
        return gc
    except Exception as e:
        print(f"⚠️ Erro ao conectar ao Google Sheets: {e}")
        return None

def load_miniaturas_from_sheets():
    """Carrega miniaturas da planilha principal do Google Sheets para o banco de dados.
    Limpa miniaturas existentes antes de carregar.
    """
    gc = get_google_sheets()
    if not gc:
        print("⚠️ Não foi possível conectar ao Google Sheets para carregar miniaturas.")
        return
    try:
        sheet = gc.open_by_key(GOOGLE_SHEETS_ID).worksheet(GOOGLE_SHEETS_SHEET)
        rows = sheet.get_all_values()
        if not rows or len(rows) <= 1:
            print("Planilha de miniaturas vazia ou sem dados.")
            return
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM miniaturas") # Limpa miniaturas existentes
        
        for i, row in enumerate(rows[1:]): # Ignora o cabeçalho
            if len(row) >= 8:
                try:
                    image_url = row[0]
                    name = row[1]
                    arrival_date = row[3]
                    stock = int(row[4]) if row[4].isdigit() else 0
                    price = float(row[5].replace(',', '.')) if row[5].replace(',', '.').replace('.', '', 1).isdigit() else 0.0
                    observations = row[6]
                    max_reservations = int(row[7]) if row[7].isdigit() else 1
                    created_at = datetime.now().isoformat() # Adiciona created_at ao carregar
                    
                    c.execute("INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (image_url, name, arrival_date, stock, price, observations, max_reservations, created_at))
                except Exception as e:
                    print(f"⚠️ Erro ao inserir linha {i+2} da planilha: {row} - {e}")
                    continue
        conn.commit()
        conn.close()
        print(f"✅ {len(rows)-1} miniaturas carregadas do Google Sheets.")
        create_json_backup() # Cria backup após carregar miniaturas
    except Exception as e:
        print(f"⚠️ Erro ao carregar miniaturas da planilha: {e}")

def backup_to_google():
    """Realiza backup automático de reservas e usuários para a planilha de backup do Google Sheets."""
    gc = get_google_sheets()
    if not gc:
        print("⚠️ Sem credenciais Google - Backup para Sheets pulado.")
        return False
    
    try:
        sheet = gc.open_by_key(BACKUP_SHEETS_ID)
        
        # --- Backup de Reservas ---
        try:
            ws_reservas = sheet.worksheet(BACKUP_RESERVAS_SHEET)
        except gspread.WorksheetNotFound:
            ws_reservas = sheet.add_worksheet(title=BACKUP_RESERVAS_SHEET, rows=1000, cols=7)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''SELECT u.name, m.name, r.quantity, r.reservation_date, r.status, m.price, (r.quantity * m.price)
                     FROM reservations r JOIN users u ON r.user_id = u.id JOIN miniaturas m ON r.miniatura_id = m.id
                     ORDER BY r.reservation_date DESC''')
        reservas = c.fetchall()
        conn.close()
        
        if reservas:
            ws_reservas.clear()
            ws_reservas.append_row(['Usuário', 'Miniatura', 'Quantidade', 'Data Reserva', 'Status', 'Preço Unitário', 'Total'])
            ws_reservas.append_rows(reservas)
            # Formatação do cabeçalho
            ws_reservas.format('A1:G1', {"backgroundColor": {"red": 0.23, "green": 0.51, "blue": 0.96}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
            print(f"✅ Backup Google Reservas: {len(reservas)} linhas atualizadas na planilha.")
        
        # --- Backup de Usuários ---
        try:
            ws_usuarios = sheet.worksheet(BACKUP_USUARIOS_SHEET)
        except gspread.WorksheetNotFound:
            ws_usuarios = sheet.add_worksheet(title=BACKUP_USUARIOS_SHEET, rows=1000, cols=4)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT name, email, phone, is_admin FROM users ORDER BY name')
        usuarios = c.fetchall()
        conn.close()
        
        if usuarios:
            ws_usuarios.clear()
            ws_usuarios.append_row(['Nome', 'Email', 'Telefone', 'Admin'])
            ws_usuarios.append_rows([[u[0], u[1], u[2], 'Sim' if u[3] else 'Não'] for u in usuarios])
            ws_usuarios.format('A1:D1', {"backgroundColor": {"red": 0.55, "green": 0.36, "blue": 0.96}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
            print(f"✅ Backup Google Usuários: {len(usuarios)} linhas atualizadas.")
        
        return True
    except Exception as e:
        print(f"⚠️ Erro no backup Google: {e}")
        return False

def sync_to_sheets():
    """Sincroniza o estado atual das miniaturas do BD para a planilha principal do Google Sheets."""
    gc = get_google_sheets()
    if not gc:
        print("⚠️ Sem credenciais Google - Sincronização Sheets pulada.")
        return False
    
    try:
        sheet = gc.open_by_key(GOOGLE_SHEETS_ID)
        ws = sheet.worksheet(GOOGLE_SHEETS_SHEET)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas')
        miniaturas = c.fetchall()
        conn.close()
        
        if miniaturas:
            ws.clear()
            # Cabeçalhos da planilha principal
            ws.append_row(['Image URL', 'Nome', '', 'Arrival Date', 'Stock', 'Price', 'Observations', 'Max Reservations'])
            # Adiciona os dados das miniaturas
            ws.append_rows([[m[0], m[1], '', m[2], m[3], m[4], m[5], m[6]] for m in miniaturas])
            print(f"✅ Sincronização Sheets: {len(miniaturas)} miniaturas atualizadas na planilha principal.")
        
        return True
    except Exception as e:
        print(f"⚠️ Erro na sincronização Sheets: {e}")
        return False

# --- Funções de Email ---

def enviar_email(destinatario, assunto, corpo_html):
    """Envia um email HTML para o destinatário. Usa simulação se credenciais não configuradas."""
    try:
        if EMAIL_SENDER == 'seu_email@gmail.com' or not EMAIL_PASSWORD:
            print(f"📧 SIMULAÇÃO: Email para {destinatario} - Assunto: {assunto}")
            return True
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = assunto
        msg['From'] = EMAIL_SENDER
        msg['To'] = destinatario
        
        parte_html = MIMEText(corpo_html, 'html', 'utf-8')
        msg.attach(parte_html)
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"✅ Email enviado para {destinatario}")
        return True
    except Exception as e:
        print(f"⚠️ Erro ao enviar email para {destinatario}: {e}")
        return False

# --- Decoradores de Autenticação e Autorização ---

def login_required(f):
    """Decorador para rotas que exigem login."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa estar logado para acessar esta página.', 'error')
            return redirect(url_for('login'))
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT id, name, email, phone, is_admin FROM users WHERE id = ?', (session['user_id'],))
        user = c.fetchone()
        conn.close()
        
        if user:
            request.user = {'user_id': user[0], 'name': user[1], 'email': user[2], 'phone': user[3], 'is_admin': bool(user[4])}
        else:
            session.pop('user_id', None) # Remove sessão inválida
            flash('Sua sessão expirou ou é inválida. Faça login novamente.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorador para rotas que exigem permissões de administrador."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not request.user.get('is_admin'):
            flash('Acesso negado: Você não tem permissões de administrador.', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# --- Rotas de Autenticação ---

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Rota para registro de novos usuários."""
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Formato de e-mail inválido.', 'error')
            return redirect(url_for('register'))
        if password != confirm_password:
            flash('As senhas não coincidem.', 'error')
            return redirect(url_for('register'))
        if len(password) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'error')
            return redirect(url_for('register'))
        
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)", (name, email, phone, hashed_password))
            conn.commit()
            create_json_backup() # Backup após novo usuário
            flash('Registro bem-sucedido! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('E-mail já registrado.', 'error')
        finally:
            conn.close()
            
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Registrar - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center">
        <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-md w-full">
            <div class="flex justify-center mb-6">
                <img src="''' + LOGO_URL + '''" class="h-16 rounded-full border-2 border-blue-400" alt="JG MINIS">
            </div>
            <h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Registrar</h2>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <ul class="mb-4">
                        {% for category, message in messages %}
                            <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                        {% endfor %}
                    </ul>
                {% endif %}
            {% endwith %}
            <form method="POST" action="/register" class="space-y-4">
                <div>
                    <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                    <input type="text" id="name" name="name" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <div>
                    <label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label>
                    <input type="email" id="email" name="email" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <div>
                    <label for="phone" class="block text-slate-300 font-bold mb-1">Telefone:</label>
                    <input type="text" id="phone" name="phone" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <div>
                    <label for="password" class="block text-slate-300 font-bold mb-1">Senha:</label>
                    <input type="password" id="password" name="password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <div>
                    <label for="confirm_password" class="block text-slate-300 font-bold mb-1">Confirmar Senha:</label>
                    <input type="password" id="confirm_password" name="confirm_password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Registrar</button>
            </form>
            <p class="text-center text-slate-400 mt-6">Já tem uma conta? <a href="/login" class="text-blue-400 hover:underline">Faça Login</a></p>
        </div>
    </body>
    </html>
    ''')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Rota para login de usuários."""
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT id, password FROM users WHERE email = ?', (email,))
        user_data = c.fetchone()
        conn.close()
        if user_data and bcrypt.checkpw(password.encode('utf-8'), user_data[1].encode('utf-8')):
            session['user_id'] = user_data[0]
            flash('Login bem-sucedido!', 'success')
            return redirect(url_for('index'))
        else:
            flash('E-mail ou senha inválidos.', 'error')
            
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center">
        <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-md w-full">
            <div class="flex justify-center mb-6">
                <img src="''' + LOGO_URL + '''" class="h-16 rounded-full border-2 border-blue-400" alt="JG MINIS">
            </div>
            <h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Login</h2>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <ul class="mb-4">
                        {% for category, message in messages %}
                            <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                        {% endfor %}
                    </ul>
                {% endif %}
            {% endwith %}
            <form method="POST" action="/login" class="space-y-4">
                <div>
                    <label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label>
                    <input type="email" id="email" name="email" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <div>
                    <label for="password" class="block text-slate-300 font-bold mb-1">Senha:</label>
                    <input type="password" id="password" name="password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                </div>
                <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Login</button>
            </form>
            <p class="text-center text-slate-400 mt-6">Não tem conta? <a href="/register" class="text-blue-400 hover:underline">Registre-se</a></p>
        </div>
    </body>
    </html>
    ''')

@app.route('/logout')
@login_required
def logout():
    """Rota para logout de usuários."""
    session.pop('user_id', None)
    flash('Logout realizado com sucesso!', 'success')
    return redirect(url_for('login'))

# --- Rotas de Usuário ---

@app.route('/', defaults={'sort': 'name', 'order': 'asc'})
@app.route('/<sort>/<order>')
@login_required
def index(sort, order):
    """Rota principal: Catálogo de miniaturas com ordenação dinâmica."""
    conn = get_db_connection()
    c = conn.cursor()
    
    valid_sorts = ['name', 'arrival_date', 'created_at', 'stock']
    if sort not in valid_sorts:
        sort = 'name' # Default
    
    valid_orders = ['asc', 'desc']
    if order not in valid_orders:
        order = 'asc' # Default
        
    order_by = f"{sort} {'ASC' if order == 'asc' else 'DESC'}"
    
    # Consulta miniaturas
    c.execute(f"SELECT id, image_url, name, arrival_date, stock, price, observations, max_reservations_per_user, created_at FROM miniaturas ORDER BY {order_by}")
    miniaturas = c.fetchall()
    conn.close()
    
    # Opções de ordenação para o dropdown
    sort_options = [
        ('name', 'Nome'), 
        ('arrival_date', 'Previsão de Chegada'), 
        ('created_at', 'Data de Inclusão'), 
        ('stock', 'Disponibilidade')
    ]
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Catálogo - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/minhas-reservas" class="text-slate-300 hover:text-blue-400">Minhas Reservas</a>
                    <a href="/perfil" class="text-slate-300 hover:text-blue-400">Perfil</a>
                    {% if request.user.is_admin %}
                        <a href="/admin" class="text-slate-300 hover:text-blue-400">Admin</a>
                    {% endif %}
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6">
            <h2 class="text-3xl font-black text-white mb-6">Catálogo de Miniaturas</h2>
            
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <ul class="mb-4">
                        {% for category, message in messages %}
                            <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                        {% endfor %}
                    </ul>
                {% endif %}
            {% endwith %}

            <!-- Dropdowns de Ordenação -->
            <div class="bg-slate-800 rounded-lg p-4 mb-6 flex flex-wrap gap-4 items-center">
                <label for="sort_by" class="text-white font-bold">Ordenar por:</label>
                <select id="sort_by" onchange="updateSort()" class="bg-slate-700 text-white px-4 py-2 rounded-lg border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    {% for s_val, s_label in sort_options %}
                        <option value="{{ s_val }}" {% if s_val == sort %}selected{% endif %}>{{ s_label }}</option>
                    {% endfor %}
                </select>
                <label for="order_by" class="text-white font-bold">Ordem:</label>
                <select id="order_by" onchange="updateSort()" class="bg-slate-700 text-white px-4 py-2 rounded-lg border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    <option value="asc" {% if order == 'asc' %}selected{% endif %}>Crescente</option>
                    <option value="desc" {% if order == 'desc' %}selected{% endif %}>Decrescente</option>
                </select>
            </div>
            
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {% for mini in miniaturas %}
                    <div class="bg-slate-800 rounded-lg p-4 border-2 border-blue-600 shadow-lg">
                        <img src="{{ mini[1] }}" class="w-full h-48 object-cover rounded-lg mb-4 border border-slate-600" alt="{{ mini[2] }}">
                        <h3 class="text-xl font-bold text-white mb-2">{{ mini[2] }}</h3>
                        <p class="text-slate-300 mb-1">Previsão de Chegada: {{ mini[3] if mini[3] else 'N/A' }}</p>
                        <p class="text-slate-300 mb-1">Estoque: {{ mini[4] }}</p>
                        <p class="text-slate-300 mb-1">Incluída em: {{ mini[8].split('T')[0] if mini[8] else 'N/A' }}</p>
                        <p class="text-green-400 font-bold text-lg mb-4">R$ {{ "%.2f"|format(mini[5]) }}</p>
                        <p class="text-slate-400 text-sm mb-4">{{ mini[6] if mini[6] else 'Sem observações.' }}</p>
                        {% if mini[4] > 0 %}
                            <a href="/reservar/{{ mini[0] }}" class="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 block text-center font-bold transition duration-300">Reservar</a>
                        {% else %}
                            <a href="https://wa.me/{{ WHATSAPP_NUMERO }}?text=Olá, tenho interesse na miniatura {{ mini[2] }} e gostaria de ser notificado quando o estoque for reabastecido." target="_blank" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 block text-center font-bold transition duration-300">Notificar Estoque (WhatsApp)</a>
                        {% endif %}
                    </div>
                {% endfor %}
            </div>
        </div>
        <script>
            function updateSort() {
                const sortBy = document.getElementById('sort_by').value;
                const orderBy = document.getElementById('order_by').value;
                window.location.href = `/${sortBy}/${orderBy}`;
            }
        </script>
    </body>
    </html>
    ''', miniaturas=miniaturas, sort=sort, order=order, sort_options=sort_options, WHATSAPP_NUMERO=WHATSAPP_NUMERO)

@app.route('/reservar/<int:mini_id>', methods=['GET', 'POST'])
@login_required
def reservar(mini_id):
    """Rota para reservar uma miniatura."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas WHERE id = ?", (mini_id,))
    mini = c.fetchone()
    
    if not mini:
        conn.close()
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        quantity = int(request.form['quantity'])
        
        if quantity <= 0:
            flash('A quantidade deve ser pelo menos 1.', 'error')
        elif quantity > mini[4]: # mini[4] é o stock
            flash(f'Estoque insuficiente. Disponível: {mini[4]}', 'error')
        else:
            # Verificar reservas existentes do usuário para esta miniatura
            c.execute("SELECT SUM(quantity) FROM reservations WHERE user_id = ? AND miniatura_id = ? AND status = 'confirmed'", (session['user_id'], mini_id))
            existing_reservations = c.fetchone()[0] or 0
            
            if existing_reservations + quantity > mini[7]: # mini[7] é max_reservations_per_user
                flash(f'Limite de reservas excedido. Você já reservou {existing_reservations} e o máximo é {mini[7]}', 'error')
            else:
                reservation_date = datetime.now().isoformat()
                c.execute("INSERT INTO reservations (user_id, miniatura_id, quantity, reservation_date) VALUES (?, ?, ?, ?)", (session['user_id'], mini_id, quantity, reservation_date))
                c.execute("UPDATE miniaturas SET stock = stock - ? WHERE id = ?", (quantity, mini_id))
                conn.commit()
                create_json_backup() # Backup após reserva
                
                # Envio de email de confirmação
                corpo_html = f'''
                <h1>Confirmação de Reserva - JG MINIS</h1>
                <p>Olá {request.user['name']},</p>
                <p>Sua reserva para a miniatura <strong>{mini[2]}</strong> foi confirmada!</p>
                <p><strong>Quantidade:</strong> {quantity}</p>
                <p><strong>Preço Unitário:</strong> R$ {mini[5]:.2f}</p>
                <p><strong>Total:</strong> R$ {(quantity * mini[5]):.2f}</p>
                <p>Data da Reserva: {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
                <p>Agradecemos a preferência!</p>
                '''
                enviar_email(request.user['email'], 'Confirmação de Reserva JG MINIS', corpo_html)
                
                flash('Reserva realizada com sucesso!', 'success')
                conn.close()
                return redirect(url_for('minhas_reservas'))
        
    conn.close()
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Reservar - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/" class="text-slate-300 hover:text-blue-400">Catálogo</a>
                    <a href="/minhas-reservas" class="text-slate-300 hover:text-blue-400">Minhas Reservas</a>
                    <a href="/perfil" class="text-slate-300 hover:text-blue-400">Perfil</a>
                    {% if request.user.is_admin %}
                        <a href="/admin" class="text-slate-300 hover:text-blue-400">Admin</a>
                    {% endif %}
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6 flex items-center justify-center">
            <div class="bg-slate-800 rounded-xl border-2 border-blue-600 shadow-2xl p-8 max-w-md w-full">
                <h2 class="text-3xl font-black text-white mb-6 text-center">Reservar {{ mini[2] }}</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <div class="mb-4 text-center">
                    <img src="{{ mini[1] }}" class="w-full h-48 object-cover rounded-lg mb-4 border border-slate-600 mx-auto" alt="{{ mini[2] }}">
                    <p class="text-slate-300">Estoque disponível: {{ mini[4] }}</p>
                    <p class="text-slate-300">Seu limite de reservas: {{ mini[7] }}</p>
                    <p class="text-green-400 font-bold text-xl mt-2">R$ {{ "%.2f"|format(mini[5]) }}</p>
                </div>
                <form method="POST" class="space-y-4">
                    <div>
                        <label for="quantity" class="block text-white font-bold mb-2">Quantidade:</label>
                        <input type="number" id="quantity" name="quantity" min="1" max="{{ mini[4] }}" value="1" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <button type="submit" class="w-full bg-green-600 text-white font-bold py-2 rounded-lg hover:bg-green-700 transition duration-300">Confirmar Reserva</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    ''', mini=mini, WHATSAPP_NUMERO=WHATSAPP_NUMERO)

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    """Rota para exibir as reservas do usuário logado."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT r.id, m.name, m.image_url, r.quantity, r.reservation_date, r.status, m.price
                 FROM reservations r 
                 JOIN miniaturas m ON r.miniatura_id = m.id 
                 WHERE r.user_id = ? ORDER BY r.reservation_date DESC''', (session['user_id'],))
    reservas = c.fetchall()
    conn.close()
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Minhas Reservas - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>td, th { color: white !important; }</style>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/" class="text-slate-300 hover:text-blue-400">Catálogo</a>
                    <a href="/perfil" class="text-slate-300 hover:text-blue-400">Perfil</a>
                    {% if request.user.is_admin %}
                        <a href="/admin" class="text-slate-300 hover:text-blue-400">Admin</a>
                    {% endif %}
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6">
            <h2 class="text-3xl font-black text-white mb-6">Minhas Reservas</h2>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <ul class="mb-4">
                        {% for category, message in messages %}
                            <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                        {% endfor %}
                    </ul>
                {% endif %}
            {% endwith %}
            {% if reservas %}
                <div class="overflow-x-auto bg-slate-800 rounded-lg border-2 border-blue-600 shadow-lg">
                    <table class="min-w-full divide-y divide-slate-700">
                        <thead class="bg-slate-700">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Miniatura</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Quantidade</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Preço Unitário</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Total</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Data Reserva</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Status</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Ações</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-700">
                            {% for res in reservas %}
                                <tr>
                                    <td class="px-6 py-4 whitespace-nowrap">
                                        <div class="flex items-center">
                                            <div class="flex-shrink-0 h-10 w-10">
                                                <img class="h-10 w-10 rounded-full" src="{{ res[2] }}" alt="{{ res[1] }}">
                                            </div>
                                            <div class="ml-4">
                                                <div class="text-sm font-medium text-white">{{ res[1] }}</div>
                                            </div>
                                        </div>
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ res[3] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">R$ {{ "%.2f"|format(res[6]) }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">R$ {{ "%.2f"|format(res[3] * res[6]) }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ res[4].split('T')[0] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm">
                                        {% if res[5] == 'confirmed' %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">Confirmada</span>
                                        {% elif res[5] == 'cancelled' %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">Cancelada</span>
                                        {% else %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">{{ res[5] }}</span>
                                        {% endif %}
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                                        {% if res[5] == 'confirmed' %}
                                            <a href="/cancelar-reserva/{{ res[0] }}" class="text-red-600 hover:text-red-900 ml-2">Cancelar</a>
                                        {% else %}
                                            <span class="text-slate-500">N/A</span>
                                        {% endif %}
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% else %}
                <p class="text-white text-center">Você ainda não fez nenhuma reserva.</p>
            {% endif %}
        </div>
    </body>
    </html>
    ''', reservas=reservas, WHATSAPP_NUMERO=WHATSAPP_NUMERO)

@app.route('/cancelar-reserva/<int:res_id>')
@login_required
def cancelar_reserva(res_id):
    """Rota para cancelar uma reserva."""
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT user_id, miniatura_id, quantity, status FROM reservations WHERE id = ?", (res_id,))
    reserva = c.fetchone()
    
    if not reserva:
        flash('Reserva não encontrada.', 'error')
    elif reserva[0] != session['user_id'] and not request.user['is_admin']:
        flash('Você não tem permissão para cancelar esta reserva.', 'error')
    elif reserva[3] == 'cancelled':
        flash('Esta reserva já foi cancelada.', 'info')
    else:
        c.execute("UPDATE reservations SET status = 'cancelled' WHERE id = ?", (res_id,))
        c.execute("UPDATE miniaturas SET stock = stock + ? WHERE id = ?", (reserva[2], reserva[1]))
        conn.commit()
        create_json_backup() # Backup após cancelamento
        flash('Reserva cancelada com sucesso!', 'success')
        
        # Envio de email de confirmação de cancelamento
        corpo_html = f'''
        <h1>Cancelamento de Reserva - JG MINIS</h1>
        <p>Olá {request.user['name']},</p>
        <p>Sua reserva para a miniatura foi cancelada com sucesso.</p>
        <p>Agradecemos a compreensão.</p>
        '''
        enviar_email(request.user['email'], 'Cancelamento de Reserva JG MINIS', corpo_html)
        
    conn.close()
    return redirect(url_for('minhas_reservas'))

@app.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    """Rota para o perfil do usuário, permitindo edição de dados e mudança de senha."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT name, email, phone FROM users WHERE id = ?', (session['user_id'],))
    user_data = c.fetchone()
    conn.close()
    
    if request.method == 'POST':
        name = request.form['name']
        phone = request.form['phone']
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET name = ?, phone = ? WHERE id = ?', (name, phone, session['user_id']))
        conn.commit()
        create_json_backup() # Backup após edição de perfil
        conn.close()
        flash('Perfil atualizado com sucesso!', 'success')
        return redirect(url_for('perfil'))
        
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Meu Perfil - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/" class="text-slate-300 hover:text-blue-400">Catálogo</a>
                    <a href="/minhas-reservas" class="text-slate-300 hover:text-blue-400">Minhas Reservas</a>
                    {% if request.user.is_admin %}
                        <a href="/admin" class="text-slate-300 hover:text-blue-400">Admin</a>
                    {% endif %}
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6 flex items-center justify-center">
            <div class="bg-slate-800 rounded-xl border-2 border-blue-600 shadow-2xl p-8 max-w-md w-full">
                <h2 class="text-3xl font-black text-white mb-6 text-center">Meu Perfil</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <form method="POST" action="/perfil" class="space-y-4">
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" value="{{ user_data[0] }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label>
                        <input type="email" id="email" name="email" value="{{ user_data[1] }}" disabled class="w-full px-4 py-2 rounded-lg bg-slate-700 text-slate-400 border-2 border-blue-600">
                    </div>
                    <div>
                        <label for="phone" class="block text-slate-300 font-bold mb-1">Telefone:</label>
                        <input type="text" id="phone" name="phone" value="{{ user_data[2] if user_data[2] else '' }}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Atualizar Perfil</button>
                </form>
                <div class="mt-6 text-center">
                    <a href="/mudar-senha" class="text-blue-400 hover:underline">Mudar Senha</a>
                </div>
            </div>
        </div>
    </body>
    </html>
    ''', user_data=user_data, LOGO_URL=LOGO_URL)

@app.route('/mudar-senha', methods=['GET', 'POST'])
@login_required
def mudar_senha():
    """Rota para o usuário mudar sua senha."""
    if request.method == 'POST':
        old_password = request.form['old_password']
        new_password = request.form['new_password']
        confirm_new_password = request.form['confirm_new_password']
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT password FROM users WHERE id = ?', (session['user_id'],))
        user_hash = c.fetchone()[0]
        
        if not bcrypt.checkpw(old_password.encode('utf-8'), user_hash.encode('utf-8')):
            flash('Senha antiga incorreta.', 'error')
        elif new_password != confirm_new_password:
            flash('As novas senhas não coincidem.', 'error')
        elif len(new_password) < 6:
            flash('A nova senha deve ter pelo menos 6 caracteres.', 'error')
        else:
            new_hashed_password = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            c.execute('UPDATE users SET password = ? WHERE id = ?', (new_hashed_password, session['user_id']))
            conn.commit()
            create_json_backup() # Backup após mudança de senha
            flash('Senha alterada com sucesso!', 'success')
            conn.close()
            return redirect(url_for('perfil'))
        conn.close()
        
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Mudar Senha - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/" class="text-slate-300 hover:text-blue-400">Catálogo</a>
                    <a href="/minhas-reservas" class="text-slate-300 hover:text-blue-400">Minhas Reservas</a>
                    <a href="/perfil" class="text-slate-300 hover:text-blue-400">Perfil</a>
                    {% if request.user.is_admin %}
                        <a href="/admin" class="text-slate-300 hover:text-blue-400">Admin</a>
                    {% endif %}
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6 flex items-center justify-center">
            <div class="bg-slate-800 rounded-xl border-2 border-blue-600 shadow-2xl p-8 max-w-md w-full">
                <h2 class="text-3xl font-black text-white mb-6 text-center">Mudar Senha</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <form method="POST" action="/mudar-senha" class="space-y-4">
                    <div>
                        <label for="old_password" class="block text-slate-300 font-bold mb-1">Senha Antiga:</label>
                        <input type="password" id="old_password" name="old_password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="new_password" class="block text-slate-300 font-bold mb-1">Nova Senha:</label>
                        <input type="password" id="new_password" name="new_password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="confirm_new_password" class="block text-slate-300 font-bold mb-1">Confirmar Nova Senha:</label>
                        <input type="password" id="confirm_new_password" name="confirm_new_password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Mudar Senha</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    ''', LOGO_URL=LOGO_URL)

# --- Rotas de Administrador ---

@app.route('/admin')
@login_required
@admin_required
def admin():
    """Dashboard do administrador."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, name, stock, price FROM miniaturas ORDER BY name')
    miniaturas = c.fetchall()
    c.execute('SELECT id, name, email, is_admin FROM users ORDER BY name')
    users = c.fetchall()
    conn.close()
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>td, th { color: white !important; }</style>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS - Admin</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/" class="text-slate-300 hover:text-blue-400">Catálogo</a>
                    <a href="/minhas-reservas" class="text-slate-300 hover:text-blue-400">Minhas Reservas</a>
                    <a href="/perfil" class="text-slate-300 hover:text-blue-400">Perfil</a>
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6">
            <h2 class="text-3xl font-black text-white mb-6">Painel Administrativo</h2>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <ul class="mb-4">
                        {% for category, message in messages %}
                            <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                        {% endfor %}
                    </ul>
                {% endif %}
            {% endwith %}

            <!-- Botões de Ação Rápida -->
            <div class="mb-8 flex flex-wrap gap-4">
                <a href="/admin/add-miniatura" class="bg-green-600 text-white px-6 py-3 rounded-lg hover:bg-green-700 font-bold transition duration-300">Adicionar Miniatura</a>
                <a href="/relatorio-reservas" class="bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 font-bold transition duration-300">Relatório de Reservas</a>
                <a href="/export-usuarios" class="bg-indigo-600 text-white px-6 py-3 rounded-lg hover:bg-indigo-700 font-bold transition duration-300">Exportar Usuários (Excel)</a>
                <a href="/export-reservas" class="bg-purple-600 text-white px-6 py-3 rounded-lg hover:bg-purple-700 font-bold transition duration-300">Exportar Reservas (Excel)</a>
            </div>

            <!-- Gerenciamento de Miniaturas -->
            <h3 class="text-2xl font-bold text-white mb-4">Gerenciar Miniaturas</h3>
            {% if miniaturas %}
                <div class="overflow-x-auto bg-slate-800 rounded-lg border-2 border-blue-600 shadow-lg mb-8">
                    <table class="min-w-full divide-y divide-slate-700">
                        <thead class="bg-slate-700">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">ID</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Nome</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Estoque</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Preço</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Ações</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-700">
                            {% for mini in miniaturas %}
                                <tr>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ mini[0] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-white">{{ mini[1] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ mini[2] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">R$ {{ "%.2f"|format(mini[3]) }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                                        <a href="/admin/edit-miniatura/{{ mini[0] }}" class="text-blue-600 hover:text-blue-900">Editar</a>
                                        <a href="/admin/delete-miniatura/{{ mini[0] }}" class="text-red-600 hover:text-red-900 ml-4">Excluir</a>
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% else %}
                <p class="text-white text-center mb-8">Nenhuma miniatura cadastrada.</p>
            {% endif %}

            <!-- Gerenciamento de Usuários -->
            <h3 class="text-2xl font-bold text-white mb-4">Gerenciar Usuários</h3>
            {% if users %}
                <div class="overflow-x-auto bg-slate-800 rounded-lg border-2 border-red-600 shadow-lg">
                    <table class="min-w-full divide-y divide-slate-700">
                        <thead class="bg-slate-700">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">ID</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Nome</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Email</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Admin</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Ações</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-700">
                            {% for user in users %}
                                <tr>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ user[0] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-white">{{ user[1] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ user[2] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm">
                                        {% if user[3] %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">Sim</span>
                                        {% else %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">Não</span>
                                        {% endif %}
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                                        {% if user[2] != 'admin@jgminis.com.br' %}
                                            {% if user[3] %}
                                                <a href="/admin/toggle-admin/{{ user[0] }}" class="text-yellow-600 hover:text-yellow-900">Remover Admin</a>
                                            {% else %}
                                                <a href="/admin/toggle-admin/{{ user[0] }}" class="text-green-600 hover:text-green-900">Tornar Admin</a>
                                            {% endif %}
                                            <a href="/admin/delete-user/{{ user[0] }}" class="text-red-600 hover:text-red-900 ml-4">Excluir</a>
                                        {% else %}
                                            <span class="text-slate-500">N/A</span>
                                        {% endif %}
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% else %}
                <p class="text-white text-center">Nenhum usuário cadastrado.</p>
            {% endif %}
        </div>
    </body>
    </html>
    ''', miniaturas=miniaturas, users=users, LOGO_URL=LOGO_URL)

@app.route('/admin/add-miniatura', methods=['GET', 'POST'])
@login_required
@admin_required
def add_miniatura():
    """Rota para adicionar uma nova miniatura."""
    if request.method == 'POST':
        image_url = request.form['image_url']
        name = request.form['name']
        arrival_date = request.form['arrival_date']
        stock = int(request.form['stock'])
        price = float(request.form['price'])
        observations = request.form['observations']
        max_reservations_per_user = int(request.form['max_reservations_per_user'])
        created_at = datetime.now().isoformat()
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user, created_at))
        conn.commit()
        create_json_backup() # Backup após adicionar miniatura
        conn.close()
        flash('Miniatura adicionada com sucesso!', 'success')
        return redirect(url_for('admin'))
        
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Adicionar Miniatura - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS - Admin</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/admin" class="text-slate-300 hover:text-blue-400">Voltar para Admin</a>
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6 flex items-center justify-center">
            <div class="bg-slate-800 rounded-xl border-2 border-blue-600 shadow-2xl p-8 max-w-md w-full">
                <h2 class="text-3xl font-black text-white mb-6 text-center">Adicionar Nova Miniatura</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <form method="POST" action="/admin/add-miniatura" class="space-y-4">
                    <div>
                        <label for="image_url" class="block text-slate-300 font-bold mb-1">URL da Imagem:</label>
                        <input type="text" id="image_url" name="image_url" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="arrival_date" class="block text-slate-300 font-bold mb-1">Previsão de Chegada:</label>
                        <input type="date" id="arrival_date" name="arrival_date" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="stock" class="block text-slate-300 font-bold mb-1">Estoque:</label>
                        <input type="number" id="stock" name="stock" required value="0" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="price" class="block text-slate-300 font-bold mb-1">Preço:</label>
                        <input type="number" id="price" name="price" step="0.01" required value="0.00" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="observations" class="block text-slate-300 font-bold mb-1">Observações:</label>
                        <textarea id="observations" name="observations" rows="3" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></textarea>
                    </div>
                    <div>
                        <label for="max_reservations_per_user" class="block text-slate-300 font-bold mb-1">Máx. Reservas por Usuário:</label>
                        <input type="number" id="max_reservations_per_user" name="max_reservations_per_user" required value="1" min="1" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <button type="submit" class="w-full bg-green-600 text-white font-bold py-2 rounded-lg hover:bg-green-700 transition duration-300">Adicionar Miniatura</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    ''', LOGO_URL=LOGO_URL)

@app.route('/admin/edit-miniatura/<int:mini_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_miniatura(mini_id):
    """Rota para editar uma miniatura existente."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas WHERE id = ?", (mini_id,))
    miniatura = c.fetchone()
    
    if not miniatura:
        conn.close()
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('admin'))
        
    if request.method == 'POST':
        image_url = request.form['image_url']
        name = request.form['name']
        arrival_date = request.form['arrival_date']
        stock = int(request.form['stock'])
        price = float(request.form['price'])
        observations = request.form['observations']
        max_reservations_per_user = int(request.form['max_reservations_per_user'])
        
        c.execute("UPDATE miniaturas SET image_url = ?, name = ?, arrival_date = ?, stock = ?, price = ?, observations = ?, max_reservations_per_user = ? WHERE id = ?", (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user, mini_id))
        conn.commit()
        create_json_backup() # Backup após editar miniatura
        conn.close()
        flash('Miniatura atualizada com sucesso!', 'success')
        return redirect(url_for('admin'))
        
    conn.close()
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Editar Miniatura - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS - Admin</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/admin" class="text-slate-300 hover:text-blue-400">Voltar para Admin</a>
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6 flex items-center justify-center">
            <div class="bg-slate-800 rounded-xl border-2 border-blue-600 shadow-2xl p-8 max-w-md w-full">
                <h2 class="text-3xl font-black text-white mb-6 text-center">Editar Miniatura: {{ miniatura[2] }}</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <form method="POST" action="/admin/edit-miniatura/{{ miniatura[0] }}" class="space-y-4">
                    <div>
                        <label for="image_url" class="block text-slate-300 font-bold mb-1">URL da Imagem:</label>
                        <input type="text" id="image_url" name="image_url" value="{{ miniatura[1] }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" value="{{ miniatura[2] }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="arrival_date" class="block text-slate-300 font-bold mb-1">Previsão de Chegada:</label>
                        <input type="date" id="arrival_date" name="arrival_date" value="{{ miniatura[3] }}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="stock" class="block text-slate-300 font-bold mb-1">Estoque:</label>
                        <input type="number" id="stock" name="stock" required value="{{ miniatura[4] }}" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="price" class="block text-slate-300 font-bold mb-1">Preço:</label>
                        <input type="number" id="price" name="price" step="0.01" required value="{{ "%.2f"|format(miniatura[5]) }}" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="observations" class="block text-slate-300 font-bold mb-1">Observações:</label>
                        <textarea id="observations" name="observations" rows="3" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">{{ miniatura[6] }}</textarea>
                    </div>
                    <div>
                        <label for="max_reservations_per_user" class="block text-slate-300 font-bold mb-1">Máx. Reservas por Usuário:</label>
                        <input type="number" id="max_reservations_per_user" name="max_reservations_per_user" required value="{{ miniatura[7] }}" min="1" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Atualizar Miniatura</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    ''', miniatura=miniatura, LOGO_URL=LOGO_URL)

@app.route('/admin/delete-miniatura/<int:mini_id>')
@login_required
@admin_required
def delete_miniatura(mini_id):
    """Rota para excluir uma miniatura."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM miniaturas WHERE id = ?", (mini_id,))
    conn.commit()
    create_json_backup() # Backup após excluir miniatura
    conn.close()
    flash('Miniatura excluída com sucesso!', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/toggle-admin/<int:user_id>')
@login_required
@admin_required
def toggle_admin(user_id):
    """Rota para alternar o status de administrador de um usuário."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT email, is_admin FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    
    if not user:
        flash('Usuário não encontrado.', 'error')
    elif user[0] == 'admin@jgminis.com.br':
        flash('Não é possível alterar o status do administrador principal.', 'error')
    else:
        new_status = 0 if user[1] else 1
        c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_status, user_id))
        conn.commit()
        create_json_backup() # Backup após alterar status admin
        flash(f'Status de administrador de {user[0]} alterado para {"Sim" if new_status else "Não"}.', 'success')
    conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/delete-user/<int:user_id>')
@login_required
@admin_required
def delete_user(user_id):
    """Rota para excluir um usuário."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE id = ?", (user_id,))
    user_email = c.fetchone()[0]
    
    if user_email == 'admin@jgminis.com.br':
        flash('Não é possível excluir o administrador principal.', 'error')
    else:
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        create_json_backup() # Backup após excluir usuário
        flash(f'Usuário {user_email} excluído com sucesso!', 'success')
    conn.close()
    return redirect(url_for('admin'))

@app.route('/relatorio-reservas')
@login_required
@admin_required
def relatorio_reservas():
    """Rota para exibir um relatório de todas as reservas com filtros."""
    conn = get_db_connection()
    c = conn.cursor()
    
    status_filter = request.args.get('status', 'all')
    user_filter = request.args.get('user', '')
    miniatura_filter = request.args.get('miniatura', '')
    
    query = '''SELECT u.name, m.name, r.quantity, r.reservation_date, r.status, m.price, (r.quantity * m.price)
               FROM reservations r 
               JOIN users u ON r.user_id = u.id 
               JOIN miniaturas m ON r.miniatura_id = m.id WHERE 1=1'''
    params = []
    
    if status_filter != 'all':
        query += ' AND r.status = ?'
        params.append(status_filter)
    if user_filter:
        query += ' AND u.name LIKE ?'
        params.append(f'%{user_filter}%')
    if miniatura_filter:
        query += ' AND m.name LIKE ?'
        params.append(f'%{miniatura_filter}%')
        
    query += ' ORDER BY r.reservation_date DESC'
    
    c.execute(query, params)
    reservas = c.fetchall()
    
    # Obter lista de todos os usuários e miniaturas para os filtros
    c.execute('SELECT DISTINCT name FROM users ORDER BY name')
    all_users = [row[0] for row in c.fetchall()]
    c.execute('SELECT DISTINCT name FROM miniaturas ORDER BY name')
    all_miniaturas = [row[0] for row in c.fetchall()]
    
    conn.close()
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Relatório de Reservas - JG MINIS</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>td, th { color: white !important; }</style>
    </head>
    <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
        <nav class="bg-slate-800 border-b-2 border-red-600 p-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center">
                    <img src="''' + LOGO_URL + '''" class="h-10 rounded-full border-2 border-blue-400 mr-4">
                    <h1 class="text-2xl font-black text-blue-400">JG MINIS - Admin</h1>
                </div>
                <div class="flex space-x-4">
                    <a href="/admin" class="text-slate-300 hover:text-blue-400">Voltar para Admin</a>
                    <a href="/logout" class="text-red-400 hover:text-red-300">Logout</a>
                </div>
            </div>
        </nav>
        <div class="p-6">
            <h2 class="text-3xl font-black text-white mb-6">Relatório de Reservas</h2>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <ul class="mb-4">
                        {% for category, message in messages %}
                            <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                        {% endfor %}
                    </ul>
                {% endif %}
            {% endwith %}

            <!-- Filtros -->
            <div class="bg-slate-800 rounded-lg p-4 mb-6 border-2 border-blue-600 shadow-lg">
                <form method="GET" action="/relatorio-reservas" class="flex flex-wrap gap-4 items-center">
                    <div>
                        <label for="status" class="block text-white font-bold mb-1">Status:</label>
                        <select id="status" name="status" class="bg-slate-700 text-white px-4 py-2 rounded-lg border-2 border-blue-600 focus:outline-none focus:border-red-500">
                            <option value="all" {% if status_filter == 'all' %}selected{% endif %}>Todos</option>
                            <option value="confirmed" {% if status_filter == 'confirmed' %}selected{% endif %}>Confirmadas</option>
                            <option value="cancelled" {% if status_filter == 'cancelled' %}selected{% endif %}>Canceladas</option>
                        </select>
                    </div>
                    <div>
                        <label for="user" class="block text-white font-bold mb-1">Usuário:</label>
                        <input type="text" id="user" name="user" value="{{ user_filter }}" placeholder="Nome do usuário" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="miniatura" class="block text-white font-bold mb-1">Miniatura:</label>
                        <input type="text" id="miniatura" name="miniatura" value="{{ miniatura_filter }}" placeholder="Nome da miniatura" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <button type="submit" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 font-bold transition duration-300">Aplicar Filtros</button>
                </form>
            </div>

            {% if reservas %}
                <div class="overflow-x-auto bg-slate-800 rounded-lg border-2 border-red-600 shadow-lg">
                    <table class="min-w-full divide-y divide-slate-700">
                        <thead class="bg-slate-700">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Usuário</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Miniatura</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Quantidade</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Preço Unitário</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Total</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Data Reserva</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-white uppercase tracking-wider">Status</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-700">
                            {% for res in reservas %}
                                <tr>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ res[0] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ res[1] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ res[2] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">R$ {{ "%.2f"|format(res[5]) }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">R$ {{ "%.2f"|format(res[6]) }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-white">{{ res[3].split('T')[0] }}</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm">
                                        {% if res[4] == 'confirmed' %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">Confirmada</span>
                                        {% elif res[4] == 'cancelled' %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">Cancelada</span>
                                        {% else %}
                                            <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">{{ res[4] }}</span>
                                        {% endif %}
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% else %}
                <p class="text-white text-center">Nenhuma reserva encontrada com os filtros aplicados.</p>
            {% endif %}
        </div>
    </body>
    </html>
    ''', reservas=reservas, status_filter=status_filter, user_filter=user_filter, miniatura_filter=miniatura_filter, LOGO_URL=LOGO_URL)

# --- Rotas de Exportação Excel ---

@app.route('/export-reservas')
@login_required
@admin_required
def export_reservas():
    """Exporta todas as reservas para um arquivo Excel (.xlsx)."""
    buffer = export_to_excel_reservas()
    if buffer:
        return send_file(buffer, as_attachment=True, download_name=f'reservas_jgminis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    else:
        flash('Nenhuma reserva para exportar ou erro na geração do arquivo.', 'error')
        return redirect(url_for('admin'))

@app.route('/export-usuarios')
@login_required
@admin_required
def export_usuarios():
    """Exporta todos os usuários para um arquivo Excel (.xlsx)."""
    buffer = export_to_excel_usuarios()
    if buffer:
        return send_file(buffer, as_attachment=True, download_name=f'usuarios_jgminis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    else:
        flash('Nenhum usuário para exportar ou erro na geração do arquivo.', 'error')
        return redirect(url_for('admin'))

# --- Inicialização da Aplicação ---

# Garante que o DB e dados iniciais são carregados na inicialização do app
with app.app_context():
    init_db()
    restore_backup() # Tenta restaurar de backup JSON
    load_initial_data()
    load_miniaturas_from_sheets() # Carrega miniaturas da planilha

# Para execução local (gunicorn ignora em produção)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True) # debug=True para desenvolvimento local
