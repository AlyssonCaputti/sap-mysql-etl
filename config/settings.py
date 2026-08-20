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

# Pasta de rede onde o ERP publica as exportacoes.
INTEGRACAO = Path(
    os.getenv(
        "ETL_PASTA_INTEGRACAO",
        r"P:\Marketing\Marketing 2026\Dados - Dashboards\dados integração",
    )
)

# Saida tratada. O nome de cada subpasta vira o nome da tabela no MySQL.
ENTRADA_VPS = Path(os.getenv("ETL_PASTA_SAIDA", str(DADOS / "para_vps")))
BACKUP = DADOS / "_backup"

# O sku_custo le direto da rede, e nao de ENTRADA_VPS como as outras entidades.
# Dois motivos: a origem sobrescreve o arquivo no lugar (o pipeline nao faz
# backup dele), e a pasta de rede e compartilhada com o ETL antigo — apontar
# ENTRADA_VPS pra la faria o upload.py varrer as 25 pastas e MOVER arquivos que
# o antigo ainda usa. Enquanto os dois convivem, so este caminho vai pra rede.
PASTA_SKU_CUSTO = Path(
    os.getenv(
        "ETL_PASTA_SKU_CUSTO",
        str(INTEGRACAO / "Y - Dados que vão para a VPS" / "sku_custo_cd_giba"),
    )
)

# O SAP exporta sozinho na raiz da integracao; o que e atualizado na mao fica
# em dados-att-manualmente.
MANUAIS = INTEGRACAO / "dados-att-manualmente"

_PADRAO_ORIGENS = {
    "clientes": INTEGRACAO / "clientes" / "dataClientesVPS.xlsx",
    "itens": INTEGRACAO / "itens" / "dataItensVPS.csv",
    "faturamento": INTEGRACAO / "Faturamento_RentNFVPS" / "dataRentNFVPS.csv",
    # Na rede a pasta chama "vendedores"; a chave fica como esta porque
    # config/tables.py casa por ela.
    "vendedores": MANUAIS / "vendedores" / "Vendedores.xlsx",
    "preco_revenda": MANUAIS / "preco_revenda" / "preco_revenda.csv",
    "imagem_url": MANUAIS / "imagem_url",  # nome do arquivo varia
}

# Tudo le da rede, que e onde a origem publica. As pastas em dados/ continuam
# servindo de fallback: ETL_ORIGEM_CLIENTES e afins sobrescrevem quando o P:
# esta fora.
ORIGENS = {
    nome: Path(os.getenv(f"ETL_ORIGEM_{nome.upper()}", str(padrao)))
    for nome, padrao in _PADRAO_ORIGENS.items()
}

SAIDAS = {
    "clientes": ENTRADA_VPS / "Clientes" / "dataClientesVPS_tratado.csv",
    "faturamento": ENTRADA_VPS / "Faturamento" / "Base NFs.csv",
    "itens": ENTRADA_VPS / "Itens" / "dataItensVPS_tratado.csv",
}

# Planilha com os nomes tecnicos do SAP. Só uso quando a origem vem com
# cabecalho em portugues (aconteceu em 01/07/2026). O caminho fica no .env,
# porque é um arquivo de referência que cada máquina guarda onde quiser.
REFERENCIA_TECNICA_CLIENTES = Path(
    os.getenv("ETL_REF_CLIENTES", str(DADOS / "referencia" / "dataClientesVPS.xlsx"))
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
