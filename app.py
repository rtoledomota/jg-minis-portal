import os
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify
import gspread
from google.oauth2.service_account import Credentials
import hashlib
import sqlite3
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_secret')

# Credenciais Google Sheets (assumindo variáveis de ambiente para Railway)
# As variáveis de ambiente devem ser configuradas no Railway
# GOOGLE_CREDENTIALS_JSON deve conter o JSON completo como uma string única,
# com '\n' substituído por '\\n' para a private_key.
# Exemplo de como GOOGLE_CREDENTIALS_JSON deve ser no Railway:
# {"type": "service_account", "project_id": "...", "private_key_id": "...", "private_key": "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n", "client_email": "...", "client_id": "...", "auth_uri": "...", "token_uri": "...", "auth_provider_x509_cert_url": "...", "client_x509_cert_url": "..."}
# O código abaixo irá parsear essa string.
try:
    google_credentials_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if google_credentials_json_str:
        creds_dict = json.loads(google_credentials_json_str)
        creds = Credentials.from_service_account_info(creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        gc = gspread.authorize(creds)
        print("gspread: Autenticação bem-sucedida.")
    else:
        print("gspread: GOOGLE_CREDENTIALS_JSON não configurado. Funcionalidades do Sheets desativadas.")
        gc = None
except Exception as e:
    print(f"gspread: Erro ao carregar credenciais ou autenticar: {e}")
    gc = None

sheet_id = os.getenv('SHEET_ID')
if gc and sheet_id:
    try:
        sheet = gc.open_by_key(sheet_id)
        print(f"gspread: Planilha '{sheet_id}' aberta com sucesso.")
    except Exception as e:
        print(f"gspread: Erro ao abrir planilha '{sheet_id}': {e}")
        sheet = None
else:
    print("gspread: SHEET_ID não configurado ou gc não inicializado. Planilha não acessível.")
    sheet = None

# Dados em memória (sync com Sheets)
carros = []
usuarios = []
reservas = []

# Função para carregar dados da planilha
def load_data():
    global carros, usuarios, reservas
    if not sheet:
        print("load_data: Planilha não acessível. Carregando dados vazios.")
        carros = []
        usuarios = []
        reservas = []
        return

    try:
        # Carregar aba 'Carros'
        try:
            carros_sheet = sheet.worksheet('Carros')
        except gspread.WorksheetNotFound:
            print("load_data: Aba 'Carros' não encontrada. Criando...")
            carros_sheet = sheet.add_worksheet('Carros', 1000, 10)
            carros_sheet.append_row(['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO'])
        
        data_carros = carros_sheet.get_all_records()
        carros = []
        for i, row in enumerate(data_carros):
            # Mapeamento das colunas da sua planilha para o modelo do app
            carro = {
                'id': int(row.get('ID', i + 1)), # Garante ID numérico
                'thumbnail_url': row.get('IMAGEM', ''),
                'modelo': row.get('NOME DA MINIATURA', ''),
                'marca': row.get('MARCA/FABRICANTE', ''),
                'ano': row.get('PREVISÃO DE CHEGADA', ''), # Usando PREVISÃO DE CHEGADA como 'ano'
                'quantidade_disponivel': int(row.get('QUANTIDADE DISPONIVEL', 0)),
                'preco_diaria': float(row.get('VALOR', 0)),
                'observacoes': row.get('OBSERVAÇÕES', ''),
                'max_reservas': int(row.get('MAX_RESERVAS_POR_USUARIO', 1))
            }
            carros.append(carro)
        print(f"load_data: Carregados {len(carros)} carros da planilha.")
        
        # Carregar aba 'Usuarios'
        try:
            usuarios_sheet = sheet.worksheet('Usuarios')
        except gspread.WorksheetNotFound:
            print("load_data: Aba 'Usuarios' não encontrada. Criando...")
            usuarios_sheet = sheet.add_worksheet('Usuarios', 100, 5)
            usuarios_sheet.append_row(['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data_Cadastro', 'Admin'])
        
        data_usuarios = usuarios_sheet.get_all_records()
        usuarios = []
        for i, row in enumerate(data_usuarios):
            usuario = {
                'id': int(row.get('ID', i + 1)),
                'nome': row.get('Nome', ''),
                'email': row.get('Email', ''),
                'senha_hash': row.get('Senha_hash', ''),
                'cpf': row.get('CPF', ''),
                'telefone': row.get('Telefone', ''),
                'data_cadastro': row.get('Data_Cadastro', ''),
                'admin': row.get('Admin', 'Não').lower() == 'sim'
            }
            usuarios.append(usuario)
        print(f"load_data: Carregados {len(usuarios)} usuários da planilha.")

        # Garantir que o admin padrão exista se a planilha estiver vazia
        if not any(u['email'] == 'admin@jgminis.com' for u in usuarios):
            print("load_data: Admin padrão não encontrado na planilha. Adicionando...")
            admin_user = {
                'id': max([u['id'] for u in usuarios] + [0]) + 1,
                'nome': 'Admin',
                'email': 'admin@jgminis.com',
                'senha_hash': hashlib.md5('admin123'.encode()).hexdigest(), # Hash para 'admin123'
                'cpf': '000.000.000-00',
                'telefone': '(00)00000-0000',
                'data_cadastro': datetime.now().strftime('%Y-%m-%d'),
                'admin': True
            }
            usuarios.append(admin_user)
            # Sincronizar imediatamente para adicionar o admin na planilha
            sync_to_sheets()
        
        # Carregar aba 'Reservas'
        try:
            reservas_sheet = sheet.worksheet('Reservas')
        except gspread.WorksheetNotFound:
            print("load_data: Aba 'Reservas' não encontrada. Criando...")
            reservas_sheet = sheet.add_worksheet('Reservas', 1000, 8)
            reservas_sheet.append_row(['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes'])
        
        data_reservas = reservas_sheet.get_all_records()
        reservas = []
        for i, row in enumerate(data_reservas):
            # Verifica se a linha tem dados suficientes antes de acessar
            if len(row) >= 8: # Ajuste conforme o número de colunas esperadas
                reserva = {
                    'id': int(row.get('ID', i + 1)),
                    'usuario_id': int(row.get('Usuario_id', 0)),
                    'carro_id': int(row.get('Carro_id', 0)),
                    'data_reserva': row.get('Data_reserva', ''),
                    'hora_inicio': row.get('Hora_inicio', ''),
                    'hora_fim': row.get('Hora_fim', ''),
                    'status': row.get('Status', 'Ativa'),
                    'observacoes': row.get('Observacoes', '')
                }
                reservas.append(reserva)
            else:
                print(f"load_data: Linha de reserva incompleta na planilha: {row}")
        print(f"load_data: Carregadas {len(reservas)} reservas da planilha.")

    except Exception as e:
        print(f"Erro ao carregar dados da planilha: {e}")
        carros = []
        usuarios = []
        reservas = []

# Função para sincronizar dados de volta para Sheets
def sync_to_sheets():
    if not sheet:
        print("sync_to_sheets: Planilha não acessível. Sincronização desativada.")
        return

    try:
        # Sincronizar Carros
        carros_sheet = sheet.worksheet('Carros')
        carros_sheet.clear()
        carros_sheet.append_row(['ID', 'IMAGEM', 'NOME DA MINIATURA', 'MARCA/FABRICANTE', 'PREVISÃO DE CHEGADA', 'QUANTIDADE DISPONIVEL', 'VALOR', 'OBSERVAÇÕES', 'MAX_RESERVAS_POR_USUARIO'])
        for carro in carros:
            carros_sheet.append_row([
                carro['id'], carro['thumbnail_url'], carro['modelo'], carro['marca'], 
                carro['ano'], carro['quantidade_disponivel'], carro['preco_diaria'], 
                carro['observacoes'], carro['max_reservas']
            ])
        print("sync_to_sheets: Carros sincronizados com sucesso.")
        
        # Sincronizar Usuarios
        usuarios_sheet = sheet.worksheet('Usuarios')
        usuarios_sheet.clear()
        usuarios_sheet.append_row(['ID', 'Nome', 'Email', 'Senha_hash', 'CPF', 'Telefone', 'Data_Cadastro', 'Admin'])
        for usuario in usuarios:
            usuarios_sheet.append_row([
                usuario['id'], usuario['nome'], usuario['email'], usuario['senha_hash'],
                usuario['cpf'], usuario['telefone'], usuario['data_cadastro'], 'Sim' if usuario['admin'] else 'Não'
            ])
        print("sync_to_sheets: Usuários sincronizados com sucesso.")
        
        # Sincronizar Reservas
        reservas_sheet = sheet.worksheet('Reservas')
        reservas_sheet.clear()
        reservas_sheet.append_row(['ID', 'Usuario_id', 'Carro_id', 'Data_reserva', 'Hora_inicio', 'Hora_fim', 'Status', 'Observacoes'])
        for reserva in reservas:
            reservas_sheet.append_row([
                reserva['id'], reserva['usuario_id'], reserva['carro_id'], reserva['data_reserva'],
                reserva['hora_inicio'], reserva['hora_fim'], reserva['status'], reserva['observacoes']
            ])
        print("sync_to_sheets: Reservas sincronizadas com sucesso.")

    except Exception as e:
        print(f"Erro ao sincronizar dados para a planilha: {e}")

# Load inicial dos dados na inicialização do app
load_data()

# Rota /health
@app.route('/health')
def health():
    return 'OK'

# Rota /login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        senha_hash = hashlib.md5(senha.encode()).hexdigest()
        
        user = next((u for u in usuarios if u['email'] == email and u['senha_hash'] == senha_hash), None)
        
        if user:
            session['logged_in'] = True
            session['user_id'] = user['id']
            session['is_admin'] = user['admin']
            return redirect(url_for('home'))
        
        return render_template_string('''
            <p style="color: red;">Login falhou. Verifique seu email e senha.</p>
            <form method="post">
                Email: <input type="email" name="email" value="{{ request.form.email or '' }}"><br>
                Senha: <input type="password" name="senha"><br>
                <input type="submit" value="Login">
            </form>
        ''')
    
    return render_template_string('''
    <html>
    <head>
        <title>Login</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f4f4; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .login-container { background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1); width: 300px; text-align: center; }
            h2 { color: #333; margin-bottom: 20px; }
            input[type="email"], input[type="password"] { width: calc(100% - 20px); padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
            input[type="submit"] { background-color: #007bff; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; }
            input[type="submit"]:hover { background-color: #0056b3; }
            p { color: red; margin-top: 10px; }
        </style>
    </head>
    <body>
        <div class="login-container">
            <h2>Login JG Minis</h2>
            <form method="post">
                <input type="email" name="email" placeholder="Email" required><br>
                <input type="password" name="senha" placeholder="Senha" required><br>
                <input type="submit" value="Entrar">
            </form>
        </div>
    </body>
    </html>
    ''')

# Rota /home
@app.route('/home')
def home():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    # Exibir todos os carros carregados da planilha
    displayed_carros = carros
    
    html_content = '''
    <html>
    <head>
        <title>JG Minis - Miniaturas</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; background-color: #f0f2f5; color: #333; }
            .header { background-color: #343a40; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .header h1 { margin: 0; font-size: 28px; }
            .header a { color: white; text-decoration: none; margin: 0 10px; }
            .header a:hover { text-decoration: underline; }
            .container { max-width: 1200px; margin: 20px auto; padding: 0 15px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 25px; }
            .card { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; transition: transform 0.2s ease-in-out; }
            .card:hover { transform: translateY(-5px); }
            .card img { width: 100%; height: 200px; object-fit: cover; border-bottom: 1px solid #eee; }
            .card-content { padding: 15px; }
            .card-content h3 { margin-top: 0; margin-bottom: 10px; color: #007bff; font-size: 20px; }
            .card-content p { margin: 5px 0; font-size: 14px; line-height: 1.5; }
            .card-content .price { font-size: 18px; font-weight: bold; color: #28a745; margin-top: 10px; }
            .card-content .availability { color: #6c757d; }
            .card-content button { background-color: #007bff; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-size: 15px; width: 100%; margin-top: 15px; transition: background-color 0.2s; }
            .card-content button:hover { background-color: #0056b3; }
            .no-items { text-align: center; color: #6c757d; font-size: 18px; margin-top: 50px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>JG Minis</h1>
            <nav>
                <a href="/home">Home</a>
                {% if session.get('is_admin') %}
                <a href="/admin">Admin</a>
                {% endif %}
                <a href="/logout">Sair</a>
            </nav>
        </div>
        <div class="container">
            <h2>Miniaturas Disponíveis</h2>
            <div class="grid">
    '''
    
    if displayed_carros:
        for carro in displayed_carros:
            html_content += f'''
                <div class="card">
                    <img src="{carro['thumbnail_url']}" alt="{carro['modelo']}">
                    <div class="card-content">
                        <h3>{carro['modelo']}</h3>
                        <p><strong>Marca:</strong> {carro['marca']}</p>
                        <p><strong>Previsão:</strong> {carro['ano']}</p>
                        <p class="availability">Disponível: {carro['quantidade_disponivel']}</p>
                        <p class="price">R$ {carro['preco_diaria']:.2f}</p>
                        <p><em>{carro['observacoes']}</em></p>
                        <button onclick="reservar({carro['id']})">Reservar</button>
                    </div>
                </div>
            '''
    else:
        html_content += '<div class="no-items">Nenhuma miniatura disponível no momento.</div>'

    html_content += '''
            </div>
        </div>
        <script>
            function reservar(carroId) {
                alert('Funcionalidade de reserva para o carro ' + carroId + ' ainda não implementada.');
                // Redirecionar para uma rota de reserva mais completa
                // window.location.href = '/reservar/' + carroId;
            }
        </script>
    </body>
    </html>
    '''
    return render_template_string(html_content, session=session) # Passa session para o template string

# Rota /admin
@app.route('/admin')
def admin():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    html_content = '''
    <html>
    <head>
        <title>Admin Panel</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; background-color: #f0f2f5; color: #333; }
            .header { background-color: #343a40; color: white; padding: 15px 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .header h1 { margin: 0; font-size: 28px; }
            .header a { color: white; text-decoration: none; margin: 0 10px; }
            .header a:hover { text-decoration: underline; }
            .container { max-width: 1200px; margin: 20px auto; padding: 0 15px; background-color: #fff; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
            h2 { color: #007bff; margin-top: 25px; margin-bottom: 15px; border-bottom: 2px solid #eee; padding-bottom: 10px; }
            table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background-color: #f8f9fa; color: #343a40; }
            .action-links a { margin-right: 10px; color: #007bff; text-decoration: none; }
            .action-links a:hover { text-decoration: underline; }
            .add-button { display: inline-block; background-color: #28a745; color: white; padding: 10px 15px; border-radius: 5px; text-decoration: none; margin-bottom: 20px; transition: background-color 0.2s; }
            .add-button:hover { background-color: #218838; }
            .sync-button { background-color: #ffc107; color: #333; padding: 10px 15px; border-radius: 5px; text-decoration: none; margin-top: 20px; display: inline-block; transition: background-color 0.2s; }
            .sync-button:hover { background-color: #e0a800; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>JG Minis Admin</h1>
            <nav>
                <a href="/home">Home</a>
                <a href="/admin">Admin</a>
                <a href="/logout">Sair</a>
            </nav>
        </div>
        <div class="container">
            <h2>Carros</h2>
            <a href="/admin/add_carro" class="add-button">Adicionar Carro</a>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Modelo</th>
                        <th>Marca</th>
                        <th>Preço Diário</th>
                        <th>Disponível</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
    '''
    for carro in carros:
        html_content += f'''
                    <tr>
                        <td>{carro['id']}</td>
                        <td>{carro['modelo']}</td>
                        <td>{carro['marca']}</td>
                        <td>R$ {carro['preco_diaria']:.2f}</td>
                        <td>{carro['quantidade_disponivel']}</td>
                        <td class="action-links">
                            <a href="/admin/edit_carro/{carro['id']}">Editar</a>
                            <a href="/admin/delete_carro/{carro['id']}">Deletar</a>
                        </td>
                    </tr>
        '''
    html_content += '''
                </tbody>
            </table>

            <h2>Usuários</h2>
            <a href="/admin/add_usuario" class="add-button">Adicionar Usuário</a>
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
    '''
    for usuario in usuarios:
        html_content += f'''
                    <tr>
                        <td>{usuario['id']}</td>
                        <td>{usuario['nome']}</td>
                        <td>{usuario['email']}</td>
                        <td>{'Sim' if usuario['admin'] else 'Não'}</td>
                        <td class="action-links">
                            <a href="/admin/edit_usuario/{usuario['id']}">Editar</a>
                            <a href="/admin/delete_usuario/{usuario['id']}">Deletar</a>
                        </td>
                    </tr>
        '''
    html_content += '''
                </tbody>
            </table>

            <h2>Reservas</h2>
            <a href="/admin/add_reserva" class="add-button">Adicionar Reserva</a>
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
    '''
    for reserva in reservas:
        html_content += f'''
                    <tr>
                        <td>{reserva['id']}</td>
                        <td>{reserva['usuario_id']}</td>
                        <td>{reserva['carro_id']}</td>
                        <td>{reserva['data_reserva']}</td>
                        <td>{reserva['status']}</td>
                        <td class="action-links">
                            <a href="/admin/edit_reserva/{reserva['id']}">Editar</a>
                            <a href="/admin/delete_reserva/{reserva['id']}">Deletar</a>
                        </td>
                    </tr>
        '''
    html_content += '''
                </tbody>
            </table>
            <a href="/admin/sync_sheets" class="sync-button">Sincronizar com Sheets</a>
        </div>
    </body>
    </html>
    '''
    return render_template_string(html_content, session=session)

# Rota /admin/sync_sheets
@app.route('/admin/sync_sheets')
def sync_sheets_route():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    load_data()  # Recarrega os dados da planilha para a memória
    sync_to_sheets() # Sincroniza os dados da memória de volta para a planilha (garante consistência)
    return render_template_string('''
        <p>Sincronização com Sheets concluída. <a href="/admin">Voltar para Admin</a></p>
    ''')

# Rotas para CRUD (simplificadas para demonstração)
@app.route('/admin/add_carro', methods=['GET', 'POST'])
def add_carro():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        novo_carro = {
            'id': max([c['id'] for c in carros] + [0]) + 1,
            'thumbnail_url': request.form['thumbnail_url'],
            'modelo': request.form['modelo'],
            'marca': request.form['marca'],
            'ano': request.form['ano'],
            'quantidade_disponivel': int(request.form['quantidade_disponivel']),
            'preco_diaria': float(request.form['preco_diaria']),
            'observacoes': request.form['observacoes'],
            'max_reservas': int(request.form['max_reservas'])
        }
        carros.append(novo_carro)
        sync_to_sheets() # Sincroniza a alteração para a planilha
        return redirect(url_for('admin'))
    
    return render_template_string('''
    <html>
    <head><title>Adicionar Carro</title></head>
    <body>
        <h1>Adicionar Novo Carro</h1>
        <form method="post">
            Thumbnail URL: <input type="text" name="thumbnail_url" required><br>
            Modelo: <input type="text" name="modelo" required><br>
            Marca: <input type="text" name="marca"><br>
            Previsão de Chegada (Ano): <input type="text" name="ano"><br>
            Quantidade Disponível: <input type="number" name="quantidade_disponivel" required><br>
            Valor (Preço Diário): <input type="number" step="0.01" name="preco_diaria" required><br>
            Observações: <textarea name="observacoes"></textarea><br>
            Max Reservas por Usuário: <input type="number" name="max_reservas" value="1" required><br>
            <input type="submit" value="Adicionar Carro">
        </form>
        <a href="/admin">Voltar para Admin</a>
    </body>
    </html>
    ''')

@app.route('/admin/edit_carro/<int:carro_id>', methods=['GET', 'POST'])
def edit_carro(carro_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    carro_to_edit = next((c for c in carros if c['id'] == carro_id), None)
    if not carro_to_edit:
        return "Carro não encontrado", 404

    if request.method == 'POST':
        carro_to_edit['thumbnail_url'] = request.form['thumbnail_url']
        carro_to_edit['modelo'] = request.form['modelo']
        carro_to_edit['marca'] = request.form['marca']
        carro_to_edit['ano'] = request.form['ano']
        carro_to_edit['quantidade_disponivel'] = int(request.form['quantidade_disponivel'])
        carro_to_edit['preco_diaria'] = float(request.form['preco_diaria'])
        carro_to_edit['observacoes'] = request.form['observacoes']
        carro_to_edit['max_reservas'] = int(request.form['max_reservas'])
        sync_to_sheets()
        return redirect(url_for('admin'))
    
    return render_template_string(f'''
    <html>
    <head><title>Editar Carro</title></head>
    <body>
        <h1>Editar Carro {carro_to_edit['modelo']}</h1>
        <form method="post">
            Thumbnail URL: <input type="text" name="thumbnail_url" value="{carro_to_edit['thumbnail_url']}" required><br>
            Modelo: <input type="text" name="modelo" value="{carro_to_edit['modelo']}" required><br>
            Marca: <input type="text" name="marca" value="{carro_to_edit['marca']}"><br>
            Previsão de Chegada (Ano): <input type="text" name="ano" value="{carro_to_edit['ano']}"><br>
            Quantidade Disponível: <input type="number" name="quantidade_disponivel" value="{carro_to_edit['quantidade_disponivel']}" required><br>
            Valor (Preço Diário): <input type="number" step="0.01" name="preco_diaria" value="{carro_to_edit['preco_diaria']}" required><br>
            Observações: <textarea name="observacoes">{carro_to_edit['observacoes']}</textarea><br>
            Max Reservas por Usuário: <input type="number" name="max_reservas" value="{carro_to_edit['max_reservas']}" required><br>
            <input type="submit" value="Salvar Alterações">
        </form>
        <a href="/admin">Voltar para Admin</a>
    </body>
    </html>
    ''')

@app.route('/admin/delete_carro/<int:carro_id>')
def delete_carro(carro_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    global carros
    carros = [c for c in carros if c['id'] != carro_id]
    sync_to_sheets()
    return redirect(url_for('admin'))

# Rotas de CRUD para Usuários (simplificadas)
@app.route('/admin/add_usuario', methods=['GET', 'POST'])
def add_usuario():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        novo_usuario = {
            'id': max([u['id'] for u in usuarios] + [0]) + 1,
            'nome': request.form['nome'],
            'email': request.form['email'],
            'senha_hash': hashlib.md5(request.form['senha'].encode()).hexdigest(),
            'cpf': request.form['cpf'],
            'telefone': request.form['telefone'],
            'data_cadastro': datetime.now().strftime('%Y-%m-%d'),
            'admin': request.form.get('admin') == 'on'
        }
        usuarios.append(novo_usuario)
        sync_to_sheets()
        return redirect(url_for('admin'))
    return render_template_string('''
    <html>
    <head><title>Adicionar Usuário</title></head>
    <body>
        <h1>Adicionar Novo Usuário</h1>
        <form method="post">
            Nome: <input type="text" name="nome" required><br>
            Email: <input type="email" name="email" required><br>
            Senha: <input type="password" name="senha" required><br>
            CPF: <input type="text" name="cpf"><br>
            Telefone: <input type="text" name="telefone"><br>
            Admin: <input type="checkbox" name="admin"><br>
            <input type="submit" value="Adicionar Usuário">
        </form>
        <a href="/admin">Voltar para Admin</a>
    </body>
    </html>
    ''')

@app.route('/admin/edit_usuario/<int:usuario_id>', methods=['GET', 'POST'])
def edit_usuario(usuario_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    usuario_to_edit = next((u for u in usuarios if u['id'] == usuario_id), None)
    if not usuario_to_edit:
        return "Usuário não encontrado", 404

    if request.method == 'POST':
        usuario_to_edit['nome'] = request.form['nome']
        usuario_to_edit['email'] = request.form['email']
        if request.form['senha']: # Atualiza senha apenas se fornecida
            usuario_to_edit['senha_hash'] = hashlib.md5(request.form['senha'].encode()).hexdigest()
        usuario_to_edit['cpf'] = request.form['cpf']
        usuario_to_edit['telefone'] = request.form['telefone']
        usuario_to_edit['admin'] = request.form.get('admin') == 'on'
        sync_to_sheets()
        return redirect(url_for('admin'))
    
    return render_template_string(f'''
    <html>
    <head><title>Editar Usuário</title></head>
    <body>
        <h1>Editar Usuário {usuario_to_edit['nome']}</h1>
        <form method="post">
            Nome: <input type="text" name="nome" value="{usuario_to_edit['nome']}" required><br>
            Email: <input type="email" name="email" value="{usuario_to_edit['email']}" required><br>
            Senha (deixe em branco para não alterar): <input type="password" name="senha"><br>
            CPF: <input type="text" name="cpf" value="{usuario_to_edit['cpf']}"><br>
            Telefone: <input type="text" name="telefone" value="{usuario_to_edit['telefone']}"><br>
            Admin: <input type="checkbox" name="admin" {'checked' if usuario_to_edit['admin'] else ''}><br>
            <input type="submit" value="Salvar Alterações">
        </form>
        <a href="/admin">Voltar para Admin</a>
    </body>
    </html>
    ''')

@app.route('/admin/delete_usuario/<int:usuario_id>')
def delete_usuario(usuario_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    global usuarios
    usuarios = [u for u in usuarios if u['id'] != usuario_id]
    sync_to_sheets()
    return redirect(url_for('admin'))

# Rotas de CRUD para Reservas (simplificadas)
@app.route('/admin/add_reserva', methods=['GET', 'POST'])
def add_reserva():
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        nova_reserva = {
            'id': max([r['id'] for r in reservas] + [0]) + 1,
            'usuario_id': int(request.form['usuario_id']),
            'carro_id': int(request.form['carro_id']),
            'data_reserva': request.form['data_reserva'],
            'hora_inicio': request.form['hora_inicio'],
            'hora_fim': request.form['hora_fim'],
            'status': request.form['status'],
            'observacoes': request.form['observacoes']
        }
        reservas.append(nova_reserva)
        sync_to_sheets()
        return redirect(url_for('admin'))
    
    return render_template_string('''
    <html>
    <head><title>Adicionar Reserva</title></head>
    <body>
        <h1>Adicionar Nova Reserva</h1>
        <form method="post">
            ID do Usuário: <input type="number" name="usuario_id" required><br>
            ID do Carro: <input type="number" name="carro_id" required><br>
            Data da Reserva: <input type="date" name="data_reserva" required><br>
            Hora Início: <input type="time" name="hora_inicio" required><br>
            Hora Fim: <input type="time" name="hora_fim" required><br>
            Status: <input type="text" name="status" value="pendente"><br>
            Observações: <textarea name="observacoes"></textarea><br>
            <input type="submit" value="Adicionar Reserva">
        </form>
        <a href="/admin">Voltar para Admin</a>
    </body>
    </html>
    ''')

@app.route('/admin/edit_reserva/<int:reserva_id>', methods=['GET', 'POST'])
def edit_reserva(reserva_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    reserva_to_edit = next((r for r in reservas if r['id'] == reserva_id), None)
    if not reserva_to_edit:
        return "Reserva não encontrada", 404

    if request.method == 'POST':
        reserva_to_edit['usuario_id'] = int(request.form['usuario_id'])
        reserva_to_edit['carro_id'] = int(request.form['carro_id'])
        reserva_to_edit['data_reserva'] = request.form['data_reserva']
        reserva_to_edit['hora_inicio'] = request.form['hora_inicio']
        reserva_to_edit['hora_fim'] = request.form['hora_fim']
        reserva_to_edit['status'] = request.form['status']
        reserva_to_edit['observacoes'] = request.form['observacoes']
        sync_to_sheets()
        return redirect(url_for('admin'))
    
    return render_template_string(f'''
    <html>
    <head><title>Editar Reserva</title></head>
    <body>
        <h1>Editar Reserva {reserva_to_edit['id']}</h1>
        <form method="post">
            ID do Usuário: <input type="number" name="usuario_id" value="{reserva_to_edit['usuario_id']}" required><br>
            ID do Carro: <input type="number" name="carro_id" value="{reserva_to_edit['carro_id']}" required><br>
            Data da Reserva: <input type="date" name="data_reserva" value="{reserva_to_edit['data_reserva']}" required><br>
            Hora Início: <input type="time" name="hora_inicio" value="{reserva_to_edit['hora_inicio']}" required><br>
            Hora Fim: <input type="time" name="hora_fim" value="{reserva_to_edit['hora_fim']}" required><br>
            Status: <input type="text" name="status" value="{reserva_to_edit['status']}"><br>
            Observações: <textarea name="observacoes">{reserva_to_edit['observacoes']}</textarea><br>
            <input type="submit" value="Salvar Alterações">
        </form>
        <a href="/admin">Voltar para Admin</a>
    </body>
    </html>
    ''')

@app.route('/admin/delete_reserva/<int:reserva_id>')
def delete_reserva(reserva_id):
    if not session.get('logged_in') or not session.get('is_admin'):
        return redirect(url_for('login'))
    
    global reservas
    reservas = [r for r in reservas if r['id'] != reserva_id]
    sync_to_sheets()
    return redirect(url_for('admin'))

# Rota de Logout
@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('user_id', None)
    session.pop('is_admin', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
