"""Testes da vigilancia do faturamento horario: origem parada e registro.

Os dois furos que estes testes fecham foram apurados em 04/09/2026 e nenhum
levanta excecao — sao falhas SILENCIOSAS, o pior tipo:

  1. a origem congelou (03/09 16:10) e o pipeline seguiu saindo calado;
  2. as ~24 rodadas horarias nao apareciam em `etl_execucoes`, entao o painel
     nao tinha como responder "como rodou o faturamento das 15h".
"""

import datetime

import pytest

from pipelines import faturamento_horario as fh


class CursorFalso:
    def __init__(self):
        self.chamadas = []

    def execute(self, sql, params=None):
        self.chamadas.append((sql, params))

    def close(self):
        pass


class ConexaoFalsa:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _mtime(agora, horas_atras):
    return (agora - datetime.timedelta(hours=horas_atras)).timestamp()


# ── Origem congelada ────────────────────────────────────────────────────

def test_origem_parada_em_dia_util_avisa(monkeypatch):
    """CICATRIZ (04/09/2026): origem parada desde 03/09 16:10, ninguem soube.

    Hash igual = nada a carregar = `return 0` calado. Sem excecao nao havia
    alerta, e o painel mostrava a ultima carga como bem-sucedida (porque foi).
    """
    avisos = []
    monkeypatch.setattr(
        fh, "falhou", lambda *a, **kw: avisos.append((a, kw)) or True
    )
    # Sexta-feira, 14h.
    agora = datetime.datetime(2026, 9, 4, 14, 0)
    assert fh._conferir_origem_parada(_mtime(agora, 22), "03/09 16:10", agora) is True
    assert len(avisos) == 1
    (pipeline, texto), kwargs = avisos[0]
    assert "22h" in texto
    # O texto tem que apontar para QUEM PUBLICA, nao para a carga: diagnostico
    # errado manda cacar problema no lugar errado.
    assert "QUEM PUBLICA" in texto
    # Chave fixa, senao as horas mudariam a chave e furariam o silencio.
    assert kwargs["chave"] == "origem_parada"


def test_origem_recente_nao_avisa(monkeypatch):
    chamou = []
    monkeypatch.setattr(fh, "falhou", lambda *a, **kw: chamou.append(1) or True)
    agora = datetime.datetime(2026, 9, 4, 14, 0)
    assert fh._conferir_origem_parada(_mtime(agora, 2), "04/09 12:00", agora) is False
    assert chamou == []


def test_origem_parada_no_fim_de_semana_nao_avisa(monkeypatch):
    """A origem nao e republicada no fim de semana.

    Avisar todo sabado treinaria o leitor a ignorar o alerta — e ai ele perde
    o de segunda, que e real.
    """
    chamou = []
    monkeypatch.setattr(fh, "falhou", lambda *a, **kw: chamou.append(1) or True)
    sabado = datetime.datetime(2026, 9, 5, 14, 0)
    assert sabado.weekday() == 5
    assert fh._conferir_origem_parada(_mtime(sabado, 40), "04/09 10:00", sabado) is False
    assert chamou == []


def test_origem_parada_de_madrugada_nao_avisa(monkeypatch):
    # O SAP nao publica as 3h; avisar nesse horario e ruido.
    chamou = []
    monkeypatch.setattr(fh, "falhou", lambda *a, **kw: chamou.append(1) or True)
    madrugada = datetime.datetime(2026, 9, 4, 3, 0)
    assert fh._conferir_origem_parada(_mtime(madrugada, 12), "03/09 15:00", madrugada) is False
    assert chamou == []


@pytest.mark.parametrize("horas,espera_aviso", [(4, False), (6, True)])
def test_limite_de_horas_da_origem(monkeypatch, horas, espera_aviso):
    # A origem e HORARIA: 5h paradas em dia util ja e anomalia.
    monkeypatch.setattr(fh, "falhou", lambda *a, **kw: True)
    agora = datetime.datetime(2026, 9, 4, 15, 0)
    resultado = fh._conferir_origem_parada(_mtime(agora, horas), "x", agora)
    assert resultado is espera_aviso


# ── Registro em etl_execucoes ───────────────────────────────────────────

def test_registrar_rodada_grava_as_duas_tabelas(monkeypatch):
    """CICATRIZ: sem isto o faturamento era invisivel em `etl_execucoes`.

    Ate 04/09/2026 a tabela tinha ~1 linha/dia (so o ETL diario das 10:10) e
    as rodadas horarias — as que mexem na tabela que o comercial olha — nao
    apareciam em lugar nenhum.
    """
    cursor = CursorFalso()
    con = ConexaoFalsa(cursor)
    monkeypatch.setattr(fh, "conexao", lambda: con)

    fh._registrar_rodada(datetime.datetime(2026, 9, 4, 15, 5), linhas=13_688)

    sql, params = cursor.chamadas[0]
    assert "INSERT INTO etl_execucoes" in sql
    assert params[2] == "TUDO OK"
    # `bases` e o que o resumo usa para separar rodada de faturamento do ETL
    # diario — as duas tabelas tem que estar la.
    assert "Faturamento=13688" in params[8]
    assert "faturamento_full=13688" in params[8]
    assert con.commits == 1


def test_registrar_rodada_com_falha_marca_amarelo(monkeypatch):
    cursor = CursorFalso()
    monkeypatch.setattr(fh, "conexao", lambda: ConexaoFalsa(cursor))

    fh._registrar_rodada(
        datetime.datetime(2026, 9, 4, 15, 5),
        linhas=0,
        falhas=1,
        erros="RuntimeError: faturamento_full falhou",
    )

    _sql, params = cursor.chamadas[0]
    assert params[2] == "RODOU COM ERROS"
    assert params[6] == 1
    assert "faturamento_full falhou" in params[7]


def test_registrar_rodada_nunca_derruba_a_carga(monkeypatch):
    """Regra de ouro do src/io/execucoes.py, valendo tambem aqui.

    Quando esta funcao roda, a carga JA foi commitada. Levantar aqui
    transformaria uma carga boa em falha — e faria o pipeline devolver exit
    code != 0, o que a tarefa agendada reportaria como erro.
    """
    def explode():
        raise RuntimeError("MySQL fora do ar")

    monkeypatch.setattr(fh, "conexao", explode)
    # Nao levanta: so loga.
    fh._registrar_rodada(datetime.datetime(2026, 9, 4, 15, 5), linhas=100)


def test_rodada_das_15h_fica_localizavel_pelo_inicio(monkeypatch):
    # O resumo acha a rodada das 15h por `inicio.hour == 15`, entao o `inicio`
    # gravado precisa ser o horario real da rodada.
    cursor = CursorFalso()
    monkeypatch.setattr(fh, "conexao", lambda: ConexaoFalsa(cursor))
    inicio = datetime.datetime(2026, 9, 4, 15, 5, 20)
    fh._registrar_rodada(inicio, linhas=13_688)
    _sql, params = cursor.chamadas[0]
    assert params[0] == inicio
    assert params[0].hour == 15
