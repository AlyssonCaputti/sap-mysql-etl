"""Registro de cada execução na tabela `etl_execucoes`.

O pipeline antigo alimentava essa tabela e algum painel a consome. Ao promover
este projeto, o registro precisa continuar — senão o histórico de execuções para
de crescer sem ninguém perceber.

O vocabulário de `status` e `cor` vem do antigo e é preservado como está, porque
quem lê espera esses valores exatos:

    TUDO OK           verde     nenhuma falha
    RODOU COM ERROS   amarelo   rodou até o fim, com N falhas
    TRAVOU NO MEIO    laranja   morreu antes de terminar

Regra de ouro: **falhar aqui nunca derruba a carga.** O registro é observação,
não parte do trabalho. Se a tabela não existir ou o INSERT falhar, loga e segue —
o dado já está no banco e é isso que importa.
"""

import logging
import socket
from datetime import datetime

log = logging.getLogger(__name__)

# `inicio` tem UNIQUE KEY no schema do antigo: duas execuções no mesmo segundo
# colidiriam. O ON DUPLICATE deixa a segunda atualizar em vez de estourar.
_INSERT = """
INSERT INTO etl_execucoes
    (inicio, fim, status, cor, arquivos, linhas_processadas, qtd_erros,
     erros, bases, resumo, host_agente, reportado_em)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    fim = VALUES(fim), status = VALUES(status), cor = VALUES(cor),
    arquivos = VALUES(arquivos),
    linhas_processadas = VALUES(linhas_processadas),
    qtd_erros = VALUES(qtd_erros), erros = VALUES(erros),
    bases = VALUES(bases), resumo = VALUES(resumo),
    reportado_em = VALUES(reportado_em)
"""

STATUS_OK = ("TUDO OK", "verde")
STATUS_COM_ERROS = ("RODOU COM ERROS", "amarelo")
STATUS_TRAVOU = ("TRAVOU NO MEIO", "laranja")

# `resumo` é varchar(255) e `erros`/`bases` são TEXT. Corto antes de enviar pra
# não perder a linha inteira por causa de um campo comprido.
_LIMITE_RESUMO = 255
_LIMITE_TEXTO = 60_000


def classificar(falhas: int, travou: bool = False) -> tuple[str, str]:
    """Devolve (status, cor) no vocabulário que o painel espera."""
    if travou:
        return STATUS_TRAVOU
    return STATUS_COM_ERROS if falhas else STATUS_OK


def formatar_bases(bases: dict[str, int]) -> str:
    """'Clientes=1;Faturamento=1' — o formato que o antigo gravava."""
    return ";".join(f"{nome}={qtd}" for nome, qtd in sorted(bases.items()))


def registrar(
    cursor,
    inicio: datetime,
    falhas: int,
    arquivos: int,
    linhas: int,
    bases: dict[str, int] | None = None,
    erros: str = "",
    travou: bool = False,
    fim: datetime | None = None,
) -> bool:
    """Grava a execução. Devolve True se conseguiu.

    Nunca levanta: quem chama já terminou a carga e não deve perdê-la por causa
    do registro. Um `cursor` é recebido de fora para o INSERT participar da
    mesma conexão de quem chamou.
    """
    fim = fim or datetime.now()
    status, cor = classificar(falhas, travou)
    resumo = (
        f"[{fim:%Y-%m-%d %H:%M:%S}] {arquivos} arquivo(s), "
        f"{linhas:,} linha(s), {falhas} falha(s)."
    )

    try:
        cursor.execute(
            _INSERT,
            (
                inicio,
                fim,
                status,
                cor,
                arquivos,
                linhas,
                falhas,
                (erros or "")[:_LIMITE_TEXTO],
                formatar_bases(bases or {})[:_LIMITE_TEXTO],
                resumo[:_LIMITE_RESUMO],
                socket.gethostname()[:120],
                datetime.now(),
            ),
        )
        log.info("  execução registrada em etl_execucoes: %s", status)
        return True
    except Exception as erro:
        # Tabela ausente, permissão, schema divergente — nada disso justifica
        # perder a carga que já foi commitada.
        log.warning("  não registrei em etl_execucoes (%s): %s", type(erro).__name__, erro)
        return False
