from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify, send_file
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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_jgminis_v4.3.18')

# Caminho do banco de dados (persistente no Railway via /tmp)
DATABASE_PATH = os.environ.get('DATABASE_PATH', '/tmp/jgminis.db')

# --- Funções de Banco de Dados ---
def init_db():
    """Inicializa o banco de dados SQLite, criando tabelas se não existirem."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_PATH)
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

        # Tabela de Carros (adicionada coluna thumbnail_url)
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

        # Tabela de Reservas (adicionada coluna thumbnail_url)
        c.execute('''CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            carro_id INTEGER,
            data_reserva DATE NOT NULL,
            hora_inicio TIME NOT NULL,
            hora_fim TIME NOT NULL,
            status TEXT DEFAULT 'pendente',
            observacoes TEXT,
            thumbnail_url TEXT,
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id),
            FOREIGN KEY (carro_id) REFERENCES carros (id)
        )''')

        conn.commit()

        # Verifica contagens existentes para logs
        c.execute('SELECT COUNT(*) FROM reservas')
        reservas_count = c.fetchone()[0]
        if reservas_count == 0:
            logging.warning('DB inicializado: 0 reservas encontradas. Considere restaurar de um backup JSON.')
        else:
            logging.info(f'DB inicializado: {reservas_count} reservas preservadas.')

        c.execute('SELECT COUNT(*) FROM usuarios')
        usuarios_count = c.fetchone()[0]
        if usuarios_count == 0:
            logging.warning('DB inicializado: 0 usuários encontrados. Criando usuário admin padrão.')
            # Cria admin padrão se nenhum usuário existir
            senha_hash = hashlib.sha256('admin123'.encode()).hexdigest()
            c.execute('INSERT INTO usuarios (nome, email, senha_hash, is_admin) VALUES (?, ?, ?, ?)',
                      ('Admin Padrão', 'admin@jgminis.com.br', senha_hash, True))
            conn.commit()
            logging.info('DB inicializado: Usuário admin padrão criado (admin@jgminis.com.br, senha: admin123).')
        else:
            logging.info(f'DB inicializado: {usuarios_count} cadastros preservados.')

        c.execute('SELECT COUNT(*) FROM carros')
        carros_count = c.fetchone()[0]
        if carros_count == 0:
            logging.warning('DB inicializado: 0 carros encontrados. Adicione carros ou restaure de um backup JSON.')
        else:
            logging.info(f'DB inicializado: {carros_count} carros preservados.')

        logging.info('App bootado com sucesso.')
    except sqlite3.Error as e:
        logging.error(f"Erro ao inicializar o banco de dados: {e}")
    finally:
        if conn:
            conn.close()

# Inicializa o DB no nível do módulo para garantir que esteja pronto para Gunicorn
init_db()

def get_db_connection():
    """Retorna uma conexão com o banco de dados."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row # Permite acessar colunas por nome
    return conn

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
    """Busca todos os carros, incluindo thumbnail_url."""
    try:
        conn = get_db_connection()
        cars = conn.execute('SELECT * FROM carros ORDER BY modelo').fetchall()
        conn.close()
        if len(cars) == 0:
            logging.info('DB vazio: 0 carros encontrados.')
        return cars
    except Exception as e:
        logging.error(f"Erro ao carregar carros: {e}")
        return []

