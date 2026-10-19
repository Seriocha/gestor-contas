"""Análise da tabela de regras para a otimizar (Categorias e regras › Otimizar regras).

As regras correm por prioridade e a 1.ª que corresponder ganha. Simulando-as sobre os movimentos existentes vê-se:
- **duplicadas**: mesmo padrão e mesmas condições (a 2.ª nunca ganha);
- **sombreadas**: correspondem a movimentos, mas uma regra anterior apanha sempre todos — redundantes se fazem o
  mesmo, em conflito se fazem outra coisa;
- **sem uso**: não correspondem a nenhum movimento;
- **agregáveis**: mesmas condições e mesmas ações com padrões diferentes — juntam-se numa só («A|B|C»). Só se propõe
  quando a simulação mostra que o resultado não muda em nenhum movimento.
"""
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import Movimento, Regra
from .qualificacao import compilar


def _alternativas(r: Regra) -> tuple:
    if r.modo == "regex":
        return (r.padrao,)
    return tuple(sorted({a.strip().upper() for a in r.padrao.split("|") if a.strip()}))


def condicoes(r: Regra) -> tuple:
    """O que limita a regra, além do padrão (a entidade do bem conta: a regra só vale nas contas dessa entidade)."""
    return (r.modo, r.conta_id, r.sentido, r.montante_min, r.montante_max,
            r.bem.entidade_id if r.bem_id and r.bem else None)


def acoes(r: Regra) -> tuple:
    return (r.categoria_id, r.etiqueta_id, r.bem_id, bool(r.marcar_transferencia), bool(r.excluir))


class _Compilada:
    def __init__(self, r: Regra):
        self.r = r
        self.rx = compilar(r.padrao, r.modo)
        self.ent_bem = r.bem.entidade_id if r.bem_id and r.bem else None

    def corresponde(self, m: Movimento, texto: str) -> bool:
        r = self.r
        if r.conta_id and r.conta_id != m.conta_id:
            return False
        if r.bem_id and (self.ent_bem is None or m.conta.entidade_id != self.ent_bem):
            return False
        if r.sentido == "SAIDA" and m.montante >= 0 or r.sentido == "ENTRADA" and m.montante <= 0:
            return False
        valor = abs(m.montante)
        if r.montante_min is not None and valor < r.montante_min or r.montante_max is not None and valor > r.montante_max:
            return False
        return bool(self.rx.search(texto))


def _texto(m: Movimento) -> str:
    return (m.descricao_norm or m.descricao_original or "").upper()


def resultados(regras: list[Regra], movs: list[Movimento]) -> list:
    """Para cada movimento, as ações da 1.ª regra que corresponde (None se nenhuma)."""
    comp = [_Compilada(r) for r in regras]
    out = []
    for m in movs:
        t = _texto(m)
        c = next((c for c in comp if c.corresponde(m, t)), None)
        out.append(acoes(c.r) if c else None)
    return out


@dataclass
class Sombreada:
    regra: Regra
    por: list                      # regras anteriores que lhe tiram os movimentos
    n: int                         # movimentos a que corresponde
    redundante: bool               # as que a tapam fazem o mesmo → pode apagar-se sem mudar nada


@dataclass
class Grupo:
    regras: list                   # pela ordem de prioridade; a 1.ª fica, as outras saem
    padrao: str                    # padrão agregado
    seguro: bool                   # a simulação não muda nenhum movimento
    mudariam: int = 0
    n: int = 0                     # movimentos a que o grupo corresponde


@dataclass
class Analise:
    n_regras: int = 0
    n_movimentos: int = 0
    duplicadas: list = field(default_factory=list)      # [(regra, igual_a)]
    sombreadas: list = field(default_factory=list)      # [Sombreada]
    sem_uso: list = field(default_factory=list)         # [Regra]
    agregaveis: list = field(default_factory=list)      # [Grupo]
    ganhos: dict = field(default_factory=dict)          # regra.id → movimentos em que ganha

    @property
    def n_sugestoes(self) -> int:
        return len(self.duplicadas) + len(self.sombreadas) + len(self.sem_uso) + len(self.agregaveis)


def agregar_padrao(regras: list[Regra]) -> str:
    alts = []
    for r in regras:
        for a in _alternativas(r):
            if a not in alts:
                alts.append(a)
    return "|".join(alts)


