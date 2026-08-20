"""Aviso por e-mail quando um pipeline falha.

Existe porque a falha era invisivel: o log registrava, o exit code sinalizava,
e ninguem olhava. O comentario no topo do rodar_etl.ps1 ja dizia isso -- "a
falha passa dias sem ninguem perceber". Este modulo e o degrau que faltava.

**Regra de ouro, a mesma de execucoes.py: falhar aqui nunca derruba a carga.**
Aviso e observacao, nao parte do trabalho. SMTP fora do ar, senha vencida ou
rede caida logam um warning e seguem. Um ETL que carregou nao pode ser marcado
como quebrado porque o servidor de e-mail estava indisponivel.

Anti-spam: o sku_custo roda a cada 5 min. Sem janela de silencio, um erro
persistente geraria 12 e-mails por hora e viraria ruido -- e alerta que vira
ruido e alerta que ninguem le. Pelo mesmo motivo o sku_custo ja sai calado do
log quando nao ha mudanca (ver pipelines/sku_custo.py). Repeti a decisao aqui.
"""

import json
import logging
import os
import smtplib
import socket
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate

from config.settings import RAIZ

log = logging.getLogger(__name__)

# Estado em arquivo, e nao no banco, de proposito: a falha que mais importa
# avisar e justamente "nao conectei no MySQL". Um anti-spam que precisa do
# banco fica cego exatamente na hora em que ele e necessario.
ARQUIVO_ESTADO = RAIZ / ".alertas_enviados.json"

# 1 hora: no pior caso o sku_custo manda 1 e-mail/hora em vez de 12.
JANELA_SILENCIO_S = 3600

# Timeout curto. Um SMTP pendurado nao pode segurar o pipeline; se nao
# respondeu em 20s, desisto e sigo -- o log e a etl_execucoes ja registraram.
TIMEOUT_SMTP_S = 20

_LIMITE_CORPO = 8000


def _config() -> dict | None:
    """Le o SMTP do .env. Devolve None se nao estiver configurado.

    Ausencia de config nao e erro: quem nao quer alerta simplesmente nao
    preenche as variaveis, e o pipeline roda igual.
    """
    host = os.getenv("ALERTA_SMTP_HOST")
    para = os.getenv("ALERTA_PARA")
    if not host or not para:
        return None

    return {
        "host": host,
        "porta": int(os.getenv("ALERTA_SMTP_PORTA", "587")),
        "usuario": os.getenv("ALERTA_SMTP_USUARIO", ""),
        "senha": os.getenv("ALERTA_SMTP_SENHA", ""),
        "de": os.getenv("ALERTA_DE") or os.getenv("ALERTA_SMTP_USUARIO", ""),
        # Varios destinatarios separados por virgula.
        "para": [e.strip() for e in para.split(",") if e.strip()],
        "tls": os.getenv("ALERTA_SMTP_TLS", "1") not in ("0", "false", "False"),
    }


def _ler_estado() -> dict:
    try:
        return json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _salvar_estado(estado: dict) -> None:
    """Grava atomico: temp + os.replace.

    Mesmo motivo do salvar_estado de src/io/controle.py -- write direto trunca
    antes de escrever, e a maquina caindo no meio deixaria um JSON pela metade.
    Aqui o estrago seria menor (reenviar um e-mail), mas o padrao ja existe no
    projeto e nao custa nada seguir.
    """
    try:
        temporario = ARQUIVO_ESTADO.with_suffix(".json.tmp")
        temporario.write_text(
            json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporario, ARQUIVO_ESTADO)
    except OSError as erro:
        log.warning("  nao gravei o estado do alerta: %s", erro)


def _enviar(cfg: dict, assunto: str, corpo: str) -> bool:
    """Manda o e-mail. Devolve True se conseguiu. Nunca levanta."""
    try:
        msg = EmailMessage()
        msg["Subject"] = assunto
        msg["From"] = cfg["de"]
        msg["To"] = ", ".join(cfg["para"])
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(corpo[:_LIMITE_CORPO])

        with smtplib.SMTP(cfg["host"], cfg["porta"], timeout=TIMEOUT_SMTP_S) as smtp:
            if cfg["tls"]:
                smtp.starttls()
            if cfg["usuario"]:
                smtp.login(cfg["usuario"], cfg["senha"])
            smtp.send_message(msg)

        log.info("  alerta enviado para %s", ", ".join(cfg["para"]))
        return True
    except Exception as erro:
        # Engolir aqui e intencional -- ver a regra de ouro no topo do modulo.
        log.warning("  nao enviei o alerta (%s): %s", type(erro).__name__, erro)
        return False


