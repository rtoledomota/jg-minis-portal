import os
import sqlite3
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, redirect, url_for, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import re
import requests
import csv
from io import StringIO, BytesIO
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

app = Flask(__name__)
app.secret_key = 'jg_minis_secret_key_2024'

DB_FILE = 'jg_minis.db'
SHEET_ID = '1sxlvo6j-UTB0xXuyivzWnhRuYvpJFcH2smL4ZzHTUps'
SHEET_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv'
LOGO_URL = 'https://i.imgur.com/Yp1OiWB.png'
WHATSAPP_NUMERO = '5511949094290'

scheduler = BackgroundScheduler()
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

def convert_drive_url(drive_url):
    if not drive_url or 'drive.google.com' not in drive_url:
        return drive_url
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', drive_url)
    if match:
        file_id = match.group(1)
        return f'https://drive.google.com/uc?export=view&id={file_id}'
    return drive_url

def load_from_google_sheets():
    try:
        print("Carregando dados da planilha...")
        response = requests.get(SHEET_URL, timeout=10)
        response.encoding = 'utf-8'
        csv_reader = csv.reader(StringIO(response.text))
        rows = list(csv_reader)
        
        if len(rows) < 2:
            print("Planilha vazia")
            return None
        
        headers = [h.strip().upper() for h in rows[0]]
        
        try:
            idx_imagem = headers.index('IMAGEM')
            idx_nome = headers.index('NOME DA MINIATURA')
            idx_chegada = headers.index('PREVISAO DE CHEGADA')
            idx_qtd = headers.index('QUANTIDADE DISPONIVEL')
            idx_valor = headers.index('VALOR')
            idx_obs = headers.index('OBSERVACOES')
            idx_max = headers.index('MAX_RESERVAS_POR_USUARIO')
        except ValueError as e:
            print(f"Coluna nao encontrada: {e}")
            return None
        
        miniaturas = []
        for i, row in enumerate(rows[1:], start=2):
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
                url_convertida = convert_drive_url(imagem)
                miniaturas.append((url_convertida, nome, row[idx_chegada].strip(), qtd, valor, row[idx_obs].strip(), max_res))
                print(f"  OK {nome} - R$ {valor:.2f} ({qtd} em estoque)")
            except Exception as e:
                print(f"  ERRO Linha {i}: {e}")
                continue
        
        print(f"OK Total carregado: {len(miniaturas)} miniaturas\n")
        return miniaturas if miniaturas else None
    except Exception as e:
        print(f"ERRO ao carregar planilha: {e}\n")
        return None

def atualizar_miniaturas():
    print(f"SINCRONIZACAO {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} - Buscando dados...")
    
    try:
        miniaturas = load_from_google_sheets()
        
        if not miniaturas:
            print("Nenhuma miniatura carregada\n")
            return False
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        c.execute('DELETE FROM miniaturas')
        c.executemany('INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)', miniaturas)
        
        conn.commit()
        conn.close()
        
        print(f"OK SINCRONIZACAO {len(miniaturas)} miniaturas atualizadas!\n")
        return True
    except Exception as e:
        print(f"ERRO na sincronizacao: {e}\n")
        return False

def format_phone(phone):
    phone = re.sub(r'\D', '', phone)
    if len(phone) == 11:
        return f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    return phone

