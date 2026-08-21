"""Conversao de tipos e criacao de indice.

O risco aqui e converter errado e calado: data no mes trocado, valor BR lido
como milhar, ou o indice sumindo na proxima carga. Cada teste cobre um desses.
"""

import pandas as pd
import pytest

from src.io.database import _TIPOS_SQL, criar_tabela
from src.quality.contracts import converter_tipos


class CursorFalso:
    """Guarda o SQL executado pra eu conferir o CREATE."""

    def __init__(self):
        self.sqls = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)

    @property
    def create(self):
        return next((s for s in self.sqls if "CREATE TABLE" in s), "")


# ── conversao ────────────────────────────────────────────────


def test_data_br_nao_vira_mes_trocado():
    """03/08/2026 e 3 de agosto. Ler como 8 de marco joga a linha pro mes
    errado calado -- foi o bug que gerou o commit 91880bc."""
    df = pd.DataFrame({"emissao": ["03/08/2026"]})
    out, _ = converter_tipos(df, {"emissao": "data"})
    assert out["emissao"].iloc[0] == pd.Timestamp("2026-08-03")


def test_data_iso_com_hora():
    df = pd.DataFrame({"emissao": ["2026-08-20 10:30:00"]})
    out, _ = converter_tipos(df, {"emissao": "datahora"})
    assert out["emissao"].iloc[0] == pd.Timestamp("2026-08-20 10:30:00")


def test_data_ilegivel_vira_null_com_aviso():
    df = pd.DataFrame({"emissao": ["2026-08-20", "banana"]})
    out, avisos = converter_tipos(df, {"emissao": "data"})
    assert pd.isna(out["emissao"].iloc[1])
    assert len(avisos) == 1
    assert "banana" in avisos[0]


def test_valor_br_com_milhar():
    df = pd.DataFrame({"valor": ["1.234,56"]})
    out, _ = converter_tipos(df, {"valor": "dinheiro"})
    assert float(out["valor"].iloc[0]) == pytest.approx(1234.56)


def test_valor_us_com_ponto_decimal():
    df = pd.DataFrame({"valor": ["99.90"]})
    out, _ = converter_tipos(df, {"valor": "dinheiro"})
    assert float(out["valor"].iloc[0]) == pytest.approx(99.90)


def test_vazio_vira_null_sem_aviso():
    """Célula vazia é ausência de dado, não erro de conversão."""
    df = pd.DataFrame({"valor": ["", None]})
    out, avisos = converter_tipos(df, {"valor": "dinheiro"})
    assert out["valor"].isna().all()
    assert avisos == []


def test_coluna_sem_tipo_continua_string():
    df = pd.DataFrame({"nome": ["ACME", None], "valor": ["10", "20"]})
    out, _ = converter_tipos(df, {"valor": "dinheiro"})
    assert out["nome"].tolist() == ["ACME", ""]


def test_tipo_de_coluna_ausente_e_ignorado():
    """A origem já tirou coluna sem avisar. Não pode quebrar."""
    df = pd.DataFrame({"sku": ["A1"]})
    out, avisos = converter_tipos(df, {"sku": "codigo", "nao_existe": "data"})
    assert list(out.columns) == ["sku"]


def test_sem_tipos_declarados_tudo_string():
    df = pd.DataFrame({"a": [1, None], "b": ["x", None]})
    out, avisos = converter_tipos(df, None)
    assert out["b"].tolist() == ["x", ""]
    assert avisos == []


# ── CREATE TABLE ─────────────────────────────────────────────


def test_sem_tipo_cria_longtext():
    cur = CursorFalso()
    criar_tabela(cur, "T", ["a", "b"])
    assert cur.create.count("LONGTEXT") == 2


def test_tipo_declarado_entra_no_create():
    cur = CursorFalso()
    criar_tabela(cur, "T", ["emissao", "obs"], {"emissao": "data"})
    assert "`emissao` DATE NULL" in cur.create
    assert "`obs` LONGTEXT" in cur.create


def test_indice_entra_no_create():
    cur = CursorFalso()
    criar_tabela(cur, "T", ["sku", "v"], {"sku": "codigo"}, [["sku"]])
    assert "KEY `ix_sku` (`sku`)" in cur.create


def test_indice_composto():
    cur = CursorFalso()
    criar_tabela(
        cur, "T", ["a", "b"], {"a": "codigo", "b": "codigo"}, [["a", "b"]]
    )
    assert "(`a`, `b`)" in cur.create


def test_indice_em_longtext_e_ignorado():
    """LONGTEXT não indexa sem prefixo. Melhor sem índice que carga perdida."""
    cur = CursorFalso()
    criar_tabela(cur, "T", ["sku"], None, [["sku"]])
    assert "KEY" not in cur.create
    assert "CREATE TABLE" in cur.create


def test_indice_de_coluna_ausente_e_ignorado():
    cur = CursorFalso()
    criar_tabela(cur, "T", ["a"], {"a": "codigo"}, [["nao_veio"]])
    assert "KEY" not in cur.create


def test_tipo_invalido_cai_pra_longtext():
    """Apelido que não existe no vocabulário não vira SQL cru."""
    cur = CursorFalso()
    criar_tabela(cur, "T", ["a"], {"a": "BIGINT; DROP TABLE x"})
    assert "DROP" not in cur.create
    assert "`a` LONGTEXT" in cur.create


def test_vocabulario_nao_tem_sql_perigoso():
    for apelido, sql in _TIPOS_SQL.items():
        assert ";" not in sql, apelido
        assert "--" not in sql, apelido


def test_nome_do_indice_nao_leva_o_da_tabela():
    """O replace cria na `__nova` e renomeia. Nome pela coluna sobrevive."""
    cur = CursorFalso()
    criar_tabela(cur, "T__nova", ["sku"], {"sku": "codigo"}, [["sku"]])
    assert "`ix_sku`" in cur.create
    assert "__nova" not in cur.create.split("KEY")[1]


def test_nome_de_indice_longo_e_cortado_em_64():
    """Índice composto de colunas longas estoura o limite de 64 do MySQL."""
    import re

    cur = CursorFalso()
    cols = ["c" * 30, "d" * 30, "e" * 30]
    criar_tabela(cur, "T", cols, {c: "codigo" for c in cols}, [cols])
    nome = re.search(r"KEY `([^`]+)`", cur.create).group(1)
    assert len(nome) <= 64
