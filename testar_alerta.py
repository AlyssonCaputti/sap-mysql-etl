"""Testa o alerta por e-mail de ponta a ponta.

    python testar_alerta.py            # diagnostica e manda um e-mail real
    python testar_alerta.py --so-checar  # so diagnostica, nao envia

Existe porque o erro de SMTP costuma ser mudo: a senha esta errada, o tenant
bloqueou, a porta e outra -- e o pipeline so registra "nao enviei o alerta".
Aqui o erro aparece traduzido, com o que fazer em cada caso.
"""

import smtplib
import sys

from src.io import alerta

DIAGNOSTICO = {
    "5.7.139": (
        "O tenant do Microsoft 365 esta com SMTP AUTH desativado (o padrao "
        "desde set/2025).\n"
        "   -> Use o Gmail com app password, ou peca ao TI para habilitar "
        "SMTP AUTH numa conta de servico."
    ),
    "5.7.3": (
        "Autenticacao recusada.\n"
        "   -> Se for Office 365 ou Gmail, a senha normal nao serve: precisa "
        "de app password."
    ),
    "5.7.8": (
        "Usuario ou senha incorretos.\n"
        "   -> Confira ALERTA_SMTP_USUARIO e ALERTA_SMTP_SENHA. No Gmail a "
        "app password tem 16 caracteres, sem espacos."
    ),
    "5.5.1": (
        "O servidor nao aceitou o comando de autenticacao.\n"
        "   -> Provavelmente a porta esta errada. Tente 587."
    ),
}


def diagnosticar(erro: Exception) -> str:
    texto = str(erro)
    for codigo, dica in DIAGNOSTICO.items():
        if codigo in texto:
            return dica
    if isinstance(erro, smtplib.SMTPAuthenticationError):
        return "   -> Credencial recusada. Veja se precisa de app password."
    if isinstance(erro, (TimeoutError, OSError)):
        return (
            "   -> Nao consegui nem conectar. Confira o host e a porta, e se "
            "a rede/firewall libera a saida na 587."
        )
    return "   -> Erro nao catalogado; a mensagem acima e do proprio servidor."


def main() -> int:
    cfg = alerta._config()
    if not cfg:
        print("Alerta DESLIGADO: falta ALERTA_SMTP_HOST ou ALERTA_PARA no .env.")
        print("Descomente o bloco de alerta no fim do .env e preencha.")
        return 1

    print(f"host:          {cfg['host']}:{cfg['porta']}  (TLS: {cfg['tls']})")
    print(f"usuario:       {cfg['usuario'] or '(sem login)'}")
    print(f"senha:         {'*' * 8 if cfg['senha'] else '(vazia)'}")
    print(f"remetente:     {cfg['de']}")
    print(f"destinatarios: {', '.join(cfg['para'])}")
    print()

    try:
        with smtplib.SMTP(cfg["host"], cfg["porta"], timeout=alerta.TIMEOUT_SMTP_S) as s:
            if cfg["tls"]:
                s.starttls()
            if cfg["usuario"]:
                s.login(cfg["usuario"], cfg["senha"])
        print("OK: conectou e autenticou.")
    except Exception as erro:
        print(f"FALHOU: {type(erro).__name__}: {erro}\n")
        print(diagnosticar(erro))
        return 1

    if "--so-checar" in sys.argv:
        return 0

    enviado = alerta._enviar(
        cfg,
        "[ETL] teste de alerta",
        alerta._corpo(
            "teste manual",
            "Se voce recebeu este e-mail, o alerta esta funcionando.\n"
            "Nenhum pipeline falhou -- foi disparado por testar_alerta.py.",
            {"Origem do teste": "testar_alerta.py"},
        ),
    )
    if enviado:
        print(f"E-mail de teste enviado para {', '.join(cfg['para'])}.")
        print("Se nao chegar em 2 min, confira o spam.")
        return 0

    print("Conectou, mas o envio falhou. Veja o motivo no log acima.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
