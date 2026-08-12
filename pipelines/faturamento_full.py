"""Monta a tabela `faturamento_full` juntando Faturamento + Clientes + Itens.

Roda depois do upload, quando as três fontes já estão no banco. Limpa a tabela
e reinsere tudo.

A `mc` é calculada aqui, não copiada: a coluna `mc` da origem vem com valor
errado. A fórmula é receita menos custos e impostos.

A tabela precisa existir antes — quem cria é a migration 002 do a API de dashboards.
"""

import logging
import sys
import time

from config.settings import PASTA_LOGS
from src.io.database import conexao

log = logging.getLogger(__name__)


# Os valores vêm como texto BR ("819,79"), então troco a vírgula por ponto e
# converto. Vazio vira NULL pra não entrar como zero numa média.
def _num(coluna: str, alias: str = "f") -> str:
    return (
        f"CAST(REPLACE(NULLIF(TRIM({alias}.`{coluna}`),''),',','.') "
        f"AS DECIMAL(18,4))"
    )


# Igual ao _num, mas vazio vira 0. Uso nas parcelas da MC: com NULL, uma
# parcela vazia contamina a soma inteira e zera a margem da linha.
def _num0(coluna: str, alias: str = "f") -> str:
    return (
        f"CAST(REPLACE(COALESCE(NULLIF(TRIM({alias}.`{coluna}`),''),'0'),',','.') "
        f"AS DECIMAL(18,4))"
    )


_MC_POSITIVAS = ("valor_total", "cred_presumido", "red_base_pis_cofins", "cashback")
_MC_NEGATIVAS = (
    "custo_contabil_total",
    "icms",
    "st",
    "ipi",
    "pis",
    "cofins",
    "fcp",
    "icmsdest",
)
_MC_SQL = (
    "(("
    + " + ".join(_num0(c) for c in _MC_POSITIVAS)
    + ") - "
    + " - ".join(_num0(c) for c in _MC_NEGATIVAS)
    + ")"
)

# YYYYMM (ex.: 202601) -> "jan/26"
_MES_ANO_SQL = """
    CASE
        WHEN f.ano_mes_n_fe IS NULL OR TRIM(f.ano_mes_n_fe) = '' THEN NULL
        ELSE CONCAT(
            CASE SUBSTRING(LPAD(f.ano_mes_n_fe, 6, '0'), 5, 2)
                WHEN '01' THEN 'jan' WHEN '02' THEN 'fev' WHEN '03' THEN 'mar'
                WHEN '04' THEN 'abr' WHEN '05' THEN 'mai' WHEN '06' THEN 'jun'
                WHEN '07' THEN 'jul' WHEN '08' THEN 'ago' WHEN '09' THEN 'set'
                WHEN '10' THEN 'out' WHEN '11' THEN 'nov' WHEN '12' THEN 'dez'
            END, '/',
            SUBSTRING(LPAD(f.ano_mes_n_fe, 6, '0'), 3, 2)
        )
    END
"""


# A ilha sai do prefixo do vendedor ("GRW-FULANO"). O BINARY deixa o match
# sensível a maiúscula, então só GRW/SA/KA exatos valem.
def _ilha_de_vendedor(alias: str = "f") -> str:
    return f"""
    CASE BINARY SUBSTRING_INDEX({alias}.vendedor, '-', 1)
        WHEN 'GRW' THEN 'GRW'
        WHEN 'SA'  THEN 'SA'
        WHEN 'KA'  THEN 'KA'
        ELSE 'outros'
    END
"""


def _carteira_de_ilha(expressao: str) -> str:
    return f"""
    CASE ({expressao})
        WHEN 'KA'  THEN 'Key Account'
        WHEN 'GRW' THEN 'Growth'
        WHEN 'SA'  THEN 'Sales Account'
        ELSE 'Outros'
    END
"""


# emissao chega como texto (DD/MM/YYYY ou ISO). Converto pra DATE, senão a
# ordenação por recência sai errada.
_EMISSAO_DATE = """
    COALESCE(
        STR_TO_DATE(emissao, '%Y-%m-%d %H:%i:%s'),
        STR_TO_DATE(emissao, '%Y-%m-%d'),
        STR_TO_DATE(emissao, '%d/%m/%Y')
    )
"""

