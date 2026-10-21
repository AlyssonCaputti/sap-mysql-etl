"""Planilha de carteiras: quem é gestor, responsável e acesso extra de cada uma.

Desde 20/10/2026 esta planilha é a fonte ÚNICA de quem vê qual carteira no hub
(o `app/scope.py` do dashboard lê `carteira_meta`). Por isso ela não sobe crua:
a origem às vezes publica o relatório de metas inteiro no lugar da planilha de
3 colunas, e cru ele quebra o acesso de todo mundo:

- o gestor só vem na primeira linha de cada grupo (célula mesclada no Excel) —
  as outras linhas subiriam sem gestor e o supervisor perderia as carteiras;
- "-No Value-" no lugar do responsável viraria uma pessoa chamada assim;
- 17 colunas de meta a mais, que o hub não lê.

Aqui eu aceito os dois formatos e devolvo sempre as mesmas 4 colunas. A coluna
"Acesso extra" é opcional na origem: são nomes, separados por vírgula, de quem
também vê a carteira sem ser o dono (ex.: um supervisor que acompanha o time de
outro). Sai sempre no arquivo, vazia quando a origem não tem, para a tabela
ter o mesmo schema todo dia.
"""

import unicodedata

import pandas as pd

COLUNAS_SAIDA = ["gestor", "vendedor", "responsavel", "acesso_extra"]

# Como a origem escreve cada coluna (comparado sem acento e sem caixa).
_APELIDOS = {
    "gestor": {"gestor"},
    "vendedor": {"vendedor", "carteira"},
    "responsavel": {"responsavel"},
    "acesso_extra": {"acesso extra", "acesso_extra"},
}
_OBRIGATORIAS = ("gestor", "vendedor", "responsavel")

# Marcadores que o relatório usa no lugar de nome. Não são pessoa.
_SEM_NOME = {"", "-no value-", "nan", "none", "sem vendedor", "sem gestor"}


def _chave(texto) -> str:
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return " ".join(s.replace("_", " ").split())


def _limpar(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    texto = " ".join(str(valor).split())
    return "" if texto.casefold() in _SEM_NOME else texto


def transformar(df: pd.DataFrame) -> pd.DataFrame:
    """Devolve gestor/vendedor/responsavel/acesso_extra, uma linha por carteira.

    Levanta ValueError em vez de adivinhar: sem uma coluna obrigatória, com
    carteira repetida ou com carteira sem gestor nenhum acima dela. Nesses
    casos quem chama mantém a tabela de ontem — subir errado aqui é tirar o
    acesso de alguém."""
    por_chave = {_chave(c): c for c in df.columns}
    origem = {}
    for destino, apelidos in _APELIDOS.items():
        achada = next((por_chave[a] for a in apelidos if a in por_chave), None)
        if achada is not None:
            origem[destino] = achada
    faltando = [c for c in _OBRIGATORIAS if c not in origem]
    if faltando:
        raise ValueError(
            f"Planilha de carteiras sem a(s) coluna(s) {faltando}. "
            f"Tem estas: {list(df.columns)}"
        )

    saida = pd.DataFrame(
        {c: df[origem[c]].map(_limpar) if c in origem else "" for c in COLUNAS_SAIDA}
    )
    # Gestor mesclado: vale o da última linha preenchida acima. Faço isso ANTES
    # de descartar linha sem carteira, para não perder um gestor que veio numa
    # linha de título.
    saida["gestor"] = saida["gestor"].replace("", pd.NA).ffill().fillna("")
    saida = saida[saida["vendedor"] != ""].reset_index(drop=True)

    if saida.empty:
        raise ValueError("Planilha de carteiras sem nenhuma carteira.")
    sem_gestor = saida.loc[saida["gestor"] == "", "vendedor"].tolist()
    if sem_gestor:
        raise ValueError(f"Carteira(s) sem gestor: {sem_gestor}")
    repetidas = saida.loc[saida["vendedor"].duplicated(), "vendedor"].tolist()
    if repetidas:
        raise ValueError(f"Carteira(s) repetida(s) na planilha: {sorted(set(repetidas))}")
    return saida[COLUNAS_SAIDA]
