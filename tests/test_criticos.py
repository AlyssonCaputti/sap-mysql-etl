"""Testes das tres correcoes criticas e da leitura resiliente.

As correcoes 1 e 2 tratam de PERDA/CORRUPCAO SILENCIOSA de dado — o tipo de
falha que nao aparece em log nem em exit code. Sem teste, uma refatoracao
futura as remove sem ninguem perceber.

    python -m pytest tests/ -v
"""

import pandas as pd
import pytest

from src.io.database import validar_identificador
from src.io.readers import (
    _precisa_reconstrucao,
    _reconstruir_linha,
    detectar_encoding,
    detectar_separador,
    ler_csv,
)
from src.load.strategies import _parsear_datas, date_range, executar


class CursorFalso:
    """Cursor minimo: registra o SQL executado, sem banco."""

    def __init__(self, tabela_existe=True):
        self.executados = []
        self._tabela_existe = tabela_existe
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executados.append((sql, params))

    def executemany(self, sql, seq):
        self.executados.append((sql, f"<{len(seq)} linhas>"))

    def fetchone(self):
        return ("Tabela",) if self._tabela_existe else None

    def fetchall(self):
        return []


# ─────────────────────────────────────────────────────────────
# CORRECAO 1 — duplicidade permanente no date_range
# ─────────────────────────────────────────────────────────────
def test_date_range_aborta_com_data_ilegivel():
    """A correcao central.

    O original so avisava e deixava a linha seguir para o INSERT. Como o DELETE
    filtra por STR_TO_DATE (que devolve NULL nessas linhas) e NULL BETWEEN
    nunca e verdadeiro, elas entravam e nenhuma carga futura conseguia apaga-las.
    """
    df = pd.DataFrame(
        {
            "emissao": ["01/08/2026", "LIXO", "03/08/2026"],
            "valor": ["10", "20", "30"],
        }
    )
    cursor = CursorFalso()

    with pytest.raises(ValueError, match="presas na tabela"):
        date_range(cursor, "Faturamento", df, "emissao", "%d/%m/%Y")

    # Nada pode ter sido escrito: nem DELETE, nem INSERT.
    sqls = " ".join(sql for sql, _ in cursor.executados).upper()
    assert "DELETE" not in sqls
    assert "INSERT" not in sqls


def test_date_range_mostra_exemplos_do_valor_ruim():
    df = pd.DataFrame({"emissao": ["01/08/2026", "31/31/9999"]})
    with pytest.raises(ValueError, match="31/31/9999"):
        date_range(CursorFalso(), "Faturamento", df, "emissao", "%d/%m/%Y")


def test_date_range_aceita_datas_todas_validas():
    df = pd.DataFrame({"emissao": ["01/08/2026", "03/08/2026"], "v": ["1", "2"]})
    cursor = CursorFalso()

    total = date_range(cursor, "Faturamento", df, "emissao", "%d/%m/%Y")

    assert total == 2
    sql, params = next(
        (sql, p) for sql, p in cursor.executados if sql.strip().startswith("DELETE")
    )
    # A janela vai como PARAMETRO, nao concatenada na query.
    assert params[-2:] == ("2026-08-01", "2026-08-03")
    assert "%s" in sql
    # Nenhum '%' literal solto: neste driver isso quebra o execute com
    # "Not enough parameters" quando a query tambem tem placeholder.
    assert "%d/%m/%Y" not in sql
    assert sql.count("%s") == len(params)


def test_replace_nao_derruba_a_tabela_antes_de_conseguir_recriar():
    """Aconteceu em produção: o DROP vinha primeiro, o CREATE falhava por falta
    de privilégio e a tabela sumia do banco. Agora monto numa temporária e só
    troco no fim."""
    from src.load.strategies import replace

    class CursorQueFalhaNoCreate(CursorFalso):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.strip().upper().startswith("CREATE TABLE"):
                raise RuntimeError("sem privilégio")

    cursor = CursorQueFalhaNoCreate()
    with pytest.raises(RuntimeError):
        replace(cursor, "Itens", pd.DataFrame({"a": ["1"]}))

    # Só a temporária pode ter sido derrubada, nunca a tabela real.
    drops = [
        sql for sql, _ in cursor.executados if sql.strip().upper().startswith("DROP")
    ]
    assert all("__nova" in sql for sql in drops), drops


