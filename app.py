from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_jgminis_v4.3.19') # Chave secreta para sessões

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

        # Verifica contagens existentes para logs
        c.execute('SELECT COUNT(*) FROM reservas')
        reservas_count = c.fetchone()[0]
        if reservas_count == 0:
            logging.warning('DB inicializado: 0 reservas encontradas.')
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
            logging.warning('DB inicializado: 0 carros encontrados.')
        else:
            logging.info(f'DB inicializado: {carros_count} carros preservados.')

        logging.info('App bootado com sucesso.')
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
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', 'SUA_SHEET_ID_AQUI') 

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

def get_sheet_data(worksheet_name):
    """Lê todos os dados de uma aba específica do Google Sheets."""
    if not gspread_client or GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning(f'gspread: Não é possível ler a aba {worksheet_name}. Cliente não inicializado ou GOOGLE_SHEET_ID não configurado.')
        return None

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet(worksheet_name)
        data = sheet.get_all_records() # Retorna uma lista de dicionários
        logging.info(f'gspread: Dados da aba "{worksheet_name}" lidos com sucesso ({len(data)} registros).')
        return data
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f'gspread: Aba "{worksheet_name}" não encontrada na planilha. Verifique o nome da aba.')
        return None
    except Exception as e:
        logging.error(f'gspread: Erro ao ler dados da aba "{worksheet_name}": {e}')
        return None

def update_sheet_data(worksheet_name, data_list, headers):
    """Atualiza uma aba do Google Sheets com uma lista de dicionários."""
    if not gspread_client or GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        logging.warning(f'gspread: Não é possível atualizar a aba {worksheet_name}. Cliente não inicializado ou GOOGLE_SHEET_ID não configurado.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet(worksheet_name)
        sheet.clear()
        
        # Converte a lista de dicionários para uma lista de listas, mantendo a ordem dos cabeçalhos
        rows_to_append = [headers]
        for item in data_list:
            row = [item.get(header, '') for header in headers]
            rows_to_append.append(row)
        
        sheet.append_rows(rows_to_append)
        logging.info(f'gspread: Aba "{worksheet_name}" atualizada com sucesso ({len(data_list)} registros).')
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f'gspread: Aba "{worksheet_name}" não encontrada para atualização. Verifique o nome da aba.')
    except Exception as e:
        logging.error(f'gspread: Erro ao atualizar dados da aba "{worksheet_name}": {e}')

def sync_from_sheets_to_db():
    """Sincroniza dados do Google Sheets para o banco de dados local."""
    logging.info('Iniciando sincronização do Google Sheets para o DB local...')
    conn = get_db_connection()
    c = conn.cursor()

    try:
        # Sincronizar Usuários
        users_from_sheets = get_sheet_data('Usuarios')
        if users_from_sheets:
            c.execute('DELETE FROM usuarios') # Limpa DB para evitar duplicatas
            for user_data in users_from_sheets:
                # Garante que 'senha_hash' existe ou gera uma temporária se ausente
                senha_hash = user_data.get('senha_hash')
                if not senha_hash:
                    senha_hash = hashlib.sha256('temp_password'.encode()).hexdigest()
                    logging.warning(f"Usuário {user_data.get('Email')} sem senha_hash no Sheets. Gerando senha temporária.")

                c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (user_data.get('ID'), user_data.get('Nome'), user_data.get('Email'), senha_hash,
                           user_data.get('CPF'), user_data.get('Telefone'), user_data.get('Data Cadastro'),
                           user_data.get('Admin') == 'Sim'))
            logging.info(f'Sincronizados {len(users_from_sheets)} usuários do Sheets para o DB.')
        else:
            logging.warning('Nenhum usuário encontrado no Google Sheets para sincronizar.')

        # Sincronizar Carros
        cars_from_sheets = get_sheet_data('Carros')
        if cars_from_sheets:
            c.execute('DELETE FROM carros') # Limpa DB
            for car_data in cars_from_sheets:
                c.execute('INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (car_data.get('ID'), car_data.get('Modelo'), car_data.get('Ano'), car_data.get('Cor'),
                           car_data.get('Placa'), car_data.get('Disponivel') == 'Sim', car_data.get('Preco Diaria'),
                           car_data.get('thumbnail_url')))
            logging.info(f'Sincronizados {len(cars_from_sheets)} carros do Sheets para o DB.')
        else:
            logging.warning('Nenhum carro encontrado no Google Sheets para sincronizar.')

        # Sincronizar Reservas
        reservas_from_sheets = get_sheet_data('Reservas')
        if reservas_from_sheets:
            c.execute('DELETE FROM reservas') # Limpa DB
            for reserva_data in reservas_from_sheets:
                # Converte 'Usuário' e 'Carro' para IDs se necessário, ou assume que IDs já estão no Sheets
                # Para simplificar, assumimos que 'usuario_id' e 'carro_id' estão no Sheets ou podem ser inferidos
                # Aqui, vamos usar os IDs diretamente se disponíveis, ou buscar por nome/modelo
                # Para esta versão, assumimos que o Sheets pode ter 'usuario_id' e 'carro_id' ou que o app os gerencia
                # Se o Sheets usa 'Usuário' e 'Carro' (nomes), precisaria de um lookup para IDs
                # Para manter a simplicidade, vamos mapear os campos do Sheets para os campos do DB
                c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (reserva_data.get('ID'), reserva_data.get('usuario_id'), reserva_data.get('carro_id'),
                           reserva_data.get('Data'), reserva_data.get('Hora Início'), reserva_data.get('Hora Fim'),
                           reserva_data.get('Status'), reserva_data.get('Observacoes')))
            logging.info(f'Sincronizadas {len(reservas_from_sheets)} reservas do Sheets para o DB.')
        else:
            logging.warning('Nenhuma reserva encontrada no Google Sheets para sincronizar.')

        conn.commit()
        logging.info('Sincronização do Google Sheets para o DB local concluída com sucesso.')
        return True
    except Exception as e:
        logging.error(f'Erro durante a sincronização do Google Sheets para o DB local: {e}')
        return False
    finally:
        conn.close()

