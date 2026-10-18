"""Tudo aquilo a que um movimento está ligado — para o detalhe só de leitura (mapa de ligações).

Cada ligação é um nó: grupo (dá o ícone e a cor), rótulo do tipo, título, detalhe e o endereço da ficha.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (TIPOS_BEM, TIPOS_CONTA, Abastecimento, Bem, Importacao, ItemRecheio, Manutencao, Movimento, Regra,
                     Subscricao, ViaVerde, Viagem)

ICONES = {"conta": "🏦", "entidade": "👥", "categoria": "🏷️", "regra": "⚙️", "par": "↔️", "casa": "🏠", "carro": "🚗",
          "rustico": "🌳", "subscricao": "🔁", "etiqueta": "#", "viagem": "✈️", "viaverde": "🛣️", "abastecimento": "⛽",
          "manutencao": "🔧", "recheio": "📦", "origem": "📄"}


@dataclass
class No:
    grupo: str
    rotulo: str
    titulo: str
    detalhe: str = ""
    href: str | None = None
    valor: int | None = None          # cêntimos, quando o nó tem um valor próprio
    cor: str | None = None

    @property
    def icone(self) -> str:
        return ICONES.get(self.grupo, "•")


@dataclass
class Mapa:
    m: Movimento
    nos: list = field(default_factory=list)
    estado: list = field(default_factory=list)     # transferência, excluído, à mão…
    via_verde: list = field(default_factory=list)
    par: Movimento | None = None


def _eur(c: int | None) -> str:
    if c is None:
        return ""
    s = f"{abs(c) / 100:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return ("-" if c < 0 else "") + s + " €"


def _subscricao_pelo_padrao(db: Session, m: Movimento) -> Subscricao | None:
    """A subscrição cujo padrão do extrato bate com este débito (sem associação à mão)."""
    from .qualificacao import compilar
    if m.montante >= 0 or m.excluido:
        return None
    for s in db.scalars(select(Subscricao).where(Subscricao.entidade_id == m.conta.entidade_id,
                                                 Subscricao.padrao.is_not(None))):
        if s.conta_id and s.conta_id != m.conta_id:
            continue
        if m.data_mov < s.data_inicio - timedelta(days=10) or (s.data_fim and m.data_mov > s.data_fim + timedelta(days=10)):
            continue
        if compilar(s.padrao, "contem").search((m.descricao_norm or m.descricao_original or "").upper()):
            return s
    return None


def do_movimento(db: Session, m: Movimento, ve_entidade) -> Mapa:
    """ve_entidade(entidade_id) -> bool: se o utilizador vê essa entidade (o outro lado de uma transferência pode não)."""
    mapa = Mapa(m=m)
    nos = mapa.nos
    c = m.conta
    nos.append(No("conta", "Conta", c.nome, f"{TIPOS_CONTA.get(c.tipo, c.tipo)} · {c.instituicao.nome}",
                  f"/contas/{c.id}/movimentos#mov-{m.id}"))
    nos.append(No("entidade", "Entidade", c.entidade.nome, "", f"/inicio?entidade={c.entidade_id}"))

    if m.categoria:
        origem = {"manual": "escolhida à mão", "regra": "por uma regra"}.get(m.qualificado_por or "", "")
        nos.append(No("categoria", "Categoria", m.categoria.nome_completo, origem,
                      f"/movimentos?entidade={c.entidade_id}&categoria={m.categoria_id}", cor=m.categoria.cor_efetiva))
    if m.regra_id and (r := db.get(Regra, m.regra_id)):
        nos.append(No("regra", "Regra", f"«{r.padrao}»", "qualificou este movimento", f"/regras/{r.id}"))

    if m.par_id and (p := db.get(Movimento, m.par_id)) and ve_entidade(p.conta.entidade_id):
        mapa.par = p
        nos.append(No("par", "Outro lado da transferência", p.conta.nome,
                      f"{p.data_mov:%d/%m/%Y} · {p.descricao_original}", f"/movimentos/{p.id}/detalhe", valor=p.montante))

    if m.bem:
        b = m.bem
        nos.append(No(b.tipo, TIPOS_BEM.get(b.tipo, "Bem"), b.nome,
                      ("escolhido à mão" if m.bem_manual else "atribuído automaticamente")
                      + (f" · {b.matricula}" if getattr(b, "matricula", None) else ""), f"/patrimonio/{b.id}"))

    sub, como = (db.get(Subscricao, m.subscricao_id), "associada à mão") if m.subscricao_id else (None, "")
    if not sub:
        sub, como = _subscricao_pelo_padrao(db, m), "pelo texto do extrato"
    if sub:
        nos.append(No("subscricao", "Subscrição", sub.nome, como, f"/subscricoes/{sub.id}"))

    ids_et = {e.id for e in m.etiquetas}
    viagens = {v.etiqueta_id: v for v in db.scalars(select(Viagem).where(Viagem.etiqueta_id.in_(ids_et)))} if ids_et else {}
    for e in m.etiquetas:
        if e.id in viagens:
            v = viagens[e.id]
            nos.append(No("viagem", "Viagem", v.nome, f"{v.destino or ''} · {v.data_ini:%d/%m/%Y} a {v.data_fim:%d/%m/%Y}".strip(" ·"),
                          f"/viagens/{v.id}"))
        else:
            nos.append(No("etiqueta", "Etiqueta", e.nome, "", f"/movimentos?entidade={c.entidade_id}&etiqueta={e.id}"))

    mapa.via_verde = db.scalars(select(ViaVerde).where(ViaVerde.movimento_id == m.id)
                                .order_by(ViaVerde.data, ViaVerde.hora)).all()
    if mapa.via_verde:
        vv = mapa.via_verde
        matriculas = sorted({x.matricula_fmt for x in vv})
        nos.append(No("viaverde", "Via Verde", f"{len(vv)} passage{'m' if len(vv) == 1 else 'ns'}",
                      " · ".join(matriculas), "#via-verde", valor=-sum(x.valor for x in vv)))

    for a in db.scalars(select(Abastecimento).where(Abastecimento.movimento_id == m.id)):
        det = f"{a.data:%d/%m/%Y} · {('%.2f' % a.litros).replace('.', ',')} l"
        if a.posto:
            det += f" · {a.posto}"
        carro = db.get(Bem, a.bem_id)
        nos.append(No("abastecimento", "Abastecimento", carro.nome if carro else "Abastecimento",
                      det, f"/patrimonio/{a.bem_id}", valor=-(a.valor or 0) if a.valor else None))

    for o in db.scalars(select(Manutencao).where(Manutencao.movimento_id == m.id)):
        nos.append(No("manutencao", "Oficina", f"{o.data:%d/%m/%Y}" + (f" · {o.km:,} km".replace(",", " ") if o.km else ""),
                      ", ".join(o.lista_itens[:3]) or (o.nota or ""), f"/patrimonio/{o.bem_id}",
                      valor=-o.custo if o.custo else None))

    for i in db.scalars(select(ItemRecheio).where(ItemRecheio.movimento_id == m.id)):
        nos.append(No("recheio", "Recheio", i.nome, " ".join(x for x in (i.marca, i.modelo) if x),
                      f"/patrimonio/recheio/{i.id}"))

    h = m.hash_dedup or ""
    if h.startswith("manual:"):
        nos.append(No("origem", "Origem", "Inserido à mão"))
    elif h.startswith("retif:"):
        nos.append(No("origem", "Origem", "Retificação de saldo"))
    elif m.importacao_id and (imp := db.get(Importacao, m.importacao_id)):
        nos.append(No("origem", "Importado de", imp.ficheiro_nome, f"{imp.criado_em:%d/%m/%Y %H:%M} · {imp.n_lidos} movimentos lidos"))

    if m.transferencia:
        mapa.estado.append("transferência entre contas próprias — não conta como receita nem despesa")
    if m.excluido:
        mapa.estado.append("excluído — não conta nos relatórios")
    if not m.categoria and not m.transferencia and not m.excluido:
        mapa.estado.append("sem categoria")
    return mapa
