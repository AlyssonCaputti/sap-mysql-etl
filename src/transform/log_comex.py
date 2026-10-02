"""Tratamento do LogComex (inteligência de importação por NCM).

A origem publica um .xlsx por mês na mesma pasta (nenhum tem coluna de
período, só o nome do arquivo diz o mês). Junto tudo em uma tabela só.
"""

import re
from pathlib import Path

import pandas as pd

# A primeira linha do arquivo é um título ("LogComex — NCM 40111000"), o
# cabeçalho real é a segunda. Pular com skiprows na leitura.
LINHAS_DE_TITULO = 1

# "logcomex_ncm_intel_2026-01_a_2026-01.xlsx" -> "2026-01"
_PADRAO_MES = re.compile(r"(\d{4}-\d{2})_a_\d{4}-\d{2}")


def extrair_mes_referencia(nome_arquivo: str) -> str:
    casado = _PADRAO_MES.search(nome_arquivo)
    if not casado:
        raise ValueError(
            f"Não consegui extrair o mês de referência de {nome_arquivo!r}. "
            f"Esperava algo como 'logcomex_ncm_intel_2026-01_a_2026-01.xlsx'."
        )
    return casado.group(1)


def transformar(arquivos: list[Path]) -> pd.DataFrame:
    """Lê cada .xlsx do LogComex e devolve tudo concatenado, com mes_referencia."""
    if not arquivos:
        raise ValueError("Nenhum arquivo do LogComex para processar.")

    lotes = []
    for arquivo in sorted(arquivos):
        mes = extrair_mes_referencia(arquivo.name)
        df = pd.read_excel(arquivo, skiprows=LINHAS_DE_TITULO)
        df["mes_referencia"] = mes
        lotes.append(df)

    return pd.concat(lotes, ignore_index=True)