def sync_db_to_sheets():
    """Sincroniza dados do banco de dados local para o Google Sheets."""
    logging.info('Iniciando sincronização do DB local para o Google Sheets...')
    try:
        # Sincronizar Usuários
        usuarios = get_usuarios_from_db()
        headers_usuarios = ['ID', 'Nome', 'Email', 'CPF', 'Telefone', 'Data Cadastro', 'Admin']
        usuarios_for_sheet = []
        for u in usuarios:
            usuarios_for_sheet.append({
                'ID': u['id'], 'Nome': u['nome'], 'Email': u['email'], 'CPF': u['cpf'],
                'Telefone': u['telefone'], 'Data Cadastro': u['data_cadastro'], 'Admin': 'Sim' if u['is_admin'] else 'Não'
            })
        update_sheet_data('Usuarios', usuarios_for_sheet, headers_usuarios)

        # Sincronizar Carros
        carros = get_carros_from_db()
        headers_carros = ['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponivel', 'Preco Diaria', 'thumbnail_url']
        carros_for_sheet = []
        for c_db in carros:
            carros_for_sheet.append({
                'ID': c_db['id'], 'Modelo': c_db['modelo'], 'Ano': c_db['ano'], 'Cor': c_db['cor'],
                'Placa': c_db['placa'], 'Disponivel': 'Sim' if c_db['disponivel'] else 'Não',
                'Preco Diaria': c_db['preco_diaria'], 'thumbnail_url': c_db['thumbnail_url']
            })
        update_sheet_data('Carros', carros_for_sheet, headers_carros)

        # Sincronizar Reservas
        reservas = get_reservas_from_db()
        headers_reservas = ['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status', 'Observacoes', 'usuario_id', 'carro_id']
        reservas_for_sheet = []
        for r_db in reservas:
            # Para o Sheets, podemos querer o nome do usuário e modelo do carro
            user = get_user_by_id(r_db['usuario_id'])
            car = get_car_by_id(r_db['carro_id'])
            reservas_for_sheet.append({
                'ID': r_db['id'], 'Usuário': user['nome'] if user else 'Desconhecido',
                'Carro': car['modelo'] if car else 'Desconhecido', 'Data': r_db['data_reserva'],
                'Hora Início': r_db['hora_inicio'], 'Hora Fim': r_db['hora_fim'], 'Status': r_db['status'],
                'Observacoes': r_db['observacoes'], 'usuario_id': r_db['usuario_id'], 'carro_id': r_db['carro_id']
            })
        update_sheet_data('Reservas', reservas_for_sheet, headers_reservas)

        logging.info('Sincronização do DB local para o Google Sheets concluída com sucesso.')
        return True
    except Exception as e:
        logging.error(f'Erro durante a sincronização do DB local para o Google Sheets: {e}')
        return False

# Funções de leitura de dados (agora priorizam Sheets, com fallback para DB)
def get_usuarios():
    """Busca todos os usuários, priorizando Google Sheets."""
    if gspread_client and GOOGLE_SHEET_ID != 'SUA_SHEET_ID_AQUI':
        users_from_sheets = get_sheet_data('Usuarios')
        if users_from_sheets:
            # Converte para o formato esperado (sqlite3.Row-like)
            # Nota: Isso é uma simplificação. Para um mapeamento completo,
            # seria necessário converter tipos e garantir todas as chaves.
            # Aqui, assumimos que os nomes das colunas do Sheets correspondem aos do DB.
            # E que 'Admin' no Sheets é 'Sim'/'Não' e no DB é BOOLEAN.
            formatted_users = []
            for u in users_from_sheets:
                row_dict = {k.lower().replace(' ', '_'): v for k, v in u.items()}
                row_dict['is_admin'] = (u.get('Admin') == 'Sim')
                formatted_users.append(sqlite3.Row(list(row_dict.keys()), list(row_dict.values())))
            return formatted_users
    logging.warning('Falha ao carregar usuários do Google Sheets. Carregando do DB local como fallback.')
    return get_usuarios_from_db()

def get_usuarios_from_db():
    """Busca todos os usuários do banco de dados local."""
    try:
        conn = get_db_connection()
        usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
        conn.close()
        if len(usuarios) == 0:
            logging.info('DB local: 0 usuários encontrados.')
        return usuarios
    except Exception as e:
        logging.error(f"Erro ao carregar usuários do DB local: {e}")
        return []

def get_user_by_id(user_id):
    """Busca um usuário pelo ID, priorizando Google Sheets."""
    users = get_usuarios() # Já prioriza Sheets
    for user in users:
        if user['id'] == user_id:
            return user
    return None

def get_user_by_email(email):
    """Busca um usuário pelo email, priorizando Google Sheets."""
    users = get_usuarios() # Já prioriza Sheets
    for user in users:
        if user['email'] == email:
            return user
    return None

