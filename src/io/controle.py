"""Controle de execução: hash pra saber se mudou, lock pra não rodar duas vezes.

Usado pelos pipelines que rodam de tempos em tempos (faturamento de hora em
hora, sku_custo a cada 5 min). O diário não precisa: roda uma vez e pronto.
"""

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path


def hash_de(caminho: Path) -> str:
    """SHA-256 do arquivo, lido em blocos pra não carregar 100 MB na memória."""
    digest = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for bloco in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


class AindaEscrevendo(Exception):
    """O arquivo mudou de tamanho enquanto eu olhava."""


def esperar_estabilizar(
    caminho: Path, tentativas: int = 6, intervalo: float = 5.0
) -> None:
    """Espera o arquivo parar de crescer antes de ler.

    A origem publica direto na pasta de rede. Se eu ler no meio da escrita,
    pego um CSV truncado — e um arquivo pela metade passa por todas as
    validações, porque as linhas que chegaram estão bem formadas. O resultado
    seria uma carga silenciosamente incompleta.

    Confiro tamanho e data de modificação até ficarem iguais duas vezes
    seguidas. Se não estabilizar, levanto e quem chamou pula a rodada — na
    próxima hora tenta de novo.
    """
    anterior = None
    for _ in range(tentativas):
        st = caminho.stat()
        atual = (st.st_size, st.st_mtime)
        if atual == anterior:
            return
        anterior = atual
        time.sleep(intervalo)

    raise AindaEscrevendo(
        f"{caminho.name} continuou mudando por "
        f"{int(tentativas * intervalo)}s — a origem deve estar escrevendo"
    )


def ler_estado(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_estado(caminho: Path, estado: dict) -> None:
    """Grava num temporário e troca no fim.

    O write direto trunca antes de escrever: se a máquina cai no meio, sobra
    um JSON pela metade, o ler_estado devolve {} e a rodada seguinte acha que
    a origem mudou — recarrega tudo e refaz o faturamento_full à toa.
    """
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporario, caminho)


class JaEstaRodando(Exception):
    """Outro processo tem o lock."""


class Lock:
    """Trava por arquivo, pra duas execuções não carregarem a mesma tabela.

    Uso:
        with Lock(RAIZ / ".lock_faturamento"):
            ...

    Se já houver alguém rodando, levanta JaEstaRodando e quem chamou decide
    (aqui os pipelines saem calados, porque na próxima hora tenta de novo).

    Lock velho é ignorado: se o processo morreu sem limpar, depois de
    `expira_em` segundos o arquivo é tratado como abandonado. Sem isso, um
    crash deixaria o pipeline parado pra sempre.
    """

    def __init__(self, caminho: Path, expira_em: int = 3600):
        self.caminho = Path(caminho)
        self.expira_em = expira_em
        self._meu = False

    def __enter__(self):
        if self.caminho.exists():
            idade = time.time() - self.caminho.stat().st_mtime
            if idade < self.expira_em:
                dono = self.caminho.read_text(encoding="utf-8", errors="replace")
                raise JaEstaRodando(
                    f"já tem uma execução em andamento desde "
                    f"{int(idade // 60)} min atrás ({dono.strip()})"
                )
            # Passou do tempo: o dono provavelmente morreu.
            self.caminho.unlink(missing_ok=True)

        self.caminho.write_text(
            f"pid={os.getpid()} desde={datetime.now():%Y-%m-%d %H:%M:%S}",
            encoding="utf-8",
        )
        self._meu = True
        return self

    def __exit__(self, *_):
        if self._meu:
            self.caminho.unlink(missing_ok=True)
        return False
