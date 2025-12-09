import os
import json
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify, flash
import gspread
from google.oauth2.service_account import Credentials
import hashlib
import sqlite3
from datetime import datetime, timedelta
import logging

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'uma_chave_secreta_padrao_e_segura') # Use uma chave forte em produção

# --- Configuração do Google Sheets ---
try:
    creds_dict = {
        "type": "service_account",
        "project_id": os.getenv('GOOGLE_PROJECT_ID'),
        "private_key_id": os.getenv('GOOGLE_PRIVATE_KEY_ID'),
        "private_key": os.getenv('GOOGLE_PRIVATE_KEY').replace('\\\n', '\n'), # Substitui \\n por \n
        "client_email": os.getenv('GOOGLE_CLIENT_EMAIL'),
        "client_id": os.getenv('GOOGLE_CLIENT_ID'),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": os.getenv('GOOGLE_CLIENT_X509_CERT_URL')
    }
    
    # Verifica se todas as chaves essenciais estão presentes e não são None
    if not all(creds_dict.get(k) for k in ["project_id", "private_key", "client_email"]):
        raise ValueError("Credenciais do Google incompletas nas variáveis de ambiente.")

    creds = Credentials.from_service_account_info(creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    gc = gspread.authorize(creds)
    logging.info("gspread: google-auth e gspread importados e autenticados com sucesso.")
    
    SHEET_ID = os.getenv('SHEET_ID')
    if not SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID não configurado nas variáveis de ambiente.")
    
    sheet = gc.open_by_key(SHEET_ID)
    logging.info(f"Planilha Google Sheets '{SHEET_ID}' aberta com sucesso.")

except Exception as e:
    logging.error(f"Erro na configuração do Google Sheets: {e}. Sincronização com Sheets desativada.")
    gc = None
    sheet = None
    SHEET_ID = None

# --- Configuração do Banco de Dados SQLite ---
DATABASE_PATH = os.getenv('DATABASE_PATH', 'jgminis.db')

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

    # Tabela de Carros (Miniaturas)
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
        logging.info("Coluna 'thumbnail_url' adicionada à tabela 'carros'.")
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
    admin_email = 'admin@jgminis.com.br'
    admin_senha_hash = hashlib.sha256('admin123'.encode()).hexdigest() # SHA256 de 'admin123'
    cursor.execute("SELECT id FROM usuarios WHERE email = ?", (admin_email,))
    if cursor.fetchone() is None:
        cursor.execute("INSERT INTO usuarios (nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       ('Admin', admin_email, admin_senha_hash, '111.111.111-11', '(11)99999-9999', datetime.now().strftime('%Y-%m-%d'), 1))
        logging.info(f"Usuário admin '{admin_email}' criado no DB local.")
    else:
        logging.info(f"Usuário admin '{admin_email}' já existe no DB local.")

    conn.commit()
    conn.close()
    logging.info("DB inicializado com sucesso.")

# --- Funções de Sincronização com Google Sheets ---
def get_sheet_data(sheet_name, default_headers):
    if not sheet:
        return []
    try:
        worksheet = sheet.worksheet(sheet_name)
        data = worksheet.get_all_records()
        logging.info(f"Dados da aba '{sheet_name}' carregados do Sheets: {len(data)} registros.")
        return data
    except gspread.WorksheetNotFound:
        logging.warning(f"Aba '{sheet_name}' não encontrada. Criando aba...")
        worksheet = sheet.add_worksheet(sheet_name, rows=1000, cols=len(default_headers))
        worksheet.append_row(default_headers)
        logging.info(f"Aba '{sheet_name}' criada com cabeçalhos padrão.")
        return []
    except Exception as e:
        logging.error(f"Erro ao carregar dados da aba '{sheet_name}' do Sheets: {e}")
        return []

def update_sheet_data(sheet_name, headers, data):
    if not sheet:
        return
    try:
        worksheet = sheet.worksheet(sheet_name)
        worksheet.clear()
        worksheet.append_row(headers)
        for row_data in data:
            row_values = [row_data.get(h, '') for h in headers]
            worksheet.append_row(row_values)
        logging.info(f"Dados da aba '{sheet_name}' atualizados no Sheets: {len(data)} registros.")
    except gspread.WorksheetNotFound:
        logging.error(f"Aba '{sheet_name}' não encontrada para atualização. Criando e populando...")
        worksheet = sheet.add_worksheet(sheet_name, rows=1000, cols=len(headers))
        worksheet.append_row(headers)
        for row_data in data:
            row_values = [row_data.get(h, '') for h in headers]
            worksheet.append_row(row_values)
    except Exception as e:
        logging.error(f"Erro ao atualizar dados da aba '{sheet_name}' no Sheets: {e}")

def load_from_sheets_to_db():
    if not sheet:
        logging.warning("Cliente gspread não inicializado. Carregando dados apenas do DB local.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    # Carregar Carros
    carros_headers = ['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO']
    sheet_carros = get_sheet_data('Carros', carros_headers)
    if sheet_carros:
        cursor.execute("DELETE FROM carros") # Limpa DB para recarregar do Sheets
        for row in sheet_carros:
            try:
                carro_id = row.get('ID')
                if carro_id is None or carro_id == '': # Gera ID se não existir na planilha
                    carro_id = cursor.execute("SELECT MAX(id) FROM carros").fetchone()[0]
                    carro_id = (carro_id if carro_id is not None else 0) + 1
                
                cursor.execute(
                    "INSERT INTO carros (id, thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        carro_id,
                        row.get('IMAGEM', ''),
                        row.get('NOME DA MINIATURA', ''),
                        row.get('MARCA/FABRICANTE', ''),
                        row.get('PREVISÃO DE CHEGADA', ''),
                        int(row.get('QUANTIDADE DISPONIVEL', 0)),
                        float(row.get('VALOR', 0.0)),
                        row.get('OBSERVAÇÕES', ''),
                        int(row.get('MAX_RESERVAS_POR_USUARIO', 1))
                    )
                )
            except Exception as e:
                logging.error(f"Erro ao inserir carro do Sheets no DB: {e} - Dados: {row}")
        logging.info(f"Carros carregados do Sheets para o DB: {len(sheet_carros)} itens.")
    else:
        logging.warning("Nenhum carro encontrado na planilha ou erro ao carregar.")

    # Carregar Usuários
    usuarios_headers = ['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data_Cadastro', 'Is_Admin']
    sheet_usuarios = get_sheet_data('Usuarios', usuarios_headers)
    if sheet_usuarios:
        cursor.execute("DELETE FROM usuarios WHERE is_admin = 0") # Mantém admin padrão se não estiver no Sheets
        for row in sheet_usuarios:
            try:
                usuario_id = row.get('ID')
                if usuario_id is None or usuario_id == '':
                    usuario_id = cursor.execute("SELECT MAX(id) FROM usuarios").fetchone()[0]
                    usuario_id = (usuario_id if usuario_id is not None else 0) + 1
                
                # Verifica se o usuário já existe (para não duplicar o admin padrão)
                cursor.execute("SELECT id FROM usuarios WHERE email = ?", (row.get('Email', ''),))
                if cursor.fetchone() is None:
                    cursor.execute(
                        "INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            usuario_id,
                            row.get('Nome', ''),
                            row.get('Email', ''),
                            row.get('Senha_hash', ''),
                            row.get('CPF', ''),
                            row.get('Telefone', ''),
                            row.get('Data_Cadastro', ''),
                            1 if row.get('Is_Admin', '').lower() == 'sim' else 0
                        )
                    )
            except Exception as e:
                logging.error(f"Erro ao inserir usuário do Sheets no DB: {e} - Dados: {row}")
        logging.info(f"Usuários carregados do Sheets para o DB: {len(sheet_usuarios)} itens.")
    else:
        logging.warning("Nenhum usuário encontrado na planilha ou erro ao carregar.")

    # Carregar Reservas
    reservas_headers = ['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes']
    sheet_reservas = get_sheet_data('Reservas', reservas_headers)
    if sheet_reservas:
        cursor.execute("DELETE FROM reservas")
        for row in sheet_reservas:
            try:
                reserva_id = row.get('ID')
                if reserva_id is None or reserva_id == '':
                    reserva_id = cursor.execute("SELECT MAX(id) FROM reservas").fetchone()[0]
                    reserva_id = (reserva_id if reserva_id is not None else 0) + 1
                
                cursor.execute(
                    "INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reserva_id,
                        int(row.get('Usuario_id', 0)),
                        int(row.get('Carro_id', 0)),
                        row.get('Data_reserva', ''),
                        row.get('Hora_inicio', ''),
                        row.get('Hora_fim', ''),
                        row.get('Status', ''),
                        row.get('Observacoes', '')
                    )
                )
            except Exception as e:
                logging.error(f"Erro ao inserir reserva do Sheets no DB: {e} - Dados: {row}")
        logging.info(f"Reservas carregadas do Sheets para o DB: {len(sheet_reservas)} itens.")
    else:
        logging.warning("Nenhuma reserva encontrada na planilha ou erro ao carregar.")

    conn.commit()
    conn.close()

