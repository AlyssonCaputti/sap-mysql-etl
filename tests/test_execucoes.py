"""Testes do registro em `etl_execucoes`.

O ponto central: registrar NUNCA pode derrubar a carga. Quando estes testes
rodam, a carga já foi commitada — perder o registro é aceitável, perder o dado
não é.
"""

from datetime import datetime

from src.io.execucoes import (
    STATUS_COM_ERROS,
    STATUS_OK,
    STATUS_TRAVOU,
    classificar,
    formatar_bases,
    registrar,
)


class CursorFalso:
    """Guarda o que foi executado, com os parametros."""

    def __init__(self, erro=None):
        self.chamadas = []
        self.erro = erro

    def execute(self, sql, params=None):
        if self.erro:
            raise self.erro
        self.chamadas.append((sql, params))


def test_classificar_sem_falha_e_tudo_ok():
    assert classificar(0) == STATUS_OK
    assert classificar(0) == ("TUDO OK", "verde")


def test_classificar_com_falha_e_amarelo():
    assert classificar(3) == STATUS_COM_ERROS
    assert classificar(1) == ("RODOU COM ERROS", "amarelo")


def test_classificar_travado_ignora_contagem_de_falhas():
    # Travar tem precedencia: nao importa quantos arquivos falharam antes.
    assert classificar(0, travou=True) == STATUS_TRAVOU
    assert classificar(9, travou=True) == ("TRAVOU NO MEIO", "laranja")


def test_formatar_bases_usa_o_formato_do_pipeline_antigo():
    # O painel espera exatamente 'Nome=N;Nome=N', ordenado.
    assert formatar_bases({"Itens": 1, "Clientes": 2}) == "Clientes=2;Itens=1"


def test_formatar_bases_vazio_nao_quebra():
    assert formatar_bases({}) == ""


def test_registrar_grava_com_os_campos_esperados():
    cursor = CursorFalso()
    ok = registrar(
        cursor,
        inicio=datetime(2026, 8, 18, 10, 0, 0),
        falhas=0,
        arquivos=5,
        linhas=26_287,
        bases={"Itens": 1},
    )

    assert ok is True
    sql, params = cursor.chamadas[0]
    assert "INSERT INTO etl_execucoes" in sql
    assert params[2] == "TUDO OK"
    assert params[3] == "verde"
    assert params[4] == 5
    assert params[5] == 26_287
    assert params[8] == "Itens=1"


def test_registrar_nunca_levanta_quando_a_tabela_nao_existe():
    """CICATRIZ (por construcao): o registro e observacao, nao trabalho.

    Se etl_execucoes nao existir no destino, a carga ja commitada tem que
    sobreviver. Levantar aqui transformaria uma carga boa em falha.
    """
    cursor = CursorFalso(erro=RuntimeError("Table 'etl_execucoes' doesn't exist"))

    ok = registrar(
        cursor,
        inicio=datetime(2026, 8, 18, 10, 0, 0),
        falhas=0,
        arquivos=5,
        linhas=100,
    )

    assert ok is False  # avisou que nao deu, sem estourar


def test_registrar_corta_erros_gigantes_antes_de_enviar():
    """`erros` recebe texto de fora (mensagem de excecao por arquivo).

    Uma carga com muitos arquivos ruins geraria um texto enorme; sem corte, o
    INSERT falha e o registro inteiro se perde — justo quando ele mais importa,
    porque e a execucao que deu errado.
    """
    cursor = CursorFalso()
    registrar(
        cursor,
        inicio=datetime(2026, 8, 18, 10, 0, 0),
        falhas=500,
        arquivos=1,
        linhas=1,
        erros="x" * 100_000,
    )
    _, params = cursor.chamadas[0]
    assert len(params[7]) <= 60_000, "erros nao foi cortado"


def test_registrar_corta_bases_gigantes_antes_de_enviar():
    # Uma pasta por tabela: muitas pastas geram um `bases` longo.
    cursor = CursorFalso()
    registrar(
        cursor,
        inicio=datetime(2026, 8, 18, 10, 0, 0),
        falhas=0,
        arquivos=1,
        linhas=1,
        bases={f"Tabela{i:05d}": 1 for i in range(6000)},
    )
    _, params = cursor.chamadas[0]
    assert len(params[8]) <= 60_000, "bases nao foi cortado"


def test_resumo_cabe_na_coluna_mesmo_com_numeros_grandes():
    # `resumo` e varchar(255). Com volumes reais precisa continuar cabendo.
    cursor = CursorFalso()
    registrar(
        cursor,
        inicio=datetime(2026, 8, 18, 10, 0, 0),
        falhas=999,
        arquivos=999_999,
        linhas=99_999_999_999,
    )
    _, params = cursor.chamadas[0]
    assert len(params[9]) <= 255


def test_registrar_com_falhas_marca_amarelo_e_guarda_o_erro():
    cursor = CursorFalso()
    registrar(
        cursor,
        inicio=datetime(2026, 8, 18, 10, 0, 0),
        falhas=2,
        arquivos=3,
        linhas=50,
        erros="a.csv: encoding\nb.csv: vazio",
    )
    _, params = cursor.chamadas[0]
    assert params[2] == "RODOU COM ERROS"
    assert params[6] == 2
    assert "a.csv" in params[7]
