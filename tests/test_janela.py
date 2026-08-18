"""Testes da janela de meses do faturamento.

Cada um reproduz um cenario que eu confirmei rodando contra os dados reais em
18/08/2026. Nenhum precisa de banco.
"""

import pandas as pd
import pytest

from src.io.parquet import gravar_particionado, meses_disponiveis, ultimos_meses
from src.load.strategies import _parsear_datas, date_range


class CursorFalso:
    """Guarda os SQLs em vez de executar. So o que date_range usa."""

    def __init__(self, existe=True):
        self.sqls = []
        self.parametros = []
        self.existe = existe
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.parametros.append(params)

    def executemany(self, sql, seq):
        self.sqls.append(sql)

    def fetchone(self):
        return ("Faturamento",) if self.existe else None


def _criar_particoes(raiz, meses):
    df = pd.DataFrame({"nota": ["1"], "valor": ["10"]})
    for mes in meses:
        gravar_particionado(df, raiz, pd.Series([mes]))


# ─────────────────────────────────────────────────────────────
# Janela pelo calendario
# ─────────────────────────────────────────────────────────────
def test_janela_normal_pega_mes_corrente_e_anterior(tmp_path):
    _criar_particoes(tmp_path, ["2026-06", "2026-07", "2026-08"])
    hoje = pd.Timestamp("2026-08-18")

    assert ultimos_meses(tmp_path, 2, hoje) == ["2026-07", "2026-08"]


def test_equivalencia_com_os_dados_reais(tmp_path):
    """32 meses contiguos de 2024-01 a 2026-08 — o parquet real de 18/08.

    A janela tem que continuar dando o mesmo de antes da mudanca.
    """
    meses = [
        f"{ano}-{mes:02d}"
        for ano in (2024, 2025, 2026)
        for mes in range(1, 13)
        if not (ano == 2026 and mes > 8)
    ]
    _criar_particoes(tmp_path, meses)

    assert len(meses_disponiveis(tmp_path)) == 32
    assert ultimos_meses(tmp_path, 2, pd.Timestamp("2026-08-18")) == [
        "2026-07",
        "2026-08",
    ]


def test_particao_futura_nao_desloca_a_janela(tmp_path):
    """Uma nota com ano digitado errado criava particao que travava a janela.

    Antes: ultimos_meses devolvia ['2026-08', '2027-05'] e o mes corrente
    parava de ser carregado.
    """
    _criar_particoes(tmp_path, ["2026-07", "2026-08", "2027-05"])

    assert ultimos_meses(tmp_path, 2, pd.Timestamp("2026-08-18")) == [
        "2026-07",
        "2026-08",
    ]


def test_gap_de_mes_nao_puxa_mes_antigo(tmp_path):
    """Sem emissao em julho, a janela nao pode alcancar junho.

    Era o caminho da perda: janela ['2026-06','2026-08'] com DELETE contiguo
    apagava julho sem repor.
    """
    _criar_particoes(tmp_path, ["2026-06", "2026-08"])

    assert ultimos_meses(tmp_path, 2, pd.Timestamp("2026-08-18")) == ["2026-08"]


def test_janela_ignora_mes_que_ainda_nao_existe(tmp_path):
    _criar_particoes(tmp_path, ["2026-08"])

    assert ultimos_meses(tmp_path, 2, pd.Timestamp("2026-08-18")) == ["2026-08"]


def test_tudo_continua_pegando_o_historico_inteiro(tmp_path):
    """--tudo usa quantidade grande e nao pode ser filtrado por calendario."""
    _criar_particoes(tmp_path, ["2024-01", "2025-06", "2026-08"])

    assert ultimos_meses(tmp_path, 999, pd.Timestamp("2026-08-18")) == [
        "2024-01",
        "2025-06",
        "2026-08",
    ]


# ─────────────────────────────────────────────────────────────
# DELETE por mes, nao por intervalo continuo
# ─────────────────────────────────────────────────────────────
def test_delete_usa_os_meses_e_nao_between():
    """Com gap, o BETWEEN apagava o mes do meio sem repor."""
    df = pd.DataFrame(
        {
            "emissao": ["2026-06-10 00:00:00", "2026-08-10 00:00:00"],
            "valor": ["1", "2"],
        }
    )
    cursor = CursorFalso()
    date_range(cursor, "Faturamento", df, "emissao", "%Y-%m-%d %H:%M:%S")

    delete = next(s for s in cursor.sqls if s.startswith("DELETE"))
    assert "BETWEEN" not in delete
    assert "IN (" in delete

    params = cursor.parametros[cursor.sqls.index(delete)]
    assert "2026-06" in params and "2026-08" in params
    assert "2026-07" not in params, "julho nao pode ser apagado"


def test_delete_preserva_o_coalesce_dos_tres_formatos():
    """A cicatriz: origem ja alternou de formato. O COALESCE fica."""
    df = pd.DataFrame({"emissao": ["2026-08-10 00:00:00"], "valor": ["1"]})
    cursor = CursorFalso()
    date_range(cursor, "Faturamento", df, "emissao", "%Y-%m-%d %H:%M:%S")

    delete = next(s for s in cursor.sqls if s.startswith("DELETE"))
    assert delete.count("STR_TO_DATE") == 3
    assert "COALESCE" in delete


def test_delete_manda_formato_por_parametro():
    """Literal com % quebra este driver quando a query tem placeholder."""
    df = pd.DataFrame({"emissao": ["2026-08-10 00:00:00"], "valor": ["1"]})
    cursor = CursorFalso()
    date_range(cursor, "Faturamento", df, "emissao", "%Y-%m-%d %H:%M:%S")

    delete = next(s for s in cursor.sqls if s.startswith("DELETE"))
    assert "%Y" not in delete, "formato tem que ir como parametro"


# ─────────────────────────────────────────────────────────────
# Parser de data
# ─────────────────────────────────────────────────────────────
def test_nao_inverte_dia_e_mes():
    """03/08 e 3 de agosto. O fallback %m/%d/%Y lia como 8 de marco."""
    df = pd.DataFrame({"emissao": ["03/08/2026"]})
    datas = _parsear_datas(df, "emissao", "%d/%m/%Y")

    assert datas.iloc[0].month == 8
    assert datas.iloc[0].day == 3


def test_formatos_mistos_dao_a_data_certa():
    """Antes so conferia que nao virava NaT — passava com dia trocado."""
    df = pd.DataFrame(
        {"emissao": ["01/08/2026", "2026-08-02", "2026-08-03 10:00:00"]}
    )
    datas = _parsear_datas(df, "emissao", "%d/%m/%Y")

    assert list(datas.dt.strftime("%Y-%m-%d")) == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
    ]


@pytest.mark.parametrize(
    "formato", ["%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", ""]
)
def test_iso_e_br_dao_o_mesmo_resultado(formato):
    """preparar() e tables.py usavam formatos diferentes. Tem que empatar."""
    df = pd.DataFrame({"emissao": ["2026-08-03 00:00:00"]})
    datas = _parsear_datas(df, "emissao", formato)

    assert datas.iloc[0].strftime("%Y-%m-%d") == "2026-08-03"
