"""Resumo diário do ETL por e-mail, às 11h.

POR QUE EXISTE
O alerta de `src/io/alerta.py` é reativo: ele fala quando algo QUEBRA. Isso
deixa dois furos que custaram caro em 04/09/2026:

1. **Silêncio é ambíguo.** Não receber e-mail pode significar "rodou tudo bem"
   ou "o alertador está quebrado". Foi exatamente o caso: o `ALERTA_PARA` tinha
   um `#` colado no endereço e nenhum alerta chegava — indistinguível de paz.
2. **Falha sem exceção não alerta.** Origem congelada, carga que não rodou
   nenhuma vez no dia, tabela que parou de ser atualizada: nada disso levanta
   exceção, então nada disso mandava e-mail.

Um resumo que chega TODO DIA no mesmo horário resolve os dois: a ausência dele
passa a ser, ela mesma, o sinal de que algo está errado.

A JANELA É 11h→11h, NÃO O DIA CIVIL
O pedido era "às 11h, contar como rodaram os processos do dia, incluindo o
faturamento das 15h". Às 11h a rodada das 15h de hoje ainda não aconteceu —
então o relatório cobre as últimas 24h (ontem 11h até hoje 11h), e a rodada das
15h de ontem cai naturalmente dentro dele. Fechar no dia civil (00h–23h)
deixaria de fora a madrugada e a manhã de hoje, que é justo o que o leitor das
11h quer conferir antes de começar o dia.

    python -m pipelines.resumo_diario              # coleta e manda
    python -m pipelines.resumo_diario --imprimir    # mostra na tela, não manda
"""

import argparse
import datetime
import logging

from src.io.alerta import _config, _enviar
from src.io.database import conexao
from src.io.log import configurar as configurar_log

log = logging.getLogger(__name__)

# A hora de corte da janela. O relatório das 11h de hoje cobre de ontem 11h
# até agora — ver a explicação no topo do módulo.
HORA_CORTE = 11

# As duas tabelas que o pedido destaca. São as que o comercial olha e as que a
# rodada horária do faturamento atualiza junto.
TABELAS_DESTAQUE = ("Faturamento", "faturamento_full")

# Tabelas do ETL diário que eu confiro no information_schema. Uso CREATE_TIME
# nas [REPLACE] (o ETL dropa e recria, então CREATE_TIME é a hora da carga) e
# UPDATE_TIME nas que só recebem UPSERT/DATE_RANGE.
#
# BaseBlacklistDelinte FICOU DE FORA de propósito: o ETL novo não a carrega mais
# (CREATE_TIME parado em 29/07/2026) e incluí-la geraria um vermelho permanente
# e falso — o tipo de ruído que faz o leitor parar de ler o relatório.
TABELAS_CONFERIDAS = (
    ("Clientes", "UPDATE_TIME"),
    ("Faturamento", "UPDATE_TIME"),
    ("faturamento_full", "UPDATE_TIME"),
    ("Itens", "CREATE_TIME"),
    ("ItensExtra1", "CREATE_TIME"),
    ("ItensExtra2", "CREATE_TIME"),
    ("ItensExtra3", "CREATE_TIME"),
)

# Acima disso a tabela é considerada velha no relatório. 26h = 1 dia + 2h de
# folga para atraso da tarefa agendada.
HORAS_TABELA_VELHA = 26

# O faturamento roda de hora em hora em horário comercial. Zero rodadas em 24h
# significa origem congelada ou tarefa agendada morta — nas duas hipóteses é
# notícia, mesmo sem nenhuma exceção ter acontecido.
MIN_RODADAS_FATURAMENTO = 1


