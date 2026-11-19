"""Planilha do Marketing "ESOV Tracker" -> tabela longa (mes, marca, componente, valor).

A planilha não é uma tabela: cada aba tem um bloco de entrada (marca nas
linhas, um mês por coluna), um total e um bloco de share calculado por
fórmula. O hub calcula SoV/SoM/ESOV a partir do dado bruto, então aqui eu
extraio SÓ os blocos de entrada e ignoro todo o resto (totais e shares da
planilha, que inclusive têm erro de fórmula nos sub-blocos de Anúncios).

Componentes extraídos:
  search   aba Share_of_Search, bloco de entrada
  meta     aba Anúncios, bloco "1. Meta Ad Library"
  google   aba Anúncios, bloco "2. Google Ads Transparency"
  youtube  aba YouTube, bloco de entrada

Acho cada bloco pelo RÓTULO (linha "Marca", "1. Meta", "2. Google"), nunca
pela posição: a planilha é editada à mão e linha inserida não pode deslocar
dado em silêncio. Qualquer coisa fora do esperado levanta com aba e célula.

Só saem os meses MEDIDOS de cada componente (soma das 10 marcas > 0). Num mês
medido, célula vazia vale 0 (marca sem anúncio é 0, não "faltando"); mês não
medido não gera linha nenhuma, e assim o hub nunca confunde 0 com vazio.
"""

import datetime
import re
import unicodedata
from pathlib import Path

import openpyxl
import pandas as pd

# As 10 marcas do conjunto competitivo, com a grafia que o hub exibe. A lista
# é fechada de propósito: marca a mais, a menos ou com outro nome derruba a
# carga, porque o share de todas depende de o conjunto ser sempre o mesmo.
MARCAS = (
    "Delinte",
    "Speedmax",
    "Linglong",
    "Kumho",
    "XBRI",
    "Itaro",
    "Hankook",
    "Prinx/Chengshan",
    "Comforser",
    "Westlake",
)

COLUNAS_SAIDA = ["mes", "marca", "componente", "valor"]

# (aba, componente, rótulo da linha que abre o bloco ou None = primeiro bloco)
BLOCOS = (
    ("share_of_search", "search", None),
    ("anuncios", "meta", "1. meta"),
    ("anuncios", "google", "2. google"),
    ("youtube", "youtube", None),
)

_MESES = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_PADRAO_MES = re.compile(r"^([a-z]{3})/(\d{2})$")
_RODAPE = ("total", "mes medido", "participacao")


def _norm(texto) -> str:
    """Minúsculo, sem acento e sem espaço sobrando — para comparar rótulos."""
    bruto = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in bruto if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def _mes(valor) -> str | None:
    """'out/26' ou uma data do Excel -> '2026-10'. Outra coisa -> None."""
    if isinstance(valor, (datetime.date, datetime.datetime)):
        return f"{valor.year:04d}-{valor.month:02d}"
    casado = _PADRAO_MES.match(_norm(valor))
    if not casado or casado.group(1) not in _MESES:
        return None
    return f"20{casado.group(2)}-{_MESES[casado.group(1)]:02d}"


def _aba(livro, chave: str):
    for nome in livro.sheetnames:
        if _norm(nome) == chave:
            return livro[nome]
    raise ValueError(f"Não achei a aba '{chave}' na planilha do ESOV.")


