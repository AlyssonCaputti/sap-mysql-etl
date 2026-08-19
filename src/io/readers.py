"""Leitura dos arquivos que o SAP exporta.

O SAP muda o formato sem avisar — encoding, separador, cabeçalho, tudo já
mudou pelo menos uma vez. Cada proteção aqui veio de uma quebra real, e a data
está no comentário. Antes de tirar alguma, veja o que ela evita.
"""

import csv
import io
import re
import zipfile
from pathlib import Path

import pandas as pd

# Caracteres de controle que o SAP às vezes enfia no texto do .xlsx e fazem o
# openpyxl estourar com "not well-formed (invalid token)".
_XML_ILEGAL = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# Número BR, com apóstrofo opcional na frente: "399", "-6", "'-1.739".
_TOKEN_NUMERICO = re.compile(r"'?-?\d{1,3}(\.\d{3})*")
# Os 2 dígitos que, colados no anterior, formam o decimal: "399" + "89".
_TOKEN_FRACAO = re.compile(r"\d{2}")

# Descartar linha acima disso não é caso isolado, é premissa errada. Aborto.
LIMITE_DESCARTE = 0.02


def detectar_encoding(caminho: Path) -> str:
    """utf-16 se tiver BOM, senão utf-8.

    O SAP já exportou o mesmo arquivo nos dois encodings em dias diferentes.
    """
    with open(caminho, "rb") as fh:
        inicio = fh.read(4)
    if inicio.startswith(b"\xff\xfe") or inicio.startswith(b"\xfe\xff"):
        return "utf-16"
    return "utf-8"


def detectar_separador(caminho: Path, encoding: str) -> str:
    """Conta ',' e ';' no cabeçalho e fica com quem aparece mais.

    Em 01-03/08/2026 a base ficou 3 dias sem carregar por causa disso: um CSV
    separado por vírgula era lido com ';', virava uma coluna só com o cabeçalho
    inteiro no nome, e o MySQL recusava com "Identifier name too long".
    """
    with open(caminho, encoding=encoding, errors="replace") as fh:
        cabecalho = fh.readline()
    return "," if cabecalho.count(",") > cabecalho.count(";") else ";"


def _tem_xml_ilegal(bruto: bytes) -> bool:
    with zipfile.ZipFile(io.BytesIO(bruto)) as zin:
        return any(
            _XML_ILEGAL.search(zin.read(item.filename))
            for item in zin.infolist()
            if item.filename.endswith(".xml")
        )


def _sanitizar_xlsx(bruto: bytes) -> io.BytesIO:
    """Reescreve o .xlsx sem os caracteres ilegais, em memória.

    Sem recompressão: o buffer é jogado fora logo depois do parse, então
    recomprimir 26 MB custaria ~3s à toa.
    """
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(bruto)) as zin,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zout,
    ):
        for item in zin.infolist():
            dados = zin.read(item.filename)
            if item.filename.endswith(".xml"):
                dados = _XML_ILEGAL.sub(b"", dados)
            zout.writestr(item, dados)
    buffer.seek(0)
    return buffer


def ler_excel(caminho: Path, colunas: list[str] | None = None) -> pd.DataFrame:
    """Lê .xlsx pelo caminho rápido, e só sanitiza se o arquivo pedir.

    O calamine é ~5x mais rápido que o openpyxl (40s → 8s no arquivo de
    clientes). Mas ele não lê o zip reescrito, então quando tem caractere
    ilegal eu volto pro openpyxl. Checar custa 0,3s e economiza ~30s.

    Os dois engines divergem só em campo de texto com quebra de linha
    (openpyxl deixa "_x000D_", calamine devolve "\\r"). São 4 colunas de texto
    livre em clientes, nenhuma usada aqui.
    """
    bruto = caminho.read_bytes()

    if _tem_xml_ilegal(bruto):
        return pd.read_excel(_sanitizar_xlsx(bruto), usecols=colunas)

    try:
        return pd.read_excel(caminho, engine="calamine", usecols=colunas)
    except (ImportError, ValueError):
        return pd.read_excel(caminho, usecols=colunas)


