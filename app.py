import os
import sqlite3
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, redirect, url_for, render_template_string, flash, get_flashed_messages
import bcrypt # For password hashing
import re
import json

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sua_chave_secreta_aqui_muito_segura_e_longa') # Use a strong, unique key

DB_FILE = 'database.db'
WHATSAPP_NUMERO = os.environ.get('WHATSAPP_NUMERO', '5511999999999') # WhatsApp number for contact

# --- Database Functions ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS miniaturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_url TEXT NOT NULL,
            name TEXT NOT NULL,
            arrival_date TEXT,
            stock INTEGER DEFAULT 0,
            price REAL DEFAULT 0.0,
            observations TEXT,
            max_reservations_per_user INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            miniatura_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            reservation_date TEXT NOT NULL,
            status TEXT DEFAULT 'pending', -- pending, confirmed, cancelled
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (miniatura_id) REFERENCES miniaturas(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            miniatura_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            notification_sent INTEGER DEFAULT 0,
            request_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (miniatura_id) REFERENCES miniaturas(id)
        )
    ''')
    conn.commit()
    conn.close()
    print("OK BD inicializado")

def load_initial_data():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Add an admin user if not exists
    c.execute("SELECT * FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)",
                  ('Admin', 'admin@jgminis.com.br', '5511999999999', hashed_password, 1))
        print("OK Usuário admin adicionado.")

    # Load sample miniaturas if table is empty
    c.execute("SELECT COUNT(*) FROM miniaturas")
    if c.fetchone()[0] == 0:
        miniaturas_data = [
            ("https://i.imgur.com/2Y0Y0Y0.jpeg", "Toyota Supra VeilSide Combat V-I White", "2025-01-15", 12, 120.00, "Edição limitada", 1),
            ("https://i.imgur.com/3Z3Z3Z3.jpeg", "Acura ARX-06 GTP #93 Acura Meyer Shank Racing 2025 IMSA Daytona 24 Hrs", "2025-02-20", 12, 130.00, "Pré-venda", 1),
            ("https://i.imgur.com/4W4W4W4.jpeg", "Toyota GR86 LB Nation Advan", "2025-03-10", 12, 130.00, "Novo lançamento", 1),
            ("https://i.imgur.com/5X5X5X5.jpeg", "Chevrolet Corvette Z06 GT3.R #13 AWA 2025 IMSA Daytona 24 Hrs /Blister packagin", "2025-04-05", 12, 130.00, "Embalagem especial", 1),
            ("https://i.imgur.com/6Y6Y6Y6.jpeg", "Nissan LB Works HAKOSUKA Baby Blue", "2025-05-25", 12, 120.00, "Cor exclusiva", 1),
            ("https://i.imgur.com/7Z7Z7Z7.jpeg", "Ford Mustang Dark Horse #24 Ford Performance Racing School", "2025-06-18", 12, 130.00, "Edição de corrida", 1),
        ]
        print("Carregando dados iniciais de miniaturas...")
        for data in miniaturas_data:
            c.execute("INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)", data)
            print(f"  OK {data[1]} - R$ {data[5]:.2f} ({data[4]} em estoque)")
        conn.commit()
    conn.close()
    print(f"OK Total carregado: {len(miniaturas_data)} miniaturas")

# --- JWT Functions ---
def get_token(user_id, is_admin):
    payload = {
        'user_id': user_id,
        'is_admin': is_admin,
        'exp': datetime.utcnow() + timedelta(hours=24) # Token expires in 24 hours
    }
    return jwt.encode(payload, app.secret_key, algorithm='HS256')

def verify_token(token):
    try:
        return jwt.decode(token, app.secret_key, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        flash('Sessão expirada. Faça login novamente.', 'error')
        return None
    except jwt.InvalidTokenError:
        flash('Token inválido. Faça login novamente.', 'error')
        return None

# --- Decorators ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('token')
        if not token:
            flash('Você precisa estar logado para acessar esta página.', 'error')
            return redirect(url_for('login'))
        
        user_data = verify_token(token)
        if not user_data:
            return redirect(url_for('login')) # Redirects if token is invalid or expired
        
        # Attach user data to request object
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id, name, email, phone, is_admin FROM users WHERE id = ?', (user_data['user_id'],))
        user_db_data = c.fetchone()
        conn.close()

        if not user_db_data:
            flash('Usuário não encontrado.', 'error')
            return redirect(url_for('login'))

        request.user = {
            'user_id': user_db_data[0],
            'name': user_db_data[1],
            'email': user_db_data[2],
            'phone': user_db_data[3],
            'is_admin': bool(user_db_data[4])
        }
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # login_required already attaches request.user
        if not request.user.get('is_admin'):
            flash('Acesso negado: Você não tem permissões de administrador.', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# --- Helper Functions ---
def format_phone(phone):
    phone = re.sub(r'\D', '', phone)
    if len(phone) == 11:
        return f"({phone[:2]}) {phone[2:7]}-{phone[7:]}"
    return phone

def validate_phone(phone):
    phone = re.sub(r'\D', '', phone)
    return len(phone) == 11

# --- Authentication Routes ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        confirm = request.form['confirm_password']

        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Formato de e-mail inválido.', 'error')
            return redirect(url_for('register'))
        if password != confirm:
            flash('As senhas não coincidem.', 'error')
            return redirect(url_for('register'))
        if len(password) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'error')
            return redirect(url_for('register'))
        if not validate_phone(phone):
            flash('Telefone inválido. Use o formato (XX) XXXXX-XXXX.', 'error')
            return redirect(url_for('register'))

        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)",
                      (name, email, format_phone(phone), hashed_password))
            conn.commit()
            flash('Registro bem-sucedido! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('E-mail já registrado.', 'error')
        finally:
            conn.close()
    
    # GET request for register page
    template = '''
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
                        <input type="text" id="phone" name="phone" placeholder="(11) 99999-9999" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
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
    '''
    return render_template_string(template)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id, password, is_admin FROM users WHERE email = ?', (email,))
        user_data = c.fetchone()
        conn.close()

        if user_data and bcrypt.checkpw(password.encode('utf-8'), user_data[1].encode('utf-8')):
            user_id = user_data[0]
            is_admin = bool(user_data[2])
            token = get_token(user_id, is_admin)
            
            response = redirect(url_for('index'))
            response.set_cookie('token', token, httponly=True, max_age=86400) # 24 hours
            flash('Login bem-sucedido!', 'success')
            return response
        else:
            flash('E-mail ou senha inválidos.', 'error')
    
    # GET request for login page
    template = '''
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
                    <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Entrar</button>
                </form>
                <p class="text-center text-slate-400 mt-6">Não tem uma conta? <a href="/register" class="text-blue-400 hover:underline">Registre-se</a></p>
            </div>
        </body>
        </html>
    '''
    return render_template_string(template)

@app.route('/logout')
def logout():
    response = redirect(url_for('login'))
    response.delete_cookie('token')
    flash('Você foi desconectado.', 'info')
    return response

# --- Main Routes ---
@app.route('/')
@login_required
def index():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas')
    miniaturas = c.fetchall()
    
    user_id = request.user.get('user_id')
    user_email = request.user.get('email')
    user_phone = request.user.get('phone')
    
    conn.close()
    
    items_html = ""
    for m in miniaturas:
        is_esgotado = m[4] <= 0
        status = "ESGOTADO" if is_esgotado else f"Em Estoque: {m[4]}"
        status_color = "red" if is_esgotado else "green"
        
        nome_json = json.dumps(m[2]) # Ensure string is properly quoted for JS
        
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
        admin_links = f'''
            <a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Admin</a>
            <a href="/pessoas" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-lg font-semibold">Pessoas</a>
            <a href="/lista-espera" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold">Lista de Espera</a>
        '''
    
    template = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JG MINIS</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span><div class="flex gap-4"><a href="/minhas-reservas" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Minhas Reservas</a>{admin_links_jinja}<a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12"><h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">Catalogo de Miniaturas</h1><p class="text-slate-300 mb-8">Pre vendas</p><div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">{items_html_jinja}</div></div><div id="confirmModal" class="hidden fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50"><div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl max-w-md w-full p-8"><h2 class="text-2xl font-black text-blue-400 mb-4">Confirmar Reserva</h2><div id="confirmContent" class="text-slate-300 mb-6 space-y-3"></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Quantidade:</label><div class="flex gap-2"><button type="button" onclick="decrementarQtd()" class="bg-red-600 text-white font-bold w-12 h-12 rounded-lg">-</button><input type="number" id="quantidadeInput" value="1" min="1" class="flex-1 bg-slate-700 text-white font-bold text-center rounded-lg border-2 border-blue-600"><button type="button" onclick="incrementarQtd()" class="bg-green-600 text-white font-bold w-12 h-12 rounded-lg">+</button></div></div><div class="flex gap-4"><button onclick="fecharModal()" class="flex-1 bg-slate-700 text-white font-bold py-2 rounded-lg">Cancelar</button><button onclick="confirmarReserva()" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg">Confirmar</button></div></div></div><script>
let reservaAtual = null;
let maxQtd = 1;
let userId = {user_id_jinja};
let userEmail = "{user_email_jinja}";
let userPhone = "{user_phone_jinja}";

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
    
    return render_template_string(template, 
                                  items_html_jinja=items_html, 
                                  admin_links_jinja=admin_links,
                                  user_id_jinja=user_id,
                                  user_email_jinja=user_email,
                                  user_phone_jinja=user_phone)

@app.route('/reservar', methods=['POST'])
@login_required
def reservar():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    quantidade = data.get('quantidade')
    user_id = request.user.get('user_id')

    if not miniatura_id or not quantidade or quantidade <= 0:
        return jsonify({'success': False, 'error': 'Dados inválidos.'}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('SELECT stock, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
        miniatura = c.fetchone()

        if not miniatura:
            return jsonify({'success': False, 'error': 'Miniatura não encontrada.'}), 404

        current_stock = miniatura[0]
        max_reservations_per_user = miniatura[1]

        if quantidade > current_stock:
            return jsonify({'success': False, 'error': f'Quantidade solicitada ({quantidade}) excede o estoque disponível ({current_stock}).'}), 400
        
        # Check existing reservations for this user and miniatura
        c.execute('SELECT SUM(quantity) FROM reservations WHERE user_id = ? AND miniatura_id = ? AND status = "pending"', (user_id, miniatura_id))
        existing_reservations_sum = c.fetchone()[0] or 0

        if (existing_reservations_sum + quantidade) > max_reservations_per_user:
            return jsonify({'success': False, 'error': f'Você já tem {existing_reservations_sum} reservas para esta miniatura. O máximo permitido é {max_reservations_per_user}.'}), 400

        # Make reservation
        c.execute('UPDATE miniaturas SET stock = stock - ? WHERE id = ?', (quantidade, miniatura_id))
        c.execute('INSERT INTO reservations (user_id, miniatura_id, quantity, reservation_date, status) VALUES (?, ?, ?, ?, ?)',
                  (user_id, miniatura_id, quantidade, datetime.now().isoformat(), 'pending'))
        conn.commit()
        return jsonify({'success': True, 'message': 'Reserva realizada com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    user_id = request.user.get('user_id')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT
            r.id, m.name, m.image_url, r.quantity, r.reservation_date, r.status, m.price
        FROM reservations r
        JOIN miniaturas m ON r.miniatura_id = m.id
        WHERE r.user_id = ?
        ORDER BY r.reservation_date DESC
    ''', (user_id,))
    reservas = c.fetchall()
    conn.close()

    reservas_html = ""
    if not reservas:
        reservas_html = '<p class="text-slate-400 text-center text-lg">Você ainda não fez nenhuma reserva.</p>'
    else:
        for r in reservas:
            total_price = r[3] * r[6] # quantity * price
            status_color = {
                'pending': 'bg-yellow-600',
                'confirmed': 'bg-green-600',
                'cancelled': 'bg-red-600'
            }.get(r[5], 'bg-gray-600')

            reservas_html += f'''
                <div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden flex flex-col md:flex-row items-center p-4 gap-4">
                    <img src="{r[2]}" class="w-32 h-32 object-cover rounded-lg" alt="{r[1]}">
                    <div class="flex-grow">
                        <h3 class="font-bold text-blue-300 text-xl mb-1">{r[1]}</h3>
                        <p class="text-sm text-slate-400">Quantidade: {r[3]}</p>
                        <p class="text-sm text-slate-400">Preço Unitário: R$ {r[6]:.2f}</p>
                        <p class="text-sm text-slate-400">Total: R$ {total_price:.2f}</p>
                        <p class="text-sm text-slate-400">Data da Reserva: {r[4].split('T')[0]}</p>
                        <span class="{status_color} text-white px-3 py-1 rounded-full text-sm font-bold mt-2 inline-block">{r[5].capitalize()}</span>
                    </div>
                    <div class="flex flex-col gap-2">
                        <button onclick="cancelarReserva({r[0]})" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Cancelar</button>
                    </div>
                </div>
            '''
    
    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Minhas Reservas - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen">
            <nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
                <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                    <span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span>
                    <div class="flex gap-4">
                        <a href="/" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Catálogo</a>
                        <a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a>
                    </div>
                </div>
            </nav>
            <div class="container mx-auto px-4 py-12">
                <h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-8">Minhas Reservas</h1>
                <div class="grid grid-cols-1 gap-6">
                    {reservas_html_jinja}
                </div>
            </div>
            <script>
                function cancelarReserva(reservaId) {{
                    if (confirm("Tem certeza que deseja cancelar esta reserva?")) {{
                        fetch("/cancelar-reserva", {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify({{reserva_id: reservaId}})
                        }}).then(r => r.json()).then(data => {{
                            if (data.success) {{
                                alert("Reserva cancelada com sucesso!");
                                location.reload();
                            }} else {{
                                alert("ERRO: " + data.error);
                            }}
                        }}).catch(e => {{
                            alert("ERRO na requisição: " + e);
                        }});
                    }}
                }}
            </script>
        </body>
        </html>
    '''
    return render_template_string(template, reservas_html_jinja=reservas_html)

@app.route('/cancelar-reserva', methods=['POST'])
@login_required
def cancelar_reserva():
    data = request.get_json()
    reserva_id = data.get('reserva_id')
    user_id = request.user.get('user_id')

    if not reserva_id:
        return jsonify({'success': False, 'error': 'ID da reserva inválido.'}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('SELECT miniatura_id, quantity, user_id, status FROM reservations WHERE id = ?', (reserva_id,))
        reserva = c.fetchone()

        if not reserva:
            return jsonify({'success': False, 'error': 'Reserva não encontrada.'}), 404
        
        if reserva[2] != user_id and not request.user.get('is_admin'):
            return jsonify({'success': False, 'error': 'Você não tem permissão para cancelar esta reserva.'}), 403

        if reserva[3] == 'cancelled':
            return jsonify({'success': False, 'error': 'Esta reserva já foi cancelada.'}), 400

        miniatura_id = reserva[0]
        quantity = reserva[1]

        c.execute('UPDATE miniaturas SET stock = stock + ? WHERE id = ?', (quantity, miniatura_id))
        c.execute('UPDATE reservations SET status = "cancelled" WHERE id = ?', (reserva_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Reserva cancelada com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- Admin Routes ---
@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, name, stock, price FROM miniaturas')
    miniaturas = c.fetchall()
    conn.close()

    miniaturas_html = ""
    for m in miniaturas:
        miniaturas_html += f'''
            <tr class="border-b border-slate-700 hover:bg-slate-700">
                <td class="px-4 py-3">{m[0]}</td>
                <td class="px-4 py-3">{m[1]}</td>
                <td class="px-4 py-3">{m[2]}</td>
                <td class="px-4 py-3">R$ {m[3]:.2f}</td>
                <td class="px-4 py-3 flex gap-2">
                    <a href="/admin/edit-miniatura/{m[0]}" class="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded-lg text-sm">Editar</a>
                    <button onclick="deleteMiniatura({m[0]})" class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-sm">Excluir</button>
                </td>
            </tr>
        '''

    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Admin - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen text-slate-200">
            <nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
                <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                    <span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS Admin</span>
                    <div class="flex gap-4">
                        <a href="/" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Catálogo</a>
                        <a href="/pessoas" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-lg font-semibold">Pessoas</a>
                        <a href="/lista-espera" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold">Lista de Espera</a>
                        <a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a>
                    </div>
                </div>
            </nav>
            <div class="container mx-auto px-4 py-12">
                <h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-8">Painel Administrativo</h1>
                
                <div class="mb-8 flex gap-4">
                    <a href="/admin/add-miniatura" class="bg-green-600 hover:bg-green-700 text-white px-5 py-2 rounded-lg font-semibold">Adicionar Miniatura</a>
                </div>

                <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-blue-600 shadow-2xl overflow-hidden">
                    <table class="min-w-full text-left text-slate-300">
                        <thead class="bg-slate-700 border-b border-slate-600">
                            <tr>
                                <th class="px-4 py-3">ID</th>
                                <th class="px-4 py-3">Nome</th>
                                <th class="px-4 py-3">Estoque</th>
                                <th class="px-4 py-3">Preço</th>
                                <th class="px-4 py-3">Ações</th>
                            </tr>
                        </thead>
                        <tbody>
                            {miniaturas_html_jinja}
                        </tbody>
                    </table>
                </div>
            </div>
            <script>
                function deleteMiniatura(id) {{
                    if (confirm("Tem certeza que deseja excluir esta miniatura?")) {{
                        fetch(`/admin/delete-miniatura/${{id}}`, {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}}
                        }}).then(r => r.json()).then(data => {{
                            if (data.success) {{
                                alert("Miniatura excluída com sucesso!");
                                location.reload();
                            }} else {{
                                alert("ERRO: " + data.error);
                            }}
                        }}).catch(e => {{
                            alert("ERRO na requisição: " + e);
                        }});
                    }}
                }}
            </script>
        </body>
        </html>
    '''
    return render_template_string(template, miniaturas_html_jinja=miniaturas_html)

