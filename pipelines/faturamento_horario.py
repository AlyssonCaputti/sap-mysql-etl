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
import datetime
import logging
import time

import pandas as pd

from config.settings import CSV_SAIDA, DADOS, ORIGENS, RAIZ, SAIDAS
from config.tables import ESTRATEGIAS
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
from src.io.alerta import falhou, normalizou
from src.io.execucoes import registrar
from src.io.log import configurar as configurar_log
from src.io.parquet import gravar_particionado, ler_meses, ultimos_meses
from src.io.readers import ler_arquivo
from src.load.strategies import _parsear_datas
from src.quality.checkpoints import (
    comparar_com_ultima,
    porta1_recepcao,
    porta2_transformacao,
    saida_carga,
)
from src.quality.contracts import normalizar_colunas
from src.transform import faturamento as t_faturamento

log = logging.getLogger(__name__)

ORIGEM = ORIGENS["faturamento"]
PARQUET = DADOS / "faturamento_parquet"
ARQUIVO_ESTADO = RAIZ / ".estado_faturamento.json"
ARQUIVO_LOCK = RAIZ / ".lock_faturamento"

# Mês corrente + anterior. O anterior é seguro contra nota retroativa.
MESES_JANELA = 2

# Quantos meses o --tudo carrega por vez. Com os 32 de uma vez o processo
# morria de out of memory numa máquina com pouca RAM livre (31/08/2026).
MESES_POR_LOTE = 4

# Um lugar só pro formato da emissão: é o mesmo que a carga usa no DELETE.
FORMATO_EMISSAO = ESTRATEGIAS["faturamento"]["formato_data"]
COLUNA_DATA = ESTRATEGIAS["faturamento"]["coluna_data"]

# Uma carga completa leva ~1min. Lock mais velho que isso é de processo morto.
LOCK_EXPIRA_EM = 30 * 60

# Quanto tempo a origem pode ficar sem ser republicada antes de eu avisar.
#
# O SAP republica o CSV várias vezes por dia. Se ele congela, este pipeline sai
# CALADO (hash igual = nada a fazer) e ninguém descobre: foi o que aconteceu em
# 04/09/2026, com a origem parada desde 03/09 16:10 e o painel dizendo "TUDO OK"
# porque o ETL, tecnicamente, não falhou.
#
# 5h em vez de 26h: a origem é HORÁRIA, então cinco horas paradas em dia útil já
# é anomalia. Um limite de um dia só avisaria quando o dado do comercial já
# estivesse velho demais para agir.
HORAS_ORIGEM_PARADA = 5

# Só cobro em horário comercial: o SAP não publica de madrugada, e avisar às 3h
# da manhã treinaria o leitor a ignorar o alerta.
HORA_COMERCIAL = range(8, 20)


def preparar(linhas_anteriores: int | None = None) -> tuple[int, int, pd.Series]:
    """Lê a origem, trata e grava no Parquet particionado.

    Devolve (linhas tratadas, partições escritas, meses de cada linha).
    """
    df, avisos = ler_arquivo(ORIGEM)
    for aviso in avisos:
        log.warning("  %s", aviso)

    porta1_recepcao(df, ORIGEM.name, avisos)
    comparar_com_ultima(len(df), linhas_anteriores, ORIGEM.name)

    tratado, mais_avisos = t_faturamento.transformar(df)
    for aviso in mais_avisos:
        log.warning("  %s", aviso)

    # Preciso do mês de cada linha pra particionar. Uso o mesmo parser e o
    # mesmo formato da estratégia de carga, pra não haver duas interpretações
    # de data no caminho.
    normalizado = normalizar_colunas(tratado)
    datas = _parsear_datas(normalizado, COLUNA_DATA, FORMATO_EMISSAO)

    # Sem chave aqui: nota_item repete de propósito quando a nota tem
    # devolução parcial (mesmo item, valores diferentes). Conferi na origem —
    # 135 linhas, nenhuma cópia exata. Avisar disso toda hora seria só ruído.
    porta2_transformacao(
        tratado,
        ORIGEM.name,
        linhas_entrada=len(df),
        coluna_data=COLUNA_DATA,
        datas=datas,
    )

    if datas.isna().any():
        raise ValueError(
            f"{int(datas.isna().sum())} linha(s) com emissão ilegível. "
            f"Exemplos: {normalizado.loc[datas.isna(), COLUNA_DATA].head(5).tolist()}"
        )

    # Data futura vira partição que não deveria existir, e essa partição fica
    # pra sempre (só reescrevo as que estão no arquivo, nunca apago as outras).
    # Aí a janela do calendário nunca mais a alcança e ela vira lixo silencioso.
    futuras = datas > pd.Timestamp.today().normalize()
    if futuras.any():
        exemplos = normalizado.loc[futuras, COLUNA_DATA].astype(str).unique()[:5]
        raise ValueError(
            f"{int(futuras.sum())} linha(s) com emissão no futuro. Abortei — "
            f"cada uma cria uma partição órfã que fica no disco pra sempre. "
            f"Exemplos: {exemplos.tolist()}"
        )

    meses = datas.dt.strftime("%Y-%m")
    particoes = gravar_particionado(tratado, PARQUET, meses)
    log.info(
        "  tratado: %s linhas em %s partição(ões) de mês",
        f"{len(tratado):,}",
        particoes,
    )
    return len(tratado), particoes, meses


