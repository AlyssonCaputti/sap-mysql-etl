"""Transformacao de faturamento (notas fiscais). Funcoes puras."""

import pandas as pd

from src.quality.contracts import resolver_renomeacao

MAPA_COLUNAS = {
    "Filial": "filial",
    "Pedido": "pedido",
    "NotaSAP": "nota_sap",
    "Emissao": "emissao",
    "CodCliente": "cod_cliente",
    "NomeCliente": "nome_cliente",
    "Nota": "nota",
    "UF": "uf",
    "Cidade": "cidade",
    "CEP": "cep",
    "Vendedor": "vendedor",
    "CodItem": "cod_item",
    "DescricaoItem": "descricao_item",
    "Quantidade": "quantidade",
    "Preco": "preco",
    "ValorTotal": "valor_total",
    "Performance": "performance",
    "Aro": "aro",
    "AnoMesNFe": "ano_mes_n_fe",
    "Linha": "linha",
    "FormadePagamento": "formade_pagamento",
    "PrazoFinalPagamento": "prazo_final_pagamento",
    "ListadePrecos": "listade_precos",
    "ValorPacote": "valor_pacote",
    "Promocao": "promocao",
    "GrupodePrecos": "grupode_precos",
    "LLGrupodePrecos": "ll_grupode_precos",
    "NCM": "ncm",
    "Utilizacao": "utilizacao",
    "Marca": "marca",
    "ICMS": "icms",
    "TAXAICMS": "taxaicms",
    "ST": "st",
    "TAXAST": "taxast",
    "IPI": "ipi",
    "TAXAIPI": "taxaipi",
    "PIS": "pis",
    "TAXAPIS": "taxapis",
    "COFINS": "cofins",
    "TAXACOFINS": "taxacofins",
    "FCP": "fcp",
    "TAXAFCP": "taxafcp",
    "ICMSDEST": "icmsdest",
    "TAXAICMSDEST": "taxaicmsdest",
    "CredPresumido": "cred_presumido",
    "RedBasePisCofins": "red_base_pis_cofins",
    "MC": "mc",
    "TipoDoc": "tipo_doc",
    "Cashback": "cashback",
    "NotaItem": "nota_item",
    "CustoContabil": "custo_contabil",
    "CustoContabilTotal": "custo_contabil_total",
    "DespesasVariaveis": "despesas_variaveis",
    "Frete": "frete",
}

# Grafias que a origem já mandou pra mesma coluna. O "confins" é typo do
# export mesmo, não meu.
APELIDOS = {"red_base_pis_cofins": ["red_base_pis_confins"]}

COLUNAS_NUMERICAS = [
    "quantidade",
    "preco",
    "valor_total",
    "custo_contabil",
    "custo_contabil_total",
    "performance",
    "valor_pacote",
    "icms",
    "taxaicms",
    "st",
    "taxast",
    "ipi",
    "taxaipi",
    "pis",
    "taxapis",
    "cofins",
    "taxacofins",
    "fcp",
    "taxafcp",
    "icmsdest",
    "taxaicmsdest",
    "cred_presumido",
    "red_base_pis_cofins",
    "despesas_variaveis",
    "frete",
    "cashback",
    "mc",
]


def e_formato_brasileiro(serie: pd.Series) -> bool:
    """True se a coluna usa vírgula decimal ('1.399,90'), False se usa ponto."""
    amostra = serie.dropna().astype(str).head(200)
    return bool(amostra.str.contains(",", regex=False).any())


def normalizar_numericas(df: pd.DataFrame) -> pd.DataFrame:
    """Deixa tudo com vírgula decimal e sem separador de milhar.

    A origem alterna entre '1.399,90' e '759.90' de uma exportação pra outra,
    então checo coluna por coluna em vez de chutar.
    """
    df = df.copy()
    for coluna in [c for c in COLUNAS_NUMERICAS if c in df.columns]:
        if e_formato_brasileiro(df[coluna]):
            # tira o separador de milhar e mantém a vírgula
            df[coluna] = df[coluna].astype(str).str.replace(".", "", regex=False)
        else:
            # troca o ponto decimal por vírgula
            df[coluna] = df[coluna].astype(str).str.replace(".", ",", regex=False)
    return df


def transformar(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Trata faturamento de ponta a ponta. Devolve (dataframe, avisos)."""
    renomeacao, ausentes = resolver_renomeacao(df, MAPA_COLUNAS, APELIDOS)

    if ausentes:
        raise KeyError(
            f"Não achei estas colunas no faturamento, nem pelo nome técnico, "
            f"nem pelo tratado, nem por apelido: {ausentes}. "
            f"Vieram essas: {list(df.columns)}"
        )

    df = df.rename(columns=renomeacao)
    df = df[list(MAPA_COLUNAS.values())]
    df = normalizar_numericas(df)

    return df, []
