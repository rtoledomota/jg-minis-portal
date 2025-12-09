import os
import json
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify
import gspread
from google.oauth2.service_account import Credentials
import hashlib
import sqlite3
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_secret_key_for_dev')

# --- Configuração Google Sheets ---
gc = None
sheet = None
sheet_id = os.getenv('GOOGLE_SHEET_ID')

# Tenta carregar credenciais e autorizar gspread
try:
    creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if creds_json_str:
        creds_dict = json.loads(creds_json_str)
        
        # Handling para private_key com newlines escapados
        private_key = creds_dict.get('private_key')
        if private_key:
            creds_dict['private_key'] = private_key.replace('\\n', '\n')

        creds = Credentials.from_service_account_info(creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        gc = gspread.authorize(creds)
        
        if sheet_id and gc:
            sheet = gc.open_by_key(sheet_id)
            print("INFO - gspread: Autenticação e conexão com planilha bem-sucedidas.")
        else:
            print("ERROR - gspread: GOOGLE_SHEET_ID ou gc não configurado. Sincronização com Sheets desativada.")
    else:
        print("ERROR - gspread: GOOGLE_CREDENTIALS_JSON não configurado. Sincronização com Sheets desativada.")
except Exception as e:
    print(f"ERROR - Erro na configuração do Google Sheets: {e}. Sincronização com Sheets desativada.")
    gc = None
    sheet = None

# --- Configuração do Banco de Dados SQLite ---
DATABASE_PATH = os.getenv('DATABASE_PATH', 'jgminis.db')

# Dados em memória (sincronizados com Sheets ou DB local)
carros = []
usuarios = []
reservas = []

# --- Funções de Manipulação do DB Local ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
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
            is_admin INTEGER DEFAULT 0
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
    # Adiciona coluna thumbnail_url se não existir (para compatibilidade)
    try:
        cursor.execute("ALTER TABLE carros ADD COLUMN thumbnail_url TEXT")
        print("INFO - Coluna 'thumbnail_url' adicionada à tabela 'carros'.")
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
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id),
            FOREIGN KEY (carro_id) REFERENCES carros (id)
        )
    ''')

    # Adiciona usuário admin padrão se não existir
    admin_email = 'admin@jgminis.com.br'
    admin_senha_hash = hashlib.sha256('admin123'.encode()).hexdigest() # SHA256 de 'admin123'
    cursor.execute("SELECT id FROM usuarios WHERE email = ?", (admin_email,))
    if cursor.fetchone() is None:
        cursor.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, is_admin, data_cadastro) VALUES (?, ?, ?, ?, ?)",
            ('Admin', admin_email, admin_senha_hash, 1, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        print(f"INFO - Usuário admin '{admin_email}' criado no DB local.")
    else:
        print(f"INFO - Usuário admin '{admin_email}' já existe no DB local.")

    conn.commit()
    conn.close()
    print("INFO - DB inicializado com sucesso.")

# --- Funções de Sincronização com Google Sheets ---
def load_data_from_sheets():
    global carros, usuarios, reservas
    if not sheet:
        print("WARNING - Cliente gspread não inicializado. Carregando dados apenas do DB local.")
        return False

    try:
        # Carregar aba 'Carros'
        try:
            carros_sheet = sheet.worksheet('Carros')
        except gspread.WorksheetNotFound:
            print("WARNING - Aba 'Carros' não encontrada. Criando nova aba 'Carros'.")
            carros_sheet = sheet.add_worksheet('Carros', rows=1000, cols=10)
            carros_sheet.append_row(['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO'])
            carros_sheet.format('A1:I1', {'textFormat': {'bold': True}}) # Formata cabeçalho
            print("INFO - Aba 'Carros' criada com cabeçalhos padrão.")
            return False # Recarregar após criação

        data_carros = carros_sheet.get_all_records()
        carros_temp = []
        for i, row in enumerate(data_carros):
            # Mapeamento exato das colunas da planilha do usuário
            carro = {
                'id': int(row.get('ID', i + 1)), # Gera ID se não existir
                'thumbnail_url': row.get('IMAGEM', ''),
                'modelo': row.get('NOME DA MINIATURA', ''),
                'marca': row.get('MARCA/FABRICANTE', ''),
                'ano': row.get('PREVISÃO DE CHEGADA', ''),
                'quantidade_disponivel': int(row.get('QUANTIDADE DISPONIVEL', 0)),
                'preco_diaria': float(row.get('VALOR', 0.0)),
                'observacoes': row.get('OBSERVAÇÕES', ''),
                'max_reservas': int(row.get('MAX_RESERVAS_POR_USUARIO', 1))
            }
            carros_temp.append(carro)
        carros = carros_temp
        print(f"INFO - Dados carregados da planilha 'Carros': {len(carros)} itens.")

        # Carregar aba 'Usuarios'
        try:
            usuarios_sheet = sheet.worksheet('Usuarios')
        except gspread.WorksheetNotFound:
            print("WARNING - Aba 'Usuarios' não encontrada. Criando nova aba 'Usuarios'.")
            usuarios_sheet = sheet.add_worksheet('Usuarios', rows=100, cols=8)
            usuarios_sheet.append_row(['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data_Cadastro', 'Is_Admin'])
            usuarios_sheet.format('A1:H1', {'textFormat': {'bold': True}})
            print("INFO - Aba 'Usuarios' criada com cabeçalhos padrão.")
            return False # Recarregar após criação

        data_usuarios = usuarios_sheet.get_all_records()
        usuarios_temp = []
        for i, row in enumerate(data_usuarios):
            usuario = {
                'id': int(row.get('ID', i + 1)),
                'nome': row.get('Nome', ''),
                'email': row.get('Email', ''),
                'senha_hash': row.get('Senha_hash', ''),
                'cpf': row.get('CPF', ''),
                'telefone': row.get('Telefone', ''),
                'data_cadastro': row.get('Data_Cadastro', ''),
                'is_admin': int(row.get('Is_Admin', 0))
            }
            usuarios_temp.append(usuario)
        usuarios = usuarios_temp
        print(f"INFO - Dados carregados da planilha 'Usuarios': {len(usuarios)} itens.")

        # Carregar aba 'Reservas'
        try:
            reservas_sheet = sheet.worksheet('Reservas')
        except gspread.WorksheetNotFound:
            print("WARNING - Aba 'Reservas' não encontrada. Criando nova aba 'Reservas'.")
            reservas_sheet = sheet.add_worksheet('Reservas', rows=1000, cols=8)
            reservas_sheet.append_row(['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes'])
            reservas_sheet.format('A1:H1', {'textFormat': {'bold': True}})
            print("INFO - Aba 'Reservas' criada com cabeçalhos padrão.")
            return False # Recarregar após criação

        data_reservas = reservas_sheet.get_all_records()
        reservas_temp = []
        for i, row in enumerate(data_reservas):
            reserva = {
                'id': int(row.get('ID', i + 1)),
                'usuario_id': int(row.get('Usuario_id', 0)),
                'carro_id': int(row.get('Carro_id', 0)),
                'data_reserva': row.get('Data_reserva', ''),
                'hora_inicio': row.get('Hora_inicio', ''),
                'hora_fim': row.get('Hora_fim', ''),
                'status': row.get('Status', 'pendente'),
                'observacoes': row.get('Observacoes', '')
            }
            reservas_temp.append(reserva)
        reservas = reservas_temp
        print(f"INFO - Dados carregados da planilha 'Reservas': {len(reservas)} itens.")
        return True # Sucesso no carregamento

    except Exception as e:
        print(f"ERROR - Erro ao carregar dados do Sheets: {e}. Carregando dados apenas do DB local.")
        return False

def sync_data_to_sheets():
    if not sheet:
        print("WARNING - Cliente gspread não inicializado. Sincronização para Sheets desativada.")
        return

    try:
        # Sincronizar Carros
        carros_sheet = sheet.worksheet('Carros')
        carros_sheet.clear()
        carros_sheet.append_row(['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO'])
        for carro in carros:
            carros_sheet.append_row([
                carro.get('id', ''),
                carro.get('thumbnail_url', ''),
                carro.get('modelo', ''),
                carro.get('marca', ''),
                carro.get('ano', ''),
                carro.get('quantidade_disponivel', 0),
                carro.get('preco_diaria', 0.0),
                carro.get('observacoes', ''),
                carro.get('max_reservas', 1)
            ])
        
        # Sincronizar Usuarios
        usuarios_sheet = sheet.worksheet('Usuarios')
        usuarios_sheet.clear()
        usuarios_sheet.append_row(['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data_Cadastro', 'Is_Admin'])
        for usuario in usuarios:
            usuarios_sheet.append_row([
                usuario.get('id', ''),
                usuario.get('nome', ''),
                usuario.get('email', ''),
                usuario.get('senha_hash', ''),
                usuario.get('cpf', ''),
                usuario.get('telefone', ''),
                usuario.get('data_cadastro', ''),
                usuario.get('is_admin', 0)
            ])
        
        # Sincronizar Reservas
        reservas_sheet = sheet.worksheet('Reservas')
        reservas_sheet.clear()
        reservas_sheet.append_row(['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes'])
        for reserva in reservas:
            reservas_sheet.append_row([
                reserva.get('id', ''),
                reserva.get('usuario_id', ''),
                reserva.get('carro_id', ''),
                reserva.get('data_reserva', ''),
                reserva.get('hora_inicio', ''),
                reserva.get('hora_fim', ''),
                reserva.get('status', ''),
                reserva.get('observacoes', '')
            ])
        print("INFO - Sincronização de dados para Sheets concluída.")
    except Exception as e:
        print(f"ERROR - Erro ao sincronizar dados para Sheets: {e}.")

# --- Inicialização do App ---
with app.app_context():
    init_db()
    if not load_data_from_sheets():
        print("WARNING - Carregamento do Sheets falhou ou foi desativado. Usando dados do DB local.")
        # Fallback para carregar do DB local se Sheets falhar
        conn = get_db_connection()
        cursor = conn.cursor()
        carros = [dict(row) for row in cursor.execute("SELECT * FROM carros").fetchall()]
        usuarios = [dict(row) for row in cursor.execute("SELECT * FROM usuarios").fetchall()]
        reservas = [dict(row) for row in cursor.execute("SELECT * FROM reservas").fetchall()]
        conn.close()
        print(f"INFO - Dados carregados do DB local: {len(carros)} carros, {len(usuarios)} usuários, {len(reservas)} reservas.")
    print("INFO - App bootado com sucesso.")

# --- Rotas do Aplicativo ---

@app.route('/health')
def health():
    return 'OK'

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        senha_hash = hashlib.sha256(senha.encode()).hexdigest()

        # Tenta autenticar com usuários da planilha/DB
        user_found = False
        for user in usuarios:
            if user['email'] == email and user['senha_hash'] == senha_hash:
                session['logged_in'] = True
                session['user_email'] = email
                session['is_admin'] = user.get('is_admin', 0) == 1
                user_found = True
                break
        
        if user_found:
            return redirect(url_for('home'))
        else:
            # Fallback para admin padrão se não encontrado na lista
            if email == 'admin@jgminis.com.br' and senha_hash == hashlib.sha256('admin123'.encode()).hexdigest():
                session['logged_in'] = True
                session['user_email'] = email
                session['is_admin'] = True
                return redirect(url_for('home'))
            
        return render_template_string('''
            <style>
                body { font-family: Arial, sans-serif; background-color: #f8f9fa; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .login-container { background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 300px; text-align: center; }
                .login-container h2 { color: #343a40; margin-bottom: 20px; }
                .login-container input[type="email"], .login-container input[type="password"] { width: calc(100% - 20px); padding: 10px; margin-bottom: 15px; border: 1px solid #ced4da; border-radius: 4px; box-sizing: border-box; }
                .login-container input[type="submit"] { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; }
                .login-container input[type="submit"]:hover { background-color: #0056b3; }
                .error-message { color: #dc3545; margin-top: 10px; }
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
            body { font-family: Arial, sans-serif; background-color: #f8f9fa; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .login-container { background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); width: 300px; text-align: center; }
            .login-container h2 { color: #343a40; margin-bottom: 20px; }
            .login-container input[type="email"], .login-container input[type="password"] { width: calc(100% - 20px); padding: 10px; margin-bottom: 15px; border: 1px solid #ced4da; border-radius: 4px; box-sizing: border-box; }
            .login-container input[type="submit"] { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; }
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

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('user_email', None)
    session.pop('is_admin', None)
    return redirect(url_for('login'))

@app.route('/home')
def home():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    if not carros:
        return render_template_string('''
            <style>
                body { font-family: Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 0; }
                .navbar { background-color: #007bff; color: white; padding: 15px 20px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
                .navbar h1 { margin: 0; font-size: 24px; }
                .navbar a { color: white; text-decoration: none; margin-left: 20px; font-size: 16px; }
                .navbar a:hover { text-decoration: underline; }
                .content { padding: 40px 20px; text-align: center; }
                .content h2 { color: #343a40; margin-bottom: 20px; }
                .no-items { color: #6c757d; font-size: 18px; }
            </style>
            <div class="navbar">
                <h1>Bem-vindo ao JG Minis!</h1>
                <div>
                    <a href="/admin">Admin</a>
                    <a href="/logout">Sair</a>
                </div>
            </div>
            <div class="content">
                <h2>Nossas Miniaturas Disponíveis</h2>
                <p class="no-items">Nenhuma miniatura disponível no momento.</p>
            </div>
        ''')

    html_content = '''
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Miniaturas</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8f9fa; margin: 0; padding: 0; color: #343a40; }
            .navbar { background-color: #007bff; color: white; padding: 15px 20px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .navbar h1 { margin: 0; font-size: 24px; }
            .navbar a { color: white; text-decoration: none; margin-left: 20px; font-size: 16px; }
            .navbar a:hover { text-decoration: underline; }
            .content { padding: 40px 20px; text-align: center; }
            .content h2 { color: #343a40; margin-bottom: 30px; font-size: 28px; }
            .grid-container {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
                gap: 25px;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px 0;
            }
            .card {
                background-color: #ffffff;
                border-radius: 10px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.08);
                overflow: hidden;
                transition: transform 0.2s ease-in-out, box-shadow 0.2s ease-in-out;
                display: flex;
                flex-direction: column;
                align-items: center;
                text-align: center;
                padding-bottom: 15px;
            }
            .card:hover {
                transform: translateY(-5px);
                box-shadow: 0 6px 16px rgba(0,0,0,0.12);
            }
            .card-image {
                width: 100%;
                height: 200px;
                object-fit: cover;
                border-bottom: 1px solid #eee;
            }
            .card-body {
                padding: 15px;
                width: 100%;
            }
            .card-body h3 {
                font-size: 20px;
                margin-top: 0;
                margin-bottom: 10px;
                color: #007bff;
            }
            .card-body p {
                font-size: 14px;
                margin: 5px 0;
                color: #555;
            }
            .card-body .price {
                font-size: 18px;
                font-weight: bold;
                color: #28a745;
                margin-top: 10px;
            }
            .card-body button {
                background-color: #28a745;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                font-size: 16px;
                margin-top: 15px;
                transition: background-color 0.2s ease-in-out;
            }
            .card-body button:hover {
                background-color: #218838;
            }
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>Bem-vindo ao JG Minis!</h1>
            <div>
                <a href="/admin">Admin</a>
                <a href="/logout">Sair</a>
            </div>
        </div>
        <div class="content">
            <h2>Nossas Miniaturas Disponíveis</h2>
            <div class="grid-container">
    '''
    for carro in carros:
        html_content += f'''
                <div class="card">
                    <img src="{carro.get('thumbnail_url', 'https://via.placeholder.com/200x150?text=Sem+Imagem')}" class="card-image" alt="{carro.get('modelo', 'Miniatura')}">
                    <div class="card-body">
                        <h3>{carro.get('modelo', 'N/A')}</h3>
                        <p><strong>Marca:</strong> {carro.get('marca', 'N/A')}</p>
                        <p><strong>Previsão:</strong> {carro.get('ano', 'N/A')}</p>
                        <p><strong>Disponível:</strong> {carro.get('quantidade_disponivel', 0)}</p>
                        <p class="price">R$ {carro.get('preco_diaria', 0.0):.2f}</p>
                        <button onclick="alert('Reserva para {carro.get('modelo', 'Miniatura')} solicitada!')">Reservar</button>
                    </div>
                </div>
        '''
    html_content += '''
            </div>
        </div>
    </body>
    </html>
    '''
    return html_content

@app.route('/admin')
def admin():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    html_content = '''
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin JG Minis</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8f9fa; margin: 0; padding: 0; color: #343a40; }
            .navbar { background-color: #007bff; color: white; padding: 15px 20px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .navbar h1 { margin: 0; font-size: 24px; }
            .navbar a { color: white; text-decoration: none; margin-left: 20px; font-size: 16px; }
            .navbar a:hover { text-decoration: underline; }
            .admin-container { max-width: 1200px; margin: 40px auto; padding: 20px; background-color: #ffffff; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
            .admin-container h2 { color: #007bff; margin-bottom: 25px; border-bottom: 2px solid #eee; padding-bottom: 10px; }
            .admin-container h3 { color: #343a40; margin-top: 30px; margin-bottom: 15px; }
            .table-responsive { overflow-x: auto; margin-bottom: 30px; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { border: 1px solid #dee2e6; padding: 10px; text-align: left; font-size: 14px; }
            th { background-color: #e9ecef; font-weight: bold; color: #495057; }
            tr:nth-child(even) { background-color: #f2f2f2; }
            .actions a { color: #007bff; text-decoration: none; margin-right: 10px; }
            .actions a:hover { text-decoration: underline; }
            .add-button { background-color: #28a745; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; font-size: 14px; display: inline-block; margin-top: 10px; }
            .add-button:hover { background-color: #218838; }
            .sync-button { background-color: #ffc107; color: #343a40; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; font-size: 16px; display: inline-block; margin-top: 30px; }
            .sync-button:hover { background-color: #e0a800; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <h1>Admin JG Minis</h1>
            <div>
                <a href="/home">Home</a>
                <a href="/logout">Sair</a>
            </div>
        </div>
        <div class="admin-container">
            <h2>Painel Administrativo</h2>

            <h3>Carros</h3>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Modelo</th>
                            <th>Marca</th>
                            <th>Preço Diária</th>
                            <th>Disponível</th>
                            <th>Ações</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    for carro in carros:
        html_content += f'''
                        <tr>
                            <td>{carro.get('id', 'N/A')}</td>
                            <td>{carro.get('modelo', 'N/A')}</td>
                            <td>{carro.get('marca', 'N/A')}</td>
                            <td>R$ {carro.get('preco_diaria', 0.0):.2f}</td>
                            <td>{carro.get('quantidade_disponivel', 0)}</td>
                            <td class="actions">
                                <a href="/admin/edit_carro/{carro.get('id', '')}">Editar</a>
                                <a href="/admin/delete_carro/{carro.get('id', '')}" onclick="return confirm('Tem certeza que deseja deletar este carro?');">Deletar</a>
                            </td>
                        </tr>
        '''
    html_content += '''
                    </tbody>
                </table>
            </div>
            <a href="/admin/add_carro" class="add-button">Adicionar Carro</a>

            <h3>Usuários</h3>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Nome</th>
                            <th>Email</th>
                            <th>Admin</th>
                            <th>Ações</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    for usuario in usuarios:
        html_content += f'''
                        <tr>
                            <td>{usuario.get('id', 'N/A')}</td>
                            <td>{usuario.get('nome', 'N/A')}</td>
                            <td>{usuario.get('email', 'N/A')}</td>
                            <td>{'Sim' if usuario.get('is_admin', 0) == 1 else 'Não'}</td>
                            <td class="actions">
                                <a href="/admin/edit_usuario/{usuario.get('id', '')}">Editar</a>
                                <a href="/admin/delete_usuario/{usuario.get('id', '')}" onclick="return confirm('Tem certeza que deseja deletar este usuário?');">Deletar</a>
                            </td>
                        </tr>
        '''
    html_content += '''
                    </tbody>
                </table>
            </div>
            <a href="/admin/add_usuario" class="add-button">Adicionar Usuário</a>

            <h3>Reservas</h3>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Usuário ID</th>
                            <th>Carro ID</th>
                            <th>Data</th>
                            <th>Status</th>
                            <th>Ações</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    for reserva in reservas:
        html_content += f'''
                        <tr>
                            <td>{reserva.get('id', 'N/A')}</td>
                            <td>{reserva.get('usuario_id', 'N/A')}</td>
                            <td>{reserva.get('carro_id', 'N/A')}</td>
                            <td>{reserva.get('data_reserva', 'N/A')}</td>
                            <td>{reserva.get('status', 'N/A')}</td>
                            <td class="actions">
                                <a href="/admin/edit_reserva/{reserva.get('id', '')}">Editar</a>
                                <a href="/admin/delete_reserva/{reserva.get('id', '')}" onclick="return confirm('Tem certeza que deseja deletar esta reserva?');">Deletar</a>
                            </td>
                        </tr>
        '''
    html_content += '''
                    </tbody>
                </table>
            </div>
            <a href="/admin/add_reserva" class="add-button">Adicionar Reserva</a>
            <br>
            <a href="/admin/sync_sheets" class="sync-button">Sincronizar com Google Sheets</a>
        </div>
    </body>
    </html>
    '''
    return html_content

@app.route('/admin/sync_sheets')
def sync_sheets():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    if load_data_from_sheets(): # Tenta carregar do Sheets
        sync_data_to_sheets() # Se carregou, sincroniza de volta (bidirecional)
        return "Sincronização com Google Sheets concluída com sucesso!"
    else:
        return "Falha na sincronização com Google Sheets. Verifique logs e credenciais."

# --- Rotas CRUD Básicas (Exemplos Simplificados) ---

@app.route('/admin/add_carro', methods=['GET', 'POST'])
def add_carro():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        new_id = max([c['id'] for c in carros] + [0]) + 1 if carros else 1
        novo_carro = {
            'id': new_id,
            'thumbnail_url': request.form.get('thumbnail_url', ''),
            'modelo': request.form.get('modelo', ''),
            'marca': request.form.get('marca', ''),
            'ano': request.form.get('ano', ''),
            'quantidade_disponivel': int(request.form.get('quantidade_disponivel', 0)),
            'preco_diaria': float(request.form.get('preco_diaria', 0.0)),
            'observacoes': request.form.get('observacoes', ''),
            'max_reservas': int(request.form.get('max_reservas', 1))
        }
        carros.append(novo_carro)
        sync_data_to_sheets() # Sincroniza após adicionar
        return redirect(url_for('admin'))
    
    return render_template_string('''
        <style>
            body { font-family: Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 20px; }
            .form-container { background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); max-width: 600px; margin: 20px auto; }
            .form-container h2 { color: #007bff; margin-bottom: 20px; text-align: center; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: #343a40; }
            .form-group input[type="text"], .form-group input[type="number"] { width: calc(100% - 22px); padding: 10px; border: 1px solid #ced4da; border-radius: 4px; box-sizing: border-box; }
            .form-group input[type="submit"] { background-color: #28a745; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; width: auto; margin-top: 10px; }
            .form-group input[type="submit"]:hover { background-color: #218838; }
            .back-link { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; }
            .back-link:hover { text-decoration: underline; }
        </style>
        <div class="form-container">
            <h2>Adicionar Novo Carro</h2>
            <form method="post">
                <div class="form-group"><label for="thumbnail_url">URL da Imagem (Thumbnail):</label><input type="text" id="thumbnail_url" name="thumbnail_url"></div>
                <div class="form-group"><label for="modelo">Modelo:</label><input type="text" id="modelo" name="modelo" required></div>
                <div class="form-group"><label for="marca">Marca:</label><input type="text" id="marca" name="marca"></div>
                <div class="form-group"><label for="ano">Previsão de Chegada:</label><input type="text" id="ano" name="ano"></div>
                <div class="form-group"><label for="quantidade_disponivel">Quantidade Disponível:</label><input type="number" id="quantidade_disponivel" name="quantidade_disponivel" value="0"></div>
                <div class="form-group"><label for="preco_diaria">Preço Diária:</label><input type="number" id="preco_diaria" name="preco_diaria" step="0.01" value="0.00"></div>
                <div class="form-group"><label for="observacoes">Observações:</label><input type="text" id="observacoes" name="observacoes"></div>
                <div class="form-group"><label for="max_reservas">Máx. Reservas por Usuário:</label><input type="number" id="max_reservas" name="max_reservas" value="1"></div>
                <div class="form-group"><input type="submit" value="Adicionar Carro"></div>
            </form>
            <a href="/admin" class="back-link">Voltar para Admin</a>
        </div>
    ''')

@app.route('/admin/edit_carro/<int:carro_id>', methods=['GET', 'POST'])
def edit_carro(carro_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    carro_to_edit = next((c for c in carros if c['id'] == carro_id), None)
    if not carro_to_edit:
        return "Carro não encontrado", 404

    if request.method == 'POST':
        carro_to_edit['thumbnail_url'] = request.form.get('thumbnail_url', '')
        carro_to_edit['modelo'] = request.form.get('modelo', '')
        carro_to_edit['marca'] = request.form.get('marca', '')
        carro_to_edit['ano'] = request.form.get('ano', '')
        carro_to_edit['quantidade_disponivel'] = int(request.form.get('quantidade_disponivel', 0))
        carro_to_edit['preco_diaria'] = float(request.form.get('preco_diaria', 0.0))
        carro_to_edit['observacoes'] = request.form.get('observacoes', '')
        carro_to_edit['max_reservas'] = int(request.form.get('max_reservas', 1))
        sync_data_to_sheets()
        return redirect(url_for('admin'))
    
    return render_template_string(f'''
        <style>
            body { font-family: Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 20px; }
            .form-container { background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); max-width: 600px; margin: 20px auto; }
            .form-container h2 { color: #007bff; margin-bottom: 20px; text-align: center; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: #343a40; }
            .form-group input[type="text"], .form-group input[type="number"] { width: calc(100% - 22px); padding: 10px; border: 1px solid #ced4da; border-radius: 4px; box-sizing: border-box; }
            .form-group input[type="submit"] { background-color: #ffc107; color: #343a40; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; width: auto; margin-top: 10px; }
            .form-group input[type="submit"]:hover { background-color: #e0a800; }
            .back-link { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; }
            .back-link:hover { text-decoration: underline; }
        </style>
        <div class="form-container">
            <h2>Editar Carro (ID: {carro_id})</h2>
            <form method="post">
                <div class="form-group"><label for="thumbnail_url">URL da Imagem (Thumbnail):</label><input type="text" id="thumbnail_url" name="thumbnail_url" value="{carro_to_edit.get('thumbnail_url', '')}"></div>
                <div class="form-group"><label for="modelo">Modelo:</label><input type="text" id="modelo" name="modelo" value="{carro_to_edit.get('modelo', '')}" required></div>
                <div class="form-group"><label for="marca">Marca:</label><input type="text" id="marca" name="marca" value="{carro_to_edit.get('marca', '')}"></div>
                <div class="form-group"><label for="ano">Previsão de Chegada:</label><input type="text" id="ano" name="ano" value="{carro_to_edit.get('ano', '')}"></div>
                <div class="form-group"><label for="quantidade_disponivel">Quantidade Disponível:</label><input type="number" id="quantidade_disponivel" name="quantidade_disponivel" value="{carro_to_edit.get('quantidade_disponivel', 0)}"></div>
                <div class="form-group"><label for="preco_diaria">Preço Diária:</label><input type="number" id="preco_diaria" name="preco_diaria" step="0.01" value="{carro_to_edit.get('preco_diaria', 0.0):.2f}"></div>
                <div class="form-group"><label for="observacoes">Observações:</label><input type="text" id="observacoes" name="observacoes" value="{carro_to_edit.get('observacoes', '')}"></div>
                <div class="form-group"><label for="max_reservas">Máx. Reservas por Usuário:</label><input type="number" id="max_reservas" name="max_reservas" value="{carro_to_edit.get('max_reservas', 1)}"></div>
                <div class="form-group"><input type="submit" value="Salvar Alterações"></div>
            </form>
            <a href="/admin" class="back-link">Voltar para Admin</a>
        </div>
    ''')

@app.route('/admin/delete_carro/<int:carro_id>')
def delete_carro(carro_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    global carros
    carros = [c for c in carros if c['id'] != carro_id]
    sync_data_to_sheets()
    return redirect(url_for('admin'))

# Rotas de CRUD para Usuários e Reservas (simplificadas, apenas redirecionam para admin)
@app.route('/admin/add_usuario')
def add_usuario():
    return "Funcionalidade de adicionar usuário não implementada. Adicione via planilha."

@app.route('/admin/edit_usuario/<int:usuario_id>')
def edit_usuario(usuario_id):
    return "Funcionalidade de editar usuário não implementada. Edite via planilha."

@app.route('/admin/delete_usuario/<int:usuario_id>')
def delete_usuario(usuario_id):
    return "Funcionalidade de deletar usuário não implementada. Delete via planilha."

@app.route('/admin/add_reserva')
def add_reserva():
    return "Funcionalidade de adicionar reserva não implementada. Adicione via planilha."

@app.route('/admin/edit_reserva/<int:reserva_id>')
def edit_reserva(reserva_id):
    return "Funcionalidade de editar reserva não implementada. Edite via planilha."

@app.route('/admin/delete_reserva/<int:reserva_id>')
def delete_reserva(reserva_id):
    return "Funcionalidade de deletar reserva não implementada. Delete via planilha."

if __name__ == '__main__':
    app.run(debug=True)