@app.route('/admin/add-miniatura', methods=['GET', 'POST'])
@login_required
@admin_required
def add_miniatura():
    if request.method == 'POST':
        image_url = request.form['image_url']
        name = request.form['name']
        arrival_date = request.form['arrival_date']
        stock = int(request.form['stock'])
        price = float(request.form['price'])
        observations = request.form['observations']
        max_reservations_per_user = int(request.form['max_reservations_per_user'])

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user))
            conn.commit()
            flash('Miniatura adicionada com sucesso!', 'success')
            return redirect(url_for('admin_panel'))
        except Exception as e:
            flash(f'Erro ao adicionar miniatura: {e}', 'error')
        finally:
            conn.close()
    
    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Adicionar Miniatura - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center text-slate-200">
            <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-lg w-full">
                <h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Adicionar Nova Miniatura</h2>
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
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="image_url" class="block text-slate-300 font-bold mb-1">URL da Imagem:</label>
                        <input type="url" id="image_url" name="image_url" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="arrival_date" class="block text-slate-300 font-bold mb-1">Previsão de Chegada:</label>
                        <input type="date" id="arrival_date" name="arrival_date" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="stock" class="block text-slate-300 font-bold mb-1">Estoque:</label>
                        <input type="number" id="stock" name="stock" required min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="price" class="block text-slate-300 font-bold mb-1">Preço:</label>
                        <input type="number" id="price" name="price" required step="0.01" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="observations" class="block text-slate-300 font-bold mb-1">Observações:</label>
                        <textarea id="observations" name="observations" rows="3" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></textarea>
                    </div>
                    <div>
                        <label for="max_reservations_per_user" class="block text-slate-300 font-bold mb-1">Máx. Reservas por Usuário:</label>
                        <input type="number" id="max_reservations_per_user" name="max_reservations_per_user" required min="1" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div class="flex gap-4">
                        <a href="/admin" class="flex-1 text-center bg-slate-700 text-white font-bold py-2 rounded-lg hover:bg-slate-600 transition duration-300">Cancelar</a>
                        <button type="submit" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Adicionar</button>
                    </div>
                </form>
            </div>
        </body>
        </html>
    '''
    return render_template_string(template)

@app.route('/admin/edit-miniatura/<int:miniatura_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_miniatura(miniatura_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        image_url = request.form['image_url']
        name = request.form['name']
        arrival_date = request.form['arrival_date']
        stock = int(request.form['stock'])
        price = float(request.form['price'])
        observations = request.form['observations']
        max_reservations_per_user = int(request.form['max_reservations_per_user'])

        try:
            c.execute("UPDATE miniaturas SET image_url=?, name=?, arrival_date=?, stock=?, price=?, observations=?, max_reservations_per_user=? WHERE id=?",
                      (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user, miniatura_id))
            conn.commit()
            flash('Miniatura atualizada com sucesso!', 'success')
            return redirect(url_for('admin_panel'))
        except Exception as e:
            flash(f'Erro ao atualizar miniatura: {e}', 'error')
        finally:
            conn.close()
    
    # GET request
    c.execute('SELECT image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
    miniatura = c.fetchone()
    conn.close()

    if not miniatura:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('admin_panel'))

    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Editar Miniatura - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center text-slate-200">
            <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-lg w-full">
                <h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Editar Miniatura</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <form method="POST" action="/admin/edit-miniatura/{miniatura_id_jinja}" class="space-y-4">
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" value="{miniatura_name_jinja}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="image_url" class="block text-slate-300 font-bold mb-1">URL da Imagem:</label>
                        <input type="url" id="image_url" name="image_url" value="{miniatura_image_url_jinja}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="arrival_date" class="block text-slate-300 font-bold mb-1">Previsão de Chegada:</label>
                        <input type="date" id="arrival_date" name="arrival_date" value="{miniatura_arrival_date_jinja}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="stock" class="block text-slate-300 font-bold mb-1">Estoque:</label>
                        <input type="number" id="stock" name="stock" value="{miniatura_stock_jinja}" required min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="price" class="block text-slate-300 font-bold mb-1">Preço:</label>
                        <input type="number" id="price" name="price" value="{miniatura_price_jinja:.2f}" required step="0.01" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="observations" class="block text-slate-300 font-bold mb-1">Observações:</label>
                        <textarea id="observations" name="observations" rows="3" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">{miniatura_observations_jinja}</textarea>
                    </div>
                    <div>
                        <label for="max_reservations_per_user" class="block text-slate-300 font-bold mb-1">Máx. Reservas por Usuário:</label>
                        <input type="number" id="max_reservations_per_user" name="max_reservations_per_user" value="{miniatura_max_reservations_per_user_jinja}" required min="1" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div class="flex gap-4">
                        <a href="/admin" class="flex-1 text-center bg-slate-700 text-white font-bold py-2 rounded-lg hover:bg-slate-600 transition duration-300">Cancelar</a>
                        <button type="submit" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Salvar Alterações</button>
                    </div>
                </form>
            </div>
        </body>
        </html>
    '''
    return render_template_string(template,
                                  miniatura_id_jinja=miniatura_id,
                                  miniatura_image_url_jinja=miniatura[0],
                                  miniatura_name_jinja=miniatura[1],
                                  miniatura_arrival_date_jinja=miniatura[2],
                                  miniatura_stock_jinja=miniatura[3],
                                  miniatura_price_jinja=miniatura[4],
                                  miniatura_observations_jinja=miniatura[5],
                                  miniatura_max_reservations_per_user_jinja=miniatura[6])

