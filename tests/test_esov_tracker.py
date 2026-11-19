"""Planilha ESOV Tracker do Marketing -> tabela longa esov_tracker.

A planilha é editada à mão e tem blocos por aba, então os testes cobrem o que
já é real nela: bloco achado por rótulo, célula vazia = 0 em mês medido, mês
não medido fora, e recusa alta (aba!célula) para marca ou valor errado.
"""

import os
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from pipelines import preparar
from src.quality.contracts import converter_tipos, normalizar_colunas
from src.transform import esov_tracker as t

MESES = ["out/26", "nov/26", "dez/26"]
PLANILHA_REAL = Path(r"C:\dados\ESOV_Tracker_2027_v03.xlsx")


def _bloco(ws, linha: int, valores: dict[str, list]) -> int:
    """Escreve cabeçalho + 10 marcas + total a partir de `linha`. Devolve a próxima livre."""
    ws.cell(linha, 1, "Marca")
    for j, mes in enumerate(MESES, start=2):
        ws.cell(linha, j, mes)
    for i, marca in enumerate(t.MARCAS, start=1):
        ws.cell(linha + i, 1, marca)
        for j, v in enumerate(valores.get(marca, []), start=2):
            ws.cell(linha + i, j, v)
    ws.cell(linha + 11, 1, "TOTAL do conjunto")
    ws.cell(linha + 11, 2, "=SUM(B2:B11)")
    return linha + 13


def _planilha(tmp_path, search=None, meta=None, google=None, youtube=None, nome="esov.xlsx"):
    padrao = {m: [10] for m in t.MARCAS}
    wb = openpyxl.Workbook()
    wb.active.title = "Leia-me"

    ws = wb.create_sheet("Share_of_Search")
    ws.cell(1, 1, "Share of search")
    _bloco(ws, 4, search or padrao)

    ws = wb.create_sheet("Anúncios")
    ws.cell(1, 1, "Anúncios ativos")
    # Bloco de total no topo, como na planilha real: tem de ser ignorado.
    prox = _bloco(ws, 5, {m: [999] for m in t.MARCAS})
    ws.cell(prox, 1, "1. Meta Ad Library — anúncios ativos")
    prox = _bloco(ws, prox + 1, meta or padrao)
    ws.cell(prox, 1, "2. Google Ads Transparency — anúncios")
    _bloco(ws, prox + 1, google or padrao)

    ws = wb.create_sheet("YouTube")
    _bloco(ws, 4, youtube or padrao)

    caminho = tmp_path / nome
    wb.save(caminho)
    return caminho


def _valor(df, mes, marca, componente):
    linha = df[(df.mes == mes) & (df.marca == marca) & (df.componente == componente)]
    assert len(linha) == 1
    return linha.valor.iloc[0]


def test_extrai_os_quatro_componentes_em_formato_longo(tmp_path):
    df = t.transformar(_planilha(tmp_path))
    assert list(df.columns) == t.COLUNAS_SAIDA
    assert set(df.componente) == {"search", "meta", "google", "youtube"}
    # Só out/26 foi medido; 4 componentes x 10 marcas.
    assert set(df.mes) == {"2026-10"}
    assert len(df) == 40


def test_ignora_o_bloco_de_total_do_topo_de_anuncios(tmp_path):
    df = t.transformar(_planilha(tmp_path))
    assert 999 not in df.valor.tolist()


def test_celula_vazia_em_mes_medido_vale_zero(tmp_path):
    youtube = {m: [100] for m in t.MARCAS}
    youtube["Comforser"] = []
    df = t.transformar(_planilha(tmp_path, youtube=youtube))
    assert _valor(df, "2026-10", "Comforser", "youtube") == 0


def test_mes_nao_medido_nao_gera_linha(tmp_path):
    search = {m: [10, None, 5] for m in t.MARCAS}
    df = t.transformar(_planilha(tmp_path, search=search))
    meses_search = set(df[df.componente == "search"].mes)
    assert meses_search == {"2026-10", "2026-12"}


def test_marca_fora_do_conjunto_recusa_com_aba_e_celula(tmp_path):
    caminho = _planilha(tmp_path)
    wb = openpyxl.load_workbook(caminho)
    wb["YouTube"]["A5"] = "Falken"
    wb.save(caminho)
    with pytest.raises(ValueError, match=r"YouTube!A5.*Falken"):
        t.transformar(caminho)


