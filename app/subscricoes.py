"""Subscrições e assinaturas: catálogo dos serviços mais comuns, deteção nos
movimentos, ligação de cada subscrição aos seus débitos e análise de
fidelização (início, fim do período de fidelização, fim).

Ligação aos movimentos: os débitos da subscrição são as saídas cuja descrição
contém o «padrão» (texto do extrato), na conta indicada (ou em qualquer conta
da entidade), entre o início e o fim. O dia de pagamento e a periodicidade dão
as datas esperadas: o próximo pagamento, e se o último esperado já foi debitado
(«em dia»), ainda não chegou o extrato («aguarda extrato») ou falta («em atraso»).

Deteção: (1) catálogo — Netflix, Spotify, MEO, NOS, EDP, ginásios, seguros… — a
partir de 2 meses; (2) recorrências — o mesmo texto em 3+ meses, cerca de um
débito por período e valor estável (±30%). Sugestões já cobertas por uma
subscrição ou ignoradas não voltam a aparecer.
"""
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import PERIODICIDADES, Categoria, Conta, Movimento, Subscricao, SubscricaoIgnorada
from .qualificacao import compilar
from .relatorios import fim_do_mes, somar_meses
from .sugestoes import padrao_de

# (chave, nome, padrão no extrato, categoria sugerida, tipo)
CATALOGO = [
    ("netflix", "Netflix", "NETFLIX", "Subscrições", "Streaming"),
    ("spotify", "Spotify", "SPOTIFY", "Subscrições", "Streaming"),
    ("disney", "Disney+", "DISNEY", "Subscrições", "Streaming"),
    ("hbo", "HBO Max", "HBO|MAX.COM", "Subscrições", "Streaming"),
    ("prime", "Amazon Prime", "AMAZON PRIME|PRIME VIDEO|AMZN PRIME", "Subscrições", "Streaming"),
    ("apple", "Apple (iCloud, Apple One, TV+)", "APPLE.COM|APPLE COM BILL|ITUNES", "Subscrições", "Streaming"),
    ("youtube", "YouTube Premium", "YOUTUBE", "Subscrições", "Streaming"),
    ("sporttv", "Sport TV", "SPORT TV|SPORTTV", "Subscrições", "Streaming"),
    ("dazn", "DAZN", "DAZN", "Subscrições", "Streaming"),
    ("audible", "Audible / Kindle", "AUDIBLE|KINDLE", "Subscrições", "Streaming"),
    ("google", "Google One / Google", "GOOGLE ONE|GOOGLE STORAGE|GOOGLE *", "Subscrições", "Software"),
    ("microsoft", "Microsoft 365", "MICROSOFT|MSFT", "Subscrições", "Software"),
    ("adobe", "Adobe", "ADOBE", "Subscrições", "Software"),
    ("openai", "ChatGPT (OpenAI)", "OPENAI|CHATGPT", "Subscrições", "Software"),
    ("claude", "Claude (Anthropic)", "CLAUDE|ANTHROPIC", "Subscrições", "Software"),
    ("dropbox", "Dropbox", "DROPBOX", "Subscrições", "Software"),
    ("playstation", "PlayStation Plus", "PLAYSTATION|SONY INTERACTIVE", "Subscrições", "Jogos"),
    ("xbox", "Xbox Game Pass", "XBOX", "Subscrições", "Jogos"),
    ("duolingo", "Duolingo", "DUOLINGO", "Subscrições", "Software"),
    ("linkedin", "LinkedIn Premium", "LINKEDIN", "Subscrições", "Software"),
    ("meo", "MEO", "MEO|ALTICE", "Internet e telemóvel", "Telecomunicações"),
    ("nos", "NOS", "NOS COMUNICA|NOSPAY|NOS SGPS|NOS ", "Internet e telemóvel", "Telecomunicações"),
    ("vodafone", "Vodafone", "VODAFONE", "Internet e telemóvel", "Telecomunicações"),
    ("digi", "DIGI", "DIGI PORTUGAL|DIGI COMUN", "Internet e telemóvel", "Telecomunicações"),
    ("edp", "EDP", "EDP COMERCIAL|EDP SERVICO|EDP ", "Eletricidade", "Energia"),
    ("goldenergy", "Goldenergy", "GOLDENERGY|G ENERGY", "Eletricidade", "Energia"),
    ("iberdrola", "Iberdrola", "IBERDROLA", "Eletricidade", "Energia"),
    ("endesa", "Endesa", "ENDESA", "Eletricidade", "Energia"),
    ("galp", "Galp (energia)", "GALP ENERGIA|GALP POWER", "Gás", "Energia"),
    ("gas", "Gás (garrafa / canalizado)", "GASCAN|GAS NATURAL|LISBOAGAS|GOLDGAS", "Gás", "Energia"),
    ("agua", "Água (SMAS / EPAL)", "SMAS|EPAL|AGUAS DE|SERVICOS MUNICIP", "Água", "Energia"),
    ("ginasio", "Ginásio", "GINAS|FITNESS|HOLMES PLACE|SOLINCA|VIVAGYM|PHIVE|GO FIT|CYBERGYM", "Desporto e ginásio", "Saúde e bem-estar"),
    ("seguro", "Seguro", "FIDELIDADE|ALLIANZ|TRANQUILIDADE|AGEAS|GENERALI|LIBERTY SEGUROS|OK TELESEGUROS|MAPFRE|ZURICH",
     "Seguros", "Seguros"),
    ("viaverde", "Via Verde", "VIA VERDE", "Portagens e estacionamento", "Transportes"),
    ("navegante", "Passe Navegante", "NAVEGANTE|PASSE", "Transportes públicos", "Transportes"),
    ("uberone", "Uber One", "UBER ONE", "Subscrições", "Transportes"),
    ("glovo", "Glovo Prime", "GLOVO PRIME", "Subscrições", "Outros"),
    ("revolut", "Plano Revolut", "COMISSAO DE MANUTENCAO DE CONTA PACOTE|REVOLUT PREMIUM|REVOLUT METAL|REVOLUT PLUS",
     "Comissões bancárias", "Banco"),
    ("jornal", "Jornal / revista", "PUBLICO|EXPRESSO|OBSERVADOR|JORNAL", "Subscrições", "Imprensa"),
]
TIPOS = {c[0]: c[4] for c in CATALOGO}
EXCLUIR = ("COMISSÃO REVOLUT", "COMISSAO REVOLUT", "CONVERSÃO CAMBIAL", "CONVERSAO CAMBIAL", "LEVANTAMENTO")
TOLERANCIA_DIAS = 7


