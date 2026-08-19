"""Testes do cache em Parquet particionado por mês.

É o que permite carregar só a janela recente em vez das 256 mil linhas.
"""

import pandas as pd
import pytest

from src.io.parquet import (
    gravar_particionado,
    ler_meses,
    meses_disponiveis,
    ultimos_meses,
)


@pytest.fixture
def dados():
    return pd.DataFrame(
        {
            "nota": ["1", "2", "3", "4", "5"],
            "valor": ["10", "20", "30", "40", "50"],
        }
    )


@pytest.fixture
def meses():
    return pd.Series(["2026-06", "2026-07", "2026-07", "2026-08", "2026-08"])


def test_grava_uma_particao_por_mes(tmp_path, dados, meses):
    destino = tmp_path / "pq"
    assert gravar_particionado(dados, destino, meses) == 3
    assert meses_disponiveis(destino) == ["2026-06", "2026-07", "2026-08"]


def test_le_so_os_meses_pedidos(tmp_path, dados, meses):
    destino = tmp_path / "pq"
    gravar_particionado(dados, destino, meses)

    so_agosto = ler_meses(destino, ["2026-08"])
    assert len(so_agosto) == 2
    assert set(so_agosto["nota"]) == {"4", "5"}


def test_janela_de_dois_meses(tmp_path, dados, meses):
    """O caso real: mês corrente + anterior, pra pegar nota retroativa."""
    destino = tmp_path / "pq"
    gravar_particionado(dados, destino, meses)

    janela = ultimos_meses(destino, 2)
    assert janela == ["2026-07", "2026-08"]

    df = ler_meses(destino, janela)
    assert len(df) == 4  # 2 de julho + 2 de agosto
    assert "1" not in set(df["nota"]), "junho não devia entrar"


def test_particao_nao_leva_a_coluna_de_mes(tmp_path, dados, meses):
    """A coluna de partição não pode ir pro banco — ela não existe na tabela."""
    destino = tmp_path / "pq"
    gravar_particionado(dados, destino, meses)

    df = ler_meses(destino, ["2026-08"])
    assert "mes" not in df.columns
    assert list(df.columns) == ["nota", "valor"]


def test_regravar_nao_duplica(tmp_path, dados, meses):
    """Rodando de hora em hora, a mesma partição é reescrita o dia todo."""
    destino = tmp_path / "pq"
    gravar_particionado(dados, destino, meses)
    gravar_particionado(dados, destino, meses)
    gravar_particionado(dados, destino, meses)

    assert len(ler_meses(destino, ["2026-08"])) == 2


def test_meses_antigos_ficam_intactos(tmp_path, dados, meses):
    """Gravar só o mês novo não pode apagar o histórico — é a razão de
    particionar."""
    destino = tmp_path / "pq"
    gravar_particionado(dados, destino, meses)

    novo = pd.DataFrame({"nota": ["9"], "valor": ["90"]})
    gravar_particionado(novo, destino, pd.Series(["2026-09"]))

    assert meses_disponiveis(destino) == [
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
    ]
    assert len(ler_meses(destino, ["2026-06"])) == 1


def test_mes_inexistente_e_ignorado(tmp_path, dados, meses):
    destino = tmp_path / "pq"
    gravar_particionado(dados, destino, meses)

    df = ler_meses(destino, ["2026-08", "2099-01"])
    assert len(df) == 2


def test_pasta_vazia_devolve_lista_vazia(tmp_path):
    assert meses_disponiveis(tmp_path / "naoexiste") == []
    assert ler_meses(tmp_path / "naoexiste", ["2026-08"]).empty
