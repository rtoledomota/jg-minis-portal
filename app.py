from flask import Flask, request, redirect, url_for, session, flash, jsonify, send_file
import sqlite3
import json
from datetime import datetime, timedelta
import os
import logging
from io import BytesIO
import hashlib

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Tenta importar google-auth e gspread, com fallback para erro se não estiver disponível
try:
    from google.oauth2.service_account import Credentials
    import gspread
    logging.info('gspread: google-auth e gspread importados com sucesso.')
except ImportError:
    logging.error('gspread: As bibliotecas google-auth ou gspread não foram encontradas. As funcionalidades de sincronização com Google Sheets não estarão disponíveis.')
    Credentials = None
    gspread = None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_jgminis_v4.3.22') # Chave secreta para sessões

# Caminho do banco de dados (persistente no Railway via /tmp)
DATABASE_PATH = os.environ.get('DATABASE_PATH', '/tmp/jgminis.db')

# --- Funções de Banco de Dados ---
def get_db_connection():
    """Retorna uma conexão com o banco de dados."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row # Permite acessar colunas por nome
    return conn

def init_db():
    """Inicializa o banco de dados SQLite, criando tabelas se não existirem."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Tabela de Usuários
        c.execute('''CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            cpf TEXT UNIQUE,
            telefone TEXT,
            data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_admin BOOLEAN DEFAULT FALSE
        )''')

        # Tabela de Carros
        c.execute('''CREATE TABLE IF NOT EXISTS carros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            modelo TEXT NOT NULL,
            ano INTEGER,
            cor TEXT,
            placa TEXT UNIQUE,
            disponivel BOOLEAN DEFAULT TRUE,
            preco_diaria REAL NOT NULL,
            thumbnail_url TEXT
        )''')

        # Tabela de Reservas
        c.execute('''CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            carro_id INTEGER,
            data_reserva DATE NOT NULL,
            hora_inicio TIME NOT NULL,
            hora_fim TIME NOT NULL,
            status TEXT DEFAULT 'pendente',
            observacoes TEXT,
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id),
            FOREIGN KEY (carro_id) REFERENCES carros (id)
        )''')

        conn.commit()

        # Cria admin padrão se nenhum usuário existir
        c.execute('SELECT COUNT(*) FROM usuarios')
        usuarios_count = c.fetchone()[0]
        if usuarios_count == 0:
            senha_hash = hashlib.sha256('admin123'.encode()).hexdigest()
            c.execute('INSERT INTO usuarios (nome, email, senha_hash, is_admin) VALUES (?, ?, ?, ?)',
                      ('Admin Padrão', 'admin@jgminis.com.br', senha_hash, True))
            conn.commit()
            logging.info('DB inicializado: Usuário admin padrão criado (admin@jgminis.com.br, senha: admin123).')
        else:
            logging.info(f'DB inicializado: {usuarios_count} cadastros preservados.')

        logging.info('DB inicializado com sucesso.')
    except sqlite3.Error as e:
        logging.error(f"Erro ao inicializar o banco de dados: {e}")
    finally:
        if conn:
            conn.close()

# --- Funções de Autenticação e Autorização ---
def is_admin():
    """Verifica se o usuário logado é administrador."""
    if 'user_id' in session:
        user = get_user_by_id(session['user_id'])
        return user and user['is_admin']
    return False

# --- Funções de Integração com Google Sheets (gspread) ---
gspread_client = None
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def init_gspread_client():
    """Inicializa o cliente gspread para acesso às planilhas."""
    global gspread_client
    if gspread_client:
        return gspread_client # Retorna cliente existente se já inicializado

    if not gspread or not Credentials:
        logging.warning('gspread: Bibliotecas gspread ou google-auth não disponíveis. Sincronização com Sheets desativada.')
        return None

    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        
        # Tenta carregar credenciais da variável de ambiente (recomendado no Railway)
        if os.environ.get('GOOGLE_CREDENTIALS_JSON'):
            creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            logging.info('gspread: Credenciais carregadas da variável de ambiente.')
        # Fallback para arquivo service_account.json (para desenvolvimento local)
        elif os.path.exists('service_account.json'):
            creds = Credentials.from_service_account_file('service_account.json', scopes=scope)
            logging.info('gspread: Credenciais carregadas do arquivo service_account.json.')
        else:
            logging.error('gspread: Nenhuma credencial encontrada (variável de ambiente ou arquivo). Sincronização com Sheets desativada.')
            return None
            
        gspread_client = gspread.authorize(creds)
        logging.info('gspread: Autenticação bem-sucedida.')
        return gspread_client
    except Exception as e:
        logging.error(f'gspread: Erro na autenticação gspread: {e}. Sincronização com Sheets desativada.')
        return None

