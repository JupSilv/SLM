# database.py
import sqlite3
import json
from datetime import datetime

DB_PATH = "deskubuddy.db"


def _connect():
    return sqlite3.connect(DB_PATH, timeout=10)


def _ensure_learning_columns(cursor):
    columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(auditoria_sinais)")
    }
    new_columns = {
        "dados_analise": "TEXT",
        "decisao": "TEXT",
        "motivo_decisao": "TEXT",
        "motivo_execucao": "TEXT",
        "order_id": "TEXT",
        "origem_resultado": "TEXT",
    }
    for name, column_type in new_columns.items():
        if name not in columns:
            cursor.execute(
                f"ALTER TABLE auditoria_sinais ADD COLUMN {name} {column_type}"
            )


def iniciar_banco():
    """Inicializa as tabelas de auditoria aplicando a restrição UNIQUE para evitar duplicados."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auditoria_sinais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ativo TEXT,
            direcao TEXT,
            payout REAL,
            setup TEXT,
            score REAL,
            status_execucao TEXT,
            insight_slm TEXT,
            UNIQUE(timestamp, ativo)
        )
    ''')
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risk_daily_state (
            session_date TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risk_session_state (
            session_date TEXT NOT NULL,
            account_type TEXT NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_date, account_type)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_type_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_type TEXT NOT NULL,
            switched_at TEXT NOT NULL,
            notes TEXT
        )
    """)
    _ensure_learning_columns(cursor)
    conn.commit()
    conn.close()


