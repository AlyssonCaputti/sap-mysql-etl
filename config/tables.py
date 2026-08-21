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
    # truncate porque o schema foi ajustado à mão e o replace recria do zero.
    # Duas chaves: a pasta local tem nome comprido, a da rede é só "vendedores".
    "vendedores ilha growth": {"estrategia": "truncate"},
    "vendedores": {"estrategia": "truncate"},
    "imagem_url": {"estrategia": "replace"},
    # O arquivo da rede é a base completa, então espelho ele.
    "chamados_garantia": {"estrategia": "replace"},
    "csat_garantia": {"estrategia": "replace"},
    "fabrica_garantia": {"estrategia": "replace"},
    # As de baixo vieram das pastas de atualizacao manual. replace faz a tabela
    # espelhar o arquivo, que é a base completa em todas elas.
    "alcance": {"estrategia": "replace"},
    "custo_geral_marketing": {"estrategia": "replace"},
    "custo_marketing": {"estrategia": "replace"},
    "custo_no_detalhe": {"estrategia": "replace"},
    "leads": {"estrategia": "replace"},
    "lista_promocional_julho": {"estrategia": "replace"},
    "preco_revenda": {"estrategia": "replace"},
    # truncate aqui: a sql_fator_uf tem `id` bigint auto_increment como chave,
    # e o replace dropa a tabela e recriaria tudo LONGTEXT sem a chave.
    "sql_fator_uf": {"estrategia": "truncate"},
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


# ── Bases de atualizacao manual ──────────────────────────────
# Pastas em dados-att-manualmente que o preparar_manuais() copia pro para_vps.
# A chave é o nome da pasta na rede; o valor, a tabela que JÁ existe no banco.
#
# Declaro o destino explicitamente em vez de deixar o nome_tabela() adivinhar:
# ele geraria `Leads` e `Alcance`, tabelas novas ao lado das que os dashboards
# já leem. Pasta que não está aqui não sobe — de propósito.
PASTA_MANUAL_PARA_TABELA = {
    "alcance": "alcance",
    "chamados_garantia": "chamados_garantia",
    "csat_garantia": "csat_garantia",
    "custo_geral_marketing": "custo_geral_marketing",
    "custo_marketing": "custo_marketing",
    "custo_no_detalhe": "custo_no_detalhe",
    "fabrica_garantia": "fabrica_garantia",
    "imagem_url": "ImagemUrl",
    "leads": "leads",
    "lista_promocional_julho": "ListaPromocionalJulho",
    "preco_revenda": "calculo_preco_revenda",
    "sql_fator_uf": "sql_fator_uf",
    "vendedores": "Vendedores",
}


# ── Tipos e indices ──────────────────────────────────────────
# Coluna sem tipo aqui continua LONGTEXT. Declaro por tabela pra poder migrar
# uma por vez em vez de arriscar as 13 de uma vez.
#
# Vocabulario disponivel em src/io/database.py:_TIPOS_SQL — declaro apelido
# ("data", "dinheiro"), nunca SQL cru.
#
# Todo indice exige tipo declarado na coluna: LONGTEXT nao indexa sem prefixo,
# e o criar_tabela levanta se eu esquecer.
TIPOS = {
    "faturamento": {
        "emissao": "datahora",
        "cod_cliente": "codigo",
        "cod_item": "codigo",
        "valor_total": "dinheiro",
    },
    "clientes": {
        "codigo_do_pn": "codigo",
        "cnpj_cpf": "codigo",
        "estado": "texto_curto",
    },
    "sku_custo_cd_giba": {
        "sku": "codigo",
        "codigo_deposito": "texto_curto",
        "custo_medio": "dinheiro",
        "em_estoque": "decimal",
        "pedido": "decimal",
    },
}

# Um indice por padrao de consulta, nao um por coluna: indice a mais custa
# escrita e espaco em toda carga.
INDICES = {
    "faturamento": [["emissao"], ["cod_cliente"], ["cod_item"]],
    "clientes": [["codigo_do_pn"]],
    "sku_custo_cd_giba": [["sku"], ["codigo_deposito"]],
}


def config_da_pasta(chave: str) -> dict:
    """Junta estrategia, contrato, tipos e indices de uma pasta num dict.

    Existe pra quem carrega nao precisar consultar quatro dicionarios e
    lembrar de todos.
    """
    cfg = dict(ESTRATEGIAS.get(chave, {}))
    cfg.setdefault("estrategia", ESTRATEGIA_PADRAO)
    if chave in TIPOS:
        cfg["tipos"] = TIPOS[chave]
    if chave in INDICES:
        cfg["indices"] = INDICES[chave]
    return cfg
