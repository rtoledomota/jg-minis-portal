from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import os
import json
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials # Importação atualizada para google-auth

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'sua_chave_secreta_aqui') # Use environment variable for secret key

DATABASE = 'jgminis.db'

# Google Sheets API configuration
# Path to your service account key file
# Ensure GOOGLE_CREDS_PATH is set in Railway or 'service_account.json' is in root
creds_path = os.getenv('GOOGLE_CREDS_PATH', 'service_account.json')
gc = None # Inicializa gc como None, será definido após autenticação bem-sucedida

# Função para inicializar o cliente gspread
def init_gspread_client():
    global gc
    try:
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        
        creds = None
        if os.path.exists(creds_path):
            creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
            app.logger.info(f"gspread: Client criado com google-auth a partir de arquivo: {creds_path}")
        elif os.getenv('GOOGLE_CREDENTIALS_JSON'): # Verifica por string JSON em variável de ambiente
            creds_info = json.loads(os.getenv('GOOGLE_CREDENTIALS_JSON'))
            creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
            app.logger.info("gspread: Client criado com google-auth a partir de variável de ambiente.")
        else:
            app.logger.error("gspread: Nenhuma credencial encontrada (arquivo ou variável de ambiente).")
            return None

        if creds:
            gc = gspread.authorize(creds)
            app.logger.info("gspread: Autenticação Sheets bem-sucedida com google-auth.")
        return gc
    except Exception as e:
        app.logger.error(f"gspread: Erro ao inicializar cliente gspread: {e}")
        return None

