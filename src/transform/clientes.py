"""Tratamento de clientes.

Só funções que recebem e devolvem DataFrame. Nada aqui lê arquivo nem toca no
banco — é o que deixa testar sem infraestrutura.
"""

import pandas as pd

from config.tables import COLUNA_MARCA_DESTINO, COLUNA_MARCA_ORIGEM, ILHAS

MAPA_COLUNAS = {
    "CardCode": "codigo_do_pn",
    "CardName": "nome_do_pn",
    "CardFName": "nome_estrangeiro",
    "Address": "endereço",
    "StreetNo": "número",
    "Block": "bairro",
    "City": "cidade",
    "State2": "estado",
    "Country": "pais",
    "ZipCode": "cep",
    "CodGrupoEconomicoTratado": "cod._grupo_economico",
    "U_U_GP_Nome_grupo_economico": "nome_do_grupo_economico",
    "CreditLine": "limite_de_credito",
    "CNPJ_CPF": "cnpj_cpf",
    "E_Mail": "e_mail",
    "CreateDate": "data_de_criacao",
    COLUNA_MARCA_ORIGEM: COLUNA_MARCA_DESTINO,
    "validFor": "ativo",
    "U_sourcepn": "origem_do_pn",
    "PrimeiraCompra": "primeira_compra",
    "UltimaCompra": "ultima_compra",
    "DiasSemCompra": "dias_sem_compra",
    "NomeVendedor": "Vendedor",
    "NomeGrupoCliente": "grupo_cliente",
    "NomeListaPreco": "lista_de_preco",
    "Phone1": "telefone",
    "Phone2": "DDD",
    "Balance": "saldo em conta",
    "U_GL_IdWake": "id_wake",
    "U_GL_AtualizacaoWake": "atualizacao_wake",
    "U_GL_ListaPreco": "lista_preco_wake",
    "U_GL_IdWakeParceiro": "id_wake_parceiro",
    "U_GL_IntegraWake": "integra_wake",
    "U_GL_EmailWake": "email_wake",
    "U_dtUltimaAnaliseCredito": "data_de_analise_credito",
    "Inadimplente": "inadimplente",
    "DataBoletoMaisAntigoInadimplente": "dt_boleto_inadimplente",
}

# Sumiram da exportacao de origem sem aviso (2026-07-23). Sao opcionais: se nao
# vierem, seguimos sem elas. O upsert na VPS preserva o ultimo valor gravado.
COLUNAS_OPCIONAIS = {"Inadimplente", "DataBoletoMaisAntigoInadimplente"}

COLUNAS_VENDEDOR = ["Vendedor", "Supervisão", "Carteira", "VendedorAtendente"]

# Carteira (como vem na planilha) -> prefixo da ilha. O espelho disso está em
# config/tables.py:ILHAS, que o faturamento_full usa.
MAPA_ILHA = {nome: prefixo for prefixo, nome in ILHAS.items()}


def realinhar_por_posicao(
    df: pd.DataFrame, colunas_referencia: list[str]
) -> pd.DataFrame:
    """Devolve os nomes técnicos quando a origem vem em português (01/07/2026).

    Alinho por posição: a exportação em português tem a mesma ordem e a mesma
    quantidade de colunas, só o texto do cabeçalho muda.
    """
    referencia = [
        c for c in colunas_referencia if not str(c).startswith("Unnamed:")
    ]
    if len(referencia) != len(df.columns):
        raise ValueError(
            f"Não deu pra realinhar: a planilha tem {len(df.columns)} colunas e "
            f"a referência tem {len(referencia)}. O layout deve ter mudado."
        )
    df = df.copy()
    df.columns = list(referencia)
    return df


def deduplicar_vendedores(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Deixa um vendedor por linha, ficando com o primeiro.

    Vendedor repetido na planilha multiplicaria as linhas de cliente no join,
    sem ninguém perceber.
    """
    avisos = []
    duplicados = int(df["Vendedor"].duplicated().sum())
    if duplicados:
        nomes = (
            df.loc[df["Vendedor"].duplicated(keep=False), "Vendedor"]
            .unique()
            .tolist()
        )
        avisos.append(
            f"{duplicados} vendedor(es) repetido(s) na planilha: {nomes}. "
            f"Fiquei com o primeiro de cada."
        )
        df = df.drop_duplicates(subset="Vendedor", keep="first")
    return df, avisos


def selecionar_e_renomear(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Fica só com as colunas do mapa e renomeia."""
    ausentes = [c for c in MAPA_COLUNAS if c not in df.columns]
    obrigatorias = [c for c in ausentes if c not in COLUNAS_OPCIONAIS]
    opcionais = [c for c in ausentes if c in COLUNAS_OPCIONAIS]

    if obrigatorias:
        raise ValueError(
            f"Faltam colunas obrigatórias em clientes: {obrigatorias}. "
            f"O layout da exportação deve ter mudado."
        )

    avisos = []
    if opcionais:
        avisos.append(f"Faltam colunas opcionais, seguindo sem elas: {opcionais}")

    manter = [c for c in MAPA_COLUNAS if c in df.columns]
    return df[manter].rename(columns=MAPA_COLUNAS), avisos


def transformar(
    df_clientes: pd.DataFrame, df_vendedores: pd.DataFrame
) -> tuple[pd.DataFrame, list[str]]:
    """Trata clientes de ponta a ponta. Devolve (dataframe, avisos)."""
    df, avisos = selecionar_e_renomear(df_clientes)

    vendedores = df_vendedores[COLUNAS_VENDEDOR]
    vendedores, avisos_dup = deduplicar_vendedores(vendedores)
    avisos.extend(avisos_dup)

    df = df.merge(vendedores, on="Vendedor", how="left")
    df["ilha"] = df["Carteira"].map(MAPA_ILHA)
    df = df.rename(columns={"Supervisão": "supervisor"})

    return df, avisos
