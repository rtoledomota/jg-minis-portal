import sqlite3
import bcrypt
import os

# --- Configurações do Banco de Dados ---
DATABASE_FILE = 'database.db'

# --- Função para HASH de Senha ---
def hash_password(password):
    """Gera um hash bcrypt para a senha fornecida."""
    # bcrypt.hashpw espera bytes para a senha e o salt
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    return hashed.decode('utf-8') # Decodifica para string para armazenar no DB

# --- Usuários Padrão ---
DEFAULT_USERS = [
    {'email': 'admin@jgminis.com.br', 'password': 'admin123', 'name': 'Admin'},
    {'email': 'usuario@example.com', 'password': 'usuario123', 'name': 'Usuário Teste'}
]

# --- Script de Limpeza e Recriação de Usuários ---
def reset_users_database():
    """
    Conecta ao banco de dados, limpa a tabela de usuários,
    e recria os usuários padrão com senhas hasheadas corretamente.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        print(f"Conectado ao banco de dados: {DATABASE_FILE}")

        # 1. Limpa os usuários antigos do banco (exclui a tabela e recria)
        print("Excluindo tabela 'usuario' existente (se houver)...")
        cursor.execute("DROP TABLE IF EXISTS usuario")
        print("Tabela 'usuario' excluída.")

        # 2. Recria a tabela 'usuario'
        print("Recriando tabela 'usuario'...")
        cursor.execute("""
            CREATE TABLE usuario (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                senha TEXT NOT NULL,
                nome TEXT NOT NULL
            )
        """)
        print("Tabela 'usuario' recriada.")

        # 3. Insere os usuários novamente com hash correto
        print("Inserindo usuários padrão com senhas hasheadas...")
        for user_data in DEFAULT_USERS:
            hashed_pw = hash_password(user_data['password'])
            cursor.execute(
                "INSERT INTO usuario (email, senha, nome) VALUES (?, ?, ?)",
                (user_data['email'], hashed_pw, user_data['name'])
            )
            print(f"  - Usuário '{user_data['email']}' inserido.")

        conn.commit()
        print("Operação concluída com sucesso! Usuários padrão recriados.")

    except sqlite3.Error as e:
        print(f"Erro no banco de dados: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexão com o banco de dados fechada.")

# --- Execução do Script ---
if __name__ == "__main__":
    print("Iniciando script de reset de usuários...")
    reset_users_database()
    print("\nVerificação de login (apenas para teste local):")
    
    # Teste de login (apenas para verificar se o hash funciona)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT email, senha FROM usuario WHERE email = ?", ('admin@jgminis.com.br',))
    user_data = cursor.fetchone()
    conn.close()

    if user_data:
        stored_email, stored_hash_str = user_data
        test_password = 'admin123'
        
        # O hash armazenado é uma string, precisa ser encodado para bytes para checkpw
        if bcrypt.checkpw(test_password.encode('utf-8'), stored_hash_str.encode('utf-8')):
            print(f"  - Teste de login para '{stored_email}' com senha '{test_password}': SUCESSO!")
        else:
            print(f"  - Teste de login para '{stored_email}' com senha '{test_password}': FALHA!")
    else:
        print("  - Usuário 'admin@jgminis.com.br' não encontrado para teste.")
