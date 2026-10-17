"""Página de entrada: o património (ativos, passivos, líquido) e a agenda das próximas tarefas.

Ativos = saldos das contas que não são dívida + valor das casas, propriedades e carros (não as viaturas da
empresa); passivos = cartões de crédito e empréstimos (a dívida dos bens já está nas contas de empréstimo).
O valor de um bem numa data é a última avaliação até essa data (ou o valor de compra).

A agenda junta o que a app já sabe que vem aí: débitos das subscrições e fim das fidelizações, receitas previstas,
pagamento dos cartões, prestações dos empréstimos, manutenção e km dos carros, revisão do valor dos bens,
tiragem da cortiça, tarefas de importação do mês e abastecimentos por validar.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import relatorios as rel
from .models import TIPOS_BEM, TIPOS_CONTA, AbastecimentoPendente, Bem, Conta, Entidade, Subscricao

HORIZONTE = 60          # dias da agenda


# ------------------------------------------------------------------ património
def valor_bem_em(b: Bem, d: date) -> int:
    if b.empresa or (b.data_venda and b.data_venda <= d):
        return 0
    if b.data_aquisicao and b.data_aquisicao > d:
        return 0
    avals = [a for a in b.avaliacoes if a.data <= d]
    if avals:
        return avals[-1].valor
    if b.valor_aquisicao:
        return b.valor_aquisicao
    return b.avaliacoes[0].valor if b.avaliacoes else 0   # sem data de compra: o 1.º valor conhecido


@dataclass
class Visao:
    datas: list
    ativos: list            # por data (cêntimos)
    passivos: list
    liquido: list
    composicao: list        # [(rótulo, valor)] dos ativos hoje
    composicao_passivos: list
    bens: int               # valor dos bens hoje

    @property
    def hoje(self) -> dict:
        return {"ativos": self.ativos[-1], "passivos": self.passivos[-1], "liquido": self.liquido[-1]}

    @property
    def variacao_ano(self) -> int:
        return self.liquido[-1] - self.liquido[0]


def visao(db: Session, ent: Entidade, hoje: date) -> Visao:
    datas = [rel.fim_do_mes(rel.somar_meses(hoje.replace(day=1), -i)) for i in range(12, 0, -1)] + [hoje]
    corte = rel.inicio_analise(db, ent.id)
    if corte:
        datas = [d for d in datas if d >= corte] or [hoje]
    pat = rel.patrimonio(db, ent.id, datas)
    bens = db.scalars(select(Bem).where(Bem.entidade_id == ent.id)).all()
    v_bens = [sum(valor_bem_em(b, d) for b in bens) for d in datas]
    ativos = [x["ativos"] + vb for x, vb in zip(pat["serie"], v_bens)]
    passivos = [x["passivos"] for x in pat["serie"]]
    comp, comp_p = defaultdict(int), defaultdict(int)
    for c, s in pat["contas"]:
        if c.tipo in rel.PASSIVOS:
            if s < 0:
                comp_p[TIPOS_CONTA.get(c.tipo, c.tipo)] += -s
        elif s > 0:
            comp[TIPOS_CONTA.get(c.tipo, c.tipo)] += s
    plurais = {"casa": "Casas", "carro": "Carros", "rustico": "Propriedades rústicas"}
    for b in bens:
        v = valor_bem_em(b, hoje)
        if v:
            comp[plurais.get(b.tipo, TIPOS_BEM.get(b.tipo, b.tipo))] += v
    ordenar = lambda dd: sorted(dd.items(), key=lambda x: -x[1])
    return Visao(datas, ativos, passivos, [a - p for a, p in zip(ativos, passivos)], ordenar(comp), ordenar(comp_p),
                 v_bens[-1])


# ------------------------------------------------------------------ detalhe (cartões da página)
GRUPOS = [  # chave, título, cor do gráfico
    ("bancarios", "Ativos bancários", "#2a78d6"),
    ("investimentos", "Investimentos", "#1baf7a"),
    ("imoveis", "Património imóvel", "#eda100"),
    ("carros", "Carros", "#4a3aa7"),
]
BANCARIOS = {"ORDEM": ("Contas à ordem", "banco"), "POUPANCA": ("Poupança e depósitos", "cofre"),
             "CARTEIRA_DIGITAL": ("Carteira digital", "carteira"), "CARTAO_REFEICAO": ("Cartão refeição", "refeicao")}
INVESTIMENTOS = {"INVESTIMENTO": "investimento", "PENSOES": "pensao", "AFORRO": "aforro"}


@dataclass
class Tile:
    rotulo: str
    valor: int
    icone: str
    href: str | None = None
    nota: str = ""


@dataclass
class Grupo:
    chave: str
    titulo: str
    cor: str
    itens: list

    @property
    def total(self) -> int:
        return sum(t.valor for t in self.itens)


def detalhe(db: Session, ent: Entidade, hoje: date) -> tuple[list, Grupo]:
    """Grupos de ativos (com os seus cartões) e o grupo dos passivos, a valores de hoje."""
    contas = rel.patrimonio(db, ent.id, [hoje])["contas"]
    g = {k: Grupo(k, t, c, []) for k, t, c in GRUPOS}
    passivos = Grupo("passivos", "Passivos", "#e34948", [])
    banc = defaultdict(lambda: [0, 0])
    for c, saldo in contas:
        if c.tipo in rel.PASSIVOS:
            if saldo < 0:
                icone = "credito_casa" if c.tipo == "EMPRESTIMO" else "cartao"
                passivos.itens.append(Tile(c.nome, -saldo, icone, f"/contas/{c.id}/movimentos",
                                           "crédito à habitação" if c.tipo == "EMPRESTIMO" else "cartão de crédito"))
        elif c.tipo in INVESTIMENTOS:
            if saldo:
                g["investimentos"].itens.append(Tile(c.nome, saldo, INVESTIMENTOS[c.tipo], f"/contas/{c.id}/movimentos",
                                                     TIPOS_CONTA.get(c.tipo, "")))
        elif saldo > 0:
            banc[c.tipo][0] += saldo
            banc[c.tipo][1] += 1
    for tipo, (rotulo, icone) in BANCARIOS.items():
        if banc[tipo][0]:
            n = banc[tipo][1]
            g["bancarios"].itens.append(Tile(rotulo, banc[tipo][0], icone, f"/contas?entidade={ent.id}",
                                             f"{n} conta{'s' if n != 1 else ''}"))
    for b in db.scalars(select(Bem).where(Bem.entidade_id == ent.id).order_by(Bem.tipo, Bem.nome)):
        v = valor_bem_em(b, hoje)
        if not v:
            continue
        if b.tipo == "carro":
            g["carros"].itens.append(Tile(b.nome, v, "carro", f"/patrimonio/{b.id}", " ".join(x for x in (b.marca, b.modelo) if x)))
        else:
            icone = "rustico" if b.tipo == "rustico" else ("ferias" if b.uso == "ferias" else "casa")
            g["imoveis"].itens.append(Tile(b.nome, v, icone, f"/patrimonio/{b.id}", b.uso_rotulo))
    for x in g.values():
        x.itens.sort(key=lambda t: -t.valor)
    passivos.itens.sort(key=lambda t: -t.valor)
    return [x for x in g.values() if x.itens], passivos


def variacao_mensal(v: "Visao") -> tuple[int, float | None]:
    """Variação do património líquido face ao fim do mês anterior (€ e %)."""
    if len(v.liquido) < 2:
        return 0, None
    antes, agora = v.liquido[-2], v.liquido[-1]
    return agora - antes, ((agora - antes) / abs(antes) * 100) if antes else None


def graficos(v: Visao, hoje: date, grupos: list | None = None) -> dict:
    from .models import CORES
    return {
        "inicio_evolucao": {"rotulos": [d.strftime("%d/%m/%Y") if d == hoje else rel.rotulo_mes(d) for d in v.datas],
                            "ativos": [x / 100 for x in v.ativos], "passivos": [x / 100 for x in v.passivos],
                            "liquido": [x / 100 for x in v.liquido]},
        "inicio_composicao": ([{"nome": gr.titulo, "valor": gr.total / 100, "cor": gr.cor} for gr in grupos]
                              if grupos is not None else
                              [{"nome": n, "valor": x / 100, "cor": CORES[i % len(CORES)]} for i, (n, x) in enumerate(v.composicao)]),
        "inicio_linha": [x / 100 for x in v.liquido],
    }


# ------------------------------------------------------------------ agenda
@dataclass
class Item:
    data: date
    titulo: str
    detalhe: str = ""
    valor: int | None = None        # positivo = a receber, negativo = a pagar
    href: str | None = None
    tipo: str = "outro"             # pagamento | receita | carro | bem | importar | validar
    atrasado: bool = False
    entidade: str = ""


def _subscricoes(db: Session, ent: Entidade, hoje: date, ate: date) -> list[Item]:
    from . import subscricoes as sb
    out = []
    for s in db.scalars(select(Subscricao).where(Subscricao.entidade_id == ent.id)):
        if s.data_fim and s.data_fim < hoje:
            continue
        p = sb.proxima_data(s, hoje)
        if p and p <= ate:
            out.append(Item(p, s.nome, "débito da subscrição", -s.valor, f"/subscricoes/{s.id}", "pagamento"))
        if s.fim_fidelizacao and hoje <= s.fim_fidelizacao <= ate:
            out.append(Item(s.fim_fidelizacao, f"Fim da fidelização · {s.nome}", "a partir daqui pode cancelar sem penalização",
                            None, f"/subscricoes/{s.id}", "bem"))
    return out


def _receitas(db: Session, ent: Entidade, hoje: date, ate: date) -> list[Item]:
    from . import previsoes
    out = []
    for mes in (hoje.replace(day=1), rel.somar_meses(hoje.replace(day=1), 1)):
        for e in previsoes.estado_mes(db, ent.id, mes):
            if e.pendente and e.data <= ate and (e.data >= hoje - timedelta(days=10) or mes.month == hoje.month):
                out.append(Item(e.data, e.prev.descricao, "receita prevista" + (" — ainda não entrou" if e.data < hoje else ""),
                                e.valor, f"/previsoes?entidade={ent.id}", "receita", atrasado=e.data < hoje))
    return out


def _contas(db: Session, ent: Entidade, hoje: date, ate: date) -> list[Item]:
    from . import emprestimos
    from .servicos import resumo_cartao
    out = []
    for c in db.scalars(select(Conta).where(Conta.entidade_id == ent.id, Conta.ativa)):
        if c.tipo == "CARTAO_CREDITO":
            r = resumo_cartao(db, c, hoje)
            if r.proximo_pagamento and hoje <= r.proximo_pagamento <= ate and r.divida > 0:
                out.append(Item(r.proximo_pagamento, f"Pagamento do cartão · {c.nome}", "débito na conta que o paga",
                                -r.divida, f"/contas/{c.id}/movimentos", "pagamento"))
        elif c.tipo == "EMPRESTIMO" and c.emprestimo:
            try:
                r = emprestimos.resumo(db, c, hoje)
            except Exception:      # empréstimo sem dados suficientes: não entra na agenda
                continue
            d = emprestimos._mes_seguinte(r.ultima_data, c.dia_pagamento) if r.ultima_data else None
            while d and d < hoje:
                d = emprestimos._mes_seguinte(d, c.dia_pagamento)
            if d and d <= ate and r.n_restantes:
                out.append(Item(d, f"Prestação · {c.nome}", f"faltam {r.n_restantes} prestações", -r.prestacao_estimada,
                                f"/contas/{c.id}/movimentos", "pagamento"))
    return out


def _bens(db: Session, ent: Entidade, hoje: date, ate: date) -> tuple[list[Item], list[Item]]:
    from . import manutencao as mt
    from . import patrimonio as pt
    agenda, depois = [], []
    for b in db.scalars(select(Bem).where(Bem.entidade_id == ent.id)):
        if b.vendido:
            continue
        if b.tipo == "carro" and ent.tem("viaturas"):
            r = mt.resumo(b, hoje)
            for e in r.atraso:
                agenda.append(Item(hoje, f"{e.item.item} · {b.nome}", "manutenção em atraso — marcar oficina", None,
                                   f"/patrimonio/{b.id}#manutencao", "carro", atrasado=True))
            for e in r.breve:
                if e.data_prevista:
                    (agenda if e.data_prevista <= ate else depois).append(
                        Item(e.data_prevista, f"{e.item.item} · {b.nome}", "manutenção prevista", None,
                             f"/patrimonio/{b.id}#manutencao", "carro"))
            if r.lembrar_km:
                agenda.append(Item(hoje, f"Registar os km · {b.nome}", "a última leitura tem mais de um mês", None,
                                   f"/patrimonio/{b.id}#km", "carro"))
        if pt.revisao_pendente(b, hoje):
            agenda.append(Item(hoje, f"Rever o valor · {b.nome}", "avaliação periódica em atraso", None,
                               f"/patrimonio/{b.id}#avaliacoes", "bem", atrasado=True))
        if b.tipo == "rustico" and b.tem_cortica and b.proxima_tiragem and b.proxima_tiragem <= hoje.year + 1:
            d = date(b.proxima_tiragem, 6, 1)
            item = Item(d, f"Tiragem da cortiça · {b.nome}", f"época de tiragem (maio a agosto de {b.proxima_tiragem})"
                        + (f" · última em {b.tiragens[-1]}" if b.tiragens else ""), None, f"/patrimonio/{b.id}", "bem",
                        atrasado=b.proxima_tiragem < hoje.year)
            (agenda if d <= ate else depois).append(item)
    return agenda, depois


def _tarefas_app(db: Session, ent: Entidade, prefs: dict, hoje: date, ate: date) -> list[Item]:
    from . import tarefas
    out = []
    ativas, dia_ini, dia_fim = tarefas.janela(prefs)
    if ativas:
        if tarefas.na_janela(prefs, hoje):
            n = sum(x.por_fazer for x in tarefas.do_ciclo(db, ent.id, tarefas.inicio_ciclo(prefs, hoje)))
            if n:
                out.append(Item(hoje, "Importar os extratos do mês", f"{n} conta{'s' if n != 1 else ''} por atualizar",
                                None, "/importar/tarefas", "importar"))
        else:
            d = hoje.replace(day=min(dia_ini, rel.fim_do_mes(hoje).day))
            if d < hoje:
                m = rel.somar_meses(hoje.replace(day=1), 1)
                d = m.replace(day=min(dia_ini, rel.fim_do_mes(m).day))
            if d <= ate:
                out.append(Item(d, "Importar os extratos do mês", f"do dia {dia_ini} ao dia {dia_fim}", None,
                                "/importar/tarefas", "importar"))
    n = db.scalar(select(func.count()).select_from(AbastecimentoPendente).join(Bem).where(Bem.entidade_id == ent.id)) or 0
    if n:
        out.append(Item(hoje, f"Validar {n} abastecimento{'s' if n != 1 else ''}", "registados na página sem login", None,
                        f"/patrimonio/abastecimentos?entidade={ent.id}", "validar"))
    return out


def agenda(db: Session, entidades: list[Entidade], prefs: dict, hoje: date, dias: int = HORIZONTE) -> tuple[list, list]:
    """(próximos `dias` dias — atrasados primeiro, mais à frente)."""
    ate = hoje + timedelta(days=dias)
    proximos, depois = [], []
    for ent in entidades:
        itens = _contas(db, ent, hoje, ate) + _receitas(db, ent, hoje, ate) + _tarefas_app(db, ent, prefs, hoje, ate)
        if ent.tem("subscricoes"):
            itens += _subscricoes(db, ent, hoje, ate)
        a, d = _bens(db, ent, hoje, ate)
        for i in itens + a + d:
            i.entidade = ent.nome if len(entidades) > 1 else ""
        proximos += itens + a
        depois += d
    # a tarefa de importação é a mesma em todas as entidades: uma só linha
    juntos, vistos = [], {}
    for i in proximos:
        if i.tipo == "importar":
            k = (i.data, i.titulo)
            if k in vistos:
                j = vistos[k]
                j.entidade = ", ".join(x for x in (j.entidade, i.entidade) if x)
                if i.detalhe != j.detalhe and "por atualizar" in i.detalhe:
                    j.detalhe = f"{j.detalhe}; {i.detalhe}"
                continue
            vistos[k] = i
        juntos.append(i)
    proximos = juntos
    proximos.sort(key=lambda i: (not i.atrasado, i.data, i.titulo))
    depois.sort(key=lambda i: (i.data, i.titulo))
    return proximos, depois
