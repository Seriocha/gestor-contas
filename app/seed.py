"""Dados iniciais: instituições e entidades por omissão (só se as tabelas
estiverem vazias — nunca sobrepõe o que o utilizador alterou)."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Entidade, Instituicao

INSTITUICOES = [
    ("Santander", "santander"),
    ("ActivoBank", "activobank"),
    ("Banco BiG", "big"),
    ("Certificados de Aforro (IGCP/CTT)", "igcp"),
    ("Revolut", "revolut"),
    ("Crédito Agrícola", "ca"),
]

ENTIDADES = [
    ("Família", "Contas da família — base dos totais e relatórios.", 1),
    ("Mãe", "Conta da Mãe, gerida à parte — nunca entra nos totais da Família.", 2),
]


def semear(db: Session) -> None:
    if not db.scalar(select(func.count()).select_from(Instituicao)):
        db.add_all(Instituicao(nome=n, parser=p) for n, p in INSTITUICOES)
    if not db.scalar(select(func.count()).select_from(Entidade)):
        db.add_all(Entidade(nome=n, descricao=d, ordem=o) for n, d, o in ENTIDADES)
    db.commit()