# A ilha do cliente vem da última compra dele da marca marca_foco — a linha de
# maior emissão, desempatando pelo maior valor_total. Quem nunca comprou
# marca_foco fica como 'outros'.
#
# Fica verboso porque evito ROW_NUMBER (pra rodar em MySQL 5.x): isolo as
# linhas marca_foco, acho o par (emissão, valor) máximo de cada cliente e junto
# de volta. O MAX no fim garante uma linha por cliente mesmo com empate.
_ILHA_MARCA_FOCO = f"""
    SELECT
        d.cod_cliente,
        MAX({_ilha_de_vendedor("d")}) AS ilha
    FROM (
        SELECT
            cod_cliente, vendedor,
            {_EMISSAO_DATE} AS emissao_dt,
            CAST(REPLACE(NULLIF(TRIM(valor_total),''),',','.') AS DECIMAL(18,4)) AS valor_total_num
        FROM Faturamento
        WHERE UPPER(TRIM(marca)) = 'MARCA_FOCO'
    ) d
    JOIN (
        SELECT cod_cliente, MAX(emissao_dt) AS max_emissao
        FROM (
            SELECT cod_cliente, {_EMISSAO_DATE} AS emissao_dt
            FROM Faturamento
            WHERE UPPER(TRIM(marca)) = 'MARCA_FOCO'
        ) e
        GROUP BY cod_cliente
    ) ult ON ult.cod_cliente = d.cod_cliente AND ult.max_emissao = d.emissao_dt
    JOIN (
        SELECT cod_cliente, emissao_dt, MAX(valor_total_num) AS max_valor
        FROM (
            SELECT
                cod_cliente,
                {_EMISSAO_DATE} AS emissao_dt,
                CAST(REPLACE(NULLIF(TRIM(valor_total),''),',','.') AS DECIMAL(18,4)) AS valor_total_num
            FROM Faturamento
            WHERE UPPER(TRIM(marca)) = 'MARCA_FOCO'
        ) v
        GROUP BY cod_cliente, emissao_dt
    ) vmax ON vmax.cod_cliente = d.cod_cliente
          AND vmax.emissao_dt = d.emissao_dt
          AND vmax.max_valor = d.valor_total_num
    GROUP BY d.cod_cliente
"""

INSERT_SQL = f"""
INSERT INTO faturamento_full (
    filial, pedido, nota_sap, emissao, mes_ano, ano_mes_n_fe, tipo_doc, nota_item,
    cod_cliente, nome_cliente, nota, uf, cidade, cep, vendedor,
    cod_item, descricao_item, quantidade, linha, marca, aro, ncm, utilizacao,
    grupode_precos, ll_grupode_precos,
    preco, valor_pacote, promocao, formade_pagamento, prazo_final_pagamento, listade_precos,
    valor_total, custo_contabil, custo_contabil_total, performance, frete, despesas_variaveis, cashback, mc,
    icms, st, ipi, pis, cofins, fcp, icmsdest, cred_presumido, red_base_pis_cofins,
    taxaicms, taxast, taxaipi, taxapis, taxacofins, taxafcp, taxaicmsdest,
    cnpj, ilha, supervisor, carteira, vendedoratendente,
    nicho, limite_credito, saldo_em_conta, cod_grupo, nome_grupo
)
SELECT
    f.filial, f.pedido, f.nota_sap,
    COALESCE(
        STR_TO_DATE(f.emissao, '%Y-%m-%d %H:%i:%s'),
        STR_TO_DATE(f.emissao, '%Y-%m-%d'),
        STR_TO_DATE(f.emissao, '%d/%m/%Y')
    )                                                                AS emissao,
    {_MES_ANO_SQL}                                                   AS mes_ano,
    CAST(NULLIF(TRIM(f.ano_mes_n_fe),'') AS UNSIGNED)                AS ano_mes_n_fe,
    f.tipo_doc, f.nota_item,
    f.cod_cliente, f.nome_cliente, f.nota, f.uf, f.cidade, f.cep, f.vendedor,
    f.cod_item, f.descricao_item,
    {_num("quantidade")}                                             AS quantidade,
    f.linha, f.marca, f.aro, f.ncm, f.utilizacao,
    f.grupode_precos, f.ll_grupode_precos,
    {_num("preco")}                                                  AS preco,
    {_num("valor_pacote")}                                           AS valor_pacote,
    f.promocao, f.formade_pagamento, f.prazo_final_pagamento, f.listade_precos,
    {_num("valor_total")}                                            AS valor_total,
    {_num("custo_contabil")}                                         AS custo_contabil,
    {_num("custo_contabil_total")}                                   AS custo_contabil_total,
    {_num("performance")}                                            AS performance,
    {_num("frete")}                                                  AS frete,
    {_num("despesas_variaveis")}                                     AS despesas_variaveis,
    {_num("cashback")}                                               AS cashback,
    {_MC_SQL}                                                        AS mc,
    {_num("icms")}                                                   AS icms,
    {_num("st")}                                                     AS st,
    {_num("ipi")}                                                    AS ipi,
    {_num("pis")}                                                    AS pis,
    {_num("cofins")}                                                 AS cofins,
    {_num("fcp")}                                                    AS fcp,
    {_num("icmsdest")}                                               AS icmsdest,
    {_num("cred_presumido")}                                         AS cred_presumido,
    {_num("red_base_pis_cofins")}                                    AS red_base_pis_cofins,
    {_num("taxaicms")}                                               AS taxaicms,
    {_num("taxast")}                                                 AS taxast,
    {_num("taxaipi")}                                                AS taxaipi,
    {_num("taxapis")}                                                AS taxapis,
    {_num("taxacofins")}                                             AS taxacofins,
    {_num("taxafcp")}                                                AS taxafcp,
    {_num("taxaicmsdest")}                                           AS taxaicmsdest,
    c.cnpj_cpf                                                       AS cnpj,
    COALESCE(d.ilha, 'outros')                                       AS ilha,
    c.supervisor                                                     AS supervisor,
    {_carteira_de_ilha("COALESCE(d.ilha, 'outros')")}                AS carteira,
    c.vendedoratendente                                              AS vendedoratendente,
    i.nicho_1                                                        AS nicho,
    {_num("limite_de_credito", "c")}                                 AS limite_credito,
    {_num("saldo_em_conta", "c")}                                    AS saldo_em_conta,
    c.cod_grupo_economico                                            AS cod_grupo,
    c.nome_do_grupo_economico                                        AS nome_grupo
FROM Faturamento f
LEFT JOIN (
    SELECT
        codigo_do_pn,
        MAX(cnpj_cpf)                 AS cnpj_cpf,
        MAX(ilha)                     AS ilha,
        MAX(supervisor)               AS supervisor,
        MAX(carteira)                 AS carteira,
        MAX(vendedoratendente)        AS vendedoratendente,
        MAX(limite_de_credito)        AS limite_de_credito,
        MAX(saldo_em_conta)           AS saldo_em_conta,
        MAX(cod_grupo_economico)      AS cod_grupo_economico,
        MAX(nome_do_grupo_economico)  AS nome_do_grupo_economico
    FROM Clientes
    GROUP BY codigo_do_pn
) c ON c.codigo_do_pn = f.cod_cliente
LEFT JOIN (
    SELECT numero_do_item, MAX(nicho_1) AS nicho_1
    FROM Itens
    GROUP BY numero_do_item
) i ON i.numero_do_item = f.cod_item
LEFT JOIN (
{_ILHA_MARCA_FOCO}
) d ON d.cod_cliente = f.cod_cliente
"""