def load_from_sheets():
    """Carrega dados das planilhas Google para o DB local."""
    if not gspread_client:
        logging.warning('load_from_sheets: Cliente gspread não inicializado. Carregando dados apenas do DB local.')
        return

    if not GOOGLE_SHEET_ID:
        logging.error('load_from_sheets: GOOGLE_SHEET_ID não configurado. Sincronização com Sheets desativada.')
        return

    conn = get_db_connection()
    c = conn.cursor()
    try:
        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEET_ID)

        # Carregar Carros
        try:
            carros_sheet = spreadsheet.worksheet('Carros')
            carros_data = carros_sheet.get_all_records()
            c.execute('DELETE FROM carros')
            for car_row in carros_data:
                c.execute('INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (car_row.get('ID'), car_row.get('Modelo'), car_row.get('Ano'), car_row.get('Cor'), car_row.get('Placa'),
                           car_row.get('Disponivel', 'Sim').lower() == 'sim', car_row.get('Preco Diaria'), car_row.get('thumbnail_url')))
            logging.info(f"Dados carregados da planilha 'Carros': {len(carros_data)} itens.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Aba 'Carros' não encontrada na planilha. Carros não carregados do Sheets.")
        except Exception as e:
            logging.error(f"Erro ao carregar carros do Sheets: {e}")

        # Carregar Usuários
        try:
            usuarios_sheet = spreadsheet.worksheet('Usuarios')
            usuarios_data = usuarios_sheet.get_all_records()
            c.execute('DELETE FROM usuarios')
            for user_row in usuarios_data:
                c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (user_row.get('ID'), user_row.get('Nome'), user_row.get('Email'), user_row.get('Senha_hash'),
                           user_row.get('CPF'), user_row.get('Telefone'), user_row.get('Data Cadastro'),
                           user_row.get('Admin', 'Não').lower() == 'sim'))
            logging.info(f"Dados carregados da planilha 'Usuarios': {len(usuarios_data)} itens.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Aba 'Usuarios' não encontrada na planilha. Usuários não carregados do Sheets.")
        except Exception as e:
            logging.error(f"Erro ao carregar usuários do Sheets: {e}")

        # Carregar Reservas
        try:
            reservas_sheet = spreadsheet.worksheet('Reservas')
            reservas_data = reservas_sheet.get_all_records()
            c.execute('DELETE FROM reservas')
            for res_row in reservas_data:
                c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (res_row.get('ID'), res_row.get('Usuario_id'), res_row.get('Carro_id'), res_row.get('Data'),
                           res_row.get('Hora Início'), res_row.get('Hora Fim'), res_row.get('Status'), res_row.get('Observacoes')))
            logging.info(f"Dados carregados da planilha 'Reservas': {len(reservas_data)} itens.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Aba 'Reservas' não encontrada na planilha. Reservas não carregadas do Sheets.")
        except Exception as e:
            logging.error(f"Erro ao carregar reservas do Sheets: {e}")

        conn.commit()
        logging.info('Sincronização inicial do Sheets para DB concluída.')
    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Planilha com ID '{GOOGLE_SHEET_ID}' não encontrada. Verifique o GOOGLE_SHEET_ID.")
    except Exception as e:
        logging.error(f"Erro geral ao carregar dados do Sheets: {e}")
    finally:
        if conn:
            conn.close()

def sync_to_sheets():
    """Sincroniza dados do DB local para as planilhas Google."""
    if not gspread_client:
        logging.warning('sync_to_sheets: Cliente gspread não inicializado. Sincronização com Sheets desativada.')
        return

    if not GOOGLE_SHEET_ID:
        logging.error('sync_to_sheets: GOOGLE_SHEET_ID não configurado. Sincronização com Sheets desativada.')
        return

    conn = get_db_connection()
    try:
        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEET_ID)

        # Sincronizar Carros
        try:
            carros_sheet = spreadsheet.worksheet('Carros')
            carros_sheet.clear()
            carros = conn.execute('SELECT * FROM carros').fetchall()
            if carros:
                headers = ['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponivel', 'Preco Diaria', 'thumbnail_url']
                data_to_append = [[c['id'], c['modelo'], c['ano'], c['cor'], c['placa'], 'Sim' if c['disponivel'] else 'Não', c['preco_diaria'], c['thumbnail_url']] for c in carros]
                carros_sheet.append_rows([headers] + data_to_append)
            logging.info(f"Dados do DB sincronizados para a planilha 'Carros': {len(carros)} itens.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Aba 'Carros' não encontrada na planilha. Carros não sincronizados para o Sheets.")
        except Exception as e:
            logging.error(f"Erro ao sincronizar carros para o Sheets: {e}")

        # Sincronizar Usuários
        try:
            usuarios_sheet = spreadsheet.worksheet('Usuarios')
            usuarios_sheet.clear()
            usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
            if usuarios:
                headers = ['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data Cadastro', 'Admin']
                data_to_append = [[u['id'], u['nome'], u['email'], u['senha_hash'], u['cpf'], u['telefone'], u['data_cadastro'], 'Sim' if u['is_admin'] else 'Não'] for u in usuarios]
                usuarios_sheet.append_rows([headers] + data_to_append)
            logging.info(f"Dados do DB sincronizados para a planilha 'Usuarios': {len(usuarios)} itens.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Aba 'Usuarios' não encontrada na planilha. Usuários não sincronizados para o Sheets.")
        except Exception as e:
            logging.error(f"Erro ao sincronizar usuários para o Sheets: {e}")

        # Sincronizar Reservas
        try:
            reservas_sheet = spreadsheet.worksheet('Reservas')
            reservas_sheet.clear()
            reservas = conn.execute('SELECT * FROM reservas').fetchall()
            if reservas:
                headers = ['ID', 'Usuario_id', 'Carro_id', 'Data', 'Hora Início', 'Hora Fim', 'Status', 'Observacoes']
                data_to_append = [[r['id'], r['usuario_id'], r['carro_id'], r['data_reserva'], r['hora_inicio'], r['hora_fim'], r['status'], r['observacoes']] for r in reservas]
                reservas_sheet.append_rows([headers] + data_to_append)
            logging.info(f"Dados do DB sincronizados para a planilha 'Reservas': {len(reservas)} itens.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Aba 'Reservas' não encontrada na planilha. Reservas não sincronizadas para o Sheets.")
        except Exception as e:
            logging.error(f"Erro ao sincronizar reservas para o Sheets: {e}")

        logging.info('Sincronização do DB para Sheets concluída.')
    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Planilha com ID '{GOOGLE_SHEET_ID}' não encontrada. Verifique o GOOGLE_SHEET_ID.")
    except Exception as e:
        logging.error(f"Erro geral ao sincronizar dados para o Sheets: {e}")
    finally:
        if conn:
            conn.close()

# --- Funções de Busca de Dados (DB local) ---
def get_user_by_id(user_id):
    """Busca um usuário pelo ID."""
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return user

