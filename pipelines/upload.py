"""Varre a pasta de saída e sobe cada arquivo pro MySQL.

Uma pasta = uma tabela. O nome da pasta vira o nome da tabela.
"""

import datetime
import logging
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from config.settings import BACKUP, ENTRADA_VPS, PASTA_LOGS
from config.tables import CONTRATOS, ESTRATEGIA_PADRAO, ESTRATEGIAS
from src.io.database import conexao, validar_identificador
from src.io.readers import ler_arquivo
from src.load.strategies import executar
from src.load.views import criar_view_itens_completo
from src.quality.contracts import (
    exigir_nao_vazio,
    normalizar_colunas,
    validar_contrato,
)

log = logging.getLogger(__name__)

EXTENSOES = (".csv", ".xlsx", ".xlsm")

# Nessas duas a tabela inteira vem do arquivo, então a contagem tem que bater.
# Em date_range sobra o histórico de fora da janela e em upsert as linhas são
# atualizadas, não somadas — comparar ali daria alarme falso.
_CONFERE_CONTAGEM = {"replace", "truncate"}


def nome_tabela(nome_pasta: str) -> str:
    """'sku custo giba' -> 'SkuCustoGiba'.

    Tiro o acento antes de quebrar em palavras, senão o "ç" de
    'tabela-preço-promocao' vira separador e sai `TabelaPreOPromocao`.
    """
    nome = unicodedata.normalize("NFKD", str(nome_pasta).strip())
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    partes = re.split(r"[^a-zA-Z0-9]+", nome)
    return "".join(p.capitalize() for p in partes if p) or "Tabela"


def fazer_backup(caminho: Path) -> Path:
    """Move pro backup do dia. Se já existir um com o mesmo nome, põe a hora."""
    destino_dir = BACKUP / datetime.date.today().isoformat()
    destino_dir.mkdir(parents=True, exist_ok=True)

    destino = destino_dir / caminho.name
    if destino.exists():
        hora = datetime.datetime.now().strftime("%H%M%S")
        destino = destino_dir / f"{caminho.stem}_{hora}{caminho.suffix}"

    shutil.move(str(caminho), str(destino))
    return destino


def conferir_carga(cursor, tabela: str, enviadas: int, estrategia: str) -> None:
    """Confere no banco se chegou o que eu mandei, ainda dentro da transação.

    Se divergir, levanta e o rollback de quem chamou desfaz tudo.
    """
    validar_identificador(tabela)
    cursor.execute(f"SELECT COUNT(*) FROM `{tabela}`")
    no_banco = cursor.fetchone()[0]

    if estrategia in _CONFERE_CONTAGEM and no_banco != enviadas:
        raise ValueError(
            f"Divergência na carga de `{tabela}`: mandei {enviadas:,} linha(s), "
            f"o banco tem {no_banco:,}. Revertido."
        )

    log.info("  conferido: %s linha(s) em `%s`", f"{no_banco:,}", tabela)


def carregar_arquivo(caminho: Path, chave_pasta: str, cursor) -> int:
    """Lê, valida e carrega um arquivo. Qualquer problema vira exceção.

    Nunca devolvo 0 calado — quem chama precisa saber a diferença entre
    "carregou zero linha" e "deu erro".
    """
    cfg = ESTRATEGIAS.get(chave_pasta, {})
    estrategia = cfg.get("estrategia", ESTRATEGIA_PADRAO)
    tabela = nome_tabela(chave_pasta)

    log.info("%s -> `%s` [%s]", caminho.name, tabela, estrategia.upper())

    df, avisos = ler_arquivo(caminho)
    for aviso in avisos:
        # Aviso de leitura = linha jogada fora. Sobe como warning pra não sumir.
        log.warning("  REJEITADAS: %s", aviso)

    exigir_nao_vazio(df, caminho.name)
    df = normalizar_colunas(df)

    contrato = CONTRATOS.get(chave_pasta)
    if contrato:
        for aviso in validar_contrato(df, contrato, caminho.name):
            log.warning("  %s", aviso)

    log.info("  %s linhas x %s colunas", f"{len(df):,}", len(df.columns))

    df = df.fillna("").astype(str)  # o destino é LONGTEXT

    linhas = executar(cursor, tabela, df, {**cfg, "estrategia": estrategia})
    conferir_carga(cursor, tabela, linhas, estrategia)
    log.info("  OK: %s linhas em `%s`", f"{linhas:,}", tabela)
    return linhas


def varrer() -> int:
    """Processa todas as pastas. Devolve quantos arquivos falharam."""
    log.info("=" * 60)
    log.info("Iniciando carga...")
    inicio = time.time()

    if not ENTRADA_VPS.is_dir():
        log.error("Não achei a pasta de entrada: %s", ENTRADA_VPS)
        return 1

    total_arquivos = total_linhas = falhas = 0

    with conexao() as con:
        cursor = con.cursor()

        for pasta in sorted(p for p in ENTRADA_VPS.iterdir() if p.is_dir()):
            arquivos = sorted(
                a for a in pasta.iterdir() if a.suffix.lower() in EXTENSOES
            )
            if not arquivos:
                log.info("  (vazia) %s/", pasta.name)
                continue

            chave = pasta.name.lower()
            for arquivo in arquivos:
                try:
                    linhas = carregar_arquivo(arquivo, chave, cursor)
                    con.commit()

                    # Backup só depois do commit. Se algo acima falhar, o
                    # arquivo fica na entrada pra tentar de novo depois.
                    destino = fazer_backup(arquivo)
                    log.info("  arquivado em %s", destino)

                    total_arquivos += 1
                    total_linhas += linhas

                except Exception as erro:
                    # Um arquivo ruim não derruba os outros, mas conta como falha.
                    con.rollback()
                    falhas += 1
                    log.error("  FALHA em %s: %s", arquivo.name, erro)
                    log.debug("traceback de %s", arquivo.name, exc_info=True)

        try:
            criar_view_itens_completo(cursor)
            con.commit()
        except Exception as erro:
            falhas += 1
            log.error("  FALHA ao criar a view ItensCompleto: %s", erro)

        cursor.close()

    # Falhas e duração na mesma linha: é o que se olha primeiro quando o ETL
    # quebrou de madrugada e você abre o log às 8h.
    log.info(
        "Fim: %s arquivo(s), %s linha(s), %s falha(s) em %.1fs.",
        total_arquivos,
        f"{total_linhas:,}",
        falhas,
        time.time() - inicio,
    )
    log.info("=" * 60)
    return falhas


def configurar_log() -> None:
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(PASTA_LOGS / "upload.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    configurar_log()

    # Banco fora do ar, rede ou credencial errada morre aqui, antes de qualquer
    # arquivo. Sem isso vira um traceback de 20 linhas do driver, quando o que
    # interessa é "não conectei, e o host é esse".
    try:
        falhas = varrer()
    except Exception as erro:
        log.error("ERRO DE INFRAESTRUTURA — nenhum arquivo processado: %s", erro)
        log.debug("traceback", exc_info=True)
        return 1

    # Exit code != 0 quando algo falhou: é assim que o .ps1 percebe o problema.
    if falhas:
        log.error("%s arquivo(s) falharam.", falhas)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