def analisar(regras: list[Regra], movs: list[Movimento]) -> Analise:
    """regras: as ativas, por prioridade (e id); movs: os movimentos onde simular (o universo das descrições)."""
    a = Analise(n_regras=len(regras), n_movimentos=len(movs))
    comp = [_Compilada(r) for r in regras]
    textos = [_texto(m) for m in movs]
    apanha = {c.r.id: set() for c in comp}             # regra → índices dos movimentos a que corresponde
    for i, (m, t) in enumerate(zip(movs, textos)):
        for c in comp:
            if c.corresponde(m, t):
                apanha[c.r.id].add(i)
    vencedor = {}
    for i in range(len(movs)):
        vencedor[i] = next((c.r for c in comp if i in apanha[c.r.id]), None)
    a.ganhos = {r.id: sum(1 for v in vencedor.values() if v is r) for r in regras}

    # duplicadas: mesmo padrão e condições que uma anterior
    vistas = {}
    duplicadas = set()
    for r in regras:
        chave = (_alternativas(r), condicoes(r))
        if chave in vistas:
            a.duplicadas.append((r, vistas[chave]))
            duplicadas.add(r.id)
        else:
            vistas[chave] = r

    for r in regras:
        if r.id in duplicadas:
            continue
        idx = apanha[r.id]
        if not idx:
            a.sem_uso.append(r)
        elif a.ganhos[r.id] == 0:
            por = []
            for i in idx:
                v = vencedor[i]
                if v is not None and v is not r and v not in por:
                    por.append(v)
            a.sombreadas.append(Sombreada(r, por, len(idx), all(acoes(p) == acoes(r) for p in por)))

    # agregáveis: mesmas condições e ações (não regex), com padrões diferentes
    grupos = {}
    for r in regras:
        if r.modo == "regex" or r.id in duplicadas:
            continue
        grupos.setdefault((condicoes(r), acoes(r)), []).append(r)
    antes = [acoes(v) if v else None for v in (vencedor[i] for i in range(len(movs)))]
    for lista in grupos.values():
        if len(lista) < 2:
            continue
        primeira = lista[0]
        padrao = agregar_padrao(lista)
        if len(padrao) > 200:             # não cabe numa regra
            continue
        # simulação: a 1.ª regra passa a ter o padrão agregado (fica com a sua prioridade); as outras saem. Só podem
        # mudar os movimentos a que alguma regra do grupo corresponde (a agregada apanha exatamente esses).
        tocados = sorted(set().union(*(apanha[r.id] for r in lista)))
        resto = [r for r in regras if r not in lista[1:]]
        sim = [_Simulada(primeira, padrao) if r is primeira else r for r in resto]
        depois = resultados(sim, [movs[i] for i in tocados])
        mudam = sum(1 for i, y in zip(tocados, depois) if antes[i] != y)
        a.agregaveis.append(Grupo(lista, padrao, mudam == 0, mudam, len(tocados)))
    a.agregaveis.sort(key=lambda g: (not g.seguro, -len(g.regras)))
    return a


class _Simulada:
    """Uma regra com outro padrão, só para a simulação (não toca na base de dados)."""
    def __init__(self, r: Regra, padrao: str):
        self.__dict__.update({k: getattr(r, k) for k in ("id", "modo", "conta_id", "sentido", "montante_min", "montante_max",
                                                          "categoria_id", "etiqueta_id", "bem_id", "marcar_transferencia",
                                                          "excluir", "bem")})
        self.padrao = padrao


def agregar(db: Session, regras: list[Regra]) -> Regra:
    """Junta as regras na 1.ª (padrão «A|B|C», a prioridade mais alta); as outras são apagadas e os movimentos que
    elas tinham qualificado passam a apontar para a que fica."""
    from sqlalchemy import update
    regras = sorted(regras, key=lambda r: (r.prioridade, r.id))
    fica = regras[0]
    fica.padrao = agregar_padrao(regras)[:200]
    for r in regras[1:]:
        db.execute(update(Movimento).where(Movimento.regra_id == r.id).values(regra_id=fica.id))
        db.delete(r)
    db.flush()
    return fica
