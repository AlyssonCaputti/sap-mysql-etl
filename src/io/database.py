"""Conexão com o MySQL, criação de tabela e insert em lote."""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager

import mysql.connector
import pandas as pd

from config.settings import DB, TAMANHO_LOTE, validar_credenciais

log = logging.getLogger(__name__)

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


# Vocabulário fechado de tipos. O config declara o apelido, nunca o SQL —
# assim nada que venha de fora entra no CREATE.
#
# Datas e valores ficam NULL quando o dado não converte, em vez de derrubar a
# carga: as portas de qualidade já barram arquivo ruim antes daqui.
_TIPOS_SQL = {
    "data": "DATE NULL",
    "datahora": "DATETIME NULL",
    "dinheiro": "DECIMAL(15,2) NULL",
    "inteiro": "BIGINT NULL",
    "decimal": "DECIMAL(18,6) NULL",
    "texto": "VARCHAR(255) NULL",
    "texto_curto": "VARCHAR(64) NULL",
    "codigo": "VARCHAR(64) NULL",
}


def tabela_existe(cursor, tabela: str) -> bool:
    # Aqui o nome vai como valor, então dá pra parametrizar de verdade.
    cursor.execute("SHOW TABLES LIKE %s", (tabela,))
    return cursor.fetchone() is not None


def criar_tabela(
    cursor,
    tabela: str,
    colunas: list[str],
    tipos: dict[str, str] | None = None,
    indices: list[list[str]] | None = None,
) -> None:
    """Cria a tabela. LONGTEXT no que não tiver tipo declarado.

    LONGTEXT em tudo era a dívida antiga: mata índice e obriga CAST em quem
    consulta. Quem declara `tipos` em config/tables.py sai dela; quem não
    declara continua igual, então dá pra migrar tabela por tabela.

    Os índices vêm junto do CREATE porque o `replace` dropa e recria — índice
    feito à mão se perderia na carga seguinte.
    """
    validar_identificador(tabela)
    for coluna in colunas:
        validar_identificador(coluna)

    # Tipo de coluna que não veio é ignorado, não erro: a origem já tirou
    # coluna sem avisar (Inadimplente, 23/07/2026) e isso não pode derrubar a
    # carga inteira. O índice abaixo é que é exigente.
    tipos = {c: t for c, t in (tipos or {}).items() if c in colunas}

    # Ajuda o CREATE a passar quando tem muita coluna LONGTEXT, mas exige
    # privilégio que nem todo usuário tem — e na maioria das vezes o CREATE
    # funciona sem. Então tento e sigo; se realmente faltar espaço de linha, o
    # próprio CREATE abaixo reclama com a mensagem certa.
    try:
        cursor.execute("SET SESSION innodb_strict_mode = OFF")
    except mysql.connector.Error:
        pass

    partes = [f"`{c}` {_TIPOS_SQL.get(tipos.get(c, ''), 'LONGTEXT')}" for c in colunas]

    for n, cols in enumerate(indices or [], 1):
        for c in cols:
            validar_identificador(c)

        # Índice só entra se TODAS as colunas dele vieram e são tipadas.
        # LONGTEXT não indexa sem prefixo, e coluna ausente não é motivo pra
        # perder a carga — sigo sem o índice e aviso.
        faltando = [c for c in cols if c not in colunas]
        sem_tipo = [c for c in cols if c in colunas and not tipos.get(c)]
        if faltando or sem_tipo:
            log.warning(
                "  índice em `%s` (%s) ignorado: %s",
                tabela,
                ", ".join(cols),
                f"coluna ausente {faltando}" if faltando else f"sem tipo {sem_tipo}",
            )
            continue

        alvo = ", ".join(f"`{c}`" for c in cols)
        # Nome vem das colunas, não da tabela: o replace cria o índice na
        # `__nova` e depois renomeia, então `ix_<tabela>` ficaria com o sufixo
        # da temporária grudado. Corto em 64, que é o limite do MySQL.
        nome_indice = f"ix_{'_'.join(cols)}"[:64]
        partes.append(f"KEY `{nome_indice}` ({alvo})")

    defs = ",\n    ".join(partes)
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
