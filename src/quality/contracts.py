"""Normaliza nome de coluna e confere se o arquivo tem o que eu espero.

Layout da origem mudando é o que mais quebra esse pipeline — 4 vezes em 2 meses
(cabeçalho virou português, coluna sumiu, separador trocou). Por isso virou
módulo, e não um if perdido no meio do script.

Regra: obrigatória faltando aborta, opcional faltando segue com aviso.
"""

import re
import unicodedata

import pandas as pd


def normalizar_coluna(nome: str) -> str:
    """minusculas, sem acento, nao-alfanumerico vira underscore."""
    nome = str(nome).strip()
    nome = unicodedata.normalize("NFKD", nome)
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    nome = re.sub(r"[^a-z0-9]+", "_", nome.lower()).strip("_")
    return nome or "col"


def chave_comparacao(nome: str) -> str:
    """So alfanumerico minusculo — para casar nomes com grafia divergente.

    A origem ja alternou entre 'CredPresumido', 'credpresumido' e
    'red_base_pis_confins' (typo por 'cofins') para a mesma coluna.
    """
    return re.sub(r"[^a-z0-9]", "", str(nome).lower())


def normalizar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza nomes e desambigua duplicatas com sufixo numerico."""
    df = df.copy()
    df.columns = [normalizar_coluna(c) for c in df.columns]

    vistas: dict[str, int] = {}
    finais = []
    for coluna in df.columns:
        if coluna in vistas:
            vistas[coluna] += 1
            finais.append(f"{coluna}_{vistas[coluna]}")
        else:
            vistas[coluna] = 0
            finais.append(coluna)
    df.columns = finais
    return df


def validar_contrato(df: pd.DataFrame, contrato: dict, origem: str) -> list[str]:
    """Devolve avisos. Levanta se faltar coluna obrigatória."""
    presentes = set(df.columns)

    obrigatorias = set(contrato.get("obrigatorias", set()))
    faltando = obrigatorias - presentes
    if faltando:
        raise ValueError(
            f"{origem}: faltam colunas obrigatórias: {sorted(faltando)}. "
            f"Vieram essas: {sorted(presentes)}. "
            f"Abortei pra não subir dado incompleto."
        )

    avisos = []
    opcionais_faltando = set(contrato.get("opcionais", set())) - presentes
    if opcionais_faltando:
        avisos.append(
            f"{origem}: faltam colunas opcionais, seguindo sem elas: "
            f"{sorted(opcionais_faltando)}"
        )
    return avisos


def exigir_nao_vazio(df: pd.DataFrame, origem: str) -> None:
    """Arquivo vazio não pode derrubar uma tabela que estava boa."""
    if df.empty:
        raise ValueError(
            f"{origem}: arquivo sem nenhuma linha. Abortei — melhor ficar com o "
            f"dado de ontem do que zerar a tabela."
        )


def resolver_renomeacao(
    df: pd.DataFrame, mapa: dict[str, str], apelidos: dict[str, list[str]] = None
) -> tuple[dict[str, str], list[str]]:
    """Casa as colunas do arquivo com o mapa, aceitando grafias diferentes.

    Pra cada destino eu aceito o nome técnico do SAP, o próprio nome final, ou
    um apelido conhecido. Assim a origem pode alternar entre cabeçalho técnico
    e já tratado sem quebrar nada.

    Devolve (renomeação, o que não achei).
    """
    apelidos = apelidos or {}
    por_chave = {chave_comparacao(c): c for c in df.columns}

    renomeacao: dict[str, str] = {}
    ausentes: list[str] = []

    for tecnico, destino in mapa.items():
        candidatos = [tecnico, destino] + apelidos.get(destino, [])
        origem = next(
            (
                por_chave[chave_comparacao(c)]
                for c in candidatos
                if chave_comparacao(c) in por_chave
            ),
            None,
        )
        if origem is None:
            ausentes.append(destino)
        else:
            renomeacao[origem] = destino

    return renomeacao, ausentes
