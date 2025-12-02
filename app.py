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
LOGO_URL = 'https://i.imgur.com/Yp1OiWB.png'

def convert_drive_url(drive_url):
    """Converte links do Google Drive para acesso direto e público"""
    if not drive_url or 'drive.google.com' not in drive_url:
        return drive_url
    
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', drive_url)
    if match:
        file_id = match.group(1)
        converted_url = f'https://lh3.google.com/d/{file_id}=w1000'
        print(f"✅ URL Convertida: {file_id}")
        return converted_url
    
    return drive_url

def load_from_google_sheets():
    """Carrega dados da planilha do Google Sheets"""
    try:
        print("📊 Carregando dados da planilha...")
        response = requests.get(SHEET_URL, timeout=10)
        response.encoding = 'utf-8'
        
        csv_reader = csv.reader(StringIO(response.text))
        rows = list(csv_reader)
        
        if len(rows) &lt; 2:
            print("❌ Planilha vazia")
            return None
        
        headers = [h.strip().upper() for h in rows[0]]
        print(f"📋 Colunas encontradas: {headers}")
        
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
            while len(row) &lt; len(headers):
                row.append('')
            
            imagem = row[idx_imagem].strip()
            nome = row[idx_nome].strip()
            
            if not imagem or not nome:
                continue
            
            try:
                qtd = int(row[idx_qtd].strip() or 0)
                valor = float(row[idx_valor].strip().replace(',', '.') or 0.0)
                max_res = int(row[idx_max].strip() or 3)
                
                url_convertida = convert_drive_url(imagem)
                
                miniaturas.append((
                    url_convertida,
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

def format_phone(phone):
    """Formata telefone para (11) xxxxx-xxxx"""
    phone = re.sub(r'\D', '', phone)
    if len(phone) == 11:
        return f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    return phone

def validate_phone(phone):
    """Valida telefone no formato (11) xxxxx-xxxx"""
    phone = re.sub(r'\D', '', phone)
    return len(phone) == 11

def init_db():
    """Inicializa banco de dados"""
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE users (
        id INTEGER PRIMARY KEY, 
        name TEXT,
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
    
    c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              ('Admin', 'admin@example.com', generate_password_hash('SenhaForte123!'), '11999999999', True, datetime.now().isoformat()))
    c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              ('Usuário Teste', 'usuario@example.com', generate_password_hash('Usuario123!'), '11988888888', False, datetime.now().isoformat()))
    
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
<body class="bg-gradient-to-br from-slate-900 via-blue-900 to-black min-h-screen flex items-center justify-center">
    <div class="w-full max-w-md">
        <div class="bg-gradient-to-b from-slate-800 to-black rounded-2xl shadow-2xl overflow-hidden border-2 border-red-600">
            <div class="bg-gradient-to-r from-blue-700 via-blue-900 to-black p-8 text-center border-b-2 border-red-600">
                <div class="w-32 h-32 bg-black rounded-xl flex items-center justify-center mx-auto mb-4 shadow-2xl border-2 border-red-600 overflow-hidden">
                    <img src="{{ logo_url }}" alt="Logo JG MINIS" class="w-full h-full object-contain p-2">
                </div>
                <h2 class="text-4xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">JG MINIS</h2>
                <p class="text-blue-300 font-semibold">Portal de Miniaturas Premium</p>
            </div>
            <div class="p-8">
                <div class="flex gap-4 mb-6 border-b border-slate-600">
                    <button class="nav-tab active pb-4 px-4 font-bold text-red-500 border-b-2 border-red-500 cursor-pointer transition" onclick="switchTab('login')">Login</button>
                    <button class="nav-tab pb-4 px-4 font-bold text-slate-400 cursor-pointer transition hover:text-blue-400" onclick="switchTab('register')">Cadastro</button>
                </div>
                <div id="login" class="tab-content">
                    {% if error %}<div class="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg mb-4 flex items-center gap-2"><i class="fas fa-exclamation-circle"></i>{{ error }}</div>{% endif %}
                    <form method="POST" action="/login">
                        <div class="mb-4">
                            <label class="block text-slate-300 font-bold mb-2">📧 Email</label>
                            <input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 focus:ring-2 focus:ring-red-500 focus:ring-opacity-50 transition" required>
                        </div>
                        <div class="mb-6">
                            <label class="block text-slate-300 font-bold mb-2">🔐 Senha</label>
                            <input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 focus:ring-2 focus:ring-red-500 focus:ring-opacity-50 transition" required>
                        </div>
                        <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 hover:from-blue-700 hover:to-red-700 text-white font-bold py-3 rounded-lg shadow-lg hover:shadow-2xl transition transform hover:scale-105">Entrar</button>
                    </form>
                    <hr class="my-4 border-slate-600">
                    <div class="bg-slate-900 p-3 rounded-lg border border-blue-600">
                        <p class="text-sm text-slate-300"><strong class="text-blue-400">👤 Teste Admin:</strong><br><code class="text-green-400">admin@example.com</code><br><code class="text-green-400">SenhaForte123!</code></p>
                    </div>
                </div>
                <div id="register" class="tab-content hidden">
                    {% if register_error %}<div class="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg mb-4">{{ register_error }}</div>{% endif %}
                    {% if register_success %}<div class="bg-green-900 border border-green-600 text-green-200 px-4 py-3 rounded-lg mb-4">{{ register_success }}</div>{% endif %}
                    <form method="POST" action="/register">
                        <div class="mb-4">
                            <label class="block text-slate-300 font-bold mb-2">👤 Nome Completo</label>
                            <input type="text" name="name" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 transition" required>
                        </div>
                        <div class="mb-4">
                            <label class="block text-slate-300 font-bold mb-2">📧 Email</label>
                            <input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 transition" required>
                        </div>
                        <div class="mb-4">
                            <label class="block text-slate-300 font-bold mb-2">📱 Telefone (11) xxxxx-xxxx</label>
                            <input type="tel" name="phone" placeholder="(11) 99999-9999" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 transition" required>
                        </div>
                        <div class="mb-4">
                            <label class="block text-slate-300 font-bold mb-2">🔐 Senha (mínimo 8 caracteres)</label>
                            <input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 transition" minlength="8" required>
                        </div>
                        <div class="mb-6">
                            <label class="block text-slate-300 font-bold mb-2">🔐 Confirme a Senha</label>
                            <input type="password" name="confirm_password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500 transition" minlength="8" required>
                        </div>
                        <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 hover:from-blue-700 hover:to-red-700 text-white font-bold py-3 rounded-lg shadow-lg hover:shadow-2xl transition transform hover:scale-105">Cadastrar</button>
                    </form>
                </div>
            </div>
        </div>
    </div>
    <script>
        function switchTab(tab){
            document.querySelectorAll('.tab-content').forEach(e=>e.classList.add('hidden'));
            document.getElementById(tab).classList.remove('hidden');
            document.querySelectorAll('.nav-tab').forEach(e=>{e.classList.remove('border-b-2','border-red-500','text-red-500'); e.classList.add('text-slate-400');});
            event.target.classList.add('border-b-2','border-red-500','text-red-500');
            event.target.classList.remove('text-slate-400');
        }
    </script>
</body>
</html>'''

HOME_HTML = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS - Catálogo</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
    <nav class="bg-gradient-to-r from-blue-900 via-slate-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
        <div class="container mx-auto px-4 py-4 flex justify-between items-center">
            <a href="/" class="flex items-center gap-3 hover:opacity-80 transition">
                <div class="w-14 h-14 bg-gradient-to-br from-blue-600 to-red-600 rounded-xl flex items-center justify-center shadow-lg border-2 border-blue-400 overflow-hidden">
                    <img src="{{ logo_url }}" alt="Logo JG MINIS" class="w-12 h-12 object-contain">
                </div>
                <div>
                    <span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span>
                    <p class="text-xs text-blue-300">Premium Miniatures</p>
                </div>
            </a>
            <div class="flex items-center gap-6">
                <a href="/minhas-reservas" class="bg-gradient-to-r from-blue-600 to-blue-700 hover:from-blue-700 hover:to-blue-800 text-white font-bold px-5 py-2 rounded-lg shadow-lg hover:shadow-2xl transition transform hover:scale-105 flex items-center gap-2">
                    <i class="fas fa-list"></i>Minhas Reservas
                </a>
                {% if is_admin %}
                <a href="/admin" class="bg-gradient-to-r from-red-600 to-red-700 hover:from-red-700 hover:to-red-800 text-white font-bold px-5 py-2 rounded-lg shadow-lg hover:shadow-2xl transition transform hover:scale-105 flex items-center gap-2">
                    <i class="fas fa-chart-bar"></i>Admin Panel
                </a>
                {% endif %}
                <div class="bg-blue-900 bg-opacity-50 border border-blue-600 px-4 py-2 rounded-lg">
                    <span class="text-slate-200 font-semibold"><i class="fas fa-user text-blue-400 mr-2"></i>{{ name }}</span>
                </div>
                <a href="/logout" class="bg-red-600 hover:bg-red-700 text-white font-bold px-5 py-2 rounded-lg shadow-lg hover:shadow-2xl transition transform hover:scale-105">
                    <i class="fas fa-sign-out-alt mr-1"></i>Sair
                </a>
            </div>
        </div>
    </nav>
    <div class="container mx-auto px-4 py-12">
        <div class="mb-12 text-center">
            <h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2"><i class="fas fa-cube text-red-500 mr-3"></i>Catálogo de Miniaturas</h1>
            <p class="text-slate-300 text-lg">Pré vendas</p>
            <div class="w-24 h-1 bg-gradient-to-r from-blue-500 to-red-500 mx-auto mt-4 rounded"></div>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
            {{ miniaturas|safe }}
        </div>
    </div>

    <!-- Modal Confirmação de Reserva -->
    <div id="confirmModal" class="hidden fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
        <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl max-w-md w-full p-8">
            <h2 class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-4"><i class="fas fa-shopping-cart text-red-500 mr-2"></i>Confirmar Reserva</h2>
            <div id="confirmContent" class="text-slate-300 mb-6"></div>
            <div class="flex gap-4">
                <button onclick="closeModal()" class="flex-1 bg-slate-700 hover:bg-slate-600 text-white font-bold py-2 rounded-lg transition">Cancelar</button>
                <button onclick="confirmarReserva()" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 hover:from-blue-700 hover:to-red-700 text-white font-bold py-2 rounded-lg transition">Confirmar</button>
            </div>
        </div>
    </div>

    <script>
        let reservaAtual = null;

        function abrirConfirmacao(id, nome, preco) {
            reservaAtual = id;
            document.getElementById('confirmContent').innerHTML = `
                <div class="space-y-2">
                    <p><strong>Produto:</strong> ${nome}</p>
                    <p><strong>Valor:</strong> R$ ${parseFloat(preco).toFixed(2)}</p>
                    <p class="text-yellow-400 mt-4"><i class="fas fa-info-circle"></i> Deseja confirmar esta reserva?</p>
                </div>
            `;
            document.getElementById('confirmModal').classList.remove('hidden');
        }

        function closeModal() {
            document.getElementById('confirmModal').classList.add('hidden');
            reservaAtual = null;
        }

        function confirmarReserva() {
            if (!reservaAtual) return;
            
            fetch('/reservar', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({miniatura_id: reservaAtual})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('✅ Reserva realizada com sucesso!');
                    location.reload();
                } else {
                    alert('❌ Erro: ' + data.error);
                }
            });
            
            closeModal();
        }
    </script>
</body>
</html>'''

MINHAS_RESERVAS_HTML = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Minhas Reservas - JG MINIS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
    <nav class="bg-gradient-to-r from-blue-900 via-slate-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
        <div class="container mx-auto px-4 py-4 flex justify-between items-center">
            <a href="/" class="flex items-center gap-3 hover:opacity-80 transition">
                <span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span>
            </a>
            <div class="flex items-center gap-6">
                <a href="/" class="bg-blue-600 hover:bg-blue-700 text-white font-bold px-5 py-2 rounded-lg transition">
                    <i class="fas fa-arrow-left mr-2"></i>Catálogo
                </a>
                <a href="/logout" class="bg-red-600 hover:bg-red-700 text-white font-bold px-5 py-2 rounded-lg transition">
                    <i class="fas fa-sign-out-alt mr-1"></i>Sair
                </a>
            </div>
        </div>
    </nav>
    <div class="container mx-auto px-4 py-12">
        <div class="mb-8">
            <h1 class="text-4xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2"><i class="fas fa-list text-blue-400 mr-3"></i>Minhas Reservas</h1>
            <p class="text-slate-300">Histórico de todas as suas reservas</p>
        </div>
        <div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-2xl p-8 border-2 border-blue-600">
            {{ content|safe }}
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
<body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
    <nav class="bg-gradient-to-r from-blue-900 via-slate-900 to-black shadow-2xl border-b-4 border-red-600">
        <div class="container mx-auto px-4 py-4 flex justify-between items-center">
            <h1 class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500"><i class="fas fa-shield text-red-500 mr-3"></i>Painel Admin</h1>
            <a href="/logout" class="bg-red-600 hover:bg-red-700 text-white font-bold px-5 py-2 rounded-lg shadow-lg hover:shadow-2xl transition">Sair</a>
        </div>
    </nav>
    <div class="container mx-auto px-4 py-8">
        <div class="grid grid-cols-1 lg:grid-cols-4 gap-6 mb-8">
            {{ stats|safe }}
        </div>
        <div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-2xl p-8 border-2 border-blue-600">
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
        
        return render_template_string(LOGIN_HTML, error='❌ Email ou senha inválidos', register_error='', register_success='', logo_url=LOGO_URL)
    
    return render_template_string(LOGIN_HTML, error='', register_error='', register_success='', logo_url=LOGO_URL)

@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    
    if not name:
        return render_template_string(LOGIN_HTML, error='', register_error='❌ Nome é obrigatório', register_success='', logo_url=LOGO_URL)
    
    if not validate_phone(phone):
        return render_template_string(LOGIN_HTML, error='', register_error='❌ Telefone inválido. Use formato (11) xxxxx-xxxx', register_success='', logo_url=LOGO_URL)
    
    if password != confirm:
        return render_template_string(LOGIN_HTML, error='', register_error='❌ Senhas não conferem', register_success='', logo_url=LOGO_URL)
    
    if len(password) &lt; 8:
        return render_template_string(LOGIN_HTML, error='', register_error='❌ Mínimo 8 caracteres na senha', register_success='', logo_url=LOGO_URL)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE email = ?', (email,))
    if c.fetchone():
        conn.close()
        return render_template_string(LOGIN_HTML, error='', register_error='❌ Email já existe', register_success='', logo_url=LOGO_URL)
    
    phone_formatted = format_phone(phone)
    
    c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (name, email, generate_password_hash(password), phone_formatted, False, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    return render_template_string(LOGIN_HTML, error='', register_error='', register_success='✅ Cadastro realizado! Faça login agora.', logo_url=LOGO_URL)

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
    
    c.execute('SELECT name FROM users WHERE id = ?', (request.user.get('user_id'),))
    user_data = c.fetchone()
    user_name = user_data[0] if user_data else 'Usuário'
    
    c.execute('SELECT id, image_url, name, arrival_date, stock, price, observations FROM miniaturas')
    miniaturas = c.fetchall()
    conn.close()
    
    html = ''
    for m in miniaturas:
        status_class = 'bg-green-600' if m[4] > 0 else 'bg-red-600'
        status_text = f'{m[4]} em estoque' if m[4] > 0 else 'Indisponível'
        button_disabled = 'opacity-50 cursor-not-allowed' if m[4] == 0 else ''
        
        html += f'''
        <div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg hover:shadow-2xl transition-all duration-300 overflow-hidden group border-2 border-blue-600 hover:border-red-500">
            <div class="relative overflow-hidden bg-black h-64 flex items-center justify-center">
                <img src="{m[1]}" 
                     class="w-full h-full object-cover group-hover:scale-110 transition-transform duration-300" 
                     alt="{m[2]}"
                     style="background: linear-gradient(135deg, #1e40af 0%, #7c3aed 100%);"
                     loading="lazy"
                     onerror="this.style.background='linear-gradient(135deg, #1e40af 0%, #7c3aed 100%)'; this.style.display='flex'; this.style.alignItems='center'; this.style.justifyContent='center'; this.innerHTML='<span style=\'color:white; font-size:24px;\'>🎲</span>';">
                <div class="absolute top-3 right-3 {status_class} text-white px-3 py-1 rounded-full text-sm font-bold shadow-lg">
                    {status_text}
                </div>
            </div>
            <div class="p-5">
                <h3 class="font-bold text-lg text-slate-100 mb-2 group-hover:text-red-400 transition">{m[2]}</h3>
                <div class="text-sm text-slate-400 mb-4 space-y-1 border-l-2 border-blue-600 pl-3">
                    <p><i class="fas fa-calendar text-blue-400 mr-2"></i>Chegada: <span class="text-blue-300">{m[3]}</span></p>
                    <p><i class="fas fa-sticky-note text-blue-400 mr-2"></i><span class="text-slate-300">{m[6]}</span></p>
                </div>
                <div class="border-t border-slate-700 pt-3 mt-3 flex justify-between items-center">
                    <span class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">R$ {m[5]:.2f}</span>
                    <button onclick="abrirConfirmacao({m[0]}, '{m[2]}', {m[5]})" class="bg-gradient-to-r from-blue-600 to-red-600 hover:from-blue-700 hover:to-red-700 text-white font-bold px-4 py-2 rounded-lg hover:shadow-lg transition transform hover:scale-105 {button_disabled}" {'disabled' if m[4] == 0 else ''}>
                        <i class="fas fa-shopping-cart mr-1"></i>Reservar
                    </button>
                </div>
            </div>
        </div>
        '''
    
    return render_template_string(HOME_HTML, miniaturas=html, name=user_name, logo_url=LOGO_URL, is_admin=request.user.get('is_admin', False))

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    user_id = request.user.get('user_id')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT r.id, r.created_at, m.name, m.price, r.quantity 
                 FROM reservations r 
                 JOIN miniaturas m ON r.miniatura_id = m.id 
                 WHERE r.user_id = ? 
                 ORDER BY r.created_at DESC''', (user_id,))
    reservas = c.fetchall()
    conn.close()
    
    if not reservas:
        html = '<div class="text-center py-12"><p class="text-slate-400 text-lg"><i class="fas fa-inbox mr-2"></i>Você ainda não fez nenhuma reserva</p></div>'
    else:
        html = '<div class="overflow-x-auto"><table class="w-full"><thead class="bg-gradient-to-r from-blue-700 to-blue-900 text-white"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Quantidade</th><th class="p-3 text-right">Valor Unitário</th><th class="p-3 text-right">Total</th></tr></thead><tbody>'
        
        total_geral = 0
        for idx, r in enumerate(reservas):
            total = r[3] * r[4]
            total_geral += total
            bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
            html += f'<tr class="{bg} border-b border-slate-600 hover:bg-blue-600 hover:bg-opacity-30 transition text-slate-200"><td class="p-3">{r[1][:10]}</td><td class="p-3 font-semibold text-blue-300">{r[2]}</td><td class="p-3 text-center">{r[4]}</td><td class="p-3 text-right">R$ {r[3]:.2f}</td><td class="p-3 text-right font-bold text-red-400">R$ {total:.2f}</td></tr>'
        
        html += f'<tr class="bg-gradient-to-r from-blue-700 to-red-700 font-black text-white"><td colspan="4" class="p-3">TOTAL</td><td class="p-3 text-right">R$ {total_geral:.2f}</td></tr></tbody></table></div>'
    
    return render_template_string(MINHAS_RESERVAS_HTML, content=html)

@app.route('/admin')
@admin_required
def admin():
    cliente_filter = request.args.get('cliente', '')
    miniatura_filter = request.args.get('miniatura', '')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) FROM reservations')
    total_reservas = c.fetchone()[0]
    c.execute('SELECT SUM(r.quantity * m.price) FROM reservations r JOIN miniaturas m ON r.miniatura_id = m.id')
    total_valor = c.fetchone()[0] or 0
    c.execute('SELECT COUNT(DISTINCT user_id) FROM reservations')
    total_clientes = c.fetchone()[0]
    
    stats = f'''
    <div class="bg-gradient-to-br from-blue-600 to-blue-800 text-white p-6 rounded-lg shadow-lg border-2 border-blue-400">
        <div class="flex items-center justify-between">
            <div><p class="text-blue-200 text-sm font-semibold">📦 Total de Reservas</p><p class="text-4xl font-black mt-2">{total_reservas}</p></div>
            <i class="fas fa-shopping-bag text-6xl opacity-30"></i>
        </div>
    </div>
    <div class="bg-gradient-to-br from-green-600 to-green-800 text-white p-6 rounded-lg shadow-lg border-2 border-green-400">
        <div class="flex items-center justify-between">
            <div><p class="text-green-200 text-sm font-semibold">💰 Valor Total</p><p class="text-4xl font-black mt-2">R$ {total_valor:.2f}</p></div>
            <i class="fas fa-dollar-sign text-6xl opacity-30"></i>
        </div>
    </div>
    <div class="bg-gradient-to-br from-purple-600 to-purple-800 text-white p-6 rounded-lg shadow-lg border-2 border-purple-400">
        <div class="flex items-center justify-between">
            <div><p class="text-purple-200 text-sm font-semibold">👥 Total de Clientes</p><p class="text-4xl font-black mt-2">{total_clientes}</p></div>
            <i class="fas fa-users text-6xl opacity-30"></i>
        </div>
    </div>
    <div class="bg-gradient-to-br from-red-600 to-red-800 text-white p-6 rounded-lg shadow-lg border-2 border-red-400">
        <div class="flex items-center justify-between">
            <div><p class="text-red-200 text-sm font-semibold">📊 Ticket Médio</p><p class="text-4xl font-black mt-2">R$ {(total_valor/total_reservas if total_reservas > 0 else 0):.2f}</p></div>
            <i class="fas fa-chart-line text-6xl opacity-30"></i>
        </div>
    </div>
    '''
    
    # Filtros
    c.execute('SELECT DISTINCT u.id, u.name FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY u.name')
    clientes = c.fetchall()
    
    c.execute('SELECT DISTINCT m.id, m.name FROM reservations r JOIN miniaturas m ON r.miniatura_id = m.id ORDER BY m.name')
    miniaturas_list = c.fetchall()
    
    # Query com filtros
    query = '''SELECT r.created_at, u.email, u.name, u.phone, m.name, r.quantity, m.price 
               FROM reservations r 
               JOIN users u ON r.user_id = u.id 
               JOIN miniaturas m ON r.miniatura_id = m.id 
               WHERE 1=1'''
    params = []
    
    if cliente_filter:
        query += ' AND u.id = ?'
        params.append(cliente_filter)
    
    if miniatura_filter:
        query += ' AND m.id = ?'
        params.append(miniatura_filter)
    
    query += ' ORDER BY r.created_at DESC LIMIT 50'
    
    c.execute(query, params)
    reservas = c.fetchall()
    conn.close()
    
    # HTML filtros
    filtros = '<div class="mb-6 flex gap-4 flex-wrap"><form method="GET" class="flex gap-4 flex-wrap">'
    filtros += '<select name="cliente" class="bg-slate-700 text-white px-4 py-2 rounded-lg border border-blue-600"><option value="">Todos os Clientes</option>'
    for cliente in clientes:
        selected = 'selected' if str(cliente[0]) == cliente_filter else ''
        filtros += f'<option value="{cliente[0]}" {selected}>{cliente[1]}</option>'
    filtros += '</select>'
    
    filtros += '<select name="miniatura" class="bg-slate-700 text-white px-4 py-2 rounded-lg border border-blue-600"><option value="">Todas as Miniaturas</option>'
    for miniatura in miniaturas_list:
        selected = 'selected' if str(miniatura[0]) == miniatura_filter else ''
        filtros += f'<option value="{miniatura[0]}" {selected}>{miniatura[1]}</option>'
    filtros += '</select>'
    
    filtros += '<button type="submit" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition">🔍 Filtrar</button>'
    filtros += '<a href="/admin" class="bg-slate-600 hover:bg-slate-700 text-white px-4 py-2 rounded-lg transition">Limpar</a>'
    filtros += '</form></div>'
    
    html = filtros
    html += '<h2 class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-6">📋 Reservas</h2>'
    html += '<div class="overflow-x-auto"><table class="w-full"><thead class="bg-gradient-to-r from-blue-700 to-blue-900 text-white"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Cliente</th><th class="p-3 text-left">Email</th><th class="p-3 text-left">Telefone</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Qtd</th><th class="p-3 text-right">Valor</th></tr></thead><tbody>'
    
    total = 0
    for idx, r in enumerate(reservas):
        valor = r[5] * r[6]
        total += valor
        bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
        html += f'<tr class="{bg} border-b border-slate-600 hover:bg-blue-600 hover:bg-opacity-30 transition text-slate-200"><td class="p-3">{r[0][:10]}</td><td class="p-3 font-semibold text-blue-300">{r[2]}</td><td class="p-3 text-slate-400">{r[1]}</td><td class="p-3 text-slate-400">{r[3]}</td><td class="p-3">{r[4]}</td><td class="p-3 text-center">{r[5]}</td><td class="p-3 text-right font-bold text-red-400">R$ {valor:.2f}</td></tr>'
    
    html += f'<tr class="bg-gradient-to-r from-blue-700 to-red-700 font-black text-white"><td colspan="6" class="p-3">TOTAL</td><td class="p-3 text-right">R$ {total:.2f}</td></tr></tbody></table></div>'
    
    return render_template_string(ADMIN_HTML, content=html, stats=stats)

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
    
    if not m or m[0] &lt;= 0:
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
