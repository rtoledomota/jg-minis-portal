import os
import re
import json
import csv
import io
import logging
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, url_for, render_template_string, flash, send_file, abort, make_response, render_template, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import hashlib # Para hash de senhas e verificação de backup
from io import BytesIO

# --- 1. Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_jg_minis_v4_3_9_production_stable')

# --- 3. Environment Variables ---
LOGO_URL = os.environ.get('LOGO_URL', 'https://i.imgur.com/Yp1OiWB.jpeg')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '5511949094290')  # Just numbers, no + or spaces
DATABASE_PATH = os.environ.get('DATABASE_PATH', '/tmp/jgminis.db') # CRÍTICO: Usar /tmp para persistência no Railway
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', 'SUA_SHEET_ID_AQUI') # ID da sua planilha

# Fallback for gspread - use only if available
GSPREAD_AVAILABLE = False
gc = None
sheet = None
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
    logging.info('google-auth e gspread importados com sucesso.')
except ImportError:
    logging.error('Erro: As bibliotecas google-auth ou gspread não foram encontradas. As funcionalidades de sincronização com Google Sheets não estarão disponíveis.')
    Credentials = None
    gspread = None

# --- 4. Database Helper Functions (Preservation Guaranteed) ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH) # Usar o caminho persistente
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # Users table - IF NOT EXISTS preserves existing data
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

        # Carros table - IF NOT EXISTS preserves existing data
        c.execute('''CREATE TABLE IF NOT EXISTS carros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            modelo TEXT NOT NULL,
            ano INTEGER,
            cor TEXT,
            placa TEXT UNIQUE,
            disponivel BOOLEAN DEFAULT TRUE,
            preco_diaria REAL NOT NULL
        )''')

        # Reservations table - IF NOT EXISTS preserves existing data
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

        # Create admin user ONLY if not exists (preserves existing admins)
        c.execute('SELECT id FROM usuarios WHERE email = ?', ('admin@jgminis.com.br',))
        if not c.fetchone():
            hashed_pw = hashlib.sha256('admin123'.encode()).hexdigest()
            c.execute('INSERT INTO usuarios (nome, email, senha_hash, telefone, is_admin) VALUES (?, ?, ?, ?, ?)',
                      ('Admin', 'admin@jgminis.com.br', hashed_pw, '11999999999', True))
            logging.info('Usuário admin criado no DB (primeira vez)')

        # Verifica contagens existentes para logs
        c.execute('SELECT COUNT(*) FROM reservas')
        reservas_count = c.fetchone()[0]
        logging.info(f'DB inicializado: {reservas_count} reservas preservadas.')

        c.execute('SELECT COUNT(*) FROM usuarios')
        usuarios_count = c.fetchone()[0]
        logging.info(f'DB inicializado: {usuarios_count} cadastros preservados.')

        c.execute('SELECT COUNT(*) FROM carros')
        carros_count = c.fetchone()[0]
        logging.info(f'DB inicializado: {carros_count} carros preservados.')

        conn.commit()
        logging.info('DB inicializado sem perda de dados - tabelas preservadas')
    except Exception as e:
        logging.error(f'Erro no init_db: {e}')
        conn.rollback()
    finally:
        conn.close()

# Inicializa o DB no nível do módulo para garantir que esteja pronto para Gunicorn
init_db()

# --- 5. Validation Functions ---
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    cleaned = re.sub(r'[^\d]', '', phone)  # Remove non-digits
    return cleaned.isdigit() and 10 <= len(cleaned) <= 11

def is_valid_cpf(cpf):
    cleaned = ''.join(filter(str.isdigit, cpf))
    return cleaned.isdigit() and len(cleaned) == 11

