"""Testes do resumo diario das 11h.

O que importa cobrir aqui e o que quebraria EM SILENCIO: a janela 11h->11h, a
distincao entre rodada do faturamento e execucao do ETL diario, e a deteccao
dos modos de falha que nao levantam excecao (origem congelada, tabela velha).
Nenhum teste precisa de banco.
"""

import datetime

from pipelines.resumo_diario import (
    HORAS_TABELA_VELHA,
    _e_do_faturamento,
    coletar_execucoes,
    janela,
    montar_corpo,
    resumir,
)


class CursorFalso:
    """Guarda os SQLs em vez de executar."""

    def __init__(self, linhas=None, description=None):
        self.sqls = []
        self.parametros = []
        self._linhas = linhas or []
        self.description = description or []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.parametros.append(params)

    def fetchall(self):
        return self._linhas


def _rodada_fat(hora, *, dia=3, linhas=13_688, erros=0, status="TUDO OK"):
    """Uma rodada do faturamento horario, como o _registrar_rodada grava."""
    inicio = datetime.datetime(2026, 9, dia, hora, 5, 0)
    return {
        "inicio": inicio,
        "fim": inicio + datetime.timedelta(seconds=106),
        "status": status,
        "cor": "verde" if not erros else "amarelo",
        "arquivos": 1,
        "linhas_processadas": linhas,
        "qtd_erros": erros,
        "erros": "",
        # A chave que distingue: o faturamento grava as duas tabelas dele.
        "bases": f"Faturamento={linhas};faturamento_full={linhas}",
        "resumo": "",
    }


def _execucao_diaria(*, dia=4, hora=10, erros=0, status="TUDO OK"):
    """Uma execucao do ETL diario, como o upload.py grava."""
    inicio = datetime.datetime(2026, 9, dia, hora, 10, 49)
    return {
        "inicio": inicio,
        "fim": inicio + datetime.timedelta(seconds=37),
        "status": status,
        "cor": "verde" if not erros else "amarelo",
        "arquivos": 20,
        "linhas_processadas": 180_187,
        "qtd_erros": erros,
        "erros": "arquivo.csv: encoding invalido" if erros else "",
        "bases": "Clientes=14854;Itens=2933;Vendedores=39",
        "resumo": "",
    }


def _tabela(nome, *, horas=1.0, linhas=1000, existe=True, carga=True):
    agora = datetime.datetime(2026, 9, 4, 11, 0)
    return {
        "tabela": nome,
        "existe": existe,
        "linhas": linhas,
        "carga_em": agora - datetime.timedelta(hours=horas) if carga else None,
        "horas": horas if carga else None,
        "velha": carga and horas > HORAS_TABELA_VELHA,
    }


_TABELAS_OK = [
    _tabela("Clientes"), _tabela("Faturamento"), _tabela("faturamento_full"),
    _tabela("Itens"),
]


# ── Janela 11h -> 11h ───────────────────────────────────────────────────

def test_janela_fecha_nas_11h_e_cobre_24h():
    # Rodando as 11h de 04/09, a janela vai de 03/09 11h a 04/09 11h.
    desde, ate = janela(datetime.datetime(2026, 9, 4, 11, 0, 12))
    assert desde == datetime.datetime(2026, 9, 3, 11, 0)
    assert ate == datetime.datetime(2026, 9, 4, 11, 0)
    assert (ate - desde) == datetime.timedelta(days=1)


def test_janela_ignora_atraso_da_tarefa_agendada():
    """A janela e a MESMA se a tarefa atrasar — o relatorio e reproduzivel.

    Se eu usasse 'agora menos 24h', um atraso de 10min deslocaria a janela e a
    rodada da borda entraria em dois relatorios (ou em nenhum).
    """
    pontual = janela(datetime.datetime(2026, 9, 4, 11, 0, 0))
    atrasada = janela(datetime.datetime(2026, 9, 4, 11, 42, 31))
    assert pontual == atrasada