def get_reservas():
    """Busca todas as reservas com detalhes de usuário e carro, incluindo thumbnail_url."""
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
                        r.observacoes,
                        r.thumbnail_url,
                        c.thumbnail_url as carro_thumbnail
                     FROM reservas r 
                     JOIN usuarios u ON r.usuario_id = u.id 
                     JOIN carros c ON r.carro_id = c.id 
                     ORDER BY r.data_reserva DESC''')
        reservas = c.fetchall()
        if len(reservas) == 0:
            logging.info('DB vazio: 0 reservas encontradas.')
        else:
            logging.info(f'Reservas: Encontradas {len(reservas)} registros no DB.')
        conn.close()
        return reservas
    except Exception as e:
        logging.error(f"Erro ao carregar reservas: {e}")
        return []

def get_usuarios():
    """Busca todos os usuários."""
    try:
        conn = get_db_connection()
        usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
        conn.close()
        if len(usuarios) == 0:
            logging.info('DB vazio: 0 usuários encontrados.')
        else:
            logging.info(f'Usuários: Encontrados {len(usuarios)} registros no DB.')
        return usuarios
    except Exception as e:
        logging.error(f"Erro ao carregar usuários: {e}")
        return []

# --- Funções de Autenticação e Autorização ---
def is_admin():
    """Verifica se o usuário logado é administrador."""
    if 'user_id' in session:
        user = get_user_by_id(session['user_id'])
        return user and user['is_admin']
    return False

# --- Funções de Integração com Google Sheets (gspread) ---
gspread_client = None

def init_gspread_client():
    """Inicializa o cliente gspread para acesso às planilhas."""
    global gspread_client
    if gspread_client:
        return gspread_client

    if not gspread or not Credentials:
        logging.warning('gspread: Bibliotecas gspread ou google-auth não disponíveis. Sincronização com Sheets desativada.')
        return None

    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        
        if os.environ.get('GOOGLE_CREDENTIALS_JSON'):
            creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            logging.info('gspread: Credenciais carregadas da variável de ambiente.')
        elif os.path.exists('service_account.json'):
            creds = Credentials.from_service_account_file('service_account.json', scopes=scope)
            logging.info('gspread: Credenciais carregadas do arquivo service_account.json.')
        else:
            logging.error('gspread: Nenhuma credencial encontrada. Sincronização com Sheets desativada.')
            return None
            
        gspread_client = gspread.authorize(creds)
        logging.info('gspread: Autenticação bem-sucedida.')
        return gspread_client
    except Exception as e:
        logging.error(f'gspread: Erro na autenticação gspread: {e}. Sincronização com Sheets desativada.')
        return None

gspread_client = init_gspread_client()

GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', 'SUA_SHEET_ID_AQUI') 

def sync_reservas_to_sheets():
    """Sincroniza as reservas do DB para a aba 'Reservas' do Google Sheets, incluindo thumbnail_url."""
    if not gspread_client:
        logging.warning('Sync reservas pulado: Cliente gspread não inicializado ou credenciais ausentes.')
        return

    if GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning('Sync reservas pulado: GOOGLE_SHEET_ID não configurado.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Reservas')
        sheet.clear()

        reservas = get_reservas()
        if reservas:
            headers = ['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status', 'Thumbnail URL']
            data_to_append = [
                [
                    str(r['id']),
                    str(r['usuario_nome']),
                    str(r['carro_modelo']),
                    str(r['data_reserva']),
                    str(r['hora_inicio']),
                    str(r['hora_fim']),
                    str(r['status']),
                    str(r['thumbnail_url'] or '')
                ] for r in reservas
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync reservas: {len(reservas)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status', 'Thumbnail URL']])
            logging.info('Sync reservas: Nenhuma reserva para sincronizar. Aba limpa e cabeçalhos adicionados.')
    except Exception as e:
        logging.error(f'Erro na sincronização de reservas com Google Sheets: {e}')
        flash('Erro ao sincronizar reservas com o Google Sheets.', 'error')

def sync_usuarios_to_sheets():
    """Sincroniza os usuários do DB para a aba 'Usuarios' do Google Sheets."""
    if not gspread_client:
        logging.warning('Sync usuários pulado: Cliente gspread não inicializado ou credenciais ausentes.')
        return

    if GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning('Sync usuários pulado: GOOGLE_SHEET_ID não configurado.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Usuarios')
        sheet.clear()

        usuarios = get_usuarios()
        if usuarios:
            headers = ['ID', 'Nome', 'Email', 'CPF', 'Telefone', 'Data Cadastro', 'Admin']
            data_to_append = [
                [
                    str(u['id']),
                    str(u['nome']),
                    str(u['email']),
                    str(u['cpf'] or ''),
                    str(u['telefone'] or ''),
                    str(u['data_cadastro']),
                    'Sim' if u['is_admin'] else 'Não'
                ] for u in usuarios
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync usuários: {len(usuarios)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Nome', 'Email', 'CPF', 'Telefone', 'Data Cadastro', 'Admin']])
            logging.info('Sync usuários: Nenhum usuário para sincronizar. Aba limpa e cabeçalhos adicionados.')
    except Exception as e:
        logging.error(f'Erro na sincronização de usuários com Google Sheets: {e}')
        flash('Erro ao sincronizar usuários com o Google Sheets.', 'error')

def sync_carros_to_sheets():
    """Sincroniza os carros do DB para a aba 'Carros' do Google Sheets, incluindo thumbnail_url."""
    if not gspread_client:
        logging.warning('Sync carros pulado: Cliente gspread não inicializado ou credenciais ausentes.')
        return

    if GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning('Sync carros pulado: GOOGLE_SHEET_ID não configurado.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Carros')
        sheet.clear()

        carros = get_all_cars()
        if carros:
            headers = ['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponível', 'Preço Diária', 'Thumbnail URL']
            data_to_append = [
                [
                    str(c['id']),
                    str(c['modelo']),
                    str(c['ano']),
                    str(c['cor']),
                    str(c['placa']),
                    'Sim' if c['disponivel'] else 'Não',
                    f"R$ {c['preco_diaria']:.2f}",
                    str(c['thumbnail_url'] or '')
                ] for c in carros
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync carros: {len(carros)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponível', 'Preço Diária', 'Thumbnail URL']])
            logging.info('Sync carros: Nenhum carro para sincronizar. Aba limpa e cabeçalhos adicionados.')
    except Exception as e:
        logging.error(f'Erro na sincronização de carros com Google Sheets: {e}')
        flash('Erro ao sincronizar carros com o Google Sheets.', 'error')

def sync_all_to_sheets():
    """Sincroniza todos os dados com o Google Sheets."""
    sync_reservas_to_sheets()
    sync_usuarios_to_sheets()
    sync_carros_to_sheets()

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
    """Página inicial com lista de carros disponíveis (com thumbnails)."""
    try:
        carros = get_all_cars()
        # HTML inline with thumbs
        html = '''
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <title>JG Minis - Home</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }
                h1 { color: #333; }
                .carro { border: 1px solid #ddd; margin: 10px; padding: 10px; background: white; display: inline-block; width: 300px; vertical-align: top; }
                .thumb { width: 100%; height: 150px; object-fit: cover; margin-bottom: 10px; }
                .preco { font-weight: bold; color: #28a745; }
                a { text-decoration: none; color: #007bff; margin-right: 10px; }
                .flash { background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; }
                .flash.error { background-color: #dc3545; color: white; }
                .flash.success { background-color: #28a745; color: white; }
                .flash.info { background-color: #17a2b8; color: white; }
            </style>
        </head>
        <body>
            '''
        if 'user_id' in session:
            html += f'<p>Bem-vindo, {session["user_name"]}! <a href="/logout">Logout</a>'
            if session['is_admin']:
                html += ' | <a href="/admin">Admin</a>'
            html += '</p>'
        else:
            html += '<p><a href="/login">Login</a> | <a href="/registro">Registrar</a></p>'

        # Flash messages
        if session.get('_flashes'):
            for category, message in session.pop('_flashes'):
                html += f'<div class="flash {category}">{message}</div>'

        html += '<h1>Carros Disponíveis</h1>'
        
        if not carros:
            html += '''
            <p>Nenhum carro disponível no momento. Acesse <a href="/admin">/admin</a> para adicionar.</p>
            '''
        else:
            for car in carros:
                thumb = car['thumbnail_url'] or 'https://via.placeholder.com/300x150?text=No+Image'
                html += f'''
                <div class="carro">
                    <img src="{thumb}" alt="{car['modelo']}" class="thumb">
                    <h3>{car['modelo']} - {car['ano']}</h3>
                    <p>Cor: {car['cor']} | Placa: {car['placa']}</p>
                    <p class="preco">Preço Diária: R$ {car['preco_diaria']:.2f}</p>
                    <a href="/reservar/{car['id']}">Reservar</a>
                </div>
                '''
        html += '''
        </body>
        </html>
        '''
        return html, 200
    except Exception as e:
        logging.error(f"Erro ao carregar carros na home: {e}")
        flash('Erro ao carregar a lista de carros. Tente novamente mais tarde.', 'error')
        # Fallback HTML
        return '''
        <!DOCTYPE html>
        <html>
        <head><title>Error</title></head>
        <body><h1>Erro ao carregar home. Tente /login.</h1></body>
        </html>
        ''', 500

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    """Rota para registro de novos usuários."""
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        senha = request.form['senha']
        cpf = request.form['cpf']
        telefone = request.form['telefone']

        # Validações básicas
        if not nome or not email or not senha:
            flash('Todos os campos obrigatórios devem ser preenchidos.', 'error')
            return get_registro_form_html()
        
        # Validação de CPF (11 dígitos)
        cleaned_cpf = ''.join(filter(str.isdigit, cpf))
        if not (cleaned_cpf.isdigit() and len(cleaned_cpf) == 11):
            flash('CPF inválido. Deve conter 11 dígitos.', 'error')
            return get_registro_form_html()

        # Validação de Telefone (10 ou 11 dígitos)
        cleaned_telefone = ''.join(filter(str.isdigit, telefone))
        if not (cleaned_telefone.isdigit() and 10 <= len(cleaned_telefone) <= 11):
            flash('Telefone inválido. Deve conter 10 ou 11 dígitos.', 'error')
            return get_registro_form_html()

        conn = get_db_connection()
        try:
            senha_hash = hashlib.sha256(senha.encode()).hexdigest()
            conn.execute('INSERT INTO usuarios (nome, email, senha_hash, cpf, telefone) VALUES (?, ?, ?, ?, ?)',
                         (nome, email, senha_hash, cleaned_cpf, cleaned_telefone))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            logging.info(f'Novo usuário registrado: {email}')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email ou CPF já cadastrado. Tente novamente com outros dados.', 'error')
        except Exception as e:
            flash(f'Erro ao registrar usuário: {e}', 'error')
            logging.error(f'Erro no registro de usuário: {e}')
        finally:
            conn.close()
        return get_registro_form_html()
    return get_registro_form_html()

def get_registro_form_html():
    """HTML inline for registro form."""
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Registro - JG Minis</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 80vh; }}
            .container {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 450px; width: 100%; }}
            h1 {{ text-align: center; color: #333; margin-bottom: 20px; }}
            input[type="text"], input[type="email"], input[type="password"] {{ width: calc(100% - 20px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ background: #28a745; color: white; padding: 12px 15px; width: 100%; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 15px; }}
            button:hover {{ background: #218838; }}
            p {{ text-align: center; margin-top: 20px; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Registro</h1>
            {flash_messages}
            <form method="POST">
                <input type="text" name="nome" placeholder="Nome Completo" required>
                <input type="email" name="email" placeholder="Email" required>
                <input type="password" name="senha" placeholder="Senha" required>
                <input type="text" name="cpf" placeholder="CPF (11 dígitos)" required>
                <input type="text" name="telefone" placeholder="Telefone" required>
                <button type="submit">Registrar</button>
            </form>
            <p><a href="/login">Já tem conta? Faça Login</a></p>
        </div>
    </body>
    </html>
    '''

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Rota para login de usuários."""
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
        return get_login_form_html()
    return get_login_form_html()

def get_login_form_html():
    """HTML inline for login form."""
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Login - JG Minis</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 80vh; }}
            .container {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; width: 100%; }}
            h1 {{ text-align: center; color: #333; margin-bottom: 20px; }}
            input[type="email"], input[type="password"] {{ width: calc(100% - 20px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ background: #007bff; color: white; padding: 12px 15px; width: 100%; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 15px; }}
            button:hover {{ background: #0056b3; }}
            p {{ text-align: center; margin-top: 20px; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Login</h1>
            {flash_messages}
            <form method="POST">
                <input type="email" name="email" placeholder="Email" required>
                <input type="password" name="senha" placeholder="Senha" required>
                <button type="submit">Login</button>
            </form>
            <p><a href="/registro">Não tem conta? Registre-se</a></p>
        </div>
    </body>
    </html>
    '''

@app.route('/logout')
def logout():
    """Rota para logout de usuários."""
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.', 'info')
    logging.info('Usuário desconectado.')
    return redirect(url_for('home'))

@app.route('/reservar/<int:car_id>', methods=['GET', 'POST'])
def reservar(car_id):
    """Rota para reservar um carro."""
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
                return get_reservar_form_html(car)
            if data_reserva == datetime.now().date() and hora_inicio < datetime.now().time():
                flash('Não é possível reservar para um horário passado no dia de hoje.', 'error')
                return get_reservar_form_html(car)
            if hora_inicio >= hora_fim:
                flash('A hora de início deve ser anterior à hora de fim.', 'error')
                return get_reservar_form_html(car)

            conn = get_db_connection()
            conn.execute('INSERT INTO reservas (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, observacoes, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (session['user_id'], car_id, data_reserva, hora_inicio, hora_fim, observacoes, car['thumbnail_url']))
            conn.execute('UPDATE carros SET disponivel = FALSE WHERE id = ?', (car_id,))
            conn.commit()
            conn.close()

            flash('Reserva realizada com sucesso!', 'success')
            logging.info(f"Reserva criada: Usuário {session['user_id']} reservou carro {car_id} para {data_reserva}")
            sync_reservas_to_sheets()
            sync_carros_to_sheets()
            return redirect(url_for('minhas_reservas'))
        except ValueError:
            flash('Formato de data ou hora inválido.', 'error')
        except Exception as e:
            flash(f'Erro ao realizar reserva: {e}', 'error')
            logging.error(f'Erro ao realizar reserva: {e}')
        return get_reservar_form_html(car)
    return get_reservar_form_html(car)

def get_reservar_form_html(car):
    """HTML inline for reservar form."""
    thumb = car['thumbnail_url'] or 'https://via.placeholder.com/200x150?text=No+Image'
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Reservar {car['modelo']} - JG Minis</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }}
            .carro-info {{ background: white; padding: 20px; border-radius: 8px; max-width: 400px; margin: 0 auto 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .thumb {{ width: 100%; height: 150px; object-fit: cover; border-radius: 8px; margin-bottom: 10px; }}
            form {{ max-width: 400px; background: white; padding: 20px; border-radius: 8px; margin: 0 auto; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            input[type="date"], input[type="time"], textarea {{ width: calc(100% - 20px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ background: #28a745; color: white; padding: 12px 15px; width: 100%; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 15px; }}
            button:hover {{ background: #218838; }}
            p {{ text-align: center; margin-top: 20px; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <div class="carro-info">
            <h1>Reservar {car['modelo']}</h1>
            {flash_messages}
            <img src="{thumb}" alt="{car['modelo']}" class="thumb">
        </div>
        <form method="POST">
            <label for="data_reserva">Data da Reserva:</label>
            <input type="date" name="data_reserva" id="data_reserva" required>
            <label for="hora_inicio">Hora de Início:</label>
            <input type="time" name="hora_inicio" id="hora_inicio" required>
            <label for="hora_fim">Hora de Fim:</label>
            <input type="time" name="hora_fim" id="hora_fim" required>
            <label for="observacoes">Observações (opcional):</label>
            <textarea name="observacoes" id="observacoes" placeholder="Observações"></textarea>
            <button type="submit">Reservar</button>
        </form>
        <p><a href="/home">Voltar para Home</a></p>
    </body>
    </html>
    '''

@app.route('/minhas_reservas')
def minhas_reservas():
    """Exibe as reservas do usuário logado (com thumbnails)."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para ver suas reservas.', 'warning')
        return redirect(url_for('login'))

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''SELECT 
                        r.id, 
                        c.modelo as carro_modelo, 
                        r.data_reserva, 
                        r.hora_inicio, 
                        r.hora_fim, 
                        r.status,
                        r.observacoes,
                        r.thumbnail_url
                     FROM reservas r 
                     JOIN carros c ON r.carro_id = c.id 
                     WHERE r.usuario_id = ? 
                     ORDER BY r.data_reserva DESC''', (session['user_id'],))
        reservas = c.fetchall()
        conn.close()
        
        flash_messages = ''
        if session.get('_flashes'):
            for category, message in session.pop('_flashes'):
                flash_messages += f'<div class="flash {category}">{message}</div>'

        html = f'''
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <title>Minhas Reservas - JG Minis</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }}
                h1 {{ color: #333; }}
                .reserva {{ border: 1px solid #ddd; margin: 10px 0; padding: 15px; background: white; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }}
                .thumb {{ width: 100px; height: 75px; object-fit: cover; border-radius: 4px; margin-right: 15px; float: left; }}
                .reserva-details {{ overflow: hidden; }}
                .status {{ font-weight: bold; margin-top: 5px; }}
                .status.pendente {{ color: #ffc107; }}
                .status.confirmada {{ color: #28a745; }}
                .status.cancelada {{ color: #dc3545; }}
                .cancel-btn {{ background: #dc3545; color: white; padding: 8px 12px; text-decoration: none; border-radius: 4px; float: right; margin-top: 10px; }}
                .cancel-btn:hover {{ background: #c82333; }}
                a {{ color: #007bff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
                .flash.error {{ background-color: #dc3545; color: white; }}
                .flash.success {{ background-color: #28a745; color: white; }}
                .flash.info {{ background-color: #17a2b8; color: white; }}
            </style>
        </head>
        <body>
            <h1>Minhas Reservas</h1>
            {flash_messages}
            '''
        if not reservas:
            html += '''
            <p>Nenhuma reserva encontrada.</p>
            '''
        else:
            for r in reservas:
                thumb = r['thumbnail_url'] or 'https://via.placeholder.com/100x75?text=No+Image'
                html += f'''
                <div class="reserva">
                    <img src="{thumb}" alt="Carro" class="thumb">
                    <div class="reserva-details">
                        <p>Carro: <strong>{r['carro_modelo']}</strong></p>
                        <p>Data: {r['data_reserva']} | Horário: {r['hora_inicio']} - {r['hora_fim']}</p>
                        <p class="status {r['status']}">Status: {r['status'].capitalize()}</p>
                        <p>{r['observacoes'] or 'Sem observações'}</p>
                        <a href="/cancelar_reserva/{r['id']}" class="cancel-btn">Cancelar Reserva</a>
                    </div>
                    <div style="clear:both;"></div>
                </div>
                '''
        html += '''
            <p><a href="/home">Voltar para Home</a></p>
        </body>
        </html>
        '''
        return html, 200
    except Exception as e:
        flash('Erro ao carregar suas reservas.', 'error')
        logging.error(f'Erro ao carregar minhas reservas: {e}')
        return '''
        <!DOCTYPE html>
        <html><head><title>Error</title></head><body><h1>Erro ao carregar reservas.</h1></body></html>
        ''', 500

@app.route('/cancelar_reserva/<int:reserva_id>')
def cancelar_reserva(reserva_id):
    """Cancela uma reserva."""
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
        sync_reservas_to_sheets()
        sync_carros_to_sheets()
    except Exception as e:
        flash(f'Erro ao cancelar reserva: {e}', 'error')
        logging.error(f'Erro ao cancelar reserva {reserva_id}: {e}')
    finally:
        conn.close()
    return redirect(url_for('minhas_reservas'))

@app.route('/admin')
def admin_panel():
    """Painel administrativo (com thumbnails)."""
    if not is_admin():
        flash('Acesso negado. Você não tem permissão de administrador.', 'error')
        return redirect(url_for('home'))
    
    try:
        reservas = get_reservas()
        usuarios = get_usuarios()
        carros = get_all_cars()
        
        flash_messages = ''
        if session.get('_flashes'):
            for category, message in session.pop('_flashes'):
                flash_messages += f'<div class="flash {category}">{message}</div>'

        # HTML inline for admin
        html = f'''
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <title>Admin - JG Minis</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }}
                h1, h2 {{ color: #333; }}
                .admin-actions {{ margin-bottom: 20px; }}
                .admin-actions a {{ margin-right: 10px; padding: 8px 15px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; }}
                .admin-actions a.logout {{ background: #6c757d; }}
                table {{ border-collapse: collapse; width: 100%; background: white; margin: 20px 0; box-shadow: 0 2px 5px rgba(0,0,0,0.05); border-radius: 8px; overflow: hidden; }}
                th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                th {{ background-color: #f2f2f2; font-weight: bold; }}
                .thumb {{ width: 50px; height: 37.5px; object-fit: cover; border-radius: 4px; }}
                .action-btn {{ padding: 6px 10px; text-decoration: none; border-radius: 4px; color: white; margin-right: 5px; }}
                .btn-primary {{ background: #007bff; }}
                .btn-success {{ background: #28a745; }}
                .btn-danger {{ background: #dc3545; }}
                .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
                .flash.error {{ background-color: #dc3545; color: white; }}
                .flash.success {{ background-color: #28a745; color: white; }}
                .flash.info {{ background-color: #17a2b8; color: white; }}
            </style>
        </head>
        <body>
            <h1>Painel Administrativo</h1>
            {flash_messages}
            <div class="admin-actions">
                <a href="/admin/add_carro">Adicionar Carro</a>
                <a href="/admin/sync_sheets">Sincronizar Sheets</a>
                <a href="/admin/backup_db">Backup DB</a>
                <a href="/admin/restore_backup">Restaurar Backup</a>
                <a href="/logout" class="logout">Logout</a>
            </div>
            
            <h2>Carros</h2>
            <table>
                <tr><th>ID</th><th>Modelo</th><th>Ano</th><th>Cor</th><th>Placa</th><th>Disponível</th><th>Preço</th><th>Thumb</th><th>Ações</th></tr>
                '''
        for car in carros:
            thumb = car['thumbnail_url'] or 'https://via.placeholder.com/50x37.5?text=No+Image'
            disponivel = 'Sim' if car['disponivel'] else 'Não'
            html += f'''
            <tr>
                <td>{car['id']}</td>
                <td>{car['modelo']}</td>
                <td>{car['ano']}</td>
                <td>{car['cor']}</td>
                <td>{car['placa']}</td>
                <td>{disponivel}</td>
                <td>R$ {car['preco_diaria']:.2f}</td>
                <td><img src="{thumb}" alt="Thumb" class="thumb"></td>
                <td>
                    <a href="/admin/edit_carro/{car['id']}" class="action-btn btn-primary">Editar</a>
                    <a href="/admin/delete_carro/{car['id']}" class="action-btn btn-danger">Deletar</a>
                </td>
            </tr>
            '''
        html += '</table>'
        
        html += '''
        <h2>Reservas</h2>
        <table>
            <tr><th>ID</th><th>Usuário</th><th>Carro</th><th>Data</th><th>Hora</th><th>Status</th><th>Thumb</th><th>Ações</th></tr>
            '''
        for r in reservas:
            thumb = r['thumbnail_url'] or 'https://via.placeholder.com/50x37.5?text=No+Image'
            html += f'''
            <tr>
                <td>{r['id']}</td>
                <td>{r['usuario_nome']}</td>
                <td>{r['carro_modelo']}</td>
                <td>{r['data_reserva']}</td>
                <td>{r['hora_inicio']} - {r['hora_fim']}</td>
                <td>{r['status']}</td>
                <td><img src="{thumb}" alt="Thumb" class="thumb"></td>
                <td>
                    <a href="/admin/update_reserva_status/{r['id']}/confirmada" class="action-btn btn-success">Confirmar</a>
                    <a href="/admin/update_reserva_status/{r['id']}/cancelada" class="action-btn btn-danger">Cancelar</a>
                </td>
            </tr>
            '''
        html += '</table>'
        
        html += '''
        <h2>Usuários</h2>
        <table>
            <tr><th>ID</th><th>Nome</th><th>Email</th><th>Admin</th><th>Ações</th></tr>
            '''
        for u in usuarios:
            is_admin_text = 'Sim' if u['is_admin'] else 'Não'
            promote_button = ''
            if not u['is_admin']:
                promote_button = f'<a href="/admin/promote_admin/{u["id"]}" class="action-btn btn-success">Promover Admin</a>'
            html += f'''
            <tr>
                <td>{u['id']}</td>
                <td>{u['nome']}</td>
                <td>{u['email']}</td>
                <td>{is_admin_text}</td>
                <td>{promote_button}</td>
            </tr>
            '''
        html += '</table></body></html>'
        return html, 200
    except Exception as e:
        logging.error(f"Erro ao carregar painel admin: {e}")
        flash('Erro ao carregar dados do painel administrativo.', 'error')
        return '''
        <!DOCTYPE html>
        <html><head><title>Admin Error</title></head><body><h1>Erro no Admin.</h1><a href="/logout">Logout</a></body></html>
        ''', 500

@app.route('/admin/add_carro', methods=['GET', 'POST'])
def admin_add_carro():
    """Adiciona um novo carro (apenas admin)."""
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
            flash('Todos os campos obrigatórios (exceto Thumbnail URL) devem ser preenchidos.', 'error')
            return get_admin_add_carro_html()
        
        try:
            conn = get_db_connection()
            conn.execute('INSERT INTO carros (modelo, ano, cor, placa, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?)',
                         (modelo, ano, cor, placa, float(preco_diaria), thumbnail_url))
            conn.commit()
            conn.close()
            flash('Carro adicionado com sucesso!', 'success')
            logging.info(f'Carro adicionado: {modelo} ({placa})')
            sync_carros_to_sheets()
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao adicionar carro: {e}', 'error')
            logging.error(f'Erro ao adicionar carro: {e}')
        return get_admin_add_carro_html()
    return get_admin_add_carro_html()

def get_admin_add_carro_html():
    """HTML inline for admin add carro form."""
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Adicionar Carro - Admin</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }}
            .container {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 600px; margin: 0 auto; }}
            h1 {{ text-align: center; color: #333; margin-bottom: 20px; }}
            input[type="text"], input[type="number"] {{ width: calc(100% - 20px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ background: #28a745; color: white; padding: 12px 15px; width: 100%; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 15px; }}
            button:hover {{ background: #218838; }}
            a {{ color: #007bff; text-decoration: none; margin-top: 20px; display: block; text-align: center; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Adicionar Novo Carro</h1>
            {flash_messages}
            <form method="POST">
                <input type="text" name="modelo" placeholder="Modelo" required>
                <input type="number" name="ano" placeholder="Ano" required>
                <input type="text" name="cor" placeholder="Cor" required>
                <input type="text" name="placa" placeholder="Placa" required>
                <input type="number" step="0.01" name="preco_diaria" placeholder="Preço Diária" required>
                <input type="text" name="thumbnail_url" placeholder="URL da Miniatura (opcional)">
                <button type="submit">Adicionar Carro</button>
            </form>
            <a href="/admin">Voltar para o Painel Admin</a>
        </div>
    </body>
    </html>
    '''

@app.route('/admin/edit_carro/<int:car_id>', methods=['GET', 'POST'])
def admin_edit_carro(car_id):
    """Edita um carro existente (apenas admin)."""
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
            flash('Todos os campos obrigatórios (exceto Thumbnail URL) devem ser preenchidos.', 'error')
            return get_admin_edit_carro_html(car)
        
        try:
            conn = get_db_connection()
            conn.execute('UPDATE carros SET modelo = ?, ano = ?, cor = ?, placa = ?, preco_diaria = ?, disponivel = ?, thumbnail_url = ? WHERE id = ?',
                         (modelo, ano, cor, placa, float(preco_diaria), disponivel, thumbnail_url, car_id))
            conn.commit()
            conn.close()
            flash('Carro atualizado com sucesso!', 'success')
            logging.info(f'Carro {car_id} atualizado: {modelo} ({placa})')
            sync_carros_to_sheets()
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada para outro carro. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao editar carro: {e}', 'error')
            logging.error(f'Erro ao editar carro {car_id}: {e}')
        return get_admin_edit_carro_html(car)
    return get_admin_edit_carro_html(car)

def get_admin_edit_carro_html(car):
    """HTML inline for admin edit carro form."""
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    disponivel_checked = 'checked' if car['disponivel'] else ''
    
    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Editar Carro - Admin</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }}
            .container {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 600px; margin: 0 auto; }}
            h1 {{ text-align: center; color: #333; margin-bottom: 20px; }}
            input[type="text"], input[type="number"] {{ width: calc(100% - 20px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            .checkbox-container {{ margin: 10px 0; }}
            .checkbox-container input {{ width: auto; margin-right: 10px; }}
            button {{ background: #007bff; color: white; padding: 12px 15px; width: 100%; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 15px; }}
            button:hover {{ background: #0056b3; }}
            a {{ color: #007bff; text-decoration: none; margin-top: 20px; display: block; text-align: center; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Editar Carro: {car['modelo']}</h1>
            {flash_messages}
            <form method="POST">
                <input type="text" name="modelo" placeholder="Modelo" value="{car['modelo']}" required>
                <input type="number" name="ano" placeholder="Ano" value="{car['ano']}" required>
                <input type="text" name="cor" placeholder="Cor" value="{car['cor']}" required>
                <input type="text" name="placa" placeholder="Placa" value="{car['placa']}" required>
                <input type="number" step="0.01" name="preco_diaria" placeholder="Preço Diária" value="{car['preco_diaria']}" required>
                <input type="text" name="thumbnail_url" placeholder="URL da Miniatura (opcional)" value="{car['thumbnail_url'] or ''}">
                <div class="checkbox-container">
                    <input type="checkbox" name="disponivel" id="disponivel" {disponivel_checked}>
                    <label for="disponivel">Disponível</label>
                </div>
                <button type="submit">Atualizar Carro</button>
            </form>
            <a href="/admin">Voltar para o Painel Admin</a>
        </div>
    </body>
    </html>
    '''

@app.route('/admin/delete_carro/<int:car_id>')
def admin_delete_carro(car_id):
    """Deleta um carro (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        reservas_ativas = conn.execute('SELECT COUNT(*) FROM reservas WHERE carro_id = ? AND status IN (?, ?)', (car_id, 'pendente', 'confirmada')).fetchone()[0]
        if reservas_ativas > 0:
            flash(f'Não é possível deletar o carro. Existem {reservas_ativas} reservas ativas para ele.', 'error')
            return redirect(url_for('admin_panel'))

        conn.execute('DELETE FROM carros WHERE id = ?', (car_id,))
        conn.commit()
        flash('Carro deletado com sucesso!', 'success')
        logging.info(f'Carro {car_id} deletado.')
        sync_carros_to_sheets()
    except Exception as e:
        flash(f'Erro ao deletar carro: {e}', 'error')
        logging.error(f'Erro ao deletar carro {car_id}: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/update_reserva_status/<int:reserva_id>/<string:status>')
def admin_update_reserva_status(reserva_id, status):
    """Atualiza o status de uma reserva (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', (status, reserva_id))
        conn.commit()
        flash(f'Status da reserva {reserva_id} atualizado para "{status}" com sucesso!', 'success')
        logging.info(f'Reserva {reserva_id} status atualizado para: {status}')
        sync_reservas_to_sheets()
    except Exception as e:
        flash(f'Erro ao atualizar status da reserva: {e}', 'error')
        logging.error(f'Erro ao atualizar status da reserva {reserva_id}: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/promote_admin/<int:user_id>')
def admin_promote_admin(user_id):
    """Promove um usuário a administrador (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        conn.execute('UPDATE usuarios SET is_admin = TRUE WHERE id = ?', (user_id,))
        conn.commit()
        flash(f'Usuário {user_id} promovido a administrador com sucesso!', 'success')
        logging.info(f'Usuário {user_id} promovido a admin.')
        sync_usuarios_to_sheets()
    except Exception as e:
        flash(f'Erro ao promover usuário a admin: {e}', 'error')
        logging.error(f'Erro ao promover usuário {user_id} a admin: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/sync_sheets')
def admin_sync_sheets():
    """Sincroniza todos os dados com o Google Sheets (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    if not gspread_client:
        flash('Sincronização com Google Sheets desativada (credenciais ausentes ou erro).', 'error')
        return redirect(url_for('admin_panel'))

    try:
        sync_all_to_sheets() # Chama a função que sincroniza tudo
        flash('Dados sincronizados com o Google Sheets com sucesso!', 'success')
        logging.info('Todas as abas do Google Sheets sincronizadas.')
    except Exception as e:
        flash(f'Erro geral ao sincronizar com Google Sheets: {e}', 'error')
        logging.error(f'Erro geral ao sincronizar com Google Sheets: {e}')
    return redirect(url_for('admin_panel'))

@app.route('/admin/backup_db')
def admin_backup_db():
    """Gera um backup do banco de dados em formato JSON (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        reservas = conn.execute('SELECT * FROM reservas').fetchall()
        usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
        carros = conn.execute('SELECT * FROM carros').fetchall()

        # Converte Row objects para dicionários para serialização JSON
        reservas_list = [dict(r) for r in reservas]
        usuarios_list = [dict(u) for u in usuarios]
        carros_list = [dict(c) for c in carros]

        backup_data = {
            'timestamp': datetime.now().isoformat(),
            'reservas': reservas_list,
            'usuarios': usuarios_list,
            'carros': carros_list
        }
        
        # Gera um hash do conteúdo para verificação de integridade
        backup_json_str = json.dumps(backup_data, indent=4, ensure_ascii=False)
        backup_data['hash'] = hashlib.sha256(backup_json_str.encode()).hexdigest()

        # Recria o JSON com o hash
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
    """Restaura o banco de dados a partir de um arquivo JSON de backup (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    if request.method == 'POST':
        if 'backup_file' not in request.files:
            flash('Nenhum arquivo de backup selecionado.', 'error')
            return get_admin_restore_backup_html()
        
        file = request.files['backup_file']
        if file.filename == '':
            flash('Nenhum arquivo de backup selecionado.', 'error')
            return get_admin_restore_backup_html()
        
        if file and file.filename.endswith('.json'):
            try:
                backup_content = file.read().decode('utf-8')
                backup_data = json.loads(backup_content)

                # Verifica integridade do hash
                received_hash = backup_data.pop('hash', None)
                if received_hash:
                    calculated_hash = hashlib.sha256(json.dumps(backup_data, indent=4, ensure_ascii=False).encode()).hexdigest()
                    if received_hash != calculated_hash:
                        flash('Erro de integridade do backup: hash não corresponde.', 'error')
                        logging.error('Erro de integridade do backup: hash não corresponde.')
                        return get_admin_restore_backup_html()
                else:
                    logging.warning('Backup sem hash de integridade. Prosseguindo com a restauração.')

                conn = get_db_connection()
                c = conn.cursor()

                # Limpa tabelas existentes (cuidado: isso apaga dados atuais!)
                c.execute('DELETE FROM reservas')
                c.execute('DELETE FROM usuarios')
                c.execute('DELETE FROM carros')

                # Restaura carros
                for car_data in backup_data.get('carros', []):
                    c.execute('INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'], car_data['placa'], car_data['disponivel'], car_data['preco_diaria'], car_data['thumbnail_url']))
                
                # Restaura usuários
                for user_data in backup_data.get('usuarios', []):
                    c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (user_data['id'], user_data['nome'], user_data['email'], user_data['senha_hash'], user_data['cpf'], user_data['telefone'], user_data['data_cadastro'], user_data['is_admin']))
                
                # Restaura reservas
                for reserva_data in backup_data.get('reservas', []):
                    c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                              (reserva_data['id'], reserva_data['usuario_id'], reserva_data['carro_id'], reserva_data['data_reserva'], reserva_data['hora_inicio'], reserva_data['hora_fim'], reserva_data['status'], reserva_data['observacoes'], reserva_data['thumbnail_url']))
                
                conn.commit()
                flash('Backup restaurado com sucesso!', 'success')
                logging.info('Backup restaurado com sucesso.')
                # Re-inicializa o gspread client para garantir que as planilhas reflitam os dados restaurados
                sync_all_to_sheets()

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
    return get_admin_restore_backup_html()

def get_admin_restore_backup_html():
    """HTML inline for admin restore backup form."""
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'

    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Restaurar Backup - Admin</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; }}
            .container {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 600px; margin: 0 auto; }}
            h1 {{ text-align: center; color: #333; margin-bottom: 20px; }}
            input[type="file"] {{ width: calc(100% - 20px); padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ background: #dc3545; color: white; padding: 12px 15px; width: 100%; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 15px; }}
            button:hover {{ background: #c82333; }}
            a {{ color: #007bff; text-decoration: none; margin-top: 20px; display: block; text-align: center; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Restaurar Backup do Banco de Dados</h1>
            {flash_messages}
            <form method="POST" enctype="multipart/form-data">
                <label for="backup_file">Selecione o arquivo JSON de backup:</label>
                <input type="file" name="backup_file" id="backup_file" accept=".json" required>
                <button type="submit">Restaurar Backup</button>
            </form>
            <a href="/admin">Voltar para o Painel Admin</a>
        </div>
    </body>
    </html>
    '''

# --- Tratamento de Erros ---
@app.errorhandler(404)
def page_not_found(e):
    """Trata erros 404 (Página não encontrada)."""
    logging.warning(f"404 Not Found: {request.url}")
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'
    return render_template_string(f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>404 - Página Não Encontrada</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; text-align: center; }}
            h1 {{ color: #dc3545; }}
            p {{ color: #666; }}
            a {{ color: #007bff; text-decoration: none; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <h1>404 - Página Não Encontrada</h1>
        {flash_messages}
        <p>A página que você está procurando não existe.</p>
        <p><a href="/home">Voltar para a página inicial</a></p>
    </body>
    </html>
    '''), 404

@app.errorhandler(500)
def internal_server_error(e):
    """Trata erros 500 (Erro interno do servidor)."""
    logging.error(f"500 Internal Server Error: {e}", exc_info=True)
    flash_messages = ''
    if session.get('_flashes'):
        for category, message in session.pop('_flashes'):
            flash_messages += f'<div class="flash {category}">{message}</div>'
    return render_template_string(f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>500 - Erro Interno do Servidor</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f4; text-align: center; }}
            h1 {{ color: #dc3545; }}
            p {{ color: #666; }}
            a {{ color: #007bff; text-decoration: none; }}
            .flash {{ background-color: #ffc107; color: #333; padding: 10px; border-radius: 5px; margin-bottom: 10px; text-align: center; }}
            .flash.error {{ background-color: #dc3545; color: white; }}
            .flash.success {{ background-color: #28a745; color: white; }}
            .flash.info {{ background-color: #17a2b8; color: white; }}
        </style>
    </head>
    <body>
        <h1>500 - Erro Interno do Servidor</h1>
        {flash_messages}
        <p>Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.</p>
        <p><a href="/home">Voltar para a página inicial</a></p>
    </body>
    </html>
    '''), 500

# O Gunicorn (servidor de produção) irá chamar a instância 'app' diretamente.
# Não precisamos do bloco if __name__ == '__main__': app.run() para deploy.
# Apenas um 'pass' para manter a estrutura se o arquivo for executado diretamente,
# mas o Gunicorn não o usará.
if __name__ == '__main__':
    pass
