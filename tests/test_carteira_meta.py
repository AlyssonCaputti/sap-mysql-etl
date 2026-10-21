"""Planilha de carteiras (carteira_meta): fonte do acesso por carteira no hub.

CICATRIZ 20/10/2026: a origem publicou o relatório de metas (20 colunas, gestor
só na primeira linha de cada grupo, "-No Value-" no responsável) no lugar da
planilha de 3 colunas. Cru, ele tiraria o gestor de 37 das 44 carteiras.
"""

import os

import pandas as pd
import pytest

from pipelines import preparar
from src.quality.contracts import normalizar_colunas
from src.transform import carteira_meta as t


def _relatorio():
    """O layout que chegou em 20/10, recortado."""
    return pd.DataFrame(
        {
            "Mês": ["Oct 2026", None, None, None],
            "GESTOR": ["Lucas Souza", None, "Raul Magnos", None],
            "VENDEDOR": ["SA-PR", "KA-SC-PR", "SA-SP-INT", "SA-SP-INT-2"],
            "Responsável": ["Eric", "-No Value-", "João B", "João Izidoro"],
            "STATUS": ["Não Bateu"] * 4,
            "REALIZADO": ["R$ 1,00"] * 4,
        }
    )


def test_relatorio_repete_o_gestor_mesclado():
    saida = t.transformar(_relatorio())
    assert saida["gestor"].tolist() == [
        "Lucas Souza",
        "Lucas Souza",
        "Raul Magnos",
        "Raul Magnos",
    ]


def test_no_value_vira_celula_vazia_e_nao_pessoa():
    saida = t.transformar(_relatorio())
    assert saida.set_index("vendedor").loc["KA-SC-PR", "responsavel"] == ""


def test_sai_sempre_com_as_quatro_colunas():
    saida = t.transformar(_relatorio())
    assert list(saida.columns) == t.COLUNAS_SAIDA
    assert (saida["acesso_extra"] == "").all()
    # O upload normaliza o cabeçalho; tem de continuar igual.
    assert list(normalizar_colunas(saida).columns) == t.COLUNAS_SAIDA


def test_planilha_de_tres_colunas_continua_valendo():
    df = pd.DataFrame(
        {
            "GESTOR": ["Lucas Souza"],
            "VENDEDOR": ["SA-PR"],
            "Responsável": ["Eric"],
            "Acesso extra": ["Andrew Oliveira"],
        }
    )
    saida = t.transformar(df)
    assert saida.iloc[0].to_dict() == {
        "gestor": "Lucas Souza",
        "vendedor": "SA-PR",
        "responsavel": "Eric",
        "acesso_extra": "Andrew Oliveira",
    }


def test_linha_sem_carteira_e_descartada():
    df = _relatorio()
    df.loc[len(df)] = [None, None, None, None, None, None]
    assert len(t.transformar(df)) == 4


@pytest.mark.parametrize(
    "mexer, trecho",
    [
        (lambda d: d.drop(columns=["Responsável"]), "responsavel"),
        (lambda d: d.assign(VENDEDOR=["SA-PR"] * 4), "repetida"),
        (lambda d: d.assign(GESTOR=[None] * 4), "sem gestor"),
    ],
)
def test_recusa_em_vez_de_adivinhar(mexer, trecho):
    with pytest.raises(ValueError, match=trecho):
        t.transformar(mexer(_relatorio()))


# ── preparar ─────────────────────────────────────────────────


@pytest.fixture
def pastas(tmp_path, monkeypatch):
    manuais, entrada = tmp_path / "manuais", tmp_path / "para_vps"
    (manuais / "carteira_meta").mkdir(parents=True)
    monkeypatch.setattr(preparar, "MANUAIS", manuais)
    monkeypatch.setattr(preparar, "ENTRADA_VPS", entrada)
    return manuais / "carteira_meta", entrada / "carteira_meta" / "carteira_meta_tratada.csv"


def _gravar(caminho, df, mtime):
    df.to_csv(caminho, index=False, encoding="utf-8-sig")
    os.utime(caminho, (mtime, mtime))


def test_vale_o_arquivo_mais_recente_da_pasta(pastas):
    origem, saida = pastas
    velho = _relatorio()
    novo = _relatorio().assign(**{"Responsável": ["Eric", "", "João B", "Fulano"]})
    # Nome do velho vem DEPOIS em ordem alfabética: é a armadilha do replace.
    _gravar(origem / "z_velho.csv", velho, 1_000_000)
    _gravar(origem / "a_novo.csv", novo, 2_000_000)
    (origem / "~$a_novo.csv").write_text("lixo do Excel")

    preparar.preparar_carteira_meta()

    lido = pd.read_csv(saida, sep=";", dtype=str, keep_default_na=False)
    assert lido.set_index("vendedor").loc["SA-SP-INT-2", "responsavel"] == "Fulano"


def test_planilha_ruim_nao_derruba_os_manuais(pastas, monkeypatch):
    origem, saida = pastas
    _gravar(origem / "ruim.csv", _relatorio().drop(columns=["GESTOR"]), 1_000_000)
    avisos = []
    monkeypatch.setattr(preparar, "base_falhou", lambda *a, **k: avisos.append(a[0]))

    preparar.preparar_manuais()  # não levanta

    assert avisos == ["carteira_meta"]
    assert not saida.exists()


def test_manuais_nao_copia_trava_do_excel(pastas, monkeypatch):
    origem, saida = pastas
    outra = origem.parent / "csat_garantia"
    outra.mkdir()
    (outra / "~$csat_garantia.xlsx").write_text("lixo do Excel")
    monkeypatch.setattr(preparar, "PASTA_MANUAL_PARA_TABELA", {"csat_garantia": "csat_garantia"})

    preparar.preparar_manuais()

    assert not (saida.parent.parent / "csat_garantia").exists()


def test_manuais_nao_copia_a_carteira_meta_crua(pastas):
    origem, saida = pastas
    _gravar(origem / "relatorio.csv", _relatorio(), 1_000_000)

    preparar.preparar_manuais()

    assert [p.name for p in saida.parent.iterdir()] == ["carteira_meta_tratada.csv"]
