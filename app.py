try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

import json
from flask import Flask, request, redirect, url_for, session, render_template_string, flash, send_file
from functools import wraps
import os
import bcrypt
import re
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from io import BytesIO
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
import logging

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sua_chave_secreta_aqui_se_nao_definida')

# Configuração de diretórios de persistência
# Para Cloudflare Pages, /tmp é o único diretório gravável e persistente entre invocações da mesma instância
PERSIST_DIR = Path("/tmp") / "JG_MINIS_PERSIST_v4"
PERSIST_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = PERSIST_DIR / "database.db"
BACKUP_FILE = PERSIST_DIR / "backup_v4.json"
EXCEL_BACKUP_FILE = PERSIST_DIR / "reservas_backup.xlsx"

# Variáveis de ambiente
WHATSAPP_NUMERO = os.environ.get('WHATSAPP_NUMERO', '5511999999999') # Número de WhatsApp para contato
GOOGLE_SHEETS_ID = os.environ.get('GOOGLE_SHEETS_ID', '1sxlvo6j-UTB0xXuyivzWnhRuYvpJFcH2smL4ZzHTUps') # ID da planilha principal de miniaturas
BACKUP_SHEETS_ID = os.environ.get('BACKUP_SHEETS_ID', '1avMoEA0WddQ7dW92X2NORo-cJSwJb7cpinjsuZMMZqI') # ID da planilha de backup

# Configurações de e-mail
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', 'seu_email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', 'sua_senha_app') # Para Gmail, use App Password
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587')) # Porta para TLS

logging.info(f"🚀 JG MINIS v4.2 - Inicializando aplicação...")
logging.info(f"Diretório de persistência: {PERSIST_DIR}")
logging.info(f"Arquivo DB: {DB_FILE}")

