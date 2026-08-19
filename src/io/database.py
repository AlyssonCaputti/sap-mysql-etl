"""Conexão com o MySQL, criação de tabela e insert em lote."""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import mysql.connector
import pandas as pd

from config.settings import DB, TAMANHO_LOTE, validar_credenciais

# Nome de tabela e coluna não pode ir como parâmetro no MySQL, e esses nomes
# vêm de nome de pasta e cabeçalho de arquivo. Então valido o formato antes de
# interpolar. Valor sempre vai por %s.
_IDENTIFICADOR_VALIDO = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def validar_identificador(nome: str) -> str:
    if not _IDENTIFICADOR_VALIDO.match(nome or ""):
        raise ValueError(
            f"Identificador inválido pra SQL: {nome!r}. "
            f"Precisa começar com letra ou _, e ter até 64 caracteres."
        )
    return nome


def conectar() -> "mysql.connector.MySQLConnection":
    validar_credenciais()
    return mysql.connector.connect(**DB)


@contextmanager
def conexao() -> Iterator["mysql.connector.MySQLConnection"]:
    """Fecha a conexão mesmo se der erro no meio."""
    con = conectar()
    try:
        yield con
    finally:
        con.close()


def tabela_existe(cursor, tabela: str) -> bool:
    # Aqui o nome vai como valor, então dá pra parametrizar de verdade.
    cursor.execute("SHOW TABLES LIKE %s", (tabela,))
    return cursor.fetchone() is not None


def criar_tabela(cursor, tabela: str, colunas: list[str]) -> None:
    """Cria tudo como LONGTEXT.

    É dívida técnica conhecida — mata índice e obriga CAST em quem consulta.
    Mas mudar afeta quem consome, então fica como decisão à parte.
    Ver a secao 6 de .claude/relatorios/RELATORIO.md.
    """
    validar_identificador(tabela)
    for coluna in colunas:
        validar_identificador(coluna)

    # Ajuda o CREATE a passar quando tem muita coluna LONGTEXT, mas exige
    # privilégio que nem todo usuário tem — e na maioria das vezes o CREATE
    # funciona sem. Então tento e sigo; se realmente faltar espaço de linha, o
    # próprio CREATE abaixo reclama com a mensagem certa.
    try:
        cursor.execute("SET SESSION innodb_strict_mode = OFF")
    except mysql.connector.Error:
        pass

    defs = ",\n    ".join(f"`{c}` LONGTEXT" for c in colunas)
    cursor.execute(
        f"CREATE TABLE IF NOT EXISTS `{tabela}` (\n    {defs}\n) "
        f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC"
    )


def inserir_em_lote(cursor, tabela: str, df: pd.DataFrame, ao_progredir=None) -> int:
    """Insere em lotes de TAMANHO_LOTE e devolve o total."""
    validar_identificador(tabela)
    for coluna in df.columns:
        validar_identificador(coluna)

    colunas = ", ".join(f"`{c}`" for c in df.columns)
    marcadores = ", ".join(["%s"] * len(df.columns))
    sql = f"INSERT INTO `{tabela}` ({colunas}) VALUES ({marcadores})"

    # Célula vazia vira NaN no pandas e o driver mandava o texto "nan" pro
    # banco, quebrando com "Unknown column 'nan'". Troco por None.
    df = df.astype(object).where(pd.notna(df), None)

    total = len(df)
    inseridas = 0
    for inicio in range(0, total, TAMANHO_LOTE):
        lote = df.iloc[inicio : inicio + TAMANHO_LOTE]
        cursor.executemany(sql, lote.values.tolist())
        inseridas += len(lote)
        if ao_progredir:
            ao_progredir(inseridas, total)

    return total
