"""Configuração do log, igual pros cinco pipelines.

Estava copiada em cada um. Só o nome do arquivo mudava.
"""

import logging
import sys

from config.settings import PASTA_LOGS


def configurar(nome: str) -> None:
    """Log em arquivo e no console. `nome` é o .log dentro de logs/."""
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(PASTA_LOGS / nome, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