# --- 6. User/Car/Reservation Getters ---
def get_user_by_id(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return user

def get_user_by_email(email):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM usuarios WHERE email = ?', (email,)).fetchone()
    conn.close()
    return user

def get_car_by_id(car_id):
    conn = get_db_connection()
    car = conn.execute('SELECT * FROM carros WHERE id = ?', (car_id,)).fetchone()
    conn.close()
    return car

def get_all_cars():
    conn = get_db_connection()
    cars = conn.execute('SELECT * FROM carros').fetchall()
    conn.close()
    return cars

def get_reservas():
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
    logging.info(f'Reservas: Encontradas {len(reservas)} registros no DB.')
    conn.close()
    return reservas

def get_usuarios():
    conn = get_db_connection()
    usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
    conn.close()
    logging.info(f'Usuários: Encontrados {len(usuarios)} registros no DB.')
    return usuarios

# --- 7. Authentication and Authorization ---
def is_admin():
    if 'user_id' in session:
        user = get_user_by_id(session['user_id'])
        return user and user['is_admin']
    return False

# --- 8. Google Sheets Integration (gspread) ---
gspread_client = None

def init_gspread_client():
    global gspread_client
    if gspread_client:
        return gspread_client

    if not GSPREAD_AVAILABLE:
        logging.warning('gspread ou google-auth não disponíveis. Sincronização com Sheets desativada.')
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
            logging.error('gspread: Nenhuma credencial encontrada (variável de ambiente ou arquivo).')
            return None

        gspread_client = gspread.authorize(creds)
        logging.info('gspread: Autenticação bem-sucedida.')
        return gspread_client
    except Exception as e:
        logging.error(f'Erro na autenticação gspread: {e}')
        return None

# Inicializa o cliente gspread no nível do módulo
gspread_client = init_gspread_client()

def sync_reservas_to_sheets():
    if not gspread_client:
        logging.error('Sync reservas falhou: Cliente gspread não inicializado.')
        return

    try:
        sheet = gspread_client.open_by_key(GOOGLE_SHEET_ID).worksheet('Reservas')
        sheet.clear()

        reservas = get_reservas()
        if reservas:
            headers = ['ID', 'Usuário', 'Carro', 'Data', 'Hora Início', 'Hora Fim', 'Status']
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
            logging.info(f'Sync reservas: {len(reservas)} registros atualizados no Sheets.')
        else:
            logging.warning('Sync reservas: Nenhuma reserva para sincronizar.')
    except Exception as e:
        logging.error(f'Erro no sync de reservas para Sheets: {e}')
        flash('Erro ao sincronizar reservas com o Google Sheets.')

def sync_usuarios_to_sheets():
    if not gspread_client:
        logging.error('Sync usuários falhou: Cliente gspread não inicializado.')
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
                    str(u['cpf']),
                    str(u['telefone']),
                    str(u['data_cadastro']),
                    'Sim' if u['is_admin'] else 'Não'
                ] for u in usuarios
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync usuários: {len(usuarios)} registros atualizados no Sheets.')
        else:
            logging.warning('Sync usuários: Nenhum usuário para sincronizar.')
    except Exception as e:
        logging.error(f'Erro no sync de usuários para Sheets: {e}')
        flash('Erro ao sincronizar usuários com o Google Sheets.')

def sync_carros_to_sheets():
    if not gspread_client:
        logging.error('Sync carros falhou: Cliente gspread não inicializado.')
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
                    str(c['preco_diaria'])
                ] for c in carros
            ]
            sheet.append_rows([headers] + data_to_append)
            logging.info(f'Sync carros: {len(carros)} registros atualizados no Sheets.')
        else:
            logging.warning('Sync carros: Nenhum carro para sincronizar.')
    except Exception as e:
        logging.error(f'Erro no sync de carros para Sheets: {e}')
        flash('Erro ao sincronizar carros com o Google Sheets.')

