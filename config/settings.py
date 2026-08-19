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

# Pasta de rede onde o ERP publica as exportacoes. Sem padrao de proposito:
# o caminho e da instalacao, entao vem do .env.
INTEGRACAO = Path(os.getenv("ETL_PASTA_INTEGRACAO", str(RAIZ / "integracao")))

# Saida tratada. O nome de cada subpasta vira o nome da tabela no MySQL.
ENTRADA_VPS = Path(os.getenv("ETL_PASTA_SAIDA", str(DADOS / "para_vps")))
BACKUP = DADOS / "_backup"

# O custo le direto da rede, e nao de ENTRADA_VPS como as outras entidades.
# Dois motivos: a origem sobrescreve o arquivo no lugar (o pipeline nao faz
# backup dele), e a pasta de rede e compartilhada com o ETL antigo — apontar
# ENTRADA_VPS pra la faria o upload.py varrer todas as pastas e MOVER arquivos
# que o antigo ainda usa. Enquanto os dois convivem, so este caminho vai pra rede.
PASTA_SKU_CUSTO = Path(
    os.getenv("ETL_PASTA_SKU_CUSTO", str(INTEGRACAO / "sku_custo"))
)

# O faturamento le direto da rede porque a origem o regera de hora em hora —
# copiar pra ca antes so criaria uma versao desatualizada no meio do caminho.
# As outras entidades sao diarias e continuam em dados/.
# O nome do arquivo que o ERP publica varia por instalação, então cada um pode
# ser trocado no .env sem mexer aqui.
ORIGENS = {
    "clientes": DADOS / "clientes" / os.getenv("ETL_ARQ_CLIENTES", "clientes.xlsx"),
    "vendedores": DADOS
    / "ilhas_vendedores"
    / os.getenv("ETL_ARQ_VENDEDORES", "vendedores.xlsx"),
    "faturamento": INTEGRACAO
    / os.getenv("ETL_SUBPASTA_FATURAMENTO", "faturamento")
    / os.getenv("ETL_ARQ_FATURAMENTO", "faturamento.csv"),
    "itens": DADOS / "itens" / os.getenv("ETL_ARQ_ITENS", "itens.csv"),
    "preco_revenda": DADOS / "preco_revenda" / "preco_revenda.csv",
    "imagem_url": DADOS / "imagem_url",  # nome do arquivo varia
}

SAIDAS = {
    "clientes": ENTRADA_VPS / "Clientes" / "clientes_tratado.csv",
    "faturamento": ENTRADA_VPS / "Faturamento" / "faturamento_tratado.csv",
    "itens": ENTRADA_VPS / "Itens" / "itens_tratado.csv",
}

# Planilha com os nomes tecnicos do SAP. Só uso quando a origem vem com
# cabecalho em portugues (aconteceu em 01/07/2026). O caminho fica no .env,
# porque é um arquivo de referência que cada máquina guarda onde quiser.
REFERENCIA_TECNICA_CLIENTES = Path(
    os.getenv("ETL_REF_CLIENTES", str(DADOS / "referencia" / "clientes.xlsx"))
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

# O que o upload e o sku_custo consideram arquivo de dado.
EXTENSOES_DADOS = (".csv", ".xlsx", ".xlsm")
