"""Qualificação de movimentos: motor de regras e alterações manuais.

Uma regra corresponde quando a descrição normalizada (maiúsculas) bate no
padrão e as condições opcionais (conta, sentido, intervalo de montante) se
verificam. Modo "contém": alternativas separadas por "|"; alternativas
curtas (até 4 caracteres, ex. "EDP", "MEO", "BP") só contam como palavra
inteira, para "MEO" não apanhar "ROMEO".
"""
import re
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Categoria, Etiqueta, Movimento, Regra


@lru_cache(maxsize=512)
def compilar(padrao: str, modo: str) -> re.Pattern:
    if modo == "regex":
        return re.compile(padrao, re.IGNORECASE)
    partes = []
    for alt in (a.strip().upper() for a in padrao.split("|")):
        if not alt:
            continue
        esc = re.escape(alt)
        if modo == "comeca":
            partes.append(f"^{esc}")
        elif len(alt) <= 4:
            partes.append(rf"(?<!\w){esc}(?!\w)")
        else:
            partes.append(rf"(?<!\w){esc}")
    return re.compile("|".join(partes) or r"(?!)")


def validar_padrao(padrao: str, modo: str) -> str | None:
    if not padrao.strip():
        return "O padrão é obrigatório."
    try:
        compilar(padrao, modo)
    except re.error as e:
        return f"Expressão regular inválida: {e}"
    return None


def corresponde(regra: Regra, m: Movimento) -> bool:
    if regra.conta_id and regra.conta_id != m.conta_id:
        return False
    if regra.sentido == "SAIDA" and m.montante >= 0:
        return False
    if regra.sentido == "ENTRADA" and m.montante <= 0:
        return False
    valor = abs(m.montante)
    if regra.montante_min is not None and valor < regra.montante_min:
        return False
    if regra.montante_max is not None and valor > regra.montante_max:
        return False
    texto = (m.descricao_norm or m.descricao_original or "").upper()
    return bool(compilar(regra.padrao, regra.modo).search(texto))


def regras_ativas(db: Session) -> list[Regra]:
    return db.scalars(select(Regra).where(Regra.ativa).order_by(Regra.prioridade, Regra.id)).all()


def aplicar_regra(regra: Regra, m: Movimento) -> None:
    if regra.categoria_id:
        m.categoria_id = regra.categoria_id
    if regra.marcar_transferencia:
        m.transferencia = True
    if regra.excluir:
        m.excluido = True
    if regra.etiqueta and regra.etiqueta not in m.etiquetas:
        m.etiquetas.append(regra.etiqueta)
    m.qualificado_por = "regra"
    m.regra_id = regra.id


def aplicar_regras(db: Session, movimentos, regras: list[Regra] | None = None) -> int:
    """Aplica a 1.ª regra que corresponda a cada movimento não qualificado
    manualmente. Devolve quantos movimentos foram qualificados."""
    regras = regras_ativas(db) if regras is None else regras
    n = 0
    for m in movimentos:
        if m.qualificado_por == "manual":
            continue
        regra = next((r for r in regras if corresponde(r, m)), None)
        if regra:
            aplicar_regra(regra, m)
            n += 1
    return n


def qualificar_manual(db: Session, m: Movimento, *, categoria_id=..., transferencia=..., excluido=...,
                      etiquetas_ids=..., notas=...) -> None:
    """Altera só os campos passados; marca o movimento como manual para que
    as regras não voltem a mexer-lhe."""
    if categoria_id is not ...:
        m.categoria_id = categoria_id if categoria_id and db.get(Categoria, categoria_id) else None
    if transferencia is not ...:
        m.transferencia = bool(transferencia)
    if excluido is not ...:
        m.excluido = bool(excluido)
    if etiquetas_ids is not ...:
        m.etiquetas = list(db.scalars(select(Etiqueta).where(Etiqueta.id.in_(etiquetas_ids)))) if etiquetas_ids else []
    if notas is not ...:
        m.notas = notas or None
    m.qualificado_por = "manual"
    m.regra_id = None


def repor(db: Session, movimentos, reaplicar_regras: bool = True) -> int:
    """Desfaz a qualificação (categoria, transferência, excluído) e a marca
    de "à mão"; etiquetas e notas mantêm-se. Com reaplicar_regras, as regras
    voltam a correr sobre estes movimentos. Devolve quantos ficaram
    qualificados pelas regras."""
    for m in movimentos:
        m.categoria_id = None
        m.transferencia = False
        m.excluido = False
        m.qualificado_por = None
        m.regra_id = None
    db.flush()
    return aplicar_regras(db, movimentos) if reaplicar_regras else 0


def arvore_categorias(db: Session) -> list[Categoria]:
    """Categorias de topo (com .filhas carregadas), despesas primeiro."""
    raizes = db.scalars(select(Categoria).where(Categoria.pai_id.is_(None))
                        .order_by(Categoria.natureza, Categoria.ordem, Categoria.nome)).all()
    return raizes
