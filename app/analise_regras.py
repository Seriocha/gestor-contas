"""Análise da tabela de regras para a otimizar (Categorias e regras › Otimizar regras).

As regras correm por prioridade e a 1.ª que corresponder ganha. Simulando-as sobre os movimentos existentes vê-se:
- **duplicadas**: mesmo padrão e mesmas condições (a 2.ª nunca ganha);
- **sombreadas**: correspondem a movimentos, mas uma regra anterior apanha sempre todos — redundantes se fazem o
  mesmo, em conflito se fazem outra coisa;
- **sem uso**: não correspondem a nenhum movimento;
- **agregáveis**: mesmas condições e mesmas ações com padrões diferentes — juntam-se numa só («A|B|C»). Só se propõe
  quando a simulação mostra que o resultado não muda em nenhum movimento.
"""
from collections import Counter
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

        self.com_condicoes = bool(r.conta_id or r.bem_id or r.sentido or r.montante_min is not None
                                  or r.montante_max is not None)

    def passa(self, conta_id: int, entidade_id, montante: int) -> bool:
        """As condições além do padrão (conta, entidade do bem, sentido, valor)."""
        r = self.r
        if r.conta_id and r.conta_id != conta_id:
            return False
        if r.bem_id and (self.ent_bem is None or entidade_id != self.ent_bem):
            return False
        if r.sentido == "SAIDA" and montante >= 0 or r.sentido == "ENTRADA" and montante <= 0:
            return False
        valor = abs(montante)
        if r.montante_min is not None and valor < r.montante_min or r.montante_max is not None and valor > r.montante_max:
            return False
        return True

    def corresponde(self, m: Movimento, texto: str) -> bool:
        ent = _entidade(m) if self.r.bem_id else None
        return self.passa(m.conta_id, ent, m.montante) and bool(self.rx.search(texto))


def _entidade(m) -> int:
    """Entidade do movimento: Movimento (pela conta) ou linha leve com a coluna entidade_id."""
    return m.entidade_id if hasattr(m, "entidade_id") else m.conta.entidade_id


def _texto(m: Movimento) -> str:
    return (m.descricao_norm or m.descricao_original or "").upper()


def _apanha(comp: list, movs: list[Movimento], textos: list[str]) -> dict:
    """regra.id → índices dos movimentos a que corresponde. O padrão corre uma vez por descrição distinta (há muitas
    repetidas) e as condições só para as regras que as têm — em vez de regra × movimento chamadas."""
    por_texto: dict[str, list[int]] = {}
    for i, t in enumerate(textos):
        por_texto.setdefault(t, []).append(i)
    precisa_ent = any(c.r.bem_id for c in comp)
    dados = [(m.conta_id, _entidade(m) if precisa_ent else None, m.montante) for m in movs]
    out = {}
    for c in comp:
        idx = [i for t, ii in por_texto.items() if c.rx.search(t) for i in ii]
        if c.com_condicoes:
            idx = [i for i in idx if c.passa(*dados[i])]
        out[c.r.id] = set(idx)
    return out


def _vencedores(comp: list, apanha: dict, n: int) -> list:
    """Para cada movimento, a 1.ª regra (por prioridade) que lhe corresponde, ou None."""
    vencedor = [None] * n
    for c in comp:
        for i in apanha[c.r.id]:
            if vencedor[i] is None:
                vencedor[i] = c.r
    return vencedor


def resultados(regras: list[Regra], movs: list[Movimento]) -> list:
    """Para cada movimento, as ações da 1.ª regra que corresponde (None se nenhuma)."""
    comp = [_Compilada(r) for r in regras]
    vencedor = _vencedores(comp, _apanha(comp, movs, [_texto(m) for m in movs]), len(movs))
    return [acoes(v) if v else None for v in vencedor]


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
    """regras: as ativas, por prioridade (e id); movs: os movimentos onde simular (o universo das descrições) —
    objetos Movimento ou linhas com id, conta_id, entidade_id, montante, descricao_norm e descricao_original."""
    a = Analise(n_regras=len(regras), n_movimentos=len(movs))
    comp = [_Compilada(r) for r in regras]
    textos = [_texto(m) for m in movs]
    apanha = _apanha(comp, movs, textos)               # regra → índices dos movimentos a que corresponde
    vencedor = _vencedores(comp, apanha, len(movs))
    contagem = Counter(id(v) for v in vencedor if v is not None)
    a.ganhos = {r.id: contagem.get(id(r), 0) for r in regras}

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
