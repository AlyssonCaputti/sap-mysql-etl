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


# Formatos que já vi nas origens. A ordem importa: BR antes de US, senão
# "03/08/2026" viraria 8 de março calado.
_FORMATOS_DATA = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y")


def _para_data(serie: pd.Series, com_hora: bool) -> pd.Series:
    """Converte pra datetime tentando os formatos conhecidos.

    Sem dayfirst automático: prefiro NULL a data adivinhada no mês errado.
    """
    bruto = serie.astype(str).str.strip().replace({"": None, "nan": None, "NaT": None})
    resultado = pd.Series(pd.NaT, index=serie.index, dtype="datetime64[ns]")

    for formato in _FORMATOS_DATA:
        faltando = resultado.isna() & bruto.notna()
        if not faltando.any():
            break
        resultado.loc[faltando] = pd.to_datetime(
            bruto[faltando], format=formato, errors="coerce"
        )

    return resultado if com_hora else resultado.dt.normalize()


def _para_numero(serie: pd.Series) -> pd.Series:
    """Converte pra número aceitando o formato BR (1.234,56)."""
    bruto = serie.astype(str).str.strip()
    # Só troco separador quando a vírgula é decimal; "1,234" com ponto ausente
    # é ambíguo, então trato o padrão BR completo primeiro.
    br = bruto.str.match(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$")
    bruto = bruto.mask(br, bruto.str.replace(".", "", regex=False))
    bruto = bruto.str.replace(",", ".", regex=False)
    bruto = bruto.replace({"": None, "nan": None, "None": None})
    return pd.to_numeric(bruto, errors="coerce")


def converter_tipos(
    df: pd.DataFrame, tipos: dict[str, str] | None
) -> tuple[pd.DataFrame, list[str]]:
    """Aplica os tipos declarados e devolve avisos do que não converteu.

    O que não tem tipo declarado vira string, como sempre — o destino é
    LONGTEXT. Só as colunas tipadas saem desse caminho.

    Valor que não converte fica NULL e entra no aviso. Não abortar aqui é
    decisão: as portas de qualidade já barraram arquivo vazio ou fora de
    contrato, e perder uma célula ilegível é melhor que perder a carga.
    """
    tipos = tipos or {}
    avisos: list[str] = []
    saida = df.copy()

    for coluna, tipo in tipos.items():
        if coluna not in saida.columns:
            continue

        antes = saida[coluna].astype(str).str.strip().replace({"": None, "nan": None})
        preenchidas = int(antes.notna().sum())

        if tipo in ("data", "datahora"):
            convertido = _para_data(saida[coluna], com_hora=(tipo == "datahora"))
        elif tipo in ("dinheiro", "inteiro", "decimal"):
            convertido = _para_numero(saida[coluna])
            if tipo == "inteiro":
                convertido = convertido.round().astype("Int64")
        else:
            # texto/codigo: só limpo e deixo o MySQL truncar se precisar
            saida[coluna] = saida[coluna].astype(str).str.strip().replace(
                {"": None, "nan": None}
            )
            continue

        perdidas = int((convertido.isna() & antes.notna()).sum())
        if perdidas:
            exemplos = (
                saida.loc[convertido.isna() & antes.notna(), coluna]
                .astype(str)
                .unique()[:3]
                .tolist()
            )
            avisos.append(
                f"{coluna}: {perdidas} de {preenchidas} valor(es) não "
                f"converteram pra {tipo} e ficaram NULL. Ex.: {exemplos}"
            )
        saida[coluna] = convertido

    # O resto segue string, que é o que o LONGTEXT espera.
    restantes = [c for c in saida.columns if c not in tipos]
    if restantes:
        saida[restantes] = saida[restantes].fillna("").astype(str)

    return saida, avisos
