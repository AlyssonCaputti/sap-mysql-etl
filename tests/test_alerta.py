"""Testes do alerta por e-mail.

Rodam sem SMTP e sem rede: o envio e trocado por um espiao. O que se testa
aqui e a decisao de MANDAR ou NAO, que e onde mora o risco -- alerta demais
vira ruido ignorado, alerta de menos e a falha invisivel de novo.
"""

import time

import pytest

from src.io import alerta

# Guardo a funcao real antes de qualquer mock: o teste de SMTP fora do ar
# precisa dela para exercitar o caminho de envio de verdade.
_ENVIAR_ORIGINAL = alerta._enviar


@pytest.fixture(autouse=True)
def isolar(tmp_path, monkeypatch):
    """Estado num tmp e SMTP configurado de mentira, para todo teste."""
    monkeypatch.setattr(alerta, "ARQUIVO_ESTADO", tmp_path / "alertas.json")
    monkeypatch.setenv("ALERTA_SMTP_HOST", "smtp.invalido.local")
    monkeypatch.setenv("ALERTA_PARA", "eu@empresa.com")
    monkeypatch.setenv("ALERTA_SMTP_USUARIO", "etl@empresa.com")


@pytest.fixture
def enviados(monkeypatch):
    """Intercepta o envio. Cada item e (assunto, corpo)."""
    caixa = []

    def espiao(cfg, assunto, corpo):
        caixa.append((assunto, corpo))
        return True

    monkeypatch.setattr(alerta, "_enviar", espiao)
    return caixa


def test_manda_na_primeira_falha(enviados):
    assert alerta.falhou("upload", "Divergencia na carga de `Itens`")
    assert len(enviados) == 1
    assunto, corpo = enviados[0]
    assert "upload" in assunto
    assert "Itens" in corpo


def test_segunda_falha_igual_e_suprimida(enviados):
    alerta.falhou("upload", "mesmo erro de sempre")
    alerta.falhou("upload", "mesmo erro de sempre")
    alerta.falhou("upload", "mesmo erro de sempre")
    assert len(enviados) == 1, "a janela de silencio deve segurar as repeticoes"


def test_erro_diferente_fura_o_silencio(enviados):
    """Erro NOVO nao pode ficar escondido atras de um antigo ainda na janela."""
    alerta.falhou("upload", "falha na tabela Itens")
    alerta.falhou("upload", "falha na tabela Clientes")
    assert len(enviados) == 2


def test_mesmo_erro_em_pipeline_diferente_manda(enviados):
    alerta.falhou("upload", "timeout no banco")
    alerta.falhou("faturamento_horario", "timeout no banco")
    assert len(enviados) == 2


def test_volta_a_mandar_quando_a_janela_expira(enviados, monkeypatch):
    alerta.falhou("upload", "erro persistente")
    assert len(enviados) == 1

    futuro = time.time() + alerta.JANELA_SILENCIO_S + 1
    monkeypatch.setattr(alerta.time, "time", lambda: futuro)

    alerta.falhou("upload", "erro persistente")
    assert len(enviados) == 2


def test_conta_quantas_vezes_suprimiu(enviados, monkeypatch):
    """O e-mail seguinte diz quantas repeticoes ficaram no meio."""
    alerta.falhou("upload", "erro teimoso")
    for _ in range(5):
        alerta.falhou("upload", "erro teimoso")

    futuro = time.time() + alerta.JANELA_SILENCIO_S + 1
    monkeypatch.setattr(alerta.time, "time", lambda: futuro)
    alerta.falhou("upload", "erro teimoso")

    assert "5x" in enviados[-1][1]


def test_sem_config_nao_manda_e_nao_quebra(enviados, monkeypatch):
    """Quem nao preenche o .env roda o ETL igual, sem alerta."""
    monkeypatch.delenv("ALERTA_SMTP_HOST")
    assert alerta.falhou("upload", "erro") is False
    assert enviados == []


