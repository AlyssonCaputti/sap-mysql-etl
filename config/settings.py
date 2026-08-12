"""Caminhos e credenciais. Antes isso estava espalhado em 6 arquivos."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent

load_dotenv(RAIZ / ".env")


# ── Banco ────────────────────────────────────────────────────
DB = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "charset": "utf8mb4",
}


def validar_credenciais() -> None:
    """Falha dizendo qual variavel faltou, em vez de erro generico do driver."""
    faltando = [c for c in ("host", "user", "password", "database") if not DB[c]]
    if faltando:
        chaves = ", ".join(f"DB_{c.upper()}" for c in faltando)
        raise RuntimeError(
            f"Credenciais ausentes no .env: {chaves}. "
            f"Copie o .env.example para .env e preencha."
        )


# ── Onde ficam os dados ──────────────────────────────────────
# Uma pasta por entidade, com a exportacao bruta do SAP dentro.
DADOS = Path(os.getenv("ETL_PASTA_DADOS", str(RAIZ / "dados")))

# Saida tratada. O nome de cada subpasta vira o nome da tabela no MySQL.
ENTRADA_VPS = Path(os.getenv("ETL_PASTA_SAIDA", str(DADOS / "para_vps")))
BACKUP = DADOS / "_backup"

ORIGENS = {
    "clientes": DADOS / "clientes" / "clientes_origem.xlsx",
    "vendedores": DADOS / "ilhas_vendedores" / "vendedores.xlsx",
    "faturamento": DADOS / "faturamento" / "faturamento_origem.csv",
    "itens": DADOS / "itens" / "itens_origem.csv",
    "preco_revenda": DADOS / "preco_revenda" / "precos.csv",
    "imagem_url": DADOS / "imagem_url",  # nome do arquivo varia
}

SAIDAS = {
    "clientes": ENTRADA_VPS / "Clientes" / "clientes_origem_tratado.csv",
    "faturamento": ENTRADA_VPS / "Faturamento" / "faturamento_tratado.csv",
    "itens": ENTRADA_VPS / "Itens" / "itens_origem_tratado.csv",
}

# Planilha com os nomes tecnicos do SAP. Só uso quando a origem vem com
# cabeçalho em português. O caminho fica no .env,
# porque é um arquivo de referência que cada máquina guarda onde quiser.
REFERENCIA_TECNICA_CLIENTES = Path(
    os.getenv("ETL_REF_CLIENTES", str(DADOS / "referencia" / "clientes_origem.xlsx"))
)

PASTA_LOGS = RAIZ / "logs"


# ── Carga ────────────────────────────────────────────────────
# Cada lote é um round-trip até a VPS. Com 500, faturamento faz 511 idas e
# vindas; com 5000, só 52. Se o servidor reclamar de "Packet too large",
# baixe para 2000.
TAMANHO_LOTE = int(os.getenv("ETL_TAMANHO_LOTE", "5000"))

# O servidor aguenta ~196 colunas LONGTEXT por tabela e itens tem ~475, então
# fatio em pedaços. Ver src/transform/itens.py.
COLUNAS_POR_LOTE_ITENS = 150

CSV_SAIDA = {"sep": ";", "encoding": "utf-8", "index": False}