def test_replace_troca_pela_temporaria_no_fim():
    from src.load.strategies import replace

    cursor = CursorFalso()
    replace(cursor, "Itens", pd.DataFrame({"a": ["1"], "b": ["2"]}))

    sqls = [sql.strip() for sql, _ in cursor.executados]
    criou = next(i for i, s in enumerate(sqls) if s.upper().startswith("CREATE TABLE"))
    dropou = next(
        i
        for i, s in enumerate(sqls)
        if s.upper().startswith("DROP TABLE") and "__nova" not in s
    )
    renomeou = next(i for i, s in enumerate(sqls) if s.upper().startswith("RENAME"))

    # cria a nova -> derruba a antiga -> renomeia. Nessa ordem.
    assert criou < dropou < renomeou


def test_date_range_aborta_se_nenhuma_data_e_legivel():
    df = pd.DataFrame({"emissao": ["a", "b"]})
    with pytest.raises(ValueError, match="nenhuma data"):
        date_range(CursorFalso(), "Faturamento", df, "emissao", "%d/%m/%Y")


def test_date_range_exige_coluna_configurada():
    df = pd.DataFrame({"outra": ["01/08/2026"]})
    with pytest.raises(ValueError, match="não achei a coluna"):
        date_range(CursorFalso(), "Faturamento", df, "emissao", "%d/%m/%Y")


def test_parseia_formatos_mistos():
    df = pd.DataFrame({"emissao": ["01/08/2026", "2026-08-02", "2026-08-03 10:00:00"]})
    datas = _parsear_datas(df, "emissao", "%d/%m/%Y")
    assert not datas.isna().any()


# ─────────────────────────────────────────────────────────────
# Validacao de estrategia
# ─────────────────────────────────────────────────────────────
def test_estrategia_desconhecida_falha_claro():
    df = pd.DataFrame({"a": ["1"]})
    with pytest.raises(ValueError, match="desconhecida"):
        executar(CursorFalso(), "T", df, {"estrategia": "inventada"})


def test_date_range_sem_coluna_data_na_config():
    df = pd.DataFrame({"a": ["1"]})
    with pytest.raises(ValueError, match="'coluna_data'"):
        executar(CursorFalso(), "T", df, {"estrategia": "date_range"})


def test_upsert_sem_chaves_na_config():
    df = pd.DataFrame({"a": ["1"]})
    with pytest.raises(ValueError, match="'chaves'"):
        executar(CursorFalso(), "T", df, {"estrategia": "upsert"})


# ─────────────────────────────────────────────────────────────
# Identificadores SQL
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("nome", ["Faturamento", "sku_custo", "_x", "T123"])
def test_identificador_valido(nome):
    assert validar_identificador(nome) == nome


@pytest.mark.parametrize(
    "nome",
    [
        "tabela; DROP TABLE x",
        "nome com espaco",
        "acentuação",
        "",
        "a" * 65,  # limite do MySQL e 64
        "1comeca_com_numero",
    ],
)
def test_identificador_invalido_e_recusado(nome):
    with pytest.raises(ValueError, match="[Ii]dentificador"):
        validar_identificador(nome)


# ─────────────────────────────────────────────────────────────
# CICATRIZ: separador errado parou a base por 3 dias (01-03/08/2026)
# ─────────────────────────────────────────────────────────────
def test_detecta_virgula(tmp_path):
    arquivo = tmp_path / "v.csv"
    arquivo.write_text("sku,ncm,custo\nA,1,2\n", encoding="utf-8")
    assert detectar_separador(arquivo, "utf-8") == ","


def test_detecta_ponto_e_virgula(tmp_path):
    arquivo = tmp_path / "pv.csv"
    arquivo.write_text("sku;ncm;custo\nA;1;2\n", encoding="utf-8")
    assert detectar_separador(arquivo, "utf-8") == ";"


def test_csv_virgula_nao_vira_coluna_unica(tmp_path):
    """O incidente exato: lido com ';', o cabecalho inteiro virava UM nome de
    coluna e o MySQL recusava com 'Identifier name is too long'."""
    arquivo = tmp_path / "estoque.csv"
    arquivo.write_text(
        "sku,descricao,ncm,codigo_deposito,nome_deposito,custo_medio,em_estoque,pedido\n"
        "A1,PNEU,4011,CD01,GIBA,100.50,5,0\n",
        encoding="utf-8",
    )
    df, _ = ler_csv(arquivo)
    assert len(df.columns) == 8
    assert "sku" in df.columns