def _reconstruir_linha(linha: str, n_esperado: int) -> list:
    """Remonta linha que o split por vírgula quebrou.

    Quando o separador é vírgula, o SAP exporta decimal BR sem aspas ("399,89")
    e o campo parte em dois. Junto em duas passadas: primeiro os pedaços de
    texto livre (que começam com espaço), depois os pares número + fração.
    """
    tokens = linha.split(",")

    juntos = []
    i = 0
    while i < len(tokens):
        atual = tokens[i]
        while i + 1 < len(tokens) and tokens[i + 1].startswith(" "):
            atual += "," + tokens[i + 1]
            i += 1
        juntos.append(atual)
        i += 1
    tokens = juntos

    mudou = True
    while len(tokens) > n_esperado and mudou:
        mudou = False
        for k in range(len(tokens) - 1):
            a, b = tokens[k], tokens[k + 1]
            if _TOKEN_NUMERICO.fullmatch(a) and _TOKEN_FRACAO.fullmatch(b):
                tokens[k : k + 2] = [a + "," + b]
                mudou = True
                break

    return tokens


def _precisa_reconstrucao(caminho: Path, encoding: str) -> bool:
    """O CSV está quebrado mesmo, ou só tem campo entre aspas?

    Uso o csv.reader porque ele respeita aspas. Com split(",") cru, qualquer
    arquivo bem-formado com vírgula dentro de campo citado parecia quebrado —
    era o caso de itens, e 984 das 2.936 linhas (33%) eram descartadas só com
    um aviso. Reconstruir é último recurso.
    """
    with open(caminho, encoding=encoding, errors="replace", newline="") as fh:
        leitor = csv.reader(fh)
        try:
            n_esperado = len(next(leitor))
        except StopIteration:
            return False
        for _, campos in zip(range(50), leitor):
            if len(campos) != n_esperado:
                return True
    return False


def _reconstruir_csv(caminho: Path, encoding: str) -> tuple[pd.DataFrame, list[str]]:
    """Caminho lento: remonta linha por linha quando o parser normal não dá."""
    with open(caminho, encoding=encoding) as fh:
        linhas = fh.readlines()

    cabecalho = linhas[0].strip("\n").split(",")
    n_esperado = len(cabecalho)

    boas, descartadas = [], 0
    for linha in linhas[1:]:
        linha = linha.strip("\n")
        if not linha:
            continue
        tokens = _reconstruir_linha(linha, n_esperado)
        if len(tokens) == n_esperado:
            boas.append(tokens)
        else:
            descartadas += 1

    avisos = []
    if descartadas:
        total = descartadas + len(boas)
        proporcao = descartadas / total

        if proporcao > LIMITE_DESCARTE:
            raise ValueError(
                f"{caminho.name}: {descartadas} de {total} linhas "
                f"({proporcao:.0%}) não puderam ser reconstruídas. Passou do "
                f"limite de {LIMITE_DESCARTE:.0%}, então abortei em vez de subir "
                f"base incompleta. Confira o formato do arquivo."
            )

        avisos.append(
            f"{descartadas} de {total} linha(s) ({proporcao:.1%}) descartada(s) "
            f"em {caminho.name}: não deu pra remontar os {n_esperado} campos."
        )

    return pd.DataFrame(boas, columns=cabecalho), avisos


def ler_csv(caminho: Path) -> tuple[pd.DataFrame, list[str]]:
    """Devolve (dataframe, avisos).

    Os avisos voltam em vez de irem pro log direto — quem chamou decide o que
    fazer com eles.
    """
    caminho = Path(caminho)
    encoding = detectar_encoding(caminho)
    separador = detectar_separador(caminho, encoding)

    if separador == "," and _precisa_reconstrucao(caminho, encoding):
        df, avisos = _reconstruir_csv(caminho, encoding)
    else:
        df = pd.read_csv(caminho, encoding=encoding, sep=separador, low_memory=False)
        avisos = []

    # Exportação antiga vinha com uma coluna de índice "#" na frente.
    if len(df.columns) and str(df.columns[0]) == "#":
        df = df.drop(columns=["#"])

    return df, avisos


def ler_arquivo(caminho: Path) -> tuple[pd.DataFrame, list[str]]:
    """Entrada única: olha a extensão e manda pro leitor certo."""
    caminho = Path(caminho)
    ext = caminho.suffix.lower()

    if ext == ".csv":
        return ler_csv(caminho)
    if ext in (".xlsx", ".xlsm"):
        return ler_excel(caminho), []

    raise ValueError(f"Não sei ler {ext} ({caminho.name})")