def test_smtp_fora_do_ar_nao_levanta(monkeypatch):
    """A regra de ouro: falhar no alerta nunca derruba a carga.

    Este e o unico teste que exercita o `_enviar` de verdade, entao desfaz o
    mock do conftest -- so o socket e trocado por um que explode.
    """
    monkeypatch.setattr(alerta, "_enviar", _ENVIAR_ORIGINAL)

    def explode(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(alerta.smtplib, "SMTP", explode)
    assert alerta.falhou("upload", "erro qualquer") is False


def test_estado_corrompido_nao_levanta(enviados):
    """JSON pela metade (maquina caiu no meio) nao pode quebrar o pipeline."""
    alerta.ARQUIVO_ESTADO.write_text("{lixo", encoding="utf-8")
    assert alerta.falhou("upload", "erro") is True


def test_normalizou_so_avisa_se_havia_falha(enviados):
    assert alerta.normalizou("upload") is False
    assert enviados == []

    alerta.falhou("upload", "erro")
    enviados.clear()

    assert alerta.normalizou("upload") is True
    assert "normalizado" in enviados[0][0]


def test_normalizou_limpa_o_estado(enviados):
    """Depois de normalizar, a proxima falha volta a avisar na hora."""
    alerta.falhou("upload", "erro")
    alerta.normalizou("upload")
    enviados.clear()

    assert alerta.falhou("upload", "erro") is True


def test_normalizou_nao_mexe_em_outro_pipeline(enviados):
    alerta.falhou("upload", "erro do upload")
    alerta.falhou("sku_custo", "erro do sku")
    enviados.clear()

    alerta.normalizou("upload")
    # sku_custo continua pendente, entao a repeticao dele segue suprimida.
    assert alerta.falhou("sku_custo", "erro do sku") is False


def test_marca_como_avisado_mesmo_se_o_envio_falhar(monkeypatch):
    """Com o SMTP fora, nao adianta tentar de novo a cada 5 min.

    Sem isso o sku_custo pagaria o timeout de 20s em toda rodada.
    """
    tentativas = []

    def falha_no_envio(cfg, assunto, corpo):
        tentativas.append(assunto)
        return False

    monkeypatch.setattr(alerta, "_enviar", falha_no_envio)

    alerta.falhou("sku_custo", "banco fora do ar")
    alerta.falhou("sku_custo", "banco fora do ar")
    alerta.falhou("sku_custo", "banco fora do ar")

    assert len(tentativas) == 1


def test_contexto_aparece_no_corpo(enviados):
    alerta.falhou(
        "upload",
        "falhou",
        contexto={"Falhas": 3, "Tabelas": "Itens, Clientes"},
    )
    corpo = enviados[0][1]
    assert "Falhas" in corpo and "3" in corpo
    assert "Itens, Clientes" in corpo


def test_erro_vazio_nao_quebra(enviados):
    """falhou() e chamado no meio de um caminho de erro: nao pode gerar outro."""
    assert alerta.falhou("upload", "") is True


# ── Aviso por base: qual parou e desde quando ────────────────


def test_base_falhou_diz_qual_base(enviados):
    alerta.base_falhou("Itens", "coluna sumiu", pipeline="upload")
    assunto, corpo = enviados[0]
    assert "Itens" in assunto
    assert "Base" in corpo and "Itens" in corpo


def test_base_nunca_carregada(enviados):
    alerta.base_falhou("NovaBase", "erro")
    assert "nunca carregou" in enviados[0][1]


def test_mostra_ha_quanto_tempo_parou(enviados):
    """O ponto do pedido: o e-mail diz desde quando a base esta parada."""
    alerta.base_ok("Itens", 2933)
    alerta.base_falhou("Itens", "quebrou")
    corpo = enviados[0][1]
    assert "ultima carga OK" in corpo
    assert "2,933 linhas" in corpo


def test_falha_desde_nao_se_move_entre_rodadas(enviados, monkeypatch):
    """Duas horas falhando devem mostrar o marco original, nao 'agora'."""
    alerta.base_ok("Itens", 100)
    alerta.base_falhou("Itens", "erro")
    marco = alerta._ler_estado()["base::Itens"]["falhando_desde"]

    futuro = time.time() + alerta.JANELA_SILENCIO_S + 1
    monkeypatch.setattr(alerta.time, "time", lambda: futuro)
    alerta.base_falhou("Itens", "erro")

    assert alerta._ler_estado()["base::Itens"]["falhando_desde"] == marco


def test_bases_diferentes_avisam_separado(enviados):
    alerta.base_falhou("Itens", "erro A")
    alerta.base_falhou("Clientes", "erro B")
    assert len(enviados) == 2
    assert "Itens" in enviados[0][0] and "Clientes" in enviados[1][0]


def test_mesma_base_respeita_a_janela(enviados):
    alerta.base_falhou("Itens", "erro")
    alerta.base_falhou("Itens", "outro texto de erro do driver")
    assert len(enviados) == 1, "chave e a base, nao a mensagem"


def test_base_ok_avisa_recuperacao(enviados):
    alerta.base_falhou("Itens", "erro")
    enviados.clear()
    alerta.base_ok("Itens", 2933)
    assert len(enviados) == 1
    assert "voltou" in enviados[0][0]


def test_base_ok_silencioso_quando_nao_falhava(enviados):
    alerta.base_ok("Itens", 2933)
    assert enviados == []


def test_apos_recuperar_nova_falha_avisa_na_hora(enviados):
    """Sem limpar o estado do alerta, a falha seguinte ficaria muda."""
    alerta.base_falhou("Itens", "erro")
    alerta.base_ok("Itens", 10)
    enviados.clear()
    assert alerta.base_falhou("Itens", "erro") is True


def test_base_ok_nao_quebra_sem_config(monkeypatch):
    monkeypatch.delenv("ALERTA_SMTP_HOST")
    alerta.base_falhou("Itens", "erro")
    alerta.base_ok("Itens", 5)  # nao pode levantar
