import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from functools import wraps
import os
from datetime import datetime
import re # Para validação de telefone e email

app = Flask(__name__)
app.secret_key = 'sua_chave_secreta_aqui' # Mude para uma chave secreta forte e única

# --- Funções de Banco de Dados ---
def get_db_connection():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect('database.db')
        db.row_factory = sqlite3.Row # Retorna linhas como dicionários
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db_connection()
        cursor = db.cursor()
        # Cria a tabela de usuários se não existir
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                telefone TEXT,
                senha TEXT NOT NULL,
                data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_admin INTEGER DEFAULT 0 -- Adiciona coluna para admin
            )
        ''')
        # Cria a tabela de reservas se não existir
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reservas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                data_reserva TEXT NOT NULL,
                hora_reserva TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        # Adiciona um usuário admin padrão se não existir
        cursor.execute("SELECT * FROM users WHERE email = 'admin@example.com'")
        admin_user = cursor.fetchone()
        if not admin_user:
            cursor.execute("INSERT INTO users (nome, email, telefone, senha, is_admin) VALUES (?, ?, ?, ?, ?)",
                           ('Admin', 'admin@example.com', '11999999999', 'admin123', 1)) # Senha simples para exemplo, usar hash em produção
        db.commit()

# Inicializa o banco de dados ao iniciar o app
with app.app_context():
    init_db()

# --- Decoradores de Autenticação e Autorização ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa estar logado para acessar esta página.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa estar logado para acessar esta página.', 'danger')
            return redirect(url_for('login'))
        
        db = get_db_connection()
        user = db.execute('SELECT is_admin FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        
        if user and user['is_admin'] == 1:
            return f(*args, **kwargs)
        else:
            flash('Acesso negado. Você não tem permissão de administrador.', 'danger')
            return redirect(url_for('dashboard')) # Ou para a página inicial
    return decorated_function

# --- Rotas Existentes (Assumindo a Estrutura Comum) ---

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    # NOTA: Remova a linha "Admin: admin@example.com / admin123" diretamente do seu arquivo login.html
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        db = get_db_connection()
        user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

        if user and user['senha'] == senha: # Em produção, use hash de senha (ex: bcrypt)
            session['user_id'] = user['id']
            session['user_name'] = user['nome']
            session['is_admin'] = user['is_admin']
            flash('Login realizado com sucesso!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        telefone = request.form['telefone']
        senha = request.form['senha']

        # Validação de email e telefone
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Formato de email inválido.', 'danger')
            return render_template('register.html', nome=nome, email=email, telefone=telefone)
        if not re.match(r"^\d{10,11}$", telefone): # Ex: 11987654321 ou 1187654321
            flash('Formato de telefone inválido. Use apenas números (10 ou 11 dígitos).', 'danger')
            return render_template('register.html', nome=nome, email=email, telefone=telefone)

        db = get_db_connection()
        try:
            db.execute('INSERT INTO users (nome, email, telefone, senha) VALUES (?, ?, ?, ?)',
                       (nome, email, telefone, senha))
            db.commit()
            flash('Cadastro realizado com sucesso! Faça login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Este email já está cadastrado.', 'danger')
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    db = get_db_connection()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    return render_template('dashboard.html', user=user)

@app.route('/reservar', methods=['GET', 'POST'])
@login_required
def reservar():
    if request.method == 'POST':
        data_reserva = request.form['data_reserva']
        hora_reserva = request.form['hora_reserva']
        user_id = session['user_id']

        # Validação simples para data e hora
        if not data_reserva or not hora_reserva:
            flash('Por favor, preencha a data e a hora da reserva.', 'danger')
            return render_template('reservar.html')
        
        # Opcional: Adicionar validação para garantir que a data não é passada
        # e que a hora está em um formato válido (ex: HH:MM)

        db = get_db_connection()
        db.execute('INSERT INTO reservas (user_id, data_reserva, hora_reserva) VALUES (?, ?, ?)',
                   (user_id, data_reserva, hora_reserva))
        db.commit()
        flash('Reserva realizada com sucesso!', 'success')
        return redirect(url_for('minhas_reservas'))
    return render_template('reservar.html')

@app.route('/minhas-reservas')
@login_required
def minhas_reservas():
    user_id = session['user_id']
    db = get_db_connection()
    reservas = db.execute('SELECT * FROM reservas WHERE user_id = ? ORDER BY data_reserva DESC, hora_reserva DESC',
                          (user_id,)).fetchall()
    return render_template('minhas_reservas.html', reservas=reservas)

@app.route('/cancelar-reserva/<int:reserva_id>', methods=['POST'])
@login_required
def cancelar_reserva(reserva_id):
    user_id = session['user_id']
    db = get_db_connection()
    
    # Verifica se a reserva pertence ao usuário logado
    reserva = db.execute('SELECT * FROM reservas WHERE id = ? AND user_id = ?', (reserva_id, user_id)).fetchone()
    
    if reserva:
        db.execute('DELETE FROM reservas WHERE id = ?', (reserva_id,))
        db.commit()
        flash('Reserva cancelada com sucesso!', 'success')
    else:
        flash('Reserva não encontrada ou você não tem permissão para cancelá-la.', 'danger')
    
    return redirect(url_for('minhas_reservas'))

# --- NOVAS ROTAS PARA ADMIN ---

@app.route('/pessoas')
@admin_required
def pessoas():
    db = get_db_connection()
    users = db.execute('SELECT id, nome, email, telefone, data_cadastro, is_admin FROM users ORDER BY data_cadastro DESC').fetchall()
    return render_template('pessoas.html', users=users)

@app.route('/editar-pessoa/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def editar_pessoa(user_id):
    db = get_db_connection()
    user = db.execute('SELECT id, nome, email, telefone, is_admin FROM users WHERE id = ?', (user_id,)).fetchone()

    if not user:
        flash('Usuário não encontrado.', 'danger')
        return redirect(url_for('pessoas'))

    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        telefone = request.form['telefone']
        is_admin = 1 if 'is_admin' in request.form else 0
        
        # Validação de email e telefone
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash('Formato de email inválido.', 'danger')
            return render_template('editar_pessoa.html', user=user)
        if not re.match(r"^\d{10,11}$", telefone):
            flash('Formato de telefone inválido. Use apenas números (10 ou 11 dígitos).', 'danger')
            return render_template('editar_pessoa.html', user=user)

        try:
            db.execute('UPDATE users SET nome = ?, email = ?, telefone = ?, is_admin = ? WHERE id = ?',
                       (nome, email, telefone, is_admin, user_id))
            db.commit()
            flash('Dados do usuário atualizados com sucesso!', 'success')
            return redirect(url_for('pessoas'))
        except sqlite3.IntegrityError:
            flash('Este email já está cadastrado para outro usuário.', 'danger')
            return render_template('editar_pessoa.html', user=user)

    return render_template('editar_pessoa.html', user=user)

@app.route('/deletar-pessoa/<int:user_id>', methods=['POST'])
@admin_required
def deletar_pessoa(user_id):
    db = get_db_connection()
    
    # Impede que o admin logado se delete
    if user_id == session['user_id']:
        flash('Você não pode deletar sua própria conta de administrador.', 'danger')
        return redirect(url_for('pessoas'))

    # Primeiro, deleta as reservas associadas ao usuário
    db.execute('DELETE FROM reservas WHERE user_id = ?', (user_id,))
    # Depois, deleta o usuário
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash('Usuário e suas reservas deletados com sucesso!', 'success')
    return redirect(url_for('pessoas'))

# --- Execução do App ---
if __name__ == '__main__':
    # Para rodar localmente, use debug=True. Para produção, use Gunicorn ou similar.
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
