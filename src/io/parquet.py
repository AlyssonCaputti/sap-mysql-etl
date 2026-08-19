"""Cache do faturamento em Parquet, particionado por mês.

POR QUE ISSO EXISTE
A origem publica o CSV inteiro (100 MB, 256 mil linhas) de hora em hora, mas o
que muda é quase só o mês corrente — 1,3% do total. Reprocessar tudo pra
atualizar isso é desperdício.

Aqui eu converto o CSV tratado num Parquet particionado por mês e leio só a
janela que interessa. Os números medidos:

    CSV inteiro   100 MB   11,0s
    Parquet       16 MB     0,1s
    só 2 meses     -        0,0s

A partição fica em dados/faturamento_parquet/mes=YYYY-MM/.
"""

import shutil
from pathlib import Path

import pandas as pd

COLUNA_PARTICAO = "mes"

# Acima disso é o --tudo pedindo o histórico inteiro, não uma janela.
LIMITE_JANELA = 24


def gravar_particionado(df: pd.DataFrame, destino: Path, meses: pd.Series) -> int:
    """Grava o DataFrame particionado por mês. Devolve quantas partições saíram.

    `meses` é a série YYYY-MM já calculada (evito reparsear a data aqui).
    Reescrevo só as partições presentes no DataFrame — as outras ficam onde
    estão, que é justamente a graça de particionar.
    """
    destino.mkdir(parents=True, exist_ok=True)

    df = df.copy()
    df[COLUNA_PARTICAO] = meses.values

    escritas = 0
    for mes, grupo in df.groupby(COLUNA_PARTICAO, sort=True):
        pasta = destino / f"{COLUNA_PARTICAO}={mes}"
        # Apago antes pra não acumular arquivo de escrita anterior na mesma
        # partição (o pandas gera nome aleatório a cada gravação).
        if pasta.exists():
            shutil.rmtree(pasta)
        pasta.mkdir(parents=True)
        grupo.drop(columns=[COLUNA_PARTICAO]).to_parquet(
            pasta / "dados.parquet", index=False
        )
        escritas += 1

    return escritas


def meses_disponiveis(origem: Path) -> list[str]:
    """Lista os meses que existem na partição, do mais antigo pro mais novo."""
    if not origem.is_dir():
        return []
    return sorted(
        p.name.split("=", 1)[1]
        for p in origem.iterdir()
        if p.is_dir() and p.name.startswith(f"{COLUNA_PARTICAO}=")
    )


def ler_meses(origem: Path, meses: list[str]) -> pd.DataFrame:
    """Lê só as partições pedidas. Mês que não existe é ignorado."""
    partes = []
    for mes in meses:
        pasta = origem / f"{COLUNA_PARTICAO}={mes}"
        if pasta.is_dir():
            partes.append(pd.read_parquet(pasta))

    if not partes:
        return pd.DataFrame()
    return pd.concat(partes, ignore_index=True)


def ultimos_meses(
    origem: Path, quantidade: int, referencia: pd.Timestamp | None = None
) -> list[str]:
    """Os N meses do calendário (corrente pra trás) que existem na partição.

    Amarro no calendário e não nos meses que existem: senão uma partição com
    data futura trava a janela, e um mês sem emissão faz a janela alcançar um
    mês antigo que o DELETE apaga sem repor.
    """
    existentes = meses_disponiveis(origem)
    if quantidade > LIMITE_JANELA:
        return existentes

    hoje = referencia if referencia is not None else pd.Timestamp.today()
    esperados = {
        (hoje - pd.DateOffset(months=i)).strftime("%Y-%m") for i in range(quantidade)
    }
    return [m for m in existentes if m in esperados]
