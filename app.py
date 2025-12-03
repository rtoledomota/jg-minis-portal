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
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sua_chave_secreta_aqui')
DB_FILE = 'database.db'
WHATSAPP_NUMERO = os.environ.get('WHATSAPP_NUMERO', '5511999999999')
GOOGLE_SHEETS_ID = '1sxlvo6j-UTB0xXuyivzWnhRuYvpJFcH2smL4ZzHTUps'
GOOGLE_SHEETS_SHEET = 'Miniaturas'

# Configurações de Email
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', 'seu_email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', 'sua_senha_app')
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))

def enviar_email(destinatario, assunto, corpo_html):
    try:
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
        
        print(f"Email enviado para {destinatario}")
        return True
    except Exception as e:
        print(f"Erro ao enviar email: {e}")
        return False

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT, password TEXT NOT NULL, is_admin INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS miniaturas (id INTEGER PRIMARY KEY AUTOINCREMENT, image_url TEXT NOT NULL, name TEXT NOT NULL, arrival_date TEXT, stock INTEGER DEFAULT 0, price REAL DEFAULT 0.0, observations TEXT, max_reservations_per_user INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reservations (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, miniatura_id INTEGER NOT NULL, quantity INTEGER NOT NULL, reservation_date TEXT NOT NULL, status TEXT DEFAULT 'confirmed', confirmacao_enviada INTEGER DEFAULT 0, FOREIGN KEY (user_id) REFERENCES users(id), FOREIGN KEY (miniatura_id) REFERENCES miniaturas(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS waitlist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, miniatura_id INTEGER NOT NULL, email TEXT NOT NULL, notification_sent INTEGER DEFAULT 0, request_date TEXT NOT NULL, FOREIGN KEY (user_id) REFERENCES users(id), FOREIGN KEY (miniatura_id) REFERENCES miniaturas(id))''')
    conn.commit()
    conn.close()
    print("OK BD inicializado")

def load_initial_data():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email = 'admin@jgminis.com.br'")
    if not c.fetchone():
        hashed_password = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)", ('Admin', 'admin@jgminis.com.br', '5511999999999', hashed_password, 1))
        print("OK Usuário admin adicionado.")
    c.execute("SELECT * FROM users WHERE email = 'usuario@example.com'")
    if not c.fetchone():
        hashed_password = bcrypt.hashpw('usuario123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)", ('Usuário Teste', 'usuario@example.com', '5511988888888', hashed_password, 0))
        print("OK Usuário teste adicionado.")
    conn.commit()
    conn.close()

def get_google_sheets():
    try:
        credentials_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS', '{}')
        credentials_dict = json.loads(credentials_json)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
        gc = gspread.authorize(credentials)
        return gc
    except Exception as e:
        print(f"Erro ao conectar ao Google Sheets: {e}")
        return None

def load_miniaturas_from_sheets():
    gc = get_google_sheets()
    if not gc:
        print("Não foi possível conectar ao Google Sheets")
        return
    try:
        sheet = gc.open_by_key(GOOGLE_SHEETS_ID).worksheet(GOOGLE_SHEETS_SHEET)
        rows = sheet.get_all_values()
        if not rows:
            print("Planilha vazia")
            return
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM miniaturas")
        for row in rows[1:]:
            if len(row) >= 8:
                try:
                    image_url = row[0]
                    name = row[1]
                    arrival_date = row[3]
                    stock = int(row[4]) if row[4] else 0
                    price = float(row[5]) if row[5] else 0.0
                    observations = row[6]
                    max_reservations = int(row[7]) if row[7] else 1
                    c.execute("INSERT INTO miniaturas (image_url, name, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?)", (image_url, name, arrival_date, stock, price, observations, max_reservations))
                except Exception as e:
                    print(f"Erro ao inserir linha: {e}")
                    continue
        conn.commit()
        conn.close()
        print(f"OK {len(rows)-1} miniaturas carregadas do Google Sheets")
    except Exception as e:
        print(f"Erro ao carregar planilha: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id, name, email, phone, is_admin FROM users WHERE id = ?', (session['user_id'],))
        user = c.fetchone()
        conn.close()
        if user:
            request.user = {'user_id': user[0], 'name': user[1], 'email': user[2], 'phone': user[3], 'is_admin': bool(user[4])}
        else:
            session.pop('user_id', None)
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not request.user.get('is_admin'):
            flash('Acesso negado: Você não tem permissões de administrador.', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/register', methods=['GET', 'POST'])
def register():
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
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)", (name, email, phone, hashed_password))
            conn.commit()
            flash('Registro bem-sucedido! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('E-mail já registrado.', 'error')
        finally:
            conn.close()
    return render_template_string('''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Registrar - JG MINIS</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center"><div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-md w-full"><h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Registrar</h2>{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}<ul class="mb-4">{% for category, message in messages %}<li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>{% endfor %}</ul>{% endif %}{% endwith %}<form method="POST" action="/register" class="space-y-4"><div><label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label><input type="text" id="name" name="name" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><div><label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label><input type="email" id="email" name="email" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><div><label for="phone" class="block text-slate-300 font-bold mb-1">Telefone:</label><input type="text" id="phone" name="phone" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><div><label for="password" class="block text-slate-300 font-bold mb-1">Senha:</label><input type="password" id="password" name="password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><div><label for="confirm_password" class="block text-slate-300 font-bold mb-1">Confirmar Senha:</label><input type="password" id="confirm_password" name="confirm_password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Registrar</button></form><p class="text-center text-slate-400 mt-6">Já tem uma conta? <a href="/login" class="text-blue-400 hover:underline">Faça Login</a></p></div></body></html>''')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = sqlite3.connect(DB_FILE)
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
    return render_template_string('''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Login - JG MINIS</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen flex items-center justify-center"><div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl p-8 max-w-md w-full"><h2 class="text-3xl font-black text-blue-400 mb-6 text-center">Login</h2>{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}<ul class="mb-4">{% for category, message in messages %}<li class="text-{{ 'red' if category == 'error' else 'green' }}-400 text-center">{{ message }}</li>{% endfor %}</ul>{% endif %}{% endwith %}<form method="POST" action="/login" class="space-y-4"><div><label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label><input type="email" id="email" name="email" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><div><label for="password" class="block text-slate-300 font-bold mb-1">Senha:</label><input type="password" id="password" name="password" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500"></div><button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Entrar</button></form><p class="text-center text-slate-400 mt-6">Não tem uma conta? <a href="/register" class="text-blue-400 hover:underline">Registre-se</a></p></div></body></html>''')

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas')
    miniaturas = c.fetchall()
    conn.close()
    
    items_html = ""
    for m in miniaturas:
        is_esgotado = m[4] <= 0
        status = "ESGOTADO" if is_esgotado else f"Em Estoque: {m[4]}"
        status_color = "red" if is_esgotado else "green"
        
        if is_esgotado:
            whatsapp_link = f"https://wa.me/{WHATSAPP_NUMERO}?text=Olá%20JG%20MINIS,%20gostaria%20de%20informações%20sobre%20a%20miniatura:%20{m[2]}"
            button_html = f'<div class="flex gap-2"><a href="{whatsapp_link}" target="_blank" class="flex-1 bg-orange-600 hover:bg-orange-700 text-white font-bold px-4 py-2 rounded-lg text-center">Contato</a><button onclick="adicionarListaEspera({m[0]}, \'{m[2]}\')" class="flex-1 bg-purple-600 hover:bg-purple-700 text-white font-bold px-4 py-2 rounded-lg">Lista Espera</button></div>'
        else:
            button_html = f'<button onclick="abrirModal({m[0]}, \'{m[2]}\', {m[5]}, {m[4]}, {m[7]})" class="w-full bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold px-4 py-2 rounded-lg">Reservar</button>'
        
        items_html += f'''<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden">
<div class="bg-black h-48 flex items-center justify-center relative overflow-hidden">
<img src="{m[1]}" class="w-full h-full object-cover" alt="{m[2]}" onerror="this.style.background='linear-gradient(135deg, #1e40af 0%, #7c3aed 100%)'">
<div class="absolute top-3 right-3 bg-{status_color}-600 text-white px-3 py-1 rounded-full text-sm font-bold">{status}</div>
</div>
<div class="p-4">
<h3 class="font-bold text-blue-300 mb-2 text-lg">{m[2]}</h3>
<p class="text-sm text-slate-400 mb-2">Chegada: {m[3]}</p>
<p class="text-sm text-slate-400 mb-3">{m[6]}</p>
<div class="flex justify-between items-center gap-2 mb-3">
<span class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">R$ {m[5]:.2f}</span>
</div>
{button_html}
</div>
</div>'''
    
    admin_links = ''
    if request.user.get('is_admin'):
        admin_links = '<a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Admin</a><a href="/usuarios" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-lg font-semibold">Usuários</a><a href="/lista-espera" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold">Lista Espera</a><a href="/relatorio-reservas" class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-lg font-semibold">📊 Relatório</a>'
    
    return render_template_string('''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>JG MINIS</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span><div class="flex gap-4"><a href="/perfil" class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-lg font-semibold">Perfil</a><a href="/minhas-reservas" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Minhas Reservas</a>''' + admin_links + '''<a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12"><h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">Catálogo de Miniaturas</h1><p class="text-slate-300 mb-8">Pré-vendas</p><div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">''' + items_html + '''</div></div><div id="confirmModal" class="hidden fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50"><div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl max-w-md w-full p-8"><h2 class="text-2xl font-black text-blue-400 mb-4">Confirmar Reserva</h2><div id="confirmContent" class="text-slate-300 mb-6 space-y-3"></div><div class="mb-4"><label class="block text-slate-300 font-bold mb-2">Quantidade:</label><div class="flex gap-2"><button type="button" onclick="decrementarQtd()" class="bg-red-600 text-white font-bold w-12 h-12 rounded-lg">-</button><input type="number" id="quantidadeInput" value="1" min="1" class="flex-1 bg-slate-700 text-white font-bold text-center rounded-lg border-2 border-blue-600"><button type="button" onclick="incrementarQtd()" class="bg-green-600 text-white font-bold w-12 h-12 rounded-lg">+</button></div></div><div class="flex gap-4"><button onclick="fecharModal()" class="flex-1 bg-slate-700 text-white font-bold py-2 rounded-lg">Cancelar</button><button onclick="confirmarReserva()" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg">Confirmar</button></div></div></div><script>
let reservaAtual = null;
let maxQtd = 1;

function abrirModal(id, nome, preco, stock, max) {
  reservaAtual = id;
  maxQtd = Math.min(stock, max);
  document.getElementById("quantidadeInput").max = maxQtd;
  document.getElementById("quantidadeInput").value = 1;
  document.getElementById("confirmContent").innerHTML = "<p><strong>Produto:</strong> " + nome + "</p><p><strong>Valor:</strong> R$ " + parseFloat(preco).toFixed(2) + "</p><p><strong>Disponível:</strong> " + stock + "</p>";
  document.getElementById("confirmModal").classList.remove("hidden");
}

function fecharModal() {
  document.getElementById("confirmModal").classList.add("hidden");
  reservaAtual = null;
}

function decrementarQtd() {
  let input = document.getElementById("quantidadeInput");
  if (input.value > 1) input.value = parseInt(input.value) - 1;
}

function incrementarQtd() {
  let input = document.getElementById("quantidadeInput");
  if (parseInt(input.value) < maxQtd) input.value = parseInt(input.value) + 1;
}

function confirmarReserva() {
  if (!reservaAtual) return;
  let qtd = parseInt(document.getElementById("quantidadeInput").value);
  
  fetch("/reservar", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({miniatura_id: reservaAtual, quantidade: qtd})
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      alert("Reserva realizada com sucesso! Verifique seu email para confirmação.");
      location.reload();
    } else {
      alert("ERRO: " + data.error);
    }
  })
  .catch(e => {
    alert("ERRO na requisição: " + e);
  });
  
  fecharModal();
}

function adicionarListaEspera(miniaturaId, nome) {
  if (confirm("Adicionar à lista de espera para " + nome + "?")) {
    fetch("/add-to-waitlist", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({miniatura_id: miniaturaId})
    })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert("Adicionado à lista de espera!");
      } else {
        alert("ERRO: " + data.error);
      }
    });
  }
}
</script></body></html>''')

@app.route('/reservar', methods=['POST'])
@login_required
def reservar():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    quantidade = data.get('quantidade')
    user_id = request.user.get('user_id')
    user_email = request.user.get('email')
    user_name = request.user.get('name')
    
    if not miniatura_id or not quantidade or quantidade <= 0:
        return {'success': False, 'error': 'Dados inválidos.'}, 400
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('SELECT stock, max_reservations_per_user, price, name FROM miniaturas WHERE id = ?', (miniatura_id,))
        miniatura = c.fetchone()
        
        if not miniatura:
            return {'success': False, 'error': 'Miniatura não encontrada.'}, 404
        
        current_stock = miniatura[0]
        max_reservations_per_user = miniatura[1]
        preco = miniatura[2]
        nome_miniatura = miniatura[3]
        
        if quantidade > current_stock:
            return {'success': False, 'error': f'Quantidade solicitada ({quantidade}) excede o estoque disponível ({current_stock}).'}, 400
        
        c.execute('SELECT COALESCE(SUM(quantity), 0) FROM reservations WHERE user_id = ? AND miniatura_id = ? AND status = "confirmed"', (user_id, miniatura_id))
        existing_reservations_sum = c.fetchone()[0]
        
        if (existing_reservations_sum + quantidade) > max_reservations_per_user:
            return {'success': False, 'error': f'Você já tem {existing_reservations_sum} reservas para esta miniatura. O máximo permitido é {max_reservations_per_user}.'}, 400
        
        c.execute('UPDATE miniaturas SET stock = stock - ? WHERE id = ?', (quantidade, miniatura_id))
        c.execute('INSERT INTO reservations (user_id, miniatura_id, quantity, reservation_date, status, confirmacao_enviada) VALUES (?, ?, ?, ?, ?, ?)', 
                  (user_id, miniatura_id, quantidade, datetime.now().isoformat(), 'confirmed', 0))
        
        reserva_id = c.lastrowid
        conn.commit()
        
        # Enviar email de confirmação
        total = quantidade * preco
        corpo_email = f'''
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; padding: 30px; border-left: 5px solid #3b82f6;">
                    <h1 style="color: #1e40af; text-align: center; margin-bottom: 30px;">🎉 Reserva Confirmada!</h1>
                    
                    <div style="background-color: #f0f9ff; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                        <p style="margin: 0; color: #333;"><strong>Olá, {user_name}!</strong></p>
                        <p style="color: #666; margin-top: 10px;">Sua reserva foi confirmada com sucesso! Aqui estão os detalhes:</p>
                    </div>
                    
                    <div style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                        <h3 style="color: #1e40af; margin-top: 0;">📦 Detalhes da Reserva:</h3>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr style="border-bottom: 1px solid #e0e0e0;">
                                <td style="padding: 10px 0; color: #666;"><strong>ID da Reserva:</strong></td>
                                <td style="padding: 10px 0; color: #333; text-align: right;">{reserva_id}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #e0e0e0;">
                                <td style="padding: 10px 0; color: #666;"><strong>Miniatura:</strong></td>
                                <td style="padding: 10px 0; color: #333; text-align: right;">{nome_miniatura}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #e0e0e0;">
                                <td style="padding: 10px 0; color: #666;"><strong>Quantidade:</strong></td>
                                <td style="padding: 10px 0; color: #333; text-align: right;">{quantidade}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #e0e0e0;">
                                <td style="padding: 10px 0; color: #666;"><strong>Preço Unitário:</strong></td>
                                <td style="padding: 10px 0; color: #333; text-align: right;">R$ {preco:.2f}</td>
                            </tr>
                            <tr style="background-color: #f0f9ff;">
                                <td style="padding: 15px 0; color: #1e40af;"><strong style="font-size: 16px;">Total:</strong></td>
                                <td style="padding: 15px 0; color: #1e40af; text-align: right; font-size: 18px;"><strong>R$ {total:.2f}</strong></td>
                            </tr>
                        </table>
                    </div>
                    
                    <div style="background-color: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                        <p style="margin: 0; color: #92400e;"><strong>⏱️ Próximas Etapas:</strong></p>
                        <p style="margin: 10px 0 0 0; color: #92400e; font-size: 14px;">Nossa equipe entrará em contato em breve com informações sobre o pagamento e entrega.</p>
                    </div>
                    
                    <div style="background-color: #f3f4f6; padding: 20px; border-radius: 8px; text-align: center;">
                        <p style="margin: 0; color: #666; font-size: 14px;">Dúvidas? Entre em contato conosco via WhatsApp</p>
                        <a href="https://wa.me/{WHATSAPP_NUMERO}" style="display: inline-block; margin-top: 10px; padding: 10px 20px; background-color: #25d366; color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">💬 Fale Conosco no WhatsApp</a>
                    </div>
                    
                    <div style="text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #e0e0e0;">
                        <p style="color: #999; font-size: 12px; margin: 0;">JG MINIS - Pré-vendas de Miniaturas</p>
                        <p style="color: #999; font-size: 12px; margin: 5px 0 0 0;">Obrigado por sua confiança!</p>
                    </div>
                </div>
            </body>
        </html>
        '''
        
        enviar_email(user_email, '✅ Sua Reserva foi Confirmada - JG MINIS', corpo_email)
        
        # Marcar como confirmação enviada
        c = conn.cursor()
        c.execute('UPDATE reservations SET confirmacao_enviada = 1 WHERE id = ?', (reserva_id,))
        conn.commit()
        
        return {'success': True, 'message': 'Reserva realizada com sucesso!'}
    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': str(e)}, 500
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
        return {'success': False, 'error': 'ID da miniatura inválido.'}, 400
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('SELECT id FROM waitlist WHERE user_id = ? AND miniatura_id = ?', (user_id, miniatura_id))
        if c.fetchone():
            return {'success': False, 'error': 'Você já está na lista de espera para esta miniatura.'}, 400
        
        c.execute('INSERT INTO waitlist (user_id, miniatura_id, email, request_date) VALUES (?, ?, ?, ?)',
                  (user_id, miniatura_id, user_email, datetime.now().isoformat()))
        conn.commit()
        return {'success': True, 'message': 'Adicionado à lista de espera!'}
    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        conn.close()

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    user_id = request.user.get('user_id')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT r.id, m.name, m.image_url, r.quantity, r.reservation_date, r.status, m.price, r.confirmacao_enviada
                 FROM reservations r 
                 JOIN miniaturas m ON r.miniatura_id = m.id 
                 WHERE r.user_id = ? 
                 ORDER BY r.reservation_date DESC''', (user_id,))
    reservas = c.fetchall()
    conn.close()
    
    reservas_html = ""
    if not reservas:
        reservas_html = '<p class="text-slate-400 text-center text-lg">Você ainda não fez nenhuma reserva.</p>'
    else:
        for r in reservas:
            total_price = r[3] * r[6]
            status_color = {'pending': 'bg-yellow-600', 'confirmed': 'bg-green-600', 'cancelled': 'bg-red-600'}.get(r[5], 'bg-gray-600')
            status_text = r[5].capitalize()
            
            confirmacao_badge = ''
            if r[7]:
                confirmacao_badge = '<span class="ml-2 bg-blue-600 text-white px-3 py-1 rounded-full text-sm font-bold">✅ Email Enviado</span>'
            
            reservas_html += f'''<div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden flex flex-col md:flex-row items-center p-4 gap-4">
<img src="{r[2]}" class="w-32 h-32 object-cover rounded-lg" alt="{r[1]}">
<div class="flex-grow">
<h3 class="font-bold text-blue-300 text-xl mb-1">{r[1]}</h3>
<p class="text-sm text-slate-400">Quantidade: {r[3]}</p>
<p class="text-sm text-slate-400">Preço Unitário: R$ {r[6]:.2f}</p>
<p class="text-sm text-slate-400">Total: R$ {total_price:.2f}</p>
<p class="text-sm text-slate-400">Data da Reserva: {r[4].split('T')[0]}</p>
<div class="flex items-center mt-2">
<span class="{status_color} text-white px-3 py-1 rounded-full text-sm font-bold">{status_text}</span>{confirmacao_badge}
</div>
</div>
<div class="flex flex-col gap-2">
<button onclick="cancelarReserva({r[0]})" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Cancelar</button>
</div>
</div>'''
    
    return render_template_string('''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Minhas Reservas - JG MINIS</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">JG MINIS</span><div class="flex gap-4"><a href="/" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Catálogo</a><a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12"><h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-8">Minhas Reservas</h1><div class="grid grid-cols-1 gap-6">''' + reservas_html + '''</div></div><script>
function cancelarReserva(reservaId) {
  if (confirm("Tem certeza que deseja cancelar esta reserva?")) {
    fetch("/cancelar-reserva", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reserva_id: reservaId})
    })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert("Reserva cancelada com sucesso!");
        location.reload();
      } else {
        alert("ERRO: " + data.error);
      }
    })
    .catch(e => {
      alert("ERRO na requisição: " + e);
    });
  }
}
</script></body></html>''')

@app.route('/cancelar-reserva', methods=['POST'])
@login_required
def cancelar_reserva():
    data = request.get_json()
    reserva_id = data.get('reserva_id')
    user_id = request.user.get('user_id')
    
    if not reserva_id:
        return {'success': False, 'error': 'ID da reserva inválido.'}, 400
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('SELECT miniatura_id, quantity, user_id, status FROM reservations WHERE id = ?', (reserva_id,))
        reserva = c.fetchone()
        
        if not reserva:
            return {'success': False, 'error': 'Reserva não encontrada.'}, 404
        
        if reserva[2] != user_id and not request.user.get('is_admin'):
            return {'success': False, 'error': 'Você não tem permissão para cancelar esta reserva.'}, 403
        
        if reserva[3] == 'cancelled':
            return {'success': False, 'error': 'Esta reserva já foi cancelada.'}, 400
        
        miniatura_id = reserva[0]
        quantity = reserva[1]
        
        c.execute('UPDATE miniaturas SET stock = stock + ? WHERE id = ?', (quantity, miniatura_id))
        c.execute('UPDATE reservations SET status = "cancelled" WHERE id = ?', (reserva_id,))
        conn.commit()
        return {'success': True, 'message': 'Reserva cancelada com sucesso!'}
    except Exception as e:
        conn.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        conn.close()

@app.route('/perfil')
@login_required
def perfil():
    user_id = request.user.get('user_id')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT name, email, phone FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    
    name, email, phone = user if user else ('', '', '')
    
    return render_template_string(f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Meu Perfil - JG MINIS</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-gradient-to-b from-slate-950 via-blue-950 to-black min-h-screen"><nav class="bg-gradient-to-r from-blue-900 to-black shadow-2xl border-b-4 border-red-600 sticky top-0 z-50"><div class="container mx-auto px-4 py-4 flex justify-between items-center"><span class="text-3xl font-black text-blue-400">JG MINIS</span><div class="flex gap-4"><a href="/" class="bg-blue-600 px-4 py-2 rounded text-white">Catálogo</a><a href="/logout" class="bg-red-700 px-4 py-2 rounded text-white">Sair</a></div></div></nav><div class="container mx-auto px-4 py-12 max-w-2xl"><h1 class="text-4xl font-black text-blue-400 mb-8">Meu Perfil</h1><div class="bg-slate-800 rounded-xl border-2 border-blue-600 p-6 mb-6"><h2 class="text-2xl text-blue-300 font-bold mb-4">Informações Pessoais</h2><div class="space-y-3"><p class="text-slate-300"><strong>Nome:</strong> {name}</p><p class="text-slate-300"><strong>Email:</strong> {email}</p><p class="text-slate-300"><strong>Telefone:</strong> {phone}</p></div></div><div class="bg-slate-800 rounded-xl border-2 border-green-600 p-6"><h2 class="text-2xl text-green-400 font-bold mb-4">Alterar Senha</h2><form action="/mudar-senha" method="POST" class="space-y-4"><div><label class="block text-slate-300 font-bold mb-2">Senha Atual:</label><input type="password" name="senha_atual" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600"></div><div><label class="block text-slate-300 font-bold mb-2">Nova Senha:</label><input type="password" name="nova_senha" required minlength="6" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600"></div><div><label class="block text-slate-300 font-bold mb-2">Confirmar Nova Senha:</label><input type="password" name="confirmar_senha" required minlength="6" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600"></div><button type="submit" class="w-full bg-gradient-to-r from-green-600 to-blue-600 text-white font-bold py-2 rounded-lg">Alterar Senha</button></form></div></div></body></html>''')

