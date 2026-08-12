"""Testes das transformacoes e das protecoes criticas.

Rodam sem acesso ao P:\\ e sem banco — essa e a razao de transform/ nao fazer
I/O. No original, testar clientes exigia a rede montada.

Cada teste marcado como CICATRIZ reproduz uma falha real de producao.

    python -m pytest tests/ -v
"""

import pandas as pd
import pytest

from src.quality.contracts import (
    chave_comparacao,
    exigir_nao_vazio,
    normalizar_colunas,
    resolver_renomeacao,
    validar_contrato,
)
from src.transform import clientes as t_clientes
from src.transform import faturamento as t_faturamento
from src.transform import itens as t_itens


# ─────────────────────────────────────────────────────────────
# Normalizacao de colunas
# ─────────────────────────────────────────────────────────────
def test_normaliza_acento_e_espaco():
    df = pd.DataFrame({"Código do PN": [1], "Supervisão": [2]})
    assert list(normalizar_colunas(df).columns) == ["codigo_do_pn", "supervisao"]


def test_desambigua_colunas_duplicadas():
    df = pd.DataFrame([[1, 2, 3]], columns=["nota", "Nota", "NOTA"])
    assert list(normalizar_colunas(df).columns) == ["nota", "nota_1", "nota_2"]


# ─────────────────────────────────────────────────────────────
# CICATRIZ: origem alterna header tecnico / tratado / com typo
# ─────────────────────────────────────────────────────────────
def test_casa_header_tecnico_sap():
    df = pd.DataFrame(columns=["CredPresumido"])
    renomeacao, ausentes = resolver_renomeacao(df, {"CredPresumido": "cred_presumido"})
    assert renomeacao == {"CredPresumido": "cred_presumido"}
    assert ausentes == []


def test_casa_header_ja_tratado():
    df = pd.DataFrame(columns=["credpresumido"])
    renomeacao, ausentes = resolver_renomeacao(df, {"CredPresumido": "cred_presumido"})
    assert renomeacao == {"credpresumido": "cred_presumido"}
    assert ausentes == []


def test_casa_apelido_com_typo_da_origem():
    # A origem ja exportou "confins" (typo) em vez de "cofins".
    df = pd.DataFrame(columns=["red_base_pis_confins"])
    renomeacao, ausentes = resolver_renomeacao(
        df,
        {"RedBasePisCofins": "red_base_pis_cofins"},
        {"red_base_pis_cofins": ["red_base_pis_confins"]},
    )
    assert renomeacao == {"red_base_pis_confins": "red_base_pis_cofins"}
    assert ausentes == []


def test_chave_comparacao_ignora_underscore_e_caixa():
    assert chave_comparacao("Cred_Presumido") == chave_comparacao("credpresumido")


# ─────────────────────────────────────────────────────────────
# Contratos de schema
# ─────────────────────────────────────────────────────────────
def test_contrato_aborta_se_falta_obrigatoria():
    df = pd.DataFrame(columns=["sku", "ncm"])
    with pytest.raises(ValueError, match="[Ff]alta.*obrigat"):
        validar_contrato(df, {"obrigatorias": {"sku", "custo_medio"}}, "teste.csv")


def test_contrato_avisa_se_falta_opcional():
    df = pd.DataFrame(columns=["sku"])
    avisos = validar_contrato(
        df, {"obrigatorias": {"sku"}, "opcionais": {"inadimplente"}}, "teste.csv"
    )
    assert len(avisos) == 1 and "inadimplente" in avisos[0]


def test_recusa_arquivo_vazio():
    # Melhor manter o dado de ontem do que publicar tabela vazia.
    with pytest.raises(ValueError, match="nenhuma linha"):
        exigir_nao_vazio(pd.DataFrame(), "vazio.csv")


# ─────────────────────────────────────────────────────────────
# CICATRIZ: vendedor duplicado multiplicava linhas no LEFT JOIN
# ─────────────────────────────────────────────────────────────
def test_vendedor_duplicado_nao_multiplica_clientes():
    vendedores = pd.DataFrame(
        {
            "Vendedor": ["ANA", "ANA", "BRUNO"],
            "Supervisão": ["S1", "S2", "S3"],
            "Carteira": ["Growth", "Growth", "Key Account"],
            "VendedorAtendente": ["X", "Y", "Z"],
        }
    )
    resultado, avisos = t_clientes.deduplicar_vendedores(vendedores)

    assert len(resultado) == 2
    assert resultado["Vendedor"].is_unique
    assert "ANA" in avisos[0]


def test_merge_preserva_contagem_de_clientes():
    clientes = pd.DataFrame(
        {
            "CardCode": ["C1", "C2", "C3"],
            "NomeVendedor": ["ANA", "ANA", "BRUNO"],
        }
    )
    vendedores = pd.DataFrame(
        {
            "Vendedor": ["ANA", "ANA", "BRUNO"],  # duplicado de proposito
            "Supervisão": ["S1", "S2", "S3"],
            "Carteira": ["Growth", "Growth", "Key Account"],
            "VendedorAtendente": ["X", "Y", "Z"],
        }
    )

    mapa = {"CardCode": "codigo_do_pn", "NomeVendedor": "Vendedor"}
    original = t_clientes.MAPA_COLUNAS
    t_clientes.MAPA_COLUNAS = mapa
    try:
        resultado, _ = t_clientes.transformar(clientes, vendedores)
    finally:
        t_clientes.MAPA_COLUNAS = original

    # Sem a deduplicacao isto viraria 5 linhas.
    assert len(resultado) == 3