def sync_db_to_sheets():
    if not sheet:
        logging.warning("Cliente gspread não inicializado. Sincronização do DB para Sheets desativada.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    # Sincronizar Carros
    carros_headers = ['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO']
    db_carros = cursor.execute("SELECT id, thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas FROM carros").fetchall()
    carros_data = [{
        'ID': c['id'],
        'IMAGEM': c['thumbnail_url'],
        'NOME DA MINIATURA': c['modelo'],
        'MARCA/FABRICANTE': c['marca'],
        'PREVISÃO DE CHEGADA': c['ano'],
        'QUANTIDADE DISPONIVEL': c['quantidade_disponivel'],
        'VALOR': c['preco_diaria'],
        'OBSERVAÇÕES': c['observacoes'],
        'MAX_RESERVAS_POR_USUARIO': c['max_reservas']
    } for c in db_carros]
    update_sheet_data('Carros', carros_headers, carros_data)

    # Sincronizar Usuários
    usuarios_headers = ['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data_Cadastro', 'Is_Admin']
    db_usuarios = cursor.execute("SELECT id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin FROM usuarios").fetchall()
    usuarios_data = [{
        'ID': u['id'],
        'Nome': u['nome'],
        'Email': u['email'],
        'Senha_hash': u['senha_hash'],
        'CPF': u['cpf'],
        'Telefone': u['telefone'],
        'Data_Cadastro': u['data_cadastro'],
        'Is_Admin': 'Sim' if u['is_admin'] == 1 else 'Não'
    } for u in db_usuarios]
    update_sheet_data('Usuarios', usuarios_headers, usuarios_data)

    # Sincronizar Reservas
    reservas_headers = ['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes']
    db_reservas = cursor.execute("SELECT id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes FROM reservas").fetchall()
    reservas_data = [{
        'ID': r['id'],
        'Usuario_id': r['usuario_id'],
        'Carro_id': r['carro_id'],
        'Data_reserva': r['data_reserva'],
        'Hora_inicio': r['hora_inicio'],
        'Hora_fim': r['hora_fim'],
        'Status': r['status'],
        'Observacoes': r['observacoes']
    } for r in db_reservas]
    update_sheet_data('Reservas', reservas_headers, reservas_data)

    conn.close()

# --- Inicialização do App ---
with app.app_context():
    init_db()
    load_from_sheets_to_db() # Carrega dados do Sheets para o DB na inicialização

# --- Decoradores ---
def login_required(f):
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            flash('Você precisa estar logado para acessar esta página.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__ # Necessário para Flask
    return wrapper

def admin_required(f):
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Acesso negado. Apenas administradores podem acessar esta página.', 'danger')
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

# --- Rotas ---
@app.route('/health')
def health():
    return 'OK'

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        senha_hash = hashlib.sha256(senha.encode()).hexdigest()

        conn = get_db_connection()
        user = conn.execute("SELECT * FROM usuarios WHERE email = ? AND senha_hash = ?", (email, senha_hash)).fetchone()
        conn.close()

        if user:
            session['logged_in'] = True
            session['user_id'] = user['id']
            session['user_name'] = user['nome']
            session['is_admin'] = bool(user['is_admin'])
            flash(f'Bem-vindo, {user["nome"]}!', 'success')
            return redirect(url_for('home'))
        else:
            flash('Login falhou. Verifique seu email e senha.', 'danger')
            return render_template_string(LOGIN_HTML, message='Login falhou. Verifique seu email e senha.')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
@login_required
def logout():
    session.clear()
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('login'))

@app.route('/')
@app.route('/home')
@login_required
def home():
    conn = get_db_connection()
    carros_db = conn.execute("SELECT * FROM carros").fetchall()
    conn.close()

    # Converte Row objects para dicionários para facilitar o acesso no HTML
    carros_list = [dict(carro) for carro in carros_db]

    return render_template_string(HOME_HTML, carros=carros_list, is_admin=session.get('is_admin'))

@app.route('/admin')
@login_required
@admin_required
def admin():
    conn = get_db_connection()
    carros_db = conn.execute("SELECT * FROM carros").fetchall()
    usuarios_db = conn.execute("SELECT * FROM usuarios").fetchall()
    reservas_db = conn.execute("SELECT * FROM reservas").fetchall()
    conn.close()

    carros_list = [dict(carro) for carro in carros_db]
    usuarios_list = [dict(usuario) for usuario in usuarios_db]
    reservas_list = [dict(reserva) for reserva in reservas_db]

    return render_template_string(ADMIN_HTML, carros=carros_list, usuarios=usuarios_list, reservas=reservas_list)

@app.route('/admin/sync_sheets')
@login_required
@admin_required
def admin_sync_sheets():
    load_from_sheets_to_db()
    sync_db_to_sheets()
    flash('Sincronização com Google Sheets concluída.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/add_carro', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_add_carro():
    if request.method == 'POST':
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "INSERT INTO carros (thumbnail_url, modelo, marca, ano, quantidade_disponivel, preco_diaria, observacoes, max_reservas) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.form['thumbnail_url'],
                request.form['modelo'],
                request.form['marca'],
                request.form['ano'],
                int(request.form['quantidade_disponivel']),
                float(request.form['preco_diaria']),
                request.form['observacoes'],
                int(request.form['max_reservas'])
            )
        )
        conn.commit()
        conn.close()
        sync_db_to_sheets() # Sincroniza a mudança de volta para o Sheets
        flash('Carro adicionado com sucesso!', 'success')
        return redirect(url_for('admin'))
    return render_template_string(ADD_CARRO_HTML)