def registrar_tipo_conta(account_type):
    """Registra a mudança de tipo de conta (PRACTICE -> REAL ou vice-versa)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO account_type_registry (account_type, switched_at, notes)
            VALUES (?, ?, ?)
            """,
            (
                account_type.upper(),
                datetime.now().isoformat(timespec="seconds"),
                f"Mudança para {account_type}",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def obter_ultimo_tipo_conta():
    """Obtém o último tipo de conta registrado."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT account_type FROM account_type_registry ORDER BY switched_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def limpar_estado_risco_diario():
    """Limpa o estado de risco diário quando houver mudança de conta (DEMO <-> REAL)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM risk_daily_state")
        conn.commit()
        return True
    except Exception as e:
        print(f"Erro ao limpar estado de risco: {e}")
        return False
    finally:
        conn.close()



def carregar_estado_risco(session_date, account_type=None):
    conn = _connect()
    try:
        cursor = conn.cursor()
        if account_type is None:
            cursor.execute(
                "SELECT state_json FROM risk_daily_state WHERE session_date = ?",
                (session_date,),
            )
        else:
            account_type = account_type.upper()
            cursor.execute(
                """
                SELECT state_json FROM risk_session_state
                WHERE session_date = ? AND account_type = ?
                """,
                (session_date, account_type),
            )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        if account_type is None:
            return None

        cursor.execute(
            "SELECT state_json FROM risk_daily_state WHERE session_date = ?",
            (session_date,),
        )
        legacy_row = cursor.fetchone()
        if legacy_row is None:
            return None

        cursor.execute(
            "SELECT account_type FROM account_type_registry "
            "ORDER BY switched_at DESC, id DESC LIMIT 1"
        )
        registry_row = cursor.fetchone()
        if registry_row is None or registry_row[0].upper() != account_type:
            raise RuntimeError(
                "Estado legado de risco pertence a uma conta não confirmada; "
                "reconciliação manual necessária."
            )

        estado = json.loads(legacy_row[0])
        estado["account_type"] = account_type
        cursor.execute(
            """
            INSERT INTO risk_session_state
                (session_date, account_type, state_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                session_date,
                account_type,
                json.dumps(estado, ensure_ascii=False, sort_keys=True),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return estado
    finally:
        conn.close()


def salvar_estado_risco(session_date, state, account_type=None):
    account_type = account_type or state.get("account_type")
    conn = _connect()
    try:
        cursor = conn.cursor()
        if account_type is None:
            cursor.execute(
                """
                INSERT INTO risk_daily_state (session_date, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_date) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_date,
                    json.dumps(state, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        else:
            state = dict(state)
            state["account_type"] = account_type.upper()
            cursor.execute(
                """
                INSERT INTO risk_session_state
                    (session_date, account_type, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_date, account_type) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_date,
                    account_type.upper(),
                    json.dumps(state, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def registrar_sinal_db(ativo, direcao, payout, setup, score=85.0, status="ANALISADO", insight_slm=""):
    """Momento 1: Registra um sinal inédito de trading no banco de forma segura contra duplicidades."""
    conn = _connect()
    cursor = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:00")
    try:
        cursor.execute('''
            INSERT INTO auditoria_sinais (timestamp, ativo, direcao, payout, setup, score, status_execucao, insight_slm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (timestamp, ativo, direcao, payout, setup, score, status, insight_slm))
        conn.commit()
        print(f"[SQLite] Novo sinal registrado para {ativo} no minuto {timestamp}.")
    except sqlite3.IntegrityError:
        print(f"[Proteção SQLite] Tentativa de inserção duplicada bloqueada para {ativo} em {timestamp}.")
    finally:
        conn.close()
    return timestamp

def registrar_candidato_db(ativo, direcao, payout, setup, dados_analise):
    timestamp = datetime.now().isoformat(timespec="microseconds")
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO auditoria_sinais
                (timestamp, ativo, direcao, payout, setup, status_execucao, dados_analise)
            VALUES (?, ?, ?, ?, ?, 'CANDIDATO', ?)
        ''', (
            timestamp,
            ativo,
            direcao,
            payout,
            setup,
            json.dumps(dados_analise, ensure_ascii=False, sort_keys=True),
        ))
        conn.commit()
        return timestamp
    finally:
        conn.close()


def atualizar_decisao_sinal(timestamp, ativo, decisao, motivo):
    status = "PULADO" if decisao.startswith("PULAR") else "AGUARDANDO_ORDEM"
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE auditoria_sinais
            SET decisao = ?, motivo_decisao = ?, status_execucao = ?
            WHERE timestamp = ? AND ativo = ?
        ''', (decisao, motivo, status, timestamp, ativo))
        conn.commit()
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Registro do sinal não encontrado ao salvar decisão: {ativo} {timestamp}"
            )
    finally:
        conn.close()


def atualizar_status_ordem_db(timestamp, ativo, status, order_id=None, motivo=None):
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE auditoria_sinais
            SET status_execucao = ?, order_id = ?, motivo_execucao = ?
            WHERE timestamp = ? AND ativo = ?
        ''', (status, str(order_id) if order_id is not None else None, motivo, timestamp, ativo))
        conn.commit()
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Registro do sinal não encontrado ao atualizar ordem: {ativo} {timestamp}"
            )
    finally:
        conn.close()


def obter_historico_comparavel(ativo, direcao, setup, limite=50, origem="TODOS"):
    origem = origem.upper()
    if origem not in ("TODOS", "REAL", "SIMULADO"):
        raise ValueError("origem deve ser 'TODOS', 'REAL' ou 'SIMULADO'.")
    if limite < 1:
        raise ValueError("limite deve ser maior que zero.")

    filtros_resultado = {
        "TODOS": """
            'WIN', 'LOSS', 'DRAW',
            'PAPER_WIN', 'PAPER_LOSS', 'PAPER_DRAW',
            'PAPER_INDETERMINADO'
        """,
        "REAL": "'WIN', 'LOSS', 'DRAW'",
        "SIMULADO": """
            'PAPER_WIN', 'PAPER_LOSS', 'PAPER_DRAW',
            'PAPER_INDETERMINADO'
        """,
    }
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT timestamp, status_execucao, payout, origem_resultado,
                dados_analise, decisao, motivo_decisao, motivo_execucao
            FROM auditoria_sinais
            WHERE ativo = ? AND direcao = ? AND setup = ?
                AND status_execucao IN ({filtros_resultado[origem]})
            ORDER BY id DESC
            LIMIT ?
        ''', (ativo, direcao, setup, limite))
        return [
            {
                "timestamp": row[0],
                "resultado": row[1],
                "payout": row[2],
                "origem": row[3] or (
                    "SIMULADO" if row[1].startswith("PAPER_") else "REAL"
                ),
                "analise": json.loads(row[4]) if row[4] else {},
                "decisao": row[5],
                "motivo_decisao": row[6],
                "motivo_execucao": row[7],
                "motivo": row[6] or row[7],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def atualizar_resultado_sinal(timestamp, ativo, resultado_final, origem=None):
    """Momento 2: Atualiza a linha existente com o veredito real da operação (WIN/LOSS)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE auditoria_sinais
            SET status_execucao = ?, origem_resultado = ?
            WHERE timestamp = ? AND ativo = ?
        ''', (resultado_final.upper(), origem, timestamp, ativo))
        conn.commit()
    finally:
        conn.close()
    print(f"[SQLite] Linha {ativo} ({timestamp}) atualizada com resultado: {resultado_final}")

def obter_metricas_sinais():
    """Retorna métricas gerais e resultados separados por origem."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM auditoria_sinais")
        total = cursor.fetchone()
        total_val = total[0] if total else 0

        cursor.execute(
            "SELECT status_execucao, COUNT(*) FROM auditoria_sinais "
            "GROUP BY status_execucao"
        )
        resultados = dict(cursor.fetchall())

        cursor.execute(
            "SELECT ativo, status_execucao, COUNT(*) FROM auditoria_sinais "
            "GROUP BY ativo, status_execucao"
        )
        desempenho_ativo = cursor.fetchall()

        cursor.execute("""
            SELECT CASE
                    WHEN status_execucao LIKE 'PAPER_%' THEN 'SIMULADO'
                    ELSE 'REAL'
                END AS origem,
                status_execucao, COUNT(*)
            FROM auditoria_sinais
            WHERE status_execucao IN (
                'WIN', 'LOSS', 'DRAW',
                'PAPER_WIN', 'PAPER_LOSS', 'PAPER_DRAW',
                'PAPER_INDETERMINADO'
            )
            GROUP BY origem, status_execucao
        """)
        resultados_por_origem = {"REAL": {}, "SIMULADO": {}}
        for origem, status, quantidade in cursor.fetchall():
            resultados_por_origem[origem][status] = quantidade

        taxas_acerto_por_origem = {}
        for origem, resultados_origem in resultados_por_origem.items():
            decisivos = (
                resultados_origem.get("WIN", 0)
                + resultados_origem.get("LOSS", 0)
                + resultados_origem.get("PAPER_WIN", 0)
                + resultados_origem.get("PAPER_LOSS", 0)
            )
            vitorias = (
                resultados_origem.get("WIN", 0)
                + resultados_origem.get("PAPER_WIN", 0)
            )
            taxas_acerto_por_origem[origem] = (
                vitorias / decisivos if decisivos else None
            )
    finally:
        conn.close()
    
    return {
        "total_amostras": total_val,
        "proporcao_resultados": resultados,
        "matriz_ativo_resultado": desempenho_ativo,
        "resultados_por_origem": resultados_por_origem,
        "taxas_acerto_por_origem": taxas_acerto_por_origem,
    }
def obter_historico_global_recente(limite=30):
    """Busca o histórico geral de operações fechadas (reais e simuladas) para aprendizado macro."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, ativo, direcao, setup, status_execucao, origem_resultado, motivo_decisao
            FROM auditoria_sinais
            WHERE status_execucao IN ('WIN', 'LOSS', 'PAPER_WIN', 'PAPER_LOSS')
            ORDER BY id DESC
            LIMIT ?
        ''', (limite,))
        
        historico = []
        for row in cursor.fetchall():
            historico.append({
                "timestamp": row[0],
                "ativo": row[1],
                "direcao": row[2],
                "setup": row[3],
                "resultado": row[4],
                "origem": row[5] or ("SIMULADO" if "PAPER" in row[4] else "REAL"),
                "motivo_anterior": row[6]
            })
        return historico
    finally:
        conn.close()


def obter_historico_otc_simulado(limite=1000):
    """Retorna resultados simulados recentes para pares OTC."""
    if limite < 1:
        raise ValueError("limite deve ser maior que zero.")

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT timestamp, ativo, status_execucao
            FROM auditoria_sinais
            WHERE ativo LIKE '%-OTC'
                AND status_execucao IN ('PAPER_WIN', 'PAPER_LOSS')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limite,),
        )
        return [
            {"timestamp": row[0], "ativo": row[1], "resultado": row[2]}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()