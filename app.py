import os
import sqlite3
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import re
import requests
import csv
from io import StringIO
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import json

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

def validate_image_url(image_url):
    """Valida se a URL da imagem é do Imgur ou URL direta válida"""
    if not image_url or not image_url.strip():
        return None
    
    image_url = image_url.strip()
    
    # Aceita URLs do Imgur
    if 'imgur.com' in image_url:
        return image_url
    
    # Aceita outras URLs diretas de imagem (jpg, png, gif, webp)
    if any(image_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
        return image_url
    
    # Se não for válida, retorna None
    return None

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
        print(f"Colunas encontradas: {headers}")
        
        try:
            idx_imagem = headers.index('IMAGEM')
            idx_nome = headers.index('NOME DA MINIATURA')
            idx_chegada = headers.index('PREVISÃO DE CHEGADA')
            idx_qtd = headers.index('QUANTIDADE DISPONIVEL')
            idx_valor = headers.index('VALOR')
            idx_obs = headers.index('OBSERVAÇÕES')
            idx_max = headers.index('MAX_RESERVAS_POR_USUARIO')
        except ValueError as e:
            print(f"Coluna nao encontrada: {e}")
            print(f"Colunas disponiveis: {headers}")
            return None
        
        miniaturas = []
        for i, row in enumerate(rows[1:], start=2):
            while len(row) < len(headers):
                row.append('')
            
            imagem = row[idx_imagem].strip()
            nome = row[idx_nome].strip()
            
            if not imagem or not nome:
                continue
            
            # Valida a URL da imagem
            imagem_valida = validate_image_url(imagem)
            if not imagem_valida:
                print(f"  AVISO Linha {i}: URL de imagem inválida: {imagem}")
                continue
            
            try:
                qtd = int(row[idx_qtd].strip() or 0)
                valor = float(row[idx_valor].strip().replace(',', '.') or 0.0)
                max_res = int(row[idx_max].strip() or 3)
                
                miniaturas.append((imagem_valida, nome, row[idx_chegada].strip(), qtd, valor, row[idx_obs].strip(), max_res))
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

    # Nova tabela waiting_list
    c.execute('''CREATE TABLE IF NOT EXISTS waiting_list (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        miniatura_id INTEGER,
        email TEXT,
        phone TEXT,
        created_at TEXT,
        notified BOOLEAN DEFAULT FALSE,
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
    
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JG MINIS</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body class="bg-gradient-to-br from-slate-900 via-blue-900 to-black min-h-screen flex items-center justify-center"><div class="w-full max-w-md"><div class="bg-gradient-to-b from-slate-800 to-black rounded-2xl shadow-2xl overflow-hidden border-2 border-red-600"><div class="bg-gradient-to-r from-blue-700 via-blue-900 to-black p-8 text-center border-b-2 border-red-600"><div class="w-32 h-32 bg-black rounded-xl flex items-center justify-center mx-auto mb-4 shadow-2xl border-2 border-red-600 overflow-hidden"><img src="{logo_url_val}" alt="Logo" class="w-full h-full object-contain p-2"></div><h2 class="text-4xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">JG MINIS</h2><p class="text-blue-300 font-semibold">Portal de Miniaturas Premium</p></div><div class="p-8"><div class="flex gap-4 mb-6 border-b border-slate-600"><button class="nav-tab active pb-4 px-4 font-bold text-red-500 border-b-2 border-red-500 cursor-pointer" onclick="switchTab('login')">Login</button><button class="nav-tab pb-4 px-4 font-bold text-slate-400 cursor-pointer hover:text-blue-400" onclick="switchTab('register')">Cadastro</button></div><div id="login" class="tab-content"><form method="POST" action="/login"><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Email</label><input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-6"><label class="block text-slate-300 font-bold mb-2">Senha</label><input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-3 rounded-lg">Entrar</button></form></div><div id="register" class="tab-content hidden"><form method="POST" action="/register"><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Nome</label><input type="text" name="name" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Email</label><input type="email" name="email" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Telefone</label><input type="tel" name="phone" placeholder="(11) 99999-9999" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Senha</label><input type="password" name="password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" minlength="8" required></div><div class="mb-6"><label class="block text-slate-300 font-bold mb-2">Confirme Senha</label><input type="password" name="confirm_password" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" minlength="8" required></div><button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-3 rounded-lg">Cadastrar</button></form></div></div></div></div><script>function switchTab(t){{document.querySelectorAll(".tab-content").forEach(e=>e.classList.add("hidden"));document.getElementById(t).classList.remove("hidden");}}</script></body></html>"""
    return html.format(logo_url_val=LOGO_URL)

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
    
    user_id = request.user.get('user_id')
    c.execute('SELECT name, email, phone FROM users WHERE id = ?', (user_id,))
    user_data = c.fetchone()
    user_name, user_email, user_phone = user_data if user_data else ('', '', '')
    
    conn.close()
    
    items_html = ""
    for m in miniaturas:
        is_esgotado = m[4] <= 0
        status = "ESGOTADO" if is_esgotado else f"Em Estoque: {m[4]}"
        status_color = "red" if is_esgotado else "green"
        
        nome_json = json.dumps(m[2])
        
        button_html = ""
        if is_esgotado:
            whatsapp_link = f"https://wa.me/{WHATSAPP_NUMERO}?text=Olá%20JG%20MINIS,%20gostaria%20de%20informações%20sobre%20a%20miniatura:%20{m[2]}"
            button_html = f'<a href="{whatsapp_link}" target="_blank" class="bg-orange-600 hover:bg-orange-700 text-white font-bold px-4 py-2 rounded-lg">Entrar em Contato</a>'
        else:
            button_html = f'<button onclick="abrirConfirmacao({m[0]}, {nome_json}, {m[5]}, {m[4]}, {m[7]})" class="bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold px-4 py-2 rounded-lg">Reservar</button>'
        
        items_html += f'''<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden">
<div class="bg-black h-48 flex items-center justify-center relative overflow-hidden">
<img src="{m[1]}" class="w-full h-full object-cover" alt="{m[2]}" onerror="this.style.background='linear-gradient(135deg, #1e40af 0%, #7c3aed 100%)'">
<div class="absolute top-3 right-3 bg-{status_color}-600 text-white px-3 py-1 rounded-full text-sm font-bold">{status}</div>
</div>
<div class="p-4">
<h3 class="font-bold text-blue-300 mb-2 text-lg">{m[2]}</h3>
<p class="text-sm text-slate-400 mb-2">Chegada: {m[3]}</p>
<p class="text-sm text-slate-400 mb-3">{m[6]}</p>
<div class="flex justify-between items-center gap-2">
<span class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">R$ {m[5]:.2f}</span>
{button_html}
</div>
</div>
</div>'''
    
    admin_links = ''
    if request.user.get('is_admin'):
        admin_links = '''
            <a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Admin</a>
            <a href="/pessoas" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-lg font-semibold">Pessoas</a>
            <a href="/lista-espera" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold">Lista de Espera</a>
        '''
    
    page = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JG MINIS</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span><div class="flex gap-4"><a href="/minhas-reservas" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Minhas Reservas</a>{admin_links_val}<a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12"><h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">Catalogo de Miniaturas</h1><p class="text-slate-300 mb-8">Pre vendas</p><div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">{items_html_val}</div></div><div id="confirmModal" class="hidden fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50"><div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl max-w-md w-full p-8"><h2 class="text-2xl font-black text-blue-400 mb-4">Confirmar Reserva</h2><div id="confirmContent" class="text-slate-300 mb-6 space-y-3"></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Quantidade:</label><div class="flex gap-2"><button type="button" onclick="decrementarQtd()" class="bg-red-600 text-white font-bold w-12 h-12 rounded-lg">-</button><input type="number" id="quantidadeInput" value="1" min="1" class="flex-1 bg-slate-700 text-white font-bold text-center rounded-lg border-2 border-blue-600"><button type="button" onclick="incrementarQtd()" class="bg-green-600 text-white font-bold w-12 h-12 rounded-lg">+</button></div></div><div class="flex gap-4"><button onclick="fecharModal()" class="flex-1 bg-slate-700 text-white font-bold py-2 rounded-lg">Cancelar</button><button onclick="confirmarReserva()" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg">Confirmar</button></div></div></div><script>
let reservaAtual = null;
let maxQtd = 1;
let userId = {user_id_val};
let userEmail = "{user_email_val}";
let userPhone = "{user_phone_val}";

function abrirConfirmacao(id, nome, preco, stock, max) {{
  reservaAtual = id;
  maxQtd = Math.min(stock, max);
  document.getElementById("quantidadeInput").max = maxQtd;
  document.getElementById("quantidadeInput").value = 1;
  document.getElementById("confirmContent").innerHTML = "<p><strong>Produto:</strong> " + nome + "</p><p><strong>Valor:</strong> R$ " + parseFloat(preco).toFixed(2) + "</p><p><strong>Disponivel:</strong> " + stock + "</p>";
  document.getElementById("confirmModal").classList.remove("hidden");
}}
function fecharModal() {{
  document.getElementById("confirmModal").classList.add("hidden");
  reservaAtual = null;
}}
function decrementarQtd() {{
  let input = document.getElementById("quantidadeInput");
  if (input.value > 1) input.value = parseInt(input.value) - 1;
}}
function incrementarQtd() {{
  let input = document.getElementById("quantidadeInput");
  if (parseInt(input.value) < maxQtd) input.value = parseInt(input.value) + 1;
}}
function confirmarReserva() {{
  if (!reservaAtual) return;
  let qtd = parseInt(document.getElementById("quantidadeInput").value);
  fetch("/reservar", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{miniatura_id: reservaAtual, quantidade: qtd}})
  }}).then(r => r.json()).then(data => {{
    if (data.success) {{
      alert("OK Reserva realizada!");
      location.reload();
    }} else {{
      alert("ERRO: " + data.error);
    }}
  }}).catch(e => {{
    alert("ERRO na requisicao");
  }});
  fecharModal();
}}
</script></body></html>'''
    
    return page.format(user_id_val=user_id, user_email_val=user_email, user_phone_val=user_phone, admin_links_val=admin_links, items_html_val=items_html)

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
        html_content = '<p class="text-slate-400 text-center py-8">Nenhuma reserva</p>'
    else:
        html_content = '<table class="w-full text-slate-200"><thead class="bg-blue-700"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Qtd</th><th class="p-3 text-right">Valor</th><th class="p-3 text-right">Total</th></tr></thead><tbody>'
        total = 0
        for idx, r in enumerate(reservas):
            subtotal = r[2] * r[3]
            total += subtotal
            bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
            html_content += f'<tr class="{bg}"><td class="p-3">{r[0][:10]}</td><td class="p-3">{r[1]}</td><td class="p-3 text-center">{r[3]}</td><td class="p-3 text-right">R$ {r[2]:.2f}</td><td class="p-3 text-right">R$ {subtotal:.2f}</td></tr>'
        html_content += f'<tr class="bg-red-700 font-black text-white"><td colspan="4" class="p-3 text-right">TOTAL:</td><td class="p-3 text-right">R$ {total:.2f}</td></tr></tbody></table>'
    
    page = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Minhas Reservas</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-950 min-h-screen"><nav class="bg-blue-900 border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between"><span class="text-3xl font-black text-red-400">JG MINIS</span><div class="flex gap-4"><a href="/" class="bg-blue-600 text-white px-4 py-2 rounded">Catalogo</a><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded">Sair</a></div></div></nav><div class="container mx-auto px-4 py-8"><h1 class="text-4xl font-black text-blue-400 mb-8">Minhas Reservas</h1><div class="bg-slate-800 rounded-xl p-8 border-2 border-blue-600">{html_content_val}</div></div></body></html>'''
    return page.format(html_content_val=html_content)

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
    
    html_content = f'<h2 class="text-3xl font-black text-blue-400 mb-6">Total: {total_reservas} reservas | R$ {total_valor:.2f}</h2>'
    html_content += '<table class="w-full text-slate-200 text-sm"><thead class="bg-blue-700"><tr><th class="p-3 text-left">Data</th><th class="p-3 text-left">Cliente</th><th class="p-3 text-left">Email</th><th class="p-3 text-left">Tel</th><th class="p-3 text-left">Produto</th><th class="p-3 text-center">Qtd</th><th class="p-3 text-right">Total</th></tr></thead><tbody>'
    
    total = 0
    for idx, r in enumerate(reservas):
        subtotal = r[5] * r[6]
        total += subtotal
        bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
        html_content += f'<tr class="{bg}"><td class="p-3">{r[0][:10]}</td><td class="p-3">{r[1]}</td><td class="p-3">{r[2]}</td><td class="p-3">{r[3]}</td><td class="p-3">{r[4]}</td><td class="p-3 text-center">{r[5]}</td><td class="p-3 text-right">R$ {subtotal:.2f}</td></tr>'
    
    html_content += f'<tr class="bg-red-700 font-black text-white"><td colspan="6" class="p-3 text-right">TOTAL</td><td class="p-3 text-right">R$ {total:.2f}</td></tr></tbody></table>'
    html_content += '<button onclick="sincronizar()" class="mt-4 bg-purple-600 text-white px-6 py-2 rounded font-bold">Sincronizar Agora</button>'
    
    page = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-950 min-h-screen"><nav class="bg-blue-900 border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between"><span class="text-3xl font-black text-red-400">JG MINIS</span><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded">Sair</a></div></nav><div class="container mx-auto px-4 py-8"><div class="bg-slate-800 rounded-xl p-8 border-2 border-blue-600">{html_content_val}</div></div><script>
function sincronizar() {{
  fetch("/sincronizar-agora", {{method: "POST", headers: {{"Content-Type": "application/json"}}}})
  .then(r => r.json())
  .then(data => {{
    alert(data.success ? "OK Sincronizado!" : "ERRO: " + data.error);
    if (data.success) location.reload();
  }}).catch(e => alert("ERRO"));
}}
</script></body></html>'''
    return page.format(html_content_val=html_content)

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
        return jsonify({'success': resultado})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/pessoas')
@admin_required
def pessoas():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, name, email, phone, is_admin, created_at FROM users ORDER BY created_at DESC')
    users = c.fetchall()
    conn.close()
    
    users_html = '<table class="w-full text-slate-200 text-sm"><thead class="bg-blue-700"><tr><th class="p-3 text-left">Nome</th><th class="p-3 text-left">Email</th><th class="p-3 text-left">Telefone</th><th class="p-3 text-left">Tipo</th><th class="p-3 text-left">Cadastro</th><th class="p-3 text-center">Ações</th></tr></thead><tbody>'
    
    for idx, u in enumerate(users):
        bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
        tipo = '👑 ADMIN' if u[4] else '👤 Usuário'
        users_html += f'<tr class="{bg}"><td class="p-3">{u[1]}</td><td class="p-3">{u[2]}</td><td class="p-3">{u[3]}</td><td class="p-3">{tipo}</td><td class="p-3">{u[5][:10]}</td><td class="p-3 text-center"><a href="/editar-pessoa/{u[0]}" class="bg-blue-600 text-white px-3 py-1 rounded mr-2">Editar</a><button onclick="deletarPessoa({u[0]}, \'{u[1]}\')" class="bg-red-600 text-white px-3 py-1 rounded">Deletar</button></td></tr>'
    
    users_html += '</tbody></table>'
    
    page = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pessoas Cadastradas</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-950 min-h-screen"><nav class="bg-blue-900 border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between"><span class="text-3xl font-black text-red-400">JG MINIS</span><div class="flex gap-4"><a href="/" class="bg-blue-600 text-white px-4 py-2 rounded">Catálogo</a><a href="/admin" class="bg-purple-600 text-white px-4 py-2 rounded">Admin</a><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded">Sair</a></div></div></nav><div class="container mx-auto px-4 py-8"><h1 class="text-4xl font-black text-blue-400 mb-8">Pessoas Cadastradas</h1><div class="bg-slate-800 rounded-xl p-8 border-2 border-blue-600 overflow-x-auto">{users_html_val}</div></div><script>
function deletarPessoa(id, nome) {{
  if (confirm("Tem certeza que quer deletar " + nome + "?")) {{
    fetch("/deletar-pessoa/" + id, {{method: "POST"}})
    .then(r => r.json())
    .then(data => {{
      if (data.success) {{
        alert("OK Pessoa deletada!");
        location.reload();
      }} else {{
        alert("ERRO: " + data.error);
      }}
    }}).catch(e => alert("ERRO"));
  }}
}}
</script></body></html>'''
    return page.format(users_html_val=users_html)

@app.route('/editar-pessoa/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def editar_pessoa(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        is_admin = request.form.get('is_admin') == 'on'
        
        if not name or not validate_phone(phone):
            conn.close()
            return redirect(f'/editar-pessoa/{user_id}')
        
        c.execute('UPDATE users SET name = ?, phone = ?, is_admin = ? WHERE id = ?',
                  (name, format_phone(phone), is_admin, user_id))
        conn.commit()
        conn.close()
        return redirect('/pessoas')
    
    c.execute('SELECT id, name, email, phone, is_admin FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return redirect('/pessoas')
    
    page = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Editar Pessoa</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-950 min-h-screen"><nav class="bg-blue-900 border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between"><span class="text-3xl font-black text-red-400">JG MINIS</span><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded">Sair</a></div></nav><div class="container mx-auto px-4 py-8 max-w-md"><div class="bg-slate-800 rounded-xl p-8 border-2 border-blue-600"><h1 class="text-2xl font-black text-blue-400 mb-6">Editar: {user_name_val}</h1><form method="POST"><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Nome</label><input type="text" name="name" value="{user_name_val}" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Email (não editável)</label><input type="text" value="{user_email_val}" class="w-full border-2 border-slate-600 bg-slate-700 text-slate-400 rounded-lg px-4 py-2" disabled></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Telefone</label><input type="tel" name="phone" value="{user_phone_val}" class="w-full border-2 border-blue-600 bg-slate-900 text-white rounded-lg px-4 py-2" required></div><div class="mb-6"><label class="flex items-center gap-3"><input type="checkbox" name="is_admin" {is_admin_checked_val} class="w-5 h-5"><span class="text-slate-300 font-bold">É Administrador?</span></label></div><div class="flex gap-4"><a href="/pessoas" class="flex-1 bg-slate-700 text-white font-bold py-2 rounded-lg text-center">Cancelar</a><button type="submit" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg">Salvar</button></div></form></div></div></body></html>'''
    return page.format(user_name_val=user[1], user_email_val=user[2], user_phone_val=user[3], is_admin_checked_val="checked" if user[4] else "")

@app.route('/deletar-pessoa/<int:user_id>', methods=['POST'])
@admin_required
def deletar_pessoa(user_id):
    # Não deleta o próprio admin logado
    if request.user.get('user_id') == user_id:
        return jsonify({'success': False, 'error': 'Não pode deletar sua própria conta'}), 400
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Deleta as reservas da pessoa primeiro
    c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,))
    # Deleta da lista de espera
    c.execute('DELETE FROM waiting_list WHERE user_id = ?', (user_id,))
    # Depois deleta a pessoa
    c.execute('DELETE FROM users WHERE id = ?', (user_id,))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/entrar-lista-espera', methods=['POST'])
