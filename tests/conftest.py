"""Blindagem da suite: teste nunca manda e-mail nem escreve o estado real.

Isto existe por causa de um incidente em 20/08/2026. O upload passou a chamar
`base_ok()` a cada arquivo carregado, e os testes de `test_upload.py` que
exercitam `varrer()` comecaram a:

  1. gravar em `.alertas_enviados.json` de verdade, poluindo o estado de
     producao com bases ficticias de 1 e 2 linhas;
  2. abrir conexao SMTP real, porque o `.env` ja estava configurado -- o que
     encheu a caixa de entrada e fez a suite passar de 4s para 52s (timeout
     de 20s por tentativa).

O isolamento fica aqui, e nao em cada arquivo de teste, porque o proximo
teste a tocar um pipeline herda a protecao sem ninguem lembrar dela.
"""

import pytest

from src.io import alerta


@pytest.fixture(autouse=True)
def _alerta_isolado(tmp_path, monkeypatch):
    """Estado num tmp e envio desligado, para TODO teste da suite.

    `test_alerta.py` sobrescreve estas duas coisas com as suas proprias
    fixtures -- ele precisa de um espiao que conte os envios. Aqui o objetivo
    e so garantir que nada escape para o mundo real.
    """
    monkeypatch.setattr(alerta, "ARQUIVO_ESTADO", tmp_path / "alertas_teste.json")
    monkeypatch.setattr(
        alerta,
        "_enviar",
        lambda cfg, assunto, corpo: True,  # finge que enviou, nao abre socket
    )
