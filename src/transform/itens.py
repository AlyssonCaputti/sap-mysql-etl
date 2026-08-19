"""Tratamento de itens.

A exportação tem ~475 colunas e o servidor só aguenta ~196 por tabela, então
divido numa tabela principal + N de extras. A view ItensCompleto junta tudo de
volta (src/load/views.py). Isso é efeito de tudo ser LONGTEXT — com tipo certo
provavelmente nem precisaria dividir.
"""

import pandas as pd

from config.settings import COLUNAS_POR_LOTE_ITENS

# Onde cada nome técnico do SAP fica. Uso quando a origem manda o cabeçalho em
# português (23/07/2026) — a ordem das colunas é a mesma nos dois casos.
POSICOES_TECNICAS = {
    0: "ItemCode",
    1: "ItemName",
    3: "ItmsGrpCod",
    53: "LastPurPrc",
    54: "LastPurCur",
    379: "U_SX_Serie",
    380: "U_SX_Marca",
    382: "U_SX_IndiceVelocidade",
    383: "U_SX_IndiceCarga",
    385: "U_SX_Secao",
    386: "U_SX_Aro",
    387: "U_SX_Modelo",
    406: "U_SX_Tradwear",
    407: "U_SX_Tracao",
    408: "U_SX_Temperatura",
    409: "U_SX_DiamInterno",
    439: "U_U_GPNicho",
    451: "U_UnidadedeNegocio",
    455: "Ativo",
}

MAPA_COLUNAS = {
    "ItemCode": "numero_do_item",
    "ItemName": "descricao_do_item",
    "U_SX_Modelo": "modelo",
    "U_SX_Secao": "secao",
    "U_SX_Aro": "aro",
    "U_SX_Serie": "serie",
    "U_SX_Marca": "marca",
    "ItmsGrpCod": "em_estoque",
    "U_SX_DiamInterno": "diametro_interno",
    "U_SX_Temperatura": "temperatura",
    "U_SX_Tracao": "tracao",
    "U_SX_Tradwear": "tradwear",
    "U_SX_IndiceCarga": "indice_de_carga",
    "U_SX_IndiceVelocidade": "indice_velocidade",
    "Ativo": "ativo",
    "U_U_GPNicho": "nicho_1",
    "U_UnidadedeNegocio": "unidade_de_negocio",
    "LastPurPrc": "preco_ultima_compra",
    "LastPurCur": "moeda",
    "AWSBA001": "awsba001",
    "AWSDF001": "awsdf001",
    "AWSES001": "awses001",
    "AWSGO001": "awsgo001",
    "AWSMG001": "awsmg001",
    "AWSMS001": "awsms001",
    "AWSMT001": "awsmt001",
    "AWSPR001": "awspr001",
    "AWSRJ010": "awsrj010",
    "AWSRS001": "awsrs001",
    "AWSSC001": "awssc001",
    "AWSSJRP1": "awssjrp1",
    "AWSSP001": "awssp001",
    "AWSUB001": "awssub001",
    "AWSNAC01": "awsnac01",
    "IMP-001": "imp_001",
}

CHAVE = "ItemCode"
CHAVE_DESTINO = "numero_do_item"


def restaurar_header_tecnico(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Repoe nomes tecnicos por posicao quando o header veio em portugues."""
    if CHAVE in df.columns:
        return df, []

    fora = [p for p in POSICOES_TECNICAS if p >= len(df.columns)]
    if fora:
        raise ValueError(
            f"O layout de itens mudou: as posições {fora} não existem num "
            f"arquivo de {len(df.columns)} colunas. Confira a exportação."
        )

    df = df.copy()
    colunas = list(df.columns)
    for posicao, tecnico in POSICOES_TECNICAS.items():
        colunas[posicao] = tecnico
    df.columns = colunas

    return df, ["Sem ItemCode no cabeçalho — usei os nomes técnicos por posição."]


def transformar_principal(df: pd.DataFrame) -> pd.DataFrame:
    """Tabela principal: só as colunas do mapa, já renomeadas."""
    ausentes = [c for c in MAPA_COLUNAS if c not in df.columns]
    if ausentes:
        raise ValueError(
            f"Faltam colunas em itens: {ausentes}. "
            f"O layout da exportação deve ter mudado."
        )
    return df[list(MAPA_COLUNAS)].rename(columns=MAPA_COLUNAS)


def fatiar_extras(df: pd.DataFrame) -> list[pd.DataFrame]:
    """Divide o que sobrou em pedaços, cada um levando a chave junto."""
    extras = [c for c in df.columns if c not in MAPA_COLUNAS]
    chave = df[[CHAVE]].rename(columns={CHAVE: CHAVE_DESTINO})

    lotes = []
    for inicio in range(0, len(extras), COLUNAS_POR_LOTE_ITENS):
        colunas = extras[inicio : inicio + COLUNAS_POR_LOTE_ITENS]
        lotes.append(pd.concat([chave, df[colunas]], axis=1))
    return lotes


def transformar(df: pd.DataFrame) -> tuple[pd.DataFrame, list[pd.DataFrame], list[str]]:
    """Devolve (principal, lotes_extras, avisos)."""
    df, avisos = restaurar_header_tecnico(df)
    return transformar_principal(df), fatiar_extras(df), avisos