# Funções de Banco de Dados
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Tabela de Usuários
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    ''')

    # Tabela de Miniaturas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS miniaturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            image_url TEXT
        )
    ''')

    # Tabela de Reservas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            miniatura_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            reservation_date TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (miniatura_id) REFERENCES miniaturas (id)
        )
    ''')

    # Adiciona usuário admin padrão se não existir
    cursor.execute("SELECT * FROM users WHERE email = ?", ('admin@jgminis.com.br',))
    admin_user = cursor.fetchone()
    if not admin_user:
        hashed_password = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cursor.execute("INSERT INTO users (name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?)",
                       ('Admin', 'admin@jgminis.com.br', '5511999999999', hashed_password, 1))
        logging.info("Usuário admin padrão criado.")
    
    conn.commit()
    conn.close()
    logging.info("Banco de dados inicializado/verificado.")

# Funções de Autenticação e Autorização
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa estar logado para acessar esta página.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa estar logado para acessar esta página.', 'warning')
            return redirect(url_for('login'))
        
        conn = get_db_connection()
        user = conn.execute("SELECT is_admin FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        
        if not user or user['is_admin'] != 1:
            flash('Acesso negado: Você não tem permissões de administrador.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# Funções de Integração Google Sheets
def get_google_sheets():
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json:
            logging.error("Variável de ambiente GOOGLE_SHEETS_CREDENTIALS não definida.")
            return None
        
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        logging.info("Conexão com Google Sheets estabelecida.")
        return client
    except Exception as e:
        logging.error(f"Erro ao conectar ao Google Sheets: {e}")
        return None

def load_miniaturas_from_sheets():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verifica se já existem miniaturas no BD para evitar duplicação em cada boot
    cursor.execute("SELECT COUNT(*) FROM miniaturas")
    if cursor.fetchone()[0] > 0:
        logging.info("Miniaturas já existem no BD, pulando carga do Sheets.")
        conn.close()
        return

    client = get_google_sheets()
    if not client:
        logging.warning("Não foi possível carregar miniaturas do Google Sheets. Usando dados padrão ou vazios.")
        conn.close()
        return

    try:
        sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet('Miniaturas')
        data = sheet.get_all_records() # Assume a primeira linha como cabeçalho
        
        for row in data:
            name = row.get('Nome')
            description = row.get('Descrição', '')
            price = float(str(row.get('Preço', '0')).replace(',', '.'))
            stock = int(row.get('Estoque', 0))
            image_url = row.get('URL Imagem', '')

            if name and price is not None and stock is not None:
                cursor.execute("INSERT INTO miniaturas (name, description, price, stock, image_url) VALUES (?, ?, ?, ?, ?)",
                               (name, description, price, stock, image_url))
        conn.commit()
        logging.info(f"Miniaturas carregadas do Google Sheets: {len(data)} itens.")
    except Exception as e:
        logging.error(f"Erro ao carregar miniaturas do Google Sheets: {e}")
    finally:
        conn.close()

def update_sheets_backup(sheet_name, data_rows, header):
    client = get_google_sheets()
    if not client:
        logging.error(f"Não foi possível atualizar o backup do Google Sheets para '{sheet_name}'.")
        return

    try:
        sheet = client.open_by_key(BACKUP_SHEETS_ID).worksheet(sheet_name)
        sheet.clear()
        sheet.append_row(header)
        sheet.append_rows(data_rows)
        logging.info(f"Backup do Google Sheets para '{sheet_name}' atualizado com sucesso.")
    except gspread.exceptions.WorksheetNotFound:
        logging.warning(f"Planilha '{sheet_name}' não encontrada na planilha de backup. Criando nova.")
        try:
            sheet = client.open_by_key(BACKUP_SHEETS_ID).add_worksheet(title=sheet_name, rows="100", cols="20")
            sheet.append_row(header)
            sheet.append_rows(data_rows)
            logging.info(f"Planilha '{sheet_name}' criada e backup atualizado.")
        except Exception as e:
            logging.error(f"Erro ao criar e atualizar planilha '{sheet_name}' no Google Sheets: {e}")
    except Exception as e:
        logging.error(f"Erro ao atualizar o backup do Google Sheets para '{sheet_name}': {e}")

def create_json_backup():
    conn = get_db_connection()
    users = conn.execute("SELECT id, name, email, phone, is_admin FROM users").fetchall()
    miniaturas = conn.execute("SELECT * FROM miniaturas").fetchall()
    reservations = conn.execute("SELECT * FROM reservations").fetchall()
    conn.close()

    backup_data = {
        'users': [dict(row) for row in users],
        'miniaturas': [dict(row) for row in miniaturas],
        'reservations': [dict(row) for row in reservations],
        'timestamp': datetime.now().isoformat()
    }

    try:
        with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=4)
        logging.info(f"Backup JSON criado em {BACKUP_FILE}")

        # Atualiza backup no Google Sheets
        users_header = ['id', 'name', 'email', 'phone', 'is_admin']
        users_rows = [[u['id'], u['name'], u['email'], u['phone'], u['is_admin']] for u in backup_data['users']]
        update_sheets_backup('Usuarios_Backup', users_rows, users_header)

        reservations_header = ['id', 'user_id', 'miniatura_id', 'quantity', 'reservation_date', 'status']
        reservations_rows = [[r['id'], r['user_id'], r['miniatura_id'], r['quantity'], r['reservation_date'], r['status']] for r in backup_data['reservations']]
        update_sheets_backup('Backups_Reservas', reservations_rows, reservations_header)

    except Exception as e:
        logging.error(f"Erro ao criar backup JSON ou Google Sheets: {e}")

def restore_backup():
    if not BACKUP_FILE.exists():
        logging.warning(f"Arquivo de backup não encontrado em {BACKUP_FILE}. Não foi possível restaurar.")
        return

    try:
        with open(BACKUP_FILE, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)
        
        conn = get_db_connection()
        cursor = conn.cursor()

        # Limpa tabelas existentes (exceto admin)
        cursor.execute("DELETE FROM reservations")
        cursor.execute("DELETE FROM miniaturas")
        cursor.execute("DELETE FROM users WHERE is_admin = 0") # Mantém o admin padrão

        # Restaura usuários
        for user_data in backup_data.get('users', []):
            if user_data['email'] == 'admin@jgminis.com.br': # Não recria admin se já existe
                continue
            try:
                cursor.execute("INSERT INTO users (id, name, email, phone, password, is_admin) VALUES (?, ?, ?, ?, ?, ?)",
                               (user_data['id'], user_data['name'], user_data['email'], user_data['phone'], user_data['password'], user_data['is_admin']))
            except sqlite3.IntegrityError:
                logging.warning(f"Usuário com ID {user_data['id']} ou email {user_data['email']} já existe, pulando.")

        # Restaura miniaturas
        for miniatura_data in backup_data.get('miniaturas', []):
            try:
                cursor.execute("INSERT INTO miniaturas (id, name, description, price, stock, image_url) VALUES (?, ?, ?, ?, ?, ?)",
                               (miniatura_data['id'], miniatura_data['name'], miniatura_data['description'], miniatura_data['price'], miniatura_data['stock'], miniatura_data['image_url']))
            except sqlite3.IntegrityError:
                logging.warning(f"Miniatura com ID {miniatura_data['id']} já existe, pulando.")

        # Restaura reservas
        for reservation_data in backup_data.get('reservations', []):
            try:
                cursor.execute("INSERT INTO reservations (id, user_id, miniatura_id, quantity, reservation_date, status) VALUES (?, ?, ?, ?, ?, ?)",
                               (reservation_data['id'], reservation_data['user_id'], reservation_data['miniatura_id'], reservation_data['quantity'], reservation_data['reservation_date'], reservation_data['status']))
            except sqlite3.IntegrityError:
                logging.warning(f"Reserva com ID {reservation_data['id']} já existe, pulando.")

        conn.commit()
        logging.info("Dados restaurados do backup JSON.")
    except Exception as e:
        logging.error(f"Erro ao restaurar backup JSON: {e}")
    finally:
        conn.close()

def export_to_excel_reservas():
    conn = get_db_connection()
    reservations = conn.execute('''
        SELECT
            r.id AS reservation_id,
            u.name AS user_name,
            u.email AS user_email,
            u.phone AS user_phone,
            m.name AS miniatura_name,
            m.price AS miniatura_price,
            r.quantity,
            r.reservation_date,
            r.status
        FROM reservations r
        JOIN users u ON r.user_id = u.id
        JOIN miniaturas m ON r.miniatura_id = m.id
        ORDER BY r.reservation_date DESC
    ''').fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reservas JG MINIS"

    # Cabeçalho
    headers = ["ID Reserva", "Nome Usuário", "Email Usuário", "Telefone Usuário",
               "Nome Miniatura", "Preço Unitário", "Quantidade", "Data Reserva", "Status"]
    ws.append(headers)

    # Estilo para cabeçalho
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
    for col_idx, cell in enumerate(ws[1]):
        cell.font = header_font
        cell.fill = header_fill
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx + 1)].width = 15 # Largura padrão

    # Dados
    for row_data in reservations:
        ws.append([row_data[h] for h in headers])

    # Ajusta largura das colunas e alinhamento
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter # Get the column name
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2)
        ws.column_dimensions[column].width = adjusted_width
        for cell in col:
            cell.alignment = Alignment(horizontal='center', vertical='center')

    # Salva em BytesIO
    excel_buffer = BytesIO()
    wb.save(excel_buffer)
    excel_buffer.seek(0)
    
    # Salva também no disco para backup local
    try:
        with open(EXCEL_BACKUP_FILE, 'wb') as f:
            f.write(excel_buffer.getvalue())
        logging.info(f"Backup Excel salvo em {EXCEL_BACKUP_FILE}")
    except Exception as e:
        logging.error(f"Erro ao salvar backup Excel localmente: {e}")
        
    excel_buffer.seek(0) # Reset buffer position for sending
    return excel_buffer

def send_reservation_email(user_email, user_name, miniatura_name, quantity, total_price, reservation_date):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        logging.warning("Configurações de e-mail incompletas. E-mail de reserva não será enviado.")
        return False

    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = user_email
    msg['Subject'] = "Confirmação de Reserva JG MINIS"

    body = f"""
    Olá {user_name},

    Sua reserva na JG MINIS foi confirmada com sucesso!

    Detalhes da Reserva:
    Miniatura: {miniatura_name}
    Quantidade: {quantity}
    Valor Total: R$ {total_price:.2f}
    Data da Reserva: {reservation_date}

    Agradecemos a sua preferência!

    Atenciosamente,
    Equipe JG MINIS
    """
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        logging.info(f"E-mail de confirmação enviado para {user_email}.")
        return True
    except Exception as e:
        logging.error(f"Erro ao enviar e-mail de confirmação para {user_email}: {e}")
        return False

# Rotas da Aplicação

@app.route('/')
def index():
    conn = get_db_connection()
    
    # Carrega miniaturas do Sheets apenas se não houver nenhuma no BD
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM miniaturas")
    if cursor.fetchone()[0] == 0:
        conn.close() # Fecha a conexão antes de chamar load_miniaturas_from_sheets
        load_miniaturas_from_sheets()
        conn = get_db_connection() # Reabre a conexão após a carga

    sort_by = request.args.get('sort_by', 'name')
    order = request.args.get('order', 'asc')

    valid_sort_columns = ['name', 'price']
    if sort_by not in valid_sort_columns:
        sort_by = 'name'
    if order not in ['asc', 'desc']:
        order = 'asc'

    miniaturas = conn.execute(f"SELECT * FROM miniaturas ORDER BY {sort_by} {order}").fetchall()
    conn.close()
    
    return render_template_string(INDEX_HTML, miniaturas=miniaturas, sort_by=sort_by, order=order, logo_url=LOGO_URL)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        
        if not name or not email or not password:
            flash('Todos os campos obrigatórios devem ser preenchidos.', 'danger')
            return redirect(url_for('register'))

        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Formato de e-mail inválido.', 'danger')
            return redirect(url_for('register'))

        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)",
                           (name, email, phone, hashed_password))
            conn.commit()
            flash('Registro realizado com sucesso! Faça login para continuar.', 'success')
            logging.info(f"Novo usuário registrado: {name} ({email}, {phone})")
            create_json_backup() # Backup após novo registro
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Este e-mail já está registrado. Tente outro ou faça login.', 'danger')
        except Exception as e:
            flash(f'Ocorreu um erro ao registrar: {e}', 'danger')
            logging.error(f"Erro ao registrar usuário {email}: {e}")
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML, logo_url=LOGO_URL)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['is_admin'] = user['is_admin']
            flash(f'Bem-vindo, {user["name"]}!', 'success')
            logging.info(f"Login bem-sucedido: {email}")
            return redirect(url_for('index'))
        else:
            flash('E-mail ou senha incorretos.', 'danger')
            logging.warning(f"Tentativa de login falha para: {email}")
    return render_template_string(LOGIN_HTML, logo_url=LOGO_URL)

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('index'))

@app.route('/reservar/<int:miniatura_id>', methods=['POST'])
@login_required
def reservar(miniatura_id):
    quantity = int(request.form['quantity'])
    
    conn = get_db_connection()
    miniatura = conn.execute("SELECT * FROM miniaturas WHERE id = ?", (miniatura_id,)).fetchone()
    
    if not miniatura:
        flash('Miniatura não encontrada.', 'danger')
        conn.close()
        return redirect(url_for('index'))

    if quantity <= 0:
        flash('A quantidade deve ser maior que zero.', 'danger')
        conn.close()
        return redirect(url_for('index'))

    if miniatura['stock'] < quantity:
        flash(f'Estoque insuficiente para {miniatura["name"]}. Disponível: {miniatura["stock"]}.', 'danger')
        conn.close()
        return redirect(url_for('index'))

    try:
        # Atualiza estoque
        conn.execute("UPDATE miniaturas SET stock = stock - ? WHERE id = ?", (quantity, miniatura_id))
        
        # Cria reserva
        reservation_date = datetime.now().isoformat()
        conn.execute("INSERT INTO reservations (user_id, miniatura_id, quantity, reservation_date, status) VALUES (?, ?, ?, ?, ?)",
                       (session['user_id'], miniatura_id, quantity, reservation_date, 'Confirmada'))
        conn.commit()
        
        flash(f'Reserva de {quantity}x {miniatura["name"]} realizada com sucesso!', 'success')
        logging.info(f"Reserva realizada: User {session['user_id']}, Miniatura {miniatura_id}, Qtd {quantity}")
        
        # Envia e-mail de confirmação
        user_info = conn.execute("SELECT name, email FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        total_price = miniatura['price'] * quantity
        send_reservation_email(user_info['email'], user_info['name'], miniatura['name'], quantity, total_price, reservation_date)
        
        create_json_backup() # Backup após nova reserva
        
    except Exception as e:
        flash(f'Ocorreu um erro ao processar sua reserva: {e}', 'danger')
        logging.error(f"Erro ao reservar miniatura {miniatura_id} para user {session['user_id']}: {e}")
        conn.rollback()
    finally:
        conn.close()
        
    return redirect(url_for('index'))

@app.route('/admin')
@admin_required
def admin():
    conn = get_db_connection()
    
    users = conn.execute("SELECT id, name, email, phone, is_admin FROM users").fetchall()
    miniaturas = conn.execute("SELECT * FROM miniaturas").fetchall()
    reservations = conn.execute('''
        SELECT
            r.id AS reservation_id,
            u.name AS user_name,
            u.email AS user_email,
            m.name AS miniatura_name,
            r.quantity,
            r.reservation_date,
            r.status
        FROM reservations r
        JOIN users u ON r.user_id = u.id
        JOIN miniaturas m ON r.miniatura_id = m.id
        ORDER BY r.reservation_date DESC
    ''').fetchall()
    
    conn.close()
    return render_template_string(ADMIN_HTML, users=users, miniaturas=miniaturas, reservations=reservations, logo_url=LOGO_URL)

@app.route('/admin/add_miniatura', methods=['POST'])
@admin_required
def add_miniatura():
    name = request.form['name']
    description = request.form['description']
    price = float(request.form['price'])
    stock = int(request.form['stock'])
    image_url = request.form['image_url']

    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO miniaturas (name, description, price, stock, image_url) VALUES (?, ?, ?, ?, ?)",
                       (name, description, price, stock, image_url))
        conn.commit()
        flash(f'Miniatura "{name}" adicionada com sucesso!', 'success')
        logging.info(f"Miniatura adicionada: {name}")
        create_json_backup() # Backup após adicionar miniatura
    except Exception as e:
        flash(f'Erro ao adicionar miniatura: {e}', 'danger')
        logging.error(f"Erro ao adicionar miniatura {name}: {e}")
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/edit_miniatura/<int:miniatura_id>', methods=['POST'])
@admin_required
def edit_miniatura(miniatura_id):
    name = request.form['name']
    description = request.form['description']
    price = float(request.form['price'])
    stock = int(request.form['stock'])
    image_url = request.form['image_url']

    conn = get_db_connection()
    try:
        conn.execute("UPDATE miniaturas SET name = ?, description = ?, price = ?, stock = ?, image_url = ? WHERE id = ?",
                       (name, description, price, stock, image_url, miniatura_id))
        conn.commit()
        flash(f'Miniatura "{name}" atualizada com sucesso!', 'success')
        logging.info(f"Miniatura editada: {name} (ID: {miniatura_id})")
        create_json_backup() # Backup após editar miniatura
    except Exception as e:
        flash(f'Erro ao editar miniatura: {e}', 'danger')
        logging.error(f"Erro ao editar miniatura {miniatura_id}: {e}")
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/delete_miniatura/<int:miniatura_id>', methods=['POST'])
@admin_required
def delete_miniatura(miniatura_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM miniaturas WHERE id = ?", (miniatura_id,))
        conn.commit()
        flash('Miniatura excluída com sucesso!', 'success')
        logging.info(f"Miniatura excluída (ID: {miniatura_id})")
        create_json_backup() # Backup após excluir miniatura
    except Exception as e:
        flash(f'Erro ao excluir miniatura: {e}', 'danger')
        logging.error(f"Erro ao excluir miniatura {miniatura_id}: {e}")
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    conn = get_db_connection()
    try:
        # Verifica se o usuário é admin antes de excluir
        user_to_delete = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if user_to_delete and user_to_delete['is_admin'] == 1:
            flash('Não é possível excluir um usuário administrador.', 'danger')
            return redirect(url_for('admin'))

        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        flash('Usuário excluído com sucesso!', 'success')
        logging.info(f"Usuário excluído (ID: {user_id})")
        create_json_backup() # Backup após excluir usuário
    except Exception as e:
        flash(f'Erro ao excluir usuário: {e}', 'danger')
        logging.error(f"Erro ao excluir usuário {user_id}: {e}")
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/update_reservation_status/<int:reservation_id>', methods=['POST'])
@admin_required
def update_reservation_status(reservation_id):
    new_status = request.form['status']
    conn = get_db_connection()
    try:
        conn.execute("UPDATE reservations SET status = ? WHERE id = ?", (new_status, reservation_id))
        conn.commit()
        flash(f'Status da reserva {reservation_id} atualizado para "{new_status}" com sucesso!', 'success')
        logging.info(f"Status da reserva {reservation_id} atualizado para {new_status}")
        create_json_backup() # Backup após atualizar status
    except Exception as e:
        flash(f'Erro ao atualizar status da reserva: {e}', 'danger')
        logging.error(f"Erro ao atualizar status da reserva {reservation_id}: {e}")
    finally:
        conn.close()
    return redirect(url_for('admin'))

@app.route('/admin/export-reservas')
@admin_required
def export_reservas():
    excel_buffer = export_to_excel_reservas()
    return send_file(excel_buffer,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name='reservas_jgminis.xlsx')

# Templates HTML (inline para simplificar o deploy)
BASE_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JG MINIS</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { background-color: #f8f9fa; }
        .navbar { background-color: #343a40 !important; }
        .navbar-brand img { max-height: 40px; }
        .footer { background-color: #343a40; color: white; padding: 20px 0; text-align: center; }
        .card-miniatura { transition: transform 0.2s; }
        .card-miniatura:hover { transform: translateY(-5px); }
        .whatsapp-float {
            position: fixed;
            width: 60px;
            height: 60px;
            bottom: 40px;
            right: 40px;
            background-color: #25d366;
            color: #FFF;
            border-radius: 50px;
            text-align: center;
            font-size: 30px;
            box-shadow: 2px 2px 3px #999;
            z-index: 100;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .whatsapp-float i {
            margin-top: 0; /* Ajuste para centralizar o ícone */
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark">
        <div class="container">
            <a class="navbar-brand" href="{{ url_for('index') }}">
                <img src="{{ logo_url }}" alt="JG MINIS Logo">
                JG MINIS
            </a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav" aria-controls="navbarNav" aria-expanded="false" aria-label="Toggle navigation">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    {% if 'user_id' in session %}
                        <li class="nav-item">
                            <a class="nav-link" href="#">Olá, {{ session['user_name'] }}</a>
                        </li>
                        {% if session['is_admin'] == 1 %}
                            <li class="nav-item">
                                <a class="nav-link" href="{{ url_for('admin') }}">Admin Dashboard</a>
                            </li>
                        {% endif %}
                        <li class="nav-item">
                            <a class="nav-link" href="{{ url_for('logout') }}">Sair</a>
                        </li>
                    {% else %}
                        <li class="nav-item">
                            <a class="nav-link" href="{{ url_for('login') }}">Login</a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link" href="{{ url_for('register') }}">Registrar</a>
                        </li>
                    {% endif %}
                </ul>
            </div>
        </div>
    </nav>

    <div class="container mt-4">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                        {{ message }}
                        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        {% block content %}{% endblock %}
    </div>

    <a href="https://wa.me/{{ WHATSAPP_NUMERO }}" class="whatsapp-float" target="_blank">
        <i class="fab fa-whatsapp"></i>
    </a>

    <footer class="footer mt-5">
        <div class="container">
            <p>&copy; {{ '%Y'|format(now()) }} JG MINIS. Todos os direitos reservados.</p>
        </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        // Função para obter o ano atual para o footer
        function now() {
            return new Date().getFullYear();
        }
    </script>
</body>
</html>
"""

INDEX_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', """
{% block content %}
<h1 class="mb-4 text-center">Catálogo de Miniaturas</h1>

<div class="d-flex justify-content-end mb-3">
    <div class="dropdown">
        <button class="btn btn-secondary dropdown-toggle" type="button" id="sortDropdown" data-bs-toggle="dropdown" aria-expanded="false">
            Ordenar por: 
            {% if sort_by == 'name' %}Nome{% elif sort_by == 'price' %}Preço{% endif %} 
            {% if order == 'asc' %}(Crescente){% else %}(Decrescente){% endif %}
        </button>
        <ul class="dropdown-menu" aria-labelledby="sortDropdown">
            <li><a class="dropdown-item" href="{{ url_for('index', sort_by='name', order='asc') }}">Nome (A-Z)</a></li>
            <li><a class="dropdown-item" href="{{ url_for('index', sort_by='name', order='desc') }}">Nome (Z-A)</a></li>
            <li><a class="dropdown-item" href="{{ url_for('index', sort_by='price', order='asc') }}">Preço (Menor para Maior)</a></li>
            <li><a class="dropdown-item" href="{{ url_for('index', sort_by='price', order='desc') }}">Preço (Maior para Menor)</a></li>
        </ul>
    </div>
</div>

<div class="row">
    {% for miniatura in miniaturas %}
    <div class="col-md-4 mb-4">
        <div class="card h-100 shadow-sm card-miniatura">
            <img src="{{ miniatura.image_url or 'https://via.placeholder.com/300x200?text=Sem+Imagem' }}" class="card-img-top" alt="{{ miniatura.name }}" style="height: 200px; object-fit: cover;">
            <div class="card-body d-flex flex-column">
                <h5 class="card-title">{{ miniatura.name }}</h5>
                <p class="card-text flex-grow-1">{{ miniatura.description }}</p>
                <p class="card-text"><strong>Preço:</strong> R$ {{ "%.2f"|format(miniatura.price) }}</p>
                <p class="card-text"><strong>Estoque:</strong> {{ miniatura.stock }}</p>
                {% if 'user_id' in session %}
                    {% if miniatura.stock > 0 %}
                        <form action="{{ url_for('reservar', miniatura_id=miniatura.id) }}" method="post" class="mt-auto">
                            <div class="input-group mb-3">
                                <input type="number" name="quantity" class="form-control" value="1" min="1" max="{{ miniatura.stock }}" required>
                                <button type="submit" class="btn btn-primary">Reservar</button>
                            </div>
                        </form>
                    {% else %}
                        <button class="btn btn-secondary mt-auto" disabled>Esgotado</button>
                    {% endif %}
                {% else %}
                    <a href="{{ url_for('login') }}" class="btn btn-info mt-auto">Faça login para reservar</a>
                {% endif %}
            </div>
        </div>
    </div>
    {% endfor %}
</div>
{% endblock %}
""")

REGISTER_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', """
{% block content %}
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card shadow-sm mt-5">
            <div class="card-body">
                <h2 class="card-title text-center mb-4">Registrar</h2>
                <form action="{{ url_for('register') }}" method="post">
                    <div class="mb-3">
                        <label for="name" class="form-label">Nome Completo</label>
                        <input type="text" class="form-control" id="name" name="name" required>
                    </div>
                    <div class="mb-3">
                        <label for="email" class="form-label">Email</label>
                        <input type="email" class="form-control" id="email" name="email" required>
                    </div>
                    <div class="mb-3">
                        <label for="phone" class="form-label">Telefone (opcional)</label>
                        <input type="text" class="form-control" id="phone" name="phone">
                    </div>
                    <div class="mb-3">
                        <label for="password" class="form-label">Senha</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Registrar</button>
                </form>
                <p class="text-center mt-3">Já tem uma conta? <a href="{{ url_for('login') }}">Faça Login</a></p>
            </div>
        </div>
    </div>
</div>
{% endblock %}
""")

LOGIN_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', """
{% block content %}
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card shadow-sm mt-5">
            <div class="card-body">
                <h2 class="card-title text-center mb-4">Login</h2>
                <form action="{{ url_for('login') }}" method="post">
                    <div class="mb-3">
                        <label for="email" class="form-label">Email</label>
                        <input type="email" class="form-control" id="email" name="email" required>
                    </div>
                    <div class="mb-3">
                        <label for="password" class="form-label">Senha</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Entrar</button>
                </form>
                <p class="text-center mt-3">Não tem uma conta? <a href="{{ url_for('register') }}">Registre-se</a></p>
            </div>
        </div>
    </div>
</div>
{% endblock %}
""")

ADMIN_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', """
{% block content %}
<h1 class="mb-4 text-center">Admin Dashboard</h1>

<ul class="nav nav-tabs mb-4" id="adminTabs" role="tablist">
    <li class="nav-item" role="presentation">
        <button class="nav-link active" id="users-tab" data-bs-toggle="tab" data-bs-target="#users" type="button" role="tab" aria-controls="users" aria-selected="true">Usuários</button>
    </li>
    <li class="nav-item" role="presentation">
        <button class="nav-link" id="miniaturas-tab" data-bs-toggle="tab" data-bs-target="#miniaturas" type="button" role="tab" aria-controls="miniaturas" aria-selected="false">Miniaturas</button>
    </li>
    <li class="nav-item" role="presentation">
        <button class="nav-link" id="reservations-tab" data-bs-toggle="tab" data-bs-target="#reservations" type="button" role="tab" aria-controls="reservations" aria-selected="false">Reservas</button>
    </li>
</ul>

<div class="tab-content" id="adminTabsContent">
    <!-- Tab Usuários -->
    <div class="tab-pane fade show active" id="users" role="tabpanel" aria-labelledby="users-tab">
        <h2>Gerenciar Usuários</h2>
        <div class="table-responsive">
            <table class="table table-striped table-hover">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Nome</th>
                        <th>Email</th>
                        <th>Telefone</th>
                        <th>Admin</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for user in users %}
                    <tr>
                        <td>{{ user.id }}</td>
                        <td>{{ user.name }}</td>
                        <td>{{ user.email }}</td>
                        <td>{{ user.phone or 'N/A' }}</td>
                        <td>{{ 'Sim' if user.is_admin else 'Não' }}</td>
                        <td>
                            {% if user.is_admin == 0 %}
                                <form action="{{ url_for('delete_user', user_id=user.id) }}" method="post" style="display:inline-block;">
                                    <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Tem certeza que deseja excluir este usuário?');">Excluir</button>
                                </form>
                            {% else %}
                                <button class="btn btn-secondary btn-sm" disabled>Admin</button>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Tab Miniaturas -->
    <div class="tab-pane fade" id="miniaturas" role="tabpanel" aria-labelledby="miniaturas-tab">
        <h2>Gerenciar Miniaturas</h2>
        <button type="button" class="btn btn-success mb-3" data-bs-toggle="modal" data-bs-target="#addMiniaturaModal">
            Adicionar Nova Miniatura
        </button>
        <div class="table-responsive">
            <table class="table table-striped table-hover">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Nome</th>
                        <th>Descrição</th>
                        <th>Preço</th>
                        <th>Estoque</th>
                        <th>Imagem URL</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for miniatura in miniaturas %}
                    <tr>
                        <td>{{ miniatura.id }}</td>
                        <td>{{ miniatura.name }}</td>
                        <td>{{ miniatura.description }}</td>
                        <td>R$ {{ "%.2f"|format(miniatura.price) }}</td>
                        <td>{{ miniatura.stock }}</td>
                        <td><a href="{{ miniatura.image_url }}" target="_blank">Ver Imagem</a></td>
                        <td>
                            <button type="button" class="btn btn-warning btn-sm" data-bs-toggle="modal" data-bs-target="#editMiniaturaModal{{ miniatura.id }}">
                                Editar
                            </button>
                            <form action="{{ url_for('delete_miniatura', miniatura_id=miniatura.id) }}" method="post" style="display:inline-block;">
                                <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Tem certeza que deseja excluir esta miniatura?');">Excluir</button>
                            </form>

                            <!-- Modal Editar Miniatura -->
                            <div class="modal fade" id="editMiniaturaModal{{ miniatura.id }}" tabindex="-1" aria-labelledby="editMiniaturaModalLabel{{ miniatura.id }}" aria-hidden="true">
                                <div class="modal-dialog">
                                    <div class="modal-content">
                                        <div class="modal-header">
                                            <h5 class="modal-title" id="editMiniaturaModalLabel{{ miniatura.id }}">Editar Miniatura</h5>
                                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                                        </div>
                                        <div class="modal-body">
                                            <form action="{{ url_for('edit_miniatura', miniatura_id=miniatura.id) }}" method="post">
                                                <div class="mb-3">
 <label for="editName{{ miniatura.id }}" class="form-label">Nome</label>
 <input type="text" class="form-control" id="editName{{ miniatura.id }}" name="name" value="{{ miniatura.name }}" required>
                                                </div>
                                                <div class="mb-3">
 <label for="editDescription{{ miniatura.id }}" class="form-label">Descrição</label>
 <textarea class="form-control" id="editDescription{{ miniatura.id }}" name="description">{{ miniatura.description }}</textarea>
                                                </div>
                                                <div class="mb-3">
 <label for="editPrice{{ miniatura.id }}" class="form-label">Preço</label>
 <input type="number" step="0.01" class="form-control" id="editPrice{{ miniatura.id }}" name="price" value="{{ miniatura.price }}" required>
                                                </div>
                                                <div class="mb-3">
 <label for="editStock{{ miniatura.id }}" class="form-label">Estoque</label>
 <input type="number" class="form-control" id="editStock{{ miniatura.id }}" name="stock" value="{{ miniatura.stock }}" required>
                                                </div>
                                                <div class="mb-3">
 <label for="editImageUrl{{ miniatura.id }}" class="form-label">URL da Imagem</label>
 <input type="url" class="form-control" id="editImageUrl{{ miniatura.id }}" name="image_url" value="{{ miniatura.image_url }}">
                                                </div>
                                                <button type="submit" class="btn btn-primary">Salvar Alterações</button>
                                            </form>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <!-- Modal Adicionar Miniatura -->
        <div class="modal fade" id="addMiniaturaModal" tabindex="-1" aria-labelledby="addMiniaturaModalLabel" aria-hidden="true">
            <div class="modal-dialog">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="addMiniaturaModalLabel">Adicionar Nova Miniatura</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                    </div>
                    <div class="modal-body">
                        <form action="{{ url_for('add_miniatura') }}" method="post">
                            <div class="mb-3">
                                <label for="name" class="form-label">Nome</label>
                                <input type="text" class="form-control" id="name" name="name" required>
                            </div>
                            <div class="mb-3">
                                <label for="description" class="form-label">Descrição</label>
                                <textarea class="form-control" id="description" name="description"></textarea>
                            </div>
                            <div class="mb-3">
                                <label for="price" class="form-label">Preço</label>
                                <input type="number" step="0.01" class="form-control" id="price" name="price" required>
                            </div>
                            <div class="mb-3">
                                <label for="stock" class="form-label">Estoque</label>
                                <input type="number" class="form-control" id="stock" name="stock" required>
                            </div>
                            <div class="mb-3">
                                <label for="image_url" class="form-label">URL da Imagem</label>
                                <input type="url" class="form-control" id="image_url" name="image_url">
                            </div>
                            <button type="submit" class="btn btn-primary">Adicionar Miniatura</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Tab Reservas -->
    <div class="tab-pane fade" id="reservations" role="tabpanel" aria-labelledby="reservations-tab">
        <h2>Gerenciar Reservas</h2>
        <a href="{{ url_for('export_reservas') }}" class="btn btn-info mb-3">Exportar Reservas para Excel</a>
        <div class="table-responsive">
            <table class="table table-striped table-hover">
                <thead>
                    <tr>
                        <th>ID Reserva</th>
                        <th>Usuário</th>
                        <th>Email Usuário</th>
                        <th>Miniatura</th>
                        <th>Quantidade</th>
                        <th>Data Reserva</th>
                        <th>Status</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for reservation in reservations %}
                    <tr>
                        <td>{{ reservation.reservation_id }}</td>
                        <td>{{ reservation.user_name }}</td>
                        <td>{{ reservation.user_email }}</td>
                        <td>{{ reservation.miniatura_name }}</td>
                        <td>{{ reservation.quantity }}</td>
                        <td>{{ reservation.reservation_date }}</td>
                        <td>{{ reservation.status }}</td>
                        <td>
                            <form action="{{ url_for('update_reservation_status', reservation_id=reservation.reservation_id) }}" method="post" style="display:inline-block;">
                                <select name="status" class="form-select form-select-sm" onchange="this.form.submit()">
                                    <option value="Confirmada" {% if reservation.status == 'Confirmada' %}selected{% endif %}>Confirmada</option>
                                    <option value="Pendente" {% if reservation.status == 'Pendente' %}selected{% endif %}>Pendente</option>
                                    <option value="Cancelada" {% if reservation.status == 'Cancelada' %}selected{% endif %}>Cancelada</option>
                                    <option value="Concluída" {% if reservation.status == 'Concluída' %}selected{% endif %}>Concluída</option>
                                </select>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}
""")

# Inicialização do Banco de Dados e Restauração de Backup
with app.app_context():
    init_db()
    restore_backup() # Tenta restaurar o backup local ao iniciar

# Ponto de entrada da aplicação
if __name__ == '__main__':
    # Para execução local, use debug=True. Em produção, False.
    # O host 0.0.0.0 é necessário para que a aplicação seja acessível externamente em ambientes de container.
    app.run(host='0.0.0.0', port=8080, debug=False)