# Colunas de Clientes que a migration original não criou. Adiciono aqui se
# faltarem (roda quantas vezes precisar). `ilha` já existe no schema base.
_COLUNAS_CLIENTE = (
    ("supervisor", "VARCHAR(128)", "ilha"),
    ("carteira", "VARCHAR(64)", "supervisor"),
    ("vendedoratendente", "VARCHAR(128)", "carteira"),
)

# O GROUP BY nas subqueries já impede que chave repetida multiplique linha do
# faturamento. Aqui eu só aviso, porque a causa costuma estar na origem.
_CHECAGENS = (("Clientes", "codigo_do_pn"), ("Itens", "numero_do_item"))


def avisar_chaves_duplicadas(cursor) -> None:
    for tabela, chave in _CHECAGENS:
        cursor.execute(
            f"SELECT COUNT(*) FROM ("
            f"  SELECT `{chave}` FROM `{tabela}`"
            f"  GROUP BY `{chave}` HAVING COUNT(*) > 1"
            f") d"
        )
        quantidade = cursor.fetchone()[0]
        if quantidade:
            log.warning(
                "%s valor(es) de '%s' duplicado(s) em %s. O JOIN usa um "
                "registro por chave (MAX); revise a origem.",
                quantidade,
                chave,
                tabela,
            )


def garantir_colunas(cursor) -> None:
    cursor.execute("SHOW COLUMNS FROM faturamento_full")
    existentes = {linha[0] for linha in cursor.fetchall()}
    for nome, tipo, depois in _COLUNAS_CLIENTE:
        if nome not in existentes:
            cursor.execute(
                f"ALTER TABLE faturamento_full "
                f"ADD COLUMN `{nome}` {tipo} NULL AFTER `{depois}`"
            )
            log.info("coluna '%s' adicionada a faturamento_full", nome)


def configurar_log() -> None:
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(
                PASTA_LOGS / "faturamento_full.log", encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    configurar_log()
    log.info("=" * 60)
    log.info("Materializando faturamento_full...")
    inicio = time.time()

    try:
        with conexao() as con:
            cursor = con.cursor()

            cursor.execute("SHOW TABLES LIKE 'faturamento_full'")
            if not cursor.fetchone():
                raise RuntimeError(
                    "Tabela faturamento_full nao existe. Rode antes: "
                    "python a API de dashboards/migrations/002_create_faturamento_full.py"
                )

            garantir_colunas(cursor)
            avisar_chaves_duplicadas(cursor)

            cursor.execute("TRUNCATE TABLE faturamento_full")
            log.info("faturamento_full truncada")

            # sql_mode='' para STR_TO_DATE devolver NULL em data ruim, em vez
            # de abortar a query inteira.
            cursor.execute("SET SESSION sql_mode = ''")
            cursor.execute(INSERT_SQL)
            log.info("%s linhas inseridas", f"{cursor.rowcount:,}")

            con.commit()
            cursor.close()
    except Exception as erro:
        log.error("FALHA: %s", erro)
        log.debug("traceback", exc_info=True)
        return 1

    log.info("OK — concluido em %.1fs", time.time() - inicio)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