# --- 9. Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        senha = request.form['senha']
        cpf = request.form['cpf']
        telefone = request.form['telefone']

        if not nome or not email or not senha:
            flash('Todos os campos obrigatórios devem ser preenchidos.')
            return redirect(url_for('registro'))
        if len(senha) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.')
            return redirect(url_for('registro'))
        if not is_valid_cpf(cpf):
            flash('CPF inválido. Deve conter 11 dígitos.')
            return redirect(url_for('registro'))
        if not is_valid_phone(telefone):
            flash('Telefone inválido. Deve conter 10 ou 11 dígitos.')
            return redirect(url_for('registro'))

        cleaned_cpf = ''.join(filter(str.isdigit, cpf))
        cleaned_telefone = ''.join(filter(str.isdigit, telefone))

        conn = get_db_connection()
        try:
            existing_user_email = conn.execute('SELECT id FROM usuarios WHERE email = ?', (email,)).fetchone()
            if existing_user_email:
                flash('Este email já está cadastrado.')
                return redirect(url_for('registro'))
            existing_user_cpf = conn.execute('SELECT id FROM usuarios WHERE cpf = ?', (cleaned_cpf,)).fetchone()
            if existing_user_cpf:
                flash('Este CPF já está cadastrado.')
                return redirect(url_for('registro'))

            senha_hash = hashlib.sha256(senha.encode()).hexdigest()
            conn.execute('INSERT INTO usuarios (nome, email, senha_hash, cpf, telefone) VALUES (?, ?, ?, ?, ?)',
                         (nome, email, senha_hash, cleaned_cpf, cleaned_telefone))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login.')
            sync_usuarios_to_sheets()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError as e:
            logging.error(f'Erro de integridade ao registrar usuário: {e}')
            flash('Erro ao registrar. Email ou CPF já podem estar em uso.')
        except Exception as e:
            logging.error(f'Erro inesperado ao registrar usuário: {e}')
            flash('Ocorreu um erro inesperado. Tente novamente.')
        finally:
            conn.close()
    return render_template('registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM usuarios WHERE email = ?', (email,)).fetchone()
        conn.close()

        if user and hashlib.sha256(senha.encode()).hexdigest() == user['senha_hash']:
            session['user_id'] = user['id']
            session['user_name'] = user['nome']
            session['is_admin'] = user['is_admin']
            flash('Login realizado com sucesso!')
            return redirect(url_for('home'))
        else:
            flash('Email ou senha incorretos.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.')
    return redirect(url_for('index'))

@app.route('/home')
def home():
    if 'user_id' not in session:
        flash('Por favor, faça login para acessar esta página.')
        return redirect(url_for('login'))

    try:
        carros = get_all_cars()
        logging.info(f'Home: Carregados {len(carros)} carros.')
        return render_template('home.html', carros=carros)
    except Exception as e:
        logging.error(f'Erro ao carregar carros na home: {e}')
        flash('Não foi possível carregar os carros no momento.')
        return redirect(url_for('index'))

@app.route('/reservas')
def reservas():
    if 'user_id' not in session:
        flash('Por favor, faça login para acessar suas reservas.')
        return redirect(url_for('login'))

    try:
        conn = get_db_connection()
        user_reservas = conn.execute('''SELECT r.*, c.modelo, c.placa, c.preco_diaria
                                        FROM reservas r
                                        JOIN usuarios u ON r.usuario_id = u.id
                                        JOIN carros c ON r.carro_id = c.id
                                        WHERE r.usuario_id = ?
                                        ORDER BY r.data_reserva DESC''', (session['user_id'],)).fetchall()
        conn.close()
        logging.info(f'Reservas do usuário {session["user_id"]}: Encontradas {len(user_reservas)}.')
        return render_template('reservas.html', reservas=user_reservas)
    except Exception as e:
        logging.error(f'Erro ao carregar reservas do usuário {session["user_id"]}: {e}')
        flash('Não foi possível carregar suas reservas no momento.')
        return redirect(url_for('home'))

@app.route('/reservar/<int:car_id>', methods=['GET', 'POST'])
def reservar(car_id):
    if 'user_id' not in session:
        flash('Por favor, faça login para reservar um carro.')
        return redirect(url_for('login'))

    carro = get_car_by_id(car_id)
    if not carro:
        flash('Carro não encontrado.')
        return redirect(url_for('home'))
    if not carro['disponivel']:
        flash('Este carro não está disponível para reserva.')
        return redirect(url_for('home'))

    if request.method == 'POST':
        data_reserva_str = request.form['data_reserva']
        hora_inicio_str = request.form['hora_inicio']
        hora_fim_str = request.form['hora_fim']
        observacoes = request.form.get('observacoes')

        try:
            data_reserva = datetime.strptime(data_reserva_str, '%Y-%m-%d').date()
            hora_inicio = datetime.strptime(hora_inicio_str, '%H:%M').time()
            hora_fim = datetime.strptime(hora_fim_str, '%H:%M').time()

            if data_reserva < datetime.now().date():
                flash('Não é possível reservar para uma data passada.')
                return render_template('reservar.html', carro=carro)
            if data_reserva == datetime.now().date() and hora_inicio < datetime.now().time():
                flash('Não é possível reservar para um horário passado no dia de hoje.')
                return render_template('reservar.html', carro=carro)
            if hora_inicio >= hora_fim:
                flash('A hora de início deve ser anterior à hora de fim.')
                return render_template('reservar.html', carro=carro)

            conn = get_db_connection()
            conflito = conn.execute('''SELECT id FROM reservas
                                       WHERE carro_id = ? AND data_reserva = ?
                                       AND (
                                           (hora_inicio < ? AND hora_fim > ?) OR
                                           (hora_inicio < ? AND hora_fim > ?) OR
                                           (hora_inicio >= ? AND hora_fim <= ?)
                                       ) AND status != 'cancelada' ''',
                                    (car_id, data_reserva, hora_fim, hora_inicio, hora_inicio, hora_fim, hora_inicio, hora_fim)).fetchone()

            if conflito:
                flash('Carro já reservado para este período.')
                conn.close()
                return render_template('reservar.html', carro=carro)

            conn.execute('INSERT INTO reservas (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, observacoes) VALUES (?, ?, ?, ?, ?, ?)',
                         (session['user_id'], car_id, data_reserva, hora_inicio, hora_fim, observacoes))
            conn.commit()
            conn.close()
            flash('Reserva realizada com sucesso!')
            sync_reservas_to_sheets()
            return redirect(url_for('reservas'))
        except ValueError:
            flash('Formato de data ou hora inválido.')
        except Exception as e:
            logging.error(f'Erro ao realizar reserva: {e}')
            flash('Ocorreu um erro ao processar sua reserva.')

    return render_template('reservar.html', carro=carro)

