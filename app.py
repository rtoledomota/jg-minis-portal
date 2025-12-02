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
    if not drive_url or 'drive.google.com' not in drive_url:
        return drive_url
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', drive_url)
    if match:
        file_id = match.group(1)
        return f'https://lh3.google.com/d/{file_id}=w1000'
    return drive_url

def load_from_google_sheets():
    try:
        print("📊 Carregando dados...")
        response = requests.get(SHEET_URL, timeout=10)
        response.encoding = 'utf-8'
        csv_reader = csv.reader(StringIO(response.text))
        rows = list(csv_reader)
        
        if len(rows) < 2:
            return None
        
        headers = [h.strip().upper() for h in rows[0]]
        
        try:
            idx_imagem = headers.index('IMAGEM')
            idx_nome = headers.index('NOME DA MINIATURA')
            idx_chegada = headers.index('PREVISÃO DE CHEGADA')
            idx_qtd = headers.index('QUANTIDADE DISPONIVEL')
            idx_valor = headers.index('VALOR')
            idx_obs = headers.index('OBSERVAÇÕES')
            idx_max = headers.index('MAX_RESERVAS_POR_USUARIO')
        except ValueError:
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
            except:
                continue
        
        return miniaturas if miniaturas else None
    except Exception as e:
        print(f"❌ Erro: {e}")
        return None

def format_phone(phone):
    phone = re.sub(r'\D', '', phone)
    if len(phone) == 11:
        return f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    return phone

def validate_phone(phone):
    phone = re.sub(r'\D', '', phone)
    return len(phone) == 11

def init_db():
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
              ('Admin', 'admin@example.com', generate_password_hash('SenhaForte123!'), '(11) 99999-9999', True, datetime.now().isoformat()))
    c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              ('Usuário Teste', 'usuario@example.com', generate_password_hash('Usuario123!'), '(11) 98888-8888', False, datetime.now().isoformat()))
    
    miniaturas = load_from_google_sheets()
    if miniaturas:
        c.executemany('INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)', miniaturas)
    
    conn.commit()
    conn.close()
    print("✅ BD inicializado")

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

LOGIN_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JG MINIS</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body class="bg-gradient-to-br from-slate-900 via-blue-900 to-black min-h-screen flex items-center justify-center"><div class="w-full max-w-md"><div class="bg-gradient-to-b from-slate-800 to-black rounded-2xl shadow-2xl overflow-hidden border-2 border-red-600"><div class="bg-gradient-to-r from-blue-700 via-blue-900 to-black p-8 text-center border-b-2 border-red-600"><div class="w-32 h-32 bg-black rounded-xl flex items-center justify-center mx-auto mb-4 shadow-2xl border-2 border-red-600 overflow-hidden"><img src="{{ logo_url }}" alt="Logo" class="w-full h-full object-contain p-2"></div><h2 class="text-4xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">JG MINIS</h2><p class="text-blue-300 font-semibold">Portal de Miniaturas Premium</p></div><div class="p-8"><div class="flex gap-4 mb-6 border-b border-slate-600"><button class="nav-tab active pb-4 px-4 font-bold text-red-500 border-b-2 border-red-500 cursor-pointer" onclick="switchTab('login')">Login</button><button class="nav-tab pb-4 px-4 font-bold text-slate-400 cursor-pointer hover:text-blue-400" onclick="switchTab('register')">Cadastro</button></div><div id="login" class="tab-content">{% if error %}<div class="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg mb-4"><i class="fas fa-exclamation-circle"></i> {{ error }}</div>{% endif %}<form method="POST" action="/login"><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">📧 Email</label><input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500" required></div><div class="mb-6"><label class="block text-slate-300 font-bold mb-2">🔐 Senha</label><input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2 focus:outline-none focus:border-red-500" required></div><button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-3 rounded-lg hover:shadow-2xl">Entrar</button></form></div><div id="register" class="tab-content hidden">{% if register_error %}<div class="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg mb-4">{{ register_error }}</div>{% endif %}<form method="POST" action="/register"><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">👤 Nome</label><input type="text" name="name" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">📧 Email</label><input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">📱 Tel (11) xxxxx-xxxx</label><input type="tel" name="phone" placeholder="(11) 99999-9999" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">🔐 Senha (min 8)</label><input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" minlength="8" required></div><div class="mb-6"><label class="block text-slate-300 font-bold mb-2">🔐 Confirme</label><input type="password" name="confirm_password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" minlength="8" required></div><button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-3 rounded-lg">Cadastrar</button></form></div></div></div></div><script>function switchTab(t){document.querySelectorAll('.tab-content').forEach(e=>e.classList.add('hidden'));document.getElementById(t).classList.remove('hidden');}</script></body></html>'''

HOME_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JG MINIS</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span><div class="flex gap-4"><a href="/minhas-reservas" class="bg-blue-600 text-white px-4 py-2 rounded-lg">Minhas Reservas</a>{% if is_admin %}<a href="/admin" class="bg-red-600 text-white px-4 py-2 rounded-lg">Admin</a>{% endif %}<a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded-lg">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12"><h1 class="text-4xl font-black text-blue-400 mb-2">Catálogo</h1><p class="text-slate-300 mb-8">Pré vendas</p><div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">{{ miniaturas|safe }}</div></div></body></html>'''

