import sqlite3
import json
from flask import Flask, request, redirect, url_for, session, render_template_string, flash, g
from functools import wraps
import os
import bcrypt
import re
from datetime import datetime

# Google Sheets imports
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sua_chave_secreta_aqui')
DB_FILE = 'database.db'
WHATSAPP_NUMERO = os.environ.get('WHATSAPP_NUMERO', '5511999999999')

# Google Sheets Configuration
GOOGLE_SHEET_ID = '1sxlvo6j-UTB0xXuyivzWnhRuYvpJFcH2smL4ZzHTUps'
GOOGLE_SHEET_NAME = 'Miniaturas'

# --- Funções de Banco de Dados ---
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_FILE)
        g.db.row_factory = sqlite3.Row # This makes rows behave like dictionaries
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        c = db.cursor()
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
                brand TEXT, 
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
                status TEXT DEFAULT 'pending',
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
        db.commit()
        print("OK BD inicializado")

def load_initial_data():
    with app.app_context():
        db = get_db()
        c = db.cursor()

        c.execute("SELECT * FROM users WHERE email = 'admin@jgminis.com.br'")
        if not c.fetchone():
            hashed_password = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)",
                      ('Admin', 'admin@jgminis.com.br', '5511999999999', hashed_password, 1))
            print("OK Usuário admin adicionado.")

        c.execute("SELECT * FROM users WHERE email = 'usuario@example.com'")
        if not c.fetchone():
            hashed_password = bcrypt.hashpw('usuario123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            c.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)",
                      ('Usuário Teste', 'usuario@example.com', '5511988888888', hashed_password, 0))
            print("OK Usuário teste adicionado.")
        db.commit()
        
        # Call Google Sheets update here initially
        try:
            update_from_google_sheets()
            print("OK Dados iniciais carregados e planilha Google Sheets sincronizada.")
        except Exception as e:
            print(f"ERRO CRÍTICO ao sincronizar com Google Sheets na inicialização: {e}")


