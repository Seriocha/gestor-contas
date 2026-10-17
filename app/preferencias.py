"""Preferências da app (tabela `definicao`), com valores por omissão.

Ficam em memória em app.state.prefs (a app corre num só processo) e são
atualizadas sempre que se gravam em Administração › Preferências."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Definicao

OMISSAO = {
    "mostrar_iban_completo": "0",  # "1": mostra o NIB/IBAN inteiro; "0": PT50 •••• 1234
    # Lembrete das tarefas de importação (menu e painel): ligado e entre que dias do mês.
    "tarefas_ativas": "1",
    "tarefas_dia_ini": "25",
    "tarefas_dia_fim": "5",
    # Património: revisão automática (IA) do valor dos bens quando passa o prazo de cada um.
    "avaliacao_automatica": "1",
}


def carregar(db: Session) -> dict[str, str]:
    prefs = dict(OMISSAO)
    prefs.update({d.chave: d.valor for d in db.scalars(select(Definicao))})
    return prefs


def gravar(db: Session, chave: str, valor: str) -> None:
    if chave not in OMISSAO:
        raise KeyError(chave)
    d = db.get(Definicao, chave)
    if d:
        d.valor = valor
    else:
        db.add(Definicao(chave=chave, valor=valor))


def ligada(prefs: dict, chave: str) -> bool:
    return prefs.get(chave, OMISSAO.get(chave)) == "1"
