"""Vista para a televisão (/tv): emparelhamento por código e os números das páginas, só de consulta.

Emparelhar: a TV pede um código de 6 dígitos (válido 10 minutos) e fica com o segredo do pedido num cookie; alguém
com sessão iniciada aprova o código no telemóvel ou no PC (/tv/aprovar) e escolhe as entidades que a TV mostra.
Na próxima consulta do estado, a TV troca o segredo por um token próprio (cookie «gestor_tv», 1 ano) — na base fica
só o hash. Os pedidos vivem em memória: a app corre num só processo e um pedido perdido num reinício pede-se outra vez.
"""
import hashlib
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from . import inicio
from . import relatorios as rel
from .models import TIPOS_CONTA, Entidade

VALIDADE_PEDIDO = 10 * 60
MAX_PEDIDOS = 200
COOKIE = "gestor_tv"
COOKIE_PEDIDO = "gestor_tv_pedido"
DURACAO_COOKIE = 365 * 24 * 3600


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Pedido:
    codigo: str
    segredo: str
    expira: float
    token: str | None = None        # preenchido quando aprovado; a TV leva-o na consulta seguinte

    @property
    def expirado(self) -> bool:
        return time.time() > self.expira


class Emparelhamentos:
    def __init__(self):
        self._pedidos: dict[str, Pedido] = {}

    def _limpar(self) -> None:
        for c in [c for c, p in self._pedidos.items() if p.expirado]:
            del self._pedidos[c]

    def novo(self) -> Pedido:
        self._limpar()
        if len(self._pedidos) >= MAX_PEDIDOS:          # muitos pedidos por aprovar: sai o mais antigo
            del self._pedidos[min(self._pedidos, key=lambda c: self._pedidos[c].expira)]
        codigo = f"{secrets.randbelow(10 ** 6):06d}"
        while codigo in self._pedidos:
            codigo = f"{secrets.randbelow(10 ** 6):06d}"
        p = Pedido(codigo, secrets.token_urlsafe(24), time.time() + VALIDADE_PEDIDO)
        self._pedidos[codigo] = p
        return p

    def por_segredo(self, segredo: str | None) -> Pedido | None:
        if not segredo:
            return None
        for p in self._pedidos.values():
            if secrets.compare_digest(p.segredo, segredo):
                return None if p.expirado and not p.token else p
        return None

    def por_codigo(self, codigo: str) -> Pedido | None:
        """Pedido à espera de aprovação com este código."""
        p = self._pedidos.get("".join(ch for ch in codigo if ch.isdigit()))
        return p if p and not p.expirado and not p.token else None

    def remover(self, p: Pedido) -> None:
        self._pedidos.pop(p.codigo, None)


# ------------------------------------------------------------------ números das páginas
def sparkline(valores: list[int], largura: int = 600, altura: int = 120, margem: int = 6) -> dict:
    """Linha e área (SVG) de uma série; o desenho é feito no servidor para não depender de JavaScript na TV."""
    if not valores:
        return {"linha": "", "area": "", "largura": largura, "altura": altura}
    lo, hi = min(valores), max(valores)
    amp = (hi - lo) or 1
    n = len(valores)
    pts = []
    for i, v in enumerate(valores):
        x = margem + (largura - 2 * margem) * (i / (n - 1) if n > 1 else 0.5)
        y = margem + (altura - 2 * margem) * (1 - (v - lo) / amp)
        pts.append(f"{x:.1f},{y:.1f}")
    linha = " ".join(pts)
    area = f"M{pts[0]} L{' L'.join(pts[1:]) or pts[0]} L{pts[-1].split(',')[0]},{altura} L{pts[0].split(',')[0]},{altura} Z"
    return {"linha": linha, "area": area, "largura": largura, "altura": altura}


def resumo(db: Session, ent: Entidade, hoje: date) -> dict:
    v = inicio.visao(db, ent, hoje)
    grupos, passivos = inicio.detalhe(db, ent, hoje)
    total_ativos = sum(g.total for g in grupos) or 1
    composicao = [{"titulo": g.titulo, "cor": g.cor, "total": g.total, "pct": g.total / total_ativos * 100}
                  for g in grupos]
    dif, pct = inicio.variacao_mensal(v)
    return {
        "hoje": v.hoje, "variacao": dif, "variacao_pct": pct, "variacao_ano": v.variacao_ano,
        "parte": ent.parte(v.hoje["liquido"]) if ent.heranca else None,
        "composicao": composicao, "passivos": passivos,
        "grafico": sparkline(v.liquido), "inicio_serie": rel.rotulo_mes(v.datas[0]),
    }


ORDEM_TIPOS = ["ORDEM", "POUPANCA", "CARTEIRA_DIGITAL", "CARTAO_REFEICAO", "INVESTIMENTO", "AFORRO", "PENSOES",
               "CARTAO_CREDITO", "EMPRESTIMO"]


def contas(db: Session, ent: Entidade, hoje: date) -> list[dict]:
    """Saldos de hoje por tipo de conta (as dívidas em valor positivo, marcadas como passivo)."""
    por_tipo = defaultdict(list)
    for c, saldo in rel.patrimonio(db, ent.id, [hoje])["contas"]:
        if not c.ativa and not saldo:
            continue
        por_tipo[c.tipo].append({"nome": c.nome, "banco": c.instituicao.nome if c.instituicao else "",
                                 "saldo": -saldo if c.tipo in rel.PASSIVOS else saldo})
    out = []
    for tipo in ORDEM_TIPOS + [t for t in por_tipo if t not in ORDEM_TIPOS]:
        if por_tipo.get(tipo):
            itens = sorted(por_tipo[tipo], key=lambda x: -x["saldo"])
            out.append({"titulo": TIPOS_CONTA.get(tipo, tipo), "passivo": tipo in rel.PASSIVOS,
                        "itens": itens, "total": sum(x["saldo"] for x in itens)})
    return out


def mes(db: Session, ent: Entidade, ini: date, max_categorias: int = 8) -> dict:
    """Receitas, despesas e as maiores categorias de despesa de um mês, com o mês anterior para comparar."""
    fim = rel.fim_do_mes(ini)
    r = rel.resumir(rel.movimentos_reportaveis(db, ent.id, ini, fim))
    ant_ini = rel.somar_meses(ini, -1)
    ant = rel.resumir(rel.movimentos_reportaveis(db, ent.id, ant_ini, rel.fim_do_mes(ant_ini)))
    despesas = [l for l in rel.linhas_ordenadas(r.por_categoria["DESPESA"], ant.por_categoria["DESPESA"]) if l.total > 0]
    maior = max([l.total for l in despesas] or [1])
    return {
        "r": r, "anterior": ant, "rotulo": f"{rel.MESES_PT[ini.month - 1].capitalize()} {ini.year}",
        "despesas": [{"nome": l.nome, "cor": l.cor, "total": l.total, "variacao": l.variacao,
                      "pct": l.total / maior * 100} for l in despesas[:max_categorias]],
        "outras": sum(l.total for l in despesas[max_categorias:]),
        "receitas": [{"nome": l.nome, "total": l.total}
                     for l in rel.linhas_ordenadas(r.por_categoria["RECEITA"]) if l.total > 0][:5],
    }