def get_all_cars():
    """Busca todos os carros, priorizando Google Sheets."""
    if gspread_client and GOOGLE_SHEET_ID != 'SUA_SHEET_ID_AQUI':
        cars_from_sheets = get_sheet_data('Carros')
        if cars_from_sheets:
            formatted_cars = []
            for c_sheet in cars_from_sheets:
                row_dict = {k.lower().replace(' ', '_'): v for k, v in c_sheet.items()}
                row_dict['disponivel'] = (c_sheet.get('Disponivel') == 'Sim')
                formatted_cars.append(sqlite3.Row(list(row_dict.keys()), list(row_dict.values())))
            return formatted_cars
    logging.warning('Falha ao carregar carros do Google Sheets. Carregando do DB local como fallback.')
    return get_carros_from_db()

def get_carros_from_db():
    """Busca todos os carros do banco de dados local."""
    try:
        conn = get_db_connection()
        cars = conn.execute('SELECT * FROM carros').fetchall()
        conn.close()
        if len(cars) == 0:
            logging.info('DB local: 0 carros encontrados.')
        return cars
    except Exception as e:
        logging.error(f"Erro ao carregar carros do DB local: {e}")
        return []

def get_car_by_id(car_id):
    """Busca um carro pelo ID, priorizando Google Sheets."""
    cars = get_all_cars() # Já prioriza Sheets
    for car in cars:
        if car['id'] == car_id:
            return car
    return None

def get_reservas():
    """Busca todas as reservas, priorizando Google Sheets."""
    if gspread_client and GOOGLE_SHEET_ID != 'SUA_SHEET_ID_AQUI':
        reservas_from_sheets = get_sheet_data('Reservas')
        if reservas_from_sheets:
            formatted_reservas = []
            for r_sheet in reservas_from_sheets:
                row_dict = {k.lower().replace(' ', '_'): v for k, v in r_sheet.items()}
                # Mapear 'Usuário' e 'Carro' para 'usuario_id' e 'carro_id' se necessário
                # Para esta versão, assumimos que 'usuario_id' e 'carro_id' estão no Sheets
                formatted_reservas.append(sqlite3.Row(list(row_dict.keys()), list(row_dict.values())))
            return formatted_reservas
    logging.warning('Falha ao carregar reservas do Google Sheets. Carregando do DB local como fallback.')
    return get_reservas_from_db()

def get_reservas_from_db():
    """Busca todas as reservas do banco de dados local."""
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
        if len(reservas) == 0:
            logging.info('DB local: 0 reservas encontradas.')
        return reservas
    except Exception as e:
        logging.error(f"Erro ao carregar reservas do DB local: {e}")
        return []

# Inicializa o DB no nível do módulo para garantir que esteja pronto para Gunicorn
# E tenta sincronizar do Sheets para o DB na inicialização
init_db()
if gspread_client and GOOGLE_SHEET_ID != 'SUA_SHEET_ID_AQUI':
    logging.info('Tentando sincronizar dados do Google Sheets para o DB local na inicialização...')
    sync_from_sheets_to_db()
else:
    logging.warning('Sincronização inicial do Google Sheets para o DB local pulada. Usando apenas DB local.')

# --- Rotas ---
@app.route('/')
def index():
    """Redireciona para a página inicial."""
    return redirect(url_for('home'))

@app.route('/health')
def health():
    """Endpoint de saúde para verificação do Railway."""
    return 'OK', 200