def validate_phone(phone):
    phone = re.sub(r'\D', '', phone)
    return len(phone) == 11

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, 
        name TEXT,
        email TEXT UNIQUE, 
        password TEXT, 
        phone TEXT, 
        is_admin BOOLEAN, 
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS miniaturas (
        id INTEGER PRIMARY KEY, 
        image_url TEXT, 
        name TEXT, 
        arrival_date TEXT, 
        stock INTEGER, 
        price REAL, 
        observations TEXT, 
        max_reservations_per_user INTEGER
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY, 
        user_id INTEGER, 
        miniatura_id INTEGER, 
        quantity INTEGER, 
        created_at TEXT, 
        FOREIGN KEY(user_id) REFERENCES users(id), 
        FOREIGN KEY(miniatura_id) REFERENCES miniaturas(id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS fila_espera (
        id INTEGER PRIMARY KEY, 
        user_id INTEGER, 
        miniatura_id INTEGER, 
        created_at TEXT, 
        FOREIGN KEY(user_id) REFERENCES users(id), 
        FOREIGN KEY(miniatura_id) REFERENCES miniaturas(id)
    )''')
    
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                  ('Admin', 'admin@example.com', generate_password_hash('admin123'), '(11) 99999-9999', True, datetime.now().isoformat()))
        c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                  ('Usuario Teste', 'usuario@example.com', generate_password_hash('usuario123'), '(11) 98888-8888', False, datetime.now().isoformat()))
    
    c.execute('SELECT COUNT(*) FROM miniaturas')
    if c.fetchone()[0] == 0:
        miniaturas = load_from_google_sheets()
        if miniaturas:
            c.executemany('INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)', miniaturas)
    
    conn.commit()
    conn.close()
    print("OK BD inicializado\n")

def get_token(user_id, is_admin):
    return jwt.encode({'user_id': user_id, 'is_admin': is_admin, 'exp': datetime.utcnow() + timedelta(hours=24)}, app.secret_key, algorithm='HS256')

def verify_token(token):
    try:
        return jwt.decode(token, app.secret_key, algorithms=['HS256'])
    except:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token')
        if not token or not verify_token(token):
            return redirect(url_for('login'))
        request.user = verify_token(token)
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
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

HOME_HTML = '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JG MINIS</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
<nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
<div class="container mx-auto px-4 py-4 flex justify-between items-center">
<span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span>
<div class="flex gap-4">
<a href="/minhas-reservas" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold"><i class="fas fa-list mr-2"></i>Minhas Reservas</a>
{% if is_admin %}
<a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold"><i class="fas fa-chart-bar mr-2"></i>Admin</a>
{% endif %}
<a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a>
</div>
</div>
</nav>
<div class="container mx-auto px-4 py-12">
<h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2"><i class="fas fa-cube mr-2"></i>Catalogo de Miniaturas</h1>
<p class="text-slate-300 mb-8">Pre vendas</p>
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
{{ miniaturas|safe }}
</div>
</div>
<div id="confirmModal" class="hidden fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
<div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl max-w-md w-full p-8">
<h2 class="text-2xl font-black text-blue-400 mb-4"><i class="fas fa-shopping-cart mr-2"></i>Confirmar Reserva</h2>
<div id="confirmContent" class="text-slate-300 mb-6 space-y-3"></div>
<div class="mb-4">
<label class="block text-slate-300 font-bold mb-2">Quantidade:</label>
<div class="flex gap-2">
<button type="button" onclick="decrementarQtd()" class="bg-red-600 hover:bg-red-700 text-white font-bold w-12 h-12 rounded-lg">-</button>
<input type="number" id="quantidadeInput" value="1" min="1" class="flex-1 bg-slate-700 text-white font-bold text-center rounded-lg border-2 border-blue-600" onchange="validarQuantidade()">
<button type="button" onclick="incrementarQtd()" class="bg-green-600 hover:bg-green-700 text-white font-bold w-12 h-12 rounded-lg">+</button>
</div>
<p id="erroQtd" class="text-red-400 text-sm mt-2 hidden"></p>
</div>
<div class="flex gap-4">
<button onclick="fecharModal()" class="flex-1 bg-slate-700 hover:bg-slate-600 text-white font-bold py-2 rounded-lg">Cancelar</button>
<button onclick="confirmarReserva()" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 hover:from-blue-700 hover:to-red-700 text-white font-bold py-2 rounded-lg">Confirmar</button>
</div>
</div>
</div>
<script>
let reservaAtual = null;
let maxQtd = 1;

function abrirConfirmacao(id, nome, preco, stock, max) {
  reservaAtual = id;
  maxQtd = Math.min(stock, max);
  document.getElementById("quantidadeInput").max = maxQtd;
  document.getElementById("quantidadeInput").value = 1;
  document.getElementById("confirmContent").innerHTML = `
    <p><strong>Produto:</strong> ${nome}</p>
    <p><strong>Valor Unitario:</strong> R$ ${parseFloat(preco).toFixed(2)}</p>
    <p><strong>Disponivel:</strong> ${stock} unidades</p>
    <p><strong>Maximo por Cliente:</strong> ${max}</p>
    <p class="text-yellow-300 text-sm mt-2"><i class="fas fa-info-circle"></i> Confirmar esta reserva?</p>
  `;
  document.getElementById("confirmModal").classList.remove("hidden");
}

function fecharModal() {
  document.getElementById("confirmModal").classList.add("hidden");
  reservaAtual = null;
  document.getElementById("erroQtd").classList.add("hidden");
}

function decrementarQtd() {
  let input = document.getElementById("quantidadeInput");
  if (input.value > 1) input.value = parseInt(input.value) - 1;
}

function incrementarQtd() {
  let input = document.getElementById("quantidadeInput");
  if (parseInt(input.value) < maxQtd) input.value = parseInt(input.value) + 1;
}

function validarQuantidade() {
  let input = document.getElementById("quantidadeInput");
  let erro = document.getElementById("erroQtd");
  if (parseInt(input.value) > maxQtd) {
    input.value = maxQtd;
    erro.textContent = "Limite maximo atingido!";
    erro.classList.remove("hidden");
  } else {
    erro.classList.add("hidden");
  }
}

function confirmarReserva() {
  if (!reservaAtual) return;
  let qtd = parseInt(document.getElementById("quantidadeInput").value);
  fetch("/reservar", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({miniatura_id: reservaAtual, quantidade: qtd})
  }).then(r => r.json()).then(data => {
    if (data.success) {
      alert("OK Reserva realizada com sucesso!");
      location.reload();
    } else {
      alert("ERRO: " + data.error);
    }
  }).catch(e => {
    alert("ERRO na requisicao");
  });
  fecharModal();
}
</script>
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
        return redirect(url_for('login'))
    return '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JG MINIS - Login</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-br from-slate-900 via-blue-900 to-black min-h-screen flex items-center justify-center">
<div class="w-full max-w-md">
<div class="bg-gradient-to-b from-slate-800 to-black rounded-2xl shadow-2xl overflow-hidden border-2 border-red-600">
<div class="bg-gradient-to-r from-blue-700 via-blue-900 to-black p-8 text-center border-b-2 border-red-600">
<div class="w-32 h-32 bg-black rounded-xl flex items-center justify-center mx-auto mb-4 shadow-2xl border-2 border-red-600 overflow-hidden">
<img src="''' + LOGO_URL + '''" alt="Logo" class="w-full h-full object-contain p-2">
</div>
<h2 class="text-4xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">JG MINIS</h2>
<p class="text-blue-300 font-semibold">Portal de Miniaturas Premium</p>
</div>
<div class="p-8">
<div class="flex gap-4 mb-6 border-b border-slate-600">
<button class="nav-tab active pb-4 px-4 font-bold text-red-500 border-b-2 border-red-500 cursor-pointer" onclick="switchTab('login')">Login</button>
<button class="nav-tab pb-4 px-4 font-bold text-slate-400 cursor-pointer hover:text-blue-400" onclick="switchTab('register')">Cadastro</button>
</div>
<div id="login" class="tab-content">
<form method="POST" action="/login">
<div class="mb-4">
<label class="block text-slate-300 font-bold mb-2">Email</label>
<input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500" required>
</div>
<div class="mb-6">
<label class="block text-slate-300 font-bold mb-2">Senha</label>
<input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500" required>
</div>
<button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-3 rounded-lg hover:shadow-2xl">Entrar</button>
</form>
<hr class="my-4 border-slate-600">
<div class="text-xs text-slate-400">
<p><strong>Admin:</strong> admin@example.com / admin123</p>
</div>
</div>
<div id="register" class="tab-content hidden">
<form method="POST" action="/register">
<div class="mb-4">
<label class="block text-slate-300 font-bold mb-2">Nome</label>
<input type="text" name="name" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required>
</div>
<div class="mb-4">
<label class="block text-slate-300 font-bold mb-2">Email</label>
<input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required>
</div>
<div class="mb-4">
<label class="block text-slate-300 font-bold mb-2">Telefone (11) xxxxx-xxxx</label>
<input type="tel" name="phone" placeholder="(11) 99999-9999" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required>
</div>
<div class="mb-4">
<label class="block text-slate-300 font-bold mb-2">Senha (min 8)</label>
<input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" minlength="8" required>
</div>
<div class="mb-6">
<label class="block text-slate-300 font-bold mb-2">Confirme a Senha</label>
<input type="password" name="confirm_password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" minlength="8" required>
</div>
<button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-3 rounded-lg">Cadastrar</button>
</form>
</div>
</div>
</div>
</div>
<script>
function switchTab(t) {
  document.querySelectorAll(".tab-content").forEach(e => e.classList.add("hidden"));
  document.getElementById(t).classList.remove("hidden");
}
</script>
</body>
</html>'''

@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    
    if not validate_phone(phone):
        return redirect(url_for('login'))
    if password != confirm or len(password) < 8:
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE email = ?', (email,))
    if c.fetchone():
        conn.close()
        return redirect(url_for('login'))
    
    c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (name, email, generate_password_hash(password), format_phone(phone), False, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return redirect(url_for('login'))

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
    c.execute('SELECT id, image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas')
    miniaturas = c.fetchall()
    conn.close()
    
    html = ''
    for m in miniaturas:
        is_esgotado = m[4] <= 0
        status_text = 'ESGOTADO' if is_esgotado else f'Em Estoque: {m[4]}'
        
        if is_esgotado:
            html += f'''<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-red-600 overflow-hidden">
                <div class="bg-black h-48 flex items-center justify-center relative overflow-hidden">
                    <img src="{m[1]}" class="w-full h-full object-cover opacity-50" alt="{m[2]}" onerror="this.style.background='linear-gradient(135deg, #1e40af 0%, #7c3aed 100%)'; this.innerHTML='ERRO'">
                    <div class="absolute top-3 right-3 bg-red-600 text-white px-3 py-1 rounded-full text-sm font-bold">{status_text}</div>
                </div>
                <div class="p-4">
                    <h3 class="font-bold text-blue-300 mb-2 text-lg">{m[2]}</h3>
                    <p class="text-sm text-slate-400 mb-2">Chegada: {m[3]}</p>
                    <p class="text-sm text-slate-400 mb-3">{m[6]}</p>
                    <span class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">R$ {m[5]:.2f}</span>
                </div>
            </div>'''
        else:
            html += f'''<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden">
                <div class="bg-black h-48 flex items-center justify-center relative overflow-hidden">
                    <img src="{m[1]}" class="w-full h-full object-cover" alt="{m[2]}" onerror="this.style.background='linear-gradient(135deg, #1e40af 0%, #7c3aed 100%)'; this.innerHTML='ERRO'">
                    <div class="absolute top-3 right-3 bg-green-600 text-white px-3 py-1 rounded-full text-sm font-bold">{status_text}</div>
                </div>
                <div class="p-4">
                    <h3 class="font-bold text-blue-300 mb-2 text-lg">{m[2]}</h3>
                    <p class="text-sm text-slate-400 mb-2">Chegada: {m[3]}</p>
                    <p class="text-sm text-slate-400 mb-3">{m[6]}</p>
                    <div class="flex justify-between items-center gap-2">
                        <span class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">R$ {m[5]:.2f}</span>
                        <button onclick="abrirConfirmacao({m[0]}, '{m[2]}', {m[5]}, {m[4]}, {m[7]})" class="bg-gradient-to-r from-blue-600 to-red-600 hover:from-blue-700 hover:to-red-700 text-white font-bold px-4 py-2 rounded-lg">Reservar</button>
                    </div>
                </div>
            </div>'''
    
    return render_template_string(HOME_HTML, miniaturas=html, is_admin=request.user.get('is_admin', False))

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    user_id = request.user.get('user_id')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT r.created_at, m.name, m.price, r.quantity FROM reservations r JOIN miniaturas m ON r.miniatura_id = m.id WHERE r.user_id = ? ORDER BY r.created_at DESC', (user_id,))
    reservas = c.fetchall()
    conn.close()
    
    if not reservas:
        html = '<p class="text-slate-400 text-center py-8">Nenhuma reserva ainda</p>'
    else:
        html = '<table class="w-full text-slate-200"><thead class="bg-blue-700"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Quantidade</th><th class="p-3 text-right">Valor Unit</th><th class="p-3 text-right">Total</th></tr></thead><tbody>'
        total = 0
        for idx, r in enumerate(reservas):
            subtotal = r[2] * r[3]
            total += subtotal
            bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
            html += f'<tr class="{bg} border-b border-slate-600"><td class="p-3">{r[0][:10]}</td><td class="p-3 font-semibold text-blue-300">{r[1]}</td><td class="p-3 text-center font-bold text-red-400">{r[3]}</td><td class="p-3 text-right">R$ {r[2]:.2f}</td><td class="p-3 text-right font-bold text-red-400">R$ {subtotal:.2f}</td></tr>'
        html += f'<tr class="bg-gradient-to-r from-blue-700 to-red-700 font-black text-white"><td colspan="4" class="p-3 text-right">TOTAL:</td><td class="p-3 text-right">R$ {total:.2f}</td></tr></tbody></table>'
    
    return '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Minhas Reservas</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
<nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600">
<div class="container mx-auto px-4 py-4 flex justify-between items-center">
<span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span>
<div class="flex gap-4">
<a href="/" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg">Catalogo</a>
<a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg">Sair</a>
</div>
</div>
</nav>
<div class="container mx-auto px-4 py-12">
<h1 class="text-4xl font-black text-blue-400 mb-8">Minhas Reservas</h1>
<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl p-8 border-2 border-blue-600">
''' + html + '''
</div>
</div>
</body>
</html>'''

@app.route('/admin')
@admin_required
def admin():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) FROM reservations')
    total_reservas = c.fetchone()[0]
    c.execute('SELECT SUM(r.quantity * m.price) FROM reservations r JOIN miniaturas m ON r.miniatura_id = m.id')
    total_valor = c.fetchone()[0] or 0
    
    c.execute('SELECT r.created_at, u.name, u.email, u.phone, m.name, r.quantity, m.price FROM reservations r JOIN users u ON r.user_id = u.id JOIN miniaturas m ON r.miniatura_id = m.id ORDER BY r.created_at DESC')
    reservas = c.fetchall()
    conn.close()
    
    html = f'<h2 class="text-3xl font-black text-blue-400 mb-6">Total Reservas: {total_reservas} | Total: R$ {total_valor:.2f}</h2>'
    html += '<div class="overflow-x-auto"><table class="w-full text-slate-200 text-sm"><thead class="bg-gradient-to-r from-blue-700 to-blue-900"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Cliente</th><th class="p-3 text-left">Email</th><th class="p-3 text-left">Telefone</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Qtd</th><th class="p-3 text-right">Total</th></tr></thead><tbody>'
    
    total = 0
    for idx, r in enumerate(reservas):
        subtotal = r[5] * r[6]
        total += subtotal
        bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
        html += f'<tr class="{bg} border-b border-slate-600"><td class="p-3">{r[0][:10]}</td><td class="p-3 font-semibold text-blue-300">{r[1]}</td><td class="p-3 text-slate-400">{r[2]}</td><td class="p-3 text-slate-400">{r[3]}</td><td class="p-3">{r[4]}</td><td class="p-3 text-center font-bold text-red-400">{r[5]}</td><td class="p-3 text-right font-bold text-red-400">R$ {subtotal:.2f}</td></tr>'
    
    html += f'<tr class="bg-gradient-to-r from-blue-700 to-red-700 font-black text-white"><td colspan="6" class="p-3 text-right">TOTAL GERAL</td><td class="p-3 text-right">R$ {total:.2f}</td></tr></tbody></table></div>'
    html += '<button onclick="sincronizarAgora()" class="mt-4 bg-gradient-to-r from-purple-600 to-purple-700 hover:from-purple-700 hover:to-purple-800 text-white px-6 py-2 rounded-lg font-bold transition">Sincronizar Agora</button>'
    
    return '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
<nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600">
<div class="container mx-auto px-4 py-4 flex justify-between items-center">
<span class="text-3xl font-black text-blue-400">Admin</span>
<a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg">Sair</a>
</div>
</nav>
<div class="container mx-auto px-4 py-8">
<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-2xl p-8 border-2 border-blue-600">
''' + html + '''
</div>
</div>
<script>
function sincronizarAgora() {
  fetch("/sincronizar-agora", {method: "POST", headers: {"Content-Type": "application/json"}})
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      alert("OK Sincronizacao realizada!");
      location.reload();
    } else {
      alert("ERRO: " + data.error);
    }
  }).catch(e => {
    alert("ERRO na sincronizacao");
  });
}
</script>
</body>
</html>'''

@app.route('/reservar', methods=['POST'])
@login_required
def reservar():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    quantidade = data.get('quantidade', 1)
    user_id = request.user.get('user_id')
    
    try:
        quantidade = int(quantidade)
        if quantidade < 1:
            return jsonify({'error': 'Quantidade invalida'}), 400
    except:
        return jsonify({'error': 'Quantidade invalida'}), 400
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT stock, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
    m = c.fetchone()
    
    if not m or m[0] <= 0:
        conn.close()
        return jsonify({'error': 'Sem estoque'}), 400
    
    if quantidade > m[0]:
        conn.close()
        return jsonify({'error': f'Apenas {m[0]} disponivel'}), 400
    
    c.execute('SELECT COALESCE(SUM(quantity), 0) FROM reservations WHERE user_id = ? AND miniatura_id = ?', (user_id, miniatura_id))
    qtd_reservada = c.fetchone()[0]
    
    if qtd_reservada + quantidade > m[1]:
        conn.close()
        return jsonify({'error': f'Limite maximo: {m[1]} unidades'}), 400
    
    c.execute('INSERT INTO reservations (user_id, miniatura_id, quantity, created_at) VALUES (?, ?, ?, ?)',
              (user_id, miniatura_id, quantidade, datetime.now().isoformat()))
    c.execute('UPDATE miniaturas SET stock = stock - ? WHERE id = ?', (quantidade, miniatura_id))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/sincronizar-agora', methods=['POST'])
@admin_required
def sincronizar_agora():
    try:
        resultado = atualizar_miniaturas()
        if resultado:
            return jsonify({'success': True, 'message': 'Sincronizacao realizada!'})
        else:
            return jsonify({'success': False, 'error': 'Erro ao sincronizar'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

scheduler.add_job(atualizar_miniaturas, 'cron', hour=0, minute=0, id='sync_sheets')

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
