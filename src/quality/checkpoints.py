"""Três pontos de checagem no caminho do dado, com foco em qualidade.

    porta 1  recepção   o que li da origem
    porta 2  transformação   o que sobrou depois de tratar
    saída    carga   o que o banco tem, comparado com a origem

Cada um devolve um dict de métricas e loga uma linha só. A ideia é conseguir
responder "o dado está bom?" lendo o log, sem abrir o banco.

Nada aqui escreve no banco nem levanta exceção por conta própria — quem chama
decide o que fazer. Só o `divergiu` da saída merece ação.
"""

import logging

import pandas as pd

log = logging.getLogger(__name__)

# Acima disso a origem encolheu demais pra ser normal.
QUEDA_SUSPEITA = 0.10


def porta1_recepcao(df: pd.DataFrame, origem: str, avisos: list[str]) -> dict:
    """O que chegou do arquivo, antes de qualquer transformação."""
    vazias = int(df.isna().all(axis=1).sum())
    metricas = {
        "origem": origem,
        "linhas": len(df),
        "colunas": len(df.columns),
        "linhas_vazias": vazias,
        "avisos": len(avisos),
    }

    log.info(
        "  [porta 1] %s: %s linhas x %s colunas | vazias=%s | avisos=%s",
        origem,
        f"{len(df):,}",
        len(df.columns),
        vazias,
        len(avisos),
    )
    if vazias:
        log.warning("  [porta 1] %s linha(s) totalmente vazia(s)", vazias)
    return metricas


def porta2_transformacao(
    df: pd.DataFrame,
    origem: str,
    linhas_entrada: int,
    chave: str | None = None,
    coluna_data: str | None = None,
    datas: pd.Series | None = None,
) -> dict:
    """O que sobrou depois de tratar, e se o dado faz sentido.

    Checo o que já mordeu esse pipeline: linha sumindo na transformação, chave
    duplicada ou vazia, e data fora do razoável.
    """
    perdidas = linhas_entrada - len(df)
    metricas = {
        "origem": origem,
        "linhas": len(df),
        "colunas": len(df.columns),
        "linhas_perdidas": perdidas,
    }

    log.info(
        "  [porta 2] %s: %s linhas x %s colunas | perdidas na transformação=%s",
        origem,
        f"{len(df):,}",
        len(df.columns),
        perdidas,
    )

    if perdidas > 0:
        proporcao = perdidas / linhas_entrada if linhas_entrada else 0
        log.warning(
            "  [porta 2] %s linha(s) (%.1f%%) sumiram entre a leitura e o "
            "tratamento",
            perdidas,
            proporcao * 100,
        )

    if chave and chave in df.columns:
        valores = df[chave].astype(str).str.strip()
        duplicados = int(valores.duplicated().sum())
        vazios = int((valores == "").sum())
        metricas["chave_duplicada"] = duplicados
        metricas["chave_vazia"] = vazios
        if duplicados:
            log.warning("  [porta 2] %s valor(es) de '%s' repetido(s)", duplicados, chave)
        if vazios:
            log.warning("  [porta 2] %s linha(s) com '%s' vazio", vazios, chave)

    if datas is not None:
        ilegiveis = int(datas.isna().sum())
        futuras = int((datas > pd.Timestamp.today().normalize()).sum())
        metricas["datas_ilegiveis"] = ilegiveis
        metricas["datas_futuras"] = futuras
        metricas["janela"] = (
            f"{datas.min():%Y-%m-%d} -> {datas.max():%Y-%m-%d}"
            if not datas.isna().all()
            else None
        )
        log.info(
            "  [porta 2] %s: janela %s | ilegíveis=%s | futuras=%s",
            coluna_data or "data",
            metricas["janela"],
            ilegiveis,
            futuras,
        )

    return metricas


def saida_carga(
    cursor,
    tabela: str,
    coluna_data: str,
    meses_carregados: list[str],
    contagem_origem: dict[str, int],
) -> dict:
    """Compara mês a mês o que a origem tem com o que ficou no banco.

    É o que enxerga a divergência retroativa: linha que existe na origem, está
    fora da janela, e por isso nunca sobe. Sem isso a diferença só aparece se
    alguém for conferir na mão.

    Só lê. Se a consulta falhar, aviso e sigo — a carga já está commitada.
    """
    metricas = {"tabela": tabela, "divergiu": False, "meses_divergentes": {}}

    try:
        cursor.execute(
            f"SELECT DATE_FORMAT(COALESCE("
            f"  STR_TO_DATE(`{coluna_data}`, %s),"
            f"  STR_TO_DATE(`{coluna_data}`, %s),"
            f"  STR_TO_DATE(`{coluna_data}`, %s)"
            f"), %s) AS mes, COUNT(*) FROM `{tabela}` GROUP BY mes",
            ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%i:%s", "%Y-%m"),
        )
        no_banco = {mes: n for mes, n in cursor.fetchall() if mes}
    except Exception as erro:
        log.warning("  [saída] não consegui conferir por mês: %s", erro)
        return metricas

    dentro, fora = {}, {}
    for mes, esperado in sorted(contagem_origem.items()):
        diferenca = esperado - no_banco.get(mes, 0)
        if diferenca:
            (dentro if mes in meses_carregados else fora)[mes] = diferenca

    metricas["meses_divergentes"] = {**dentro, **fora}
    metricas["divergiu"] = bool(dentro or fora)
    metricas["linhas_no_banco"] = sum(no_banco.values())

    log.info(
        "  [saída] `%s`: %s linhas no banco em %s meses",
        tabela,
        f"{sum(no_banco.values()):,}",
        len(no_banco),
    )

    if dentro:
        log.warning(
            "  [saída] divergência DENTRO da janela (era pra ter subido): %s",
            ", ".join(f"{m}={d:+,}" for m, d in sorted(dentro.items())),
        )
    if fora:
        total = sum(fora.values())
        log.warning(
            "  [saída] %s linha(s) retroativa(s) FORA da janela em %s: %s. "
            "Rode --tudo pra recuperar.",
            f"{total:,}",
            ", ".join(sorted(fora)),
            ", ".join(f"{m}={d:+,}" for m, d in sorted(fora.items())),
        )

    return metricas


def comparar_com_ultima(linhas: int, anterior: int | None, origem: str) -> bool:
    """Avisa se a origem encolheu de repente. Devolve True se está suspeito."""
    if not anterior or linhas >= anterior * (1 - QUEDA_SUSPEITA):
        return False

    log.warning(
        "  [porta 1] %s veio com %s linhas, %.0f%% menos que as %s da última "
        "vez. Confira a exportação antes de confiar nessa carga.",
        origem,
        f"{linhas:,}",
        (1 - linhas / anterior) * 100,
        f"{anterior:,}",
    )
    return True
