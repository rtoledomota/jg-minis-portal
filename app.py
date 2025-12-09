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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_jgminis_v4.3.11') # Chave secreta para sessões

# Define o caminho do banco de dados, usando variável de ambiente ou padrão
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
            preco_diaria REAL NOT NULL
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
            logging.warning('DB inicializado: 0 reservas encontradas. Considere restaurar de um backup JSON.')
        else:
            logging.info(f'DB inicializado: {reservas_count} reservas preservadas.')

        c.execute('SELECT COUNT(*) FROM usuarios')
        usuarios_count = c.fetchone()[0]
        if usuarios_count == 0:
            logging.warning('DB inicializado: 0 usuários encontrados. Considere restaurar de um backup JSON.')
            # Cria usuário admin padrão se DB estiver vazio
            admin_email = 'admin@jgminis.com.br'
            admin_password_hash = hashlib.sha256('admin123'.encode()).hexdigest()
            c.execute('INSERT INTO usuarios (nome, email, senha_hash, is_admin) VALUES (?, ?, ?, ?)',
                      ('Admin', admin_email, admin_password_hash, True))
            conn.commit()
            logging.info(f'Usuário admin padrão criado: {admin_email}')
        else:
            logging.info(f'DB inicializado: {usuarios_count} cadastros preservados.')

        c.execute('SELECT COUNT(*) FROM carros')
        carros_count = c.fetchone()[0]
        if carros_count == 0:
            logging.warning('DB inicializado: 0 carros encontrados. Adicione carros ou restaure de um backup JSON.')
        else:
            logging.info(f'DB inicializado: {carros_count} carros preservados.')

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
    """Busca todos os carros."""
    conn = get_db_connection()
    cars = conn.execute('SELECT * FROM carros').fetchall()
    conn.close()
    if len(cars) == 0:
        logging.info('DB vazio: 0 carros encontrados.')
    return cars

def get_reservas():
    """Busca todas as reservas com detalhes de usuário e carro."""
    conn = get_db_connection()
    c = conn.cursor()
    # Seleciona colunas explicitamente na ordem para facilitar o mapeamento para Sheets
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
        logging.info('DB vazio: 0 reservas encontradas.')
    else:
        logging.info(f'Reservas: Encontradas {len(reservas)} registros no DB.')
    conn.close()
    return reservas