def test_detecta_utf16_pelo_bom(tmp_path):
    arquivo = tmp_path / "u16.csv"
    arquivo.write_text("a;b\n1;2\n", encoding="utf-16")
    assert detectar_encoding(arquivo) == "utf-16"


def test_le_csv_utf16(tmp_path):
    arquivo = tmp_path / "u16.csv"
    arquivo.write_text("sku;valor\nA;10\n", encoding="utf-16")
    df, _ = ler_csv(arquivo)
    assert list(df.columns) == ["sku", "valor"]


def test_remove_coluna_indice_hash(tmp_path):
    arquivo = tmp_path / "h.csv"
    arquivo.write_text("#;sku;valor\n1;A;10\n", encoding="utf-8")
    df, _ = ler_csv(arquivo)
    assert "#" not in df.columns


# ─────────────────────────────────────────────────────────────
# CICATRIZ: decimais BR sem aspas quebravam o split por virgula
# ─────────────────────────────────────────────────────────────
def test_reconstroi_decimal_br_partido():
    # "399,89" foi partido em "399" e "89" pelo split.
    tokens = _reconstruir_linha("GP RS,196520,399,89,DELINTE", 4)
    assert tokens == ["GP RS", "196520", "399,89", "DELINTE"]


def test_reconstroi_negativo_com_milhar():
    tokens = _reconstruir_linha("X,-1.739,50,Y", 3)
    assert tokens == ["X", "-1.739,50", "Y"]


def test_mantem_texto_livre_com_virgula_e_espaco():
    # Campo PrazoFinalPagamento: "28, 56 e 84 dias" — continuacao comeca com espaco.
    tokens = _reconstruir_linha("A,28, 56 e 84 dias,B", 3)
    assert tokens == ["A", "28, 56 e 84 dias", "B"]


# ─────────────────────────────────────────────────────────────
# CICATRIZ: CSV bem-formado com aspas caia no reconstrutor e
# perdia 33% das linhas em silencio
# ─────────────────────────────────────────────────────────────
def test_csv_com_aspas_nao_cai_no_reconstrutor(tmp_path):
    """O caso real de dataItensVPS.csv.

    Campos citados contendo virgula E aspas escapadas — `"17"" 205 50 ZR17"` —
    faziam o split(",") cru da deteccao ver contagem errada, o arquivo caia no
    reconstrutor heuristico e 984 de 2.936 linhas eram DESCARTADAS com um mero
    aviso. O csv.reader entende aspas e le as linhas corretamente.
    """
    arquivo = tmp_path / "itens.csv"
    arquivo.write_text(
        'ItemCode,ItemName,Preco\n'
        '1096,"17"" 205 50 ZR17 93W XL D7 THUNDER",114.44\n'
        '1097,"18"" 215 35 ZR18, com virgula",99.90\n',
        encoding="utf-8",
    )

    assert not _precisa_reconstrucao(arquivo, "utf-8")

    df, avisos = ler_csv(arquivo)
    assert len(df) == 2, "nenhuma linha pode ser descartada"
    assert avisos == []
    assert df["ItemName"].iloc[0] == '17" 205 50 ZR17 93W XL D7 THUNDER'
    assert df["ItemName"].iloc[1] == '18" 215 35 ZR18, com virgula'


def test_descarte_acima_do_limite_aborta(tmp_path, monkeypatch):
    """Perder muita linha nao pode passar como aviso.

    Ate LIMITE_DESCARTE e o caso conhecido (decimal BR ambiguo). Acima disso a
    premissa esta errada para aquele arquivo — melhor abortar que publicar
    uma base incompleta.
    """
    import src.io.readers as readers

    # Linhas com contagem irrecuperavel: texto livre que o heuristico nao junta.
    linhas = ["a,b,c"] + [f"x{i},y{i} z,w,extra{i},mais{i}" for i in range(20)]
    arquivo = tmp_path / "ruim.csv"
    arquivo.write_text("\n".join(linhas) + "\n", encoding="utf-8")

    monkeypatch.setattr(readers, "_precisa_reconstrucao", lambda *a: True)

    with pytest.raises(ValueError, match="Passou do limite"):
        readers.ler_csv(arquivo)
