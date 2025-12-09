import os
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify
import gspread
from google.oauth2.service_account import Credentials
import hashlib
import sqlite3
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_secret_key_for_dev_only')

# --- Configuração Google Sheets ---
# As credenciais são carregadas de variáveis de ambiente do Railway
creds_dict = {
    "type": "service_account",
    "project_id": os.getenv('GOOGLE_PROJECT_ID'),
    "private_key_id": os.getenv('GOOGLE_PRIVATE_KEY_ID'),
    "private_key": os.getenv('GOOGLE_PRIVATE_KEY', '').replace('\\\n', '\n'), # Substitui \\n por \n
    "client_email": os.getenv('GOOGLE_CLIENT_EMAIL'),
    "client_id": os.getenv('GOOGLE_CLIENT_ID'),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": os.getenv('GOOGLE_CLIENT_X509_CERT_URL')
}

gc = None
sheet_id = os.getenv('SHEET_ID')
if sheet_id and all(creds_dict.values()): # Verifica se todas as credenciais e SHEET_ID estão configurados
    try:
        creds = Credentials.from_service_account_info(creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        gc = gspread.authorize(creds)
        print("gspread: Autenticação bem-sucedida.")
    except Exception as e:
        print(f"gspread: Erro na autenticação ou carregamento de credenciais: {e}")
        gc = None
else:
    print("gspread: Variáveis de ambiente para Google Sheets incompletas. Sincronização com Sheets desativada.")

# --- Configuração do Banco de Dados SQLite ---
DATABASE_PATH = os.getenv('DATABASE_PATH', 'jgminis.db')

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row # Permite acessar colunas por nome
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Tabela de Usuários
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            cpf TEXT,
            telefone TEXT,
            data_cadastro TEXT,
            admin BOOLEAN DEFAULT 0
        )
    ''')

    # Tabela de Carros
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS carros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thumbnail_url TEXT,
            modelo TEXT NOT NULL,
            marca TEXT,
            ano TEXT,
            quantidade_disponivel INTEGER,
            preco_diaria REAL,
            observacoes TEXT,
            max_reservas INTEGER
        )
    ''')
    # Adiciona a coluna thumbnail_url se não existir (para compatibilidade)
    try:
        cursor.execute("ALTER TABLE carros ADD COLUMN thumbnail_url TEXT")
        print("DB: Coluna 'thumbnail_url' adicionada à tabela 'carros'.")
    except sqlite3.OperationalError:
        pass # Coluna já existe

    # Tabela de Reservas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            carro_id INTEGER NOT NULL,
            data_reserva TEXT NOT NULL,
            hora_inicio TEXT,
            hora_fim TEXT,
            status TEXT,
            observacoes TEXT,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
            FOREIGN KEY (carro_id) REFERENCES carros(id)
        )
    ''')

    # Cria usuário admin padrão se não existir
    admin_email = "admin@jgminis.com.br"
    admin_senha_hash = hashlib.sha256("admin123".encode()).hexdigest() # SHA256 de 'admin123'
    cursor.execute("SELECT * FROM usuarios WHERE email = ?", (admin_email,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO usuarios (nome, email, senha_hash, admin) VALUES (?, ?, ?, ?)",
                       ('Administrador', admin_email, admin_senha_hash, True))
        print(f"DB: Usuário admin '{admin_email}' criado.")
    else:
        print(f"DB: Usuário admin '{admin_email}' já existe.")

    conn.commit()
    conn.close()

# --- Funções de Sincronização com Sheets ---
def load_from_sheets():
    if not gc or not sheet_id:
        print("load_from_sheets: Cliente gspread não inicializado ou SHEET_ID ausente. Carregando dados apenas do DB local.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        gsheet = gc.open_by_key(sheet_id)

        # --- Carregar Carros ---
        try:
            carros_sheet = gsheet.worksheet('Carros')
            data_carros = carros_sheet.get_all_records()
            cursor.execute("DELETE FROM carros") # Limpa DB para recarregar do Sheets
            for i, row in enumerate(data_carros):
                # Mapeamento das colunas da planilha do usuário
                carro_id = row.get('ID') if row.get('ID') else (i + 1) # Usa ID da planilha ou gera
                thumbnail_url = row.get('IMAGEM', '')
                modelo = row.get('NOME DA MINIATURA', '')
                marca = row.get('MARCA/FABRICANTE', '')
                ano = row.get('PREVISÃO DE CHEGADA', '')
                quantidade_disponivel = int(row.get('QUANTIDADE DISPONIVEL', 0))
                preco_diaria = float(row.get('VALOR', 0))
                observacoes = row.get('OBSERVAÇÕES', '') + (f" (Previsão: {ano})" if ano else "")
                max_reservas = int(row.get('MAX_RESERVAS_POR_USUARIO', 1))

                cursor.execute("INSERT INTO carros (id, thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (carro_id, thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas))
            print(f"load_from_sheets: {len(data_carros)} carros carregados da planilha 'Carros'.")
        except gspread.WorksheetNotFound:
            print("load_from_sheets: Aba 'Carros' não encontrada na planilha. Criando aba e populando DB com dados padrão.")
            carros_sheet = gsheet.add_worksheet('Carros', 1000, 9)
            carros_sheet.append_row(['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO'])
            # O DB de carros ficará vazio até que dados sejam adicionados via admin ou planilha
        except Exception as e:
            print(f"load_from_sheets: Erro ao carregar carros do Sheets: {e}")

        # --- Carregar Usuários ---
        try:
            usuarios_sheet = gsheet.worksheet('Usuarios')
            data_usuarios = usuarios_sheet.get_all_records()
            # Não limpa usuários do DB para preservar o admin padrão se não estiver na planilha
            for i, row in enumerate(data_usuarios):
                usuario_id = row.get('ID') if row.get('ID') else (i + 1)
                email = row.get('Email', '')
                if email and not cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email,)).fetchone():
                    cursor.execute("INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   (usuario_id, row.get('Nome', ''), email, row.get('Senha_hash', ''), row.get('CPF', ''), row.get('Telefone', ''), row.get('Data Cadastro', ''), row.get('Admin', 'Não').lower() == 'sim'))
            print(f"load_from_sheets: {len(data_usuarios)} usuários carregados da planilha 'Usuarios'.")
        except gspread.WorksheetNotFound:
            print("load_from_sheets: Aba 'Usuarios' não encontrada na planilha. Usuários não carregados do Sheets.")
            # A aba será criada automaticamente se dados forem adicionados via admin e sync_to_sheets for chamado
        except Exception as e:
            print(f"load_from_sheets: Erro ao carregar usuários do Sheets: {e}")

        # --- Carregar Reservas ---
        try:
            reservas_sheet = gsheet.worksheet('Reservas')
            data_reservas = reservas_sheet.get_all_records()
            cursor.execute("DELETE FROM reservas") # Limpa DB para recarregar do Sheets
            for i, row in enumerate(data_reservas):
                reserva_id = row.get('ID') if row.get('ID') else (i + 1)
                usuario_id = int(row.get('Usuario_id', 0))
                carro_id = int(row.get('Carro_id', 0))
                data_reserva = row.get('Data_reserva', '')
                hora_inicio = row.get('Hora_inicio', '')
                hora_fim = row.get('Hora_fim', '')
                status = row.get('Status', 'pendente')
                observacoes = row.get('Observacoes', '')

                cursor.execute("INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (reserva_id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes))
            print(f"load_from_sheets: {len(data_reservas)} reservas carregadas da planilha 'Reservas'.")
        except gspread.WorksheetNotFound:
            print("load_from_sheets: Aba 'Reservas' não encontrada na planilha. Reservas não carregadas do Sheets.")
        except Exception as e:
            print(f"load_from_sheets: Erro ao carregar reservas do Sheets: {e}")

    except Exception as e:
        print(f"load_from_sheets: Erro geral ao acessar planilha: {e}")
    finally:
        conn.commit()
        conn.close()

def sync_to_sheets():
    if not gc or not sheet_id:
        print("sync_to_sheets: Cliente gspread não inicializado ou SHEET_ID ausente. Sincronização para Sheets desativada.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        gsheet = gc.open_by_key(sheet_id)

        # --- Sincronizar Carros ---
        carros_db = cursor.execute("SELECT * FROM carros").fetchall()
        try:
            carros_sheet = gsheet.worksheet('Carros')
        except gspread.WorksheetNotFound:
            carros_sheet = gsheet.add_worksheet('Carros', 1000, 9)
        carros_sheet.clear()
        carros_sheet.append_row(['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO'])
        for carro in carros_db:
            carros_sheet.append_row([
                carro['id'], carro['thumbnail_url'], carro['modelo'], carro['marca'], carro['ano'],
                carro['quantidade_disponivel'], carro['preco_diaria'], carro['observacoes'], carro['max_reservas']
            ])
        print(f"sync_to_sheets: {len(carros_db)} carros sincronizados para a planilha 'Carros'.")

        # --- Sincronizar Usuários ---
        usuarios_db = cursor.execute("SELECT * FROM usuarios").fetchall()
        try:
            usuarios_sheet = gsheet.worksheet('Usuarios')
        except gspread.WorksheetNotFound:
            usuarios_sheet = gsheet.add_worksheet('Usuarios', 100, 8)
        usuarios_sheet.clear()
        usuarios_sheet.append_row(['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data Cadastro', 'Admin'])
        for usuario in usuarios_db:
            usuarios_sheet.append_row([
                usuario['id'], usuario['nome'], usuario['email'], usuario['senha_hash'],
                usuario['cpf'], usuario['telefone'], usuario['data_cadastro'], 'Sim' if usuario['admin'] else 'Não'
            ])
        print(f"sync_to_sheets: {len(usuarios_db)} usuários sincronizados para a planilha 'Usuarios'.")

        # --- Sincronizar Reservas ---
        reservas_db = cursor.execute("SELECT * FROM reservas").fetchall()
        try:
            reservas_sheet = gsheet.worksheet('Reservas')
        except gspread.WorksheetNotFound:
            reservas_sheet = gsheet.add_worksheet('Reservas', 1000, 8)
        reservas_sheet.clear()
        reservas_sheet.append_row(['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes'])
        for reserva in reservas_db:
            reservas_sheet.append_row([
                reserva['id'], reserva['usuario_id'], reserva['carro_id'], reserva['data_reserva'],
                reserva['hora_inicio'], reserva['hora_fim'], reserva['status'], reserva['observacoes']
            ])
        print(f"sync_to_sheets: {len(reservas_db)} reservas sincronizadas para a planilha 'Reservas'.")

    except Exception as e:
        print(f"sync_to_sheets: Erro geral ao sincronizar para planilha: {e}")
    finally:
        conn.close()

# --- Inicialização do DB e Carregamento de Dados ---
with app.app_context():
    init_db()
    load_from_sheets()
    print("App bootado com sucesso.")

# --- Rotas da Aplicação ---

# Rota /health
@app.route('/health')
def health():
    return 'OK'

# Rota /login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        senha = request.form.get('senha')
        senha_hash = hashlib.sha256(senha.encode()).hexdigest()

        conn = get_db_connection()
        cursor = conn.cursor()
        user = cursor.execute("SELECT * FROM usuarios WHERE email = ? AND senha_hash = ?", (email, senha_hash)).fetchone()
        conn.close()

        if user:
            session['logged_in'] = True
            session['user_id'] = user['id']
            session['is_admin'] = user['admin']
            return redirect(url_for('home'))
        else:
            return render_template_string('''
                <style>
                    body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                    .login-container { background-color: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }
                    .login-container h2 { color: #333; margin-bottom: 20px; }
                    .login-container input[type="email"], .login-container input[type="password"] { width: calc(100% - 22px); padding: 10px; margin-bottom: 10px; border: 1px solid #ddd; border-radius: 4px; }
                    .login-container input[type="submit"] { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
                    .login-container input[type="submit"]:hover { background-color: #0056b3; }
                    .error-message { color: red; margin-top: 10px; }
                </style>
                <div class="login-container">
                    <h2>Login</h2>
                    <form method="post">
                        <input type="email" name="email" placeholder="Email" required><br>
                        <input type="password" name="senha" placeholder="Senha" required><br>
                        <input type="submit" value="Entrar">
                    </form>
                    <p class="error-message">Login falhou. Verifique seu email e senha.</p>
                </div>
            ''')

    return render_template_string('''
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .login-container { background-color: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }
            .login-container h2 { color: #333; margin-bottom: 20px; }
            .login-container input[type="email"], .login-container input[type="password"] { width: calc(100% - 22px); padding: 10px; margin-bottom: 10px; border: 1px solid #ddd; border-radius: 4px; }
            .login-container input[type="submit"] { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
            .login-container input[type="submit"]:hover { background-color: #0056b3; }
        </style>
        <div class="login-container">
            <h2>Login</h2>
            <form method="post">
                <input type="email" name="email" placeholder="Email" required><br>
                <input type="password" name="senha" placeholder="Senha" required><br>
                <input type="submit" value="Entrar">
            </form>
        </div>
    ''')

# Rota /home
@app.route('/home')
def home():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db_connection()
    carros_db = conn.execute("SELECT * FROM carros").fetchall()
    conn.close()

    html_carros = ""
    if not carros_db:
        html_carros = "<p>Nenhuma miniatura disponível no momento.</p>"
    else:
        for carro in carros_db:
            html_carros += f'''
                <div class="card">
                    <img src="{carro['thumbnail_url']}" alt="{carro['modelo']}">
                    <h3>{carro['modelo']}</h3>
                    <p>Marca: {carro['marca']}</p>
                    <p>Preço Diário: R$ {carro['preco_diaria']:.2f}</p>
                    <p>Disponível: {carro['quantidade_disponivel']}</p>
                    <button onclick="reservar({carro['id']})">Reservar</button>
                </div>
            '''
    
    return render_template_string(f'''
    <html>
    <head>
        <title>JG Minis - Miniaturas</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }}
            .navbar {{ background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
            .navbar a {{ color: white; text-decoration: none; margin-left: 15px; }}
            .navbar a:hover {{ text-decoration: underline; }}
            .container {{ padding: 20px; }}
            h1 {{ color: #333; text-align: center; margin-bottom: 30px; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 20px; }}
            .card {{ background-color: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .card img {{ max-width: 100%; height: 150px; object-fit: cover; border-radius: 4px; margin-bottom: 10px; }}
            .card h3 {{ margin: 10px 0; color: #007bff; }}
            .card p {{ margin: 5px 0; color: #555; font-size: 0.9em; }}
            .card button {{ background: #28a745; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-size: 1em; }}
            .card button:hover {{ background-color: #218838; }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <span>Bem-vindo, {session.get('user_id')}!</span>
            <div>
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('admin')}">Admin</a>
                <a href="{url_for('logout')}">Sair</a>
            </div>
        </div>
        <div class="container">
            <h1>JG Minis - Miniaturas Disponíveis</h1>
            <div class="grid">
                {html_carros}
            </div>
        </div>
        <script>
            function reservar(carroId) {{
                alert('Reserva para carro ' + carroId + ' solicitada. Funcionalidade de reserva completa será implementada.');
                // Redirecionar para uma rota de reserva mais complexa
            }}
        </script>
    </body>
    </html>
    ''')

# Rota /admin
@app.route('/admin')
def admin():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))

    conn = get_db_connection()
    carros_db = conn.execute("SELECT * FROM carros").fetchall()
    usuarios_db = conn.execute("SELECT * FROM usuarios").fetchall()
    reservas_db = conn.execute("SELECT * FROM reservas").fetchall()
    conn.close()

    html_carros = ""
    for carro in carros_db:
        html_carros += f'<tr><td>{carro["id"]}</td><td><img src="{carro["thumbnail_url"]}" width="50"></td><td>{carro["modelo"]}</td><td>{carro["marca"]}</td><td>{carro["preco_diaria"]:.2f}</td><td><a href="/admin/edit_carro/{carro["id"]}">Editar</a> <a href="/admin/delete_carro/{carro["id"]}">Deletar</a></td></tr>'

    html_usuarios = ""
    for usuario in usuarios_db:
        html_usuarios += f'<tr><td>{usuario["id"]}</td><td>{usuario["nome"]}</td><td>{usuario["email"]}</td><td>{"Sim" if usuario["admin"] else "Não"}</td><td><a href="/admin/edit_usuario/{usuario["id"]}">Editar</a> <a href="/admin/delete_usuario/{usuario["id"]}">Deletar</a></td></tr>'

    html_reservas = ""
    for reserva in reservas_db:
        html_reservas += f'<tr><td>{reserva["id"]}</td><td>{reserva["usuario_id"]}</td><td>{reserva["carro_id"]}</td><td>{reserva["data_reserva"]}</td><td>{reserva["status"]}</td><td><a href="/admin/edit_reserva/{reserva["id"]}">Editar</a> <a href="/admin/delete_reserva/{reserva["id"]}">Deletar</a></td></tr>'

    return render_template_string(f'''
    <html>
    <head>
        <title>Admin Panel</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }}
            .navbar {{ background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
            .navbar a {{ color: white; text-decoration: none; margin-left: 15px; }}
            .navbar a:hover {{ text-decoration: underline; }}
            .container {{ padding: 20px; }}
            h1, h2 {{ color: #333; margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; background-color: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #e9ecef; }}
            .btn {{ background-color: #007bff; color: white; padding: 8px 12px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; margin-right: 5px; }}
            .btn-success {{ background-color: #28a745; }}
            .btn-danger {{ background-color: #dc3545; }}
            .btn:hover {{ opacity: 0.9; }}
            .form-group {{ margin-bottom: 15px; }}
            .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            .form-group input[type="text"], .form-group input[type="number"], .form-group input[type="email"], .form-group input[type="password"] {{ width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <span>Admin Panel</span>
            <div>
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('logout')}">Sair</a>
            </div>
        </div>
        <div class="container">
            <h1>Painel Administrativo</h1>

            <h2>Carros</h2>
            <a href="{url_for('add_carro')}" class="btn btn-success">Adicionar Carro</a>
            <a href="{url_for('sync_sheets')}" class="btn">Sincronizar com Sheets</a>
            <table>
                <thead>
                    <tr><th>ID</th><th>Thumbnail</th><th>Modelo</th><th>Marca</th><th>Preço Diária</th><th>Ações</th></tr>
                </thead>
                <tbody>
                    {html_carros}
                </tbody>
            </table>

            <h2>Usuários</h2>
            <a href="{url_for('add_usuario')}" class="btn btn-success">Adicionar Usuário</a>
            <table>
                <thead>
                    <tr><th>ID</th><th>Nome</th><th>Email</th><th>Admin</th><th>Ações</th></tr>
                </thead>
                <tbody>
                    {html_usuarios}
                </tbody>
            </table>

            <h2>Reservas</h2>
            <a href="{url_for('add_reserva')}" class="btn btn-success">Adicionar Reserva</a>
            <table>
                <thead>
                    <tr><th>ID</th><th>Usuário ID</th><th>Carro ID</th><th>Data Reserva</th><th>Status</th><th>Ações</th></tr>
                </thead>
                <tbody>
                    {html_reservas}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    ''')

# Rota para Logout
@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('user_id', None)
    session.pop('is_admin', None)
    return redirect(url_for('login'))

# Rota /admin/sync_sheets
@app.route('/admin/sync_sheets')
def sync_sheets():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    load_from_sheets() # Recarrega do Sheets para o DB
    sync_to_sheets()   # Sincroniza do DB para o Sheets
    return render_template_string('''
        <script>
            alert('Sincronização com Sheets concluída.');
            window.location.href = '/admin';
        </script>
    ''')

# --- Rotas CRUD para Carros ---
@app.route('/admin/add_carro', methods=['GET', 'POST'])
def add_carro():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    if request.method == 'POST':
        thumbnail_url = request.form.get('thumbnail_url')
        modelo = request.form.get('modelo')
        marca = request.form.get('marca')
        ano = request.form.get('ano')
        quantidade_disponivel = int(request.form.get('quantidade_disponivel', 0))
        preco_diaria = float(request.form.get('preco_diaria', 0.0))
        observacoes = request.form.get('observacoes')
        max_reservas = int(request.form.get('max_reservas', 1))

        cursor = conn.cursor()
        cursor.execute("INSERT INTO carros (thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas))
        conn.commit()
        conn.close()
        return redirect(url_for('sync_sheets')) # Sincroniza após adicionar
    
    conn.close()
    return render_template_string('''
    <html>
    <head>
        <title>Adicionar Carro</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }
            .navbar { background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }
            .navbar a { color: white; text-decoration: none; margin-left: 15px; }
            .navbar a:hover { text-decoration: underline; }
            .container { padding: 20px; background-color: white; margin: 20px auto; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }
            h1 { color: #333; margin-bottom: 20px; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input[type="text"], .form-group input[type="number"] { width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
            .btn { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
            .btn:hover { background-color: #0056b3; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <span>Adicionar Carro</span>
            <div>
                <a href="/admin">Voltar ao Admin</a>
                <a href="/logout">Sair</a>
            </div>
        </div>
        <div class="container">
            <h1>Adicionar Nova Miniatura</h1>
            <form method="post">
                <div class="form-group">
                    <label for="thumbnail_url">URL da Imagem (Thumbnail):</label>
                    <input type="text" id="thumbnail_url" name="thumbnail_url" required>
                </div>
                <div class="form-group">
                    <label for="modelo">Nome da Miniatura (Modelo):</label>
                    <input type="text" id="modelo" name="modelo" required>
                </div>
                <div class="form-group">
                    <label for="marca">Marca/Fabricante:</label>
                    <input type="text" id="marca" name="marca">
                </div>
                <div class="form-group">
                    <label for="ano">Previsão de Chegada (Ano/Data):</label>
                    <input type="text" id="ano" name="ano">
                </div>
                <div class="form-group">
                    <label for="quantidade_disponivel">Quantidade Disponível:</label>
                    <input type="number" id="quantidade_disponivel" name="quantidade_disponivel" value="0" required>
                </div>
                <div class="form-group">
                    <label for="preco_diaria">Valor (Preço):</label>
                    <input type="number" step="0.01" id="preco_diaria" name="preco_diaria" value="0.00" required>
                </div>
                <div class="form-group">
                    <label for="observacoes">Observações:</label>
                    <input type="text" id="observacoes" name="observacoes">
                </div>
                <div class="form-group">
                    <label for="max_reservas">Máximo de Reservas por Usuário:</label>
                    <input type="number" id="max_reservas" name="max_reservas" value="1" required>
                </div>
                <button type="submit" class="btn">Adicionar Miniatura</button>
            </form>
        </div>
    </body>
    </html>
    ''')

@app.route('/admin/edit_carro/<int:carro_id>', methods=['GET', 'POST'])
def edit_carro(carro_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    carro = conn.execute("SELECT * FROM carros WHERE id = ?", (carro_id,)).fetchone()

    if request.method == 'POST':
        thumbnail_url = request.form.get('thumbnail_url')
        modelo = request.form.get('modelo')
        marca = request.form.get('marca')
        ano = request.form.get('ano')
        quantidade_disponivel = int(request.form.get('quantidade_disponivel', 0))
        preco_diaria = float(request.form.get('preco_diaria', 0.0))
        observacoes = request.form.get('observacoes')
        max_reservas = int(request.form.get('max_reservas', 1))

        conn.execute("UPDATE carros SET thumbnail_url=?, modelo=?, marca=?, ano=?, quantidade_disponivel=?, preco_diaria=?, observacoes=?, max_reservas=? WHERE id=?",
                       (thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas, carro_id))
        conn.commit()
        conn.close()
        return redirect(url_for('sync_sheets'))
    
    conn.close()
    if carro:
        return render_template_string(f'''
        <html>
        <head>
            <title>Editar Carro</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }}
                .navbar {{ background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
                .navbar a {{ color: white; text-decoration: none; margin-left: 15px; }}
                .navbar a:hover {{ text-decoration: underline; }}
                .container {{ padding: 20px; background-color: white; margin: 20px auto; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }}
                h1 {{ color: #333; margin-bottom: 20px; }}
                .form-group {{ margin-bottom: 15px; }}
                .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                .form-group input[type="text"], .form-group input[type="number"] {{ width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
                .btn {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
                .btn:hover {{ background-color: #0056b3; }}
            </style>
        </head>
        <body>
            <div class="navbar">
                <span>Editar Carro</span>
                <div>
                    <a href="/admin">Voltar ao Admin</a>
                    <a href="/logout">Sair</a>
                </div>
            </div>
            <div class="container">
                <h1>Editar Miniatura: {carro['modelo']}</h1>
                <form method="post">
                    <div class="form-group">
                        <label for="thumbnail_url">URL da Imagem (Thumbnail):</label>
                        <input type="text" id="thumbnail_url" name="thumbnail_url" value="{carro['thumbnail_url']}" required>
                    </div>
                    <div class="form-group">
                        <label for="modelo">Nome da Miniatura (Modelo):</label>
                        <input type="text" id="modelo" name="modelo" value="{carro['modelo']}" required>
                    </div>
                    <div class="form-group">
                        <label for="marca">Marca/Fabricante:</label>
                        <input type="text" id="marca" name="marca" value="{carro['marca']}">
                    </div>
                    <div class="form-group">
                        <label for="ano">Previsão de Chegada (Ano/Data):</label>
                        <input type="text" id="ano" name="ano" value="{carro['ano']}">
                    </div>
                    <div class="form-group">
                        <label for="quantidade_disponivel">Quantidade Disponível:</label>
                        <input type="number" id="quantidade_disponivel" name="quantidade_disponivel" value="{carro['quantidade_disponivel']}" required>
                    </div>
                    <div class="form-group">
                        <label for="preco_diaria">Valor (Preço):</label>
                        <input type="number" step="0.01" id="preco_diaria" name="preco_diaria" value="{carro['preco_diaria']}" required>
                    </div>
                    <div class="form-group">
                        <label for="observacoes">Observações:</label>
                        <input type="text" id="observacoes" name="observacoes" value="{carro['observacoes']}">
                    </div>
                    <div class="form-group">
                        <label for="max_reservas">Máximo de Reservas por Usuário:</label>
                        <input type="number" id="max_reservas" name="max_reservas" value="{carro['max_reservas']}" required>
                    </div>
                    <button type="submit" class="btn">Salvar Alterações</button>
                </form>
            </div>
        </body>
        </html>
        ''')
    return "Carro não encontrado", 404

@app.route('/admin/delete_carro/<int:carro_id>')
def delete_carro(carro_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute("DELETE FROM carros WHERE id = ?", (carro_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('sync_sheets'))

# --- Rotas CRUD para Usuários (Simplificadas) ---
@app.route('/admin/add_usuario', methods=['GET', 'POST'])
def add_usuario():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    if request.method == 'POST':
        nome = request.form.get('nome')
        email = request.form.get('email')
        senha = request.form.get('senha')
        senha_hash = hashlib.sha256(senha.encode()).hexdigest()
        cpf = request.form.get('cpf')
        telefone = request.form.get('telefone')
        admin = True if request.form.get('admin') == 'on' else False
        data_cadastro = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        cursor = conn.cursor()
        cursor.execute("INSERT INTO usuarios (nome, email, senha_hash, cpf, telefone, data_cadastro, admin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (nome, email, senha_hash, cpf, telefone, data_cadastro, admin))
        conn.commit()
        conn.close()
        return redirect(url_for('sync_sheets'))
    
    conn.close()
    return render_template_string('''
    <html>
    <head>
        <title>Adicionar Usuário</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }
            .navbar { background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }
            .navbar a { color: white; text-decoration: none; margin-left: 15px; }
            .navbar a:hover { text-decoration: underline; }
            .container { padding: 20px; background-color: white; margin: 20px auto; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }
            h1 { color: #333; margin-bottom: 20px; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input[type="text"], .form-group input[type="email"], .form-group input[type="password"] { width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
            .form-group input[type="checkbox"] { margin-right: 5px; }
            .btn { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
            .btn:hover { background-color: #0056b3; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <span>Adicionar Usuário</span>
            <div>
                <a href="/admin">Voltar ao Admin</a>
                <a href="/logout">Sair</a>
            </div>
        </div>
        <div class="container">
            <h1>Adicionar Novo Usuário</h1>
            <form method="post">
                <div class="form-group">
                    <label for="nome">Nome:</label>
                    <input type="text" id="nome" name="nome" required>
                </div>
                <div class="form-group">
                    <label for="email">Email:</label>
                    <input type="email" id="email" name="email" required>
                </div>
                <div class="form-group">
                    <label for="senha">Senha:</label>
                    <input type="password" id="senha" name="senha" required>
                </div>
                <div class="form-group">
                    <label for="cpf">CPF:</label>
                    <input type="text" id="cpf" name="cpf">
                </div>
                <div class="form-group">
                    <label for="telefone">Telefone:</label>
                    <input type="text" id="telefone" name="telefone">
                </div>
                <div class="form-group">
                    <input type="checkbox" id="admin" name="admin">
                    <label for="admin">Administrador</label>
                </div>
                <button type="submit" class="btn">Adicionar Usuário</button>
            </form>
        </div>
    </body>
    </html>
    ''')

@app.route('/admin/edit_usuario/<int:usuario_id>', methods=['GET', 'POST'])
def edit_usuario(usuario_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()

    if request.method == 'POST':
        nome = request.form.get('nome')
        email = request.form.get('email')
        senha = request.form.get('senha')
        cpf = request.form.get('cpf')
        telefone = request.form.get('telefone')
        admin = True if request.form.get('admin') == 'on' else False
        
        update_query = "UPDATE usuarios SET nome=?, email=?, cpf=?, telefone=?, admin=? WHERE id=?"
        update_params = [nome, email, cpf, telefone, admin, usuario_id]

        if senha: # Atualiza senha apenas se fornecida
            senha_hash = hashlib.sha256(senha.encode()).hexdigest()
            update_query = "UPDATE usuarios SET nome=?, email=?, senha_hash=?, cpf=?, telefone=?, admin=? WHERE id=?"
            update_params = [nome, email, senha_hash, cpf, telefone, admin, usuario_id]

        conn.execute(update_query, tuple(update_params))
        conn.commit()
        conn.close()
        return redirect(url_for('sync_sheets'))
    
    conn.close()
    if usuario:
        return render_template_string(f'''
        <html>
        <head>
            <title>Editar Usuário</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }}
                .navbar {{ background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
                .navbar a {{ color: white; text-decoration: none; margin-left: 15px; }}
                .navbar a:hover {{ text-decoration: underline; }}
                .container {{ padding: 20px; background-color: white; margin: 20px auto; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }}
                h1 {{ color: #333; margin-bottom: 20px; }}
                .form-group {{ margin-bottom: 15px; }}
                .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                .form-group input[type="text"], .form-group input[type="email"], .form-group input[type="password"] {{ width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
                .form-group input[type="checkbox"] {{ margin-right: 5px; }}
                .btn {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
                .btn:hover {{ background-color: #0056b3; }}
            </style>
        </head>
        <body>
            <div class="navbar">
                <span>Editar Usuário</span>
                <div>
                    <a href="/admin">Voltar ao Admin</a>
                    <a href="/logout">Sair</a>
                </div>
            </div>
            <div class="container">
                <h1>Editar Usuário: {usuario['nome']}</h1>
                <form method="post">
                    <div class="form-group">
                        <label for="nome">Nome:</label>
                        <input type="text" id="nome" name="nome" value="{usuario['nome']}" required>
                    </div>
                    <div class="form-group">
                        <label for="email">Email:</label>
                        <input type="email" id="email" name="email" value="{usuario['email']}" required>
                    </div>
                    <div class="form-group">
                        <label for="senha">Nova Senha (deixe em branco para não alterar):</label>
                        <input type="password" id="senha" name="senha">
                    </div>
                    <div class="form-group">
                        <label for="cpf">CPF:</label>
                        <input type="text" id="cpf" name="cpf" value="{usuario['cpf'] or ''}">
                    </div>
                    <div class="form-group">
                        <label for="telefone">Telefone:</label>
                        <input type="text" id="telefone" name="telefone" value="{usuario['telefone'] or ''}">
                    </div>
                    <div class="form-group">
                        <input type="checkbox" id="admin" name="admin" {'checked' if usuario['admin'] else ''}>
                        <label for="admin">Administrador</label>
                    </div>
                    <button type="submit" class="btn">Salvar Alterações</button>
                </form>
            </div>
        </body>
        </html>
        ''')
    return "Usuário não encontrado", 404

@app.route('/admin/delete_usuario/<int:usuario_id>')
def delete_usuario(usuario_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('sync_sheets'))

# --- Rotas CRUD para Reservas (Simplificadas) ---
@app.route('/admin/add_reserva', methods=['GET', 'POST'])
def add_reserva():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    if request.method == 'POST':
        usuario_id = int(request.form.get('usuario_id', 0))
        carro_id = int(request.form.get('carro_id', 0))
        data_reserva = request.form.get('data_reserva')
        hora_inicio = request.form.get('hora_inicio')
        hora_fim = request.form.get('hora_fim')
        status = request.form.get('status', 'pendente')
        observacoes = request.form.get('observacoes')

        cursor = conn.cursor()
        cursor.execute("INSERT INTO reservas (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes))
        conn.commit()
        conn.close()
        return redirect(url_for('sync_sheets'))
    
    usuarios_db = conn.execute("SELECT id, nome FROM usuarios").fetchall()
    carros_db = conn.execute("SELECT id, modelo FROM carros").fetchall()
    conn.close()

    usuarios_options = "".join([f"<option value='{u['id']}'>{u['nome']} (ID: {u['id']})</option>" for u in usuarios_db])
    carros_options = "".join([f"<option value='{c['id']}'>{c['modelo']} (ID: {c['id']})</option>" for c in carros_db])

    return render_template_string(f'''
    <html>
    <head>
        <title>Adicionar Reserva</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }}
            .navbar {{ background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
            .navbar a {{ color: white; text-decoration: none; margin-left: 15px; }}
            .navbar a:hover {{ text-decoration: underline; }}
            .container {{ padding: 20px; background-color: white; margin: 20px auto; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }}
            h1 {{ color: #333; margin-bottom: 20px; }}
            .form-group {{ margin-bottom: 15px; }}
            .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            .form-group input[type="text"], .form-group input[type="date"], .form-group input[type="time"], .form-group select {{ width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
            .btn {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            .btn:hover {{ background-color: #0056b3; }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <span>Adicionar Reserva</span>
            <div>
                <a href="/admin">Voltar ao Admin</a>
                <a href="/logout">Sair</a>
            </div>
        </div>
        <div class="container">
            <h1>Adicionar Nova Reserva</h1>
            <form method="post">
                <div class="form-group">
                    <label for="usuario_id">Usuário:</label>
                    <select id="usuario_id" name="usuario_id" required>
                        {usuarios_options}
                    </select>
                </div>
                <div class="form-group">
                    <label for="carro_id">Miniatura (Carro):</label>
                    <select id="carro_id" name="carro_id" required>
                        {carros_options}
                    </select>
                </div>
                <div class="form-group">
                    <label for="data_reserva">Data da Reserva:</label>
                    <input type="date" id="data_reserva" name="data_reserva" required>
                </div>
                <div class="form-group">
                    <label for="hora_inicio">Hora Início:</label>
                    <input type="time" id="hora_inicio" name="hora_inicio">
                </div>
                <div class="form-group">
                    <label for="hora_fim">Hora Fim:</label>
                    <input type="time" id="hora_fim" name="hora_fim">
                </div>
                <div class="form-group">
                    <label for="status">Status:</label>
                    <select id="status" name="status">
                        <option value="pendente">Pendente</option>
                        <option value="confirmada">Confirmada</option>
                        <option value="cancelada">Cancelada</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="observacoes">Observações:</label>
                    <input type="text" id="observacoes" name="observacoes">
                </div>
                <button type="submit" class="btn">Adicionar Reserva</button>
            </form>
        </div>
    </body>
    </html>
    ''')

@app.route('/admin/edit_reserva/<int:reserva_id>', methods=['GET', 'POST'])
def edit_reserva(reserva_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    reserva = conn.execute("SELECT * FROM reservas WHERE id = ?", (reserva_id,)).fetchone()
    usuarios_db = conn.execute("SELECT id, nome FROM usuarios").fetchall()
    carros_db = conn.execute("SELECT id, modelo FROM carros").fetchall()

    if request.method == 'POST':
        usuario_id = int(request.form.get('usuario_id', 0))
        carro_id = int(request.form.get('carro_id', 0))
        data_reserva = request.form.get('data_reserva')
        hora_inicio = request.form.get('hora_inicio')
        hora_fim = request.form.get('hora_fim')
        status = request.form.get('status', 'pendente')
        observacoes = request.form.get('observacoes')

        conn.execute("UPDATE reservas SET usuario_id=?, carro_id=?, data_reserva=?, hora_inicio=?, hora_fim=?, status=?, observacoes=? WHERE id=?",
                       (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes, reserva_id))
        conn.commit()
        conn.close()
        return redirect(url_for('sync_sheets'))
    
    conn.close()
    if reserva:
        usuarios_options = "".join([f"<option value='{u['id']}' {'selected' if u['id'] == reserva['usuario_id'] else ''}>{u['nome']} (ID: {u['id']})</option>" for u in usuarios_db])
        carros_options = "".join([f"<option value='{c['id']}' {'selected' if c['id'] == reserva['carro_id'] else ''}>{c['modelo']} (ID: {c['id']})</option>" for c in carros_db])

        return render_template_string(f'''
        <html>
        <head>
            <title>Editar Reserva</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; background-color: #f8f9fa; }}
                .navbar {{ background-color: #343a40; color: white; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
                .navbar a {{ color: white; text-decoration: none; margin-left: 15px; }}
                .navbar a:hover {{ text-decoration: underline; }}
                .container {{ padding: 20px; background-color: white; margin: 20px auto; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }}
                h1 {{ color: #333; margin-bottom: 20px; }}
                .form-group {{ margin-bottom: 15px; }}
                .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                .form-group input[type="text"], .form-group input[type="date"], .form-group input[type="time"], .form-group select {{ width: calc(100% - 22px); padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
                .btn {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
                .btn:hover {{ background-color: #0056b3; }}
            </style>
        </head>
        <body>
            <div class="navbar">
                <span>Editar Reserva</span>
                <div>
                    <a href="/admin">Voltar ao Admin</a>
                    <a href="/logout">Sair</a>
                </div>
            </div>
            <div class="container">
                <h1>Editar Reserva ID: {reserva['id']}</h1>
                <form method="post">
                    <div class="form-group">
                        <label for="usuario_id">Usuário:</label>
                        <select id="usuario_id" name="usuario_id" required>
                            {usuarios_options}
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="carro_id">Miniatura (Carro):</label>
                        <select id="carro_id" name="carro_id" required>
                            {carros_options}
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="data_reserva">Data da Reserva:</label>
                        <input type="date" id="data_reserva" name="data_reserva" value="{reserva['data_reserva']}" required>
                    </div>
                    <div class="form-group">
                        <label for="hora_inicio">Hora Início:</label>
                        <input type="time" id="hora_inicio" name="hora_inicio" value="{reserva['hora_inicio'] or ''}">
                    </div>
                    <div class="form-group">
                        <label for="hora_fim">Hora Fim:</label>
                        <input type="time" id="hora_fim" name="hora_fim" value="{reserva['hora_fim'] or ''}">
                    </div>
                    <div class="form-group">
                        <label for="status">Status:</label>
                        <select id="status" name="status">
                            <option value="pendente" {'selected' if reserva['status'] == 'pendente' else ''}>Pendente</option>
                            <option value="confirmada" {'selected' if reserva['status'] == 'confirmada' else ''}>Confirmada</option>
                            <option value="cancelada" {'selected' if reserva['status'] == 'cancelada' else ''}>Cancelada</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="observacoes">Observações:</label>
                        <input type="text" id="observacoes" name="observacoes" value="{reserva['observacoes'] or ''}">
                    </div>
                    <button type="submit" class="btn">Salvar Alterações</button>
                </form>
            </div>
        </body>
        </html>
        ''')
    return "Reserva não encontrada", 404

@app.route('/admin/delete_reserva/<int:reserva_id>')
def delete_reserva(reserva_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute("DELETE FROM reservas WHERE id = ?", (reserva_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('sync_sheets'))

if __name__ == '__main__':
    app.run(debug=True)
