"""As quatro formas de escrever no MySQL: replace, truncate, date_range, upsert.

Qual usar em cada tabela está em config/tables.py.
"""

import logging
import warnings

import pandas as pd

from config.settings import TAMANHO_LOTE
from src.io.database import (
    criar_tabela,
    inserir_em_lote,
    tabela_existe,
    validar_identificador,
)

log = logging.getLogger(__name__)


def _progresso(inseridas: int, total: int) -> None:
    """Loga a cada ~50k linhas. Um log por lote seria só barulho."""
    if inseridas % 50_000 < TAMANHO_LOTE or inseridas == total:
        log.info("    %s/%s linhas...", f"{inseridas:,}", f"{total:,}")


def replace(
    cursor,
    tabela: str,
    df: pd.DataFrame,
    tipos: dict[str, str] | None = None,
    indices: list[list[str]] | None = None,
) -> int:
    """Troca o conteúdo inteiro da tabela.

    Monto numa tabela temporária e só troco no fim. Se o CREATE ou o INSERT
    falhar no meio, a tabela original continua lá — antes eu dava DROP primeiro
    e um erro depois disso deixava o banco sem a tabela.
    """
    validar_identificador(tabela)
    nova = f"{tabela}__nova"[:64]
    validar_identificador(nova)

    cursor.execute(f"DROP TABLE IF EXISTS `{nova}`")
    criar_tabela(cursor, nova, list(df.columns), tipos, indices)
    total = inserir_em_lote(cursor, nova, df, _progresso)

    cursor.execute(f"DROP TABLE IF EXISTS `{tabela}`")
    cursor.execute(f"RENAME TABLE `{nova}` TO `{tabela}`")
    log.info("    tabela `%s` trocada", tabela)
    return total


def truncate(
    cursor,
    tabela: str,
    df: pd.DataFrame,
    tipos: dict[str, str] | None = None,
    indices: list[list[str]] | None = None,
) -> int:
    """TRUNCATE (mantem schema) + insert completo."""
    validar_identificador(tabela)
    if tabela_existe(cursor, tabela):
        cursor.execute(f"TRUNCATE TABLE `{tabela}`")
        log.info("    tabela `%s` truncada", tabela)
    else:
        criar_tabela(cursor, tabela, list(df.columns), tipos, indices)
        log.info("    tabela `%s` criada", tabela)
    return inserir_em_lote(cursor, tabela, df, _progresso)


def _parsear_datas(df: pd.DataFrame, coluna: str, formato: str) -> pd.Series:
    """Tenta o formato configurado, depois os fallbacks conhecidos."""
    bruto = df[coluna].astype(str).str.strip()

    # Sem %m/%d/%Y aqui de propósito: ele lê "03/08/2026" como 8 de março e
    # a linha vai pro mês errado, calada. Se a origem mandar US um dia, prefiro
    # que aborte a que adivinhe.
    formatos = [formato] if formato else []
    for f in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ):
        if f not in formatos:
            formatos.append(f)

    datas = pd.Series(pd.NaT, index=df.index)
    for f in formatos:
        pendentes = datas.isna()
        if not pendentes.any():
            break
        datas.loc[pendentes] = pd.to_datetime(
            bruto[pendentes], format=f, errors="coerce"
        )

    if datas.isna().any():
        pendentes = datas.isna()
        # Última tentativa com o parser solto do pandas. Ele reclama que não
        # inferiu o formato, o que é esperado aqui — só sobrou o que os
        # formatos conhecidos não pegaram. Silencio o aviso.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            datas.loc[pendentes] = pd.to_datetime(
                bruto[pendentes], errors="coerce", dayfirst=True
            )

    return datas