@app.route('/admin/delete-miniatura/<int:miniatura_id>', methods=['POST'])
@login_required
@admin_required
def delete_miniatura(miniatura_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        # Delete related reservations and waitlist entries first
        c.execute('DELETE FROM reservations WHERE miniatura_id = ?', (miniatura_id,))
        c.execute('DELETE FROM waitlist WHERE miniatura_id = ?', (miniatura_id,))
        c.execute('DELETE FROM miniaturas WHERE id = ?', (miniatura_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Miniatura excluída com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/pessoas')
@login_required
@admin_required
def pessoas():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, name, email, phone, is_admin FROM users')
    users = c.fetchall()
    conn.close()

    users_html = ""
    for u in users:
        admin_status = "Sim" if u[4] else "Não"
        users_html += f'''
            <tr class="border-b border-slate-700 hover:bg-slate-700">
                <td class="px-4 py-3">{u[0]}</td>
                <td class="px-4 py-3">{u[1]}</td>
                <td class="px-4 py-3">{u[2]}</td>
                <td class="px-4 py-3">{u[3]}</td>
                <td class="px-4 py-3">{admin_status}</td>
                <td class="px-4 py-3 flex gap-2">
                    <a href="/admin/edit-user/{u[0]}" class="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded-lg text-sm">Editar</a>
                    <button onclick="deleteUser({u[0]})" class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-sm">Excluir</button>
                </td>
            </tr>
        '''

    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Pessoas - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen text-slate-200">
            <nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
                <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                    <span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS Admin</span>
                    <div class="flex gap-4">
                        <a href="/" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Catálogo</a>
                        <a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Admin</a>
                        <a href="/lista-espera" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold">Lista de Espera</a>
                        <a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a>
                    </div>
                </div>
            </nav>
            <div class="container mx-auto px-4 py-12">
                <h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-8">Gerenciar Pessoas</h1>
                
                <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-blue-600 shadow-2xl overflow-hidden">
                    <table class="min-w-full text-left text-slate-300">
                        <thead class="bg-slate-700 border-b border-slate-600">
                            <tr>
                                <th class="px-4 py-3">ID</th>
                                <th class="px-4 py-3">Nome</th>
                                <th class="px-4 py-3">E-mail</th>
                                <th class="px-4 py-3">Telefone</th>
                                <th class="px-4 py-3">Admin</th>
                                <th class="px-4 py-3">Ações</th>
                            </tr>
                        </thead>
                        <tbody>
                            {users_html_jinja}
                        </tbody>
                    </table>
                </div>
            </div>
            <script>
                function deleteUser(id) {{
                    if (confirm("Tem certeza que deseja excluir este usuário?")) {{
                        fetch(`/admin/delete-user/${{id}}`, {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}}
                        }}).then(r => r.json()).then(data => {{
                            if (data.success) {{
                                alert("Usuário excluído com sucesso!");
                                location.reload();
                            }} else {{
                                alert("ERRO: " + data.error);
                            }}
                        }}).catch(e => {{
                            alert("ERRO na requisição: " + e);
                        }});
                    }}
                }}
            </script>
        </body>
        </html>
    '''
    return render_template_string(template, users_html_jinja=users_html)

@app.route('/admin/edit-user/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        is_admin = 1 if 'is_admin' in request.form else 0

        try:
            c.execute("UPDATE users SET name=?, email=?, phone=?, is_admin=? WHERE id=?",
                      (name, email, format_phone(phone), is_admin, user_id))
            conn.commit()
            flash('Usuário atualizado com sucesso!', 'success')
            return redirect(url_for('pessoas'))
        except Exception as e:
            flash(f'Erro ao atualizar usuário: {e}', 'error')
        finally:
            conn.close()
    
    # GET request
    c.execute('SELECT name, email, phone, is_admin FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()

    if not user:
        flash('Usuário não encontrado.', 'error')
        return redirect(url_for('pessoas'))

    is_admin_checked = "checked" if user[3] else ""

    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Editar Usuário - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center text-slate-200">
            <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-lg w-full">
                <h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Editar Usuário</h2>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        <ul class="mb-4">
                            {% for category, message in messages %}
                                <li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>
                            {% endfor %}
                        </ul>
                    {% endif %}
                {% endwith %}
                <form method="POST" action="/admin/edit-user/{user_id_jinja}" class="space-y-4">
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" value="{user_name_jinja}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label>
                        <input type="email" id="email" name="email" value="{user_email_jinja}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="phone" class="block text-slate-300 font-bold mb-1">Telefone:</label>
                        <input type="text" id="phone" name="phone" value="{user_phone_jinja}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div class="flex items-center">
                        <input type="checkbox" id="is_admin" name="is_admin" {is_admin_checked_jinja} class="h-5 w-5 text-blue-600 rounded border-gray-300 focus:ring-blue-500">
                        <label for="is_admin" class="ml-2 block text-slate-300 font-bold">É Administrador</label>
                    </div>
                    <div class="flex gap-4">
                        <a href="/pessoas" class="flex-1 text-center bg-slate-700 text-white font-bold py-2 rounded-lg hover:bg-slate-600 transition duration-300">Cancelar</a>
                        <button type="submit" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Salvar Alterações</button>
                    </div>
                </form>
            </div>
        </body>
        </html>
    '''
    return render_template_string(template,
                                  user_id_jinja=user_id,
                                  user_name_jinja=user[0],
                                  user_email_jinja=user[1],
                                  user_phone_jinja=user[2],
                                  is_admin_checked_jinja=is_admin_checked)

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    # Cannot delete self
    if request.user.get('user_id') == user_id:
        return jsonify({'success': False, 'error': 'Você não pode deletar sua própria conta.'}), 400
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        # Delete related reservations and waitlist entries first
        c.execute('DELETE FROM reservations WHERE user_id = ?', (user_id,))
        c.execute('DELETE FROM waitlist WHERE user_id = ?', (user_id,))
        c.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Usuário excluído com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/lista-espera')
