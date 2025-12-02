import os
import sqlite3
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import re
import requests
import csv
from io import StringIO

app = Flask(__name__)
app.secret_key = 'jg_minis_secret_key_2024'

DB_FILE = 'jg_minis.db'
SHEET_ID = '1sxlvo6j-UTB0xXuyivzWnhRuYvpJFcH2smL4ZzHTUps'
SHEET_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv'
LOGO_URL = 'https://imgur.com/seu-logo.png'

def convert_drive_url(drive_url):
    """Converte links do Google Drive para acesso direto"""
    if not drive_url or 'drive.google.com' not in drive_url:
        return drive_url
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', drive_url)
    if match:
        file_id = match.group(1)
        return f'https://drive.google.com/uc?id={file_id}&export=download'
    return drive_url

def load_from_google_sheets():
    """Carrega dados da planilha do Google Sheets"""
    try:
        print("📊 Carregando dados da planilha...")
        response = requests.get(SHEET_URL, timeout=10)
        response.encoding = 'utf-8'
        
        csv_reader = csv.reader(StringIO(response.text))
        rows = list(csv_reader)
        
        if len(rows) < 2:
            print("❌ Planilha vazia")
            return None
        
        headers = [h.strip().upper() for h in rows[0]]
        
        # Mapear colunas
        try:
            idx_imagem = headers.index('IMAGEM')
            idx_nome = headers.index('NOME DA MINIATURA')
            idx_chegada = headers.index('PREVISÃO DE CHEGADA')
            idx_qtd = headers.index('QUANTIDADE DISPONIVEL')
            idx_valor = headers.index('VALOR')
            idx_obs = headers.index('OBSERVAÇÕES')
            idx_max = headers.index('MAX_RESERVAS_POR_USUARIO')
        except ValueError as e:
            print(f"❌ Coluna não encontrada: {e}")
            print(f"Colunas disponíveis: {headers}")
            return None
        
        miniaturas = []
        for i, row in enumerate(rows[1:], start=2):
            # Garantir que a linha tem células suficientes
            while len(row) < len(headers):
                row.append('')
            
            imagem = row[idx_imagem].strip()
            nome = row[idx_nome].strip()
            
            if not imagem or not nome:
                continue
            
            try:
                qtd = int(row[idx_qtd].strip() or 0)
                valor = float(row[idx_valor].strip().replace(',', '.') or 0.0)
                max_res = int(row[idx_max].strip() or 3)
                
                miniaturas.append((
                    convert_drive_url(imagem),
                    nome,
                    row[idx_chegada].strip(),
                    qtd,
                    valor,
                    row[idx_obs].strip(),
                    max_res
                ))
                print(f"  ✅ Linha {i}: {nome} - R$ {valor:.2f} ({qtd} em estoque)")
            except Exception as e:
                print(f"  ⚠️ Linha {i}: Erro ao processar - {e}")
                continue
        
        print(f"✅ Total de miniaturas carregadas: {len(miniaturas)}\n")
        return miniaturas if miniaturas else None
    except Exception as e:
        print(f"❌ Erro ao carregar planilha: {e}")
        return None