def get_usuarios():
    """Busca todos os usuários."""
    conn = get_db_connection()
    usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
    conn.close()
    if len(usuarios) == 0:
        logging.info('DB vazio: 0 usuários encontrados.')
    else:
        logging.info(f'Usuários: Encontrados {len(usuarios)} registros no DB.')
    return usuarios

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
            headers = ['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status']
            # Mapeia os dados da tupla para a ordem dos cabeçalhos
            data_to_append = [
                [
                    str(r[0]),  # r.id
                    str(r[1]),  # u.nome
                    str(r[2]),  # c.modelo
                    str(r[3]),  # r.data_reserva
                    str(r[4]),  # r.hora_inicio
                    str(r[5]),  # r.hora_fim
                    str(r[6])   # r.status
                ] for r in reservas
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync reservas: {len(reservas)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status']])
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
            headers = ['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponível', 'Preço Diária']
            data_to_append = [
                [
                    str(c['id']),
                    str(c['modelo']),
                    str(c['ano']),
                    str(c['cor']),
                    str(c['placa']),
                    'Sim' if c['disponivel'] else 'Não',
                    f"R$ {c['preco_diaria']:.2f}"
                ] for c in carros
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync carros: {len(carros)} registros sincronizados com o Google Sheets.')
        else:
            sheet.append_rows([['ID', 'Modelo', 'Ano', 'Cor', 'Placa', 'Disponível', 'Preço Diária']])
            logging.info('Sync carros: Nenhum carro para sincronizar. Aba limpa e cabeçalhos adicionados.')
    except Exception as e:
        logging.error(f'Erro na sincronização de carros com Google Sheets: {e}')
        flash('Erro ao sincronizar carros com o Google Sheets.', 'error')

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
        if not carros:
            flash('Nenhum carro disponível no momento. Por favor, adicione carros via painel administrativo.', 'info')
        return render_template('home.html', carros=carros)
    except Exception as e:
        logging.error(f"Erro ao carregar carros na home: {e}")
        flash('Erro ao carregar a lista de carros. Tente novamente mais tarde.', 'error')
        return render_template('home.html', carros=[])

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
            return render_template('registro.html')
        
        # Validação de CPF (apenas dígitos, 11 ou 14 caracteres para CPF/CNPJ)
        cleaned_cpf = ''.join(filter(str.isdigit, cpf))
        if not (cleaned_cpf.isdigit() and 10 <= len(cleaned_cpf) <= 11): # Corrigido <=
            flash('CPF inválido. Deve conter 11 dígitos.', 'error')
            return render_template('registro.html')

        # Validação de Telefone (apenas dígitos, 10 ou 11 caracteres)
        cleaned_telefone = ''.join(filter(str.isdigit, telefone))
        if not (cleaned_telefone.isdigit() and 10 <= len(cleaned_telefone) <= 11): # Corrigido <=
            flash('Telefone inválido. Deve conter 10 ou 11 dígitos.', 'error')
            return render_template('registro.html')

        conn = get_db_connection()
        try:
            # Hash da senha
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
    return render_template('registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Rota para login de usuários."""
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']

        conn = get_db_connection()
        user = conn.execute('SELECT * FROM usuarios WHERE email = ?', (email,)).fetchone()
        conn.close()

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
    return render_template('login.html')

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
                return render_template('reservar.html', car=car)
            if data_reserva == datetime.now().date() and hora_inicio < datetime.now().time():
                flash('Não é possível reservar para um horário passado no dia de hoje.', 'error')
                return render_template('reservar.html', car=car)
            if hora_inicio >= hora_fim:
                flash('A hora de início deve ser anterior à hora de fim.', 'error')
                return render_template('reservar.html', car=car)

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
            logging.error(f'Erro ao realizar reserva: {e}')
    
    return render_template('reservar.html', car=car)

@app.route('/minhas_reservas')
def minhas_reservas():
    """Exibe as reservas do usuário logado."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para ver suas reservas.', 'warning')
        return redirect(url_for('login'))

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT 
                    r.id, 
                    c.modelo as carro_modelo, 
                    r.data_reserva, 
                    r.hora_inicio, 
                    r.hora_fim, 
                    r.status,
                    r.observacoes
                 FROM reservas r 
                 JOIN carros c ON r.carro_id = c.id 
                 WHERE r.usuario_id = ? 
                 ORDER BY r.data_reserva DESC''', (session['user_id'],))
    reservas = c.fetchall()
    conn.close()
    return render_template('minhas_reservas.html', reservas=reservas)

@app.route('/cancelar_reserva/<int:reserva_id>')
def cancelar_reserva(reserva_id):
    """Cancela uma reserva."""
    if 'user_id' not in session:
        flash('Você precisa estar logado para cancelar uma reserva.', 'warning')
        return redirect(url_for('login'))

    conn = get_db_connection()
    reserva = conn.execute('SELECT * FROM reservas WHERE id = ? AND usuario_id = ?', (reserva_id, session['user_id'])).fetchone()

    if not reserva:
        flash('Reserva não encontrada ou você não tem permissão para cancelá-la.', 'error')
        conn.close()
        return redirect(url_for('minhas_reservas'))

    try:
        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', ('cancelada', reserva_id))
        conn.execute('UPDATE carros SET disponivel = TRUE WHERE id = ?', (reserva['carro_id'],))
        conn.commit()
        flash('Reserva cancelada com sucesso!', 'success')
        logging.info(f"Reserva {reserva_id} cancelada pelo usuário {session['user_id']}")
        sync_reservas_to_sheets() # Sincroniza após o cancelamento
        sync_carros_to_sheets() # Sincroniza status do carro
    except Exception as e:
        flash(f'Erro ao cancelar reserva: {e}', 'error')
        logging.error(f'Erro ao cancelar reserva {reserva_id}: {e}')
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
        return render_template('admin.html', reservas=reservas, usuarios=usuarios, carros=carros)
    except Exception as e:
        logging.error(f"Erro ao carregar painel admin: {e}")
        flash('Erro ao carregar dados do painel administrativo.', 'error')
        return render_template('admin.html', reservas=[], usuarios=[], carros=[])

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

        if not modelo or not ano or not cor or not placa or not preco_diaria:
            flash('Todos os campos são obrigatórios.', 'error')
            return render_template('admin_add_carro.html')
        
        try:
            conn = get_db_connection()
            conn.execute('INSERT INTO carros (modelo, ano, cor, placa, preco_diaria) VALUES (?, ?, ?, ?, ?)',
                         (modelo, ano, cor, placa, float(preco_diaria)))
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
            logging.error(f'Erro ao adicionar carro: {e}')
    return render_template('admin_add_carro.html')

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

        if not modelo or not ano or not cor or not placa or not preco_diaria:
            flash('Todos os campos são obrigatórios.', 'error')
            return render_template('admin_edit_carro.html', car=car)
        
        try:
            conn = get_db_connection()
            conn.execute('UPDATE carros SET modelo = ?, ano = ?, cor = ?, placa = ?, preco_diaria = ?, disponivel = ? WHERE id = ?',
                         (modelo, ano, cor, placa, float(preco_diaria), disponivel, car_id))
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
            logging.error(f'Erro ao editar carro {car_id}: {e}')
    return render_template('admin_edit_carro.html', car=car)

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
        sync_reservas_to_sheets() # Sincroniza após atualizar status
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
        sync_usuarios_to_sheets() # Sincroniza após promover admin
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
        sync_reservas_to_sheets()
        sync_usuarios_to_sheets()
        sync_carros_to_sheets()
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

    if request.method == 'POST':
        if 'backup_file' not in request.files:
            flash('Nenhum arquivo de backup selecionado.', 'error')
            return redirect(url_for('admin_panel'))
        
        file = request.files['backup_file']
        if file.filename == '':
            flash('Nenhum arquivo de backup selecionado.', 'error')
            return redirect(url_for('admin_panel'))
        
        if file and file.filename.endswith('.json'):
            try:
                backup_content = file.read().decode('utf-8')
                backup_data = json.loads(backup_content)

                # Verifica integridade do hash
                received_hash = backup_data.pop('hash', None)
                if received_hash:
                    # Recria o JSON sem o hash para calcular o hash do conteúdo
                    content_for_hash = json.dumps(backup_data, indent=4, ensure_ascii=False)
                    calculated_hash = hashlib.sha256(content_for_hash.encode()).hexdigest()
                    if received_hash != calculated_hash:
                        flash('Erro de integridade do backup: hash não corresponde.', 'error')
                        logging.error('Erro de integridade do backup: hash não corresponde.')
                        return redirect(url_for('admin_panel'))
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
                    c.execute('INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria) VALUES (?, ?, ?, ?, ?, ?, ?)',
                              (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'], car_data['placa'], car_data['disponivel'], car_data['preco_diaria']))
                
                # Restaura usuários
                for user_data in backup_data.get('usuarios', []):
                    c.execute('INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (user_data['id'], user_data['nome'], user_data['email'], user_data['senha_hash'], user_data['cpf'], user_data['telefone'], user_data['data_cadastro'], user_data['is_admin']))
                
                # Restaura reservas
                for reserva_data in backup_data.get('reservas', []):
                    c.execute('INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                              (reserva_data['id'], reserva_data['usuario_id'], reserva_data['carro_id'], reserva_data['data_reserva'], reserva_data['hora_inicio'], reserva_data['hora_fim'], reserva_data['status'], reserva_data['observacoes']))
                
                conn.commit()
                conn.close()
                flash('Banco de dados restaurado com sucesso a partir do backup!', 'success')
                logging.info(f"DB restaurado: {len(backup_data.get('reservas', []))} reservas, {len(backup_data.get('usuarios', []))} usuários, {len(backup_data.get('carros', []))} carros.")
                
                # Sincroniza com Sheets após restauração
                sync_reservas_to_sheets()
                sync_usuarios_to_sheets()
                sync_carros_to_sheets()

            except json.JSONDecodeError:
                flash('Arquivo de backup inválido: não é um JSON válido.', 'error')
                logging.error('Arquivo de backup inválido: JSONDecodeError.')
            except sqlite3.Error as e:
                flash(f'Erro ao restaurar banco de dados: {e}', 'error')
                logging.error(f'Erro SQLite ao restaurar DB: {e}')
            except Exception as e:
                flash(f'Erro inesperado ao restaurar backup: {e}', 'error')
                logging.error(f'Erro inesperado ao restaurar backup: {e}')
        else:
            flash('Por favor, selecione um arquivo JSON válido.', 'error')
    return redirect(url_for('admin_panel'))

# --- Tratamento de Erros ---
@app.errorhandler(500)
def internal_server_error(e):
    """Manipulador de erro para 500 Internal Server Error."""
    logging.exception('Ocorreu um erro interno no servidor.')
    flash('Ocorreu um erro inesperado no servidor. Por favor, tente novamente mais tarde.', 'error')
    return render_template('error.html', error_message='Erro Interno do Servidor'), 500

@app.errorhandler(404)
def page_not_found(e):
    """Manipulador de erro para 404 Not Found."""
    return render_template('error.html', error_message='Página não encontrada'), 404

if __name__ == '__main__':
    app.run(debug=True)