@login_required
@admin_required
def lista_espera():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT
            wl.id, u.name, u.email, m.name, wl.request_date, wl.notification_sent
        FROM waitlist wl
        JOIN users u ON wl.user_id = u.id
        JOIN miniaturas m ON wl.miniatura_id = m.id
        ORDER BY wl.request_date DESC
    ''')
    waitlist_entries = c.fetchall()
    conn.close()

    waitlist_html = ""
    for entry in waitlist_entries:
        notification_status = "Enviada" if entry[5] else "Pendente"
        status_color = "green" if entry[5] else "yellow"
        waitlist_html += f'''
            <tr class="border-b border-slate-700 hover:bg-slate-700">
                <td class="px-4 py-3">{entry[0]}</td>
                <td class="px-4 py-3">{entry[1]}</td>
                <td class="px-4 py-3">{entry[2]}</td>
                <td class="px-4 py-3">{entry[3]}</td>
                <td class="px-4 py-3">{entry[4].split('T')[0]}</td>
                <td class="px-4 py-3"><span class="bg-{status_color}-600 text-white px-3 py-1 rounded-full text-sm">{notification_status}</span></td>
                <td class="px-4 py-3 flex gap-2">
                    <button onclick="markNotified({entry[0]})" class="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded-lg text-sm">Marcar Notificado</button>
                    <button onclick="deleteWaitlistEntry({entry[0]})" class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-sm">Excluir</button>
                </td>
            </tr>
        '''

    template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Lista de Espera - JG MINIS</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen text-slate-200">
            <nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50">
                <div class="container mx-auto px-4 py-4 flex justify-between items-center">
                    <span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS Admin</span>
                    <div class="flex gap-4">
                        <a href="/" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Catálogo</a>
                        <a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Admin</a>
                        <a href="/pessoas" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-lg font-semibold">Pessoas</a>
                        <a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a>
                    </div>
                </div>
            </nav>
            <div class="container mx-auto px-4 py-12">
                <h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-8">Lista de Espera</h1>
                
                <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-blue-600 shadow-2xl overflow-hidden">
                    <table class="min-w-full text-left text-slate-300">
                        <thead class="bg-slate-700 border-b border-slate-600">
                            <tr>
                                <th class="px-4 py-3">ID</th>
                                <th class="px-4 py-3">Usuário</th>
                                <th class="px-4 py-3">E-mail</th>
                                <th class="px-4 py-3">Miniatura</th>
                                <th class="px-4 py-3">Data Pedido</th>
                                <th class="px-4 py-3">Notificação</th>
                                <th class="px-4 py-3">Ações</th>
                            </tr>
                        </thead>
                        <tbody>
                            {waitlist_html_jinja}
                        </tbody>
                    </table>
                </div>
            </div>
            <script>
                function markNotified(id) {{
                    if (confirm("Marcar este item como 'notificação enviada'?")) {{
                        fetch(`/admin/mark-notified/${{id}}`, {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}}
                        }}).then(r => r.json()).then(data => {{
                            if (data.success) {{
                                alert("Status atualizado!");
                                location.reload();
                            }} else {{
                                alert("ERRO: " + data.error);
                            }}
                        }}).catch(e => {{
                            alert("ERRO na requisição: " + e);
                        }});
                    }}
                }}

                function deleteWaitlistEntry(id) {{
                    if (confirm("Tem certeza que deseja excluir este item da lista de espera?")) {{
                        fetch(`/admin/delete-waitlist/${{id}}`, {{
                            method: "POST",
                            headers: {{"Content-Type": "application/json"}}
                        }}).then(r => r.json()).then(data => {{
                            if (data.success) {{
                                alert("Item excluído com sucesso!");
                                location.reload();
                            }} else {{
                                alert("ERRO: " + data.error);
                            }}
                        }}).catch(e => {{
                            alert("ERRO na requisição: " + e);
                        }});
                    }}
                }}
            </script>
        </body>
        </html>
    '''
    return render_template_string(template, waitlist_html_jinja=waitlist_html)