MINHAS_RESERVAS_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Minhas Reservas</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-blue-400">JG MINIS</span><div class="flex gap-4"><a href="/" class="bg-blue-600 text-white px-4 py-2 rounded-lg">Catálogo</a><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded-lg">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12"><h1 class="text-4xl font-black text-blue-400 mb-8">Minhas Reservas</h1><div class="bg-slate-800 rounded-xl p-8 border-2 border-blue-600">{{ content|safe }}</div></div></body></html>'''

ADMIN_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between"><span class="text-3xl font-black text-blue-400">Admin</span><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded-lg">Sair</a></div></nav><div class="container mx-auto px-4 py-8">{{ content|safe }}</div></body></html>'''

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
        return render_template_string(LOGIN_HTML, error='Email ou senha inválidos', register_error='', logo_url=LOGO_URL)
    return render_template_string(LOGIN_HTML, error='', register_error='', logo_url=LOGO_URL)

@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    
    if not validate_phone(phone):
        return render_template_string(LOGIN_HTML, error='', register_error='Telefone inválido', logo_url=LOGO_URL)
    if password != confirm or len(password) < 8:
        return render_template_string(LOGIN_HTML, error='', register_error='Senhas inválidas', logo_url=LOGO_URL)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE email = ?', (email,))
    if c.fetchone():
        conn.close()
        return render_template_string(LOGIN_HTML, error='', register_error='Email existe', logo_url=LOGO_URL)
    
    c.execute('INSERT INTO users (name, email, password, phone, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
              (name, email, generate_password_hash(password), format_phone(phone), False, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return render_template_string(LOGIN_HTML, error='', register_error='Cadastro OK! Faça login', logo_url=LOGO_URL)

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
        html += f'''<div class="bg-slate-800 rounded-xl border-2 border-blue-600 overflow-hidden"><div class="bg-black h-48 flex items-center justify-center"><img src="{m[1]}" class="w-full h-full object-cover" alt="{m[2]}" onerror="this.innerHTML='🎲'"></div><div class="p-4"><h3 class="font-bold text-blue-300">{m[2]}</h3><p class="text-sm text-slate-400 mb-2">Chegada: {m[3]}</p><p class="text-lg font-bold text-red-400">R$ {m[5]:.2f}</p><button onclick="reservar({m[0]})" class="w-full mt-2 bg-blue-600 text-white px-4 py-2 rounded-lg" {'disabled' if m[4] == 0 else ''}>Reservar ({m[4]})</button></div></div>'''
    
    return render_template_string(HOME_HTML, miniaturas=html, is_admin=request.user.get('is_admin', False), logo_url=LOGO_URL)

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
        html = '<p class="text-slate-400">Nenhuma reserva ainda</p>'
    else:
        html = '<table class="w-full text-slate-200"><tr class="border-b border-slate-600"><th class="p-2 text-left">Data</th><th class="p-2 text-left">Produto</th><th class="p-2 text-center">Qtd</th><th class="p-2 text-right">Total</th></tr>'
        total = 0
        for r in reservas:
            subtotal = r[2] * r[3]
            total += subtotal
            html += f'<tr class="border-b border-slate-600"><td class="p-2">{r[0][:10]}</td><td class="p-2">{r[1]}</td><td class="p-2 text-center">{r[3]}</td><td class="p-2 text-right">R$ {subtotal:.2f}</td></tr>'
        html += f'<tr class="font-bold"><td colspan="3" class="p-2">TOTAL</td><td class="p-2 text-right">R$ {total:.2f}</td></tr></table>'
    
    return render_template_string(MINHAS_RESERVAS_HTML, content=html)

@app.route('/admin')
@admin_required
def admin():
    cliente_filter = request.args.get('cliente', '')
    miniatura_filter = request.args.get('miniatura', '')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    query = 'SELECT r.created_at, u.name, m.name, r.quantity, m.price FROM reservations r JOIN users u ON r.user_id = u.id JOIN miniaturas m ON r.miniatura_id = m.id WHERE 1=1'
    params = []
    
    if cliente_filter:
        query += ' AND u.id = ?'
        params.append(cliente_filter)
    if miniatura_filter:
        query += ' AND m.id = ?'
        params.append(miniatura_filter)
    
    query += ' ORDER BY r.created_at DESC'
    
    c.execute(query, params)
    reservas = c.fetchall()
    
    c.execute('SELECT DISTINCT u.id, u.name FROM reservations r JOIN users u ON r.user_id = u.id ORDER BY u.name')
    clientes = c.fetchall()
    
    c.execute('SELECT DISTINCT m.id, m.name FROM reservations r JOIN miniaturas m ON r.miniatura_id = m.id ORDER BY m.name')
    miniaturas_list = c.fetchall()
    
    conn.close()
    
    filtros = '<div class="mb-6 flex gap-4 flex-wrap"><form method="GET" class="flex gap-4"><select name="cliente" class="bg-slate-700 text-white px-4 py-2 rounded-lg"><option>Todos</option>'
    for cli in clientes:
        selected = 'selected' if str(cli[0]) == cliente_filter else ''
        filtros += f'<option value="{cli[0]}" {selected}>{cli[1]}</option>'
    filtros += '</select><select name="miniatura" class="bg-slate-700 text-white px-4 py-2 rounded-lg"><option>Todas</option>'
    for mini in miniaturas_list:
        selected = 'selected' if str(mini[0]) == miniatura_filter else ''
        filtros += f'<option value="{mini[0]}" {selected}>{mini[1]}</option>'
    filtros += '</select><button class="bg-blue-600 text-white px-4 py-2 rounded-lg">Filtrar</button></form></div>'
    
    html = filtros + '<table class="w-full text-slate-200 text-sm"><tr class="border-b"><th class="p-2 text-left">Data</th><th class="p-2 text-left">Cliente</th><th class="p-2 text-left">Produto</th><th class="p-2 text-center">Qtd</th><th class="p-2 text-right">Total</th></tr>'
    total = 0
    for r in reservas:
        subtotal = r[3] * r[4]
        total += subtotal
        html += f'<tr class="border-b border-slate-600"><td class="p-2">{r[0][:10]}</td><td class="p-2">{r[1]}</td><td class="p-2">{r[2]}</td><td class="p-2 text-center">{r[3]}</td><td class="p-2 text-right">R$ {subtotal:.2f}</td></tr>'
    html += f'<tr class="font-bold"><td colspan="4" class="p-2">TOTAL</td><td class="p-2 text-right">R$ {total:.2f}</td></tr></table>'
    
    return render_template_string(ADMIN_HTML, content=html)

@app.route('/reservar', methods=['POST'])
@login_required
def reservar():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    user_id = request.user.get('user_id')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT stock FROM miniaturas WHERE id = ?', (miniatura_id,))
    m = c.fetchone()
    
    if not m or m[0] <= 0:
        conn.close()
        return jsonify({'error': 'Sem estoque'}), 400
    
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
