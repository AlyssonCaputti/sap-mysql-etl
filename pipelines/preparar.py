"""Lê as exportações do SAP e grava os CSVs tratados.

    python -m pipelines.preparar              # tudo
    python -m pipelines.preparar clientes     # só uma etapa
"""

import logging
import shutil
import sys
from pathlib import Path

import pandas as pd

from config.settings import (
    COLUNAS_POR_LOTE_ITENS,
    CSV_SAIDA,
    ENTRADA_VPS,
    EXTENSOES_DADOS,
    MANUAIS,
    ORIGENS,
    REFERENCIA_TECNICA_CLIENTES,
    SAIDAS,
)
from config.tables import PASTA_MANUAL_PARA_TABELA
from src.io.log import configurar as configurar_log
from src.io.readers import ler_arquivo, ler_excel
from src.transform import clientes as t_clientes
from src.transform import faturamento as t_faturamento
from src.transform import itens as t_itens

log = logging.getLogger(__name__)


def _escrever(df: pd.DataFrame, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destino, **CSV_SAIDA)
    log.info(
        "  gravado: %s (%s linhas x %s colunas)",
        destino.name,
        f"{len(df):,}",
        len(df.columns),
    )


def _avisar(avisos: list[str]) -> None:
    for aviso in avisos:
        log.warning("  %s", aviso)


def preparar_clientes() -> None:
    log.info("clientes...")
    df = ler_excel(ORIGENS["clientes"])

    # Em 01/07/2026 a origem veio com cabeçalho em português. Quando isso
    # acontece eu conserto pela posição, usando a planilha de referência.
    if "CardCode" not in df.columns:
        log.warning("  sem cabeçalho técnico (CardCode) — realinhando por posição.")
        referencia = pd.read_excel(REFERENCIA_TECNICA_CLIENTES, nrows=0)
        df = t_clientes.realinhar_por_posicao(df, list(referencia.columns))

    vendedores = pd.read_excel(ORIGENS["vendedores"])
    resultado, avisos = t_clientes.transformar(df, vendedores)
    _avisar(avisos)
    _escrever(resultado, SAIDAS["clientes"])


def preparar_faturamento() -> None:
    log.info("faturamento...")
    df, avisos_leitura = ler_arquivo(ORIGENS["faturamento"])
    _avisar(avisos_leitura)

    resultado, avisos = t_faturamento.transformar(df)
    _avisar(avisos)
    _escrever(resultado, SAIDAS["faturamento"])


def preparar_itens() -> None:
    log.info("itens...")
    df, avisos_leitura = ler_arquivo(ORIGENS["itens"])
    _avisar(avisos_leitura)

    principal, extras, avisos = t_itens.transformar(df)
    _avisar(avisos)
    _escrever(principal, SAIDAS["itens"])

    # Cada pedaço numa pasta própria, porque o upload cria uma tabela por
    # pasta. A view ItensCompleto junta tudo de volta depois.
    for indice, lote in enumerate(extras, 1):
        destino = (
            ENTRADA_VPS / f"Itens Extra {indice}" / f"dataItensVPS_extra{indice}.csv"
        )
        _escrever(lote, destino)

    log.info(
        "  itens dividido em 1 principal + %s extra(s) de até %s colunas",
        len(extras),
        COLUNAS_POR_LOTE_ITENS,
    )


def preparar_manuais() -> None:
    """Copia o que estiver nas pastas de atualizacao manual pro para_vps.

    Nao transformo nada aqui: essas bases vao cruas pro banco. Só levo o
    arquivo pra pasta certa, e o upload cuida do resto.

    Copio em vez de mover porque a origem é a pasta de rede compartilhada — o
    ETL antigo e outras pessoas ainda olham pra ela.
    """
    if not MANUAIS.is_dir():
        log.warning("não achei a pasta de atualizacao manual: %s", MANUAIS)
        return

    copiados = 0
    for pasta in sorted(p for p in MANUAIS.iterdir() if p.is_dir()):
        tabela = PASTA_MANUAL_PARA_TABELA.get(pasta.name)
        if not tabela:
            # Pasta sem destino declarado fica de fora de propósito: subir
            # tabela nova sem alguem decidir o nome é como se criam as
            # duplicadas em PascalCase.
            continue

        arquivos = sorted(
            a for a in pasta.iterdir() if a.suffix.lower() in EXTENSOES_DADOS
        )
        if not arquivos:
            continue

        destino_dir = ENTRADA_VPS / pasta.name
        destino_dir.mkdir(parents=True, exist_ok=True)
        for arquivo in arquivos:
            destino = destino_dir / arquivo.name
            # Sem cópia se o que está lá já é o mesmo arquivo: evita refazer
            # carga de base que ninguém atualizou.
            if destino.exists() and destino.stat().st_mtime >= arquivo.stat().st_mtime:
                log.info("  (sem mudança) %s/%s", pasta.name, arquivo.name)
                continue
            shutil.copy2(arquivo, destino)
            log.info("  %s/%s -> `%s`", pasta.name, arquivo.name, tabela)
            copiados += 1

    log.info("manuais: %s arquivo(s) na fila do upload", copiados)


ETAPAS = {
    "clientes": preparar_clientes,
    "faturamento": preparar_faturamento,
    "itens": preparar_itens,
    "manuais": preparar_manuais,
}


def main(argumentos: list[str] | None = None) -> int:
    configurar_log("preparar.log")
    argumentos = argumentos if argumentos is not None else sys.argv[1:]

    escolhidas = argumentos or list(ETAPAS)
    invalidas = [e for e in escolhidas if e not in ETAPAS]
    if invalidas:
        log.error("Não conheço a etapa %s. Tenho: %s", invalidas, ", ".join(ETAPAS))
        return 2

    # Uma etapa que falha não impede as outras, mas o exit code acusa.
    falhas = 0
    for nome in escolhidas:
        try:
            ETAPAS[nome]()
        except Exception as erro:
            falhas += 1
            log.error("FALHA em %s: %s", nome, erro)
            log.debug("traceback de %s", nome, exc_info=True)

    if falhas:
        log.error("%s etapa(s) falharam.", falhas)
        return 1

    log.info("Preparação concluída.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