@app.route('/home')
def home():
    """Página inicial com lista de carros disponíveis (HTML inline)."""
    try:
        carros = get_all_cars()
        
        # HTML inline para a página home
        html_content = """
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>JG Minis - Home</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
                .container { width: 80%; margin: 20px auto; background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
                h1 { color: #0056b3; text-align: center; }
                .navbar { background-color: #007bff; padding: 10px 0; text-align: center; }
                .navbar a { color: white; text-decoration: none; padding: 0 15px; }
                .car-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-top: 20px; }
                .car-item { background-color: #e9ecef; padding: 15px; border-radius: 5px; text-align: center; }
                .car-item img { max-width: 100%; height: 150px; object-fit: cover; border-radius: 5px; margin-bottom: 10px; }
                .car-item h3 { margin: 10px 0; color: #0056b3; }
                .car-item p { font-size: 0.9em; color: #555; }
                .car-item .price { font-weight: bold; color: #28a745; font-size: 1.1em; margin-top: 10px; }
                .car-item a { background-color: #007bff; color: white; padding: 8px 15px; border-radius: 5px; text-decoration: none; margin-top: 10px; display: inline-block; }
                .car-item a:hover { background-color: #0056b3; }
                .message { text-align: center; padding: 20px; background-color: #ffeeba; border: 1px solid #ffc107; border-radius: 5px; margin-top: 20px; }
                .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
                .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
                .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
                .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
                .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
            </style>
        </head>
        <body>
            <div class="navbar">
                <a href="/">Home</a>
                <a href="/registro">Registro</a>
                <a href="/login">Login</a>
                <a href="/minhas_reservas">Minhas Reservas</a>
                {% if session.get('is_admin') %}
                <a href="/admin">Admin</a>
                {% endif %}
                {% if session.get('user_id') %}
                <a href="/logout">Logout ({{ session.get('user_name') }})</a>
                {% endif %}
            </div>
            <div class="container">
                <h1>Carros Disponíveis</h1>
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="flash-message flash-{{ category }}">{{ message }}</div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                {% if carros %}
                    <div class="car-list">
                        {% for car in carros %}
                            <div class="car-item">
                                {% if car.thumbnail_url %}
                                <img src="{{ car.thumbnail_url }}" alt="Miniatura do {{ car.modelo }}">
                                {% else %}
                                <img src="https://via.placeholder.com/150?text=Sem+Imagem" alt="Sem Imagem">
                                {% endif %}
                                <h3>{{ car.modelo }} ({{ car.ano }})</h3>
                                <p>Cor: {{ car.cor }}</p>
                                <p>Placa: {{ car.placa }}</p>
                                <div class="price">R$ {{ car.preco_diaria | round(2) }} / diária</div>
                                {% if car.disponivel %}
                                <a href="/reservar/{{ car.id }}">Reservar</a>
                                {% else %}
                                <p style="color: red;">Indisponível</p>
                                {% endif %}
                            </div>
                        {% endfor %}
                    </div>
                {% else %}
                    <div class="message">
                        <p>Nenhum carro disponível no momento. Por favor, adicione carros via painel administrativo.</p>
                    </div>
                {% endif %}
            </div>
        </body>
        </html>
        """
        return render_template_string(html_content, carros=carros, session=session, get_flashed_messages=flash)
    except Exception as e:
        logging.error(f"Erro ao carregar a página home: {e}")
        flash('Erro ao carregar a lista de carros. Tente novamente mais tarde.', 'error')
        return render_template_string("<h1>Erro ao carregar carros</h1><p>Tente novamente mais tarde.</p>", session=session, get_flashed_messages=flash)

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    """Rota para registro de novos usuários (HTML inline)."""
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

            # Obter o próximo ID para o usuário
            c = conn.cursor()
            c.execute("SELECT MAX(id) FROM usuarios")
            max_id = c.fetchone()[0]
            new_id = (max_id or 0) + 1

            conn.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone) VALUES (?, ?, ?, ?, ?, ?)',
                         (new_id, nome, email, senha_hash, cleaned_cpf, cleaned_telefone))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            logging.info(f'Novo usuário registrado: {email}')
            sync_db_to_sheets() # Sincroniza DB para Sheets
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email ou CPF já cadastrado. Tente novamente com outros dados.', 'error')
        except Exception as e:
            flash(f'Erro ao registrar usuário: {e}', 'error')
            logging.error(f'Erro no registro de usuário: {e}')
        finally:
            conn.close()
    
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Registro</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
            .register-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 400px; }
            h1 { color: #0056b3; text-align: center; margin-bottom: 20px; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input[type="text"],
            .form-group input[type="email"],
            .form-group input[type="password"] {
                width: calc(100% - 20px);
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }
            .btn-submit {
                width: 100%;
                padding: 10px;
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }
            .btn-submit:hover { background-color: #218838; }
            .link-login { text-align: center; margin-top: 20px; }
            .link-login a { color: #007bff; text-decoration: none; }
            .link-login a:hover { text-decoration: underline; }
            .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
            .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        </style>
    </head>
    <body>
        <div class="register-container">
            <h1>Registro</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="POST">
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
                    <input type="text" id="cpf" name="cpf" placeholder="Ex: 123.456.789-00" required>
                </div>
                <div class="form-group">
                    <label for="telefone">Telefone:</label>
                    <input type="text" id="telefone" name="telefone" placeholder="Ex: (DD) 9XXXX-XXXX" required>
                </div>
                <button type="submit" class="btn-submit">Registrar</button>
            </form>
            <div class="link-login">
                Já tem uma conta? <a href="/login">Faça Login</a>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, session=session, get_flashed_messages=flash)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Rota para login de usuários (HTML inline)."""
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']

        user = get_user_by_email(email) # Prioriza Sheets
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
        
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Login</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
            .login-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 400px; }
            h1 { color: #0056b3; text-align: center; margin-bottom: 20px; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input[type="email"],
            .form-group input[type="password"] {
                width: calc(100% - 20px);
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }
            .btn-submit {
                width: 100%;
                padding: 10px;
                background-color: #007bff;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }
            .btn-submit:hover { background-color: #0056b3; }
            .link-register { text-align: center; margin-top: 20px; }
            .link-register a { color: #28a745; text-decoration: none; }
            .link-register a:hover { text-decoration: underline; }
            .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
            .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        </style>
    </head>
    <body>
        <div class="login-container">
            <h1>Login</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="POST">
                <div class="form-group">
                    <label for="email">Email:</label>
                    <input type="email" id="email" name="email" required>
                </div>
                <div class="form-group">
                    <label for="senha">Senha:</label>
                    <input type="password" id="senha" name="senha" required>
                </div>
                <button type="submit" class="btn-submit">Entrar</button>
            </form>
            <div class="link-register">
                Não tem uma conta? <a href="/registro">Registre-se</a>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, session=session, get_flashed_messages=flash)

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
    """Rota para reservar um carro (HTML inline)."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para fazer uma reserva.', 'warning')
        return redirect(url_for('login'))

    car = get_car_by_id(car_id) # Prioriza Sheets
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
            c = conn.cursor()
            c.execute("SELECT MAX(id) FROM reservas")
            max_id = c.fetchone()[0]
            new_id = (max_id or 0) + 1

            conn.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (new_id, session['user_id'], car_id, data_reserva, hora_inicio, hora_fim, observacoes))
            conn.execute('UPDATE carros SET disponivel = FALSE WHERE id = ?', (car_id,))
            conn.commit()
            conn.close()

            flash('Reserva realizada com sucesso!', 'success')
            logging.info(f"Reserva criada: Usuário {session['user_id']} reservou carro {car_id} para {data_reserva}")
            sync_db_to_sheets() # Sincroniza DB para Sheets
            return redirect(url_for('minhas_reservas'))
        except ValueError:
            flash('Formato de data ou hora inválido.', 'error')
        except Exception as e:
            flash(f'Erro ao realizar reserva: {e}', 'error')
            logging.error(f'Erro ao realizar reserva: {e}')
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Reservar {car['modelo']}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }}
            .container {{ width: 80%; margin: 20px auto; background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            h1 {{ color: #0056b3; text-align: center; }}
            .navbar {{ background-color: #007bff; padding: 10px 0; text-align: center; }}
            .navbar a {{ color: white; text-decoration: none; padding: 0 15px; }}
            .car-details {{ display: flex; align-items: center; margin-bottom: 20px; border: 1px solid #ddd; padding: 15px; border-radius: 8px; }}
            .car-details img {{ max-width: 150px; height: auto; margin-right: 20px; border-radius: 5px; }}
            .car-details div {{ flex-grow: 1; }}
            .car-details h2 {{ margin-top: 0; color: #0056b3; }}
            .form-group {{ margin-bottom: 15px; }}
            .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            .form-group input[type="date"],
            .form-group input[type="time"],
            .form-group textarea {{
                width: calc(100% - 20px);
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }}
            .btn-submit {{
                width: 100%;
                padding: 10px;
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }}
            .btn-submit:hover {{ background-color: #218838; }}
            .flash-message {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }}
            .flash-success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
            .flash-warning {{ background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }}
        </style>
    </head>
    <body>
        <div class="navbar">
            <a href="/">Home</a>
            <a href="/registro">Registro</a>
            <a href="/login">Login</a>
            <a href="/minhas_reservas">Minhas Reservas</a>
            {% if session.get('is_admin') %}
            <a href="/admin">Admin</a>
            {% endif %}
            {% if session.get('user_id') %}
            <a href="/logout">Logout ({{ session.get('user_name') }})</a>
            {% endif %}
        </div>
        <div class="container">
            <h1>Reservar Carro</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <div class="car-details">
                {% if car.thumbnail_url %}
                <img src="{car['thumbnail_url']}" alt="Miniatura do {car['modelo']}">
                {% else %}
                <img src="https://via.placeholder.com/150?text=Sem+Imagem" alt="Sem Imagem">
                {% endif %}
                <div>
                    <h2>{car['modelo']} ({car['ano']})</h2>
                    <p>Cor: {car['cor']}</p>
                    <p>Placa: {car['placa']}</p>
                    <p>Preço: R$ {car['preco_diaria']:.2f} / diária</p>
                </div>
            </div>
            <form method="POST">
                <div class="form-group">
                    <label for="data_reserva">Data da Reserva:</label>
                    <input type="date" id="data_reserva" name="data_reserva" required>
                </div>
                <div class="form-group">
                    <label for="hora_inicio">Hora de Início:</label>
                    <input type="time" id="hora_inicio" name="hora_inicio" required>
                </div>
                <div class="form-group">
                    <label for="hora_fim">Hora de Fim:</label>
                    <input type="time" id="hora_fim" name="hora_fim" required>
                </div>
                <div class="form-group">
                    <label for="observacoes">Observações:</label>
                    <textarea id="observacoes" name="observacoes" rows="4"></textarea>
                </div>
                <button type="submit" class="btn-submit">Confirmar Reserva</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, car=car, session=session, get_flashed_messages=flash)

@app.route('/minhas_reservas')
def minhas_reservas():
    """Exibe as reservas do usuário logado (HTML inline)."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para ver suas reservas.', 'warning')
        return redirect(url_for('login'))

    reservas = get_reservas() # Prioriza Sheets
    user_reservas = [r for r in reservas if r['usuario_id'] == session['user_id']]

    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Minhas Reservas</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
            .container { width: 80%; margin: 20px auto; background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h1 { color: #0056b3; text-align: center; }
            .navbar { background-color: #007bff; padding: 10px 0; text-align: center; }
            .navbar a { color: white; text-decoration: none; padding: 0 15px; }
            .reservas-table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            .reservas-table th, .reservas-table td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            .reservas-table th { background-color: #007bff; color: white; }
            .reservas-table tr:nth-child(even) { background-color: #f2f2f2; }
            .reservas-table tr:hover { background-color: #ddd; }
            .status-pendente { color: orange; font-weight: bold; }
            .status-confirmada { color: green; font-weight: bold; }
            .status-cancelada { color: red; font-weight: bold; }
            .btn-cancelar { background-color: #dc3545; color: white; padding: 5px 10px; border-radius: 4px; text-decoration: none; font-size: 0.9em; }
            .btn-cancelar:hover { background-color: #c82333; }
            .message { text-align: center; padding: 20px; background-color: #ffeeba; border: 1px solid #ffc107; border-radius: 5px; margin-top: 20px; }
            .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
            .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <a href="/">Home</a>
            <a href="/registro">Registro</a>
            <a href="/login">Login</a>
            <a href="/minhas_reservas">Minhas Reservas</a>
            {% if session.get('is_admin') %}
            <a href="/admin">Admin</a>
            {% endif %}
            {% if session.get('user_id') %}
            <a href="/logout">Logout ({{ session.get('user_name') }})</a>
            {% endif %}
        </div>
        <div class="container">
            <h1>Minhas Reservas</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            {% if user_reservas %}
                <table class="reservas-table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Carro</th>
                            <th>Data</th>
                            <th>Início</th>
                            <th>Fim</th>
                            <th>Status</th>
                            <th>Observações</th>
                            <th>Ações</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for reserva in user_reservas %}
                            <tr>
                                <td>{{ reserva.id }}</td>
                                <td>{{ reserva.carro_modelo }}</td>
                                <td>{{ reserva.data_reserva }}</td>
                                <td>{{ reserva.hora_inicio }}</td>
                                <td>{{ reserva.hora_fim }}</td>
                                <td class="status-{{ reserva.status }}">{{ reserva.status }}</td>
                                <td>{{ reserva.observacoes }}</td>
                                <td>
                                    {% if reserva.status == 'pendente' %}
                                    <a href="/cancelar_reserva/{{ reserva.id }}" class="btn-cancelar">Cancelar</a>
                                    {% else %}
                                    -
                                    {% endif %}
                                </td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="message">
                    <p>Você não possui nenhuma reserva.</p>
                </div>
            {% endif %}
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, user_reservas=user_reservas, session=session, get_flashed_messages=flash)

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
        sync_db_to_sheets() # Sincroniza DB para Sheets
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
    
    reservas = get_reservas() # Prioriza Sheets
    usuarios = get_usuarios() # Prioriza Sheets
    carros = get_all_cars() # Prioriza Sheets

    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Painel Admin</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; color: #333; }
            .container { width: 90%; margin: 20px auto; background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h1, h2 { color: #0056b3; text-align: center; margin-bottom: 20px; }
            .navbar { background-color: #007bff; padding: 10px 0; text-align: center; }
            .navbar a { color: white; text-decoration: none; padding: 0 15px; }
            .section { margin-bottom: 40px; }
            .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
            .section-header h2 { margin: 0; }
            .btn-action { background-color: #28a745; color: white; padding: 8px 15px; border-radius: 5px; text-decoration: none; margin-left: 10px; }
            .btn-action.red { background-color: #dc3545; }
            .btn-action.orange { background-color: #ffc107; color: #333; }
            .btn-action.blue { background-color: #007bff; }
            .btn-action:hover { opacity: 0.9; }
            .data-table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            .data-table th, .data-table td { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 0.9em; }
            .data-table th { background-color: #007bff; color: white; }
            .data-table tr:nth-child(even) { background-color: #f2f2f2; }
            .data-table tr:hover { background-color: #ddd; }
            .data-table img { max-width: 80px; height: auto; border-radius: 3px; }
            .status-pendente { color: orange; font-weight: bold; }
            .status-confirmada { color: green; font-weight: bold; }
            .status-cancelada { color: red; font-weight: bold; }
            .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
            .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        </style>
    </head>
    <body>
        <div class="navbar">
            <a href="/">Home</a>
            <a href="/registro">Registro</a>
            <a href="/login">Login</a>
            <a href="/minhas_reservas">Minhas Reservas</a>
            {% if session.get('is_admin') %}
            <a href="/admin">Admin</a>
            {% endif %}
            {% if session.get('user_id') %}
            <a href="/logout">Logout ({{ session.get('user_name') }})</a>
            {% endif %}
        </div>
        <div class="container">
            <h1>Painel Administrativo</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <div class="section">
                <div class="section-header">
                    <h2>Carros</h2>
                    <div>
                        <a href="/admin/add_carro" class="btn-action">Adicionar Carro</a>
                    </div>
                </div>
                {% if carros %}
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Miniatura</th>
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
                            {% for car in carros %}
                                <tr>
                                    <td>{{ car.id }}</td>
                                    <td>
                                        {% if car.thumbnail_url %}
                                        <img src="{{ car.thumbnail_url }}" alt="Miniatura">
                                        {% else %}
                                        -
                                        {% endif %}
                                    </td>
                                    <td>{{ car.modelo }}</td>
                                    <td>{{ car.ano }}</td>
                                    <td>{{ car.cor }}</td>
                                    <td>{{ car.placa }}</td>
                                    <td>{{ 'Sim' if car.disponivel else 'Não' }}</td>
                                    <td>R$ {{ car.preco_diaria | round(2) }}</td>
                                    <td>
                                        <a href="/admin/edit_carro/{{ car.id }}" class="btn-action orange">Editar</a>
                                        <a href="/admin/delete_carro/{{ car.id }}" class="btn-action red">Deletar</a>
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>Nenhum carro cadastrado.</p>
                {% endif %}
            </div>

            <div class="section">
                <div class="section-header">
                    <h2>Usuários</h2>
                    <div>
                        <a href="/registro" class="btn-action">Novo Usuário</a>
                    </div>
                </div>
                {% if usuarios %}
                    <table class="data-table">
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
                            {% for user in usuarios %}
                                <tr>
                                    <td>{{ user.id }}</td>
                                    <td>{{ user.nome }}</td>
                                    <td>{{ user.email }}</td>
                                    <td>{{ user.cpf }}</td>
                                    <td>{{ user.telefone }}</td>
                                    <td>{{ 'Sim' if user.is_admin else 'Não' }}</td>
                                    <td>
                                        {% if not user.is_admin %}
                                        <a href="/admin/promote_admin/{{ user.id }}" class="btn-action blue">Promover Admin</a>
                                        {% else %}
                                        Admin
                                        {% endif %}
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>Nenhum usuário cadastrado.</p>
                {% endif %}
            </div>

            <div class="section">
                <div class="section-header">
                    <h2>Reservas</h2>
                    <div>
                        <!-- Botão para adicionar reserva manualmente, se necessário -->
                    </div>
                </div>
                {% if reservas %}
                    <table class="data-table">
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
                            {% for reserva in reservas %}
                                <tr>
                                    <td>{{ reserva.id }}</td>
                                    <td>{{ reserva.usuario_nome }}</td>
                                    <td>{{ reserva.carro_modelo }}</td>
                                    <td>{{ reserva.data_reserva }}</td>
                                    <td>{{ reserva.hora_inicio }}</td>
                                    <td>{{ reserva.hora_fim }}</td>
                                    <td class="status-{{ reserva.status }}">{{ reserva.status }}</td>
                                    <td>
                                        {% if reserva.status == 'pendente' %}
                                        <a href="/admin/update_reserva_status/{{ reserva.id }}/confirmada" class="btn-action">Confirmar</a>
                                        <a href="/admin/update_reserva_status/{{ reserva.id }}/cancelada" class="btn-action red">Cancelar</a>
                                        {% else %}
                                        -
                                        {% endif %}
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>Nenhuma reserva cadastrada.</p>
                {% endif %}
            </div>

            <div class="section">
                <div class="section-header">
                    <h2>Ferramentas de Sincronização e Backup</h2>
                    <div>
                        <a href="/admin/sync_sheets" class="btn-action blue">Sincronizar com Google Sheets</a>
                        <a href="/admin/backup_db" class="btn-action">Gerar Backup DB</a>
                        <a href="/admin/restore_backup" class="btn-action orange">Restaurar Backup DB</a>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, reservas=reservas, usuarios=usuarios, carros=carros, session=session, get_flashed_messages=flash)

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
        
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT MAX(id) FROM carros")
            max_id = c.fetchone()[0]
            new_id = (max_id or 0) + 1

            conn.execute('INSERT INTO carros (id, modelo, ano, cor, placa, preco_diaria, thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (new_id, modelo, ano, cor, placa, float(preco_diaria), thumbnail_url))
            conn.commit()
            conn.close()
            flash('Carro adicionado com sucesso!', 'success')
            logging.info(f'Carro adicionado: {modelo} ({placa})')
            sync_db_to_sheets() # Sincroniza DB para Sheets
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao adicionar carro: {e}', 'error')
            logging.error(f'Erro ao adicionar carro: {e}')
    
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Adicionar Carro</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
            .form-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }
            h1 { color: #0056b3; text-align: center; margin-bottom: 20px; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input[type="text"],
            .form-group input[type="number"] {
                width: calc(100% - 20px);
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }
            .btn-submit {
                width: 100%;
                padding: 10px;
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }
            .btn-submit:hover { background-color: #218838; }
            .btn-back { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; }
            .btn-back:hover { text-decoration: underline; }
            .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
            .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Adicionar Novo Carro</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="POST">
                <div class="form-group">
                    <label for="modelo">Modelo:</label>
                    <input type="text" id="modelo" name="modelo" required>
                </div>
                <div class="form-group">
                    <label for="ano">Ano:</label>
                    <input type="number" id="ano" name="ano" required>
                </div>
                <div class="form-group">
                    <label for="cor">Cor:</label>
                    <input type="text" id="cor" name="cor" required>
                </div>
                <div class="form-group">
                    <label for="placa">Placa:</label>
                    <input type="text" id="placa" name="placa" required>
                </div>
                <div class="form-group">
                    <label for="preco_diaria">Preço Diária:</label>
                    <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" required>
                </div>
                <div class="form-group">
                    <label for="thumbnail_url">URL da Miniatura (Opcional):</label>
                    <input type="text" id="thumbnail_url" name="thumbnail_url">
                </div>
                <button type="submit" class="btn-submit">Adicionar Carro</button>
            </form>
            <a href="/admin" class="btn-back">Voltar ao Painel Admin</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, session=session, get_flashed_messages=flash)

@app.route('/admin/edit_carro/<int:car_id>', methods=['GET', 'POST'])
def admin_edit_carro(car_id):
    """Edita um carro existente (apenas admin) (HTML inline)."""
    if not is_admin():
        flash('Acesso negado.', 'error')
        return redirect(url_for('home'))

    car = get_car_by_id(car_id) # Prioriza Sheets
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
        
        conn = get_db_connection()
        try:
            conn.execute('UPDATE carros SET modelo = ?, ano = ?, cor = ?, placa = ?, preco_diaria = ?, disponivel = ?, thumbnail_url = ? WHERE id = ?',
                         (modelo, ano, cor, placa, float(preco_diaria), disponivel, thumbnail_url, car_id))
            conn.commit()
            conn.close()
            flash('Carro atualizado com sucesso!', 'success')
            logging.info(f'Carro {car_id} atualizado: {modelo} ({placa})')
            sync_db_to_sheets() # Sincroniza DB para Sheets
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada para outro carro. Verifique os dados.', 'error')
        except ValueError:
            flash('Preço diária inválido. Use um número.', 'error')
        except Exception as e:
            flash(f'Erro ao editar carro: {e}', 'error')
            logging.error(f'Erro ao editar carro {car_id}: {e}')
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Editar Carro</title>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
            .form-container {{ background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }}
            h1 {{ color: #0056b3; text-align: center; margin-bottom: 20px; }}
            .form-group {{ margin-bottom: 15px; }}
            .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            .form-group input[type="text"],
            .form-group input[type="number"] {{
                width: calc(100% - 20px);
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }}
            .form-group input[type="checkbox"] {{ margin-right: 5px; }}
            .btn-submit {{
                width: 100%;
                padding: 10px;
                background-color: #007bff;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }}
            .btn-submit:hover {{ background-color: #0056b3; }}
            .btn-back {{ display: block; text-align: center; margin-top: 20px; color: #dc3545; text-decoration: none; }}
            .btn-back:hover {{ text-decoration: underline; }}
            .flash-message {{ padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }}
            .flash-success {{ background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .flash-error {{ background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .flash-info {{ background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
            .flash-warning {{ background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }}
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Editar Carro</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="POST">
                <div class="form-group">
                    <label for="modelo">Modelo:</label>
                    <input type="text" id="modelo" name="modelo" value="{car['modelo']}" required>
                </div>
                <div class="form-group">
                    <label for="ano">Ano:</label>
                    <input type="number" id="ano" name="ano" value="{car['ano']}" required>
                </div>
                <div class="form-group">
                    <label for="cor">Cor:</label>
                    <input type="text" id="cor" name="cor" value="{car['cor']}" required>
                </div>
                <div class="form-group">
                    <label for="placa">Placa:</label>
                    <input type="text" id="placa" name="placa" value="{car['placa']}" required>
                </div>
                <div class="form-group">
                    <label for="preco_diaria">Preço Diária:</label>
                    <input type="number" id="preco_diaria" name="preco_diaria" step="0.01" value="{car['preco_diaria']}" required>
                </div>
                <div class="form-group">
                    <label for="thumbnail_url">URL da Miniatura (Opcional):</label>
                    <input type="text" id="thumbnail_url" name="thumbnail_url" value="{car['thumbnail_url'] or ''}">
                </div>
                <div class="form-group">
                    <input type="checkbox" id="disponivel" name="disponivel" {'checked' if car['disponivel'] else ''}>
                    <label for="disponivel">Disponível</label>
                </div>
                <button type="submit" class="btn-submit">Atualizar Carro</button>
            </form>
            <a href="/admin" class="btn-back">Voltar ao Painel Admin</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, car=car, session=session, get_flashed_messages=flash)

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
        sync_db_to_sheets() # Sincroniza DB para Sheets
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
        sync_db_to_sheets() # Sincroniza DB para Sheets
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
        sync_db_to_sheets() # Sincroniza DB para Sheets
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

    if not gspread_client or GOOGLE_SHEET_ID == 'SUA_SHEET_ID_AQUI':
        flash('Sincronização com Google Sheets desativada (credenciais ausentes ou GOOGLE_SHEET_ID não configurado).', 'error')
        return redirect(url_for('admin_panel'))

    try:
        # Primeiro, puxa do Sheets para o DB (para garantir que o DB tenha as últimas do Sheets)
        logging.info('Executando sync_from_sheets_to_db...')
        if not sync_from_sheets_to_db():
            flash('Erro ao puxar dados do Google Sheets para o DB.', 'error')
            return redirect(url_for('admin_panel'))

        # Depois, empurra do DB para o Sheets (para garantir que o Sheets tenha as últimas do DB, incluindo IDs gerados)
        logging.info('Executando sync_db_to_sheets...')
        if not sync_db_to_sheets():
            flash('Erro ao empurrar dados do DB para o Google Sheets.', 'error')
            return redirect(url_for('admin_panel'))

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
                              (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'], car_data['placa'], car_data['disponivel'], car_data['preco_diaria'], car_data['thumbnail_url']))
                
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
                # Re-sincroniza para o Sheets após restaurar o DB local
                sync_db_to_sheets()

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
    
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JG Minis - Restaurar Backup</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
            .form-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); width: 100%; max-width: 500px; }
            h1 { color: #0056b3; text-align: center; margin-bottom: 20px; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
            .form-group input[type="file"] {
                width: calc(100% - 20px);
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 4px;
                box-sizing: border-box;
            }
            .btn-submit {
                width: 100%;
                padding: 10px;
                background-color: #ffc107;
                color: #333;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }
            .btn-submit:hover { background-color: #e0a800; }
            .btn-back { display: block; text-align: center; margin-top: 20px; color: #007bff; text-decoration: none; }
            .btn-back:hover { text-decoration: underline; }
            .flash-message { padding: 10px; margin-bottom: 10px; border-radius: 5px; text-align: center; }
            .flash-success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .flash-info { background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .flash-warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>Restaurar Backup do Banco de Dados</h1>
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="flash-message flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="POST" enctype="multipart/form-data">
                <div class="form-group">
                    <label for="backup_file">Selecione o arquivo JSON de backup:</label>
                    <input type="file" id="backup_file" name="backup_file" accept=".json" required>
                </div>
                <button type="submit" class="btn-submit">Restaurar Backup</button>
            </form>
            <a href="/admin" class="btn-back">Voltar ao Painel Admin</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content, session=session, get_flashed_messages=flash)

# --- Tratamento de Erros ---
from flask import render_template_string # Importar aqui para evitar circular import se usado em error handlers

@app.errorhandler(404)
def page_not_found(e):
    """Trata erros 404 (Página não encontrada) (HTML inline)."""
    logging.warning(f"404 Not Found: {request.url}")
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>404 - Página Não Encontrada</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; text-align: center; }
            .error-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h1 { color: #dc3545; font-size: 3em; margin-bottom: 10px; }
            p { color: #666; font-size: 1.2em; }
            a { color: #007bff; text-decoration: none; margin-top: 20px; display: inline-block; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="error-container">
            <h1>404</h1>
            <p>Página Não Encontrada</p>
            <p>A URL que você tentou acessar não existe.</p>
            <a href="/">Voltar para a Home</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content), 404

@app.errorhandler(500)
def internal_server_error(e):
    """Trata erros 500 (Erro interno do servidor) (HTML inline)."""
    logging.error(f"500 Internal Server Error: {e}", exc_info=True)
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>500 - Erro Interno do Servidor</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; text-align: center; }
            .error-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h1 { color: #dc3545; font-size: 3em; margin-bottom: 10px; }
            p { color: #666; font-size: 1.2em; }
            a { color: #007bff; text-decoration: none; margin-top: 20px; display: inline-block; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="error-container">
            <h1>500</h1>
            <p>Erro Interno do Servidor</p>
            <p>Ocorreu um problema inesperado. Por favor, tente novamente mais tarde.</p>
            <a href="/">Voltar para a Home</a>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content), 500

# O Gunicorn (servidor de produção) irá chamar a instância 'app' diretamente.
# Não precisamos do bloco if __name__ == '__main__': app.run() para deploy.
# Apenas um 'pass' para manter a estrutura se o arquivo for executado diretamente,
# mas o Gunicorn não o usará.
if __name__ == '__main__':
    # Para desenvolvimento local, você pode descomentar e usar app.run()
    # app.run(debug=True)
    pass