def _compilados():
    return [(c, compilar(c[2], "contem")) for c in CATALOGO]


def do_catalogo(texto: str):
    t = (texto or "").upper()
    return next((c for c, rx in _compilados() if rx.search(t)), None)


def categoria_por_nome(db: Session, nome: str | None) -> int | None:
    if not nome:
        return None
    return db.scalar(select(Categoria.id).where(Categoria.nome == nome).limit(1))


# ---------------------------------------------------------------- datas esperadas e estado

def datas_esperadas(s: Subscricao, ate: date) -> list[date]:
    """Datas em que se espera um débito, do início até `ate` (ou ao fim da subscrição)."""
    dia = s.dia_pagamento or s.data_inicio.day
    fim = min(ate, s.data_fim) if s.data_fim else ate
    out, n = [], 0
    while True:
        mes = somar_meses(s.data_inicio.replace(day=1), n * s.meses_periodo)
        d = mes.replace(day=min(dia, fim_do_mes(mes).day))
        if d > fim:
            return out
        if d >= s.data_inicio - timedelta(days=TOLERANCIA_DIAS):
            out.append(d)
        n += 1
        if n > 1200:
            return out


def proxima_data(s: Subscricao, hoje: date) -> date | None:
    if s.data_fim and s.data_fim < hoje:
        return None
    futuras = [d for d in datas_esperadas(s, hoje + timedelta(days=400)) if d >= hoje]
    return futuras[0] if futuras else None


def pagamentos(db: Session, s: Subscricao, hoje: date) -> list[Movimento]:
    """Débitos da subscrição no extrato (pelo padrão, na conta indicada ou em qualquer conta da entidade)."""
    if not s.padrao:
        return []
    contas = [s.conta_id] if s.conta_id else list(db.scalars(select(Conta.id).where(Conta.entidade_id == s.entidade_id)))
    ate = (s.data_fim or hoje) + timedelta(days=TOLERANCIA_DIAS)
    movs = db.scalars(select(Movimento).where(
        Movimento.conta_id.in_(contas), Movimento.montante < 0, ~Movimento.excluido,
        Movimento.data_mov.between(s.data_inicio - timedelta(days=TOLERANCIA_DIAS), ate))
        .options(selectinload(Movimento.conta)).order_by(Movimento.data_mov)).all()
    rx = compilar(s.padrao, "contem")
    return [m for m in movs if rx.search((m.descricao_norm or m.descricao_original or "").upper())]