@app.route('/admin/edit_carro/<int:carro_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_carro(carro_id):
    conn = get_db_connection()
    carro = conn.execute("SELECT * FROM carros WHERE id = ?", (carro_id,)).fetchone()
    
    if request.method == 'POST':
        conn.execute(
            "UPDATE carros SET thumbnail_url=?, modelo=?, marca=?, ano=?, quantidade_disponivel=?, preco_diaria=?, observacoes=?, max_reservas=? WHERE id=?",
            (
                request.form['thumbnail_url'],
                request.form['modelo'],
                request.form['marca'],
                request.form['ano'],
                int(request.form['quantidade_disponivel']),
                float(request.form['preco_diaria']),
                request.form['observacoes'],
                int(request.form['max_reservas']),
                carro_id
            )
        )
        conn.commit()
        conn.close()
        sync_db_to_sheets()
        flash('Carro atualizado com sucesso!', 'success')
        return redirect(url_for('admin'))
    
    conn.close()
    if carro:
        return render_template_string(EDIT_CARRO_HTML, carro=dict(carro))
    flash('Carro não encontrado.', 'danger')
    return redirect(url_for('admin'))

@app.route('/admin/delete_carro/<int:carro_id>')
@login_required
@admin_required
def admin_delete_carro(carro_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM carros WHERE id = ?", (carro_id,))
    conn.commit()
    conn.close()
    sync_db_to_sheets()
    flash('Carro deletado com sucesso!', 'success')
    return redirect(url_for('admin'))

# --- HTML Templates Inline (para self-contained) ---
BASE_CSS = '''
<style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; background-color: #f4f7f6; color: #333; }
    .container { max-width: 1200px; margin: 20px auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
    .header { background-color: #007bff; color: white; padding: 15px 20px; border-radius: 8px 8px 0 0; display: flex; justify-content: space-between; align-items: center; }
    .header h1 { margin: 0; font-size: 1.8em; }
    .header .nav-links a { color: white; text-decoration: none; margin-left: 20px; font-weight: bold; }
    .header .nav-links a:hover { text-decoration: underline; }
    .flash-messages { margin-top: 15px; padding: 10px; border-radius: 5px; }
    .flash-messages.success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
    .flash-messages.danger { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
    .flash-messages.info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
    .grid-container { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 25px; margin-top: 30px; }
    .card { background-color: #fff; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); transition: transform 0.2s ease-in-out; }
    .card:hover { transform: translateY(-5px); box-shadow: 0 6px 16px rgba(0,0,0,0.1); }
    .card img { width: 100%; height: 200px; object-fit: cover; border-bottom: 1px solid #eee; }
    .card-content { padding: 15px; }
    .card-content h3 { margin-top: 0; margin-bottom: 10px; color: #007bff; font-size: 1.4em; }
    .card-content p { margin: 5px 0; font-size: 0.95em; line-height: 1.4; }
    .card-content .price { font-size: 1.2em; font-weight: bold; color: #28a745; margin-top: 10px; }
    .card-content button { background-color: #007bff; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-size: 1em; margin-top: 15px; width: 100%; transition: background-color 0.2s; }
    .card-content button:hover { background-color: #0056b3; }
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
    .form-group input[type="text"], .form-group input[type="email"], .form-group input[type="password"], .form-group input[type="number"] {
        width: calc(100% - 22px); padding: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em;
    }
    .form-group button { background-color: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 1em; transition: background-color 0.2s; }
    .form-group button:hover { background-color: #218838; }
    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
    th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
    th { background-color: #f2f2f2; font-weight: bold; }
    .admin-actions a { margin-right: 10px; color: #007bff; text-decoration: none; }
    .admin-actions a:hover { text-decoration: underline; }
    .add-button { display: inline-block; background-color: #007bff; color: white; padding: 10px 15px; border-radius: 5px; text-decoration: none; margin-top: 15px; }
    .add-button:hover { background-color: #0056b3; }
</style>
'''

LOGIN_HTML = BASE_CSS + '''
<div class="container">
    <div class="header">
        <h1>Login - JG Minis</h1>
    </div>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash-messages {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <form method="post" class="container" style="max-width: 400px; margin-top: 20px;">
        <div class="form-group">
            <label for="email">Email:</label>
            <input type="email" id="email" name="email" required>
        </div>
        <div class="form-group">
            <label for="senha">Senha:</label>
            <input type="password" id="senha" name="senha" required>
        </div>
        <div class="form-group">
            <button type="submit">Entrar</button>
        </div>
    </form>
</div>
'''

HOME_HTML = BASE_CSS + '''
<div class="container">
    <div class="header">
        <h1>Bem-vindo ao JG Minis!</h1>
        <div class="nav-links">
            {% if is_admin %}<a href="/admin">Admin</a>{% endif %}
            <a href="/logout">Sair</a>
        </div>
    </div>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash-messages {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <h2>Nossas Miniaturas Disponíveis</h2>
    {% if carros %}
        <div class="grid-container">
            {% for carro in carros %}
                <div class="card">
                    <img src="{{ carro.thumbnail_url }}" alt="{{ carro.modelo }}">
                    <div class="card-content">
                        <h3>{{ carro.modelo }}</h3>
                        <p><strong>Marca:</strong> {{ carro.marca }}</p>
                        <p><strong>Previsão:</strong> {{ carro.ano }}</p>
                        <p><strong>Disponível:</strong> {{ carro.quantidade_disponivel }} unidades</p>
                        <p class="price">R$ {{ "%.2f"|format(carro.preco_diaria) }} / dia</p>
                        <button onclick="alert('Funcionalidade de reserva em desenvolvimento para {{ carro.modelo }} (ID: {{ carro.id }})')">Reservar</button>
                    </div>
                </div>
            {% endfor %}
        </div>
    {% else %}
        <p>Nenhuma miniatura disponível no momento.</p>
    {% endif %}
</div>
'''

ADMIN_HTML = BASE_CSS + '''
<div class="container">
    <div class="header">
        <h1>Painel Administrativo</h1>
        <div class="nav-links">
            <a href="/home">Home</a>
            <a href="/logout">Sair</a>
        </div>
    </div>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash-messages {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <h2>Gerenciar Carros (Miniaturas)</h2>
    <a href="/admin/add_carro" class="add-button">Adicionar Nova Miniatura</a>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Imagem</th>
                <th>Modelo</th>
                <th>Marca</th>
                <th>Preço Diário</th>
                <th>Disponível</th>
                <th>Ações</th>
            </tr>
        </thead>
        <tbody>
            {% for carro in carros %}
            <tr>
                <td>{{ carro.id }}</td>
                <td><img src="{{ carro.thumbnail_url }}" alt="{{ carro.modelo }}" style="width: 50px; height: 50px; object-fit: cover;"></td>
                <td>{{ carro.modelo }}</td>
                <td>{{ carro.marca }}</td>
                <td>R$ {{ "%.2f"|format(carro.preco_diaria) }}</td>
                <td>{{ carro.quantidade_disponivel }}</td>
                <td class="admin-actions">
                    <a href="/admin/edit_carro/{{ carro.id }}">Editar</a>
                    <a href="/admin/delete_carro/{{ carro.id }}" onclick="return confirm('Tem certeza que deseja deletar {{ carro.modelo }}?')">Deletar</a>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Gerenciar Usuários</h2>
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
            {% for usuario in usuarios %}
            <tr>
                <td>{{ usuario.id }}</td>
                <td>{{ usuario.nome }}</td>
                <td>{{ usuario.email }}</td>
                <td>{% if usuario.is_admin %}Sim{% else %}Não{% endif %}</td>
                <td class="admin-actions">
                    <a href="#">Editar</a>
                    <a href="#">Deletar</a>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Gerenciar Reservas</h2>
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
            {% for reserva in reservas %}
            <tr>
                <td>{{ reserva.id }}</td>
                <td>{{ reserva.usuario_id }}</td>
                <td>{{ reserva.carro_id }}</td>
                <td>{{ reserva.data_reserva }}</td>
                <td>{{ reserva.status }}</td>
                <td class="admin-actions">
                    <a href="#">Editar</a>
                    <a href="#">Deletar</a>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <div style="margin-top: 30px;">
        <a href="/admin/sync_sheets" class="add-button" style="background-color: #28a745;">Sincronizar com Google Sheets</a>
    </div>
</div>
'''

ADD_CARRO_HTML = BASE_CSS + '''
<div class="container">
    <div class="header">
        <h1>Adicionar Nova Miniatura</h1>
        <div class="nav-links">
            <a href="/admin">Voltar ao Admin</a>
            <a href="/logout">Sair</a>
        </div>
    </div>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash-messages {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <form method="post" class="container" style="max-width: 600px; margin-top: 20px;">
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
            <label for="ano">Previsão de Chegada (Ano/Período):</label>
            <input type="text" id="ano" name="ano">
        </div>
        <div class="form-group">
            <label for="quantidade_disponivel">Quantidade Disponível:</label>
            <input type="number" id="quantidade_disponivel" name="quantidade_disponivel" required min="0">
        </div>
        <div class="form-group">
            <label for="preco_diaria">Valor (Preço):</label>
            <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" required min="0">
        </div>
        <div class="form-group">
            <label for="observacoes">Observações:</label>
            <input type="text" id="observacoes" name="observacoes">
        </div>
        <div class="form-group">
            <label for="max_reservas">Máx. Reservas por Usuário:</label>
            <input type="number" id="max_reservas" name="max_reservas" required min="1">
        </div>
        <div class="form-group">
            <button type="submit">Adicionar Miniatura</button>
        </div>
    </form>
</div>
'''

EDIT_CARRO_HTML = BASE_CSS + '''
<div class="container">
    <div class="header">
        <h1>Editar Miniatura</h1>
        <div class="nav-links">
            <a href="/admin">Voltar ao Admin</a>
            <a href="/logout">Sair</a>
        </div>
    </div>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash-messages {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <form method="post" class="container" style="max-width: 600px; margin-top: 20px;">
        <div class="form-group">
            <label for="thumbnail_url">URL da Imagem (Thumbnail):</label>
            <input type="text" id="thumbnail_url" name="thumbnail_url" value="{{ carro.thumbnail_url }}" required>
        </div>
        <div class="form-group">
            <label for="modelo">Nome da Miniatura (Modelo):</label>
            <input type="text" id="modelo" name="modelo" value="{{ carro.modelo }}" required>
        </div>
        <div class="form-group">
            <label for="marca">Marca/Fabricante:</label>
            <input type="text" id="marca" name="marca" value="{{ carro.marca }}">
        </div>
        <div class="form-group">
            <label for="ano">Previsão de Chegada (Ano/Período):</label>
            <input type="text" id="ano" name="ano" value="{{ carro.ano }}">
        </div>
        <div class="form-group">
            <label for="quantidade_disponivel">Quantidade Disponível:</label>
            <input type="number" id="quantidade_disponivel" name="quantidade_disponivel" value="{{ carro.quantidade_disponivel }}" required min="0">
        </div>
        <div class="form-group">
            <label for="preco_diaria">Valor (Preço):</label>
            <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" value="{{ carro.preco_diaria }}" required min="0">
        </div>
        <div class="form-group">
            <label for="observacoes">Observações:</label>
            <input type="text" id="observacoes" name="observacoes" value="{{ carro.observacoes }}">
        </div>
        <div class="form-group">
            <label for="max_reservas">Máx. Reservas por Usuário:</label>
            <input type="number" id="max_reservas" name="max_reservas" value="{{ carro.max_reservas }}" required min="1">
        </div>
        <div class="form-group">
            <button type="submit">Salvar Alterações</button>
        </div>
    </form>
</div>
'''

if __name__ == '__main__':
    app.run(debug=True)
