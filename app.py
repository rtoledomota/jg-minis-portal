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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_jgminis_v4.3.20') # Chave secreta para sessões

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

        # Verifica contagens existentes para logs
        c.execute('SELECT COUNT(*) FROM reservas')
        reservas_count = c.fetchone()[0]
        if reservas_count == 0:
            logging.warning('DB inicializado: 0 reservas encontradas. Considere restaurar de um backup JSON ou sincronizar do Sheets.')
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
            logging.warning('DB inicializado: 0 carros encontrados. Adicione carros, restaure de um backup JSON ou sincronize do Sheets.')
        else:
            logging.info(f'DB inicializado: {carros_count} carros preservados.')

        logging.info('App bootado com sucesso.')
    except sqlite3.Error as e:
        logging.error(f"Erro ao inicializar o banco de dados: {e}")
    finally:
        if conn:
            conn.close()

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
    """Busca todos os carros."""
    try:
        conn = get_db_connection()
        cars = conn.execute('SELECT * FROM carros').fetchall()
        conn.close()
        if len(cars) == 0:
            logging.info('DB vazio: 0 carros encontrados.')
        return cars
    except Exception as e:
        logging.error(f"Erro ao carregar carros: {e}")
        return []

def get_reservas():
    """Busca todas as reservas com detalhes de usuário e carro."""
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
                        r.observacoes,
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
        # Fallback para arquivo service_account.json
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

# Inicializa o cliente gspread no nível do módulo
gspread_client = init_gspread_client()

# Substitua 'SUA_SHEET_ID' pela ID real da sua planilha do Google Sheets
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', 'SUA_SHEET_ID_AQUI') 