# Database initialization
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha TEXT NOT NULL,
            telefone TEXT,
            cpf TEXT UNIQUE,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS carros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            modelo TEXT NOT NULL,
            ano INTEGER NOT NULL,
            cor TEXT NOT NULL,
            placa TEXT UNIQUE NOT NULL,
            valor_diaria REAL NOT NULL,
            imagem TEXT,
            disponivel INTEGER DEFAULT 1
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            carro_id INTEGER NOT NULL,
            data_reserva DATE NOT NULL,
            hora_inicio TIME NOT NULL,
            hora_fim TIME NOT NULL,
            status TEXT DEFAULT 'Pendente', -- Pendente, Confirmada, Cancelada, Concluida
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id),
            FOREIGN KEY (carro_id) REFERENCES carros (id)
        )
    ''')
    conn.commit()
    conn.close()
    app.logger.info("DB inicializado sem perda de dados (CREATE TABLE IF NOT EXISTS).")

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# Chamadas de inicialização para Gunicorn (executadas quando o módulo é carregado)
init_db()
init_gspread_client()

# Helper functions
def get_user_by_id(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return user

def get_car_by_id(car_id):
    conn = get_db_connection()
    car = conn.execute('SELECT * FROM carros WHERE id = ?', (car_id,)).fetchone()
    conn.close()
    return car

def get_reservas(user_id=None, status=None):
    conn = get_db_connection()
    query = '''
        SELECT
            r.id AS reserva_id,
            r.data_reserva,
            r.hora_inicio,
            r.hora_fim,
            r.status,
            u.id AS usuario_id,
            u.nome AS usuario_nome,
            u.email AS usuario_email,
            u.telefone AS usuario_telefone,
            u.cpf AS usuario_cpf,
            c.id AS carro_id,
            c.modelo AS carro_modelo,
            c.ano AS carro_ano,
            c.cor AS carro_cor,
            c.placa AS carro_placa,
            c.valor_diaria AS carro_valor_diaria,
            c.imagem AS carro_imagem
        FROM reservas r
        JOIN usuarios u ON r.usuario_id = u.id
        JOIN carros c ON r.carro_id = c.id
    '''
    params = []
    conditions = []

    if user_id:
        conditions.append('u.id = ?')
        params.append(user_id)
    if status:
        conditions.append('r.status = ?')
        params.append(status)

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    
    query += ' ORDER BY r.data_reserva DESC, r.hora_inicio DESC'

    reservas = conn.execute(query, params).fetchall()
    conn.close()
    app.logger.info(f"DB: Encontradas {len(reservas)} reservas.")
    return reservas

def is_admin():
    return session.get('is_admin', False)

# Decorator para verificar login
def login_required(f):
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa estar logado para acessar esta página.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__ # Preserva o nome da função original
    return wrapper

# Decorator para verificar admin
def admin_required(f):
    def wrapper(*args, **kwargs):
        if not is_admin():
            flash('Acesso negado. Apenas administradores podem acessar esta página.', 'danger')
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__ # Preserva o nome da função original
    return wrapper

# Validation functions
def validate_cpf(cpf):
    cleaned = ''.join(filter(str.isdigit, cpf))
    return len(cleaned) == 11

def validate_telefone(telefone):
    cleaned = ''.join(filter(str.isdigit, telefone))
    # Correção: <= em texto plano para evitar SyntaxError
    return cleaned.isdigit() and 10 <= len(cleaned) <= 11

# Google Sheets Sync Functions
def sync_data_to_sheets(sheet_name, data_rows, header):
    if not gc:
        app.logger.error(f"gspread: Cliente não inicializado para sincronizar {sheet_name}. Tentando inicializar novamente.")
        init_gspread_client() # Tenta inicializar novamente
        if not gc:
            app.logger.error(f"gspread: Falha ao inicializar cliente para sincronizar {sheet_name}.")
            return False
    try:
        worksheet = gc.open("JG Minis - Banco de Dados").worksheet(sheet_name)
        worksheet.clear()
        worksheet.append_row(header)
        if data_rows:
            worksheet.append_rows(data_rows)
        app.logger.info(f"gspread: Sincronização da aba '{sheet_name}' concluída. {len(data_rows)} registros atualizados.")
        return True
    except Exception as e:
        app.logger.error(f"gspread: Erro ao sincronizar aba '{sheet_name}': {e}")
        return False

def sync_reservas_to_sheets():
    conn = get_db_connection()
    reservas_db = conn.execute('''
        SELECT
            r.id, u.nome, u.email, u.telefone, u.cpf,
            c.modelo, c.placa, c.valor_diaria,
            r.data_reserva, r.hora_inicio, r.hora_fim, r.status
        FROM reservas r
        JOIN usuarios u ON r.usuario_id = u.id
        JOIN carros c ON r.carro_id = c.id
        ORDER BY r.data_reserva DESC, r.hora_inicio DESC
    ''').fetchall()
    conn.close()

    header = [
        "ID Reserva", "Nome Cliente", "Email Cliente", "Telefone Cliente", "CPF Cliente",
        "Modelo Carro", "Placa Carro", "Valor Diária",
        "Data Reserva", "Hora Início", "Hora Fim", "Status"
    ]
    data_rows = [list(reserva) for reserva in reservas_db]
    return sync_data_to_sheets("Reservas", data_rows, header)

def sync_usuarios_to_sheets():
    conn = get_db_connection()
    usuarios_db = conn.execute('SELECT id, nome, email, telefone, cpf, is_admin FROM usuarios ORDER BY nome').fetchall()
    conn.close()

    header = ["ID", "Nome", "Email", "Telefone", "CPF", "Admin"]
    data_rows = [list(usuario) for usuario in usuarios_db]
    return sync_data_to_sheets("Usuarios", data_rows, header)

def sync_carros_to_sheets():
    conn = get_db_connection()
    carros_db = conn.execute('SELECT id, modelo, ano, cor, placa, valor_diaria, disponivel FROM carros ORDER BY modelo').fetchall()
    conn.close()

    header = ["ID", "Modelo", "Ano", "Cor", "Placa", "Valor Diária", "Disponível"]
    data_rows = [list(carro) for carro in carros_db]
    return sync_data_to_sheets("Carros", data_rows, header)

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return render_template('index.html')

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        senha = request.form['senha']
        telefone = request.form['telefone']
        cpf = request.form['cpf']

        if not validate_cpf(cpf):
            flash('CPF inválido.', 'danger')
            return render_template('registro.html')
        if not validate_telefone(telefone):
            flash('Telefone inválido. Deve ter 10 ou 11 dígitos.', 'danger')
            return render_template('registro.html')

        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO usuarios (nome, email, senha, telefone, cpf) VALUES (?, ?, ?, ?, ?)',
                         (nome, email, senha, telefone, cpf))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login.', 'success')
            sync_usuarios_to_sheets() # Sync after user registration
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email ou CPF já cadastrados.', 'danger')
        finally:
            conn.close()
    return render_template('registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM usuarios WHERE email = ? AND senha = ?', (email, senha)).fetchone()
        conn.close()
        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['nome']
            session['is_admin'] = bool(user['is_admin'])
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('home'))
        else:
            flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('index'))

@app.route('/home')
@login_required
def home():
    conn = get_db_connection()
    carros = conn.execute('SELECT * FROM carros WHERE disponivel = 1').fetchall()
    conn.close()
    return render_template('home.html', carros=carros)

@app.route('/reservas')
@login_required
def minhas_reservas():
    user_id = session['user_id']
    reservas = get_reservas(user_id=user_id)
    return render_template('minhas_reservas.html', reservas=reservas)

@app.route('/reservar/<int:car_id>', methods=['GET', 'POST'])
@login_required
def reservar_carro(car_id):
    carro = get_car_by_id(car_id)
    if not carro or not carro['disponivel']:
        flash('Carro não encontrado ou não disponível.', 'danger')
        return redirect(url_for('home'))

    if request.method == 'POST':
        data_reserva_str = request.form['data_reserva']
        hora_inicio_str = request.form['hora_inicio']
        hora_fim_str = request.form['hora_fim']

        try:
            data_reserva = datetime.strptime(data_reserva_str, '%Y-%m-%d').date()
            hora_inicio = datetime.strptime(hora_inicio_str, '%H:%M').time()
            hora_fim = datetime.strptime(hora_fim_str, '%H:%M').time()
        except ValueError:
            flash('Formato de data ou hora inválido.', 'danger')
            return render_template('reservar.html', carro=carro)

        # Basic validation for reservation times
        if data_reserva < datetime.now().date():
            flash('Não é possível reservar para uma data passada.', 'danger')
            return render_template('reservar.html', carro=carro)
        if data_reserva == datetime.now().date() and hora_inicio < datetime.now().time():
            flash('Não é possível reservar para uma hora passada no dia de hoje.', 'danger')
            return render_template('reservar.html', carro=carro)
        if hora_inicio >= hora_fim:
            flash('A hora de início deve ser anterior à hora de fim.', 'danger')
            return render_template('reservar.html', carro=carro)

        # Check for overlapping reservations for the same car
        conn = get_db_connection()
        overlapping_reservations = conn.execute('''
            SELECT * FROM reservas
            WHERE carro_id = ?
            AND data_reserva = ?
            AND (
                (? < hora_fim AND ? > hora_inicio) OR
                (? = hora_inicio) OR
                (? = hora_fim)
            )
            AND status IN ('Pendente', 'Confirmada')
        ''', (car_id, data_reserva_str, hora_inicio_str, hora_fim_str, hora_inicio_str, hora_fim_str)).fetchone()

        if overlapping_reservations:
            flash('Já existe uma reserva para este carro neste período.', 'danger')
            conn.close()
            return render_template('reservar.html', carro=carro)

        try:
            conn.execute('INSERT INTO reservas (usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status) VALUES (?, ?, ?, ?, ?, ?)',
                         (session['user_id'], car_id, data_reserva_str, hora_inicio_str, hora_fim_str, 'Pendente'))
            conn.commit()
            flash('Reserva solicitada com sucesso! Aguardando confirmação.', 'success')
            sync_reservas_to_sheets() # Sync after reservation
            return redirect(url_for('minhas_reservas'))
        except Exception as e:
            flash(f'Erro ao solicitar reserva: {e}', 'danger')
        finally:
            conn.close()

    return render_template('reservar.html', carro=carro)

@app.route('/cancelar_reserva/<int:reserva_id>')
@login_required
def cancelar_reserva(reserva_id):
    conn = get_db_connection()
    reserva = conn.execute('SELECT * FROM reservas WHERE id = ? AND usuario_id = ?', (reserva_id, session['user_id'])).fetchone()

    if not reserva:
        flash('Reserva não encontrada ou você não tem permissão para cancelá-la.', 'danger')
        conn.close()
        return redirect(url_for('minhas_reservas'))

    if reserva['status'] == 'Confirmada':
        flash('Reservas confirmadas não podem ser canceladas diretamente. Entre em contato com o administrador.', 'warning')
        conn.close()
        return redirect(url_for('minhas_reservas'))

    try:
        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', ('Cancelada', reserva_id))
        conn.commit()
        flash('Reserva cancelada com sucesso.', 'success')
        sync_reservas_to_sheets() # Sync after cancellation
    except Exception as e:
        flash(f'Erro ao cancelar reserva: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('minhas_reservas'))

# Admin Routes
@app.route('/admin')
@admin_required
def admin_dashboard():
    conn = get_db_connection()
    usuarios = conn.execute('SELECT * FROM usuarios').fetchall()
    carros = conn.execute('SELECT * FROM carros').fetchall()
    reservas = get_reservas() # Get all reservations for admin
    conn.close()
    return render_template('admin.html', usuarios=usuarios, carros=carros, reservas=reservas)

@app.route('/admin/add_carro', methods=['GET', 'POST'])
@admin_required
def add_carro():
    if request.method == 'POST':
        modelo = request.form['modelo']
        ano = request.form['ano']
        cor = request.form['cor']
        placa = request.form['placa']
        valor_diaria = request.form['valor_diaria']
        imagem = request.form['imagem'] # URL da imagem

        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO carros (modelo, ano, cor, placa, valor_diaria, imagem) VALUES (?, ?, ?, ?, ?, ?)',
                         (modelo, ano, cor, placa, valor_diaria, imagem))
            conn.commit()
            flash('Carro adicionado com sucesso!', 'success')
            sync_carros_to_sheets() # Sync after adding car
            return redirect(url_for('admin_dashboard'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada.', 'danger')
        except Exception as e:
            flash(f'Erro ao adicionar carro: {e}', 'danger')
        finally:
            conn.close()
    return render_template('add_carro.html')

@app.route('/admin/edit_carro/<int:car_id>', methods=['GET', 'POST'])
@admin_required
def edit_carro(car_id):
    conn = get_db_connection()
    carro = conn.execute('SELECT * FROM carros WHERE id = ?', (car_id,)).fetchone()

    if not carro:
        flash('Carro não encontrado.', 'danger')
        conn.close()
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        modelo = request.form['modelo']
        ano = request.form['ano']
        cor = request.form['cor']
        placa = request.form['placa']
        valor_diaria = request.form['valor_diaria']
        imagem = request.form['imagem']
        disponivel = 1 if 'disponivel' in request.form else 0

        try:
            conn.execute('UPDATE carros SET modelo = ?, ano = ?, cor = ?, placa = ?, valor_diaria = ?, imagem = ?, disponivel = ? WHERE id = ?',
                         (modelo, ano, cor, placa, valor_diaria, imagem, disponivel, car_id))
            conn.commit()
            flash('Carro atualizado com sucesso!', 'success')
            sync_carros_to_sheets() # Sync after editing car
            return redirect(url_for('admin_dashboard'))
        except sqlite3.IntegrityError:
            flash('Placa já cadastrada para outro carro.', 'danger')
        except Exception as e:
            flash(f'Erro ao atualizar carro: {e}', 'danger')
        finally:
            conn.close()
    
    conn.close()
    return render_template('edit_carro.html', carro=carro)

@app.route('/admin/delete_carro/<int:car_id>')
@admin_required
def delete_carro(car_id):
    conn = get_db_connection()
    try:
        # Check for existing reservations for this car
        existing_reservations = conn.execute('SELECT COUNT(*) FROM reservas WHERE carro_id = ? AND status IN (?, ?)', (car_id, 'Pendente', 'Confirmada')).fetchone()[0]
        if existing_reservations > 0:
            flash('Não é possível deletar carro com reservas pendentes ou confirmadas.', 'danger')
            return redirect(url_for('admin_dashboard'))

        conn.execute('DELETE FROM carros WHERE id = ?', (car_id,))
        conn.commit()
        flash('Carro deletado com sucesso!', 'success')
        sync_carros_to_sheets() # Sync after deleting car
    except Exception as e:
        flash(f'Erro ao deletar carro: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_reserva_status/<int:reserva_id>', methods=['POST'])
@admin_required
def update_reserva_status(reserva_id):
    new_status = request.form['status']
    conn = get_db_connection()
    try:
        conn.execute('UPDATE reservas SET status = ? WHERE id = ?', (new_status, reserva_id))
        conn.commit()
        flash(f'Status da reserva {reserva_id} atualizado para {new_status}.', 'success')
        sync_reservas_to_sheets() # Sync after status update
    except Exception as e:
        flash(f'Erro ao atualizar status da reserva: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/promote_admin/<int:user_id>')
@admin_required
def promote_admin(user_id):
    conn = get_db_connection()
    try:
        conn.execute('UPDATE usuarios SET is_admin = 1 WHERE id = ?', (user_id,))
        conn.commit()
        flash('Usuário promovido a administrador com sucesso!', 'success')
        sync_usuarios_to_sheets() # Sync after promoting admin
    except Exception as e:
        flash(f'Erro ao promover usuário: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/demote_admin/<int:user_id>')
@admin_required
def demote_admin(user_id):
    conn = get_db_connection()
    try:
        conn.execute('UPDATE usuarios SET is_admin = 0 WHERE id = ?', (user_id,))
        conn.commit()
        flash('Usuário rebaixado de administrador com sucesso!', 'success')
        sync_usuarios_to_sheets() # Sync after demoting admin
    except Exception as e:
        flash(f'Erro ao rebaixar usuário: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/sync_sheets')
@admin_required
def admin_sync_sheets():
    success_reservas = sync_reservas_to_sheets()
    success_usuarios = sync_usuarios_to_sheets()
    success_carros = sync_carros_to_sheets()

    if success_reservas and success_usuarios and success_carros:
        flash('Todas as planilhas sincronizadas com sucesso!', 'success')
    else:
        flash('Ocorreu um erro em uma ou mais sincronizações. Verifique os logs.', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/backup_db')
@admin_required
def backup_db():
    conn = get_db_connection()
    try:
        tables = ['usuarios', 'carros', 'reservas']
        backup_data = {}
        for table_name in tables:
            cursor = conn.execute(f"SELECT * FROM {table_name}")
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
            backup_data[table_name] = [dict(zip(columns, row)) for row in rows]
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_jgminis_{timestamp}.json"
        backup_path = os.path.join('backups', backup_filename) # Store backups in a 'backups' folder

        if not os.path.exists('backups'):
            os.makedirs('backups')

        with open(backup_path, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=4)
        
        flash(f'Backup do banco de dados criado com sucesso: {backup_filename}', 'success')
        return jsonify(backup_data), 200 # Return JSON directly for download/view
    except Exception as e:
        flash(f'Erro ao criar backup: {e}', 'danger')
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/admin/restore_backup', methods=['GET', 'POST'])
@admin_required
def restore_backup():
    if request.method == 'POST':
        if 'backup_file' not in request.files:
            flash('Nenhum arquivo de backup selecionado.', 'danger')
            return redirect(url_for('admin_dashboard'))
        
        backup_file = request.files['backup_file']
        if backup_file.filename == '':
            flash('Nenhum arquivo de backup selecionado.', 'danger')
            return redirect(url_for('admin_dashboard'))
        
        if backup_file:
            try:
                backup_data = json.load(backup_file)
                conn = get_db_connection()
                cursor = conn.cursor()

                # Clear existing data (careful with this in production!)
                # For this app, we'll just re-insert, assuming IDs might change or be managed.
                # A more robust restore would handle ID conflicts or truncate.
                # For simplicity, we'll clear and re-insert.
                cursor.execute("DELETE FROM reservas")
                cursor.execute("DELETE FROM carros")
                cursor.execute("DELETE FROM usuarios")

                # Restore users
                for user_data in backup_data.get('usuarios', []):
                    cursor.execute('''
                        INSERT INTO usuarios (id, nome, email, senha, telefone, cpf, is_admin)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (user_data['id'], user_data['nome'], user_data['email'], user_data['senha'],
                          user_data['telefone'], user_data['cpf'], user_data['is_admin']))
                
                # Restore cars
                for car_data in backup_data.get('carros', []):
                    cursor.execute('''
                        INSERT INTO carros (id, modelo, ano, cor, placa, valor_diaria, imagem, disponivel)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (car_data['id'], car_data['modelo'], car_data['ano'], car_data['cor'],
                          car_data['placa'], car_data['valor_diaria'], car_data['imagem'], car_data['disponivel']))
                
                # Restore reservations
                for reserva_data in backup_data.get('reservas', []):
                    cursor.execute('''
                        INSERT INTO reservas (id, usuario_id, carro_id, data_reserva, hora_inicio, hora_fim, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (reserva_data['id'], reserva_data['usuario_id'], reserva_data['carro_id'],
                          reserva_data['data_reserva'], reserva_data['hora_inicio'], reserva_data['hora_fim'],
                          reserva_data['status']))
                
                conn.commit()
                conn.close()
                flash('Banco de dados restaurado com sucesso!', 'success')
                
                # Sync sheets after restore to reflect new data
                sync_reservas_to_sheets()
                sync_usuarios_to_sheets()
                sync_carros_to_sheets()

                return redirect(url_for('admin_dashboard'))
            except Exception as e:
                flash(f'Erro ao restaurar backup: {e}', 'danger')
                return redirect(url_for('admin_dashboard'))
    
    # For GET request, render a simple form to upload backup file
    return render_template('restore_backup.html')


# Run the app (apenas quando executado diretamente, não pelo Gunicorn)
if __name__ == '__main__':
    app.run(debug=True)