def test_janela_antes_das_11h_fecha_no_corte_de_ontem():
    """CICATRIZ: rodar na mao as 9h nao pode criar janela com futuro.

    Com o corte de HOJE 11h e agora 9h, as duas ultimas horas da janela ainda
    nao aconteceram — o relatorio prometeria cobrir um periodo inexistente.
    """
    desde, ate = janela(datetime.datetime(2026, 9, 4, 9, 30))
    assert ate == datetime.datetime(2026, 9, 3, 11, 0)
    assert desde == datetime.datetime(2026, 9, 2, 11, 0)
    assert ate < datetime.datetime(2026, 9, 4, 9, 30)


def test_coletar_execucoes_filtra_pela_janela_com_parametros():
    cursor = CursorFalso(description=[("inicio",), ("status",)])
    desde = datetime.datetime(2026, 9, 3, 11, 0)
    ate = datetime.datetime(2026, 9, 4, 11, 0)
    coletar_execucoes(cursor, desde, ate)
    # Valor sempre por %s, nunca concatenado.
    assert "inicio >= %s AND inicio < %s" in cursor.sqls[0]
    assert cursor.parametros[0] == (desde, ate)


# ── Faturamento x ETL diario ────────────────────────────────────────────

def test_distingue_rodada_do_faturamento_da_execucao_diaria():
    """A separacao sai do campo `bases`, nao do horario.

    Se eu separasse por hora, a rodada do faturamento que caisse as 10h ficaria
    contada como ETL diario e o relatorio mentiria nas duas secoes.
    """
    assert _e_do_faturamento(_rodada_fat(15)) is True
    assert _e_do_faturamento(_execucao_diaria()) is False


def test_resumir_separa_as_duas_familias():
    execucoes = [_rodada_fat(14), _rodada_fat(15), _execucao_diaria()]
    d = resumir(execucoes, _TABELAS_OK)
    assert len(d["faturamento"]) == 2
    assert len(d["diarias"]) == 1
    assert d["ok"] is True


def test_rodada_das_15h_aparece_marcada_no_corpo():
    # O pedido era explicito sobre as 15h: ela tem que ser localizavel de olho.
    execucoes = [_rodada_fat(14), _rodada_fat(15), _rodada_fat(16)]
    d = resumir(execucoes, _TABELAS_OK)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    linha_15h = [l for l in corpo.splitlines() if "03/09 15:05" in l]
    assert len(linha_15h) == 1
    assert "rodada das 15h" in linha_15h[0]
    # E as vizinhas continuam la, sem marca: a das 15h so se le em contexto.
    assert "03/09 14:05" in corpo and "03/09 16:05" in corpo
    assert corpo.count("rodada das 15h") == 1


def test_faturamento_e_faturamento_full_aparecem_como_destaque():
    d = resumir([_rodada_fat(15)], _TABELAS_OK)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    assert "FATURAMENTO + FATURAMENTO_FULL" in corpo
    # As duas marcadas com * na secao de idade das tabelas.
    assert "* Faturamento " in corpo or "* Faturamento  " in corpo
    assert "* faturamento_full" in corpo


# ── Modos de falha que nao levantam excecao ─────────────────────────────

def test_zero_rodadas_de_faturamento_e_problema():
    """CICATRIZ (04/09/2026): a origem congelou e nada acusou.

    O pipeline sai calado quando o hash nao muda, entao nao ha excecao, nao ha
    alerta e o painel segue dizendo TUDO OK. Zero rodadas em 24h e o unico
    sinal que sobra.
    """
    d = resumir([_execucao_diaria()], _TABELAS_OK)
    assert d["ok"] is False
    assert any("faturamento NAO rodou" in p for p in d["problemas"])


def test_corpo_explica_por_que_zero_rodadas_e_grave():
    d = resumir([_execucao_diaria()], _TABELAS_OK)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    assert "NENHUMA rodada na janela" in corpo
    # Tem que apontar as DUAS causas possiveis, senao manda cacar a errada.
    assert "republicada" in corpo
    assert "ETL-Faturamento-Horario" in corpo


