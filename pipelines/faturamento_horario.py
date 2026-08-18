"""Atualiza Faturamento + faturamento_full de hora em hora.

COMO FUNCIONA
A origem publica o CSV inteiro (100 MB) toda hora, mas o que muda é quase só o
mês corrente — 1,3% das linhas. Então:

  1. leio o CSV da rede e trato;
  2. gravo num Parquet particionado por mês (dados/faturamento_parquet/);
  3. carrego no MySQL SÓ os últimos MESES_JANELA meses.

O date_range apaga a janela que está no DataFrame e reinsere. Passando 2 meses,
ele mexe só neles e o histórico fica onde está.

Por que 2 meses e não 1: nota retroativa. Se a origem lançar hoje uma nota com
emissão do mês passado, uma janela de 1 mês não a pegaria.

O faturamento_full é refeito INTEIRO, não só a janela. A regra da ilha define a
carteira do cliente pela última compra da marca foco em TODO o histórico — com
só a janela, quem comprou há meses perderia a ilha.

Proteções: comparo o hash antes de trabalhar, espero o arquivo parar de crescer
(a origem escreve direto na rede) e uso lock pra não rodar duas vezes junto.

    python -m pipelines.faturamento_horario            # roda se mudou
    python -m pipelines.faturamento_horario --forcar   # roda de qualquer jeito
    python -m pipelines.faturamento_horario --status   # só olha, não escreve
    python -m pipelines.faturamento_horario --tudo     # carrega o histórico todo
"""

import argparse
import logging
import sys
import time

import pandas as pd

from config.settings import CSV_SAIDA, DADOS, ORIGENS, PASTA_LOGS, RAIZ, SAIDAS
from src.io.controle import (
    AindaEscrevendo,
    JaEstaRodando,
    Lock,
    esperar_estabilizar,
    hash_de,
    ler_estado,
    salvar_estado,
)
from src.io.database import conexao
from src.io.parquet import gravar_particionado, ler_meses, ultimos_meses
from src.io.readers import ler_arquivo
from src.load.strategies import _parsear_datas
from src.quality.contracts import normalizar_colunas
from src.transform import faturamento as t_faturamento

log = logging.getLogger(__name__)

ORIGEM = ORIGENS["faturamento"]
PARQUET = DADOS / "faturamento_parquet"
ARQUIVO_ESTADO = RAIZ / ".estado_faturamento.json"
ARQUIVO_LOCK = RAIZ / ".lock_faturamento"

# Mês corrente + anterior. O anterior é seguro contra nota retroativa.
MESES_JANELA = 2

# Uma carga completa leva ~1min. Lock mais velho que isso é de processo morto.
LOCK_EXPIRA_EM = 30 * 60


def preparar() -> tuple[int, int]:
    """Lê a origem, trata e grava no Parquet particionado.

    Devolve (linhas tratadas, partições escritas).
    """
    df, avisos = ler_arquivo(ORIGEM)
    for aviso in avisos:
        log.warning("  %s", aviso)

    tratado, mais_avisos = t_faturamento.transformar(df)
    for aviso in mais_avisos:
        log.warning("  %s", aviso)

    # Preciso do mês de cada linha pra particionar. Uso o mesmo parser da
    # estratégia de carga, pra não haver divergência de interpretação de data.
    normalizado = normalizar_colunas(tratado)
    datas = _parsear_datas(normalizado, "emissao", "%d/%m/%Y")

    if datas.isna().any():
        raise ValueError(
            f"{int(datas.isna().sum())} linha(s) com emissão ilegível. "
            f"Exemplos: {normalizado.loc[datas.isna(), 'emissao'].head(5).tolist()}"
        )

    particoes = gravar_particionado(tratado, PARQUET, datas.dt.strftime("%Y-%m"))
    log.info(
        "  tratado: %s linhas em %s partição(ões) de mês",
        f"{len(tratado):,}",
        particoes,
    )
    return len(tratado), particoes