@app.route('/mudar-senha', methods=['POST'])
@login_required
def mudar_senha():
    user_id = request.user.get('user_id')
    senha_atual = request.form['senha_atual']
    nova_senha = request.form['nova_senha']
    confirmar_senha = request.form['confirmar_senha']
    
    if nova_senha != confirmar_senha:
        flash('As novas senhas não coincidem.', 'error')
        return redirect(url_for('perfil'))
    
    if len(nova_senha) < 6:
        flash('A nova senha deve ter pelo menos 6 caracteres.', 'error')
        return redirect(url_for('perfil'))
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT password FROM users WHERE id = ?', (user_id,))
    user_data = c.fetchone()
    
    if not user_data or not bcrypt.checkpw(senha_atual.encode('utf-8'), user_data[0].encode('utf-8')):
        flash('Senha atual incorreta.', 'error')
        conn.close()
        return redirect(url_for('perfil'))
    
    nova_senha_hash = bcrypt.hashpw(nova_senha.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    c.execute('UPDATE users SET password = ? WHERE id = ?', (nova_senha_hash, user_id))
    conn.commit()
    conn.close()
    
    flash('Senha alterada com sucesso!', 'success')
    return redirect(url_for('perfil'))

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
        miniaturas_html += f'<tr class="border-b border-slate-700"><td class="px-4 py-3">{m[0]}</td><td class="px-4 py-3">{m[1]}</td><td class="px-4 py-3">{m[2]}</td><td class="px-4 py-3">R$ {m[3]:.2f}</td><td class="px-4 py-3 flex gap-2"><a href="/admin/edit-miniatura/{m[0]}" class="bg-blue-600 text-white px-3 py-1 rounded text-sm">Editar</a><button onclick="deleteMiniatura({m[0]})" class="bg-red-600 text-white px-3 py-1 rounded text-sm">Excluir</button></td></tr>'
    
    return render_template_string('''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin - JG MINIS</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 min-h-screen"><nav class="bg-blue-900 p-4 flex justify-between"><a href="/" class="text-white font-bold">JG MINIS</a><div class="flex gap-2"><a href="/usuarios" class="bg-purple-600 text-white px-3 py-2 rounded">Usuários</a><a href="/lista-espera" class="bg-green-600 text-white px-3 py-2 rounded">Lista</a><a href="/relatorio-reservas" class="bg-indigo-600 text-white px-3 py-2 rounded">Relatório</a><a href="/logout" class="bg-red-700 text-white px-3 py-2 rounded">Sair</a></div></nav><div class="p-8"><h1 class="text-3xl text-blue-400 font-bold mb-6">Admin</h1><a href="/admin/add-miniatura" class="bg-green-600 text-white px-4 py-2 rounded mb-4 inline-block">Adicionar</a><table class="w-full bg-slate-800 rounded"><thead class="bg-slate-700"><tr><th class="px-4 py-2 text-white">ID</th><th class="px-4 py-2 text-white">Nome</th><th class="px-4 py-2 text-white">Estoque</th><th class="px-4 py-2 text-white">Preço</th><th class="px-4 py-2 text-white">Ações</th></tr></thead><tbody>''' + miniaturas_html + '''</tbody></table></div><script>
function deleteMiniatura(id) {
  if (confirm("Excluir?")) {
    fetch("/admin/delete-miniatura/" + id, {method: "POST"}).then(() => location.reload());
  }
}
</script></body></html>''')

@app.route('/atualizar-planilha')
@login_required
@admin_required
def atualizar_planilha():
    load_miniaturas_from_sheets()
    flash('Atualizado!', 'success')
    return redirect(url_for('admin_panel'))

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
            flash('Adicionada!', 'success')
            return redirect(url_for('admin_panel'))
        except:
            flash('Erro!', 'error')
        finally:
            conn.close()
    return render_template_string('''<!DOCTYPE html><html><head><title>Adicionar</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 min-h-screen flex items-center justify-center"><div class="bg-slate-800 p-8 rounded-xl border border-blue-600 max-w-sm w-full"><h2 class="text-2xl text-blue-400 font-bold mb-4">Nova Miniatura</h2><form method="POST" class="space-y-3"><input type="text" name="name" placeholder="Nome" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="url" name="image_url" placeholder="URL" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="date" name="arrival_date" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="number" name="stock" placeholder="Estoque" required min="0" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="number" name="price" placeholder="Preço" required step="0.01" min="0" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><textarea name="observations" placeholder="Obs" rows="2" class="w-full px-3 py-2 bg-slate-700 text-white rounded"></textarea><input type="number" name="max_reservations_per_user" placeholder="Máx" required min="1" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><div class="flex gap-2"><a href="/admin" class="flex-1 text-center bg-slate-700 text-white py-2 rounded">Voltar</a><button class="flex-1 bg-green-600 text-white font-bold py-2 rounded">Salvar</button></div></form></div></body></html>''')

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
            flash('Atualizado!', 'success')
            return redirect(url_for('admin_panel'))
        except:
            flash('Erro!', 'error')
        finally:
            conn.close()
    
    c.execute('SELECT image_url, name, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
    m = c.fetchone()
    conn.close()
    
    if not m:
        return redirect(url_for('admin_panel'))
    
    return render_template_string('''<!DOCTYPE html><html><head><title>Editar</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 min-h-screen flex items-center justify-center"><div class="bg-slate-800 p-8 rounded-xl border border-blue-600 max-w-sm w-full"><h2 class="text-2xl text-blue-400 font-bold mb-4">Editar</h2><form method="POST" class="space-y-3"><input type="text" name="name" value="''' + m[1] + '''" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="url" name="image_url" value="''' + m[0] + '''" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="date" name="arrival_date" value="''' + m[2] + '''" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="number" name="stock" value="''' + str(m[3]) + '''" required min="0" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="number" name="price" value="''' + str(m[4]) + '''" required step="0.01" min="0" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><textarea name="observations" rows="2" class="w-full px-3 py-2 bg-slate-700 text-white rounded">''' + m[5] + '''</textarea><input type="number" name="max_reservations_per_user" value="''' + str(m[6]) + '''" required min="1" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><div class="flex gap-2"><a href="/admin" class="flex-1 text-center bg-slate-700 text-white py-2 rounded">Voltar</a><button class="flex-1 bg-blue-600 text-white font-bold py-2 rounded">Salvar</button></div></form></div></body></html>''')

@app.route('/admin/delete-miniatura/<int:miniatura_id>', methods=['POST'])
@login_required
@admin_required
def delete_miniatura(miniatura_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM miniaturas WHERE id = ?', (miniatura_id,))
    conn.commit()
    conn.close()
    return {'success': True}

@app.route('/usuarios')
@login_required
@admin_required
def usuarios():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, name, email, phone, is_admin FROM users')
    users = c.fetchall()
    conn.close()
    
    html = ""
    for u in users:
        admin = "Sim" if u[4] else "Não"
        promote = "" if u[4] else f'<button onclick="promover({u[0]})" class="bg-yellow-600 px-2 py-1 rounded text-white text-sm">Promover</button>'
        html += f'<tr><td class="px-4 py-2 text-white">{u[0]}</td><td class="px-4 py-2 text-white">{u[1]}</td><td class="px-4 py-2 text-white">{u[2]}</td><td class="px-4 py-2 text-white">{u[3]}</td><td class="px-4 py-2 text-white">{admin}</td><td class="px-4 py-2">{promote}<a href="/admin/edit-user/{u[0]}" class="bg-blue-600 px-2 py-1 rounded text-white text-sm">Editar</a><button onclick="resetar({u[0]})" class="bg-orange-600 px-2 py-1 rounded text-white text-sm">Reset</button><button onclick="deletar({u[0]})" class="bg-red-600 px-2 py-1 rounded text-white text-sm">Excluir</button></td></tr>'
    
    return render_template_string(f'''<!DOCTYPE html><html><head><title>Usuários</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 min-h-screen"><nav class="bg-blue-900 p-4 flex justify-between"><a href="/admin" class="text-white">← Admin</a></nav><div class="p-8"><h1 class="text-3xl text-blue-400 font-bold mb-6">Usuários</h1><a href="/admin/add-user" class="bg-green-600 text-white px-4 py-2 rounded">Novo</a><a href="/export-usuarios" class="bg-indigo-600 text-white px-4 py-2 rounded ml-2">Excel</a><table class="w-full bg-slate-800 rounded mt-4"><thead class="bg-slate-700"><tr><th class="px-4 py-2 text-white">ID</th><th class="px-4 py-2 text-white">Nome</th><th class="px-4 py-2 text-white">Email</th><th class="px-4 py-2 text-white">Tel</th><th class="px-4 py-2 text-white">Admin</th><th class="px-4 py-2 text-white">Ações</th></tr></thead><tbody>{html}</tbody></table></div><script>
function promover(id) {{if (confirm("Promover?")) {{fetch("/admin/promote-user/" + id, {{method: "POST"}}).then(() => location.reload());}}}}
function resetar(id) {{let s = prompt("Nova senha:"); if (s && s.length >= 6) {{fetch("/admin/change-password/" + id, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{nova_senha: s}})}}). then(() => alert("Ok!"))}}}}
function deletar(id) {{if (confirm("Deletar?")) {{fetch("/admin/delete-user/" + id, {{method: "POST"}}).then(() => location.reload());}}}}
</script></body></html>''')

@app.route('/admin/add-user', methods=['GET', 'POST'])
@login_required
@admin_required
def add_user():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        is_admin = 1 if 'is_admin' in request.form else 0
        
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)",
                      (name, email, phone, hashed, is_admin))
            conn.commit()
            return redirect(url_for('usuarios'))
        except:
            pass
        finally:
            conn.close()
    
    return render_template_string('''<!DOCTYPE html><html><head><title>Novo Usuário</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 flex items-center justify-center min-h-screen"><div class="bg-slate-800 p-8 rounded-xl border border-blue-600 max-w-sm w-full"><h2 class="text-2xl text-blue-400 font-bold mb-4">Novo Usuário</h2><form method="POST" class="space-y-3"><input type="text" name="name" placeholder="Nome" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="email" name="email" placeholder="Email" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="text" name="phone" placeholder="Telefone" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="password" name="password" placeholder="Senha" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><label class="flex items-center text-white"><input type="checkbox" name="is_admin" class="mr-2">Admin</label><div class="flex gap-2"><a href="/usuarios" class="flex-1 text-center bg-slate-700 text-white py-2 rounded">Voltar</a><button class="flex-1 bg-green-600 text-white font-bold py-2 rounded">Salvar</button></div></form></div></body></html>''')

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
        
        c.execute("UPDATE users SET name=?, email=?, phone=?, is_admin=? WHERE id=?",
                  (name, email, phone, is_admin, user_id))
        conn.commit()
        conn.close()
        return redirect(url_for('usuarios'))
    
    c.execute('SELECT name, email, phone, is_admin FROM users WHERE id = ?', (user_id,))
    u = c.fetchone()
    conn.close()
    
    checked = "checked" if u[3] else ""
    
    return render_template_string(f'''<!DOCTYPE html><html><head><title>Editar Usuário</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 flex items-center justify-center min-h-screen"><div class="bg-slate-800 p-8 rounded-xl border border-blue-600 max-w-sm w-full"><h2 class="text-2xl text-blue-400 font-bold mb-4">Editar</h2><form method="POST" class="space-y-3"><input type="text" name="name" value="{u[0]}" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="email" name="email" value="{u[1]}" required class="w-full px-3 py-2 bg-slate-700 text-white rounded"><input type="text" name="phone" value="{u[2]}" class="w-full px-3 py-2 bg-slate-700 text-white rounded"><label class="flex items-center text-white"><input type="checkbox" name="is_admin" {checked} class="mr-2">Admin</label><div class="flex gap-2"><a href="/usuarios" class="flex-1 text-center bg-slate-700 text-white py-2 rounded">Voltar</a><button class="flex-1 bg-blue-600 text-white font-bold py-2 rounded">Salvar</button></div></form></div></body></html>''')

@app.route('/admin/promote-user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def promote_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET is_admin = 1 WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return {'success': True}

@app.route('/admin/change-password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def change_password(user_id):
    data = request.get_json()
    nova_senha = data.get('nova_senha')
    
    if not nova_senha or len(nova_senha) < 6:
        return {'success': False, 'error': 'Mínimo 6'}, 400
    
    hashed = bcrypt.hashpw(nova_senha.encode(), bcrypt.gensalt()).decode()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET password = ? WHERE id = ?', (hashed, user_id))
    conn.commit()
    conn.close()
    return {'success': True}

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return {'success': True}

@app.route('/export-usuarios')
@login_required
@admin_required
def export_usuarios():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, name, email, phone, is_admin FROM users')
    users = c.fetchall()
    conn.close()
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Usuários"
    ws.append(['ID', 'Nome', 'Email', 'Telefone', 'Admin'])
    
    for u in users:
        ws.append([u[0], u[1], u[2], u[3], "Sim" if u[4] else "Não"])
    
    file = BytesIO()
    wb.save(file)
    file.seek(0)
    return send_file(file, mimetype='application/vnd.ms-excel', as_attachment=True, download_name='usuarios.xlsx')

@app.route('/relatorio-reservas')
@login_required
@admin_required
def relatorio_reservas():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT r.id, u.name, m.name, r.quantity, m.price, r.reservation_date, r.status, r.confirmacao_enviada
                 FROM reservations r
                 JOIN users u ON r.user_id = u.id
                 JOIN miniaturas m ON r.miniatura_id = m.id
                 ORDER BY r.reservation_date DESC''')
    reservas = c.fetchall()
    conn.close()
    
    html = ""
    total = 0
    for r in reservas:
        subtotal = r[3] * r[4]
        total += subtotal
        confirmacao = "✅" if r[7] else "❌"
        status_color = {'confirmed': 'green', 'cancelled': 'red', 'pending': 'yellow'}.get(r[6], 'gray')
        html += f'<tr><td class="px-4 py-2 text-white">{r[0]}</td><td class="px-4 py-2 text-white">{r[1]}</td><td class="px-4 py-2 text-white">{r[2]}</td><td class="px-4 py-2 text-white">{r[3]}</td><td class="px-4 py-2 text-white">R$ {r[4]:.2f}</td><td class="px-4 py-2 text-white">R$ {subtotal:.2f}</td><td class="px-4 py-2"><span class="bg-{status_color}-600 px-2 py-1 rounded text-white text-sm">{r[6]}</span></td><td class="px-4 py-2 text-white text-center">{confirmacao}</td></tr>'
    
    return render_template_string(f'''<!DOCTYPE html><html><head><title>Relatório</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 min-h-screen"><nav class="bg-blue-900 p-4"><a href="/admin" class="text-white">← Admin</a></nav><div class="p-8"><h1 class="text-3xl text-blue-400 font-bold mb-6">Relatório de Reservas</h1><table class="w-full bg-slate-800 rounded"><thead class="bg-slate-700"><tr><th class="px-4 py-2 text-white">ID</th><th class="px-4 py-2 text-white">Usuário</th><th class="px-4 py-2 text-white">Miniatura</th><th class="px-4 py-2 text-white">Qtd</th><th class="px-4 py-2 text-white">Preço</th><th class="px-4 py-2 text-white">Total</th><th class="px-4 py-2 text-white">Status</th><th class="px-4 py-2 text-white">Email</th></tr></thead><tbody>{html}</tbody></table><div class="mt-4 bg-slate-800 p-4 rounded"><h3 class="text-xl text-blue-400 font-bold">Total: R$ {total:.2f}</h3></div></div></body></html>''')

@app.route('/lista-espera')
@login_required
@admin_required
def lista_espera():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT wl.id, u.name, u.email, m.name, wl.notification_sent 
                 FROM waitlist wl 
                 JOIN users u ON wl.user_id = u.id 
                 JOIN miniaturas m ON wl.miniatura_id = m.id''')
    waitlist = c.fetchall()
    conn.close()
    
    html = ""
    for w in waitlist:
        status = "✓" if w[4] else "⏳"
        html += f'<tr><td class="px-4 py-2 text-white">{w[0]}</td><td class="px-4 py-2 text-white">{w[1]}</td><td class="px-4 py-2 text-white">{w[2]}</td><td class="px-4 py-2 text-white">{w[3]}</td><td class="px-4 py-2 text-white">{status}</td></tr>'
    
    return render_template_string(f'''<!DOCTYPE html><html><head><title>Lista de Espera</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 min-h-screen"><nav class="bg-blue-900 p-4"><a href="/admin" class="text-white">← Admin</a></nav><div class="p-8"><h1 class="text-3xl text-blue-400 font-bold mb-6">Lista de Espera</h1><table class="w-full bg-slate-800 rounded"><thead class="bg-slate-700"><tr><th class="px-4 py-2 text-white">ID</th><th class="px-4 py-2 text-white">Usuário</th><th class="px-4 py-2 text-white">Email</th><th class="px-4 py-2 text-white">Miniatura</th><th class="px-4 py-2 text-white">Status</th></tr></thead><tbody>{html}</tbody></table></div></body></html>''')

init_db()
load_initial_data()
load_miniaturas_from_sheets()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