def get_user_by_email(email):
    """Busca um usuário pelo email."""
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM usuarios WHERE email = ?', (email,)).fetchone()
    conn.close()
    return user

def get_car_by_id(car_id):
    """Busca um carro pelo ID."""
    conn = get_db_connection()
    car = conn.execute('SELECT * FROM carros WHERE id = ?', (car_id,)).fetchone()
    conn.close()
    return car

def get_all_cars():
    """Busca todos os carros."""
    try:
        conn = get_db_connection()
        cars = conn.execute('SELECT * FROM carros').fetchall()
        conn.close()
        return cars
    except Exception as e:
        logging.error(f"Erro ao carregar carros: {e}")
        return []

def get_reservas_db():
    """Busca todas as reservas com detalhes de usuário e carro do DB."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''SELECT 
                        r.id, 
                        u.nome as usuario_nome, 
                        c.modelo as carro_modelo, 
                        r.data_reserva, 
                        r.hora_inicio, 
                        r.hora_fim, 
                        r.status,
                        r.usuario_id,
                        r.carro_id,
                        r.observacoes
                     FROM reservas r 
                     JOIN usuarios u ON r.usuario_id = u.id 
                     JOIN carros c ON r.carro_id = c.id 
                     ORDER BY r.data_reserva DESC''')
        reservas = c.fetchall()
        conn.close()
        return reservas
    except Exception as e:
        logging.error(f"Erro ao carregar reservas do DB: {e}")
        return []

def get_usuarios_db():
    """Busca todos os usuários do DB."""
    try:
        conn = get_db_connection()
        usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
        conn.close()
        return usuarios
    except Exception as e:
        logging.error(f"Erro ao carregar usuários do DB: {e}")
        return []

# --- Rotas ---
@app.route('/health')
def health():
    """Endpoint de saúde para verificação do Railway."""
    return 'OK', 200

@app.route('/')
def index():
    """Redireciona para a página inicial."""
    return redirect(url_for('home'))

@app.route('/home')
def home():
    """Página inicial com lista de carros disponíveis (HTML inline)."""
    carros = get_all_cars()
    
    carros_html = ""
    if carros:
        for car in carros:
            thumbnail_tag = f'<img src="{car["thumbnail_url"]}" alt="{car["modelo"]}" style="width:100px;height:auto;margin-right:10px;">' if car["thumbnail_url"] else ''
            carros_html += f"""
            <div style="border: 1px solid #ccc; padding: 10px; margin-bottom: 10px; display: flex; align-items: center;">
                {thumbnail_tag}
                <div>
                    <h3>{car['modelo']} ({car['ano']})</h3>
                    <p>Cor: {car['cor']}</p>
                    <p>Placa: {car['placa']}</p>
                    <p>Preço Diária: R$ {car['preco_diaria']:.2f}</p>
                    <p>Disponível: {'Sim' if car['disponivel'] else 'Não'}</p>
                    <a href="{url_for('reservar', car_id=car['id'])}" style="background-color:#007bff;color:white;padding:8px 12px;text-decoration:none;border-radius:5px;">Reservar</a>
                </div>
            </div>
            """
    else:
        carros_html = "<p>Nenhum carro disponível no momento. Por favor, adicione carros via painel administrativo ou planilha.</p>"

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Home</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 800px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Carros Disponíveis</h1>
            {get_flashed_messages_html()}
            {carros_html}
        </div>
    </body>
    </html>
    """