@dataclass
class Estado:
    sub: Subscricao
    pagos: list
    proxima: date | None
    situacao: str                    # em dia | aguarda extrato | em atraso | terminada | sem padrão | ainda não começou
    ultimo: Movimento | None = None
    valor_mudou: int | None = None   # valor do último débito quando difere do previsto
    total_pago: int = 0
    meses: int = 0
    fidelizada_dias: int | None = None   # dias até ao fim da fidelização (se ainda fidelizada)
    falhas: list = field(default_factory=list)   # datas esperadas sem débito (até ao último extrato)

    @property
    def custo_anual(self) -> int:
        return round(self.sub.custo_mensal * 12)


def estado(db: Session, s: Subscricao, hoje: date) -> Estado:
    pagos = pagamentos(db, s, hoje)
    e = Estado(s, pagos, proxima_data(s, hoje), "em dia")
    e.total_pago = -sum(m.montante for m in pagos)
    fim = min(hoje, s.data_fim) if s.data_fim else hoje
    e.meses = max(0, (fim.year - s.data_inicio.year) * 12 + fim.month - s.data_inicio.month
                  - (1 if fim.day < s.data_inicio.day else 0))   # meses completos
    if s.fim_fidelizacao and s.fim_fidelizacao >= hoje:
        e.fidelizada_dias = (s.fim_fidelizacao - hoje).days
    if pagos:
        e.ultimo = pagos[-1]
        if abs(abs(e.ultimo.montante) - s.valor) > max(s.valor * 0.02, 1):
            e.valor_mudou = -e.ultimo.montante
    if s.data_fim and s.data_fim < hoje:
        e.situacao = "terminada"
        return e
    if s.data_inicio > hoje:
        e.situacao = "ainda não começou"
        return e
    if not s.padrao:
        e.situacao = "sem padrão"
        return e
    # até onde há extratos: a data do último movimento da conta (ou de qualquer conta da entidade)
    q = select(Movimento.data_mov).order_by(Movimento.data_mov.desc()).limit(1)
    q = q.where(Movimento.conta_id == s.conta_id) if s.conta_id else q.join(Conta).where(Conta.entidade_id == s.entidade_id)
    ultimo_extrato = db.scalar(q) or hoje
    datas_pagas = [m.data_mov for m in pagos]
    ultima_verificavel = None   # a última data esperada que os extratos já cobrem
    for d in datas_esperadas(s, hoje):
        pago = any(abs((p - d).days) <= TOLERANCIA_DIAS for p in datas_pagas)
        if pago or d + timedelta(days=TOLERANCIA_DIAS) <= ultimo_extrato:
            ultima_verificavel = (d, pago)
            if not pago:
                e.falhas.append(d)
        elif d <= hoje and not pago:
            e.situacao = "aguarda extrato"
    # «em atraso» só quando falta o último pagamento que os extratos já deviam mostrar;
    # falhas mais antigas ficam no aviso da ficha
    if ultima_verificavel and not ultima_verificavel[1]:
        e.situacao = "em atraso"
    return e


# ---------------------------------------------------------------- deteção

@dataclass
class Sugestao:
    nome: str
    padrao: str
    valor: int
    periodicidade: str
    dia: int
    inicio: date
    ultima: date
    conta_id: int | None
    conta_nome: str
    categoria_id: int | None
    n: int
    fonte: str          # catálogo | recorrente
    servico: str | None
    ativa: bool


def _periodicidade(datas: list[date]) -> str:
    if len(datas) < 2:
        return "mensal"
    gaps = [(b - a).days for a, b in zip(datas, datas[1:])]
    g = statistics.median(gaps)
    return "mensal" if g < 45 else "bimestral" if g < 75 else "trimestral" if g < 135 else "semestral" if g < 270 else "anual"


