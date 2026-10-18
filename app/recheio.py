"""Recheio das casas (para o seguro e as garantias).

As categorias marcadas como recheio (Administração › Recheio; a marca vale também para as subcategorias) fazem com
que cada compra (saída) classificada nelas — à mão, por regra, na importação ou na consolidação — passe a ser um item
do recheio: na casa do movimento ou, sem casa, na casa «por omissão» da entidade. A ficha do item começa com a data e
o valor da compra e a loja (do movimento) e completa-se à mão: tipo, marca, modelo, n.º de série, características,
garantia (por omissão 3 anos, a garantia legal) e contactos do fornecedor; os pedidos de garantia ficam no item.
"""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import GARANTIA_LEGAL_MESES, Bem, Categoria, Conta, ItemRecheio, Movimento

TIPO_POR_CATEGORIA = {"tecnologia": "informatica", "eletrodom": "eletrodomestico", "decora": "decoracao",
                      "mobili": "mobiliario", "ferrament": "ferramenta", "jardim": "ferramenta", "imagem": "eletronica"}


def categorias(db: Session) -> set[int]:
    """Ids das categorias de recheio (as marcadas e as filhas das marcadas). Em cache na sessão."""
    if "recheio_cats" not in db.info:
        marcadas = set(db.scalars(select(Categoria.id).where(Categoria.recheio)))
        filhas = set(db.scalars(select(Categoria.id).where(Categoria.pai_id.in_(marcadas)))) if marcadas else set()
        db.info["recheio_cats"] = marcadas | filhas
    return db.info["recheio_cats"]


def valor_minimo(db: Session) -> int:
    """Compras abaixo deste valor (cêntimos) não entram no recheio (consumíveis, pequenas compras)."""
    from .models import Definicao
    d = db.get(Definicao, "recheio_valor_minimo")
    return int(d.valor) * 100 if d and d.valor.isdigit() else 5000


def garantia_omissao(db: Session) -> int:
    from .models import Definicao
    d = db.get(Definicao, "recheio_garantia_meses")
    return int(d.valor) if d and d.valor.isdigit() else GARANTIA_LEGAL_MESES


def casa_para(db: Session, m: Movimento) -> Bem | None:
    """A casa do movimento; senão a casa «por omissão» da entidade; senão a única casa (ou nenhuma)."""
    if m.bem and m.bem.tipo == "casa":
        return m.bem
    ent_id = m.conta.entidade_id if m.conta else db.get(Conta, m.conta_id).entidade_id
    casas = [b for b in db.scalars(select(Bem).where(Bem.entidade_id == ent_id, Bem.tipo == "casa")) if not b.vendido]
    return next((b for b in casas if b.omissao), None) or (casas[0] if len(casas) == 1 else None)


def _tipo(cat: Categoria | None) -> str | None:
    nome = (cat.nome_completo if cat else "").lower()
    return next((t for k, t in TIPO_POR_CATEGORIA.items() if k in nome), None)


def registar(db: Session, m: Movimento) -> ItemRecheio | None:
    """Cria o item de recheio da compra, se a categoria for de recheio e ainda não houver item para este movimento."""
    if m.montante is None or m.montante >= 0 or m.transferencia or not m.categoria_id or m.categoria_id not in categorias(db):
        return None
    if -m.montante < valor_minimo(db):
        return None
    if m.id is None:
        db.flush()
    if db.scalar(select(ItemRecheio.id).where(ItemRecheio.movimento_id == m.id)):
        return None
    from .sugestoes import padrao_de
    casa = casa_para(db, m)
    ent_id = m.conta.entidade_id if m.conta else db.get(Conta, m.conta_id).entidade_id
    cat = m.categoria or db.get(Categoria, m.categoria_id)
    loja = padrao_de(m.descricao_norm or m.descricao_original or "") or None
    item = ItemRecheio(entidade_id=ent_id, bem_id=casa.id if casa else None, movimento_id=m.id,
                       nome=(f"Compra · {loja.title()}" if loja else (m.descricao_original or "Compra"))[:120],
                       tipo=_tipo(cat), data_compra=m.data_mov,
                       valor=-m.montante, garantia_meses=garantia_omissao(db),
                       loja=loja.title()[:120] if loja else None)
    db.add(item)
    db.flush()
    return item


def registar_varios(db: Session, movs) -> int:
    return sum(1 for m in movs if registar(db, m))


def preencher(db: Session, entidade_id: int) -> int:
    """Cria os itens das compras já classificadas em categorias de recheio (dentro da cobertura dos dados)."""
    cats = categorias(db)
    if not cats:
        return 0
    from .relatorios import inicio_analise
    corte = inicio_analise(db, entidade_id) or date.min
    movs = db.scalars(select(Movimento).join(Conta).where(
        Conta.entidade_id == entidade_id, Movimento.categoria_id.in_(cats), Movimento.montante < 0,
        ~Movimento.transferencia, Movimento.data_mov >= corte)).all()
    return registar_varios(db, movs)


@dataclass
class Resumo:
    itens: list = field(default_factory=list)
    total: int = 0                  # valor dos itens em uso ou avariados
    capital: int | None = None      # capital seguro do recheio
    a_terminar: list = field(default_factory=list)
    pedidos_abertos: list = field(default_factory=list)

    @property
    def subseguro(self) -> int | None:
        """Quanto falta ao capital seguro para cobrir o valor do recheio (None = coberto ou sem capital)."""
        if self.capital is None or self.total <= self.capital:
            return None
        return self.total - self.capital


def resumo_casa(db: Session, casa: Bem, hoje: date) -> Resumo:
    itens = db.scalars(select(ItemRecheio).where(ItemRecheio.bem_id == casa.id, ItemRecheio.estado != "ignorado")
                       .order_by(ItemRecheio.data_compra.desc())).all()
    r = Resumo(itens, sum(i.valor or 0 for i in itens if i.estado in ("ativo", "avariado")), casa.capital_recheio)
    r.a_terminar = [i for i in itens if i.estado == "ativo" and i.estado_garantia(hoje) == "termina em breve"]
    r.pedidos_abertos = [i for i in itens if i.pedido_aberto]
    return r


def sem_casa(db: Session, entidade_id: int) -> list[ItemRecheio]:
    return db.scalars(select(ItemRecheio).where(ItemRecheio.entidade_id == entidade_id, ItemRecheio.bem_id.is_(None),
                                                ItemRecheio.estado != "ignorado")).all()