def carregar(tudo: bool = False, meses_origem: pd.Series | None = None) -> int:
    """Carrega a janela de meses no MySQL. Devolve as linhas enviadas."""
    meses = ultimos_meses(PARQUET, 999 if tudo else MESES_JANELA)
    if not meses:
        raise RuntimeError(f"nenhuma partição em {PARQUET}")

    # O --tudo vai em lotes de meses; a janela normal cabe num lote só.
    #
    # Ler os 32 meses de uma vez estourava a memória da máquina (o CSV
    # intermediário passa de 100 MB e o pandas precisa de vários GB pra
    # parsear) e a carga inteira morria antes de tocar no banco. Em lote o
    # pico fica no tamanho do maior lote, não do histórico.
    #
    # Fatiar é seguro porque o date_range apaga exatamente os meses do lote:
    # cada lote reescreve os seus meses e não encosta nos outros.
    if not tudo:
        return _carregar_meses(meses, meses_origem)

    total = 0
    lotes = [
        meses[i : i + MESES_POR_LOTE] for i in range(0, len(meses), MESES_POR_LOTE)
    ]
    log.info(
        "  --tudo: %s mês(es) em %s lote(s) de até %s",
        len(meses),
        len(lotes),
        MESES_POR_LOTE,
    )
    for indice, lote in enumerate(lotes, 1):
        log.info("  lote %s/%s", indice, len(lotes))
        # A conferência por mês só no último lote: ela compara a origem
        # inteira com o banco, e no meio da carga a diferença seria só dos
        # meses que ainda não subiram.
        total += _carregar_meses(
            lote, meses_origem if indice == len(lotes) else None
        )
    return total


def _carregar_meses(meses: list[str], meses_origem: pd.Series | None) -> int:
    """Carrega um conjunto de meses. É a carga que o date_range enxerga."""
    from pipelines.upload import carregar_arquivo

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

        # Depois do commit: comparo mês a mês a origem com o banco. É o que
        # mostra a nota retroativa que caiu fora da janela — sem isso a
        # diferença só aparece se alguém for conferir na mão.
        if meses_origem is not None:
            try:
                saida_carga(
                    cursor,
                    "Faturamento",
                    COLUNA_DATA,
                    meses,
                    meses_origem.value_counts().to_dict(),
                )
            except Exception as erro:
                log.warning("  [saída] conferência por mês falhou: %s", erro)

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


def _conferir_origem_parada(
    mtime_origem: float, modificado: str, agora: datetime.datetime | None = None
) -> bool:
    """Avisa se o SAP parou de republicar o CSV. Devolve True se avisou.

    Este é o modo de falha que o pipeline NÃO cobria: quando a origem congela,
    o hash não muda, não há exceção nenhuma e o pipeline sai calado — para
    sempre. O painel mostra a última carga como bem-sucedida (porque foi) e o
    dado do comercial vai envelhecendo sem ninguém saber.

    Não é falha do ETL, é falha de quem publica — e por isso o texto do alerta
    aponta para o SAP/origem, não para a carga. Diagnóstico errado faz o leitor
    caçar problema no lugar errado.

    A janela de silêncio do alerta.falhou() cuida da repetição: rodando de hora
    em hora, isto vira 1 e-mail/hora no máximo, não 24 iguais.
    """
    agora = agora or datetime.datetime.now()
    idade_h = (agora.timestamp() - mtime_origem) / 3600

    if idade_h < HORAS_ORIGEM_PARADA:
        return False
    # weekday() >= 5 é sábado/domingo: a origem não é republicada no fim de
    # semana, e cobrar isso todo sábado tornaria o alerta ruído.
    if agora.weekday() >= 5 or agora.hour not in HORA_COMERCIAL:
        return False

    log.warning(
        "origem parada há %.0fh (mod=%s) — nada a carregar, mas isso é anomalia",
        idade_h,
        modificado,
    )
    return falhou(
        "faturamento_horario",
        f"A origem do faturamento nao e republicada ha {idade_h:.0f}h.\n\n"
        f"O ETL esta OK: sem arquivo novo, nao ha o que carregar. O problema "
        f"esta em QUEM PUBLICA o CSV (SAP/integracao).\n\n"
        f"Enquanto isso, `Faturamento` e `faturamento_full` seguem com o dado "
        f"da ultima carga — cada vez mais velho.",
        contexto={
            "Origem": str(ORIGEM),
            "Publicada em": modificado,
            "Parada ha": f"{idade_h:.0f}h",
            "Limite": f"{HORAS_ORIGEM_PARADA}h em dia util, 08-19h",
        },
        # Chave fixa: o texto carrega as horas, que mudam a cada rodada e
        # furariam a janela de silencio toda hora.
        chave="origem_parada",
    )