def _corpo(pipeline: str, erros: str, contexto: dict | None) -> str:
    campos = {
        "Origem": pipeline,
        "Maquina": socket.gethostname(),
        "Quando": f"{datetime.now():%d/%m/%Y %H:%M:%S}",
        **(contexto or {}),
    }
    # Alinha pela maior chave em vez de largura fixa: "Ultimo volume" e
    # "Falha desde" estouravam as 10 colunas e saiam grudados no valor.
    largura = max(len(c) for c in campos) + 2
    linhas = [f"{c + ':':{largura}}{v}" for c, v in campos.items()]
    linhas += ["", "-" * 50, "", erros or "(sem detalhe)"]
    return "\n".join(linhas)


def falhou(
    pipeline: str,
    erros: str,
    contexto: dict | None = None,
    chave: str | None = None,
) -> bool:
    """Avisa que `pipeline` falhou, respeitando a janela de silencio.

    `chave` distingue erros diferentes do mesmo pipeline: por padrao usa a
    primeira linha do erro, entao uma falha NOVA fura o silencio de uma antiga
    em vez de ficar escondida atras dela.

    Devolve True se mandou. Nunca levanta -- quem chama esta no meio de um
    caminho de erro e nao pode ganhar uma segunda excecao por cima da primeira.
    """
    try:
        cfg = _config()
        if not cfg:
            log.debug("alerta nao configurado (falta ALERTA_SMTP_HOST/ALERTA_PARA)")
            return False

        primeira_linha = (erros or "").strip().splitlines()
        chave = chave or (primeira_linha[0][:200] if primeira_linha else "erro")
        id_alerta = f"{pipeline}::{chave}"
        agora = time.time()

        estado = _ler_estado()
        anterior = estado.get(id_alerta, {})
        ultimo = anterior.get("ultimo_envio", 0)

        if agora - ultimo < JANELA_SILENCIO_S:
            repetido = anterior.get("repeticoes", 0) + 1
            estado[id_alerta] = {**anterior, "repeticoes": repetido}
            _salvar_estado(estado)
            log.info(
                "  alerta suprimido (mesmo erro ha %d min, %dx)",
                int((agora - ultimo) // 60),
                repetido,
            )
            return False

        suprimidos = anterior.get("repeticoes", 0)
        corpo = _corpo(pipeline, erros, contexto)
        if suprimidos:
            corpo += (
                f"\n\n(este erro se repetiu {suprimidos}x desde o ultimo aviso, "
                f"suprimido pela janela de {JANELA_SILENCIO_S // 60} min)"
            )

        enviado = _enviar(cfg, f"[ETL FALHOU] {pipeline}", corpo)

        # Marco como avisado mesmo se o envio falhou: senao, com o SMTP fora,
        # toda rodada tentaria de novo e o pipeline pagaria o timeout de 20s a
        # cada 5 min. O log ja registrou a falha do envio.
        estado[id_alerta] = {
            "ultimo_envio": agora,
            "repeticoes": 0,
            "em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "enviado": enviado,
        }
        _salvar_estado(estado)
        return enviado
    except Exception as erro:
        log.warning("  erro no proprio alertador: %s", erro)
        return False


def _desde_quando(registro: dict) -> str:
    """'parou as 14:35 de hoje (ha 2h10)' a partir do ultimo sucesso."""
    ultimo = registro.get("ultimo_ok")
    if not ultimo:
        return "nunca carregou com sucesso nesta maquina"

    try:
        quando = datetime.strptime(ultimo, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return f"ultimo sucesso registrado: {ultimo}"

    delta = datetime.now() - quando
    horas, resto = divmod(int(delta.total_seconds()), 3600)
    if horas >= 24:
        idade = f"ha {horas // 24}d {horas % 24}h"
    elif horas:
        idade = f"ha {horas}h{resto // 60:02d}"
    else:
        idade = f"ha {resto // 60} min"

    return f"ultima carga OK em {quando:%d/%m %H:%M} ({idade})"


def base_ok(base: str, linhas: int | None = None) -> None:
    """Registra que `base` carregou. Nao manda e-mail, so anota o horario.

    E daqui que sai o "desde quando" do alerta: sem esse carimbo o e-mail
    conseguiria dizer que a base quebrou, mas nao ha quanto tempo ela esta
    parada — que e o que decide se voce corre ou se espera a proxima rodada.
    """
    try:
        estado = _ler_estado()
        chave = f"base::{base}"
        registro = estado.get(chave, {})
        estava_falhando = registro.pop("falhando_desde", None)

        registro["ultimo_ok"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if linhas is not None:
            registro["linhas"] = linhas
        estado[chave] = registro

        # Some tambem o registro de alerta, senao a proxima falha desta base
        # cairia na janela de silencio da anterior e ficaria muda. Busco pelo
        # sufixo porque o prefixo carrega o nome do pipeline, que varia.
        sufixo = f"::base::{base}"
        for k in [k for k in estado if k.endswith(sufixo)]:
            estado.pop(k, None)
        _salvar_estado(estado)

        if estava_falhando:
            cfg = _config()
            if cfg:
                volume = f"{linhas:,} linhas" if linhas is not None else "sem contagem"
                _enviar(
                    cfg,
                    f"[ETL OK] base {base} voltou",
                    _corpo(
                        f"base {base}",
                        f"A base voltou a carregar.\n\n"
                        f"Estava falhando desde {estava_falhando}.",
                        {"Base": base, "Volume agora": volume},
                    ),
                )
    except Exception as erro:
        log.debug("nao registrei o ok de %s: %s", base, erro)


def base_falhou(base: str, erro: str, pipeline: str = "") -> bool:
    """Avisa que uma base especifica parou, dizendo desde quando.

    Uma chamada por base: se tres tabelas quebram na mesma rodada, saem tres
    e-mails com assunto distinto. Cada um respeita a janela de silencio por
    conta propria, entao a base que voltar a funcionar para de avisar sem
    silenciar as outras.
    """
    try:
        estado = _ler_estado()
        chave = f"base::{base}"
        registro = estado.get(chave, {})

        # Primeira falha da sequencia: marca o inicio. Se ja estava falhando,
        # preserva o marco original — e ele que responde "parou quando".
        if not registro.get("falhando_desde"):
            registro["falhando_desde"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            estado[chave] = registro
            _salvar_estado(estado)

        contexto = {
            "Base": base,
            "Parou desde": registro.get("falhando_desde", "agora"),
            "Situacao": _desde_quando(registro),
        }
        if registro.get("linhas") is not None:
            contexto["Ultimo volume"] = f"{registro['linhas']:,} linhas"

        return falhou(
            f"{pipeline} / {base}" if pipeline else base,
            erro,
            contexto=contexto,
            # Chave pela base, nao pela mensagem: o driver costuma variar o
            # texto do erro a cada tentativa e isso furaria a janela toda vez.
            chave=f"base::{base}",
        )
    except Exception as e:
        log.warning("  erro ao avisar falha da base %s: %s", base, e)
        return False


def normalizou(pipeline: str, contexto: dict | None = None) -> bool:
    """Avisa que `pipeline` voltou ao normal, se havia falha pendente.

    Fecha o ciclo: sem isso voce so descobre que normalizou porque parou de
    receber e-mail, o que e indistinguivel do alertador ter quebrado.

    Sai calado quando nao havia falha registrada -- que e o caso na esmagadora
    maioria das rodadas.
    """
    try:
        cfg = _config()
        if not cfg:
            return False

        estado = _ler_estado()
        pendentes = [k for k in estado if k.startswith(f"{pipeline}::")]
        if not pendentes:
            return False

        detalhe = "\n".join(
            f"  - {k.split('::', 1)[1]} (ultimo aviso: {estado[k].get('em', '?')})"
            for k in pendentes
        )
        corpo = _corpo(
            pipeline,
            f"O pipeline voltou a rodar sem erro.\n\nEstava falhando com:\n{detalhe}",
            contexto,
        )
        enviado = _enviar(cfg, f"[ETL OK] {pipeline} normalizado", corpo)

        for k in pendentes:
            estado.pop(k, None)
        _salvar_estado(estado)
        return enviado
    except Exception as erro:
        log.warning("  erro ao avisar normalizacao: %s", erro)
        return False