def get_flashed_messages_html():
    messages = ""
    for category, message in session.pop('_flashed_messages', []):
        messages += f'<div class="flash {category}">{message}</div>'
    return messages

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    """Rota para registro de novos usuários (HTML inline)."""
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        senha = request.form['senha']
        cpf = request.form['cpf']
        telefone = request.form['telefone']

        if not nome or not email or not senha:
            flash('Todos os campos obrigatórios devem ser preenchidos.', 'error')
            return redirect(url_for('registro'))
        
        cleaned_cpf = ''.join(filter(str.isdigit, cpf))
        if not (cleaned_cpf.isdigit() and len(cleaned_cpf) == 11):
            flash('CPF inválido. Deve conter 11 dígitos.', 'error')
            return redirect(url_for('registro'))

        cleaned_telefone = ''.join(filter(str.isdigit, telefone))
        if not (cleaned_telefone.isdigit() and 10 <= len(cleaned_telefone) <= 11):
            flash('Telefone inválido. Deve conter 10 ou 11 dígitos.', 'error')
            return redirect(url_for('registro'))

        conn = get_db_connection()
        try:
            senha_hash = hashlib.sha256(senha.encode()).hexdigest()
            c = conn.cursor()
            c.execute('INSERT INTO usuarios (nome, email, senha_hash, cpf, telefone) VALUES (?, ?, ?, ?, ?)',
                         (nome, email, senha_hash, cleaned_cpf, cleaned_telefone))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            logging.info(f'Novo usuário registrado: {email}')
            sync_to_sheets() # Sincroniza para Sheets
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email ou CPF já cadastrado. Tente novamente com outros dados.', 'error')
        except Exception as e:
            flash(f'Erro ao registrar usuário: {e}', 'error')
            logging.error(f'Erro no registro de usuário: {e}')
        finally:
            conn.close()
        return redirect(url_for('registro')) # Redireciona em caso de erro para mostrar flash message

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Registro</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 500px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="text"], input[type="email"], input[type="password"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ background-color: #28a745; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background-color: #218838; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Registro de Usuário</h1>
            {get_flashed_messages_html()}
            <form method="POST">
                <label for="nome">Nome:</label>
                <input type="text" id="nome" name="nome" required>
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
                <label for="senha">Senha:</label>
                <input type="password" id="senha" name="senha" required>
                <label for="cpf">CPF:</label>
                <input type="text" id="cpf" name="cpf" placeholder="Ex: 123.456.789-00" required>
                <label for="telefone">Telefone:</label>
                <input type="text" id="telefone" name="telefone" placeholder="Ex: (DD) 99999-9999" required>
                <button type="submit">Registrar</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Rota para login de usuários (HTML inline)."""
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']

        conn = get_db_connection()
        try:
            user = conn.execute('SELECT * FROM usuarios WHERE email = ?', (email,)).fetchone()
            if user:
                senha_hash = hashlib.sha256(senha.encode()).hexdigest()
                if user['senha_hash'] == senha_hash:
                    session['user_id'] = user['id']
                    session['user_name'] = user['nome']
                    session['is_admin'] = user['is_admin']
                    flash('Login realizado com sucesso!', 'success')
                    logging.info(f'Usuário logado: {email}')
                    return redirect(url_for('home'))
                else:
                    flash('Senha incorreta.', 'error')
            else:
                flash('Email não encontrado.', 'error')
        except Exception as e:
            flash('Erro ao fazer login. Tente novamente.', 'error')
            logging.error(f'Erro no login: {e}')
        finally:
            conn.close()
        return redirect(url_for('login')) # Redireciona em caso de erro para mostrar flash message

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Login</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 400px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="email"], input[type="password"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background-color: #0056b3; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Login</h1>
            {get_flashed_messages_html()}
            <form method="POST">
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
                <label for="senha">Senha:</label>
                <input type="password" id="senha" name="senha" required>
                <button type="submit">Entrar</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.route('/logout')
def logout():
    """Rota para logout de usuários (HTML inline)."""
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.', 'info')
    logging.info('Usuário desconectado.')
    return redirect(url_for('home'))

@app.route('/reservar/<int:car_id>', methods=['GET', 'POST'])
def reservar(car_id):
    """Rota para reservar um carro (HTML inline)."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para fazer uma reserva.', 'warning')
        return redirect(url_for('login'))

    car = get_car_by_id(car_id)
    if not car:
        flash('Carro não encontrado.', 'error')
        return redirect(url_for('home'))

    if not car['disponivel']:
        flash('Este carro não está disponível para reserva no momento.', 'warning')
        return redirect(url_for('home'))

    if request.method == 'POST':
        data_reserva_str = request.form['data_reserva']
        hora_inicio_str = request.form['hora_inicio']
        hora_fim_str = request.form['hora_fim']
        observacoes = request.form.get('observacoes', '')

        try:
            data_reserva = datetime.strptime(data_reserva_str, '%Y-%m-%d').date()
            hora_inicio = datetime.strptime(hora_inicio_str, '%H:%M').time()
            hora_fim = datetime.strptime(hora_fim_str, '%H:%M').time()

            if data_reserva < datetime.now().date():
                flash('Não é possível reservar para uma data passada.', 'error')
                return redirect(url_for('reservar', car_id=car_id))
            if data_reserva == datetime.now().date() and hora_inicio < datetime.now().time():
                flash('Não é possível reservar para um horário passado no dia de hoje.', 'error')
                return redirect(url_for('reservar', car_id=car_id))
            if hora_inicio >= hora_fim:
                flash('A hora de início deve ser anterior à hora de fim.', 'error')
                return redirect(url_for('reservar', car_id=car_id))

            conn = get_db_connection()
            c = conn.cursor()
            c.execute('INSERT INTO reservas (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, observacoes) VALUES (?, ?, ?, ?, ?, ?)',
                         (session['user_id'], car_id, data_reserva, hora_inicio, hora_fim, observacoes))
            c.execute('UPDATE carros SET disponivel = FALSE WHERE id = ?', (car_id,))
            conn.commit()
            conn.close()

            flash('Reserva realizada com sucesso!', 'success')
            logging.info(f"Reserva criada: Usuário {session['user_id']} reservou carro {car_id} para {data_reserva}")
            sync_to_sheets() # Sincroniza após a reserva
            return redirect(url_for('minhas_reservas'))
        except ValueError:
            flash('Formato de data ou hora inválido.', 'error')
        except Exception as e:
            flash(f'Erro ao realizar reserva: {e}', 'error')
            logging.error(f'Erro ao realizar reserva: {e}')
        return redirect(url_for('reservar', car_id=car_id))

    thumbnail_tag = f'<img src="{car["thumbnail_url"]}" alt="{car["modelo"]}" style="width:150px;height:auto;margin-bottom:10px;">' if car["thumbnail_url"] else ''

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Reservar {car['modelo']}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="date"], input[type="time"], textarea {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background-color: #0056b3; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Reservar Carro: {car['modelo']}</h1>
            {get_flashed_messages_html()}
            {thumbnail_tag}
            <p>Ano: {car['ano']}</p>
            <p>Cor: {car['cor']}</p>
            <p>Placa: {car['placa']}</p>
            <p>Preço Diária: R$ {car['preco_diaria']:.2f}</p>
            <form method="POST">
                <label for="data_reserva">Data da Reserva:</label>
                <input type="date" id="data_reserva" name="data_reserva" required>
                <label for="hora_inicio">Hora de Início:</label>
                <input type="time" id="hora_inicio" name="hora_inicio" required>
                <label for="hora_fim">Hora de Fim:</label>
                <input type="time" id="hora_fim" name="hora_fim" required>
                <label for="observacoes">Observações (opcional):</label>
                <textarea id="observacoes" name="observacoes" rows="4"></textarea>
                <button type="submit">Confirmar Reserva</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.route('/minhas_reservas')
def minhas_reservas():
    """Exibe as reservas do usuário logado (HTML inline)."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para ver suas reservas.', 'warning')
        return redirect(url_for('login'))

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('''SELECT 
                        r.id, 
                        c.modelo as carro_modelo, 
                        r.data_reserva, 
                        r.hora_inicio, 
                        r.hora_fim, 
                        r.status,
                        r.observacoes,
                        c.thumbnail_url
                     FROM reservas r 
                     JOIN carros c ON r.carro_id = c.id 
                     WHERE r.usuario_id = ? 
                     ORDER BY r.data_reserva DESC''', (session['user_id'],))
        reservas = c.fetchall()
        conn.close()

        reservas_html = ""
        if reservas:
            for res in reservas:
                thumbnail_tag = f'<img src="{res["thumbnail_url"]}" alt="{res["carro_modelo"]}" style="width:80px;height:auto;margin-right:10px;">' if res["thumbnail_url"] else ''
                cancel_button = ""
                if res['status'] == 'pendente' or res['status'] == 'confirmada':
                    cancel_button = f'<a href="{url_for("cancelar_reserva", reserva_id=res["id"])}" style="background-color:#dc3545;color:white;padding:5px 10px;text-decoration:none;border-radius:5px;margin-left:10px;">Cancelar</a>'
                
                reservas_html += f"""
                <div style="border: 1px solid #ccc; padding: 10px; margin-bottom: 10px; display: flex; align-items: center;">
                    {thumbnail_tag}
                    <div>
                        <h3>Reserva ID: {res['id']} - Carro: {res['carro_modelo']}</h3>
                        <p>Data: {res['data_reserva']} | Horário: {res['hora_inicio']} - {res['hora_fim']}</p>
                        <p>Status: {res['status'].capitalize()}</p>
                        <p>Observações: {res['observacoes'] or 'Nenhuma'}</p>
                        {cancel_button}
                    </div>
                </div>
                """
        else:
            reservas_html = "<p>Você não possui reservas.</p>"

        return f"""
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>JG Minis - Minhas Reservas</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
                .container {{ max-width: 800px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba{'(0,0,0,0.1)'}; }}
                h1 {{ color: #333; }}
                .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
                .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
                .navbar a:hover {{ background-color: #ddd; color: black; }}
                .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
                .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
                .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
                .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="navbar">
                    <a href="{url_for('home')}">Home</a>
                    <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                    {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                    {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
                </div>
                <h1>Minhas Reservas</h1>
                {get_flashed_messages_html()}
                {reservas_html}
            </div>
        </body>
        </html>
        """
    except Exception as e:
        flash('Erro ao carregar suas reservas.', 'error')
        logging.error(f'Erro ao carregar minhas reservas: {e}')
        return redirect(url_for('home'))

@app.route('/cancelar_reserva/<int:reserva_id>')
def cancelar_reserva(reserva_id):
    """Cancela uma reserva (HTML inline)."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para cancelar uma reserva.', 'warning')
        return redirect(url_for('login'))

    conn = get_db_connection()
    try:
        reserva = conn.execute('SELECT * FROM reservas WHERE id = ? AND usuario_id = ?', (reserva_id, session['user_id'])).fetchone()

        if not reserva:
            flash('Reserva não encontrada ou você não tem permissão para cancelá-la.', 'error')
            return redirect(url_for('minhas_reservas'))

        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', ('cancelada', reserva_id))
        conn.execute('UPDATE carros SET disponivel = TRUE WHERE id = ?', (reserva['carro_id'],))
        conn.commit()
        flash('Reserva cancelada com sucesso!', 'success')
        logging.info(f"Reserva {reserva_id} cancelada pelo usuário {session['user_id']}")
        sync_to_sheets() # Sincroniza após o cancelamento
    except Exception as e:
        flash(f'Erro ao cancelar reserva: {e}', 'error')
        logging.error(f'Erro ao cancelar reserva {reserva_id}: {e}')
    finally:
        conn.close()
    return redirect(url_for('minhas_reservas'))

@app.route('/admin')
def admin_panel():
    """Painel administrativo (HTML inline)."""
    if not is_admin():
        flash('Acesso negado. Você não tem permissão de administrador.', 'error')
        return redirect(url_for('home'))
    
    reservas = get_reservas_db()
    usuarios = get_usuarios_db()
    carros = get_all_cars()

    reservas_table_rows = ""
    for res in reservas:
        thumbnail_tag = f'<img src="{res["thumbnail_url"]}" alt="{res["carro_modelo"]}" style="width:50px;height:auto;">' if res["thumbnail_url"] else ''
        reservas_table_rows += f"""
        <tr>
            <td>{res['id']}</td>
            <td>{res['usuario_nome']}</td>
            <td>{res['carro_modelo']} {thumbnail_tag}</td>
            <td>{res['data_reserva']}</td>
            <td>{res['hora_inicio']} - {res['hora_fim']}</td>
            <td>{res['status'].capitalize()}</td>
            <td>
                <a href="{url_for('admin_update_reserva_status', reserva_id=res['id'], status='confirmada')}" style="color:green;">Confirmar</a> |
                <a href="{url_for('admin_update_reserva_status', reserva_id=res['id'], status='cancelada')}" style="color:red;">Cancelar</a>
            </td>
        </tr>
        """

    usuarios_table_rows = ""
    for user in usuarios:
        usuarios_table_rows += f"""
        <tr>
            <td>{user['id']}</td>
            <td>{user['nome']}</td>
            <td>{user['email']}</td>
            <td>{user['cpf'] or ''}</td>
            <td>{user['telefone'] or ''}</td>
            <td>{'Sim' if user['is_admin'] else 'Não'}</td>
            <td>
                {'<a href="' + url_for('admin_promote_admin', user_id=user['id']) + '" style="color:blue;">Promover Admin</a>' if not user['is_admin'] else ''}
            </td>
        </tr>
        """

    carros_table_rows = ""
    for car in carros:
        thumbnail_tag = f'<img src="{car["thumbnail_url"]}" alt="{car["modelo"]}" style="width:50px;height:auto;">' if car["thumbnail_url"] else ''
        carros_table_rows += f"""
        <tr>
            <td>{car['id']}</td>
            <td>{car['modelo']} {thumbnail_tag}</td>
            <td>{car['ano']}</td>
            <td>{car['cor']}</td>
            <td>{car['placa']}</td>
            <td>{'Sim' if car['disponivel'] else 'Não'}</td>
            <td>R$ {car['preco_diaria']:.2f}</td>
            <td>
                <a href="{url_for('admin_edit_carro', car_id=car['id'])}" style="color:blue;">Editar</a> |
                <a href="{url_for('admin_delete_carro', car_id=car['id'])}" style="color:red;">Deletar</a>
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Painel Admin</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 1200px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1, h2 {{ color: #333; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
            .admin-actions a {{ margin-right: 10px; background-color: #007bff; color: white; padding: 8px 12px; text-decoration: none; border-radius: 5px; display: inline-block; margin-bottom: 10px; }}
            .admin-actions a:hover {{ background-color: #0056b3; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Painel Administrativo</h1>
            {get_flashed_messages_html()}

            <div class="admin-actions">
                <a href="{url_for('admin_add_carro')}">Adicionar Novo Carro</a>
                <a href="{url_for('admin_sync_sheets')}">Sincronizar com Google Sheets</a>
                <a href="{url_for('admin_backup_db')}">Gerar Backup DB (JSON)</a>
                <a href="{url_for('admin_restore_backup')}">Restaurar Backup DB (JSON)</a>
            </div>

            <h2>Reservas</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Usuário</th>
                        <th>Carro</th>
                        <th>Data</th>
                        <th>Horário</th>
                        <th>Status</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {reservas_table_rows}
                </tbody>
            </table>

            <h2>Usuários</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Nome</th>
                        <th>Email</th>
                        <th>CPF</th>
                        <th>Telefone</th>
                        <th>Admin</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {usuarios_table_rows}
                </tbody>
            </table>

            <h2>Carros</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Modelo</th>
                        <th>Ano</th>
                        <th>Cor</th>
                        <th>Placa</th>
                        <th>Disponível</th>
                        <th>Preço Diária</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {carros_table_rows}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """

@app.route('/admin/add_carro', methods=['GET', 'POST'])
def admin_add_carro():
    """Adiciona um novo carro (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    if request.method == 'POST':
        modelo = request.form['modelo']
        ano = request.form['ano']
        cor = request.form['cor']
        placa = request.form['placa']
        preco_diaria = request.form['preco_diaria']
        thumbnail_url = request.form.get('thumbnail_url', '')

        if not modelo or not ano or not cor or not placa or not preco_diaria:
            flash('Todos os campos obrigatórios devem ser preenchidos.', 'error')
            return redirect(url_for('admin_add_carro'))
        
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('INSERT INTO carros (modelo, ano, cor, placa, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?)',
                         (modelo, ano, cor, placa, float(preco_diaria), thumbnail_url))
            conn.commit()
            conn.close()
            flash('Carro adicionado com sucesso!', 'success')
            logging.info(f'Carro adicionado: {modelo} ({placa})')
            sync_to_sheets() # Sincroniza após adicionar carro
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao adicionar carro: {e}', 'error')
            logging.error(f'Erro ao adicionar carro: {e}')
        return redirect(url_for('admin_add_carro'))

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Adicionar Carro</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="text"], input[type="number"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ background-color: #28a745; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background-color: #218838; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Adicionar Novo Carro</h1>
            {get_flashed_messages_html()}
            <form method="POST">
                <label for="modelo">Modelo:</label>
                <input type="text" id="modelo" name="modelo" required>
                <label for="ano">Ano:</label>
                <input type="number" id="ano" name="ano" required>
                <label for="cor">Cor:</label>
                <input type="text" id="cor" name="cor" required>
                <label for="placa">Placa:</label>
                <input type="text" id="placa" name="placa" required>
                <label for="preco_diaria">Preço Diária:</label>
                <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" required>
                <label for="thumbnail_url">URL da Miniatura (opcional):</label>
                <input type="text" id="thumbnail_url" name="thumbnail_url">
                <button type="submit">Adicionar Carro</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.route('/admin/edit_carro/<int:car_id>', methods=['GET', 'POST'])
def admin_edit_carro(car_id):
    """Edita um carro existente (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    car = get_car_by_id(car_id)
    if not car:
        flash('Carro não encontrado.', 'error')
        return redirect(url_for('admin_panel'))

    if request.method == 'POST':
        modelo = request.form['modelo']
        ano = request.form['ano']
        cor = request.form['cor']
        placa = request.form['placa']
        preco_diaria = request.form['preco_diaria']
        disponivel = 'disponivel' in request.form
        thumbnail_url = request.form.get('thumbnail_url', '')

        if not modelo or not ano or not cor or not placa or not preco_diaria:
            flash('Todos os campos obrigatórios devem ser preenchidos.', 'error')
            return redirect(url_for('admin_edit_carro', car_id=car_id))
        
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('UPDATE carros SET modelo = ?, ano = ?, cor = ?, placa = ?, preco_diaria = ?, disponivel = ?, thumbnail_url = ? WHERE id = ?',
                         (modelo, ano, cor, placa, float(preco_diaria), disponivel, thumbnail_url, car_id))
            conn.commit()
            conn.close()
            flash('Carro atualizado com sucesso!', 'success')
            logging.info(f'Carro {car_id} atualizado: {modelo} ({placa})')
            sync_to_sheets() # Sincroniza após editar carro
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada para outro carro. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao editar carro: {e}', 'error')
            logging.error(f'Erro ao editar carro {car_id}: {e}')
        return redirect(url_for('admin_edit_carro', car_id=car_id))

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Editar Carro</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="text"], input[type="number"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            input[type="checkbox"] {{ margin-right: 5px; }}
            button {{ background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background-color: #0056b3; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Editar Carro: {car['modelo']}</h1>
            {get_flashed_messages_html()}
            <form method="POST">
                <label for="modelo">Modelo:</label>
                <input type="text" id="modelo" name="modelo" value="{car['modelo']}" required>
                <label for="ano">Ano:</label>
                <input type="number" id="ano" name="ano" value="{car['ano']}" required>
                <label for="cor">Cor:</label>
                <input type="text" id="cor" name="cor" value="{car['cor']}" required>
                <label for="placa">Placa:</label>
                <input type="text" id="placa" name="placa" value="{car['placa']}" required>
                <label for="preco_diaria">Preço Diária:</label>
                <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" value="{car['preco_diaria']}" required>
                <label for="thumbnail_url">URL da Miniatura (opcional):</label>
                <input type="text" id="thumbnail_url" name="thumbnail_url" value="{car['thumbnail_url'] or ''}">
                <label>
                    <input type="checkbox" id="disponivel" name="disponivel" {'checked' if car['disponivel'] else ''}>
                    Disponível
                </label><br><br>
                <button type="submit">Atualizar Carro</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.route('/admin/delete_carro/<int:car_id>')
def admin_delete_carro(car_id):
    """Deleta um carro (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        c = conn.cursor()
        reservas_ativas = c.execute('SELECT COUNT(*) FROM reservas WHERE carro_id = ? AND status IN (?, ?)', (car_id, 'pendente', 'confirmada')).fetchone()[0]
        if reservas_ativas > 0:
            flash(f'Não é possível deletar o carro. Existem {reservas_ativas} reservas ativas para ele.', 'error')
            return redirect(url_for('admin_panel'))

        c.execute('DELETE FROM carros WHERE id = ?', (car_id,))
        conn.commit()
        flash('Carro deletado com sucesso!', 'success')
        logging.info(f'Carro {car_id} deletado.')
        sync_to_sheets() # Sincroniza após deletar carro
    except Exception as e:
        flash(f'Erro ao deletar carro: {e}', 'error')
        logging.error(f'Erro ao deletar carro {car_id}: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/update_reserva_status/<int:reserva_id>/<string:status>')
def admin_update_reserva_status(reserva_id, status):
    """Atualiza o status de uma reserva (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('UPDATE reservas SET status = ? WHERE id = ?', (status, reserva_id))
        conn.commit()
        flash(f'Status da reserva {reserva_id} atualizado para "{status}" com sucesso!', 'success')
        logging.info(f'Reserva {reserva_id} status atualizado para: {status}')
        sync_to_sheets() # Sincroniza após atualizar status
    except Exception as e:
        flash(f'Erro ao atualizar status da reserva: {e}', 'error')
        logging.error(f'Erro ao atualizar status da reserva {reserva_id}: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/promote_admin/<int:user_id>')
def admin_promote_admin(user_id):
    """Promove um usuário a administrador (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('UPDATE usuarios SET is_admin = TRUE WHERE id = ?', (user_id,))
        conn.commit()
        flash(f'Usuário {user_id} promovido a administrador com sucesso!', 'success')
        logging.info(f'Usuário {user_id} promovido a admin.')
        sync_to_sheets() # Sincroniza após promover admin
    except Exception as e:
        flash(f'Erro ao promover usuário a admin: {e}', 'error')
        logging.error(f'Erro ao promover usuário {user_id} a admin: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/sync_sheets')
def admin_sync_sheets():
    """Sincroniza todos os dados com o Google Sheets (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    if not gspread_client or not GOOGLE_SHEET_ID:
        flash('Sincronização com Google Sheets desativada (credenciais ou ID ausentes).', 'error')
        return redirect(url_for('admin_panel'))

    try:
        load_from_sheets() # Carrega do Sheets para o DB
        sync_to_sheets()   # Sincroniza do DB para o Sheets (garante consistência)
        flash('Dados sincronizados com o Google Sheets com sucesso!', 'success')
        logging.info('Todas as abas do Google Sheets sincronizadas.')
    except Exception as e:
        flash(f'Erro geral ao sincronizar com Google Sheets: {e}', 'error')
        logging.error(f'Erro geral ao sincronizar com Google Sheets: {e}')
    return redirect(url_for('admin_panel'))

@app.route('/admin/backup_db')
def admin_backup_db():
    """Gera um backup do banco de dados em formato JSON (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        reservas = conn.execute('SELECT * FROM reservas').fetchall()
        usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
        carros = conn.execute('SELECT * FROM carros').fetchall()

        reservas_list = [dict(r) for r in reservas]
        usuarios_list = [dict(u) for u in usuarios]
        carros_list = [dict(c) for c in carros]

        backup_data = {
            'timestamp': datetime.now().isoformat(),
            'reservas': reservas_list,
            'usuarios': usuarios_list,
            'carros': carros_list
        }
        
        backup_json_str = json.dumps(backup_data, indent=4, ensure_ascii=False)
        backup_data['hash'] = hashlib.sha256(backup_json_str.encode()).hexdigest()

        backup_json_str_final = json.dumps(backup_data, indent=4, ensure_ascii=False)

        buffer = BytesIO()
        buffer.write(backup_json_str_final.encode('utf-8'))
        buffer.seek(0)

        logging.info(f"Backup gerado: {len(reservas_list)} reservas, {len(usuarios_list)} usuários, {len(carros_list)} carros exportados.")
        flash('Backup do banco de dados gerado com sucesso!', 'success')
        return send_file(buffer, as_attachment=True, download_name=f'jgminis_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json', mimetype='application/json')
    except Exception as e:
        flash(f'Erro ao gerar backup do banco de dados: {e}', 'error')
        logging.error(f'Erro ao gerar backup do DB: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/restore_backup', methods=['GET', 'POST'])
def admin_restore_backup():
    """Restaura o banco de dados a partir de um arquivo JSON de backup (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    if request.method == 'POST':
        if 'backup_file' not in request.files:
            flash('Nenhum arquivo de backup selecionado.', 'error')
            return redirect(url_for('admin_restore_backup'))
        
        file = request.files['backup_file']
        if file.filename == '':
            flash('Nenhum arquivo de backup selecionado.', 'error')
            return redirect(url_for('admin_restore_backup'))
        
        if file and file.filename.endswith('.json'):
            try:
                backup_content = file.read().decode('utf-8')
                backup_data = json.loads(backup_content)

                received_hash = backup_data.pop('hash', None)
                if received_hash:
                    calculated_hash = hashlib.sha256(json.dumps(backup_data, indent=4, ensure_ascii=False).encode()).hexdigest()
                    if received_hash != calculated_hash:
                        flash('Erro de integridade do backup: hash não corresponde.', 'error')
                        logging.error('Erro de integridade do backup: hash não corresponde.')
                        return redirect(url_for('admin_restore_backup'))
                else:
                    logging.warning('Backup sem hash de integridade. Prosseguindo com a restauração.')

                conn = get_db_connection()
                c = conn.cursor()

                c.execute('DELETE FROM reservas')
                c.execute('DELETE FROM usuarios')
                c.execute('DELETE FROM carros')

                for car_data in backup_data.get('carros', []):
                    c.execute('INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'], car_data['placa'], car_data['disponivel'], car_data['preco_diaria'], car_data.get('thumbnail_url')))
                
                for user_data in backup_data.get('usuarios', []):
                    c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (user_data['id'], user_data['nome'], user_data['email'], user_data['senha_hash'], user_data['cpf'], user_data['telefone'], user_data['data_cadastro'], user_data['is_admin']))
                
                for reserva_data in backup_data.get('reservas', []):
                    c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (reserva_data['id'], reserva_data['usuario_id'], reserva_data['carro_id'], reserva_data['data_reserva'], reserva_data['hora_inicio'], reserva_data['hora_fim'], reserva_data['status'], reserva_data['observacoes']))
                
                conn.commit()
                flash('Backup restaurado com sucesso!', 'success')
                logging.info('Backup restaurado com sucesso.')
                sync_to_sheets() # Sincroniza para Sheets após restaurar DB

            except json.JSONDecodeError:
                flash('Arquivo de backup inválido: não é um JSON válido.', 'error')
                logging.error('Erro: Arquivo de backup inválido (JSONDecodeError).')
            except KeyError as ke:
                flash(f'Arquivo de backup inválido: chave ausente - {ke}.', 'error')
                logging.error(f'Erro: Arquivo de backup inválido (KeyError: {ke}).')
            except Exception as e:
                flash(f'Erro ao restaurar backup: {e}', 'error')
                logging.error(f'Erro ao restaurar backup: {e}')
            finally:
                conn.close()
        else:
            flash('Por favor, selecione um arquivo JSON válido.', 'error')
        return redirect(url_for('admin_restore_backup'))

    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Restaurar Backup</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; }}
            .container {{ max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            input[type="file"] {{ margin-bottom: 15px; }}
            button {{ background-color: #ffc107; color: black; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }}
            button:hover {{ background-color: #e0a800; }}
            .navbar {{ background-color: #333; overflow: hidden; margin-bottom: 20px; }}
            .navbar a {{ float: left; display: block; color: #f2f2f2; text-align: center; padding: 14px 16px; text-decoration: none; }}
            .navbar a:hover {{ background-color: #ddd; color: black; }}
            .flash {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash.success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash.error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash.info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="navbar">
                <a href="{url_for('home')}">Home</a>
                <a href="{url_for('minhas_reservas')}">Minhas Reservas</a>
                {'<a href="' + url_for('admin_panel') + '">Admin</a>' if is_admin() else ''}
                {'<a href="' + url_for('logout') + '">Logout</a>' if 'user_id' in session else '<a href="' + url_for('login') + '">Login</a><a href="' + url_for('registro') + '">Registro</a>'}
            </div>
            <h1>Restaurar Backup do Banco de Dados</h1>
            {get_flashed_messages_html()}
            <form method="POST" enctype="multipart/form-data">
                <label for="backup_file">Selecione o arquivo JSON de backup:</label>
                <input type="file" id="backup_file" name="backup_file" accept=".json" required>
                <button type="submit">Restaurar</button>
            </form>
        </div>
    </body>
    </html>
    """

# --- Tratamento de Erros ---
@app.errorhandler(404)
def page_not_found(e):
    """Trata erros 404 (Página não encontrada) (HTML inline)."""
    logging.warning(f"404 Not Found: {request.url}")
    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>404 - Página Não Encontrada</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; text-align: center; }}
            .container {{ max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #dc3545; }}
            p {{ color: #666; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Erro 404: Página Não Encontrada</h1>
            <p>A página que você está procurando não existe.</p>
            <p><a href="{url_for('home')}">Voltar para a página inicial</a></p>
        </div>
    </body>
    </html>
    """, 404

@app.errorhandler(500)
def internal_server_error(e):
    """Trata erros 500 (Erro interno do servidor) (HTML inline)."""
    logging.error(f"500 Internal Server Error: {e}", exc_info=True)
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>500 - Erro Interno do Servidor</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; text-align: center; }}
            .container {{ max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #dc3545; }}
            p {{ color: #666; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Erro 500: Erro Interno do Servidor</h1>
            <p>Ocorreu um erro inesperado. Nossa equipe já foi notificada.</p>
            <p><a href="{url_for('home')}">Voltar para a página inicial</a></p>
            {get_flashed_messages_html()}
        </div>
    </body>
    </html>
    """, 500

# Inicializa o cliente gspread e o DB no nível do módulo
gspread_client = init_gspread_client()
init_db()
load_from_sheets() # Carrega dados da planilha para o DB na inicialização

# O Gunicorn (servidor de produção) irá chamar a instância 'app' diretamente.
# Não precisamos do bloco if __name__ == '__main__': app.run() para deploy.
if __name__ == '__main__':
    pass
