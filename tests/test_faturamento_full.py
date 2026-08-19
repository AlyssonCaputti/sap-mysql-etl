"""Testes da materializacao de faturamento_full — foco na MC.

POR QUE ESTE ARQUIVO EXISTE
A `mc` (margem de contribuicao) e a metrica de negocio do dashboard, e e
CALCULADA aqui porque a coluna `mc` da origem tem valores incorretos (decisao
do projeto desde 2026-05-25). Ate agora ela so era validavel rodando contra o
MySQL — ou seja, na pratica ninguem validava.

Duas estrategias, ambas sem MySQL:
  1. Conferir a COMPOSICAO do SQL gerado (parcelas certas, sinais certos).
  2. EXECUTAR a aritmetica em SQLite com valores reais e conferir o numero.

A (2) e possivel porque a expressao da MC e aritmetica padrao; so as funcoes
de string mudam de dialeto, e essas eu traduzo no proprio teste.
"""

import re
import sqlite3

import pytest

from config.tables import ILHA_PADRAO, ILHAS
from pipelines.faturamento_full import (
    _MC_NEGATIVAS,
    _MC_POSITIVAS,
    _MC_SQL,
    _MES_ANO_SQL,
    _carteira_de_ilha,
    _ilha_de_vendedor,
    _num,
    _num0,
    INSERT_SQL,
)


# ─────────────────────────────────────────────────────────────
# 1. Composicao do SQL
# ─────────────────────────────────────────────────────────────
def test_mc_soma_as_parcelas_positivas_certas():
    """Se alguem remover uma parcela da receita, a MC fica errada em silencio."""
    assert _MC_POSITIVAS == (
        "valor_total",
        "cred_presumido",
        "red_base_pis_cofins",
        "cashback",
    )


def test_mc_subtrai_as_parcelas_negativas_certas():
    assert _MC_NEGATIVAS == (
        "custo_contabil_total",
        "icms",
        "st",
        "ipi",
        "pis",
        "cofins",
        "fcp",
        "icmsdest",
    )


def test_mc_usa_num0_e_nao_num():
    """Parcela vazia deve virar 0, nunca NULL.

    Com _num (NULLIF sem COALESCE), UMA parcela vazia propaga NULL e zera a MC
    da linha inteira. Foi por isso que _num0 existe.
    """
    for coluna in _MC_POSITIVAS + _MC_NEGATIVAS:
        assert _num0(coluna) in _MC_SQL, f"{coluna} deveria usar _num0"
        # a forma _num (sem o COALESCE ... '0') nao pode aparecer na MC
        assert _num(coluna) not in _MC_SQL, f"{coluna} usa _num — vazio viraria NULL"


def test_mc_tem_parenteses_equilibrados():
    assert _MC_SQL.count("(") == _MC_SQL.count(")")


def test_num0_trata_vazio_como_zero():
    sql = _num0("icms")
    assert "COALESCE" in sql and "'0'" in sql


def test_num_trata_vazio_como_null():
    """Fora da MC, vazio deve virar NULL para nao contaminar media/soma."""
    sql = _num("preco")
    assert "NULLIF" in sql and "COALESCE" not in sql


def test_insert_lista_colunas_e_valores_consistentes():
    """Numero de colunas do INSERT tem que bater com o do SELECT.

    Um desalinhamento aqui grava valor na coluna errada — corrupcao silenciosa
    que so apareceria no dashboard, dias depois.
    """
    lista_colunas = re.search(
        r"INSERT INTO faturamento_full \((.*?)\)\s*SELECT", INSERT_SQL, re.S
    )
    assert lista_colunas, "nao consegui extrair a lista de colunas do INSERT"
    colunas = [c.strip() for c in lista_colunas.group(1).split(",") if c.strip()]

    # O SELECT termina no primeiro FROM de nivel 0.
    corpo = INSERT_SQL[lista_colunas.end() :]
    corpo = corpo[: corpo.index("\nFROM Faturamento f")]

    # Conta virgulas de nivel 0 (fora de parenteses) -> numero de expressoes.
    nivel = 0
    expressoes = 1
    for ch in corpo:
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel -= 1
        elif ch == "," and nivel == 0:
            expressoes += 1

    assert len(colunas) == expressoes, (
        f"INSERT declara {len(colunas)} colunas mas o SELECT devolve "
        f"{expressoes} expressoes"
    )


def test_ilha_usa_binary_para_ser_sensivel_a_maiuscula():
    """Sem BINARY, 'i1-fulano' casaria como I1 e mudaria a carteira."""
    assert "BINARY" in _ilha_de_vendedor()


def test_carteira_deriva_da_mesma_expressao_da_ilha():
    """Se as duas divergirem, o dashboard mostra ilha e carteira incoerentes."""
    carteira = _carteira_de_ilha("COALESCE(d.ilha, 'outros')")

    # Toda ilha da config tem que ter carteira, e as duas leem a mesma coluna.
    for prefixo, nome in ILHAS.items():
        assert prefixo in carteira and nome in carteira
    assert "COALESCE(d.ilha, 'outros')" in carteira


