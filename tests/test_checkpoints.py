"""Testes dos três pontos de checagem. Nenhum precisa de banco."""

import logging

import pandas as pd

from src.quality.checkpoints import (
    comparar_com_ultima,
    porta1_recepcao,
    porta2_transformacao,
    saida_carga,
)


class CursorFalso:
    """Devolve a contagem por mês que eu mandar."""

    def __init__(self, por_mes=None, erro=None):
        self.por_mes = por_mes or {}
        self.erro = erro

    def execute(self, sql, params=None):
        if self.erro:
            raise self.erro

    def fetchall(self):
        return list(self.por_mes.items())


# ─────────────────────────────────────────────────────────────
# Porta 1 — recepção
# ─────────────────────────────────────────────────────────────
def test_porta1_conta_linhas_e_colunas():
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    m = porta1_recepcao(df, "arquivo.csv", [])

    assert m["linhas"] == 3
    assert m["colunas"] == 2
    assert m["linhas_vazias"] == 0


def test_porta1_acusa_linha_toda_vazia(caplog):
    df = pd.DataFrame({"a": [1, None], "b": ["x", None]})
    with caplog.at_level(logging.WARNING):
        m = porta1_recepcao(df, "arquivo.csv", [])

    assert m["linhas_vazias"] == 1
    assert "vazia" in caplog.text


def test_porta1_registra_avisos_da_leitura():
    df = pd.DataFrame({"a": [1]})
    m = porta1_recepcao(df, "arquivo.csv", ["descartei 3 linhas"])

    assert m["avisos"] == 1


# ─────────────────────────────────────────────────────────────
# Porta 2 — transformação
# ─────────────────────────────────────────────────────────────
def test_porta2_acusa_linha_perdida_na_transformacao(caplog):
    df = pd.DataFrame({"a": [1, 2]})
    with caplog.at_level(logging.WARNING):
        m = porta2_transformacao(df, "arquivo.csv", linhas_entrada=10)

    assert m["linhas_perdidas"] == 8
    assert "sumiram" in caplog.text


def test_porta2_nao_reclama_quando_nada_se_perde(caplog):
    df = pd.DataFrame({"a": [1, 2]})
    with caplog.at_level(logging.WARNING):
        porta2_transformacao(df, "arquivo.csv", linhas_entrada=2)

    assert "sumiram" not in caplog.text


def test_porta2_acha_chave_duplicada(caplog):
    df = pd.DataFrame({"sku": ["A", "B", "A"]})
    with caplog.at_level(logging.WARNING):
        m = porta2_transformacao(df, "x.csv", linhas_entrada=3, chave="sku")

    assert m["chave_duplicada"] == 1
    assert "repetido" in caplog.text


def test_porta2_acha_chave_vazia(caplog):
    df = pd.DataFrame({"sku": ["A", "", "  "]})
    with caplog.at_level(logging.WARNING):
        m = porta2_transformacao(df, "x.csv", linhas_entrada=3, chave="sku")

    assert m["chave_vazia"] == 2


def test_porta2_reporta_a_janela_de_datas():
    df = pd.DataFrame({"emissao": ["x", "y"]})
    datas = pd.Series([pd.Timestamp("2026-07-01"), pd.Timestamp("2026-08-10")])
    m = porta2_transformacao(
        df, "x.csv", linhas_entrada=2, coluna_data="emissao", datas=datas
    )

    assert m["janela"] == "2026-07-01 -> 2026-08-10"
    assert m["datas_ilegiveis"] == 0
    assert m["datas_futuras"] == 0


def test_porta2_conta_data_futura():
    df = pd.DataFrame({"emissao": ["x"]})
    futura = pd.Timestamp.today().normalize() + pd.Timedelta(days=30)
    m = porta2_transformacao(
        df, "x.csv", linhas_entrada=1, coluna_data="emissao", datas=pd.Series([futura])
    )

    assert m["datas_futuras"] == 1


# ─────────────────────────────────────────────────────────────
# Saída — origem vs banco
# ─────────────────────────────────────────────────────────────
def test_saida_nao_reclama_quando_bate():
    cursor = CursorFalso({"2026-07": 100, "2026-08": 50})
    m = saida_carga(
        cursor, "Faturamento", "emissao", ["2026-07", "2026-08"],
        {"2026-07": 100, "2026-08": 50},
    )

    assert m["divergiu"] is False


def test_saida_acusa_retroativo_fora_da_janela(caplog):
    """O caso real de 18/08: 31 linhas em 2026-06, fora da janela."""
    cursor = CursorFalso({"2026-06": 8662, "2026-07": 8017, "2026-08": 5860})
    with caplog.at_level(logging.WARNING):
        m = saida_carga(
            cursor, "Faturamento", "emissao", ["2026-07", "2026-08"],
            {"2026-06": 8693, "2026-07": 8017, "2026-08": 5860},
        )

    assert m["divergiu"] is True
    assert m["meses_divergentes"] == {"2026-06": 31}
    assert "FORA da janela" in caplog.text
    assert "--tudo" in caplog.text


def test_saida_separa_dentro_de_fora_da_janela(caplog):
    cursor = CursorFalso({"2026-06": 90, "2026-08": 40})
    with caplog.at_level(logging.WARNING):
        saida_carga(
            cursor, "Faturamento", "emissao", ["2026-08"],
            {"2026-06": 100, "2026-08": 50},
        )

    assert "DENTRO da janela" in caplog.text
    assert "FORA da janela" in caplog.text


def test_saida_nunca_levanta_se_a_consulta_falhar(caplog):
    """A carga já foi commitada. Conferência que falha não pode derrubar nada."""
    cursor = CursorFalso(erro=RuntimeError("sem permissão"))
    with caplog.at_level(logging.WARNING):
        m = saida_carga(cursor, "Faturamento", "emissao", ["2026-08"], {"2026-08": 10})

    assert m["divergiu"] is False
    assert "não consegui conferir" in caplog.text


# ─────────────────────────────────────────────────────────────
# Queda de volume
# ─────────────────────────────────────────────────────────────
def test_avisa_quando_a_origem_encolhe(caplog):
    with caplog.at_level(logging.WARNING):
        assert comparar_com_ultima(5_000, 100_000, "x.csv") is True
    assert "menos" in caplog.text


def test_variacao_pequena_nao_alarma():
    assert comparar_com_ultima(99_000, 100_000, "x.csv") is False


def test_origem_crescendo_nao_alarma():
    assert comparar_com_ultima(120_000, 100_000, "x.csv") is False


def test_primeira_execucao_nao_alarma():
    assert comparar_com_ultima(100, None, "x.csv") is False