def test_etl_diario_ausente_e_problema():
    d = resumir([_rodada_fat(15)], _TABELAS_OK)
    assert d["ok"] is False
    assert any("ETL diario" in p for p in d["problemas"])


def test_tabela_velha_entra_como_problema():
    tabelas = [*_TABELAS_OK, _tabela("Itens", horas=40)]
    d = resumir([_rodada_fat(15), _execucao_diaria()], tabelas)
    assert d["ok"] is False
    assert any("sem carga ha 40h" in p for p in d["problemas"])


def test_tabela_ausente_entra_como_problema():
    tabelas = [*_TABELAS_OK, _tabela("ItensExtra1", existe=False, carga=False)]
    tabelas[-1]["velha"] = True
    d = resumir([_rodada_fat(15), _execucao_diaria()], tabelas)
    assert any("nao existe no banco" in p for p in d["problemas"])


def test_update_time_nulo_nunca_vira_nunca_carregou():
    """UPDATE_TIME NULL e "desconhecido", nao "nunca carregou".

    O InnoDB devolve NULL em varias tabelas e ZERA no restart do MySQL. Dizer
    "nunca carregou" de uma tabela com 245 mil linhas seria mentira, e e a
    leitura errada que faz alguem recarregar tudo sem precisar.
    """
    tabelas = [_tabela("Faturamento", carga=False, linhas=245_405)]
    d = resumir([_rodada_fat(15), _execucao_diaria()], tabelas)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    assert "desconhecido" in corpo
    assert "nunca carregou" not in corpo.lower()
    # Desconhecido NAO e problema: nao sei nao e o mesmo que esta velha.
    assert not any("Faturamento" in p for p in d["problemas"])


def test_execucao_com_erro_detalha_a_mensagem():
    d = resumir(
        [_rodada_fat(15), _execucao_diaria(erros=1, status="RODOU COM ERROS")],
        _TABELAS_OK,
    )
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    assert d["ok"] is False
    assert "ERROS NO DETALHE" in corpo
    assert "encoding invalido" in corpo


def test_blacklist_ficou_fora_das_tabelas_conferidas():
    """O ETL novo nao carrega mais BaseBlacklistDelinte (parada em 29/07/2026).

    Inclui-la geraria vermelho permanente e falso — o ruido que faz o leitor
    parar de ler o relatorio inteiro.
    """
    from pipelines.resumo_diario import TABELAS_CONFERIDAS

    assert "BaseBlacklistDelinte" not in [n for n, _c in TABELAS_CONFERIDAS]


# ── Dia tranquilo ───────────────────────────────────────────────────────

def test_dia_sem_problema_fica_ok_e_sem_lista_de_atencao():
    execucoes = [_rodada_fat(h) for h in (9, 12, 15, 17)] + [_execucao_diaria()]
    d = resumir(execucoes, _TABELAS_OK)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    assert d["ok"] is True
    assert d["problemas"] == []
    assert "tudo em ordem" in corpo
    assert "precisa de atencao" not in corpo


def test_corpo_diz_que_a_ausencia_do_email_e_sinal():
    """A razao de ser do relatorio periodico tem que estar escrita nele.

    Sem essa frase, quem recebe nao sabe que PARAR de receber e a informacao
    mais importante que este e-mail carrega.
    """
    d = resumir([_rodada_fat(15), _execucao_diaria()], _TABELAS_OK)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    assert "PARAR de chegar" in corpo


def test_corpo_declara_a_janela_no_topo():
    d = resumir([_rodada_fat(15), _execucao_diaria()], _TABELAS_OK)
    corpo = montar_corpo(
        d, datetime.datetime(2026, 9, 3, 11), datetime.datetime(2026, 9, 4, 11)
    )
    # Quem le as 11h precisa saber que o relatorio fala das ultimas 24h, e nao
    # do dia civil — senao procura a rodada das 15h de hoje, que nao existe.
    assert "03/09 11:00 -> 04/09 11:00" in corpo