def init_db():
    """Inicializa banco de dados"""
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE users (
        id INTEGER PRIMARY KEY, 
        email TEXT UNIQUE, 
        password TEXT, 
        phone TEXT, 
        is_admin BOOLEAN, 
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE miniaturas (
        id INTEGER PRIMARY KEY, 
        image_url TEXT, 
        name TEXT, 
        arrival_date TEXT, 
        stock INTEGER, 
        price REAL, 
        observations TEXT, 
        max_reservations_per_user INTEGER
    )''')
    
    c.execute('''CREATE TABLE reservations (
        id INTEGER PRIMARY KEY, 
        user_id INTEGER, 
        miniatura_id INTEGER, 
        quantity INTEGER, 
        created_at TEXT, 
        FOREIGN KEY(user_id) REFERENCES users(id), 
        FOREIGN KEY(miniatura_id) REFERENCES miniaturas(id)
    )''')
    
    # Usuários padrão
    c.execute('INSERT INTO users (email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?)',
              ('admin@example.com', generate_password_hash('admin123'), '11999999999', True, datetime.now().isoformat()))
    c.execute('INSERT INTO users (email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?)',
              ('usuario@example.com', generate_password_hash('usuario123'), '11988888888', False, datetime.now().isoformat()))
    
    # Carregar miniaturas
    miniaturas = load_from_google_sheets()
    if miniaturas:
        c.executemany('INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)', miniaturas)
    
    conn.commit()
    conn.close()
    print("✅ Banco de dados inicializado.\n")

def get_token(user_id, is_admin):
    """Gera token JWT"""
    return jwt.encode(
        {'user_id': user_id, 'is_admin': is_admin, 'exp': datetime.utcnow() + timedelta(hours=24)}, 
        app.secret_key, 
        algorithm='HS256'
    )

def verify_token(token):
    """Verifica token JWT"""
    try:
        return jwt.decode(token, app.secret_key, algorithms=['HS256'])
    except:
        return None

def login_required(f):
    """Decorator para exigir login"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token')
        if not token or not verify_token(token):
            return redirect(url_for('login'))
        request.user = verify_token(token)
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """Decorator para exigir admin"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token')
        if not token:
            return redirect(url_for('login'))
        user = verify_token(token)
        if not user or not user.get('is_admin'):
            return redirect(url_for('index'))
        request.user = user
        return f(*args, **kwargs)
    return decorated

LOGIN_HTML = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-br from-orange-500 via-yellow-500 to-orange-600 min-h-screen flex items-center justify-center">
    <div class="w-full max-w-md">
        <div class="bg-white rounded-2xl shadow-2xl overflow-hidden">
            <div class="bg-gradient-to-r from-orange-500 to-yellow-500 p-8 text-center">
                <div class="w-24 h-24 bg-white rounded-xl flex items-center justify-center mx-auto mb-4 shadow-lg">
                    <img src="{{ logo_url }}" alt="Logo" class="w-20 h-20 object-contain">
                </div>
                <h2 class="text-3xl font-bold text-white mb-2">JG MINIS</h2>
                <p class="text-orange-100">Portal de Miniaturas</p>
            </div>
            <div class="p-8">
                <div class="flex gap-4 mb-6 border-b">
                    <button class="nav-tab active pb-4 px-4 font-bold text-orange-600 border-b-2 border-orange-600 cursor-pointer" onclick="switchTab('login')">Login</button>
                    <button class="nav-tab pb-4 px-4 font-bold text-gray-500 cursor-pointer" onclick="switchTab('register')">Cadastro</button>
                </div>
                <div id="login" class="tab-content">
                    {% if error %}<div class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded-lg mb-4">{{ error }}</div>{% endif %}
                    <form method="POST" action="/login">
                        <div class="mb-4">
                            <label class="block text-gray-700 font-bold mb-2">Email</label>
                            <input type="email" name="email" class="w-full border-2 border-gray-300 rounded-lg px-4 py-2 focus:outline-none focus:border-orange-500" required>
                        </div>
                        <div class="mb-6">
                            <label class="block text-gray-700 font-bold mb-2">Senha</label>
                            <input type="password" name="password" class="w-full border-2 border-gray-300 rounded-lg px-4 py-2 focus:outline-none focus:border-orange-500" required>
                        </div>
                        <button type="submit" class="w-full bg-gradient-to-r from-orange-500 to-yellow-500 text-white font-bold py-3 rounded-lg hover:shadow-lg transition">Entrar</button>
                    </form>
                    <hr class="my-4">
                    <p class="text-sm text-gray-600"><strong>Teste:</strong><br>admin@example.com / admin123<br>usuario@example.com / usuario123</p>
                </div>
                <div id="register" class="tab-content hidden">
                    {% if register_error %}<div class="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded-lg mb-4">{{ register_error }}</div>{% endif %}
                    {% if register_success %}<div class="bg-green-100 border border-green-400 text-green-700 px-4 py-3 rounded-lg mb-4">{{ register_success }}</div>{% endif %}
                    <form method="POST" action="/register">
                        <div class="mb-4">
                            <label class="block text-gray-700 font-bold mb-2">Email</label>
                            <input type="email" name="email" class="w-full border-2 border-gray-300 rounded-lg px-4 py-2 focus:outline-none focus:border-orange-500" required>
                        </div>
                        <div class="mb-4">
                            <label class="block text-gray-700 font-bold mb-2">Telefone</label>
                            <input type="tel" name="phone" class="w-full border-2 border-gray-300 rounded-lg px-4 py-2 focus:outline-none focus:border-orange-500" required>
                        </div>
                        <div class="mb-4">
                            <label class="block text-gray-700 font-bold mb-2">Senha</label>
                            <input type="password" name="password" class="w-full border-2 border-gray-300 rounded-lg px-4 py-2 focus:outline-none focus:border-orange-500" minlength="6" required>
                        </div>
                        <div class="mb-6">
                            <label class="block text-gray-700 font-bold mb-2">Confirme a Senha</label>
                            <input type="password" name="confirm_password" class="w-full border-2 border-gray-300 rounded-lg px-4 py-2 focus:outline-none focus:border-orange-500" minlength="6" required>
                        </div>
                        <button type="submit" class="w-full bg-gradient-to-r from-orange-500 to-yellow-500 text-white font-bold py-3 rounded-lg hover:shadow-lg transition">Cadastrar</button>
                    </form>
                </div>
            </div>
        </div>
    </div>
    <script>
        function switchTab(tab){
            document.querySelectorAll('.tab-content').forEach(e=>e.classList.add('hidden'));
            document.getElementById(tab).classList.remove('hidden');
            document.querySelectorAll('.nav-tab').forEach(e=>e.classList.remove('border-b-2','border-orange-600','text-orange-600'));
            event.target.classList.add('border-b-2','border-orange-600','text-orange-600');
        }
    </script>
</body>
</html>'''

HOME_HTML = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gray-50">
    <nav class="bg-gradient-to-r from-orange-500 to-yellow-500 shadow-lg sticky top-0 z-50">
        <div class="container mx-auto px-4 py-4 flex justify-between items-center">
            <a href="/" class="flex items-center gap-3">
                <div class="w-12 h-12 bg-white rounded-lg flex items-center justify-center shadow-md">
                    <img src="{{ logo_url }}" alt="Logo" class="w-10 h-10 object-contain">
                </div>
                <span class="text-2xl font-bold text-white">JG MINIS</span>
            </a>
            <div class="flex items-center gap-4">
                {% if is_admin %}
                <a href="/admin" class="bg-white text-orange-600 font-bold px-4 py-2 rounded-lg hover:shadow-lg transition">
                    <i class="fas fa-chart-bar mr-2"></i>Admin
                </a>
                {% endif %}
                <span class="text-white font-semibold"><i class="fas fa-user mr-2"></i>{{ email }}</span>
                <a href="/logout" class="bg-red-600 text-white font-bold px-4 py-2 rounded-lg hover:bg-red-700 transition">Sair</a>
            </div>
        </div>
    </nav>
    <div class="container mx-auto px-4 py-12">
        <h1 class="text-4xl font-bold text-gray-900 mb-2"><i class="fas fa-cube text-orange-500 mr-3"></i>Catálogo de Miniaturas</h1>
        <p class="text-gray-600 mb-8">Explore nossa coleção exclusiva</p>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
            {{ miniaturas|safe }}
        </div>
    </div>
</body>
</html>'''

ADMIN_HTML = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS - Admin</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gray-50">
    <nav class="bg-gradient-to-r from-orange-500 to-yellow-500 shadow-lg">
        <div class="container mx-auto px-4 py-4 flex justify-between items-center">
            <h1 class="text-2xl font-bold text-white"><i class="fas fa-shield mr-2"></i>JG MINIS - Admin</h1>
            <a href="/logout" class="bg-red-600 text-white px-4 py-2 rounded-lg hover:bg-red-700">Sair</a>
        </div>
    </nav>
    <div class="container mx-auto px-4 py-8">
        <div class="grid grid-cols-1 lg:grid-cols-4 gap-6 mb-8">
            {{ stats|safe }}
        </div>
        <div class="bg-white rounded-lg shadow-lg p-6">
            {{ content|safe }}
        </div>
    </div>
</body>
</html>'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id, password, is_admin FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        conn.close()
        
        if user and check_password_hash(user[1], password):
            token = get_token(user[0], user[2])
            response = redirect(url_for('index'))
            response.set_cookie('token', token, httponly=True, max_age=86400)
            return response
        
        return render_template_string(LOGIN_HTML, error='Email ou senha inválidos', register_error='', register_success='', logo_url=LOGO_URL)
    
    return render_template_string(LOGIN_HTML, error='', register_error='', register_success='', logo_url=LOGO_URL)

@app.route('/register', methods=['POST'])
def register():
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    
    if password != confirm:
        return render_template_string(LOGIN_HTML, error='', register_error='Senhas não conferem', register_success='', logo_url=LOGO_URL)
    if len(password) < 6:
        return render_template_string(LOGIN_HTML, error='', register_error='Mínimo 6 caracteres', register_success='', logo_url=LOGO_URL)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE email = ?', (email,))
    if c.fetchone():
        conn.close()
        return render_template_string(LOGIN_HTML, error='', register_error='Email já existe', register_success='', logo_url=LOGO_URL)
    
    c.execute('INSERT INTO users (email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?)',
              (email, generate_password_hash(password), phone, False, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    return render_template_string(LOGIN_HTML, error='', register_error='', register_success='Cadastro realizado! Faça login agora.', logo_url=LOGO_URL)

@app.route('/logout')
def logout():
    response = redirect(url_for('login'))
    response.delete_cookie('token')
    return response

@app.route('/')
@login_required
def index():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, image_url, name, arrival_date, stock, price, observations FROM miniaturas')
    miniaturas = c.fetchall()
    conn.close()
    
    html = ''
    for m in miniaturas:
        html += f'''
        <div class="bg-white rounded-xl shadow-md hover:shadow-2xl transition-all duration-300 overflow-hidden group">
            <div class="relative overflow-hidden bg-gray-200 h-64">
                <img src="{m[1]}" class="w-full h-full object-cover group-hover:scale-110 transition-transform duration-300" alt="{m[2]}" onerror="this.src='https://via.placeholder.com/300'">
                <div class="absolute top-3 right-3 bg-orange-500 text-white px-3 py-1 rounded-full text-sm font-bold shadow-lg">
                    {m[4]} em estoque
                </div>
            </div>
            <div class="p-4">
                <h3 class="font-bold text-lg text-gray-900 mb-2">{m[2]}</h3>
                <div class="text-sm text-gray-600 mb-3 space-y-1">
                    <p><i class="fas fa-calendar mr-2 text-orange-500"></i>Chegada: {m[3]}</p>
                    <p><i class="fas fa-sticky-note mr-2 text-orange-500"></i>{m[6]}</p>
                </div>
                <div class="border-t pt-3 mt-3 flex justify-between items-center">
                    <span class="text-2xl font-bold text-orange-600">R$ {m[5]:.2f}</span>
                    <button onclick="reservar({m[0]})" class="bg-gradient-to-r from-orange-500 to-yellow-500 text-white font-bold px-4 py-2 rounded-lg hover:shadow-lg transition">
                        <i class="fas fa-shopping-cart mr-1"></i>Reservar
                    </button>
                </div>
            </div>
        </div>
        '''
    
    return render_template_string(HOME_HTML, miniaturas=html, email=request.user.get('user_id'), logo_url=LOGO_URL, is_admin=request.user.get('is_admin', False))

@app.route('/admin')
@admin_required
def admin():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) FROM reservations')
    total_reservas = c.fetchone()[0]
    c.execute('SELECT SUM(r.quantity * m.price) FROM reservations r JOIN miniaturas m ON r.miniatura_id = m.id')
    total_valor = c.fetchone()[0] or 0
    c.execute('SELECT COUNT(DISTINCT user_id) FROM reservations')
    total_clientes = c.fetchone()[0]
    
    stats = f'''
    <div class="bg-gradient-to-br from-blue-500 to-blue-600 text-white p-6 rounded-lg shadow-lg">
        <div class="flex items-center justify-between">
            <div><p class="text-blue-100">Total de Reservas</p><p class="text-3xl font-bold">{total_reservas}</p></div>
            <i class="fas fa-shopping-bag text-5xl opacity-20"></i>
        </div>
    </div>
    <div class="bg-gradient-to-br from-green-500 to-green-600 text-white p-6 rounded-lg shadow-lg">
        <div class="flex items-center justify-between">
            <div><p class="text-green-100">Valor Total</p><p class="text-3xl font-bold">R$ {total_valor:.2f}</p></div>
            <i class="fas fa-dollar-sign text-5xl opacity-20"></i>
        </div>
    </div>
    <div class="bg-gradient-to-br from-purple-500 to-purple-600 text-white p-6 rounded-lg shadow-lg">
        <div class="flex items-center justify-between">
            <div><p class="text-purple-100">Total de Clientes</p><p class="text-3xl font-bold">{total_clientes}</p></div>
            <i class="fas fa-users text-5xl opacity-20"></i>
        </div>
    </div>
    <div class="bg-gradient-to-br from-orange-500 to-yellow-500 text-white p-6 rounded-lg shadow-lg">
        <div class="flex items-center justify-between">
            <div><p class="text-orange-100">Ticket Médio</p><p class="text-3xl font-bold">R$ {(total_valor/total_reservas if total_reservas > 0 else 0):.2f}</p></div>
            <i class="fas fa-chart-line text-5xl opacity-20"></i>
        </div>
    </div>
    '''
    
    c.execute('SELECT r.created_at, u.email, u.phone, m.name, r.quantity, m.price FROM reservations r JOIN users u ON r.user_id = u.id JOIN miniaturas m ON r.miniatura_id = m.id ORDER BY r.created_at DESC LIMIT 50')
    reservas = c.fetchall()
    conn.close()
    
    html = '<h2 class="text-2xl font-bold mb-4">📋 Últimas Reservas</h2><div class="overflow-x-auto"><table class="w-full"><thead class="bg-gray-200"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Cliente</th><th class="p-3 text-left">Email</th><th class="p-3 text-left">Telefone</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Qtd</th><th class="p-3 text-right">Valor</th></tr></thead><tbody>'
    
    total = 0
    for r in reservas:
        valor = r[4] * r[5]
        total += valor
        html += f'<tr class="border-b hover:bg-gray-50"><td class="p-3">{r[0][:10]}</td><td class="p-3 font-semibold">{r[1].split("@")[0]}</td><td class="p-3">{r[1]}</td><td class="p-3">{r[2]}</td><td class="p-3">{r[3]}</td><td class="p-3 text-center">{r[4]}</td><td class="p-3 text-right font-bold">R$ {valor:.2f}</td></tr>'
    
    html += f'<tr class="bg-orange-100 font-bold"><td colspan="6" class="p-3">TOTAL</td><td class="p-3 text-right">R$ {total:.2f}</td></tr></tbody></table></div>'
    
    return render_template_string(ADMIN_HTML, content=html, stats=stats)

@app.route('/admin/usuarios')
@admin_required
def admin_usuarios():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, email, phone, is_admin, created_at FROM users ORDER BY created_at DESC')
    usuarios = c.fetchall()
    
    html = '<h2 class="text-2xl font-bold mb-4">👥 Usuários do Sistema</h2><div class="overflow-x-auto"><table class="w-full"><thead class="bg-gray-200"><tr><th class="p-3 text-left">Email</th><th class="p-3 text-left">Telefone</th><th class="p-3 text-left">Cadastro</th><th class="p-3 text-left">Tipo</th><th class="p-3 text-center">Reservas</th></tr></thead><tbody>'
    
    for u in usuarios:
        c.execute('SELECT COUNT(*) FROM reservations WHERE user_id = ?', (u[0],))
        qtd = c.fetchone()[0]
        tipo = '<span class="bg-orange-200 text-orange-800 px-3 py-1 rounded-full text-sm font-bold">Admin</span>' if u[3] else '<span class="bg-blue-200 text-blue-800 px-3 py-1 rounded-full text-sm font-bold">Usuário</span>'
        html += f'<tr class="border-b hover:bg-gray-50"><td class="p-3 font-semibold">{u[1]}</td><td class="p-3">{u[2]}</td><td class="p-3">{u[4][:10]}</td><td class="p-3">{tipo}</td><td class="p-3 text-center">{qtd}</td></tr>'
    
    conn.close()
    html += '</tbody></table></div>'
    
    return render_template_string(ADMIN_HTML, content=html, stats='')

@app.route('/reservar', methods=['POST'])
@login_required
def reservar():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    user_id = request.user.get('user_id')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT stock, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
    m = c.fetchone()
    
    if not m or m[0] <= 0:
        conn.close()
        return jsonify({'error': 'Sem estoque'}), 400
    
    c.execute('SELECT COUNT(*) FROM reservations WHERE user_id = ? AND miniatura_id = ?', (user_id, miniatura_id))
    if c.fetchone()[0] >= m[1]:
        conn.close()
        return jsonify({'error': 'Limite de reservas atingido para este produto'}), 400
    
    c.execute('INSERT INTO reservations (user_id, miniatura_id, quantity, created_at) VALUES (?, ?, ?, ?)',
              (user_id, miniatura_id, 1, datetime.now().isoformat()))
    c.execute('UPDATE miniaturas SET stock = stock - 1 WHERE id = ?', (miniatura_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
