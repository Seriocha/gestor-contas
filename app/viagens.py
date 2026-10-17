"""Viagens: registo da viagem, sugestão dos movimentos que podem ser gastos dela e
um modelo de critérios que se afina com o uso.

Uma viagem associa os seus movimentos através de uma etiqueta própria («Viagem: …»),
por isso também aparecem nos filtros de movimentos e nos relatórios por etiqueta.

Sugestões — cada movimento da entidade entre (partida − dias_antes) e (regresso +
dias_depois), que não seja transferência nem excluído, ganha pontos:
  +3 descrição com um padrão de viagem do modelo (AIRBNB, RYANAIR, TAP, …)
  +2 padrão já confirmado em viagens anteriores (aprendido)
  +2 numa conta de viagem (tipos/contas do modelo) durante as datas da viagem
  +1 qualquer gasto durante as datas da viagem
  +1 categoria típica do modelo
  −2 despesa habitual (o mesmo padrão em 3 ou mais meses fora da viagem)
  −1 abaixo do valor mínimo do modelo
Antes/depois das datas só contam reservas (padrão do modelo ou aprendido); entradas
(reembolsos) também só com padrão. Fica de fora o que bate num padrão a excluir, o que
foi rejeitado nesta viagem e os padrões rejeitados 2+ vezes e nunca confirmados.
Sugere-se a partir de 2 pontos; vem marcado a partir de 3.

Aprender: confirmar um movimento soma +1 «positivo» ao seu padrão de descrição no
modelo; rejeitar soma +1 «negativo». O ecrã do modelo mostra e deixa corrigir.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import (Categoria, Conta, Etiqueta, ModeloPadrao, ModeloViagem, Movimento, Viagem,
                     ViagemRejeitado)
from .qualificacao import compilar
from .sugestoes import padrao_de

MODELO_OMISSAO = "Viagem (padrão)"
INCLUIR_OMISSAO = """AIRBNB
BOOKING
HOTEL
HOSTEL
EXPEDIA
TRIP.COM
EDREAMS
RYANAIR
EASYJET
TAP AIR
TAP PORTUGAL
VUELING
IBERIA
LUFTHANSA
AIR FRANCE
KLM
TRANSAVIA
WIZZ
HERTZ
AVIS
EUROPCAR
SIXT
AEROPORTO
AIRPORT
DUTY FREE"""
EXCLUIR_OMISSAO = """CONDOMINIO
SEGURO
EDP
MEO
VODAFONE
VENCIMENTO"""
SUGERIR, MARCAR = 2, 3


def _lista(texto: str) -> list[str]:
    return [l.strip().upper() for l in (texto or "").splitlines() if l.strip()]


def _ids(csv: str) -> set[int]:
    return {int(x) for x in (csv or "").split(",") if x.strip().isdigit()}


def modelo_omissao(db: Session) -> ModeloViagem:
    """O modelo «Viagem (padrão)»; se foi renomeado ou apagado, o 1.º que existir. Só se cria
    quando não há nenhum modelo."""
    m = (db.scalar(select(ModeloViagem).where(ModeloViagem.nome == MODELO_OMISSAO))
         or db.scalar(select(ModeloViagem).order_by(ModeloViagem.id).limit(1)))
    if not m:
        nomes = ("Restaurantes e cafés", "Transportes", "Lazer", "Viagens")
        cats = [c.id for c in db.scalars(select(Categoria).where(Categoria.nome.in_(nomes)))]
        m = ModeloViagem(nome=MODELO_OMISSAO, dias_antes=90, dias_depois=3,
                         tipos_conta="CARTEIRA_DIGITAL,CARTAO_CREDITO", contas="", incluir=INCLUIR_OMISSAO,
                         excluir=EXCLUIR_OMISSAO, categorias=",".join(map(str, cats)), valor_min=None)
        db.add(m)
        db.flush()
    return m


def etiqueta_da(db: Session, v: Viagem) -> Etiqueta:
    """A etiqueta que marca os movimentos desta viagem (criada na 1.ª vez)."""
    if v.etiqueta_id and db.get(Etiqueta, v.etiqueta_id):
        return db.get(Etiqueta, v.etiqueta_id)
    base = f"Viagem: {v.nome}"[:44]
    nome, n = base, 1
    while db.scalar(select(Etiqueta.id).where(Etiqueta.nome == nome)):
        n += 1
        nome = f"{base} ({n})"
    et = Etiqueta(nome=nome)
    db.add(et)
    db.flush()
    v.etiqueta_id = et.id
    return et


@dataclass
class Sugestao:
    mov: Movimento
    pontos: int
    razoes: list = field(default_factory=list)
    padrao: str = ""
    fase: str = "durante"   # antes | durante | depois

    @property
    def marcar(self) -> bool:
        return self.pontos >= MARCAR


def _compilados(linhas: list[str]):
    return [(l, compilar(l, "contem")) for l in linhas]


def candidatos(db: Session, v: Viagem) -> list[Sugestao]:
    modelo = v.modelo or modelo_omissao(db)
    ini, fim = v.data_ini - timedelta(days=modelo.dias_antes), v.data_fim + timedelta(days=modelo.dias_depois)
    contas = {c.id: c for c in db.scalars(select(Conta).where(Conta.entidade_id == v.entidade_id))}
    tipos, contas_viagem, cats = set(filter(None, modelo.tipos_conta.split(","))), _ids(modelo.contas), _ids(modelo.categorias)
    incluir, excluir = _compilados(_lista(modelo.incluir)), _compilados(_lista(modelo.excluir))
    aprendidos = {p.padrao: p for p in modelo.padroes}
    rejeitados = set(db.scalars(select(ViagemRejeitado.movimento_id).where(ViagemRejeitado.viagem_id == v.id)))
    ja = set()
    if v.etiqueta_id:
        ja = set(db.scalars(select(Movimento.id).where(Movimento.etiquetas.any(id=v.etiqueta_id))))
    # padrões habituais: aparecem em 3+ meses distintos no ano anterior, fora das datas da viagem
    meses = defaultdict(set)
    for d, desc, norm in db.execute(select(Movimento.data_mov, Movimento.descricao_original, Movimento.descricao_norm)
                                    .where(Movimento.conta_id.in_(contas), Movimento.data_mov >= ini - timedelta(days=365),
                                           Movimento.data_mov <= fim + timedelta(days=90),
                                           (Movimento.data_mov < v.data_ini) | (Movimento.data_mov > v.data_fim))):
        meses[padrao_de(norm or desc)].add((d.year, d.month))
    movs = db.scalars(select(Movimento).where(
        Movimento.conta_id.in_(contas), Movimento.data_mov.between(ini, fim), ~Movimento.transferencia,
        ~Movimento.excluido).options(selectinload(Movimento.conta), selectinload(Movimento.categoria))
        .order_by(Movimento.data_mov)).all()
    out = []
    for m in movs:
        if m.id in ja or m.id in rejeitados:
            continue
        texto = (m.descricao_norm or m.descricao_original or "").upper()
        if any(rx.search(texto) for _, rx in excluir):
            continue
        p = padrao_de(texto)
        apr = aprendidos.get(p)
        if apr and apr.negativos >= 2 and not apr.positivos:
            continue
        fase = "antes" if m.data_mov < v.data_ini else "depois" if m.data_mov > v.data_fim else "durante"
        pts, razoes = 0, []
        alvo = next((l for l, rx in incluir if rx.search(texto)), None)
        if alvo:
            pts += 3
            razoes.append(f"padrão de viagem «{alvo}»")
        if apr and apr.positivos > apr.negativos:
            pts += 2
            razoes.append(f"confirmado em viagens anteriores ({apr.positivos}×)")
        reserva = bool(alvo or (apr and apr.positivos > apr.negativos))
        if fase != "durante" or m.montante > 0:
            if not reserva:
                continue   # fora das datas (ou entradas) só contam reservas / reembolsos reconhecidos
        if fase == "durante":
            conta = contas[m.conta_id]
            if conta.tipo in tipos or conta.id in contas_viagem:
                pts += 2
                razoes.append(f"conta de viagem ({conta.nome})")
            pts += 1
            razoes.append("durante a viagem")
        if m.categoria_id and (m.categoria_id in cats or (m.categoria and m.categoria.pai_id in cats)):
            pts += 1
            razoes.append("categoria típica")
        if len(meses.get(p, ())) >= 3 and not reserva:
            pts -= 2
            razoes.append("despesa habitual")
        if modelo.valor_min and abs(m.montante) < modelo.valor_min:
            pts -= 1
            razoes.append("valor baixo")
        if pts >= SUGERIR:
            out.append(Sugestao(m, pts, razoes, p, fase))
    out.sort(key=lambda s: (s.mov.data_mov, -s.pontos))
    return out


def _aprender(db: Session, modelo: ModeloViagem, movs: list[Movimento], positivo: bool) -> None:
    existentes = {p.padrao: p for p in modelo.padroes}
    for padrao in {padrao_de(m.descricao_norm or m.descricao_original) for m in movs} - {""}:
        p = existentes.get(padrao)
        if not p:
            p = ModeloPadrao(modelo=modelo, padrao=padrao, positivos=0, negativos=0)
            db.add(p)
            existentes[padrao] = p
        if positivo:
            p.positivos += 1
        else:
            p.negativos += 1


def associar(db: Session, v: Viagem, movs: list[Movimento], aprender: bool = True) -> int:
    et = etiqueta_da(db, v)
    n = 0
    for m in movs:
        if et not in m.etiquetas:
            m.etiquetas.append(et)
            n += 1
    if aprender and movs:
        _aprender(db, v.modelo or modelo_omissao(db), movs, True)
    return n


def rejeitar(db: Session, v: Viagem, movs: list[Movimento], aprender: bool = True) -> int:
    ja = set(db.scalars(select(ViagemRejeitado.movimento_id).where(ViagemRejeitado.viagem_id == v.id)))
    for m in movs:
        if m.id not in ja:
            db.add(ViagemRejeitado(viagem_id=v.id, movimento_id=m.id))
    if aprender and movs:
        _aprender(db, v.modelo or modelo_omissao(db), movs, False)
    return len(movs)


def desassociar(db: Session, v: Viagem, movs: list[Movimento]) -> int:
    n = 0
    for m in movs:
        antes = len(m.etiquetas)
        m.etiquetas = [e for e in m.etiquetas if e.id != v.etiqueta_id]
        n += antes != len(m.etiquetas)
    return n


@dataclass
class Resumo:
    movimentos: list
    total: int                 # gasto líquido (saídas − reembolsos), positivo
    antes: int                 # parte gasta antes da partida (reservas)
    por_categoria: list        # [(nome, cor, valor)]
    por_conta: list            # [(nome, valor)]
    por_dia: float
    por_pessoa: float


def resumo(db: Session, v: Viagem) -> Resumo:
    movs = []
    if v.etiqueta_id:
        movs = db.scalars(select(Movimento).where(Movimento.etiquetas.any(id=v.etiqueta_id))
                          .options(selectinload(Movimento.conta),
                                   selectinload(Movimento.categoria).selectinload(Categoria.pai))
                          .order_by(Movimento.data_mov, Movimento.id)).all()
    total = -sum(m.montante for m in movs)
    antes = -sum(m.montante for m in movs if m.data_mov < v.data_ini)
    cats, cores, contas = Counter(), {}, Counter()
    for m in movs:
        raiz = m.categoria.raiz if m.categoria else None
        nome = raiz.nome if raiz else "Sem categoria"
        cats[nome] -= m.montante
        cores[nome] = raiz.cor_efetiva if raiz else "#b4b2a9"
        contas[m.conta.nome] -= m.montante
    return Resumo(movs, total, antes, [(n, cores[n], x) for n, x in cats.most_common()],
                  contas.most_common(), total / max(v.dias, 1), total / max(v.pessoas or 1, 1))


def estatisticas_modelo(db: Session, modelo: ModeloViagem) -> dict:
    """Para o ecrã do modelo: contas onde caíram os gastos confirmados das viagens que o usam."""
    viagens = db.scalars(select(Viagem).where(Viagem.modelo_id == modelo.id, Viagem.etiqueta_id.is_not(None))).all()
    por_conta, n = Counter(), 0
    for v in viagens:
        for m in db.scalars(select(Movimento).where(Movimento.etiquetas.any(id=v.etiqueta_id))
                            .options(selectinload(Movimento.conta))):
            por_conta[(m.conta.nome, m.conta.tipo_rotulo)] += 1
            n += 1
    return {"viagens": len(viagens), "movimentos": n, "por_conta": por_conta.most_common(10)}