def carregar(tudo: bool = False) -> int:
    """Carrega a janela de meses no MySQL. Devolve as linhas enviadas."""
    from pipelines.upload import carregar_arquivo

    meses = ultimos_meses(PARQUET, 999 if tudo else MESES_JANELA)
    if not meses:
        raise RuntimeError(f"nenhuma partição em {PARQUET}")

    df = ler_meses(PARQUET, meses)
    log.info("  janela: %s (%s linhas)", ", ".join(meses), f"{len(df):,}")

    # O upload le de arquivo, entao gravo a janela no CSV que ele espera.
    # Mantenho o mesmo caminho de sempre pra reusar carregar_arquivo() inteiro:
    # mesma validacao, mesma estrategia, mesma conferencia pos-carga.
    destino = SAIDAS["faturamento"]
    destino.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destino, **CSV_SAIDA)

    with conexao() as con:
        cursor = con.cursor()
        try:
            linhas = carregar_arquivo(destino, "faturamento", cursor)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            cursor.close()
    return linhas


def materializar() -> None:
    """Refaz o faturamento_full INTEIRO.

    Não dá pra fazer só a janela: a ilha do cliente vem da última compra da
    marca foco em todo o histórico. Com só a janela, quem comprou há meses
    apareceria como 'outros'.
    """
    from pipelines.faturamento_full import main as full

    if full():
        raise RuntimeError("faturamento_full falhou — ver o log dele")


def configurar_log() -> None:
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(
                PASTA_LOGS / "faturamento_horario.log", encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main(argumentos: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Atualiza Faturamento + faturamento_full de hora em hora"
    )
    parser.add_argument(
        "--forcar", action="store_true", help="roda mesmo se o arquivo não mudou"
    )
    parser.add_argument("--status", action="store_true", help="só relata")
    parser.add_argument(
        "--tudo",
        action="store_true",
        help="carrega o histórico inteiro, não só a janela",
    )
    args = parser.parse_args(argumentos)

    configurar_log()

    if not ORIGEM.exists():
        log.error("não achei a origem: %s", ORIGEM)
        return 1

    # A origem escreve direto na pasta de rede, então pode estar no meio da
    # publicação. Um CSV truncado passaria por todas as validações — as linhas
    # que chegaram estão bem formadas.
    if not args.status:
        try:
            esperar_estabilizar(ORIGEM)
        except AindaEscrevendo as erro:
            log.warning("pulei esta rodada — %s", erro)
            return 0

    estado = ler_estado(ARQUIVO_ESTADO)
    atual = hash_de(ORIGEM)
    modificado = time.strftime(
        "%d/%m %H:%M", time.localtime(ORIGEM.stat().st_mtime)
    )

    if args.status:
        igual = atual == estado.get("hash")
        log.info(
            "origem mod=%s | última carga=%s | %s",
            modificado,
            estado.get("carregado_em", "nunca"),
            "sem mudança" if igual else "MUDOU (carga pendente)",
        )
        return 0

    if atual == estado.get("hash") and not (args.forcar or args.tudo):
        # Calado de propósito: rodando toda hora, um "sem mudança" por hora
        # enterraria o que importa no log.
        return 0

    inicio = time.time()
    try:
        with Lock(ARQUIVO_LOCK, LOCK_EXPIRA_EM):
            log.info("=" * 60)
            log.info("origem mudou (mod=%s) — atualizando", modificado)

            total, _ = preparar()
            linhas = carregar(tudo=args.tudo)
            materializar()

    except JaEstaRodando as erro:
        log.warning("pulei esta rodada — %s", erro)
        return 0
    except Exception as erro:
        # Não gravo o hash: na próxima hora tenta de novo.
        log.error("FALHA: %s: %s", type(erro).__name__, erro)
        log.debug("traceback", exc_info=True)
        return 1

    salvar_estado(
        ARQUIVO_ESTADO,
        {
            "hash": atual,
            "arquivo": ORIGEM.name,
            "modificado_em": modificado,
            "carregado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
            "linhas_origem": total,
            "linhas_carregadas": linhas,
        },
    )
    log.info(
        "OK: %s linhas na janela + faturamento_full completo (%.0fs)",
        f"{linhas:,}",
        time.time() - inicio,
    )
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