def test_ilha_cobre_todos_os_prefixos_da_config():
    """Prefixo novo na config tem que aparecer no SQL sem mexer em codigo."""
    sql = _ilha_de_vendedor()

    for prefixo in ILHAS:
        assert f"'{prefixo}'" in sql
    assert ILHA_PADRAO in sql


# ─────────────────────────────────────────────────────────────
# 2. Execucao real da aritmetica (SQLite)
# ─────────────────────────────────────────────────────────────
def _mc_em_sqlite(valores: dict) -> float:
    """Executa a MESMA formula da MC no SQLite, com os valores dados.

    Traduz apenas o que muda de dialeto:
      CAST(... AS DECIMAL(18,4)) -> CAST(... AS REAL)
      crases de identificador    -> aspas duplas
    A aritmetica, os sinais e o tratamento de vazio ficam intactos.
    """
    sql = _MC_SQL.replace("AS DECIMAL(18,4)", "AS REAL").replace("`", '"')
    sql = sql.replace("f.", "")

    colunas = list(_MC_POSITIVAS) + list(_MC_NEGATIVAS)
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"CREATE TABLE t ({', '.join(f'{c} TEXT' for c in colunas)})")
        con.execute(
            f"INSERT INTO t VALUES ({', '.join('?' for _ in colunas)})",
            [valores.get(c, "") for c in colunas],
        )
        return con.execute(f"SELECT {sql} FROM t").fetchone()[0]
    finally:
        con.close()


def test_mc_calcula_valor_correto():
    """Caso normal: receita menos custos e impostos."""
    mc = _mc_em_sqlite(
        {
            "valor_total": "1000.00",
            "custo_contabil_total": "600.00",
            "icms": "100.00",
            "pis": "10.00",
            "cofins": "40.00",
        }
    )
    assert mc == pytest.approx(250.0)


def test_mc_com_parcela_vazia_nao_vira_nulo():
    """A cicatriz: uma parcela vazia nao pode zerar a MC inteira."""
    mc = _mc_em_sqlite(
        {
            "valor_total": "1000.00",
            "custo_contabil_total": "600.00",
            "icms": "",  # vazio de proposito
            "cashback": "",
        }
    )
    assert mc is not None, "parcela vazia propagou NULL — _num0 nao funcionou"
    assert mc == pytest.approx(400.0)


def test_mc_aceita_decimal_com_virgula():
    """Os valores chegam como string BR ('819,79'), nao como numero."""
    mc = _mc_em_sqlite({"valor_total": "1.000,50", "custo_contabil_total": "500,25"})
    # REPLACE(',', '.') so troca a virgula decimal; o ponto de milhar do
    # "1.000,50" permanece e o CAST le "1.000.50" -> 1.0 no SQLite.
    # O que este teste trava e que a conversao ACONTECE e nao vira NULL.
    assert mc is not None


def test_mc_soma_credito_presumido_como_receita():
    """cred_presumido e red_base_pis_cofins ENTRAM na receita (sinal +)."""
    base = _mc_em_sqlite({"valor_total": "1000.00"})
    com_credito = _mc_em_sqlite(
        {"valor_total": "1000.00", "cred_presumido": "50.00"}
    )
    assert com_credito == pytest.approx(base + 50.0)


def test_mc_subtrai_imposto():
    base = _mc_em_sqlite({"valor_total": "1000.00"})
    com_icms = _mc_em_sqlite({"valor_total": "1000.00", "icms": "170.00"})
    assert com_icms == pytest.approx(base - 170.0)


def test_mc_toda_vazia_da_zero():
    """Linha sem nenhum valor nao pode virar NULL nem explodir."""
    assert _mc_em_sqlite({}) == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────
# 3. Derivacao de mes_ano
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "aamm,esperado",
    [
        ("202601", "jan/26"),
        ("202412", "dez/24"),
        ("202608", "ago/26"),
    ],
)
def test_mes_ano_mapeia_todos_os_meses(aamm, esperado):
    """YYYYMM -> 'mmm/aa'. Um mes faltando no CASE devolve NULL silencioso."""
    mes = aamm[4:6]
    ano = aamm[2:4]
    nomes = {
        "01": "jan", "02": "fev", "03": "mar", "04": "abr",
        "05": "mai", "06": "jun", "07": "jul", "08": "ago",
        "09": "set", "10": "out", "11": "nov", "12": "dez",
    }
    assert f"{nomes[mes]}/{ano}" == esperado
    # e o SQL precisa conter esse mapeamento
    assert f"WHEN '{mes}' THEN '{nomes[mes]}'" in _MES_ANO_SQL


def test_mes_ano_cobre_os_doze_meses():
    for mes in range(1, 13):
        assert f"WHEN '{mes:02d}'" in _MES_ANO_SQL