def update_from_google_sheets():
    with app.app_context():
        try:
            # Load credentials from environment variable
            creds_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
            if not creds_json:
                raise ValueError("GOOGLE_SHEETS_CREDENTIALS environment variable not set.")
            
            creds_dict = json.loads(creds_json)
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            client = gspread.authorize(creds)

            sheet = client.open_by_id(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_NAME)
            data = sheet.get_all_records() # Get all rows as a list of dictionaries

            db = get_db()
            c = db.cursor()

            # Clear existing miniaturas to re-sync
            c.execute('DELETE FROM miniaturas') 
            print("OK Miniaturas existentes apagadas para sincronização.")

            for row in data:
                try:
                    image_url = row.get('IMAGEM', '')
                    name = row.get('NOME DA MINIATURA', '')
                    brand = row.get('MARCA/FABRICANTE', '') 
                    arrival_date = row.get('PREVISÃO DE CHEGADA', '')
                    stock = int(row.get('QUANTIDADE DISPONIVEL', 0))
                    price = float(str(row.get('VALOR', 0.0)).replace(',', '.')) # Handle comma as decimal separator
                    observations = row.get('OBSERVAÇÕES', '')
                    max_reservations_per_user = int(row.get('MAX_RESERVAS_POR_USUARIO', 1))

                    if not name: # Skip rows without a name
                        continue

                    c.execute("INSERT INTO miniaturas (image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user))
                    # print(f"  OK Sincronizado: {name}") # Too verbose for production
                except Exception as e:
                    print(f"  ERRO ao processar linha da planilha: {row} - {e}")
            
            db.commit()
            print(f"OK Total sincronizado: {len(data)} miniaturas da planilha.")
        except Exception as e:
            print(f"ERRO ao sincronizar com Google Sheets: {e}")
            # Re-raise to be caught by the route handler if called via HTTP
            raise

# --- Decoradores ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        
        db = get_db()
        c = db.cursor()
        c.execute('SELECT id, name, email, phone, is_admin FROM users WHERE id = ?', (session['user_id'],))
        user = c.fetchone()
        
        if user:
            request.user = {
                'user_id': user['id'],
                'name': user['name'],
                'email': user['email'],
                'phone': user['phone'],
                'is_admin': bool(user['is_admin'])
            }
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

# --- Rotas de Autenticação ---
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

        db = get_db()
        c = db.cursor()
        try:
            c.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)",
                      (name, email, phone, hashed_password))
            db.commit()
            flash('Registro bem-sucedido! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('E-mail já registrado.', 'error')
        finally:
            pass # db is closed by teardown_appcontext
    
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
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        db = get_db()
        c = db.cursor()
        c.execute('SELECT id, password FROM users WHERE email = ?', (email,))
        user_data = c.fetchone()

        if user_data and bcrypt.checkpw(password.encode('utf-8'), user_data['password'].encode('utf-8')):
            session['user_id'] = user_data['id']
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
    ''')

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('login'))

# --- Rotas Principais ---
@app.route('/')
@login_required
def index():
    db = get_db()
    c = db.cursor()
    c.execute('SELECT id, image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas')
    miniaturas_db = c.fetchall() # Fetch raw data from DB
    
    user_id = request.user.get('user_id')
    c.execute('SELECT name, email, phone FROM users WHERE id = ?', (user_id,))
    user_data = c.fetchone()
    user_name, user_email, user_phone = user_data['name'], user_data['email'], user_data['phone']
    
    # Prepare miniaturas data for rendering and JavaScript
    miniaturas_for_template = []
    miniaturas_for_js = {} # Dictionary for quick lookup in JS
    for m in miniaturas_db:
        miniatura_dict = {
            'id': m['id'],
            'image_url': m['image_url'],
            'name': m['name'],
            'brand': m['brand'],
            'arrival_date': m['arrival_date'],
            'stock': m['stock'],
            'price': m['price'],
            'observations': m['observations'],
            'max_reservations_per_user': m['max_reservations_per_user'],
            'is_esgotado': m['stock'] <= 0,
            'status': "ESGOTADO" if m['stock'] <= 0 else f"Em Estoque: {m['stock']}",
            'status_color': "red" if m['stock'] <= 0 else "green"
        }
        miniaturas_for_template.append(miniatura_dict)
        miniaturas_for_js[m['id']] = miniatura_dict # Store by ID for JS lookup

    admin_links = ''
    if request.user.get('is_admin'):
        admin_links = '''
            <a href="/admin" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Admin</a>
            <a href="/pessoas" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-lg font-semibold">Pessoas</a>
            <a href="/lista-espera" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold">Lista de Espera</a>
            <button onclick="updateSheet()" class="bg-yellow-600 hover:bg-yellow-700 text-white px-4 py-2 rounded-lg font-semibold">Atualizar Planilha 🔄</button>
        '''
    
    # Use render_template_string with Jinja2 syntax for loops and conditionals
    return render_template_string('''
        <!DOCTYPE html>
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
                        <a href="/minhas-reservas" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-semibold">Minhas Reservas</a>
                        ''' + admin_links + '''
                        <a href="/logout" class="bg-red-700 hover:bg-red-800 text-white px-4 py-2 rounded-lg font-semibold">Sair</a>
                    </div>
                </div>
            </nav>
            <div class="container mx-auto px-4 py-12">
                <h1 class="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500 mb-2">Catálogo de Miniaturas</h1>
                <p class="text-slate-300 mb-8">Pré-vendas</p>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                    {% for m in miniaturas %}
                    <div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden">
                        <div class="bg-black h-48 flex items-center justify-center relative overflow-hidden">
                            <img src="{{ m.image_url }}" class="w-full h-full object-cover" alt="{{ m.name }}" onerror="this.style.background='linear-gradient(135deg, #1e40af 0%, #7c3aed 100%)'">
                            <div class="absolute top-3 right-3 bg-{{ m.status_color }}-600 text-white px-3 py-1 rounded-full text-sm font-bold">{{ m.status }}</div>
                        </div>
                        <div class="p-4">
                            <h3 class="font-bold text-blue-300 mb-2 text-lg">{{ m.name }}</h3>
                            <p class="text-sm text-slate-400 mb-2">Chegada: {{ m.arrival_date }}</p>
                            <p class="text-sm text-slate-400 mb-3">{{ m.observations }}</p>
                            <div class="flex justify-between items-center gap-2">
                                <span class="text-2xl font-black text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-red-500">R$ {{ "%.2f"|format(m.price) }}</span>
                                {% if m.is_esgotado %}
                                    <a href="https://wa.me/{{ whatsapp_numero }}?text=Olá%20JG%20MINIS,%20gostaria%20de%20informações%20sobre%20a%20miniatura:%20{{ m.name }}" target="_blank" class="bg-orange-600 hover:bg-orange-700 text-white font-bold px-4 py-2 rounded-lg">Entrar em Contato</a>
                                {% else %}
                                    <button onclick="abrirConfirmacao({{ m.id }})" class="bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold px-4 py-2 rounded-lg">Reservar</button>
                                {% endif %}
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            <div id="confirmModal" class="hidden fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
                <div class="bg-gradient-to-b from-slate-800 to-black rounded-xl border-2 border-red-600 shadow-2xl max-w-md w-full p-8">
                    <h2 class="text-2xl font-black text-blue-400 mb-4">Confirmar Reserva</h2>
                    <div id="confirmContent" class="text-slate-300 mb-6 space-y-3"></div>
                    <div class="mb-4">
                        <label class="block text-slate-300 font-bold mb-2">Quantidade:</label>
                        <div class="flex gap-2">
                            <button type="button" onclick="decrementarQtd()" class="bg-red-600 text-white font-bold w-12 h-12 rounded-lg">-</button>
                            <input type="number" id="quantidadeInput" value="1" min="1" class="flex-1 bg-slate-700 text-white font-bold text-center rounded-lg border-2 border-blue-600">
                            <button type="button" onclick="incrementarQtd()" class="bg-green-600 text-white font-bold w-12 h-12 rounded-lg">+</button>
                        </div>
                    </div>
                    <div class="flex gap-4">
                        <button onclick="fecharModal()" class="flex-1 text-center bg-slate-700 text-white font-bold py-2 rounded-lg">Cancelar</button>
                        <button onclick="confirmarReserva()" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg">Confirmar</button>
                    </div>
                </div>
            </div>
            <script>
                // Pass Python data to JavaScript
                const miniaturasData = {{ miniaturas_for_js | tojson }};
                let reservaAtualId = null;
                let maxQtd = 1;
                const whatsappNumero = "{{ whatsapp_numero }}";

                function abrirConfirmacao(miniaturaId) {
                    const miniatura = miniaturasData[miniaturaId];
                    if (!miniatura) {
                        console.error("Miniatura não encontrada:", miniaturaId);
                        alert("Erro: Miniatura não encontrada.");
                        return;
                    }
                    reservaAtualId = miniaturaId;
                    maxQtd = Math.min(miniatura.stock, miniatura.max_reservations_per_user);
                    const quantidadeInput = document.getElementById("quantidadeInput");
                    quantidadeInput.max = maxQtd;
                    quantidadeInput.value = 1; // Reset quantity to 1
                    document.getElementById("confirmContent").innerHTML = `
                        <p><strong>Produto:</strong> ${miniatura.name}</p>
                        <p><strong>Valor:</strong> R$ ${miniatura.price.toFixed(2)}</p>
                        <p><strong>Disponível:</strong> ${miniatura.stock}</p>
                    `;
                    document.getElementById("confirmModal").classList.remove("hidden");
                }

                function fecharModal() {
                    document.getElementById("confirmModal").classList.add("hidden");
                    reservaAtualId = null;
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
                    if (!reservaAtualId) return;
                    let qtd = parseInt(document.getElementById("quantidadeInput").value);
                    fetch("/reservar", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({miniatura_id: reservaAtualId, quantidade: qtd})
                    })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            alert("OK Reserva realizada!");
                            location.reload();
                        } else {
                            alert("ERRO: " + data.error);
                        }
                    })
                    .catch(e => {
                        alert("ERRO na requisição: " + e);
                        console.error("Erro na requisição de reserva:", e);
                    });
                    fecharModal();
                }

                function updateSheet() {
                    if (confirm("Tem certeza que deseja atualizar os dados da planilha? Isso recarregará todas as miniaturas.")) {
                        fetch("/admin/update-sheet", {
                            method: "POST",
                            headers: {"Content-Type": "application/json"}
                        })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                alert("Planilha atualizada com sucesso! A página será recarregada.");
                                location.reload();
                            } else {
                                alert("ERRO ao atualizar planilha: " + data.error);
                            }
                        })
                        .catch(e => {
                            alert("ERRO na requisição de atualização da planilha: " + e);
                            console.error("Erro na requisição de atualização da planilha:", e);
                        });
                    }
                }
            </script>
        </body>
        </html>
    ''', miniaturas=miniaturas_for_template, whatsapp_numero=WHATSAPP_NUMERO, miniaturas_for_js=miniaturas_for_js)

@app.route('/reservar', methods=['POST'])
@login_required
def reservar():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    quantidade = data.get('quantidade')
    user_id = request.user.get('user_id')

    if not miniatura_id or not quantidade or quantidade <= 0:
        return {'success': False, 'error': 'Dados inválidos.'}, 400

    db = get_db()
    c = db.cursor()
    try:
        c.execute('SELECT stock, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
        miniatura = c.fetchone()

        if not miniatura:
            return {'success': False, 'error': 'Miniatura não encontrada.'}, 404

        current_stock = miniatura['stock']
        max_reservations_per_user = miniatura['max_reservations_per_user']

        if quantidade > current_stock:
            return {'success': False, 'error': f'Quantidade solicitada ({quantidade}) excede o estoque disponível ({current_stock}).'}, 400
        
        c.execute('SELECT SUM(quantity) FROM reservations WHERE user_id = ? AND miniatura_id = ? AND status = "pending"', (user_id, miniatura_id))
        existing_reservations_sum = c.fetchone()[0] or 0

        if (existing_reservations_sum + quantidade) > max_reservations_per_user:
            return {'success': False, 'error': f'Você já tem {existing_reservations_sum} reservas para esta miniatura. O máximo permitido é {max_reservations_per_user}.'}, 400

        c.execute('UPDATE miniaturas SET stock = stock - ? WHERE id = ?', (quantidade, miniatura_id))
        c.execute('INSERT INTO reservations (user_id, miniatura_id, quantity, reservation_date, status) VALUES (?, ?, ?, ?, ?)',
                  (user_id, miniatura_id, quantidade, datetime.now().isoformat(), 'pending'))
        db.commit()
        return {'success': True, 'message': 'Reserva realizada com sucesso!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    user_id = request.user.get('user_id')
    db = get_db()
    c = db.cursor()
    c.execute('''
        SELECT
            r.id, m.name, m.image_url, r.quantity, r.reservation_date, r.status, m.price
        FROM reservations r
        JOIN miniaturas m ON r.miniatura_id = m.id
        WHERE r.user_id = ?
        ORDER BY r.reservation_date DESC
    ''', (user_id,))
    reservas = c.fetchall()

    reservas_html = ""
    if not reservas:
        reservas_html = '<p class="text-slate-400 text-center text-lg">Você ainda não fez nenhuma reserva.</p>'
    else:
        for r in reservas:
            total_price = r['quantity'] * r['price']
            status_color = {
                'pending': 'bg-yellow-600',
                'confirmed': 'bg-green-600',
                'cancelled': 'bg-red-600'
            }.get(r['status'], 'bg-gray-600')

            reservas_html += '''
                <div class="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-lg border-2 border-blue-600 overflow-hidden flex flex-col md:flex-row items-center p-4 gap-4">
                    <img src="{image_url}" class="w-32 h-32 object-cover rounded-lg" alt="{name}">
                    <div class="flex-grow">
                        <h3 class="font-bold text-blue-300 text-xl mb-1">{name}</h3>
                        <p class="text-sm text-slate-400">Quantidade: {quantity}</p>
                        <p class="text-sm text-slate-400">Preço Unitário: R$ {price:.2f}</p>
                        <p class="text-sm text-slate-400">Total: R$ {total_price:.2f}</p>
                        <p class="text-sm text-slate-400">Data da Reserva: {reservation_date}</p>
                        <span class="{status_color} text-white px-3 py-1 rounded-full text-sm font-bold mt-2 inline-block">{status_text}</span>
                    </div>
                    <div class="flex flex-col gap-2">
                        <button onclick="cancelarReserva({reservation_id})" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-semibold">Cancelar</button>
                    </div>
                </div>
            '''.format(
                image_url=r['image_url'],
                name=r['name'],
                quantity=r['quantity'],
                price=r['price'],
                total_price=total_price,
                reservation_date=r['reservation_date'].split('T')[0],
                status_color=status_color,
                status_text=r['status'].capitalize(),
                reservation_id=r['id']
            )
    
    return render_template_string('''
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
                    {reservas_html}
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
    '''.format(reservas_html=reservas_html))

@app.route('/cancelar-reserva', methods=['POST'])
@login_required
def cancelar_reserva():
    data = request.get_json()
    reserva_id = data.get('reserva_id')
    user_id = request.user.get('user_id')

    if not reserva_id:
        return {'success': False, 'error': 'ID da reserva inválido.'}, 400

    db = get_db()
    c = db.cursor()
    try:
        c.execute('SELECT miniatura_id, quantity, user_id, status FROM reservations WHERE id = ?', (reserva_id,))
        reserva = c.fetchone()

        if not reserva:
            return {'success': False, 'error': 'Reserva não encontrada.'}, 404
        
        if reserva['user_id'] != user_id and not request.user.get('is_admin'):
            return {'success': False, 'error': 'Você não tem permissão para cancelar esta reserva.'}, 403

        if reserva['status'] == 'cancelled':
            return {'success': False, 'error': 'Esta reserva já foi cancelada.'}, 400

        miniatura_id = reserva['miniatura_id']
        quantity = reserva['quantity']

        c.execute('UPDATE miniaturas SET stock = stock + ? WHERE id = ?', (quantity, miniatura_id))
        c.execute('UPDATE reservations SET status = "cancelled" WHERE id = ?', (reserva_id,))
        db.commit()
        return {'success': True, 'message': 'Reserva cancelada com sucesso!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

# --- Rotas de Admin ---
@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    db = get_db()
    c = db.cursor()
    c.execute('SELECT id, name, stock, price FROM miniaturas')
    miniaturas = c.fetchall()

    return render_template_string('''
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
                            {% for m in miniaturas %}
                            <tr class="border-b border-slate-700 hover:bg-slate-700">
                                <td class="px-4 py-3">{{ m.id }}</td>
                                <td class="px-4 py-3">{{ m.name }}</td>
                                <td class="px-4 py-3">{{ m.stock }}</td>
                                <td class="px-4 py-3">R$ {{ "%.2f"|format(m.price) }}</td>
                                <td class="px-4 py-3 flex gap-2">
                                    <a href="/admin/edit-miniatura/{{ m.id }}" class="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded-lg text-sm">Editar</a>
                                    <button onclick="deleteMiniatura({{ m.id }})" class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-sm">Excluir</button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            <script>
                function deleteMiniatura(id) {
                    if (confirm("Tem certeza que deseja excluir esta miniatura?")) {
                        fetch("/admin/delete-miniatura/" + id, {
                            method: "POST",
                            headers: {"Content-Type": "application/json"}
                        })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                alert("Miniatura excluída com sucesso!");
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
            </script>
        </body>
        </html>
    ''', miniaturas=miniaturas) # Pass miniaturas to the template

@app.route('/admin/add-miniatura', methods=['GET', 'POST'])
@login_required
@admin_required
def add_miniatura():
    if request.method == 'POST':
        image_url = request.form['image_url']
        name = request.form['name']
        brand = request.form['brand'] # New field
        arrival_date = request.form['arrival_date']
        stock = int(request.form['stock'])
        price = float(request.form['price'])
        observations = request.form['observations']
        max_reservations_per_user = int(request.form['max_reservations_per_user'])

        db = get_db()
        c = db.cursor()
        try:
            c.execute("INSERT INTO miniaturas (image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user))
            db.commit()
            flash('Miniatura adicionada com sucesso!', 'success')
            return redirect(url_for('admin_panel'))
        except Exception as e:
            flash(f'Erro ao adicionar miniatura: {e}', 'error')
        finally:
            pass # db is closed by teardown_appcontext
    
    return render_template_string('''
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
                        <label for="brand" class="block text-slate-300 font-bold mb-1">Marca/Fabricante:</label>
                        <input type="text" id="brand" name="brand" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
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
    ''')

@app.route('/admin/edit-miniatura/<int:miniatura_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_miniatura(miniatura_id):
    db = get_db()
    c = db.cursor()
    
    if request.method == 'POST':
        image_url = request.form['image_url']
        name = request.form['name']
        brand = request.form['brand'] # New field
        arrival_date = request.form['arrival_date']
        stock = int(request.form['stock'])
        price = float(request.form['price'])
        observations = request.form['observations']
        max_reservations_per_user = int(request.form['max_reservations_per_user'])

        try:
            c.execute("UPDATE miniaturas SET image_url=?, name=?, brand=?, arrival_date=?, stock=?, price=?, observations=?, max_reservations_per_user=? WHERE id=?",
                      (image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user, miniatura_id))
            db.commit()
            flash('Miniatura atualizada com sucesso!', 'success')
            return redirect(url_for('admin_panel'))
        except Exception as e:
            flash(f'Erro ao atualizar miniatura: {e}', 'error')
        finally:
            pass # db is closed by teardown_appcontext
    
    c.execute('SELECT image_url, name, brand, arrival_date, stock, price, observations, max_reservations_per_user FROM miniaturas WHERE id = ?', (miniatura_id,))
    miniatura = c.fetchone()

    if not miniatura:
        flash('Miniatura não encontrada.', 'error')
        return redirect(url_for('admin_panel'))

    return render_template_string('''
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
                <form method="POST" action="/admin/edit-miniatura/{{ miniatura_id }}" class="space-y-4">
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" value="{{ miniatura.name }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="brand" class="block text-slate-300 font-bold mb-1">Marca/Fabricante:</label>
                        <input type="text" id="brand" name="brand" value="{{ miniatura.brand }}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="image_url" class="block text-slate-300 font-bold mb-1">URL da Imagem:</label>
                        <input type="url" id="image_url" name="image_url" value="{{ miniatura.image_url }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="arrival_date" class="block text-slate-300 font-bold mb-1">Previsão de Chegada:</label>
                        <input type="date" id="arrival_date" name="arrival_date" value="{{ miniatura.arrival_date }}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="stock" class="block text-slate-300 font-bold mb-1">Estoque:</label>
                        <input type="number" id="stock" name="stock" value="{{ miniatura.stock }}" required min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="price" class="block text-slate-300 font-bold mb-1">Preço:</label>
                        <input type="number" id="price" name="price" value="{{ "%.2f"|format(miniatura.price) }}" required step="0.01" min="0" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="observations" class="block text-slate-300 font-bold mb-1">Observações:</label>
                        <textarea id="observations" name="observations" rows="3" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">{{ miniatura.observations }}</textarea>
                    </div>
                    <div>
                        <label for="max_reservations_per_user" class="block text-slate-300 font-bold mb-1">Máx. Reservas por Usuário:</label>
                        <input type="number" id="max_reservations_per_user" name="max_reservations_per_user" value="{{ miniatura.max_reservations_per_user }}" required min="1" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div class="flex gap-4">
                        <a href="/admin" class="flex-1 text-center bg-slate-700 text-white font-bold py-2 rounded-lg hover:bg-slate-600 transition duration-300">Cancelar</a>
                        <button type="submit" class="flex-1 bg-gradient-to-r from-blue-600 to-red-600 text-white font-bold py-2 rounded-lg hover:from-blue-700 hover:to-red-700 transition duration-300">Salvar Alterações</button>
                    </div>
                </form>
            </div>
        </body>
        </html>
    ''', miniatura_id=miniatura_id, miniatura=miniatura)

@app.route('/admin/delete-miniatura/<int:miniatura_id>', methods=['POST'])
@login_required
@admin_required
def delete_miniatura(miniatura_id):
    db = get_db()
    c = db.cursor()
    try:
        c.execute('DELETE FROM miniaturas WHERE id = ?', (miniatura_id,))
        db.commit()
        return {'success': True, 'message': 'Miniatura excluída com sucesso!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

@app.route('/pessoas')
@login_required
@admin_required
def pessoas():
    db = get_db()
    c = db.cursor()
    c.execute('SELECT id, name, email, phone, is_admin FROM users')
    users = c.fetchall()

    return render_template_string('''
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
                            {% for u in users %}
                            <tr class="border-b border-slate-700 hover:bg-slate-700">
                                <td class="px-4 py-3">{{ u.id }}</td>
                                <td class="px-4 py-3">{{ u.name }}</td>
                                <td class="px-4 py-3">{{ u.email }}</td>
                                <td class="px-4 py-3">{{ u.phone }}</td>
                                <td class="px-4 py-3">{{ "Sim" if u.is_admin else "Não" }}</td>
                                <td class="px-4 py-3 flex gap-2">
                                    <a href="/admin/edit-user/{{ u.id }}" class="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded-lg text-sm">Editar</a>
                                    <button onclick="deleteUser({{ u.id }})" class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-sm">Excluir</button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            <script>
                function deleteUser(id) {
                    if (confirm("Tem certeza que deseja excluir este usuário?")) {
                        fetch("/admin/delete-user/" + id, {
                            method: "POST",
                            headers: {"Content-Type": "application/json"}
                        })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                alert("Usuário excluído com sucesso!");
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
            </script>
        </body>
        </html>
    ''', users=users)

@app.route('/admin/edit-user/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    db = get_db()
    c = db.cursor()
    
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        is_admin = 1 if 'is_admin' in request.form else 0

        try:
            c.execute("UPDATE users SET name=?, email=?, phone=?, is_admin=? WHERE id=?",
                      (name, email, phone, is_admin, user_id))
            db.commit()
            flash('Usuário atualizado com sucesso!', 'success')
            return redirect(url_for('pessoas'))
        except Exception as e:
            flash(f'Erro ao atualizar usuário: {e}', 'error')
        finally:
            pass # db is closed by teardown_appcontext
    
    c.execute('SELECT name, email, phone, is_admin FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()

    if not user:
        flash('Usuário não encontrado.', 'error')
        return redirect(url_for('pessoas'))

    is_admin_checked = "checked" if user['is_admin'] else ""

    return render_template_string('''
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
                <form method="POST" action="/admin/edit-user/{{ user_id }}" class="space-y-4">
                    <div>
                        <label for="name" class="block text-slate-300 font-bold mb-1">Nome:</label>
                        <input type="text" id="name" name="name" value="{{ user.name }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="email" class="block text-slate-300 font-bold mb-1">E-mail:</label>
                        <input type="email" id="email" name="email" value="{{ user.email }}" required class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div>
                        <label for="phone" class="block text-slate-300 font-bold mb-1">Telefone:</label>
                        <input type="text" id="phone" name="phone" value="{{ user.phone }}" class="w-full px-4 py-2 rounded-lg bg-slate-700 text-white border-2 border-blue-600 focus:outline-none focus:border-red-500">
                    </div>
                    <div class="flex items-center">
                        <input type="checkbox" id="is_admin" name="is_admin" {{ is_admin_checked }} class="h-5 w-5 text-blue-600 rounded border-gray-300 focus:ring-blue-500">
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
    ''', user_id=user_id, user=user, is_admin_checked=is_admin_checked)

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    db = get_db()
    c = db.cursor()
    try:
        c.execute('DELETE FROM users WHERE id = ?', (user_id,))
        db.commit()
        return {'success': True, 'message': 'Usuário excluído com sucesso!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

@app.route('/lista-espera')
@login_required
@admin_required
def lista_espera():
    db = get_db()
    c = db.cursor()
    c.execute('''
        SELECT
            wl.id, u.name AS user_name, u.email, m.name AS miniatura_name, wl.request_date, wl.notification_sent
        FROM waitlist wl
        JOIN users u ON wl.user_id = u.id
        JOIN miniaturas m ON wl.miniatura_id = m.id
        ORDER BY wl.request_date DESC
    ''')
    waitlist_entries = c.fetchall()

    return render_template_string('''
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
                            {% for entry in waitlist_entries %}
                            {% set notification_status = "Enviada" if entry.notification_sent else "Pendente" %}
                            {% set status_color = "green" if entry.notification_sent else "yellow" %}
                            <tr class="border-b border-slate-700 hover:bg-slate-700">
                                <td class="px-4 py-3">{{ entry.id }}</td>
                                <td class="px-4 py-3">{{ entry.user_name }}</td>
                                <td class="px-4 py-3">{{ entry.email }}</td>
                                <td class="px-4 py-3">{{ entry.miniatura_name }}</td>
                                <td class="px-4 py-3">{{ entry.request_date.split('T')[0] }}</td>
                                <td class="px-4 py-3"><span class="bg-{{ status_color }}-600 text-white px-3 py-1 rounded-full text-sm">{{ notification_status }}</span></td>
                                <td class="px-4 py-3 flex gap-2">
                                    <button onclick="markNotified({{ entry.id }})" class="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded-lg text-sm">Marcar Notificado</button>
                                    <button onclick="deleteWaitlistEntry({{ entry.id }})" class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-sm">Excluir</button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            <script>
                function markNotified(id) {
                    if (confirm("Marcar este item como 'notificação enviada'?")) {
                        fetch("/admin/mark-notified/" + id, {
                            method: "POST",
                            headers: {"Content-Type": "application/json"}
                        })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                alert("Status atualizado!");
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

                function deleteWaitlistEntry(id) {
                    if (confirm("Tem certeza que deseja excluir este item da lista de espera?")) {
                        fetch("/admin/delete-waitlist/" + id, {
                            method: "POST",
                            headers: {"Content-Type": "application/json"}
                        })
                        .then(r => r.json())
                        .then(data => {
                            if (data.success) {
                                alert("Item excluído com sucesso!");
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
            </script>
        </body>
        </html>
    ''', waitlist_entries=waitlist_entries)

@app.route('/admin/mark-notified/<int:entry_id>', methods=['POST'])
@login_required
@admin_required
def mark_notified(entry_id):
    db = get_db()
    c = db.cursor()
    try:
        c.execute('UPDATE waitlist SET notification_sent = 1 WHERE id = ?', (entry_id,))
        db.commit()
        return {'success': True, 'message': 'Status de notificação atualizado!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

@app.route('/admin/delete-waitlist/<int:entry_id>', methods=['POST'])
@login_required
@admin_required
def delete_waitlist(entry_id):
    db = get_db()
    c = db.cursor()
    try:
        c.execute('DELETE FROM waitlist WHERE id = ?', (entry_id,))
        db.commit()
        return {'success': True, 'message': 'Item da lista de espera excluído com sucesso!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

@app.route('/add-to-waitlist', methods=['POST'])
@login_required
def add_to_waitlist():
    data = request.get_json()
    miniatura_id = data.get('miniatura_id')
    user_id = request.user.get('user_id')
    user_email = request.user.get('email')

    if not miniatura_id:
        return {'success': False, 'error': 'ID da miniatura inválido.'}, 400

    db = get_db()
    c = db.cursor()
    try:
        c.execute('SELECT id FROM waitlist WHERE user_id = ? AND miniatura_id = ?', (user_id, miniatura_id))
        if c.fetchone():
            return {'success': False, 'error': 'Você já está na lista de espera para esta miniatura.'}, 400

        c.execute('INSERT INTO waitlist (user_id, miniatura_id, email, request_date) VALUES (?, ?, ?, ?)',
                  (user_id, miniatura_id, user_email, datetime.now().isoformat()))
        db.commit()
        return {'success': True, 'message': 'Adicionado à lista de espera com sucesso!'}
    except Exception as e:
        db.rollback()
        return {'success': False, 'error': str(e)}, 500
    finally:
        pass # db is closed by teardown_appcontext

@app.route('/admin/update-sheet', methods=['POST'])
@login_required
@admin_required
def admin_update_sheet():
    try:
        update_from_google_sheets() # Call the function to update from Google Sheets
        return {'success': True, 'message': 'Dados da planilha atualizados com sucesso!'}
    except Exception as e:
        return {'success': False, 'error': str(e)}, 500

# --- Inicialização ---
# Ensure init_db and load_initial_data are called only once
with app.app_context():
    init_db()
    load_initial_data() # This now calls update_from_google_sheets internally

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