def date_range(
    cursor,
    tabela: str,
    df: pd.DataFrame,
    coluna_data: str,
    formato_data: str = "%Y-%m-%d",
    tipos: dict[str, str] | None = None,
    indices: list[list[str]] | None = None,
) -> int:
    """Apaga so a janela de datas do arquivo e reinsere."""
    validar_identificador(tabela)
    validar_identificador(coluna_data)

    if coluna_data not in df.columns:
        raise ValueError(
            f"date_range: não achei a coluna '{coluna_data}' no arquivo. "
            f"Tem essas: {list(df.columns)}"
        )

    datas = _parsear_datas(df, coluna_data, formato_data)

    if datas.isna().all():
        raise ValueError(
            f"date_range: não consegui ler nenhuma data em '{coluna_data}'. "
            f"Exemplos: {df[coluna_data].head(5).tolist()}"
        )

    # Linha com data ilegível não pode entrar. O DELETE lá embaixo filtra por
    # STR_TO_DATE, que devolve NULL justamente nessas linhas, e NULL BETWEEN
    # nunca é verdade — então elas entram e nenhuma carga futura consegue tirar.
    # Cada rodada soma outra cópia. Melhor abortar e manter o dado de ontem.
    if datas.isna().any():
        exemplos = df.loc[datas.isna(), coluna_data].astype(str).unique()[:10]
        raise ValueError(
            f"date_range: {int(datas.isna().sum())} de {len(df)} linha(s) com "
            f"'{coluna_data}' ilegível. Abortei — essas linhas entrariam sem "
            f"data válida e ficariam presas na tabela pra sempre. "
            f"Exemplos: {exemplos.tolist()}"
        )

    data_min = datas.min().strftime("%Y-%m-%d")
    data_max = datas.max().strftime("%Y-%m-%d")
    meses = sorted(datas.dt.strftime("%Y-%m").unique())
    log.info(
        "    janela do arquivo: %s -> %s (%s mês/meses)",
        data_min,
        data_max,
        len(meses),
    )

    if not tabela_existe(cursor, tabela):
        criar_tabela(cursor, tabela, list(df.columns), tipos, indices)
        log.info("    tabela `%s` criada (primeira carga)", tabela)
    else:
        # Com sql_mode vazio o STR_TO_DATE devolve NULL numa data ruim em vez
        # de matar a query, e o COALESCE consegue tentar o formato seguinte.
        cursor.execute("SET SESSION sql_mode = ''")

        # Apago mês a mês, não o intervalo inteiro: com BETWEEN, um mês sem
        # linha no arquivo mas dentro do intervalo era apagado e nunca reposto.
        #
        # Os formatos vão como parâmetro junto com os meses. Assim não sobra
        # '%' literal na query — escapar com '%%' não funciona neste driver
        # quando a query também tem placeholder.
        marcadores = ", ".join(["%s"] * len(meses))
        cursor.execute(
            f"DELETE FROM `{tabela}` WHERE DATE_FORMAT(COALESCE("
            f"  STR_TO_DATE(`{coluna_data}`, %s),"
            f"  STR_TO_DATE(`{coluna_data}`, %s),"
            f"  STR_TO_DATE(`{coluna_data}`, %s)"
            f"), %s) IN ({marcadores})",
            (
                "%d/%m/%Y",
                "%Y-%m-%d",
                "%Y-%m-%d %H:%i:%s",
                "%Y-%m",
                *meses,
            ),
        )
        log.info(
            "    %s linha(s) apagadas em %s",
            f"{cursor.rowcount:,}",
            ", ".join(meses),
        )

        cursor.execute(
            "SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'"
        )

    return inserir_em_lote(cursor, tabela, df, _progresso)


def upsert(cursor, tabela: str, df: pd.DataFrame, chaves: list[str]) -> int:
    """Atualiza quem já existe e insere o resto. Coluna que sumiu da origem
    mantém o valor antigo."""
    validar_identificador(tabela)
    for chave in chaves:
        validar_identificador(chave)
        if chave not in df.columns:
            raise ValueError(
                f"upsert: não achei a coluna-chave '{chave}' no arquivo. "
                f"Tem essas: {list(df.columns)}"
            )

    conjunto_chaves = set(chaves)

    if not tabela_existe(cursor, tabela):
        # Chave vira VARCHAR porque LONGTEXT não aceita UNIQUE sem prefixo.
        defs = [
            f"`{c}` VARCHAR(255) NOT NULL DEFAULT ''"
            if c in conjunto_chaves
            else f"`{c}` LONGTEXT"
            for c in df.columns
        ]
        colunas_unicas = ", ".join(f"`{k}`" for k in chaves)
        cursor.execute("SET SESSION innodb_strict_mode = OFF")
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS `{tabela}` ({', '.join(defs)}, "
            f"UNIQUE KEY uq_chave ({colunas_unicas})) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC"
        )
        log.info("    tabela `%s` criada com UNIQUE(%s)", tabela, ", ".join(chaves))

    colunas = ", ".join(f"`{c}`" for c in df.columns)
    marcadores = ", ".join(["%s"] * len(df.columns))
    atualizacoes = ", ".join(
        f"`{c}` = VALUES(`{c}`)" for c in df.columns if c not in conjunto_chaves
    )
    sql = (
        f"INSERT INTO `{tabela}` ({colunas}) VALUES ({marcadores}) "
        f"ON DUPLICATE KEY UPDATE {atualizacoes}"
    )

    df = df.astype(object).where(pd.notna(df), None)

    total = len(df)
    processadas = 0
    for inicio in range(0, total, TAMANHO_LOTE):
        lote = df.iloc[inicio : inicio + TAMANHO_LOTE]
        cursor.executemany(sql, lote.values.tolist())
        processadas += len(lote)
        _progresso(processadas, total)

    return total


EXECUTORES = {
    "replace": lambda cur, tab, df, cfg: replace(
        cur, tab, df, cfg.get("tipos"), cfg.get("indices")
    ),
    "truncate": lambda cur, tab, df, cfg: truncate(
        cur, tab, df, cfg.get("tipos"), cfg.get("indices")
    ),
    "date_range": lambda cur, tab, df, cfg: date_range(
        cur,
        tab,
        df,
        cfg["coluna_data"],
        cfg.get("formato_data", "%Y-%m-%d"),
        cfg.get("tipos"),
        cfg.get("indices"),
    ),
    "upsert": lambda cur, tab, df, cfg: upsert(cur, tab, df, cfg["chaves"]),
}


def executar(cursor, tabela: str, df: pd.DataFrame, cfg: dict) -> int:
    """Chama a estratégia da config, conferindo antes se ela tem o que precisa."""
    nome = cfg.get("estrategia", "replace")

    if nome not in EXECUTORES:
        raise ValueError(
            f"Estratégia desconhecida: {nome!r}. "
            f"Tenho: {', '.join(sorted(EXECUTORES))}"
        )
    if nome == "date_range" and not cfg.get("coluna_data"):
        raise ValueError("date_range precisa de 'coluna_data' na config.")
    if nome == "upsert" and not cfg.get("chaves"):
        raise ValueError("upsert precisa de 'chaves' na config.")

    return EXECUTORES[nome](cursor, tabela, df, cfg)