@login_required
def entrar_lista_espera():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    user_id = request.user.get('user_id')
    email = data.get('email')
    phone = data.get('phone')

    if not all([miniatura_id, user_id, email, phone]):
        return jsonify({'success': False, 'error': 'Dados incompletos'}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Verifica se já está na lista de espera para esta miniatura
    c.execute('SELECT id FROM waiting_list WHERE user_id = ? AND miniatura_id = ?', (user_id, miniatura_id))
    if c.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Você já está na lista de espera para esta miniatura.'}), 409

    try:
        c.execute('INSERT INTO waiting_list (user_id, miniatura_id, email, phone, created_at, notified) VALUES (?, ?, ?, ?, ?, ?)',
                  (user_id, miniatura_id, email, phone, datetime.now().isoformat(), False))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Adicionado à lista de espera com sucesso!'})
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/lista-espera')
@admin_required
def lista_espera():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT 
            wl.id, u.name, u.email, u.phone, m.name, wl.created_at, wl.notified
        FROM 
            waiting_list wl
        JOIN 
            users u ON wl.user_id = u.id
        JOIN 
            miniaturas m ON wl.miniatura_id = m.id
        ORDER BY 
            wl.created_at DESC
    ''')
    espera = c.fetchall()
    conn.close()

    espera_html = '<table class="w-full text-slate-200 text-sm"><thead class="bg-blue-700"><tr><th class="p-3 text-left">Nome</th><th class="p-3 text-left">Email</th><th class="p-3 text-left">Telefone</th><th class="p-3 text-left">Produto</th><th class="p-3 text-left">Data Cadastro</th><th class="p-3 text-center">Notificado</th><th class="p-3 text-center">Ações</th></tr></thead><tbody>'
    
    for idx, item in enumerate(espera):
        bg = 'bg-slate-700' if idx % 2 == 0 else 'bg-slate-800'
        notified_status = '✅ Sim' if item[6] else '❌ Não'
        espera_html += f'<tr class="{bg}"><td class="p-3">{item[1]}</td><td class="p-3">{item[2]}</td><td class="p-3">{item[3]}</td><td class="p-3">{item[4]}</td><td class="p-3">{item[5][:10]}</td><td class="p-3 text-center">{notified_status}</td><td class="p-3 text-center"><button onclick="deletarDaLista({item[0]}, \'{item[1]}\', \'{item[4]}\')" class="bg-red-600 text-white px-3 py-1 rounded">Deletar</button></td></tr>'
    
    espera_html += '</tbody></table>'

    page = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Lista de Espera</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-950 min-h-screen"><nav class="bg-blue-900 border-b-4 border-red-600"><div class="container mx-auto px-4 py-4 flex justify-between"><span class="text-3xl font-black text-red-400">JG MINIS</span><div class="flex gap-4"><a href="/" class="bg-blue-600 text-white px-4 py-2 rounded">Catálogo</a><a href="/admin" class="bg-purple-600 text-white px-4 py-2 rounded">Admin</a><a href="/logout" class="bg-red-700 text-white px-4 py-2 rounded">Sair</a></div></div></nav><div class="container mx-auto px-4 py-8"><h1 class="text-4xl font-black text-blue-400 mb-8">Lista de Espera</h1><div class="bg-slate-800 rounded-xl p-8 border-2 border-blue-600 overflow-x-auto">{espera_html_val}</div></div><script>
function deletarDaLista(id, nomeUsuario, nomeMiniatura) {{
  if (confirm("Tem certeza que quer remover " + nomeUsuario + " da lista de espera para " + nomeMiniatura + "?")) {{
    fetch("/deletar-lista-espera/" + id, {{method: "POST"}})
    .then(r => r.json())
    .then(data => {{
      if (data.success) {{
        alert("OK Removido da lista de espera!");
        location.reload();
      }} else {{
        alert("ERRO: " + data.error);
      }}
    }}).catch(e => alert("ERRO"));
  }}
}}
</script></body></html>'''
    return page.format(espera_html_val=espera_html)

@app.route('/deletar-lista-espera/<int:item_id>', methods=['POST'])
@admin_required
def deletar_lista_espera(item_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM waiting_list WHERE id = ?', (item_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

scheduler.add_job(atualizar_miniaturas, 'cron', hour=0, minute=0, id='sync_sheets')

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