def _registrar_rodada(
    inicio: datetime.datetime,
    linhas: int,
    falhas: int = 0,
    erros: str = "",
) -> None:
    """Anota esta rodada em `etl_execucoes` — a tabela que o Vigia ETL lê.

    Sem isto o faturamento é INVISÍVEL para o painel: até 04/09/2026 a tabela
    tinha ~1 linha/dia (só o ETL diário das 10:10), e as 24 rodadas horárias do
    faturamento — justo as que mexem na tabela que o comercial olha — não
    apareciam em lugar nenhum. Quem quisesse saber "como rodou o faturamento das
    15h" não tinha onde consultar.

    Abro conexão própria de propósito: a de `_carregar_meses` já fechou quando
    chego aqui, e o registro é observação — não vale a pena mantê-la aberta
    durante o `materializar()`, que leva ~1min.

    `bases` leva o par Faturamento/faturamento_full porque as duas são
    atualizadas nesta rodada, e é isso que o resumo diário conta separadamente.

    Segue a regra de ouro do src/io/execucoes.py: falhar aqui nunca derruba a
    carga. Se a rodada já gravou no MySQL, perder o registro é aceitável.
    """
    try:
        with conexao() as con:
            cursor = con.cursor()
            registrar(
                cursor,
                inicio=inicio,
                falhas=falhas,
                # 1 arquivo: a origem é sempre o dataRentNFVPS.csv.
                arquivos=1,
                linhas=linhas,
                bases={"Faturamento": linhas, "faturamento_full": linhas},
                erros=erros,
            )
            con.commit()
            cursor.close()
    except Exception as erro:
        log.warning(
            "  não registrei a rodada em etl_execucoes (%s): %s",
            type(erro).__name__,
            erro,
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

    configurar_log("faturamento_horario.log")

    if not ORIGEM.exists():
        log.error("não achei a origem: %s", ORIGEM)
        falhou(
            "faturamento_horario",
            f"Não achei a origem: {ORIGEM}",
            chave="origem_ausente",
        )
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
        # Calado de propósito no log: rodando toda hora, um "sem mudança" por
        # hora enterraria o que importa. Mas ficar calado para SEMPRE é o furo
        # que este bloco fecha — ver _conferir_origem_parada.
        _conferir_origem_parada(ORIGEM.stat().st_mtime, modificado)
        return 0

    inicio = time.time()
    inicio_dt = datetime.datetime.now()
    try:
        with Lock(ARQUIVO_LOCK, LOCK_EXPIRA_EM):
            log.info("=" * 60)
            log.info("origem mudou (mod=%s) — atualizando", modificado)

            total, _, meses = preparar(estado.get("linhas_origem"))
            linhas = carregar(tudo=args.tudo, meses_origem=meses)
            materializar()

    except JaEstaRodando as erro:
        # Não registro: não houve rodada, outro processo é que está com o lock.
        log.warning("pulei esta rodada — %s", erro)
        return 0
    except Exception as erro:
        # Não gravo o hash: na próxima hora tenta de novo.
        log.error("FALHA: %s: %s", type(erro).__name__, erro)
        log.debug("traceback", exc_info=True)
        # Registro a falha ANTES do alerta: o e-mail pode estar desconfigurado
        # (foi o que aconteceu de 31/08 a 04/09/2026), e nesse caso a tabela é o
        # único lugar onde a falha fica registrada.
        _registrar_rodada(
            inicio_dt, linhas=0, falhas=1, erros=f"{type(erro).__name__}: {erro}"
        )
        falhou(
            "faturamento_horario",
            f"{type(erro).__name__}: {erro}",
            contexto={"Origem mod": modificado},
        )
        return 1

    _registrar_rodada(inicio_dt, linhas=linhas)

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
    normalizou("faturamento_horario", contexto={"Linhas": f"{linhas:,}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