@app.route('/cancelar_reserva/<int:reserva_id>')
def cancelar_reserva(reserva_id):
    if 'user_id' not in session:
        flash('Acesso negado.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    reserva = conn.execute('SELECT * FROM reservas WHERE id = ? AND usuario_id = ?', (reserva_id, session['user_id'])).fetchone()

    if not reserva:
        flash('Reserva não encontrada ou você não tem permissão para cancelá-la.')
        conn.close()
        return redirect(url_for('reservas'))

    try:
        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', ('cancelada', reserva_id))
        conn.commit()
        flash('Reserva cancelada com sucesso.')
        sync_reservas_to_sheets()
    except Exception as e:
        logging.error(f'Erro ao cancelar reserva {reserva_id}: {e}')
        flash('Erro ao cancelar reserva.')
    finally:
        conn.close()
    return redirect(url_for('reservas'))

# --- 10. Admin Routes ---
@app.route('/admin')
def admin():
    if not is_admin():
        flash('Acesso negado. Apenas administradores.')
        return redirect(url_for('login'))

    try:
        usuarios = get_usuarios()
        carros = get_all_cars()
        reservas = get_reservas()
        logging.info(f'Admin: Carregados {len(usuarios)} usuários, {len(carros)} carros, {len(reservas)} reservas.')
        return render_template('admin.html', usuarios=usuarios, carros=carros, reservas=reservas)
    except Exception as e:
        logging.error(f'Erro ao carregar painel admin: {e}')
        flash('Erro ao carregar dados administrativos.')
        return redirect(url_for('home'))

@app.route('/admin/add_carro', methods=['GET', 'POST'])
def add_carro():
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('login'))

    if request.method == 'POST':
        modelo = request.form['modelo']
        ano = request.form['ano']
        cor = request.form['cor']
        placa = request.form['placa']
        preco_diaria = request.form['preco_diaria']

        if not modelo or not ano or not cor or not placa or not preco_diaria:
            flash('Todos os campos são obrigatórios.')
            return redirect(url_for('add_carro'))

        try:
            conn = get_db_connection()
            conn.execute('INSERT INTO carros (modelo, ano, cor, placa, preco_diaria) VALUES (?, ?, ?, ?, ?)',
                         (modelo, int(ano), cor, placa, float(preco_diaria)))
            conn.commit()
            flash('Carro adicionado com sucesso!')
            sync_carros_to_sheets()
            return redirect(url_for('admin'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada.')
        except ValueError:
            flash('Ano ou preço diária inválidos.')
        except Exception as e:
            logging.error(f'Erro ao adicionar carro: {e}')
            flash('Erro ao adicionar carro.')
        finally:
            conn.close()
    return render_template('add_carro.html')

@app.route('/admin/edit_carro/<int:car_id>', methods=['GET', 'POST'])
def edit_carro(car_id):
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('login'))

    carro = get_car_by_id(car_id)
    if not carro:
        flash('Carro não encontrado.')
        return redirect(url_for('admin'))

    if request.method == 'POST':
        modelo = request.form['modelo']
        ano = request.form['ano']
        cor = request.form['cor']
        placa = request.form['placa']
        preco_diaria = request.form['preco_diaria']
        disponivel = 'disponivel' in request.form

        if not modelo or not ano or not cor or not placa or not preco_diaria:
            flash('Todos os campos são obrigatórios.')
            return redirect(url_for('edit_carro', car_id=car_id))

        try:
            conn = get_db_connection()
            conn.execute('UPDATE carros SET modelo = ?, ano = ?, cor = ?, placa = ?, preco_diaria = ?, disponivel = ? WHERE id = ?',
                         (modelo, int(ano), cor, placa, float(preco_diaria), disponivel, car_id))
            conn.commit()
            flash('Carro atualizado com sucesso!')
            sync_carros_to_sheets()
            return redirect(url_for('admin'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada para outro carro.')
        except ValueError:
            flash('Ano ou preço diária inválidos.')
        except Exception as e:
            logging.error(f'Erro ao editar carro {car_id}: {e}')
            flash('Erro ao atualizar carro.')
        finally:
            conn.close()

    return render_template('edit_carro.html', carro=carro)

@app.route('/admin/delete_carro/<int:car_id>')
def delete_carro(car_id):
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    try:
        active_reservas = conn.execute("SELECT COUNT(*) FROM reservas WHERE carro_id = ? AND status != 'cancelada'", (car_id,)).fetchone()[0]
        if active_reservas > 0:
            flash('Não é possível deletar carro com reservas ativas.')
            return redirect(url_for('admin'))

        conn.execute('DELETE FROM carros WHERE id = ?', (car_id,))
        conn.commit()
        flash('Carro deletado com sucesso!')
        sync_carros_to_sheets()
    except Exception as e:
        logging.error(f'Erro ao deletar carro {car_id}: {e}')
        flash('Erro ao deletar carro.')
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/promote_admin/<int:user_id>')
def promote_admin(user_id):
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('login'))

    conn = get_db_connection()
    try:
        conn.execute('UPDATE usuarios SET is_admin = TRUE WHERE id = ?', (user_id,))
        conn.commit()
        flash('Usuário promovido a administrador com sucesso!')
        sync_usuarios_to_sheets()
    except Exception as e:
        logging.error(f'Erro ao promover usuário {user_id} a admin: {e}')
        flash('Erro ao promover usuário.')
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/update_reserva_status/<int:reserva_id>', methods=['POST'])
def update_reserva_status(reserva_id):
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('login'))

    new_status = request.form['status']
    conn = get_db_connection()
    try:
        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', (new_status, reserva_id))
        conn.commit()
        flash(f'Status da reserva {reserva_id} atualizado para {new_status}.')
        sync_reservas_to_sheets()
    except Exception as e:
        logging.error(f'Erro ao atualizar status da reserva {reserva_id}: {e}')
        flash('Erro ao atualizar status da reserva.')
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/sync_sheets')
def trigger_sync_sheets():
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('login'))

    try:
        sync_reservas_to_sheets()
        sync_usuarios_to_sheets()
        sync_carros_to_sheets()
        flash('Sincronização com Google Sheets concluída!')
    except Exception as e:
        logging.error(f'Erro geral ao acionar sincronização: {e}')
        flash('Erro ao acionar sincronização com Google Sheets.')
    return redirect(url_for('admin'))

