"""Testes do orquestrador de carga: correcoes 2 e 3.

Ambas tratam de falha INVISIVEL — o pipeline seguia como se tivesse dado certo.
Sao testadas de ponta a ponta com pasta temporaria e cursor falso, sem banco.
"""

import pandas as pd
import pytest

import pipelines.upload as upload


class CursorFalso:
    """Cursor minimo. Responde COUNT(*) com o total realmente 'inserido',
    para que a conferencia pos-carga tenha o que comparar."""

    def __init__(self, contagem_forcada=None):
        self.executados = []
        self.rowcount = 0
        self.inseridas = 0
        self.contagem_forcada = contagem_forcada
        self._ultimo = None

    def execute(self, sql, params=None):
        self.executados.append(sql)
        self._ultimo = sql

    def executemany(self, sql, seq):
        self.executados.append(sql)
        self._ultimo = sql
        if sql.strip().upper().startswith("INSERT"):
            self.inseridas += len(seq)

    def fetchone(self):
        if self._ultimo and "COUNT(*)" in self._ultimo:
            if self.contagem_forcada is not None:
                return (self.contagem_forcada,)
            return (self.inseridas,)
        return ("Tabela",)

    def fetchall(self):
        return []

    def close(self):
        pass


class ConexaoFalsa:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Monta entrada/backup temporarios e injeta a conexao falsa."""
    entrada = tmp_path / "entrada"
    backup = tmp_path / "backup"
    entrada.mkdir()
    backup.mkdir()

    cursor = CursorFalso()
    conexao = ConexaoFalsa(cursor)

    monkeypatch.setattr(upload, "ENTRADA_VPS", entrada)
    monkeypatch.setattr(upload, "BACKUP", backup)
    monkeypatch.setattr(upload, "conexao", lambda: conexao)

    return entrada, backup, conexao


def _criar_csv(pasta, nome, conteudo):
    destino = pasta / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(conteudo, encoding="utf-8")
    return destino


# ─────────────────────────────────────────────────────────────
# CORRECAO 2 — backup so depois de carga bem-sucedida
# ─────────────────────────────────────────────────────────────
def test_arquivo_ilegivel_permanece_na_entrada(ambiente, monkeypatch):
    """No original, leitura falhava, upload_file devolvia 0 sem levantar, e o
    fluxo seguia para commit + backup: o arquivo saia da entrada sem nunca ter
    entrado no banco. Aconteceu em 05/08/2026 ('File is not a zip file')."""
    entrada, backup, _ = ambiente
    arquivo = _criar_csv(entrada / "itens", "quebrado.xlsx", "isto nao e um xlsx")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})

    falhas = upload.varrer()

    assert falhas == 1
    assert arquivo.exists(), "arquivo deve permanecer na entrada para nova tentativa"
    assert not list(backup.rglob("*.xlsx")), "nao pode ter ido para o backup"


def test_arquivo_vazio_nao_vai_para_backup(ambiente, monkeypatch):
    entrada, backup, conexao = ambiente
    arquivo = _criar_csv(entrada / "itens", "vazio.csv", "sku;valor\n")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})

    falhas = upload.varrer()

    assert falhas == 1
    assert arquivo.exists()
    assert conexao.rollbacks >= 1


def test_carga_bem_sucedida_move_para_backup(ambiente, monkeypatch):
    entrada, backup, conexao = ambiente
    arquivo = _criar_csv(entrada / "itens", "ok.csv", "sku;valor\nA1;10\nA2;20\n")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})

    falhas = upload.varrer()

    assert falhas == 0
    assert not arquivo.exists(), "arquivo carregado deve sair da entrada"
    assert len(list(backup.rglob("ok.csv"))) == 1
    assert conexao.commits >= 1


# ─────────────────────────────────────────────────────────────
# CORRECAO 3 — falha de arquivo chega ao exit code
# ─────────────────────────────────────────────────────────────
def test_main_devolve_erro_quando_arquivo_falha(ambiente, monkeypatch):
    """O original saia com 0 e o rodar_etl.ps1 registrava SUCESSO enquanto
    arquivos falhavam — foi o que escondeu a quebra de 01 a 03/08/2026."""
    entrada, _, _ = ambiente
    _criar_csv(entrada / "itens", "quebrado.xlsx", "nao e xlsx")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})
    monkeypatch.setattr(upload, "configurar_log", lambda _: None)

    assert upload.main() == 1


def test_falha_de_infraestrutura_nao_vira_traceback(tmp_path, monkeypatch, caplog):
    """Banco fora do ar deve dar mensagem operacional + exit 1, nao traceback.

    Foi o caso da execucao de 12/08/2026 10:15: morreu na conexao e o log
    parou em 'scan starting', sem dizer o motivo.
    """
    entrada = tmp_path / "entrada"
    (entrada / "itens").mkdir(parents=True)
    (entrada / "itens" / "x.csv").write_text("a;b\n1;2\n", encoding="utf-8")

    def conexao_que_falha():
        raise RuntimeError("2003 (HY000): Can't connect to MySQL server")

    monkeypatch.setattr(upload, "ENTRADA_VPS", entrada)
    monkeypatch.setattr(upload, "conexao", conexao_que_falha)
    monkeypatch.setattr(upload, "configurar_log", lambda _: None)

    assert upload.main() == 1
    assert "INFRAESTRUTURA" in caplog.text
    assert "Can't connect" in caplog.text


def test_main_devolve_zero_quando_tudo_carrega(ambiente, monkeypatch):
    entrada, _, _ = ambiente
    _criar_csv(entrada / "itens", "ok.csv", "sku;valor\nA1;10\n")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})
    monkeypatch.setattr(upload, "configurar_log", lambda _: None)

    assert upload.main() == 0


def test_um_arquivo_ruim_nao_impede_os_outros(ambiente, monkeypatch):
    """Isolamento por arquivo: o ruim falha, o bom carrega. Mas o exit code
    ainda acusa a falha."""
    entrada, backup, _ = ambiente
    ruim = _criar_csv(entrada / "itens", "a_ruim.xlsx", "nao e xlsx")
    bom = _criar_csv(entrada / "clientes", "b_bom.csv", "codigo_do_pn;nome\nC1;X\n")

    monkeypatch.setattr(
        upload,
        "ESTRATEGIAS",
        {"itens": {"estrategia": "replace"}, "clientes": {"estrategia": "replace"}},
    )

    falhas = upload.varrer()

    assert falhas == 1
    assert ruim.exists(), "o que falhou fica"
    assert not bom.exists(), "o que carregou sai"
    assert len(list(backup.rglob("b_bom.csv"))) == 1


# ─────────────────────────────────────────────────────────────
# Nome de tabela a partir da pasta
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Faturamento é do pipeline horário, não do diário
# ─────────────────────────────────────────────────────────────
def test_diario_nao_carrega_faturamento(ambiente, monkeypatch):
    """O faturamento roda de hora em hora. Se o diário carregasse também, as
    duas execuções disputariam a mesma tabela."""
    entrada, backup, _ = ambiente
    fat = _criar_csv(entrada / "faturamento", "nf.csv", "emissao;valor\n01/08/2026;10\n")
    outro = _criar_csv(entrada / "itens", "ok.csv", "sku;valor\nA1;10\n")

    monkeypatch.setattr(
        upload,
        "ESTRATEGIAS",
        {"itens": {"estrategia": "replace"}, "faturamento": {"estrategia": "replace"}},
    )

    assert upload.varrer() == 0

    assert fat.exists(), "faturamento tem que ficar pro pipeline horário"
    assert not outro.exists(), "itens é do diário, deve ter sido carregado"


def test_da_pra_pedir_o_faturamento_explicitamente(ambiente, monkeypatch):
    """Passando pular=set(), o upload volta a processar tudo — é assim que o
    pipeline horário reusa esta função."""
    entrada, _, _ = ambiente
    fat = _criar_csv(entrada / "faturamento", "nf.csv", "sku;valor\nA;1\n")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"faturamento": {"estrategia": "replace"}})

    assert upload.varrer(pular=set()) == 0
    assert not fat.exists(), "com pular vazio, o faturamento é carregado"


@pytest.mark.parametrize(
    "pasta,esperado",
    [
        ("faturamento", "Faturamento"),
        ("sku_custo", "SkuCusto"),
        ("Vendedores ilha 1", "VendedoresIlha1"),
        ("itens", "Itens"),
        ("base-blacklist-marca", "BaseBlacklistMarca"),
        ("v4 Google Ads", "V4GoogleAds"),
    ],
)
def test_nome_tabela(pasta, esperado):
    assert upload.nome_tabela(pasta) == esperado


@pytest.mark.parametrize(
    "pasta,esperado",
    [
        ("integração", "Integracao"),
        ("posição estoque", "PosicaoEstoque"),
        ("preço promocao", "PrecoPromocao"),
    ],
)
def test_nome_tabela_translitera_acento(pasta, esperado):
    """Bug encontrado numa pasta de producao com "ç" no nome.

    O split direto em [^a-zA-Z0-9] tratava o acento como separador: "preço"
    virava "pre" + "o". Presente tambem no codigo original.

    A pasta que expos o bug esta hoje em NOMES_FIXOS (ver o teste abaixo), mas a
    correcao continua valendo pra qualquer outra pasta acentuada.
    """
    assert upload.nome_tabela(pasta) == esperado


def test_nome_fixo_preserva_o_nome_que_esta_no_banco(monkeypatch):
    """CICATRIZ: tabela que existe no banco com o nome corrompido pelo bug do ç.

    A transliteracao geraria o nome certo — tabela NOVA, deixando a antiga orfa
    e o consumidor lendo dado congelado. A entrada em NOMES_FIXOS mantem o nome
    que ja esta la.
    """
    monkeypatch.setattr(upload, "NOMES_FIXOS", {"tabela-preço-x": "TabelaPreOX"})

    assert upload.nome_tabela("tabela-preço-x") == "TabelaPreOX"


def test_nome_fixo_nao_depende_de_caixa_da_pasta(monkeypatch):
    # O nome da pasta na rede pode vir com caixa diferente.
    monkeypatch.setattr(upload, "NOMES_FIXOS", {"tabela-preço-x": "TabelaPreOX"})

    assert upload.nome_tabela("Tabela-Preço-X") == "TabelaPreOX"
    assert upload.nome_tabela("  tabela-preço-x  ") == "TabelaPreOX"


def test_sem_nome_fixo_a_transliteracao_vale(monkeypatch):
    """Sem entrada em NOMES_FIXOS, o ç vira 'c' como deve ser."""
    monkeypatch.setattr(upload, "NOMES_FIXOS", {})

    assert upload.nome_tabela("tabela-preço-x") == "TabelaPrecoX"


# ─────────────────────────────────────────────────────────────
# Conferencia pos-carga
# ─────────────────────────────────────────────────────────────
def test_conferencia_detecta_divergencia_e_reverte(ambiente, monkeypatch):
    """Se o banco tiver contagem diferente da enviada, a carga tem que reverter.

    Fecha o buraco do item 6 do checklist de operacao: antes o log dizia
    quantas linhas foram enviadas, e nada verificava quantas chegaram.
    """
    entrada, backup, conexao = ambiente
    arquivo = _criar_csv(entrada / "itens", "ok.csv", "sku;valor\nA1;10\nA2;20\n")

    # Banco responde 99 linhas para uma carga de 2 -> divergencia.
    conexao._cursor.contagem_forcada = 99
    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})

    falhas = upload.varrer()

    assert falhas == 1
    assert conexao.rollbacks >= 1
    assert arquivo.exists(), "arquivo fica na entrada para nova tentativa"
    assert not list(backup.rglob("ok.csv"))


def test_conferencia_aceita_contagem_igual(ambiente, monkeypatch):
    entrada, backup, conexao = ambiente
    _criar_csv(entrada / "itens", "ok.csv", "sku;valor\nA1;10\nA2;20\n")

    monkeypatch.setattr(upload, "ESTRATEGIAS", {"itens": {"estrategia": "replace"}})

    assert upload.varrer() == 0
    assert len(list(backup.rglob("ok.csv"))) == 1


def test_conferencia_nao_exige_igualdade_em_date_range(ambiente, monkeypatch):
    """date_range convive com o historico fora da janela: COUNT(*) e maior
    que o enviado por definicao, e isso NAO e erro."""
    entrada, _, conexao = ambiente
    _criar_csv(
        entrada / "faturamento",
        "nf.csv",
        "emissao;valor\n01/08/2026;10\n02/08/2026;20\n",
    )

    conexao._cursor.contagem_forcada = 500_000  # historico acumulado
    monkeypatch.setattr(
        upload,
        "ESTRATEGIAS",
        {
            "faturamento": {
                "estrategia": "date_range",
                "coluna_data": "emissao",
                "formato_data": "%d/%m/%Y",
            }
        },
    )

    assert upload.varrer() == 0, "date_range nao deve exigir contagem igual"


def test_backup_nao_sobrescreve_arquivo_do_mesmo_dia(ambiente):
    entrada, backup, _ = ambiente
    pasta = entrada / "itens"

    primeiro = _criar_csv(pasta, "igual.csv", "a;b\n1;2\n")
    upload.fazer_backup(primeiro)

    segundo = _criar_csv(pasta, "igual.csv", "a;b\n3;4\n")
    upload.fazer_backup(segundo)

    # O segundo ganha sufixo de hora em vez de sobrescrever o primeiro.
    assert len(list(backup.rglob("igual*.csv"))) == 2
