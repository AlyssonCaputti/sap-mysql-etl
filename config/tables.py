"""Como cada tabela é carregada e o que espero encontrar nela.

As estratégias:
  replace     apaga a tabela e recria
  truncate    limpa as linhas, mantém o schema
  date_range  apaga só a janela de datas do arquivo e reinsere
  upsert      atualiza quem já existe, insere o resto
"""

import os

# Só pelo efeito colateral do load_dotenv: sem isto os os.getenv abaixo não
# veriam o .env.
from config import settings  # noqa: F401

# ── Regras de negócio configuráveis ──────────────────────────
# A carteira do cliente vem da última compra dele nesta marca. Fica em
# variável porque é decisão comercial, não do pipeline.
MARCA_FOCO = os.getenv("ETL_MARCA_FOCO", "MARCA_A")

# O campo do ERP que marca se o cliente é da marca foco, e como ele se chama no
# destino. É campo customizado, então o nome varia por instalação.
COLUNA_MARCA_ORIGEM = os.getenv("ETL_COLUNA_MARCA_ORIGEM", "U_MarcaFoco")
COLUNA_MARCA_DESTINO = os.getenv("ETL_COLUNA_MARCA_DESTINO", "marca_foco")

# Prefixo do vendedor -> ilha -> nome da carteira. O prefixo vem antes do
# primeiro "-" no código do vendedor ("I1-FULANO").
ILHAS = {
    "I1": "Ilha 1",
    "I2": "Ilha 2",
    "I3": "Ilha 3",
}
ILHA_PADRAO = "outros"
CARTEIRA_PADRAO = "Outros"

# Pastas cujo nome de tabela no banco divergiu do que o código geraria, e que
# ficam como estão pra não criar tabela nova e deixar a antiga órfã.
# Formato: ETL_NOMES_LEGADOS="pasta-com-acento=NomeNoBanco;outra=OutroNome"
NOMES_LEGADOS = [
    {"pasta": par.split("=")[0].strip().lower(), "tabela": par.split("=")[1].strip()}
    for par in os.getenv("ETL_NOMES_LEGADOS", "").split(";")
    if "=" in par
]


ESTRATEGIAS = {
    # date_range: dá pra subir só um período sem perder o histórico.
    "faturamento": {
        "estrategia": "date_range",
        "coluna_data": "emissao",
        # A origem manda ISO com hora ("2026-08-03 00:00:00") — verificado nas
        # 258.766 linhas do banco em 18/08/2026, onde o formato BR nao casa
        # nenhuma. Antes daqui estava "%d/%m/%Y" e funcionava só pelos
        # fallbacks de _parsear_datas; nao mexa neles nem no COALESCE do
        # DELETE, que é o que cobre origem alternando de formato.
        "formato_data": "%Y-%m-%d %H:%M:%S",
    },
    # upsert: se uma coluna sumir da origem, o valor antigo fica.
    # A coluna Inadimplente sumiu sem aviso em 23/07/2026.
    "clientes": {
        "estrategia": "upsert",
        "chaves": ["codigo_do_pn"],
    },
    # Cadastros completos, sem histórico pra preservar.
    "itens": {"estrategia": "replace"},
    "sku_custo": {"estrategia": "replace"},
    # truncate preserva o schema ajustado na mão. O nome da pasta varia por
    # instalação, então vem do .env — cair no replace padrão derrubaria a
    # tabela e perderia o ajuste.
    os.getenv("ETL_PASTA_VENDEDORES", "vendedores ilha"): {"estrategia": "truncate"},
}

# Pasta sem entrada acima cai aqui — é de propósito, pasta nova funciona sem
# mexer em código. Só lembre que replace derruba a tabela, então índice ou tipo
# ajustado na mão se perde a cada carga.
ESTRATEGIA_PADRAO = "replace"


# ── Contratos de schema ──────────────────────────────────────
# O que mais quebra esse pipeline é a origem mudar de layout — já aconteceu 4
# vezes. Declarando o que espero, o erro sai aqui com nome, e não 3 passos
# adiante como erro do MySQL.
#
# obrigatória faltando → aborta. opcional faltando → segue com aviso.
CONTRATOS = {
    # O Forecast usa essa tabela pra calcular MCB. Sem custo, ele cai num
    # fallback silencioso — por isso tudo aqui é obrigatório.
    "sku_custo": {
        "obrigatorias": {
            "sku",
            "ncm",
            "codigo_deposito",
            "nome_deposito",
            "custo_medio",
            "em_estoque",
            "pedido",
        },
        "opcionais": set(),
    },
}
