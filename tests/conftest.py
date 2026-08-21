"""Blindagem da suite: teste nunca manda e-mail nem escreve o estado real.

Em 20/08/2026 os testes que exercitam varrer() comecaram a mandar e-mail de
verdade, porque o upload passou a chamar base_ok() e o .env estava
configurado. A suite foi de 4s para 52s de timeout de SMTP.

Ponho aqui e nao em cada arquivo para o proximo teste que tocar um pipeline
herdar a protecao sem ninguem lembrar dela.
"""

import pytest

from src.io import alerta


@pytest.fixture(autouse=True)
def _alerta_isolado(tmp_path, monkeypatch):
    """Estado num tmp e envio desligado, para todo teste da suite.

    O test_alerta.py sobrescreve isto com as fixtures dele, que precisam de um
    espiao contando os envios. Aqui so garanto que nada escapa.
    """
    monkeypatch.setattr(alerta, "ARQUIVO_ESTADO", tmp_path / "alertas_teste.json")
    monkeypatch.setattr(alerta, "_enviar", lambda cfg, assunto, corpo: True)