@app.route('/admin/backup_db')
def backup_db():
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('admin'))

    try:
        conn = get_db_connection()

        reservas = conn.execute('SELECT * FROM reservas').fetchall()
        usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
        carros = conn.execute('SELECT * FROM carros').fetchall()

        conn.close()

        reservas_list = [dict(row) for row in reservas]
        usuarios_list = [dict(row) for row in usuarios]
        carros_list = [dict(row) for row in carros]

        data = {
            'reservas': reservas_list,
            'usuarios': usuarios_list,
            'carros': carros_list,
            'timestamp': datetime.now().isoformat(),
            'hash': hashlib.md5(json.dumps(reservas_list + usuarios_list + carros_list, default=str).encode()).hexdigest()
        }

        json_data = json.dumps(data, default=str, indent=4)
        logging.info(f'Backup gerado: {len(reservas_list)} reservas, {len(usuarios_list)} cadastros, {len(carros_list)} carros exportados.')

        output = BytesIO()
        output.write(json_data.encode('utf-8'))
        output.seek(0)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return send_file(output, as_attachment=True, download_name=f'backup_jgminis_{timestamp}.json', mimetype='application/json')
    except Exception as e:
        logging.error(f'Erro no backup: {e}')
        flash('Erro ao gerar backup.')
        return redirect(url_for('admin'))

