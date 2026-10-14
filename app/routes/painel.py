"""Página inicial: resumo por entidade."""
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..backup import ultimo_backup
from ..deps import entidades_visiveis, get_db, render, utilizador_atual
from ..models import TIPOS_CONTA, Conta, Utilizador

router = APIRouter()


@router.get("/")
def painel(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    blocos = []
    for entidade, papel in entidades_visiveis(db, u):
        contas = db.scalars(
            select(Conta).where(Conta.entidade_id == entidade.id, Conta.ativa)
            .options(selectinload(Conta.instituicao)).order_by(Conta.nome)
        ).all()
        por_tipo = defaultdict(list)
        for c in contas:
            por_tipo[c.tipo].append(c)
        total_eur = sum(c.saldo_inicial for c in contas if c.moeda == "EUR")
        blocos.append({
            "entidade": entidade, "papel": papel, "n_contas": len(contas), "total_eur": total_eur,
            "grupos": [(TIPOS_CONTA[t], por_tipo[t]) for t in TIPOS_CONTA if por_tipo[t]],
        })
    backup = ultimo_backup(request.app.state.defs) if u.is_admin else None
    return render(request, "painel.html", blocos=blocos, ultimo_backup=backup)