def test_mapeia_ilha_a_partir_da_carteira():
    clientes = pd.DataFrame({"CardCode": ["C1"], "NomeVendedor": ["ANA"]})
    vendedores = pd.DataFrame(
        {
            "Vendedor": ["ANA"],
            "Supervisão": ["S1"],
            "Carteira": ["Key Account"],
            "VendedorAtendente": ["X"],
        }
    )

    original = t_clientes.MAPA_COLUNAS
    t_clientes.MAPA_COLUNAS = {"CardCode": "codigo_do_pn", "NomeVendedor": "Vendedor"}
    try:
        resultado, _ = t_clientes.transformar(clientes, vendedores)
    finally:
        t_clientes.MAPA_COLUNAS = original

    assert resultado["ilha"].iloc[0] == "KA"
    assert "supervisor" in resultado.columns


# ─────────────────────────────────────────────────────────────
# CICATRIZ: coluna opcional sumiu da origem (2026-07-23)
# ─────────────────────────────────────────────────────────────
def test_segue_sem_coluna_opcional():
    df = pd.DataFrame({c: ["x"] for c in t_clientes.MAPA_COLUNAS if c != "Inadimplente"})
    df = df.drop(columns=["DataBoletoMaisAntigoInadimplente"], errors="ignore")

    resultado, avisos = t_clientes.selecionar_e_renomear(df)

    assert len(resultado) == 1
    assert any("opcionais" in a for a in avisos)


def test_aborta_se_falta_coluna_obrigatoria():
    df = pd.DataFrame({"CardCode": ["C1"]})
    with pytest.raises(ValueError, match="[Ff]alta.*obrigat"):
        t_clientes.selecionar_e_renomear(df)


# ─────────────────────────────────────────────────────────────
# CICATRIZ: origem alterna formato BR e ponto decimal
# ─────────────────────────────────────────────────────────────
def test_detecta_formato_brasileiro():
    assert t_faturamento.e_formato_brasileiro(pd.Series(["1.399,90", "759,00"]))
    assert not t_faturamento.e_formato_brasileiro(pd.Series(["1399.90", "759.00"]))


def test_normaliza_br_removendo_separador_de_milhar():
    df = pd.DataFrame({"valor_total": ["1.399,90", "819,79"]})
    resultado = t_faturamento.normalizar_numericas(df)
    assert resultado["valor_total"].tolist() == ["1399,90", "819,79"]


def test_normaliza_ponto_decimal_para_virgula():
    df = pd.DataFrame({"valor_total": ["1399.90", "819.79"]})
    resultado = t_faturamento.normalizar_numericas(df)
    assert resultado["valor_total"].tolist() == ["1399,90", "819,79"]


# ─────────────────────────────────────────────────────────────
# CICATRIZ: header de itens veio em portugues (2026-07-23)
# ─────────────────────────────────────────────────────────────
def test_restaura_header_tecnico_por_posicao():
    colunas = [f"col{i}" for i in range(460)]
    colunas[0] = "Codigo do Item"  # header em portugues
    df = pd.DataFrame([[None] * 460], columns=colunas)

    resultado, avisos = t_itens.restaurar_header_tecnico(df)

    assert resultado.columns[0] == "ItemCode"
    assert resultado.columns[1] == "ItemName"
    assert resultado.columns[455] == "Ativo"
    assert avisos


def test_nao_mexe_se_header_ja_e_tecnico():
    df = pd.DataFrame(columns=["ItemCode", "ItemName"])
    resultado, avisos = t_itens.restaurar_header_tecnico(df)
    assert list(resultado.columns) == ["ItemCode", "ItemName"]
    assert avisos == []


def test_aborta_se_layout_de_itens_encolheu():
    df = pd.DataFrame([[1, 2]], columns=["a", "b"])
    with pytest.raises(ValueError, match="layout de itens mudou"):
        t_itens.restaurar_header_tecnico(df)


# ─────────────────────────────────────────────────────────────
# CICATRIZ: limite de colunas do InnoDB forca fatiamento
# ─────────────────────────────────────────────────────────────
def test_fatia_extras_com_chave_em_cada_lote():
    colunas = {"ItemCode": ["I1"]}
    colunas.update({f"extra{i}": ["v"] for i in range(320)})
    df = pd.DataFrame(colunas)

    lotes = t_itens.fatiar_extras(df)

    assert len(lotes) == 3  # 320 extras / 150 por lote
    for lote in lotes:
        assert lote.columns[0] == "numero_do_item"
        assert len(lote.columns) <= t_itens.COLUNAS_POR_LOTE_ITENS + 1