@app.route('/admin/restore_backup', methods=['GET', 'POST'])
def restore_backup():
    if not is_admin():
        flash('Acesso negado.')
        return redirect(url_for('admin'))

    if request.method == 'POST':
        if 'backup_file' not in request.files:
            flash('Nenhum arquivo selecionado.')
            return redirect(url_for('restore_backup'))

        file = request.files['backup_file']
        if file.filename == '':
            flash('Nenhum arquivo selecionado.')
            return redirect(url_for('restore_backup'))

        if file and file.filename.endswith('.json'):
            try:
                backup_data = json.loads(file.read().decode('utf-8'))

                if not all(k in backup_data for k in ['reservas', 'usuarios', 'carros', 'timestamp', 'hash']):
                    flash('Arquivo de backup inválido: estrutura incompleta.')
                    return redirect(url_for('restore_backup'))

                expected_hash = backup_data.get('hash')
                calculated_hash = hashlib.md5(json.dumps(backup_data['reservas'] + backup_data['usuarios'] + backup_data['carros'], default=str).encode()).hexdigest()
                if expected_hash and expected_hash != calculated_hash:
                    flash('Aviso: Hash do backup não corresponde. O arquivo pode estar corrompido ou modificado.')

                conn = get_db_connection()
                c = conn.cursor()

                c.execute('DELETE FROM reservas')
                c.execute('DELETE FROM usuarios')
                c.execute('DELETE FROM carros')

                for user_data in backup_data['usuarios']:
                    c.execute('''INSERT INTO usuarios (id, nome, email, senha_hash, cpf, telefone, data_cadastro, is_admin)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                                 (user_data['id'], user_data['nome'], user_data['email'], user_data['senha_hash'],
                                  user_data['cpf'], user_data['telefone'], user_data['data_cadastro'], user_data['is_admin']))

                for car_data in backup_data['carros']:
                    c.execute('''INSERT INTO carros (id, modelo, ano, cor, placa, disponivel, preco_diaria)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                 (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'],
                                  car_data['placa'], car_data['disponivel'], car_data['preco_diaria']))

                for reserva_data in backup_data['reservas']:
                    c.execute('''INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status, observacoes)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                                 (reserva_data['id'], reserva_data['usuario_id'], reserva_data['carro_id'],
                                  reserva_data['data_reserva'], reserva_data['hora_inicio'], reserva_data['hora_fim'],
                                  reserva_data['status'], reserva_data['observacoes']))

                conn.commit()
                conn.close()
                flash('Backup restaurado com sucesso! Sincronizando com Sheets...')

                sync_usuarios_to_sheets()
                sync_carros_to_sheets()
                sync_reservas_to_sheets()

                return redirect(url_for('admin'))
            except json.JSONDecodeError:
                flash('Arquivo de backup inválido: não é um JSON válido.')
            except sqlite3.IntegrityError as e:
                flash(f'Erro de integridade ao restaurar: {e}. Verifique se IDs são únicos.')
            except Exception as e:
                logging.error(f'Erro ao restaurar backup: {e}')
                flash(f'Erro inesperado ao restaurar backup: {e}')
        else:
            flash('Por favor, selecione um arquivo JSON.')

    return render_template('restore_backup.html')

# --- 11. API Endpoints (Exemplo) ---
@app.route('/api/carros')
def api_carros():
    carros = get_all_cars()
    carros_dict = [dict(carro) for carro in carros]
    return jsonify(carros_dict)

# --- 12. Global Error Handlers ---
@app.errorhandler(404)
def not_found_error(error):
    logging.warning(f'Página não encontrada (404): {request.url}')
    return render_template('error.html', error_message='Página não encontrada.'), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error(f'Erro interno do servidor (500): {error}', exc_info=True)
    flash('Ocorreu um erro interno no servidor. Por favor, tente novamente mais tarde.')
    return render_template('error.html', error_message='Erro interno do servidor.'), 500

# --- 13. Application Execution ---
if __name__ == '__main__':
    app.run(debug=True)
