"""Como cada tabela é carregada e o que espero encontrar nela.

As estratégias:
  replace     apaga a tabela e recria
  truncate    limpa as linhas, mantém o schema
  date_range  apaga só a janela de datas do arquivo e reinsere
  upsert      atualiza quem já existe, insere o resto
"""

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
    "sku_custo_cd_giba": {"estrategia": "replace"},
    "vendedores ilha growth": {"estrategia": "truncate"},
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
    "sku_custo_cd_giba": {
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
