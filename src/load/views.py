"""A view ItensCompleto, que junta Itens + ItensExtra* de volta.

Itens foi dividido porque o servidor não aguenta 475 colunas LONGTEXT numa
tabela só. A view existe pra quem consulta não precisar saber disso.
"""

import logging

from src.io.database import tabela_existe, validar_identificador

log = logging.getLogger(__name__)

CHAVE_JOIN = "numero_do_item"


def criar_view_itens_completo(cursor) -> bool:
    """Cria ou atualiza a view. Devolve False se não tiver o que juntar."""
    if not tabela_existe(cursor, "Itens"):
        log.warning("Pulei a view ItensCompleto: a tabela `Itens` não existe.")
        return False

    cursor.execute("SHOW TABLES LIKE 'ItensExtra%'")
    extras = sorted(linha[0] for linha in cursor.fetchall())
    if not extras:
        log.warning("Pulei a view ItensCompleto: não achei nenhuma ItensExtra*.")
        return False

    cursor.execute("SHOW COLUMNS FROM `Itens`")
    ja_vistas = {linha[0] for linha in cursor.fetchall()}

    selecoes = ["i.*"]
    joins = []
    for indice, tabela in enumerate(extras, 1):
        validar_identificador(tabela)
        alias = f"e{indice}"

        cursor.execute(f"SHOW COLUMNS FROM `{tabela}`")
        # Só o que ainda não apareceu, e sem repetir a chave do join.
        novas = [
            linha[0]
            for linha in cursor.fetchall()
            if linha[0] != CHAVE_JOIN and linha[0] not in ja_vistas
        ]
        ja_vistas.update(novas)

        if novas:
            selecoes.append(", ".join(f"{alias}.`{c}`" for c in novas))
        joins.append(
            f"LEFT JOIN `{tabela}` {alias} "
            f"ON i.`{CHAVE_JOIN}` = {alias}.`{CHAVE_JOIN}`"
        )

    cursor.execute(
        "CREATE OR REPLACE VIEW `ItensCompleto` AS SELECT "
        + ", ".join(selecoes)
        + " FROM `Itens` i "
        + " ".join(joins)
    )
    log.info("VIEW ItensCompleto atualizada (%s tabelas).", len(extras) + 1)
    return True
