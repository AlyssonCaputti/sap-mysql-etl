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
from src.io.execucoes import registrar
from src.io.readers import ler_arquivo
from src.load.strategies import executar
from src.load.views import criar_view_itens_completo
from src.quality.checkpoints import porta1_recepcao, porta2_transformacao
from src.quality.contracts import (
    exigir_nao_vazio,
    normalizar_colunas,
    validar_contrato,
)

log = logging.getLogger(__name__)

EXTENSOES = (".csv", ".xlsx", ".xlsm")

# Pastas que outro pipeline carrega. O faturamento roda de hora em hora
# (pipelines/faturamento_horario.py) e não pode ser carregado aqui também —
# duas cargas simultâneas na mesma tabela dão problema.
PASTAS_DE_OUTRO_PIPELINE = {"faturamento"}

# Nessas duas a tabela inteira vem do arquivo, então a contagem tem que bater.
# Em date_range sobra o histórico de fora da janela e em upsert as linhas são
# atualizadas, não somadas — comparar ali daria alarme falso.
_CONFERE_CONTAGEM = {"replace", "truncate"}


# Nomes que já existem no banco com a grafia "errada" e ficam como estão.
#
# `tabela-preço-promocao` virou `TabelaPreOPromocao` no banco por causa do bug do
# "ç" (o split em [^a-zA-Z0-9] tratava o acento como separador). O bug está
# corrigido abaixo, mas a tabela em produção continua com o nome antigo e há
# consumidor apontado pra ela. Gerar `TabelaPrecoPromocao` criaria uma tabela
# nova e deixaria a antiga parada, alimentando dashboard com dado congelado.
#
# Decisão de 18/08/2026: manter o nome como está. Para renomear algum dia, é
# preciso mudar o banco e os consumidores juntos — e então a entrada sai daqui.
NOMES_FIXOS = {"tabela-preço-promocao": "TabelaPreOPromocao"}


def nome_tabela(nome_pasta: str) -> str:
    """'sku custo giba' -> 'SkuCustoGiba'.

    Tiro o acento antes de quebrar em palavras, senão o "ç" de
    'tabela-preço-promocao' vira separador e sai `TabelaPreOPromocao`.

    Pasta listada em NOMES_FIXOS escapa dessa regra e usa o nome que já está no
    banco — a correção não vale a pena quando renomear a tabela é o risco.
    """
    bruto = str(nome_pasta).strip()
    fixo = NOMES_FIXOS.get(bruto.lower())
    if fixo:
        return fixo

    nome = unicodedata.normalize("NFKD", bruto)
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

    porta1_recepcao(df, caminho.name, avisos)
    linhas_lidas = len(df)

    exigir_nao_vazio(df, caminho.name)
    df = normalizar_colunas(df)

    contrato = CONTRATOS.get(chave_pasta)
    if contrato:
        for aviso in validar_contrato(df, contrato, caminho.name):
            log.warning("  %s", aviso)

    porta2_transformacao(
        df,
        caminho.name,
        linhas_entrada=linhas_lidas,
        chave=(cfg.get("chaves") or [None])[0],
    )

    df = df.fillna("").astype(str)  # o destino é LONGTEXT

    linhas = executar(cursor, tabela, df, {**cfg, "estrategia": estrategia})
    conferir_carga(cursor, tabela, linhas, estrategia)
    log.info("  OK: %s linhas em `%s`", f"{linhas:,}", tabela)
    return linhas


def varrer(pular: set[str] | None = None) -> int:
    """Processa todas as pastas. Devolve quantos arquivos falharam.

    `pular` recebe nomes de pasta (minúsculo) que outro pipeline cuida — hoje
    o faturamento, que roda de hora em hora. Sem isso as duas cargas
    disputariam a mesma tabela.
    """
    # `is None` e não `or`: um set vazio é falsy, e quem passa pular=set()
    # está pedindo pra processar tudo, não pra usar o padrão.
    if pular is None:
        pular = PASTAS_DE_OUTRO_PIPELINE
    pular = {p.lower() for p in pular}

    log.info("=" * 60)
    log.info("Iniciando carga...")
    inicio = time.time()
    inicio_dt = datetime.datetime.now()

    if not ENTRADA_VPS.is_dir():
        log.error("Não achei a pasta de entrada: %s", ENTRADA_VPS)
        return 1

    total_arquivos = total_linhas = falhas = 0
    # Para o registro em etl_execucoes: quais tabelas foram tocadas e o que deu
    # errado. O painel do ETL antigo lê esses dois campos.
    bases: dict[str, int] = {}
    mensagens_de_erro: list[str] = []

    with conexao() as con:
        cursor = con.cursor()

        for pasta in sorted(p for p in ENTRADA_VPS.iterdir() if p.is_dir()):
            if pasta.name.lower() in pular:
                log.info("  (outro pipeline) %s/", pasta.name)
                continue

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
                    tabela = nome_tabela(chave)
                    bases[tabela] = bases.get(tabela, 0) + 1

                except Exception as erro:
                    # Um arquivo ruim não derruba os outros, mas conta como falha.
                    con.rollback()
                    falhas += 1
                    log.error("  FALHA em %s: %s", arquivo.name, erro)
                    log.debug("traceback de %s", arquivo.name, exc_info=True)
                    mensagens_de_erro.append(f"{arquivo.name}: {erro}")

        try:
            criar_view_itens_completo(cursor)
            con.commit()
        except Exception as erro:
            falhas += 1
            log.error("  FALHA ao criar a view ItensCompleto: %s", erro)
            mensagens_de_erro.append(f"view ItensCompleto: {erro}")

        # Registro por último, com a carga já commitada: se falhar, perde-se o
        # registro, nunca o dado. Commit próprio pelo mesmo motivo.
        registrar(
            cursor,
            inicio=inicio_dt,
            falhas=falhas,
            arquivos=total_arquivos,
            linhas=total_linhas,
            bases=bases,
            erros="\n".join(mensagens_de_erro),
        )
        try:
            con.commit()
        except Exception as erro:
            log.warning("  não commitei o registro de execução: %s", erro)

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