def janela(agora: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    """Devolve (desde, ate) da janela de 24h que fecha na hora de corte.

    Rodando às 11h de 04/09, devolve (03/09 11:00, 04/09 11:00). Uso a hora de
    corte fixa em vez de "agora menos 24h" para o relatório ser reproduzível: se
    a tarefa agendada atrasar 10min, a janela continua a mesma.
    """
    agora = agora or datetime.datetime.now()
    ate = agora.replace(hour=HORA_CORTE, minute=0, second=0, microsecond=0)
    # Rodou antes das 11h (execução manual, ou tarefa adiantada): fecho a janela
    # no corte de ONTEM, senão eu incluiria um futuro que não existe.
    if agora < ate:
        ate -= datetime.timedelta(days=1)
    return ate - datetime.timedelta(days=1), ate


def coletar_execucoes(cursor, desde: datetime.datetime, ate: datetime.datetime) -> list[dict]:
    """As execuções de `etl_execucoes` na janela, mais antiga primeiro."""
    cursor.execute(
        "SELECT inicio, fim, status, cor, arquivos, linhas_processadas, "
        "       qtd_erros, erros, bases, resumo "
        "FROM etl_execucoes WHERE inicio >= %s AND inicio < %s "
        "ORDER BY inicio",
        (desde, ate),
    )
    colunas = [c[0] for c in cursor.description]
    return [dict(zip(colunas, linha)) for linha in cursor.fetchall()]


def coletar_tabelas(cursor) -> list[dict]:
    """Idade de cada tabela conferida, pelo information_schema.

    `carga_em=None` vira "desconhecido", nunca "nunca carregou": o UPDATE_TIME
    do InnoDB vem NULL em várias tabelas e ZERA quando o MySQL reinicia. Chamar
    isso de "nunca carregou" seria mentira sobre uma tabela com 245 mil linhas.
    """
    nomes = tuple(n for n, _coluna in TABELAS_CONFERIDAS)
    ph = ",".join(["%s"] * len(nomes))
    cursor.execute(
        "SELECT TABLE_NAME, TABLE_ROWS, CREATE_TIME, UPDATE_TIME "
        "FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({ph})",
        nomes,
    )
    por_nome = {linha[0]: linha for linha in cursor.fetchall()}

    agora = datetime.datetime.now()
    saida = []
    for nome, coluna in TABELAS_CONFERIDAS:
        linha = por_nome.get(nome)
        if linha is None:
            saida.append({"tabela": nome, "existe": False, "linhas": None,
                          "carga_em": None, "horas": None, "velha": True})
            continue
        _n, total_linhas, create_time, update_time = linha
        carga = create_time if coluna == "CREATE_TIME" else update_time
        horas = (agora - carga).total_seconds() / 3600 if carga else None
        saida.append({
            "tabela": nome, "existe": True, "linhas": total_linhas,
            "carga_em": carga, "horas": horas,
            "velha": horas is not None and horas > HORAS_TABELA_VELHA,
        })
    return saida


def _e_do_faturamento(execucao: dict) -> bool:
    """A execução é de uma rodada do faturamento horário?

    Distingo pelo campo `bases`, que o `_registrar_rodada` do
    faturamento_horario preenche com as duas tabelas dele. O ETL diário grava
    as ~20 pastas que carregou, então não colide.
    """
    bases = execucao.get("bases") or ""
    return "faturamento_full=" in bases


def resumir(execucoes: list[dict], tabelas: list[dict]) -> dict:
    """Consolida os números da janela. Sem I/O — é o que os testes exercitam."""
    faturamento = [e for e in execucoes if _e_do_faturamento(e)]
    diarias = [e for e in execucoes if not _e_do_faturamento(e)]
    com_erro = [e for e in execucoes if (e.get("qtd_erros") or 0) > 0]
    velhas = [t for t in tabelas if t["velha"]]

    # Os problemas em ordem de gravidade. Esta lista é o que decide o assunto do
    # e-mail: com ela vazia o assunto diz OK, e o leitor sabe pelo assunto se
    # precisa abrir.
    problemas = []
    if len(faturamento) < MIN_RODADAS_FATURAMENTO:
        problemas.append(
            "o faturamento NAO rodou nenhuma vez na janela "
            "(origem congelada ou tarefa agendada parada)"
        )
    if not diarias:
        problemas.append("o ETL diario (clientes/itens) nao rodou na janela")
    for e in com_erro:
        problemas.append(
            f"execucao de {e['inicio']:%d/%m %H:%M} com {e['qtd_erros']} falha(s)"
        )
    for t in velhas:
        if not t["existe"]:
            problemas.append(f"tabela `{t['tabela']}` nao existe no banco")
        else:
            problemas.append(
                f"`{t['tabela']}` sem carga ha {t['horas']:.0f}h"
            )

    return {
        "execucoes": execucoes,
        "faturamento": faturamento,
        "diarias": diarias,
        "com_erro": com_erro,
        "tabelas": tabelas,
        "problemas": problemas,
        "ok": not problemas,
    }


def _bloco_faturamento(rodadas: list[dict]) -> list[str]:
    """As rodadas horárias, uma por linha, com destaque para a das 15h.

    O pedido pedia explicitamente a das 15h; mostro TODAS e marco a das 15h com
    "<--", porque a das 15h só faz sentido lida ao lado das vizinhas (se as
    14h e 16h rodaram e a das 15h não, isso é uma informação; se nenhuma rodou,
    é outra bem diferente).
    """
    if not rodadas:
        return [
            "  NENHUMA rodada na janela.",
            "",
            "  O faturamento deveria rodar de hora em hora. Zero rodadas",
            "  significa que a origem parou de ser republicada (o pipeline sai",
            "  calado quando o hash nao muda) ou que a tarefa agendada",
            "  'ETL-Faturamento-Horario' parou.",
        ]

    linhas = []
    for r in rodadas:
        marca = "  <-- rodada das 15h" if r["inicio"].hour == 15 else ""
        duracao = f"{(r['fim'] - r['inicio']).total_seconds():.0f}s" if r["fim"] else "sem fim"
        erro = f" | {r['qtd_erros']} FALHA(S)" if (r.get("qtd_erros") or 0) else ""
        linhas.append(
            f"  {r['inicio']:%d/%m %H:%M}  {r['status']:<16} "
            f"{r['linhas_processadas']:>8,} linhas  {duracao:>7}{erro}{marca}"
        )
    return linhas


def montar_corpo(dados: dict, desde: datetime.datetime, ate: datetime.datetime) -> str:
    """O texto do e-mail. Texto puro, alinhado — é lido no celular às 11h."""
    L = [
        f"Janela: {desde:%d/%m %H:%M} -> {ate:%d/%m %H:%M} (24h)",
        "",
    ]

    if dados["ok"]:
        L += ["SITUACAO: tudo em ordem.", ""]
    else:
        L += ["SITUACAO: precisa de atencao", ""]
        L += [f"  - {p}" for p in dados["problemas"]]
        L += [""]

    L += ["=" * 62, "FATURAMENTO + FATURAMENTO_FULL (rodada horaria)", "=" * 62]
    L += _bloco_faturamento(dados["faturamento"])
    L += [""]

    L += ["=" * 62, "ETL DIARIO (clientes, itens e demais bases)", "=" * 62]
    if dados["diarias"]:
        for e in dados["diarias"]:
            duracao = (
                f"{(e['fim'] - e['inicio']).total_seconds():.0f}s"
                if e["fim"] else "sem fim"
            )
            L.append(
                f"  {e['inicio']:%d/%m %H:%M}  {e['status']:<16} "
                f"{e['arquivos']:>3} arq  {e['linhas_processadas']:>8,} linhas  {duracao:>7}"
            )
    else:
        L.append("  NENHUMA execucao na janela.")
    L += [""]

    if dados["com_erro"]:
        L += ["=" * 62, "ERROS NO DETALHE", "=" * 62]
        for e in dados["com_erro"]:
            L.append(f"  {e['inicio']:%d/%m %H:%M} ({e['qtd_erros']} falha(s)):")
            # Corto por linha: o campo guarda uma mensagem por arquivo que
            # falhou e o e-mail inteiro tem teto de 8000 chars no _enviar.
            for detalhe in (e.get("erros") or "").splitlines()[:10]:
                L.append(f"      {detalhe[:150]}")
        L += [""]

    L += ["=" * 62, "IDADE DAS TABELAS NO BANCO", "=" * 62]
    for t in dados["tabelas"]:
        destaque = " *" if t["tabela"] in TABELAS_DESTAQUE else "  "
        if not t["existe"]:
            L.append(f"{destaque} {t['tabela']:<20} NAO EXISTE no banco")
        elif t["carga_em"] is None:
            # Ver o docstring de coletar_tabelas: NULL e "desconhecido".
            L.append(
                f"{destaque} {t['tabela']:<20} desconhecido "
                f"(o InnoDB nao informa; {t['linhas'] or 0:,} linhas)"
            )
        else:
            aviso = "  <-- VELHA" if t["velha"] else ""
            L.append(
                f"{destaque} {t['tabela']:<20} {t['carga_em']:%d/%m %H:%M} "
                f"(ha {t['horas']:.0f}h, {t['linhas'] or 0:,} linhas){aviso}"
            )
    L += [
        "",
        "  * as duas tabelas que a rodada horaria do faturamento atualiza.",
        "  'desconhecido' = o UPDATE_TIME do InnoDB vem NULL ou zera quando o",
        "  MySQL reinicia; nao quer dizer que a tabela esteja vazia.",
        "",
        "-" * 62,
        "Este resumo chega todo dia as 11h. Se ele PARAR de chegar, algo esta",
        "errado com o ETL ou com o proprio alertador — a ausencia dele e sinal.",
    ]
    return "\n".join(L)


def main(argumentos: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resumo diario do ETL por e-mail")
    parser.add_argument(
        "--imprimir", action="store_true", help="mostra na tela e nao manda e-mail"
    )
    args = parser.parse_args(argumentos)

    configurar_log("resumo_diario.log")
    desde, ate = janela()

    try:
        with conexao() as con:
            cursor = con.cursor()
            execucoes = coletar_execucoes(cursor, desde, ate)
            tabelas = coletar_tabelas(cursor)
            cursor.close()
    except Exception as erro:
        # Sem banco não há resumo. Mando o e-mail dizendo isso: um resumo que
        # falha calado nos devolveria ao problema que este pipeline resolve.
        log.error("FALHA ao coletar: %s: %s", type(erro).__name__, erro)
        cfg = _config()
        if cfg and not args.imprimir:
            _enviar(
                cfg,
                "[ETL RESUMO] FALHOU ao gerar o resumo diario",
                f"Nao consegui ler o banco para montar o resumo das 11h.\n\n"
                f"{type(erro).__name__}: {erro}\n\n"
                f"O ETL pode estar rodando normalmente — o que falhou foi a "
                f"leitura do banco para gerar este relatorio.",
            )
        return 1

    dados = resumir(execucoes, tabelas)
    corpo = montar_corpo(dados, desde, ate)

    if args.imprimir:
        print(corpo)
        return 0

    cfg = _config()
    if not cfg:
        log.warning(
            "alerta nao configurado (falta ALERTA_SMTP_HOST/ALERTA_PARA) — "
            "resumo so no log"
        )
        log.info("\n%s", corpo)
        return 0

    # O assunto carrega o veredito para o resumo ser triado sem abrir.
    if dados["ok"]:
        assunto = f"[ETL RESUMO] {ate:%d/%m} OK — {len(dados['faturamento'])} rodada(s) de faturamento"
    else:
        assunto = f"[ETL RESUMO] {ate:%d/%m} ATENCAO — {len(dados['problemas'])} ponto(s)"

    # Sem janela de silêncio de propósito: este e-mail é periódico, não reativo.
    # É justamente a chegada dele todo dia que prova que a vigilância funciona.
    if _enviar(cfg, assunto, corpo):
        log.info("resumo enviado: %s", assunto)
        return 0
    log.warning("nao consegui enviar o resumo; segue no log")
    log.info("\n%s", corpo)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