def detetar(db: Session, entidade_id: int, hoje: date, meses: int = 18) -> list[Sugestao]:
    contas = {c.id: c for c in db.scalars(select(Conta).where(Conta.entidade_id == entidade_id))}
    validas = [cid for cid, c in contas.items() if c.tipo not in ("EMPRESTIMO", "INVESTIMENTO", "AFORRO")]
    if not validas:
        return []
    desde = somar_meses(hoje.replace(day=1), -meses)
    movs = db.scalars(select(Movimento).where(
        Movimento.conta_id.in_(validas), Movimento.montante < 0, ~Movimento.transferencia, ~Movimento.excluido,
        Movimento.data_mov >= desde).order_by(Movimento.data_mov)).all()
    existentes = db.scalars(select(Subscricao).where(Subscricao.entidade_id == entidade_id)).all()
    cobertos = [compilar(s.padrao, "contem") for s in existentes if s.padrao]
    servicos_existentes = {s.servico for s in existentes if s.servico}
    ignorados = set(db.scalars(select(SubscricaoIgnorada.padrao).where(SubscricaoIgnorada.entidade_id == entidade_id)))
    grupos = defaultdict(list)
    for m in movs:
        texto = (m.descricao_norm or m.descricao_original or "").upper()
        if texto.startswith(EXCLUIR) or any(rx.search(texto) for rx in cobertos):
            continue
        cat = do_catalogo(texto)
        chave = ("cat", cat[0]) if cat else ("rec", padrao_de(texto))
        if chave[1]:
            grupos[chave].append(m)
    out = []
    for (fonte, chave), ms in grupos.items():
        cat = next((c for c in CATALOGO if c[0] == chave), None) if fonte == "cat" else None
        padrao = cat[2] if cat else chave
        if padrao in ignorados or (cat and cat[0] in servicos_existentes):
            continue
        por_mes = defaultdict(list)
        for m in ms:
            por_mes[(m.data_mov.year, m.data_mov.month)].append(m)
        n_meses = len(por_mes)
        primeiros = [sorted(v, key=lambda m: m.data_mov)[0] for _, v in sorted(por_mes.items())]
        valores = [abs(m.montante) for m in primeiros]
        if cat:
            if n_meses < 2:
                continue
        else:
            if n_meses < 3 or len(ms) / n_meses > 1.5 or max(valores) > min(valores) * 1.3:
                continue
        datas = [m.data_mov for m in primeiros]
        per = _periodicidade(datas)
        ultimo = primeiros[-1]
        conta_id = Counter(m.conta_id for m in ms).most_common(1)[0][0]
        cat_id = categoria_por_nome(db, cat[3]) if cat else None
        if not cat_id:
            cats = Counter(m.categoria_id for m in ms if m.categoria_id)
            cat_id = cats.most_common(1)[0][0] if cats else None
        meses_per = PERIODICIDADES[per][1]
        out.append(Sugestao(
            nome=cat[1] if cat else chave.title(), padrao=padrao, valor=abs(ultimo.montante), periodicidade=per,
            dia=Counter(d.day for d in datas).most_common(1)[0][0], inicio=datas[0], ultima=datas[-1],
            conta_id=conta_id, conta_nome=contas[conta_id].nome, categoria_id=cat_id, n=len(primeiros),
            fonte="catálogo" if cat else "recorrente", servico=cat[0] if cat else None,
            ativa=(hoje - datas[-1]).days <= 45 * meses_per))
    out.sort(key=lambda s: (not s.ativa, s.fonte != "catálogo", -s.valor))
    return out


@dataclass
class Resumo:
    ativas: list
    terminadas: list
    custo_mensal: int
    custo_anual: int
    proximos_30: int
    fidelizadas: int


def resumo(db: Session, entidade_id: int, hoje: date) -> Resumo:
    subs = db.scalars(select(Subscricao).where(Subscricao.entidade_id == entidade_id)
                      .options(selectinload(Subscricao.conta), selectinload(Subscricao.categoria))
                      .order_by(Subscricao.nome)).all()
    estados = [estado(db, s, hoje) for s in subs]
    ativas = [e for e in estados if e.situacao != "terminada"]
    ativas.sort(key=lambda e: (e.proxima or date.max, e.sub.nome.lower()))
    terminadas = sorted((e for e in estados if e.situacao == "terminada"), key=lambda e: e.sub.data_fim, reverse=True)
    em_curso = [e for e in ativas if e.situacao != "ainda não começou"]
    return Resumo(
        ativas=ativas, terminadas=terminadas,
        custo_mensal=round(sum(e.sub.custo_mensal for e in em_curso)),
        custo_anual=round(sum(e.sub.custo_mensal for e in em_curso) * 12),
        proximos_30=sum(e.sub.valor for e in ativas if e.proxima and e.proxima <= hoje + timedelta(days=30)),
        fidelizadas=sum(1 for e in ativas if e.fidelizada_dias is not None))