def _ler_bloco(valores, formulas, componente: str, rotulo: str | None) -> list[dict]:
    """Extrai um bloco de entrada de uma aba já aberta (valores e fórmulas)."""
    aba = valores.title
    linhas = list(valores.iter_rows())

    inicio = 0
    if rotulo:
        achou = [i for i, l in enumerate(linhas) if _norm(l[0].value).startswith(rotulo)]
        if not achou:
            raise ValueError(f"{aba}: não achei o bloco que começa com '{rotulo}'.")
        inicio = achou[0]

    cab = next(
        (i for i in range(inicio, len(linhas)) if _norm(linhas[i][0].value) == "marca"),
        None,
    )
    if cab is None:
        raise ValueError(f"{aba}: não achei a linha de cabeçalho 'Marca' ({componente}).")

    # Colunas de mês: da B em diante, até a primeira que não é mês (a aba SOM,
    # por exemplo, tem "Fonte usada" no fim).
    meses: list[tuple[int, str]] = []
    for j, cel in enumerate(linhas[cab][1:], start=1):
        mes = _mes(cel.value)
        if mes is None:
            break
        meses.append((j, mes))
    if not meses:
        raise ValueError(f"{aba}!{linhas[cab][1].coordinate}: cabeçalho sem mês (esperava 'out/26').")

    brutos: dict[tuple[str, str], float] = {}
    vistas: list[str] = []
    for linha in linhas[cab + 1:]:
        nome = linha[0].value
        if nome is None or str(nome).strip() == "":
            break
        if _norm(nome).startswith(_RODAPE):
            break
        marca = next((m for m in MARCAS if _norm(m) == _norm(nome)), None)
        if marca is None:
            raise ValueError(f"{aba}!{linha[0].coordinate}: marca '{nome}' fora do conjunto das 10.")
        if marca in vistas:
            raise ValueError(f"{aba}!{linha[0].coordinate}: marca '{marca}' repetida.")
        vistas.append(marca)

        for j, mes in meses:
            cel = linha[j]
            valor = cel.value
            if valor is None:
                # Fórmula sem valor em cache = arquivo salvo por script sem
                # recalcular. Ler como vazio viraria 0 calado; prefiro parar.
                bruto = formulas[cel.coordinate].value
                if isinstance(bruto, str) and bruto.startswith("="):
                    raise ValueError(
                        f"{aba}!{cel.coordinate}: fórmula sem valor calculado. "
                        "Abra a planilha no Excel e salve de novo."
                    )
                continue
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                raise ValueError(f"{aba}!{cel.coordinate}: valor '{valor}' não é número.")
            if valor < 0:
                raise ValueError(f"{aba}!{cel.coordinate}: valor negativo ({valor}).")
            brutos[(mes, marca)] = float(valor)

    faltando = [m for m in MARCAS if m not in vistas]
    if faltando:
        raise ValueError(f"{aba} ({componente}): faltam as marcas {', '.join(faltando)}.")

    saida: list[dict] = []
    for _, mes in meses:
        total = sum(brutos.get((mes, m), 0.0) for m in MARCAS)
        if total <= 0:
            continue  # mês não medido neste componente: não gera linha
        for marca in MARCAS:
            saida.append(
                {
                    "mes": mes,
                    "marca": marca,
                    "componente": componente,
                    # 2 casas: o upload lê "1.234" como milhar no padrão BR, e
                    # com no máximo 2 decimais esse caso não existe.
                    "valor": round(brutos.get((mes, marca), 0.0), 2),
                }
            )
    return saida


def transformar(caminho: Path) -> pd.DataFrame:
    """Lê a planilha do ESOV e devolve a tabela longa dos blocos de entrada."""
    # Abro duas vezes: data_only=True dá o valor em cache das fórmulas (a
    # célula Meta/Delinte de out/26 é "=5+27"); a outra serve só para
    # distinguir "vazio" de "fórmula sem cache".
    valores = openpyxl.load_workbook(caminho, data_only=True)
    formulas = openpyxl.load_workbook(caminho, data_only=False)

    linhas: list[dict] = []
    for chave_aba, componente, rotulo in BLOCOS:
        linhas.extend(
            _ler_bloco(_aba(valores, chave_aba), _aba(formulas, chave_aba), componente, rotulo)
        )

    if not linhas:
        raise ValueError("Planilha do ESOV sem nenhum mês medido: nada para subir.")
    return pd.DataFrame(linhas, columns=COLUNAS_SAIDA)
