"""Testes do controle de execução: hash e lock.

São o que impede duas cargas simultâneas na mesma tabela e o retrabalho de
reprocessar 255 mil linhas quando nada mudou.
"""

import os
import time

import pytest

from src.io.controle import (
    AindaEscrevendo,
    JaEstaRodando,
    Lock,
    esperar_estabilizar,
    hash_de,
    ler_estado,
    salvar_estado,
)


# ─────────────────────────────────────────────────────────────
# Hash
# ─────────────────────────────────────────────────────────────
def test_hash_muda_quando_conteudo_muda(tmp_path):
    arquivo = tmp_path / "x.csv"
    arquivo.write_text("a,b\n1,2\n", encoding="utf-8")
    antes = hash_de(arquivo)

    arquivo.write_text("a,b\n1,3\n", encoding="utf-8")
    assert hash_de(arquivo) != antes


def test_hash_igual_para_conteudo_igual(tmp_path):
    """O que decide é o conteúdo, não a data de modificação.

    A origem reescreve o arquivo toda hora mesmo sem faturamento novo; se eu
    olhasse o mtime, reprocessaria 255 mil linhas à toa.
    """
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("sku;valor\nA;10\n", encoding="utf-8")
    time.sleep(0.01)
    b.write_text("sku;valor\nA;10\n", encoding="utf-8")

    assert hash_de(a) == hash_de(b)


# ─────────────────────────────────────────────────────────────
# Estado
# ─────────────────────────────────────────────────────────────
def test_estado_ida_e_volta(tmp_path):
    arquivo = tmp_path / "estado.json"
    salvar_estado(arquivo, {"hash": "abc", "linhas": 255439})
    assert ler_estado(arquivo)["linhas"] == 255439


def test_estado_ausente_ou_corrompido_devolve_vazio(tmp_path):
    assert ler_estado(tmp_path / "naoexiste.json") == {}

    ruim = tmp_path / "ruim.json"
    ruim.write_text("{isso nao e json", encoding="utf-8")
    assert ler_estado(ruim) == {}


def test_estado_nao_deixa_temporario_para_tras(tmp_path):
    """Gravo num .tmp e troco. O .tmp não pode sobrar na pasta."""
    arquivo = tmp_path / "estado.json"
    salvar_estado(arquivo, {"hash": "abc"})

    assert list(p.name for p in tmp_path.iterdir()) == ["estado.json"]


def test_estado_anterior_sobrevive_a_falha_na_escrita(tmp_path, monkeypatch):
    """Se a escrita morre no meio, quero o estado de antes, não meia linha.

    Sem a troca atômica o arquivo ficava truncado, ler_estado devolvia {} e a
    rodada seguinte recarregava tudo à toa.
    """
    arquivo = tmp_path / "estado.json"
    salvar_estado(arquivo, {"hash": "bom", "linhas": 256352})

    def morrer(*_, **__):
        raise OSError("disco cheio")

    monkeypatch.setattr("src.io.controle.os.replace", morrer)
    with pytest.raises(OSError):
        salvar_estado(arquivo, {"hash": "novo", "linhas": 1})

    assert ler_estado(arquivo) == {"hash": "bom", "linhas": 256352}


def test_estado_sobrescreve_o_anterior(tmp_path):
    arquivo = tmp_path / "estado.json"
    salvar_estado(arquivo, {"hash": "primeiro"})
    salvar_estado(arquivo, {"hash": "segundo"})

    assert ler_estado(arquivo)["hash"] == "segundo"


# ─────────────────────────────────────────────────────────────
# Lock
# ─────────────────────────────────────────────────────────────
def test_lock_impede_segunda_execucao(tmp_path):
    trava = tmp_path / ".lock"
    with Lock(trava):
        with pytest.raises(JaEstaRodando):
            with Lock(trava):
                pass


def test_lock_e_liberado_no_fim(tmp_path):
    trava = tmp_path / ".lock"
    with Lock(trava):
        assert trava.exists()
    assert not trava.exists()

    # e dá pra pegar de novo
    with Lock(trava):
        pass


def test_lock_e_liberado_mesmo_com_erro(tmp_path):
    """Exceção dentro do bloco não pode deixar o pipeline travado pra sempre."""
    trava = tmp_path / ".lock"
    with pytest.raises(RuntimeError):
        with Lock(trava):
            raise RuntimeError("algo quebrou")
    assert not trava.exists()


def test_lock_velho_e_ignorado(tmp_path):
    """Se o processo morreu sem limpar, o lock não pode travar tudo pra sempre."""
    trava = tmp_path / ".lock"
    trava.write_text("pid=999 (processo morto)", encoding="utf-8")

    antigo = time.time() - 7200  # 2 horas atrás
    import os

    os.utime(trava, (antigo, antigo))

    # expira em 1h: este tem 2h, então deve ser tratado como abandonado
    with Lock(trava, expira_em=3600):
        assert trava.exists()


def test_lock_recente_de_outro_processo_e_respeitado(tmp_path):
    trava = tmp_path / ".lock"
    trava.write_text("pid=999", encoding="utf-8")

    with pytest.raises(JaEstaRodando, match="em andamento"):
        with Lock(trava, expira_em=3600):
            pass


# ─────────────────────────────────────────────────────────────
# Arquivo estável: a origem publica direto na pasta de rede
# ─────────────────────────────────────────────────────────────
def test_arquivo_parado_passa_direto(tmp_path):
    arquivo = tmp_path / "estavel.csv"
    arquivo.write_text("a,b\n1,2\n", encoding="utf-8")

    inicio = time.time()
    esperar_estabilizar(arquivo, tentativas=3, intervalo=0.01)
    assert time.time() - inicio < 1


def test_arquivo_crescendo_levanta(tmp_path):
    """Se o arquivo não para de mudar, é melhor pular a rodada do que ler
    metade dele — um CSV truncado passa por todas as validações."""
    arquivo = tmp_path / "crescendo.csv"
    arquivo.write_text("a,b\n", encoding="utf-8")

    tamanho = [100]

    class StatFalso:
        def __init__(self):
            tamanho[0] += 100
            self.st_size = tamanho[0]
            self.st_mtime = tamanho[0]

    original = type(arquivo).stat
    type(arquivo).stat = lambda self, **k: StatFalso()
    try:
        with pytest.raises(AindaEscrevendo, match="continuou mudando"):
            esperar_estabilizar(arquivo, tentativas=3, intervalo=0.01)
    finally:
        type(arquivo).stat = original


def test_arquivo_que_para_de_crescer_e_aceito(tmp_path):
    arquivo = tmp_path / "para.csv"
    arquivo.write_text("a,b\n", encoding="utf-8")

    chamadas = [0]

    class StatFalso:
        def __init__(self):
            chamadas[0] += 1
            # cresce na 1a leitura, depois estabiliza
            self.st_size = 100 if chamadas[0] == 1 else 200
            self.st_mtime = self.st_size

    original = type(arquivo).stat
    type(arquivo).stat = lambda self, **k: StatFalso()
    try:
        esperar_estabilizar(arquivo, tentativas=5, intervalo=0.01)
    finally:
        type(arquivo).stat = original
