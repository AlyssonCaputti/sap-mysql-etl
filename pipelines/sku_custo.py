"""Carga da SkuCustoCdGiba (custo médio e estoque por depósito), a cada 5 min.

Essa base é separada do ETL diário porque a origem regera o arquivo várias
vezes por dia e o Forecast depende dela. Aqui eu não movo o arquivo pro backup
(a origem sobrescreve no lugar) e só recarrego quando o conteúdo muda de
verdade — comparo o hash. Senão seriam 288 DROP/CREATE por dia numa tabela que
quase sempre está igual.

    python -m pipelines.sku_custo            # carrega se mudou
    python -m pipelines.sku_custo --forcar   # carrega de qualquer jeito
    python -m pipelines.sku_custo --status   # só olha, não escreve
"""

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from config.settings import PASTA_LOGS, PASTA_SKU_CUSTO, RAIZ
from config.tables import CONTRATOS
from src.io.database import conexao
from src.io.readers import ler_arquivo
from src.load.strategies import replace
from src.quality.checkpoints import (
    comparar_com_ultima,
    porta1_recepcao,
    porta2_transformacao,
)
from src.quality.contracts import (
    exigir_nao_vazio,
    normalizar_colunas,
    validar_contrato,
)

log = logging.getLogger(__name__)

CHAVE_PASTA = "sku_custo_cd_giba"
# Direto da rede — ver o comentario em config/settings.py:PASTA_SKU_CUSTO.
PASTA = PASTA_SKU_CUSTO
TABELA = "SkuCustoCdGiba"
ARQUIVO_ESTADO = RAIZ / ".estado_sku_custo.json"
EXTENSOES = (".csv", ".xlsx", ".xlsm")


def achar_arquivo() -> Path | None:
    if not PASTA.is_dir():
        log.error("não achei a pasta: %s", PASTA)
        return None
    alvos = [a for a in sorted(PASTA.iterdir()) if a.suffix.lower() in EXTENSOES]
    if not alvos:
        return None
    # Normalmente só tem um. Se tiver mais, fico com o mais recente.
    return max(alvos, key=lambda a: a.stat().st_mtime)


def hash_de(caminho: Path) -> str:
    digest = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for bloco in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def ler_estado() -> dict:
    try:
        return json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_estado(estado: dict) -> None:
    ARQUIVO_ESTADO.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def carregar(caminho: Path, linhas_anteriores: int | None = None) -> int:
    """Le, valida contra o contrato e substitui a tabela."""
    df, avisos = ler_arquivo(caminho)
    for aviso in avisos:
        log.warning("%s", aviso)

    porta1_recepcao(df, caminho.name, avisos)
    comparar_com_ultima(len(df), linhas_anteriores, caminho.name)
    linhas_lidas = len(df)

    df = normalizar_colunas(df)

    # Sem custo aqui, a MCB do Forecast cai num fallback silencioso. Melhor
    # ficar com o dado de ontem.
    validar_contrato(df, CONTRATOS[CHAVE_PASTA], caminho.name)
    exigir_nao_vazio(df, caminho.name)

    porta2_transformacao(df, caminho.name, linhas_entrada=linhas_lidas)

    df = df.fillna("").astype(str)

    with conexao() as con:
        cursor = con.cursor()
        replace(cursor, TABELA, df)
        con.commit()
        cursor.execute(f"SELECT COUNT(*) FROM `{TABELA}`")
        gravadas = cursor.fetchone()[0]
        cursor.close()

    return gravadas


def configurar_log() -> None:
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(PASTA_LOGS / "sku_custo.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main(argumentos: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ETL incremental SkuCustoCdGiba")
    parser.add_argument(
        "--forcar", action="store_true", help="recarrega mesmo sem mudanca"
    )
    parser.add_argument(
        "--status", action="store_true", help="so relata, nao escreve"
    )
    args = parser.parse_args(argumentos)

    configurar_log()

    caminho = achar_arquivo()
    if not caminho:
        log.info("nenhum arquivo na pasta — nada a fazer")
        return 0

    estado = ler_estado()
    atual = hash_de(caminho)
    modificado = datetime.fromtimestamp(caminho.stat().st_mtime)

    if args.status:
        igual = atual == estado.get("hash")
        log.info(
            "arquivo=%s mod=%s | ultima carga=%s | %s",
            caminho.name,
            modificado.strftime("%d/%m %H:%M"),
            estado.get("carregado_em", "nunca"),
            "sem mudanca" if igual else "MUDOU (carga pendente)",
        )
        return 0

    if atual == estado.get("hash") and not args.forcar:
        # Saio calado de propósito. Rodando de 5 em 5 min, um "sem mudança"
        # 288 vezes por dia enterraria o que importa no log.
        return 0

    inicio = time.time()
    try:
        linhas = carregar(caminho, estado.get("linhas"))
    except Exception as erro:
        # Não gravo o hash, então a próxima rodada tenta de novo sozinha.
        log.error("ERRO ao carregar: %s: %s", type(erro).__name__, erro)
        log.debug("traceback", exc_info=True)
        return 1

    salvar_estado(
        {
            "hash": atual,
            "arquivo": caminho.name,
            "modificado_em": modificado.strftime("%Y-%m-%d %H:%M:%S"),
            "carregado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "linhas": linhas,
        }
    )
    log.info(
        "OK %s: %s linhas (%.1fs) | origem mod=%s",
        TABELA,
        f"{linhas:,}",
        time.time() - inicio,
        modificado.strftime("%d/%m %H:%M"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