def sync_reservas_to_sheets():
    """Sincroniza as reservas do DB para a aba 'Reservas' do Google Sheets."""
    if not gspread_client:
        logging.warning('Sync reservas pulado: Cliente gspread não inicializado ou credenciais ausentes.')
        return

    if GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning('Sync reservas pulado: GOOGLE_SHEET_ID não configurado. Por favor, defina a variável de ambiente.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Reservas')
        sheet.clear() # Limpa a aba existente

        reservas = get_reservas()
        if reservas:
            # Cabeçalhos na ordem desejada
            headers = ['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status', 'Observacoes', 'Carro Thumbnail']
            # Mapeia os dados da tupla para a ordem dos cabeçalhos
            data_to_append = [
                [
                    str(r['id']),
                    str(r['usuario_nome']),
                    str(r['carro_modelo']),
                    str(r['data_reserva']),
                    str(r['hora_inicio']),
                    str(r['hora_fim']),
                    str(r['status']),
                    str(r['observacoes'] or ''),
                    str(r['carro_thumbnail'] or '')
                ] for r in reservas
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync reservas: {len(reservas)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status', 'Observacoes', 'Carro Thumbnail']])
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
        logging.warning('Sync usuários pulado: GOOGLE_SHEET_ID não configurado. Por favor, defina a variável de ambiente.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Usuarios')
        sheet.clear()

        usuarios = get_usuarios()
        if usuarios:
            headers = ['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data Cadastro', 'Admin']
            data_to_append = [
                [
                    str(u['id']),
                    str(u['nome']),
                    str(u['email']),
                    str(u['senha_hash']), # Não expor senhas em produção, apenas para debug/backup
                    str(u['cpf'] or ''),
                    str(u['telefone'] or ''),
                    str(u['data_cadastro']),
                    'Sim' if u['is_admin'] else 'Não'
                ] for u in usuarios
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync usuários: {len(usuarios)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data Cadastro', 'Admin']])
            logging.info('Sync usuários: Nenhum usuário para sincronizar. Aba limpa e cabeçalhos adicionados.')
    except Exception as e:
        logging.error(f'Erro na sincronização de usuários com Google Sheets: {e}')
        flash('Erro ao sincronizar usuários com o Google Sheets.', 'error')

def sync_carros_to_sheets():
    """Sincroniza os carros do DB para a aba 'Carros' do Google Sheets."""
    if not gspread_client:
        logging.warning('Sync carros pulado: Cliente gspread não inicializado ou credenciais ausentes.')
        return

    if GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning('Sync carros pulado: GOOGLE_SHEET_ID não configurado. Por favor, defina a variável de ambiente.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Carros')
        sheet.clear()

        carros = get_all_cars()
        if carros:
            headers = ['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponivel', 'Preco Diaria', 'thumbnail_url']
            data_to_append = [
                [
                    str(c['id']),
                    str(c['modelo']),
                    str(c['ano']),
                    str(c['cor']),
                    str(c['placa']),
                    'Sim' if c['disponivel'] else 'Não',
                    f"{c['preco_diaria']:.2f}",
                    str(c['thumbnail_url'] or '')
                ] for c in carros
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync carros: {len(carros)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponivel', 'Preco Diaria', 'thumbnail_url']])
            logging.info('Sync carros: Nenhum carro para sincronizar. Aba limpa e cabeçalhos adicionados.')
    except Exception as e:
        logging.error(f'Erro na sincronização de carros com Google Sheets: {e}')
        flash('Erro ao sincronizar carros com o Google Sheets.', 'error')

def load_from_sheets():
    """Carrega dados das planilhas Google Sheets para o banco de dados SQLite."""
    if not gspread_client:
        logging.warning('Load from Sheets pulado: Cliente gspread não inicializado ou credenciais ausentes.')
        return

    if GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning('Load from Sheets pulado: GOOGLE_SHEET_ID não configurado. Por favor, defina a variável de ambiente.')
        return

    conn = get_db_connection()
    c = conn.cursor()
    try:
        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEET_ID)

        # Carregar Carros
        try:
            carros_sheet = spreadsheet.worksheet('Carros')
            carros_data = carros_sheet.get_all_records()
            if carros_data:
                c.execute('DELETE FROM carros') # Limpa antes de carregar
                for car in carros_data:
                    c.execute('INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (car.get('ID'), car.get('Modelo'), car.get('Ano'), car.get('Cor'), car.get('Placa'),
                               car.get('Disponivel') == 'Sim', car.get('Preco Diaria'), car.get('thumbnail_url')))
                logging.info(f'Sheets: {len(carros_data)} carros carregados da planilha.')
            else:
                logging.info('Sheets: Nenhuma dado na aba Carros.')
        except gspread.exceptions.WorksheetNotFound:
            logging.warning('Sheets: Aba "Carros" não encontrada na planilha. Ignorando carregamento de carros.')
        except Exception as e:
            logging.error(f'Sheets: Erro ao carregar carros da planilha: {e}')

        # Carregar Usuários
        try:
            usuarios_sheet = spreadsheet.worksheet('Usuarios')
            usuarios_data = usuarios_sheet.get_all_records()
            if usuarios_data:
                c.execute('DELETE FROM usuarios') # Limpa antes de carregar
                for user in usuarios_data:
                    c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (user.get('ID'), user.get('Nome'), user.get('Email'), user.get('Senha_hash'),
                               user.get('CPF'), user.get('Telefone'), user.get('Data Cadastro'), user.get('Admin') == 'Sim'))
                logging.info(f'Sheets: {len(usuarios_data)} usuários carregados da planilha.')
            else:
                logging.info('Sheets: Nenhuma dado na aba Usuarios.')
        except gspread.exceptions.WorksheetNotFound:
            logging.warning('Sheets: Aba "Usuarios" não encontrada na planilha. Ignorando carregamento de usuários.')
        except Exception as e:
            logging.error(f'Sheets: Erro ao carregar usuários da planilha: {e}')

        # Carregar Reservas
        try:
            reservas_sheet = spreadsheet.worksheet('Reservas')
            reservas_data = reservas_sheet.get_all_records()
            if reservas_data:
                c.execute('DELETE FROM reservas') # Limpa antes de carregar
                for reserva in reservas_data:
                    # Precisa buscar usuario_id e carro_id pelo nome/modelo
                    usuario_id = conn.execute('SELECT id FROM usuarios WHERE nome = ?', (reserva.get('Usuário'),)).fetchone()
                    carro_id = conn.execute('SELECT id FROM carros WHERE modelo = ?', (reserva.get('Carro'),)).fetchone()
                    
                    if usuario_id and carro_id:
                        c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                  (reserva.get('ID'), usuario_id[0], carro_id[0], reserva.get('Data'), reserva.get('Hora Início'),
                                   reserva.get('Hora Fim'), reserva.get('Status'), reserva.get('Observacoes')))
                    else:
                        logging.warning(f"Sheets: Reserva ID {reserva.get('ID')} ignorada. Usuário ou Carro não encontrados no DB.")
                logging.info(f'Sheets: {len(reservas_data)} reservas carregadas da planilha.')
            else:
                logging.info('Sheets: Nenhuma dado na aba Reservas.')
        except gspread.exceptions.WorksheetNotFound:
            logging.warning('Sheets: Aba "Reservas" não encontrada na planilha. Ignorando carregamento de reservas.')
        except Exception as e:
            logging.error(f'Sheets: Erro ao carregar reservas da planilha: {e}')

        conn.commit()
        logging.info('Sheets: Dados carregados da planilha Google Sheets para o DB SQLite.')
    except Exception as e:
        logging.error(f'Sheets: Erro geral ao carregar dados da planilha: {e}')
    finally:
        conn.close()

# Inicializa o DB e carrega dados do Sheets na inicialização do app
init_db()
load_from_sheets()

# Rota de saúde para Railway (retorna OK se app vivo)
@app.route('/health')
def health():
    """Endpoint de saúde para verificação do Railway."""
    return 'OK', 200

# --- Rotas ---
@app.route('/')
def index():
    """Redireciona para a página inicial."""
    return redirect(url_for('home'))

@app.route('/home')
def home():
    """Página inicial com lista de carros disponíveis."""
    try:
        carros = get_all_cars()
        
        # HTML inline para a página home
        html_content = """
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>JG Minis - Home</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; }
                .container { max-width: 1200px; margin: auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
                h1 { color: #0056b3; text-align: center; margin-bottom: 30px; }
                .car-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }
                .car-card { background: #f9f9f9; border: 1px solid #ddd; border-radius: 8px; overflow: hidden; text-align: center; padding-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
                .car-card img { width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #eee; }
                .car-card h2 { font-size: 1.2em; margin: 15px 0 5px; color: #333; }
                .car-card p { font-size: 0.9em; color: #666; margin: 5px 10px; }
                .car-card .price { font-size: 1.1em; color: #007bff; font-weight: bold; margin: 10px 0; }
                .car-card a { display: inline-block; background-color: #007bff; color: white; padding: 8px 15px; border-radius: 5px; text-decoration: none; margin-top: 10px; }
                .car-card a:hover { background-color: #0056b3; }
                .message { text-align: center; padding: 20px; background-color: #e9ecef; border-radius: 5px; margin-top: 20px; }
                .navbar { background-color: #007bff; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }
                .navbar a { color: white; text-decoration: none; padding: 8px 15px; border-radius: 5px; }
                .navbar a:hover { background-color: #0056b3; }
                .navbar .logo { font-weight: bold; font-size: 1.5em; }
                .flash-messages { list-style: none; padding: 0; margin: 20px 0; }
                .flash-messages li { padding: 10px; margin-bottom: 10px; border-radius: 5px; }
                .flash-messages .success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
                .flash-messages .error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
                .flash-messages .info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
                .admin-link { background-color: #28a745; margin-left: 10px; }
                .admin-link:hover { background-color: #218838; }
            </style>
        </head>
        <body>
            <div class="navbar">
                <a href="/" class="logo">JG Minis</a>
                <div>
                    <a href="/home">Home</a>
                    """
        if 'user_id' in session:
            html_content += f"""
                    <a href="/minhas_reservas">Minhas Reservas</a>
                    <a href="/logout">Logout ({session.get('user_name', 'Usuário')})</a>
                    """
            if is_admin():
                html_content += """
                    <a href="/admin" class="admin-link">Admin</a>
                    """
        else:
            html_content += """
                    <a href="/login">Login</a>
                    <a href="/registro">Registro</a>
                    """
        html_content += """
                </div>
            </div>
            <div class="container">
                """
        # Flash messages
        if '_flashes' in session:
            html_content += '<ul class="flash-messages">'
            for category, message in session['_flashes']:
                html_content += f'<li class="{category}">{message}</li>'
            session.pop('_flashes', None) # Clear flashes after displaying
            html_content += '</ul>'

        html_content += """
                <h1>Carros Disponíveis</h1>
                <div class="car-grid">
        """
        if carros:
            for car in carros:
                if car['disponivel']:
                    thumbnail_src = car['thumbnail_url'] if car['thumbnail_url'] else 'https://via.placeholder.com/300x180?text=Sem+Imagem'
                    html_content += f"""
                    <div class="car-card">
                        <img src="{thumbnail_src}" alt="{car['modelo']}">
                        <h2>{car['modelo']} - {car['ano']}</h2>
                        <p>{car['cor']} - Placa: {car['placa']}</p>
                        <p class="price">R$ {car['preco_diaria']:.2f} / diária</p>
                        <a href="/reservar/{car['id']}">Reservar</a>
                    </div>
                    """
        else:
            html_content += """
                </div>
                <div class="message">
                    <p>Nenhum carro disponível no momento. Por favor, adicione carros via painel administrativo.</p>
                    <p>Se você é administrador, acesse <a href="/admin">/admin</a>.</p>
                </div>
            """
        html_content += """
                </div>
            </div>
        </body>
        </html>
        """
        return html_content
    except Exception as e:
        logging.error(f"Erro ao carregar a página inicial: {e}", exc_info=True)
        flash('Erro ao carregar a lista de carros. Tente novamente mais tarde.', 'error')
        return """
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Erro</title>
            <style>body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }</style>
        </head>
        <body>
            <h1>Ocorreu um erro!</h1>
            <p>Não foi possível carregar a página inicial. Por favor, tente novamente mais tarde.</p>
            <a href="/">Voltar para Home</a>
        </body>
        </html>
        """

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
            return redirect(url_for('registro'))
        
        # Validação de CPF (apenas dígitos, 11 caracteres)
        cleaned_cpf = ''.join(filter(str.isdigit, cpf))
        if not (cleaned_cpf.isdigit() and len(cleaned_cpf) == 11):
            flash('CPF inválido. Deve conter 11 dígitos.', 'error')
            return redirect(url_for('registro'))

        # Validação de Telefone (apenas dígitos, 10 ou 11 caracteres)
        cleaned_telefone = ''.join(filter(str.isdigit, telefone))
        if not (cleaned_telefone.isdigit() and 10 <= len(cleaned_telefone) <= 11):
            flash('Telefone inválido. Deve conter 10 ou 11 dígitos.', 'error')
            return redirect(url_for('registro'))

        conn = get_db_connection()
        try:
            # Hash da senha
            senha_hash = hashlib.sha256(senha.encode()).hexdigest()

            conn.execute('INSERT INTO usuarios (nome, email, senha_hash, cpf, telefone) VALUES (?, ?, ?, ?, ?)',
                         (nome, email, senha_hash, cleaned_cpf, cleaned_telefone))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            logging.info(f'Novo usuário registrado: {email}')
            sync_usuarios_to_sheets() # Sincroniza após registro
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email ou CPF já cadastrado. Tente novamente com outros dados.', 'error')
        except Exception as e:
            flash(f'Erro ao registrar usuário: {e}', 'error')
            logging.error(f'Erro no registro de usuário: {e}', exc_info=True)
        finally:
            conn.close()
    
    # HTML inline para a página de registro
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Registro</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .form-container {{ background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 400px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="text"], input[type="email"], input[type="password"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            button:hover {{ background-color: #0056b3; }}
            .link {{ text-align: center; margin-top: 15px; }}
            .link a {{ color: #007bff; text-decoration: none; }}
            .link a:hover {{ text-decoration: underline; }}
            .flash-messages {{ list-style: none; padding: 0; margin: 0 0 15px 0; }}
            .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Registro</h1>
            """
    if '_flashes' in session:
        html_content += '<ul class="flash-messages">'
        for category, message in session['_flashes']:
            html_content += f'<li class="{category}">{message}</li>'
        session.pop('_flashes', None)
        html_content += '</ul>'

    html_content += """
            <form method="POST">
                <label for="nome">Nome:</label>
                <input type="text" id="nome" name="nome" required>
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
                <label for="senha">Senha:</label>
                <input type="password" id="senha" name="senha" required>
                <label for="cpf">CPF (apenas números):</label>
                <input type="text" id="cpf" name="cpf" pattern="[0-9]{11}" title="CPF deve conter 11 dígitos numéricos" required>
                <label for="telefone">Telefone (apenas números, 10 ou 11 dígitos):</label>
                <input type="text" id="telefone" name="telefone" pattern="[0-9]{10,11}" title="Telefone deve conter 10 ou 11 dígitos numéricos" required>
                <button type="submit">Registrar</button>
            </form>
            <div class="link">
                Já tem uma conta? <a href="/login">Faça Login</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html_content

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
                # Verifica a senha
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
            logging.error(f'Erro no login: {e}', exc_info=True)
        finally:
            conn.close()
    
    # HTML inline para a página de login
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Login</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .form-container {{ background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 400px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="email"], input[type="password"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            button:hover {{ background-color: #0056b3; }}
            .link {{ text-align: center; margin-top: 15px; }}
            .link a {{ color: #007bff; text-decoration: none; }}
            .link a:hover {{ text-decoration: underline; }}
            .flash-messages {{ list-style: none; padding: 0; margin: 0 0 15px 0; }}
            .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Login</h1>
            """
    if '_flashes' in session:
        html_content += '<ul class="flash-messages">'
        for category, message in session['_flashes']:
            html_content += f'<li class="{category}">{message}</li>'
        session.pop('_flashes', None)
        html_content += '</ul>'

    html_content += """
            <form method="POST">
                <label for="email">Email:</label>
                <input type="email" id="email" name="email" required>
                <label for="senha">Senha:</label>
                <input type="password" id="senha" name="senha" required>
                <button type="submit">Entrar</button>
            </form>
            <div class="link">
                Não tem uma conta? <a href="/registro">Registre-se</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html_content

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

            # Validação de data e hora
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
            conn.execute('INSERT INTO reservas (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, observacoes) VALUES (?, ?, ?, ?, ?, ?)',
                         (session['user_id'], car_id, data_reserva, hora_inicio, hora_fim, observacoes))
            conn.execute('UPDATE carros SET disponivel = FALSE WHERE id = ?', (car_id,))
            conn.commit()
            conn.close()

            flash('Reserva realizada com sucesso!', 'success')
            logging.info(f"Reserva criada: Usuário {session['user_id']} reservou carro {car_id} para {data_reserva}")
            sync_reservas_to_sheets() # Sincroniza após a reserva
            sync_carros_to_sheets() # Sincroniza status do carro
            return redirect(url_for('minhas_reservas'))
        except ValueError:
            flash('Formato de data ou hora inválido.', 'error')
        except Exception as e:
            flash(f'Erro ao realizar reserva: {e}', 'error')
            logging.error(f'Erro ao realizar reserva: {e}', exc_info=True)
    
    # HTML inline para a página de reserva
    thumbnail_src = car['thumbnail_url'] if car['thumbnail_url'] else 'https://via.placeholder.com/300x180?text=Sem+Imagem'
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Reservar {car['modelo']}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .form-container {{ background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            .car-details {{ text-align: center; margin-bottom: 20px; }}
            .car-details img {{ max-width: 100%; height: auto; border-radius: 8px; margin-bottom: 10px; }}
            .car-details h2 {{ margin: 5px 0; }}
            .car-details p {{ margin: 2px 0; color: #666; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="date"], input[type="time"], textarea {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            button:hover {{ background-color: #0056b3; }}
            .flash-messages {{ list-style: none; padding: 0; margin: 0 0 15px 0; }}
            .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Reservar Carro</h1>
            """
    if '_flashes' in session:
        html_content += '<ul class="flash-messages">'
        for category, message in session['_flashes']:
            html_content += f'<li class="{category}">{message}</li>'
        session.pop('_flashes', None)
        html_content += '</ul>'

    html_content += f"""
            <div class="car-details">
                <img src="{thumbnail_src}" alt="{car['modelo']}">
                <h2>{car['modelo']} - {car['ano']}</h2>
                <p>{car['cor']} - Placa: {car['placa']}</p>
                <p>Preço: R$ {car['preco_diaria']:.2f} / diária</p>
            </div>
            <form method="POST">
                <label for="data_reserva">Data da Reserva:</label>
                <input type="date" id="data_reserva" name="data_reserva" required>
                <label for="hora_inicio">Hora de Início:</label>
                <input type="time" id="hora_inicio" name="hora_inicio" required>
                <label for="hora_fim">Hora de Fim:</label>
                <input type="time" id="hora_fim" name="hora_fim" required>
                <label for="observacoes">Observações (opcional):</label>
                <textarea id="observacoes" name="observacoes" rows="3"></textarea>
                <button type="submit">Confirmar Reserva</button>
            </form>
        </div>
    </body>
    </html>
    """
    return html_content

@app.route('/minhas_reservas')
def minhas_reservas():
    """Exibe as reservas do usuário logado."""
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
                        c.thumbnail_url as carro_thumbnail
                     FROM reservas r
                     JOIN carros c ON r.carro_id = c.id
                     WHERE r.usuario_id = ?
                     ORDER BY r.data_reserva DESC''', (session['user_id'],))
        reservas = c.fetchall()
        conn.close()

        # HTML inline para a página de minhas reservas
        html_content = f"""
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>JG Minis - Minhas Reservas</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; }}
                .container {{ max-width: 1200px; margin: auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                h1 {{ color: #0056b3; text-align: center; margin-bottom: 30px; }}
                .reservation-card {{ background: #f9f9f9; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 15px; padding: 15px; display: flex; align-items: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
                .reservation-card img {{ width: 100px; height: 60px; object-fit: cover; border-radius: 4px; margin-right: 15px; }}
                .reservation-details {{ flex-grow: 1; }}
                .reservation-details h2 {{ font-size: 1.1em; margin: 0 0 5px; color: #333; }}
                .reservation-details p {{ font-size: 0.9em; color: #666; margin: 2px 0; }}
                .reservation-actions a {{ background-color: #dc3545; color: white; padding: 8px 12px; border-radius: 5px; text-decoration: none; font-size: 0.9em; }}
                .reservation-actions a:hover {{ background-color: #c82333; }}
                .message {{ text-align: center; padding: 20px; background-color: #e9ecef; border-radius: 5px; margin-top: 20px; }}
                .navbar {{ background-color: #007bff; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
                .navbar a {{ color: white; text-decoration: none; padding: 8px 15px; border-radius: 5px; }}
                .navbar a:hover {{ background-color: #0056b3; }}
                .navbar .logo {{ font-weight: bold; font-size: 1.5em; }}
                .flash-messages {{ list-style: none; padding: 0; margin: 20px 0; }}
                .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
                .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
                .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
                .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
                .admin-link { background-color: #28a745; margin-left: 10px; }
                .admin-link:hover { background-color: #218838; }
            </style>
        </head>
        <body>
            <div class="navbar">
                <a href="/" class="logo">JG Minis</a>
                <div>
                    <a href="/home">Home</a>
                    """
        if 'user_id' in session:
            html_content += f"""
                    <a href="/minhas_reservas">Minhas Reservas</a>
                    <a href="/logout">Logout ({session.get('user_name', 'Usuário')})</a>
                    """
            if is_admin():
                html_content += """
                    <a href="/admin" class="admin-link">Admin</a>
                    """
        else:
            html_content += """
                    <a href="/login">Login</a>
                    <a href="/registro">Registro</a>
                    """
        html_content += """
                </div>
            </div>
            <div class="container">
                """
        if '_flashes' in session:
            html_content += '<ul class="flash-messages">'
            for category, message in session['_flashes']:
                html_content += f'<li class="{category}">{message}</li>'
            session.pop('_flashes', None)
            html_content += '</ul>'

        html_content += """
                <h1>Minhas Reservas</h1>
        """
        if reservas:
            for reserva in reservas:
                thumbnail_src = reserva['carro_thumbnail'] if reserva['carro_thumbnail'] else 'https://via.placeholder.com/100x60?text=Sem+Imagem'
                html_content += f"""
                <div class="reservation-card">
                    <img src="{thumbnail_src}" alt="{reserva['carro_modelo']}">
                    <div class="reservation-details">
                        <h2>{reserva['carro_modelo']}</h2>
                        <p>Data: {reserva['data_reserva']} ({reserva['hora_inicio']} - {reserva['hora_fim']})</p>
                        <p>Status: {reserva['status'].capitalize()}</p>
                        <p>Obs: {reserva['observacoes'] or 'Nenhuma'}</p>
                    </div>
                    <div class="reservation-actions">
                        """
                if reserva['status'] == 'pendente':
                    html_content += f"""
                        <a href="/cancelar_reserva/{reserva['id']}">Cancelar</a>
                        """
                html_content += """
                    </div>
                </div>
                """
        else:
            html_content += """
                <div class="message">
                    <p>Você não possui nenhuma reserva.</p>
                    <p><a href="/home">Ver carros disponíveis</a></p>
                </div>
            """
        html_content += """
            </div>
        </body>
        </html>
        """
        return html_content
    except Exception as e:
        flash('Erro ao carregar suas reservas.', 'error')
        logging.error(f'Erro ao carregar minhas reservas: {e}', exc_info=True)
        return redirect(url_for('home'))

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
        sync_reservas_to_sheets() # Sincroniza após o cancelamento
        sync_carros_to_sheets() # Sincroniza status do carro
    except Exception as e:
        flash(f'Erro ao cancelar reserva: {e}', 'error')
        logging.error(f'Erro ao cancelar reserva {reserva_id}: {e}', exc_info=True)
    finally:
        conn.close()
    return redirect(url_for('minhas_reservas'))

@app.route('/admin')
def admin_panel():
    """Painel administrativo."""
    if not is_admin():
        flash('Acesso negado. Você não tem permissão de administrador.', 'error')
        return redirect(url_for('home'))
    
    try:
        reservas = get_reservas()
        usuarios = get_usuarios()
        carros = get_all_cars()

        # HTML inline para o painel administrativo
        html_content = f"""
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>JG Minis - Admin</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; }}
                .container {{ max-width: 1200px; margin: auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                h1 {{ color: #0056b3; text-align: center; margin-bottom: 30px; }}
                h2 {{ color: #0056b3; margin-top: 30px; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
                .admin-actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }}
                .admin-actions a, .admin-actions button {{ background-color: #007bff; color: white; padding: 10px 15px; border-radius: 5px; text-decoration: none; border: none; cursor: pointer; font-size: 0.9em; }}
                .admin-actions a:hover, .admin-actions button:hover {{ background-color: #0056b3; }}
                .admin-actions .sync-btn {{ background-color: #28a745; }}
                .admin-actions .sync-btn:hover {{ background-color: #218838; }}
                .admin-actions .backup-btn {{ background-color: #ffc107; color: #333; }}
                .admin-actions .backup-btn:hover {{ background-color: #e0a800; }}
                .admin-actions .restore-btn {{ background-color: #dc3545; }}
                .admin-actions .restore-btn:hover {{ background-color: #c82333; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 0.9em; }}
                th {{ background-color: #f2f2f2; }}
                .table-actions a, .table-actions button {{ background-color: #007bff; color: white; padding: 5px 8px; border-radius: 3px; text-decoration: none; font-size: 0.8em; margin-right: 5px; border: none; cursor: pointer; }}
                .table-actions a.edit {{ background-color: #ffc107; color: #333; }}
                .table-actions a.delete {{ background-color: #dc3545; }}
                .table-actions a:hover, .table-actions button:hover {{ opacity: 0.9; }}
                .navbar {{ background-color: #007bff; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }}
                .navbar a {{ color: white; text-decoration: none; padding: 8px 15px; border-radius: 5px; }}
                .navbar a:hover {{ background-color: #0056b3; }}
                .navbar .logo {{ font-weight: bold; font-size: 1.5em; }}
                .flash-messages {{ list-style: none; padding: 0; margin: 20px 0; }}
                .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
                .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
                .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
                .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
                .admin-link { background-color: #28a745; margin-left: 10px; }
                .admin-link:hover { background-color: #218838; }
                .thumbnail-img { width: 50px; height: 30px; object-fit: cover; border-radius: 3px; }
            </style>
        </head>
        <body>
            <div class="navbar">
                <a href="/" class="logo">JG Minis</a>
                <div>
                    <a href="/home">Home</a>
                    """
        if 'user_id' in session:
            html_content += f"""
                    <a href="/minhas_reservas">Minhas Reservas</a>
                    <a href="/logout">Logout ({session.get('user_name', 'Usuário')})</a>
                    """
            if is_admin():
                html_content += """
                    <a href="/admin" class="admin-link">Admin</a>
                    """
        else:
            html_content += """
                    <a href="/login">Login</a>
                    <a href="/registro">Registro</a>
                    """
        html_content += """
                </div>
            </div>
            <div class="container">
                """
        if '_flashes' in session:
            html_content += '<ul class="flash-messages">'
            for category, message in session['_flashes']:
                html_content += f'<li class="{category}">{message}</li>'
            session.pop('_flashes', None)
            html_content += '</ul>'

        html_content += """
                <h1>Painel Administrativo</h1>
                <div class="admin-actions">
                    <a href="/admin/add_carro">Adicionar Novo Carro</a>
                    <a href="/admin/sync_sheets" class="sync-btn">Sincronizar com Sheets</a>
                    <a href="/admin/backup_db" class="backup-btn">Backup DB (JSON)</a>
                    <a href="/admin/restore_backup" class="restore-btn">Restaurar DB (JSON)</a>
                </div>

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
                            <th>Thumbnail</th>
                            <th>Ações</th>
                        </tr>
                    </thead>
                    <tbody>
        """
        if carros:
            for car in carros:
                thumbnail_src = car['thumbnail_url'] if car['thumbnail_url'] else 'https://via.placeholder.com/50x30?text=N/A'
                html_content += f"""
                        <tr>
                            <td>{car['id']}</td>
                            <td>{car['modelo']}</td>
                            <td>{car['ano']}</td>
                            <td>{car['cor']}</td>
                            <td>{car['placa']}</td>
                            <td>{'Sim' if car['disponivel'] else 'Não'}</td>
                            <td>R$ {car['preco_diaria']:.2f}</td>
                            <td><img src="{thumbnail_src}" class="thumbnail-img" alt="Thumbnail"></td>
                            <td class="table-actions">
                                <a href="/admin/edit_carro/{car['id']}" class="edit">Editar</a>
                                <a href="/admin/delete_carro/{car['id']}" class="delete" onclick="return confirm('Tem certeza que deseja deletar este carro?');">Deletar</a>
                            </td>
                        </tr>
                """
        else:
            html_content += """
                        <tr><td colspan="9">Nenhum carro cadastrado.</td></tr>
            """
        html_content += """
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
        """
        if usuarios:
            for user in usuarios:
                html_content += f"""
                        <tr>
                            <td>{user['id']}</td>
                            <td>{user['nome']}</td>
                            <td>{user['email']}</td>
                            <td>{user['cpf'] or 'N/A'}</td>
                            <td>{user['telefone'] or 'N/A'}</td>
                            <td>{'Sim' if user['is_admin'] else 'Não'}</td>
                            <td class="table-actions">
                                """
                if not user['is_admin']:
                    html_content += f"""
                                <a href="/admin/promote_admin/{user['id']}">Promover Admin</a>
                                """
                html_content += """
                            </td>
                        </tr>
                """
        else:
            html_content += """
                        <tr><td colspan="7">Nenhum usuário cadastrado.</td></tr>
            """
        html_content += """
                    </tbody>
                </table>

                <h2>Reservas</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Usuário</th>
                            <th>Carro</th>
                            <th>Data</th>
                            <th>Início</th>
                            <th>Fim</th>
                            <th>Status</th>
                            <th>Ações</th>
                        </tr>
                    </thead>
                    <tbody>
        """
        if reservas:
            for reserva in reservas:
                html_content += f"""
                        <tr>
                            <td>{reserva['id']}</td>
                            <td>{reserva['usuario_nome']}</td>
                            <td>{reserva['carro_modelo']}</td>
                            <td>{reserva['data_reserva']}</td>
                            <td>{reserva['hora_inicio']}</td>
                            <td>{reserva['hora_fim']}</td>
                            <td>{reserva['status'].capitalize()}</td>
                            <td class="table-actions">
                                """
                if reserva['status'] == 'pendente':
                    html_content += f"""
                                <a href="/admin/update_reserva_status/{reserva['id']}/confirmada" class="sync-btn">Confirmar</a>
                                <a href="/admin/update_reserva_status/{reserva['id']}/cancelada" class="delete">Cancelar</a>
                                """
                elif reserva['status'] == 'confirmada':
                    html_content += f"""
                                <a href="/admin/update_reserva_status/{reserva['id']}/concluida" class="sync-btn">Concluir</a>
                                """
                html_content += """
                            </td>
                        </tr>
                """
        else:
            html_content += """
                        <tr><td colspan="8">Nenhuma reserva encontrada.</td></tr>
            """
        html_content += """
                    </tbody>
                </table>
            </div>
        </body>
        </html>
        """
        return html_content
    except Exception as e:
        flash('Erro ao carregar o painel administrativo.', 'error')
        logging.error(f'Erro ao carregar painel admin: {e}', exc_info=True)
        return redirect(url_for('home'))

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
            sync_carros_to_sheets() # Sincroniza após adicionar carro
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao adicionar carro: {e}', 'error')
            logging.error(f'Erro ao adicionar carro: {e}', exc_info=True)
    
    # HTML inline para a página de adicionar carro
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Adicionar Carro</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .form-container {{ background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="text"], input[type="number"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            button:hover {{ background-color: #0056b3; }}
            .flash-messages {{ list-style: none; padding: 0; margin: 0 0 15px 0; }}
            .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Adicionar Novo Carro</h1>
            """
    if '_flashes' in session:
        html_content += '<ul class="flash-messages">'
        for category, message in session['_flashes']:
            html_content += f'<li class="{category}">{message}</li>'
        session.pop('_flashes', None)
        html_content += '</ul>'

    html_content += """
            <form method="POST">
                <label for="modelo">Modelo:</label>
                <input type="text" id="modelo" name="modelo" required>
                <label for="ano">Ano:</label>
                <input type="number" id="ano" name="ano" required>
                <label for="cor">Cor:</label>
                <input type="text" id="cor" name="cor" required>
                <label for="placa">Placa:</label>
                <input type="text" id="placa" name="placa" required>
                <label for="preco_diaria">Preço por Diária:</label>
                <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" required>
                <label for="thumbnail_url">URL da Miniatura (opcional):</label>
                <input type="text" id="thumbnail_url" name="thumbnail_url">
                <button type="submit">Adicionar Carro</button>
            </form>
        </div>
    </body>
    </html>
    """
    return html_content

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
            sync_carros_to_sheets() # Sincroniza após editar carro
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada para outro carro. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao editar carro: {e}', 'error')
            logging.error(f'Erro ao editar carro {car_id}: {e}', exc_info=True)
    
    # HTML inline para a página de editar carro
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Editar Carro</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .form-container {{ background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input[type="text"], input[type="number"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            input[type="checkbox"] {{ margin-right: 10px; }}
            button {{ width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            button:hover {{ background-color: #0056b3; }}
            .flash-messages {{ list-style: none; padding: 0; margin: 0 0 15px 0; }}
            .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Editar Carro</h1>
            """
    if '_flashes' in session:
        html_content += '<ul class="flash-messages">'
        for category, message in session['_flashes']:
            html_content += f'<li class="{category}">{message}</li>'
        session.pop('_flashes', None)
        html_content += '</ul>'

    html_content += f"""
            <form method="POST">
                <label for="modelo">Modelo:</label>
                <input type="text" id="modelo" name="modelo" value="{car['modelo']}" required>
                <label for="ano">Ano:</label>
                <input type="number" id="ano" name="ano" value="{car['ano']}" required>
                <label for="cor">Cor:</label>
                <input type="text" id="cor" name="cor" value="{car['cor']}" required>
                <label for="placa">Placa:</label>
                <input type="text" id="placa" name="placa" value="{car['placa']}" required>
                <label for="preco_diaria">Preço por Diária:</label>
                <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" value="{car['preco_diaria']}" required>
                <label for="thumbnail_url">URL da Miniatura (opcional):</label>
                <input type="text" id="thumbnail_url" name="thumbnail_url" value="{car['thumbnail_url'] or ''}">
                <label>
                    <input type="checkbox" id="disponivel" name="disponivel" {'checked' if car['disponivel'] else ''}>
                    Disponível
                </label>
                <button type="submit">Atualizar Carro</button>
            </form>
        </div>
    </body>
    </html>
    """
    return html_content

@app.route('/admin/delete_carro/<int:car_id>')
def admin_delete_carro(car_id):
    """Deleta um carro (apenas admin)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    try:
        # Verifica se há reservas ativas para este carro
        reservas_ativas = conn.execute('SELECT COUNT(*) FROM reservas WHERE carro_id = ? AND status IN (?, ?)', (car_id, 'pendente', 'confirmada')).fetchone()[0]
        if reservas_ativas > 0:
            flash(f'Não é possível deletar o carro. Existem {reservas_ativas} reservas ativas para ele.', 'error')
            return redirect(url_for('admin_panel'))

        conn.execute('DELETE FROM carros WHERE id = ?', (car_id,))
        conn.commit()
        flash('Carro deletado com sucesso!', 'success')
        logging.info(f'Carro {car_id} deletado.')
        sync_carros_to_sheets() # Sincroniza após deletar carro
    except Exception as e:
        flash(f'Erro ao deletar carro: {e}', 'error')
        logging.error(f'Erro ao deletar carro {car_id}: {e}', exc_info=True)
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
        sync_reservas_to_sheets() # Sincroniza após atualizar status
    except Exception as e:
        flash(f'Erro ao atualizar status da reserva: {e}', 'error')
        logging.error(f'Erro ao atualizar status da reserva: {e}', exc_info=True)
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
        sync_usuarios_to_sheets() # Sincroniza após promover admin
    except Exception as e:
        flash(f'Erro ao promover usuário a admin: {e}', 'error')
        logging.error(f'Erro ao promover usuário {user_id} a admin: {e}', exc_info=True)
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
        sync_reservas_to_sheets()
        sync_usuarios_to_sheets()
        sync_carros_to_sheets()
        flash('Dados sincronizados com o Google Sheets com sucesso!', 'success')
        logging.info('Todas as abas do Google Sheets sincronizadas.')
    except Exception as e:
        flash(f'Erro geral ao sincronizar com Google Sheets: {e}', 'error')
        logging.error(f'Erro geral ao sincronizar com Google Sheets: {e}', exc_info=True)
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
        logging.error(f'Erro ao gerar backup do DB: {e}', exc_info=True)
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/admin/restore_backup', methods=['GET', 'POST'])
def admin_restore_backup():
    """Restaura o banco de dados a partir de um arquivo JSON de backup (apenas admin)."""
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

                # Verifica integridade do hash
                received_hash = backup_data.pop('hash', None)
                if received_hash:
                    # Recalcula o hash do conteúdo sem o hash original para comparação
                    calculated_hash = hashlib.sha256(json.dumps(backup_data, indent=4, ensure_ascii=False).encode()).hexdigest()
                    if received_hash != calculated_hash:
                        flash('Erro de integridade do backup: hash não corresponde.', 'error')
                        logging.error('Erro de integridade do backup: hash não corresponde.')
                        return redirect(url_for('admin_restore_backup'))
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
                              (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'], car_data['placa'], car_data['disponivel'], car_data['preco_diaria'], car_data.get('thumbnail_url')))
                
                # Restaura usuários
                for user_data in backup_data.get('usuarios', []):
                    c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (user_data['id'], user_data['nome'], user_data['email'], user_data['senha_hash'], user_data['cpf'], user_data['telefone'], user_data['data_cadastro'], user_data['is_admin']))
                
                # Restaura reservas
                for reserva_data in backup_data.get('reservas', []):
                    c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (reserva_data['id'], reserva_data['usuario_id'], reserva_data['carro_id'], reserva_data['data_reserva'], reserva_data['hora_inicio'], reserva_data['hora_fim'], reserva_data['status'], reserva_data['observacoes']))
                
                conn.commit()
                flash('Backup restaurado com sucesso!', 'success')
                logging.info('Backup restaurado com sucesso.')
                # Sincroniza com Sheets após a restauração para refletir os dados
                sync_reservas_to_sheets()
                sync_usuarios_to_sheets()
                sync_carros_to_sheets()

            except json.JSONDecodeError:
                flash('Arquivo de backup inválido: não é um JSON válido.', 'error')
                logging.error('Erro: Arquivo de backup inválido (JSONDecodeError).', exc_info=True)
            except KeyError as ke:
                flash(f'Arquivo de backup inválido: chave ausente - {ke}.', 'error')
                logging.error(f'Erro: Arquivo de backup inválido (KeyError: {ke}).', exc_info=True)
            except Exception as e:
                flash(f'Erro ao restaurar backup: {e}', 'error')
                logging.error(f'Erro ao restaurar backup: {e}', exc_info=True)
            finally:
                conn.close()
        else:
            flash('Por favor, selecione um arquivo JSON válido.', 'error')
    
    # HTML inline para a página de restaurar backup
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Restaurar Backup</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f4f4; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .form-container {{ background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            input[type="file"] {{ width: calc(100% - 22px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; }}
            button {{ width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            button:hover {{ background-color: #0056b3; }}
            .flash-messages {{ list-style: none; padding: 0; margin: 0 0 15px 0; }}
            .flash-messages li {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .flash-messages .success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-messages .error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-messages .info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Restaurar Backup do Banco de Dados</h1>
            """
    if '_flashes' in session:
        html_content += '<ul class="flash-messages">'
        for category, message in session['_flashes']:
            html_content += f'<li class="{category}">{message}</li>'
        session.pop('_flashes', None)
        html_content += '</ul>'

    html_content += """
            <form method="POST" enctype="multipart/form-data">
                <label for="backup_file">Selecione o arquivo JSON de backup:</label>
                <input type="file" id="backup_file" name="backup_file" accept=".json" required>
                <button type="submit">Restaurar</button>
            </form>
        </div>
    </body>
    </html>
    """
    return html_content

# --- Tratamento de Erros ---
@app.errorhandler(404)
def page_not_found(e):
    """Trata erros 404 (Página não encontrada)."""
    logging.warning(f"404 Not Found: {request.url}")
    return """
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>404 - Página Não Encontrada</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f4f4f4; color: #333; }
            h1 { color: #dc3545; }
            p { font-size: 1.1em; }
            a { color: #007bff; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <h1>404 - Página Não Encontrada</h1>
        <p>A página que você está procurando não existe.</p>
        <p><a href="/">Voltar para a página inicial</a></p>
    </body>
    </html>
    """, 404

@app.errorhandler(500)
def internal_server_error(e):
    """Trata erros 500 (Erro interno do servidor)."""
    logging.error(f"500 Internal Server Error: {e}", exc_info=True)
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return """
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>500 - Erro Interno do Servidor</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f4f4f4; color: #333; }
            h1 { color: #dc3545; }
            p { font-size: 1.1em; }
            a { color: #007bff; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <h1>500 - Erro Interno do Servidor</h1>
        <p>Ocorreu um erro inesperado. Nossa equipe já foi notificada.</p>
        <p><a href="/">Voltar para a página inicial</a></p>
    </body>
    </html>
    """, 500

# O Gunicorn (servidor de produção) irá chamar a instância 'app' diretamente.
# Não precisamos do bloco if __name__ == '__main__': app.run() para deploy.
# Apenas um 'pass' para manter a estrutura se o arquivo for executado diretamente,
# mas o Gunicorn não o usará.
if __name__ == '__main__':
    pass