@app.route('/admin/mark-notified/<int:entry_id>', methods=['POST'])
@login_required
@admin_required
def mark_notified(entry_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('UPDATE waitlist SET notification_sent = 1 WHERE id = ?', (entry_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Status de notificação atualizado!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/admin/delete-waitlist/<int:entry_id>', methods=['POST'])
@login_required
@admin_required
def delete_waitlist(entry_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM waitlist WHERE id = ?', (entry_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Item da lista de espera excluído com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/add-to-waitlist', methods=['POST'])
@login_required
def add_to_waitlist():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    user_id = request.user.get('user_id')
    user_email = request.user.get('email')

    if not miniatura_id:
        return jsonify({'success': False, 'error': 'ID da miniatura inválido.'}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('SELECT id FROM waitlist WHERE user_id = ? AND miniatura_id = ?', (user_id, miniatura_id))
        if c.fetchone():
            return jsonify({'success': False, 'error': 'Você já está na lista de espera para esta miniatura.'}), 400

        c.execute('INSERT INTO waitlist (user_id, miniatura_id, email, request_date) VALUES (?, ?, ?, ?)',
                  (user_id, miniatura_id, user_email, datetime.now().isoformat()))
        conn.commit()
        return jsonify({'success': True, 'message': 'Adicionado à lista de espera com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

# --- Initialization ---
init_db()
load_initial_data()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