def test_marca_faltando_recusa(tmp_path):
    caminho = _planilha(tmp_path)
    wb = openpyxl.load_workbook(caminho)
    wb["YouTube"].delete_rows(14)  # some a Westlake (última marca)
    wb.save(caminho)
    with pytest.raises(ValueError, match="Westlake"):
        t.transformar(caminho)


@pytest.mark.parametrize("ruim", ["dez", -3])
def test_valor_que_nao_e_numero_ou_negativo_recusa(tmp_path, ruim):
    search = {m: [10] for m in t.MARCAS}
    search["Kumho"] = [ruim]
    with pytest.raises(ValueError, match=r"Share_of_Search!B8"):
        t.transformar(_planilha(tmp_path, search=search))


def test_formula_sem_valor_calculado_recusa(tmp_path):
    # openpyxl grava fórmula sem cache — é o caso de arquivo salvo por script.
    meta = {m: [1] for m in t.MARCAS}
    meta["Delinte"] = ["=5+27"]
    with pytest.raises(ValueError, match="fórmula sem valor calculado"):
        t.transformar(_planilha(tmp_path, meta=meta))


def test_linha_inserida_antes_do_bloco_nao_desloca_dado(tmp_path):
    caminho = _planilha(tmp_path)
    wb = openpyxl.load_workbook(caminho)
    wb["Share_of_Search"].insert_rows(2, amount=3)
    wb.save(caminho)
    df = t.transformar(caminho)
    assert len(df[df.componente == "search"]) == 10


def test_valor_sai_com_no_maximo_duas_casas(tmp_path):
    # "1.234" no CSV seria lido como milhar pelo conversor BR do upload.
    search = {m: [10] for m in t.MARCAS}
    search["Delinte"] = [1.234]
    df = t.transformar(_planilha(tmp_path, search=search))
    assert _valor(df, "2026-10", "Delinte", "search") == 1.23

    convertido, avisos = converter_tipos(
        df.astype({"valor": str}), {"valor": "decimal"}
    )
    assert not avisos
    assert convertido.loc[df.marca == "Delinte"].valor.iloc[0] == pytest.approx(1.23)


def test_saida_passa_pela_normalizacao_do_upload_sem_mudar(tmp_path):
    df = t.transformar(_planilha(tmp_path))
    assert list(normalizar_colunas(df).columns) == t.COLUNAS_SAIDA


def test_preparar_usa_a_planilha_mais_recente(tmp_path, monkeypatch):
    pasta = tmp_path / "manuais" / "esov_tracker"
    pasta.mkdir(parents=True)
    velha = _planilha(pasta, search={m: [1] for m in t.MARCAS}, nome="v02.xlsx")
    nova = _planilha(pasta, search={m: [7] for m in t.MARCAS}, nome="v03.xlsx")
    os.utime(velha, (1_000_000, 1_000_000))
    (pasta / "~$v03.xlsx").write_bytes(b"trava do excel")

    saida = tmp_path / "para_vps"
    monkeypatch.setattr(preparar, "MANUAIS", tmp_path / "manuais")
    monkeypatch.setattr(preparar, "ENTRADA_VPS", saida)
    preparar.preparar_esov_tracker()

    csv = pd.read_csv(saida / "esov_tracker" / "esov_tracker_tratado.csv", sep=";")
    assert set(csv[csv.componente == "search"].valor) == {7}
    assert nova.exists()


@pytest.mark.skipif(not PLANILHA_REAL.exists(), reason="planilha real só existe na máquina do ETL")
def test_planilha_real_de_out_26():
    df = t.transformar(PLANILHA_REAL)
    assert set(df.mes) == {"2026-10"}
    assert len(df) == 40
    assert _valor(df, "2026-10", "Delinte", "search") == 210
    # Meta/Delinte é a fórmula "=5+27": tem de vir o valor calculado.
    assert _valor(df, "2026-10", "Delinte", "meta") == 32
    assert _valor(df, "2026-10", "Delinte", "google") == 25
    assert _valor(df, "2026-10", "Delinte", "youtube") == 17078
    # Totais batem com a linha "TOTAL do conjunto" de cada aba.
    soma = df.groupby("componente").valor.sum()
    assert soma["search"] == 6089
    assert soma["meta"] + soma["google"] == 164
    assert soma["youtube"] == 2898668
