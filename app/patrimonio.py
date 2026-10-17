"""Património fora das contas: casas e carros.

- **Despesas por bem**: cada movimento pode ficar ligado a um bem (movimento.bem_id) — é o nível de
  divisão entre a casa principal, a casa de férias, cada carro. A atribuição automática usa os textos
  do extrato de cada bem (padrões) e, para o bem «por omissão» de cada tipo, as despesas das
  categorias Casa / Transportes que nenhum padrão apanhou.
- **Valor**: histórico de avaliações (IA ou à mão). A IA (Claude API, com pesquisa na web quando a
  conta a permite) estima o valor de mercado a partir dos atributos — tipologia, área, localização,
  ano de construção, estado; nos carros marca, modelo, ano, km. Nunca se enviam nomes, contas,
  movimentos nem o preço de compra. A revisão é periódica (revisao_meses) e corre sozinha uma vez
  por dia, quando há chave da API e a preferência está ligada.
"""
import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ia
from . import relatorios as rel
from .models import ACESSOS, AGUAS, CICLO_CORTICA, COMBUSTIVEIS, ESTADOS_BEM, OCUPACOES, USOS_BEM, AvaliacaoBem, Bem, Categoria, Conta, Movimento

log = logging.getLogger(__name__)

# Categoria (raiz) cujas despesas sem padrão vão para o bem «por omissão» de cada tipo.
RAIZ_POR_TIPO = {"casa": "Casa", "carro": "Transportes"}
# Subcategorias de Transportes que não são do carro (o passe, a Uber…).
NAO_CARRO = {"Transportes públicos"}
# O que se aloca à viatura da empresa.
COMBUSTIVEL = {"Combustível"}


# ------------------------------------------------------------------ atribuição das despesas
def _raiz(m: Movimento) -> str | None:
    c = m.categoria
    return (c.pai.nome if c.pai else c.nome) if c else None


def _no_periodo(b: Bem, d: date) -> bool:
    return (not b.data_aquisicao or d >= b.data_aquisicao) and (not b.data_venda or d <= b.data_venda)


def e_combustivel(m: Movimento) -> bool:
    return bool(m.categoria and m.categoria.nome in COMBUSTIVEL)


def grupo_omissao(b: Bem) -> str:
    """Cada grupo tem no máximo um bem «por omissão»: casa, carro e viatura da empresa."""
    return "empresa" if b.empresa else b.tipo


def bem_para(m: Movimento, padroes: list, omissao: dict) -> int | None:
    """O bem a que o movimento pertence pelas regras de atribuição (ou None).

    A viatura da empresa só recebe combustível (pelos padrões, também combustível ainda sem
    categoria) e, sendo «por omissão», tem prioridade sobre o carro próprio no combustível."""
    if m.transferencia:
        return None
    desc = (m.descricao_norm or m.descricao_original or "").upper()
    for p, b in padroes:
        if p in desc and _no_periodo(b, m.data_mov) and (not b.empresa or e_combustivel(m) or not m.categoria):
            return b.id
    if m.montante >= 0:
        return None
    b = omissao.get("empresa")
    if b and e_combustivel(m) and _no_periodo(b, m.data_mov):
        return b.id
    raiz = _raiz(m)
    for grupo in ("casa", "carro"):
        b = omissao.get(grupo)
        if b and raiz == RAIZ_POR_TIPO[grupo] and _no_periodo(b, m.data_mov) \
                and not (grupo == "carro" and m.categoria.nome in NAO_CARRO):
            return b.id
    return None


def atribuir(db: Session, entidade_id: int, movs: list[Movimento] | None = None) -> int:
    """Atribui os movimentos aos bens. Devolve quantos mudaram.

    1.º os padrões de cada bem (texto contido na descrição, dentro do período em que o bem foi nosso);
    2.º o bem «por omissão» do tipo fica com as saídas da categoria Casa (casas) ou Transportes (carros,
    sem os transportes públicos). Sem `movs`, revê todos os movimentos da entidade — a atribuição
    automática acompanha as alterações aos padrões —, menos os escolhidos à mão."""
    bens = db.scalars(select(Bem).where(Bem.entidade_id == entidade_id)).all()
    if movs is None:
        if not bens and not db.scalar(select(Movimento.id).join(Conta).where(
                Conta.entidade_id == entidade_id, Movimento.bem_id.is_not(None)).limit(1)):
            return 0
        movs = db.scalars(select(Movimento).join(Conta).where(
            Conta.entidade_id == entidade_id, ~Movimento.bem_manual)).all()
    padroes = [(p, b) for b in bens for p in b.lista_padroes]
    omissao = {grupo_omissao(b): b for b in bens if b.omissao}
    from . import fuelio
    abastecidos = fuelio.bens_dos_debitos(db, entidade_id)     # combustível com registo no Fuelio
    n = 0
    for m in movs:
        if m.bem_manual:
            continue
        alvo = abastecidos.get(m.id) or bem_para(m, padroes, omissao)
        if alvo != m.bem_id:
            m.bem_id = alvo
            n += 1
    db.flush()
    return n


# ------------------------------------------------------------------ valor e custos
def ultima_avaliacao(b: Bem) -> AvaliacaoBem | None:
    return b.avaliacoes[-1] if b.avaliacoes else None


def valor_atual(b: Bem) -> int | None:
    if b.empresa:
        return None
    if b.vendido:
        return 0
    a = ultima_avaliacao(b)
    return a.valor if a else b.valor_aquisicao


def proxima_revisao(b: Bem) -> date | None:
    """Data em que o valor deve ser revisto (None = revisão só à mão ou bem vendido)."""
    if b.vendido or b.empresa or not b.revisao_meses:
        return None
    a = ultima_avaliacao(b)
    return somar_meses_dia(a.data, b.revisao_meses) if a else date.min


def somar_meses_dia(d: date, n: int) -> date:
    """d + n meses, no mesmo dia (ou no último dia do mês, se esse não existir)."""
    p = rel.somar_meses(d, n)
    return p.replace(day=min(d.day, rel.fim_do_mes(p).day))


def revisao_pendente(b: Bem, hoje: date) -> bool:
    p = proxima_revisao(b)
    return p is not None and p <= hoje


def divida(db: Session, b: Bem, hoje: date) -> int:
    """Capital em dívida do empréstimo associado (positivo), ou 0."""
    if not b.emprestimo_conta or b.vendido or b.empresa:
        return 0
    s = rel.HistoricoConta(db, b.emprestimo_conta).saldo_em(hoje)
    return -s if s and s < 0 else 0


@dataclass
class Custos:
    despesas: int = 0
    receitas: int = 0                                    # rendas, seguros pagos…
    por_categoria: list = field(default_factory=list)    # [(nome, cor, total)]
    por_mes: list = field(default_factory=list)          # [(rótulo, total)]
    movimentos: list = field(default_factory=list)
    partes: dict = field(default_factory=dict)          # {movimento_id: montante deste bem} (débitos repartidos)

    @property
    def liquido(self) -> int:
        return self.despesas - self.receitas


def por_bem(db: Session, entidade_id: int, movs: list[Movimento]) -> list[tuple[Movimento, int | None, int]]:
    """(movimento, bem, montante com sinal) — os débitos Via Verde repartem-se pelos carros (matrícula)."""
    from . import viaverde
    partes = viaverde.partes(db, entidade_id, movs)
    r = []
    for m in movs:
        if m.id in partes:
            r += [(m, bid, -v) for bid, v in partes[m.id].items()]
        else:
            r.append((m, m.bem_id, m.montante))
    return r


def custos(db: Session, b: Bem, ini: date, fim: date) -> Custos:
    linhas = [(m, v) for m, bid, v in por_bem(db, b.entidade_id, rel.movimentos_reportaveis(db, b.entidade_id, ini, fim))
              if bid == b.id]
    movs = [m for m, _ in linhas]
    c = Custos(movimentos=sorted(movs, key=lambda m: (m.data_mov, m.id), reverse=True))
    c.partes = {m.id: v for m, v in linhas if v != m.montante}
    cats, meses = defaultdict(int), defaultdict(int)
    cores = {}
    for m, v in linhas:
        if v < 0:
            c.despesas += -v
            nome = m.categoria.nome_completo if m.categoria else "Sem categoria"
            cats[nome] += -v
            cores[nome] = m.categoria.cor_efetiva if m.categoria else "#8a8a85"
        else:
            c.receitas += v
        meses[m.data_mov.replace(day=1)] += -v
    c.por_categoria = sorted(((n, cores[n], t) for n, t in cats.items()), key=lambda x: -x[2])
    d, ultimo = ini.replace(day=1), fim.replace(day=1)
    while d <= ultimo:
        c.por_mes.append((rel.rotulo_mes(d), meses.get(d, 0)))
        d = rel.somar_meses(d, 1)
    return c


@dataclass
class LinhaBem:
    bem: Bem
    valor: int | None
    divida: int
    custo_12m: int
    receitas_12m: int
    proxima: date | None
    pendente: bool
    manutencao: object = None        # manutencao.Resumo (carros)

    @property
    def liquido(self) -> int:
        return (self.valor or 0) - self.divida

    @property
    def custo_mensal(self) -> int:
        return round(self.custo_12m / 12)

    @property
    def variacao(self) -> int | None:
        """Valor atual face ao preço de compra."""
        b = self.bem
        return (self.valor - b.valor_aquisicao) if self.valor is not None and b.valor_aquisicao else None


def resumo(db: Session, entidade_id: int, hoje: date) -> dict:
    bens = db.scalars(select(Bem).where(Bem.entidade_id == entidade_id).order_by(Bem.tipo, Bem.nome)).all()
    ini = rel.somar_meses(hoje.replace(day=1), -11)
    gastos, recebido = defaultdict(int), defaultdict(int)
    for m, bid, v in por_bem(db, entidade_id, rel.movimentos_reportaveis(db, entidade_id, ini, hoje)):
        if not bid:
            continue
        if v < 0:
            gastos[bid] += -v
        else:
            recebido[bid] += v
    linhas = [LinhaBem(b, valor_atual(b), divida(db, b, hoje), gastos[b.id], recebido[b.id], proxima_revisao(b),
                       revisao_pendente(b, hoje)) for b in bens]
    from . import manutencao as mt
    for l in linhas:
        l.manutencao = mt.resumo(l.bem, hoje) if l.bem.tipo == "carro" and not l.bem.vendido else None
    ativos = [l for l in linhas if not l.bem.vendido]
    proprios = [l for l in ativos if not l.bem.empresa]
    por_uso = defaultdict(lambda: [0, 0])          # (tipo, uso) → [custo 12m, n.º de bens]
    for l in ativos:
        por_uso[(l.bem.tipo, l.bem.uso)][0] += l.custo_12m - l.receitas_12m
        por_uso[(l.bem.tipo, l.bem.uso)][1] += 1
    # Despesas de casa/transportes ainda sem bem (para atribuir).
    sem_bem = [m for m in rel.movimentos_reportaveis(db, entidade_id, ini, hoje)
               if not m.bem_id and m.montante < 0 and _raiz(m) in {RAIZ_POR_TIPO[b.tipo] for b in bens if b.tipo in RAIZ_POR_TIPO}
               and m.categoria.nome not in NAO_CARRO]
    return {
        "linhas": linhas,
        "casas": [l for l in linhas if l.bem.tipo == "casa"],
        "carros": [l for l in linhas if l.bem.tipo == "carro"],
        "rusticos": [l for l in linhas if l.bem.tipo == "rustico"],
        "valor": sum(l.valor or 0 for l in proprios),
        "divida": sum(l.divida for l in proprios),
        "liquido": sum(l.liquido for l in proprios),
        "custo_12m": sum(l.custo_12m - l.receitas_12m for l in ativos),
        "por_uso": dict(por_uso),
        # Comparação casa principal vs. férias (só interessa com mais de um uso).
        "casas_por_uso": [(USOS_BEM["casa"].get(uso, uso), custo, nb) for (tipo, uso), (custo, nb) in
                          sorted(por_uso.items(), key=lambda x: list(USOS_BEM["casa"]).index(x[0][1])
                                 if x[0][1] in USOS_BEM["casa"] else 99) if tipo == "casa"],
        "pendentes": [l for l in ativos if l.pendente],
        "oficina": [l for l in ativos if l.manutencao and l.manutencao.precisa_oficina],
        "ler_km": [l for l in ativos if l.manutencao and l.manutencao.lembrar_km],
        "sem_bem": len(sem_bem), "sem_bem_total": -sum(m.montante for m in sem_bem),
        "desde": ini,
    }


# ------------------------------------------------------------------ avaliação por IA
SISTEMA = """És um avaliador imobiliário (urbano e rústico) e automóvel em Portugal. Estimas o valor de mercado ATUAL \
(preço provável de venda entre particulares) de um bem, a partir dos atributos dados. \
Se tiveres a ferramenta de pesquisa na web, usa-a para preços recentes na zona (ex.: preço mediano \
por m² do INE, anúncios do idealista/imovirtual, preço por hectare de terrenos rústicos da mesma ocupação e \
região, ou anúncios de carros usados no standvirtual) e \
indica as páginas consultadas. Tem em conta a tendência face às avaliações anteriores, se as houver, \
mas não as copies. Valores em euros, números inteiros. Se faltar informação, alarga o intervalo e \
baixa a confiança — não inventes.
Nos prédios rústicos com montado de sobro, a cortiça conta: o valor depende dos sobreiros em produção, da \
produção (arrobas) e de quantos anos faltam para a próxima tiragem (ciclo de 9 anos).
Responde APENAS com um objeto JSON com as chaves: valor, minimo, maximo, preco_m2 (casas; null nos \
outros), preco_ha (prédios rústicos; null nos outros), justificacao (2 a 4 frases em português de Portugal), referencias (lista de URLs), confianca (0 a 1)."""


def descricao_para_ia(b: Bem) -> str:
    """Só atributos do bem — nunca o nome que o utilizador lhe deu, contas, movimentos nem o preço de compra."""
    linhas = []
    if b.tipo == "rustico":
        linhas.append("Prédio rústico" + (f" com {('%g' % b.area_ha).replace('.', ',')} ha" if b.area_ha else ""))
        if b.localizacao:
            linhas.append(f"Localização: {b.localizacao}, Portugal")
        if b.lista_ocupacao:
            linhas.append("Ocupação do solo: " + ", ".join(OCUPACOES[x] for x in b.lista_ocupacao))
        if b.lista_agua:
            linhas.append("Água: " + ", ".join(AGUAS[x] for x in b.lista_agua))
        if b.acesso:
            linhas.append(f"Acesso: {ACESSOS[b.acesso]}")
        if b.tem_cortica:
            c = "Cortiça: sim"
            if b.sobreiros:
                c += f", cerca de {b.sobreiros} sobreiros em produção"
            if b.tiragens:
                c += f"; tiragens em {', '.join(map(str, b.tiragens))} (próxima prevista em {b.proxima_tiragem})"
            if b.producao_arrobas:
                c += f"; última produção {b.producao_arrobas} arrobas"
            linhas.append(c)
        if b.extras:
            linhas.append(f"Construções e benfeitorias: {b.extras}")
        return "\n".join(linhas)
    if b.tipo == "casa":
        linhas.append(f"Imóvel: habitação T{b.tipologia}" if b.tipologia is not None else "Imóvel: habitação")
        if b.area_m2:
            linhas.append(f"Área útil: {b.area_m2} m²")
        if b.localizacao:
            linhas.append(f"Localização: {b.localizacao}, Portugal")
        if b.ano:
            linhas.append(f"Ano de construção: {b.ano}")
    else:
        linhas.append("Automóvel: " + " ".join(x for x in (b.marca, b.modelo) if x))
        if b.ano:
            linhas.append(f"Ano: {b.ano}")
        if b.km is not None:
            linhas.append(f"Quilómetros: {b.km}")
        if b.combustivel:
            linhas.append(f"Combustível: {COMBUSTIVEIS.get(b.combustivel, b.combustivel)}")
    if b.estado:
        linhas.append(f"Estado: {ESTADOS_BEM.get(b.estado, b.estado)}")
    if b.extras:
        linhas.append(("Características: " if b.tipo == "casa" else "Versão / equipamento: ") + b.extras)
    return "\n".join(linhas)


def falta_para_ia(b: Bem) -> list[str]:
    """Atributos mínimos para a IA dar um valor com sentido."""
    if b.tipo == "rustico":
        return [n for n, v in (("área", b.area_ha), ("localização", b.localizacao)) if not v]
    if b.tipo == "casa":
        falta = [n for n, v in (("tipologia", b.tipologia), ("localização", b.localizacao)) if v in (None, "")]
        if not b.area_m2 and b.tipologia is None:
            falta.append("área")
        return falta
    return [n for n, v in (("marca", b.marca), ("modelo", b.modelo), ("ano", b.ano)) if not v]


def _euros(v) -> int | None:
    try:
        return int(round(float(v) * 100)) if v is not None and float(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _objeto_json(texto: str) -> dict:
    ini, fim = texto.find("{"), texto.rfind("}")
    if ini < 0 or fim < ini:
        raise ia.ErroIA("A resposta da IA não trouxe JSON.")
    try:
        d = json.loads(texto[ini:fim + 1])
    except json.JSONDecodeError as e:
        raise ia.ErroIA(f"JSON inválido na resposta da IA ({e}).") from e
    if not isinstance(d, dict):
        raise ia.ErroIA("A resposta da IA não é um objeto JSON.")
    return d


PESQUISA_WEB = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5, "user_location": {
    "type": "approximate", "country": "PT", "timezone": "Europe/Lisbon"}}]


def avaliar_ia(db: Session, b: Bem, hoje: date) -> AvaliacaoBem:
    """Pede à IA o valor atual do bem e guarda-o no histórico."""
    if b.empresa:
        raise ia.ErroIA("A viatura da empresa não entra no património — não tem avaliação.")
    if not ia.disponivel():
        raise ia.ErroIA("A chave da Claude API não está configurada (ANTHROPIC_API_KEY).")
    falta = falta_para_ia(b)
    if falta:
        raise ia.ErroIA("Para avaliar, indica: " + ", ".join(falta) + ".")
    historico = "\n".join(f"- {a.data.strftime('%d/%m/%Y')}: {a.valor // 100} €" for a in b.avaliacoes[-6:])
    mensagem = (f"Data de hoje: {hoje.strftime('%d/%m/%Y')}\n\n{descricao_para_ia(b)}"
                + (f"\n\nAvaliações anteriores deste bem:\n{historico}" if historico else ""))
    try:
        texto = ia._chamar(SISTEMA, mensagem, ferramentas=PESQUISA_WEB)
    except ia.ErroIA as e:
        # Conta sem pesquisa na web ativada: repete sem a ferramenta (estimativa só com o conhecimento do modelo).
        if "400" not in str(e) and "web_search" not in str(e):
            raise
        texto = ia._chamar(SISTEMA, mensagem)
    d = _objeto_json(texto)
    valor = _euros(d.get("valor"))
    if not valor:
        raise ia.ErroIA("A IA não deu um valor.")
    minimo, maximo = _euros(d.get("minimo")), _euros(d.get("maximo"))
    try:
        conf = max(0.0, min(1.0, float(d.get("confianca"))))
    except (TypeError, ValueError):
        conf = None
    refs = d.get("referencias") or []
    refs = [str(r)[:300] for r in refs if isinstance(r, str) and r.startswith("http")][:8] if isinstance(refs, list) else []
    justificacao = str(d.get("justificacao") or "")[:1500] or None
    if d.get("preco_ha") and b.tipo == "rustico":
        try:
            justificacao = f"{justificacao or ''} (≈ {int(float(d['preco_ha']))} €/ha)".strip()
        except (TypeError, ValueError):
            pass
    if d.get("preco_m2") and b.tipo == "casa":
        try:
            justificacao = f"{justificacao or ''} (≈ {int(float(d['preco_m2']))} €/m²)".strip()
        except (TypeError, ValueError):
            pass
    a = AvaliacaoBem(bem_id=b.id, data=hoje, valor=valor,
                     minimo=minimo if minimo and minimo <= valor else None,
                     maximo=maximo if maximo and maximo >= valor else None,
                     fonte="ia", justificacao=justificacao, referencias="\n".join(refs) or None,
                     confianca=conf, modelo=ia.modelo())
    b.avaliacoes.append(a)
    db.flush()
    return a


def rever_pendentes(db: Session, hoje: date) -> int:
    """Revisão periódica: avalia por IA os bens com revisão em atraso. Devolve quantos foram avaliados."""
    if not ia.disponivel():
        return 0
    n = 0
    for b in db.scalars(select(Bem)):
        if revisao_pendente(b, hoje) and not falta_para_ia(b):
            try:
                avaliar_ia(db, b, hoje)
                db.commit()
                n += 1
            except ia.ErroIA:
                db.rollback()
                log.warning("Revisão automática do valor de «%s» falhou", b.nome, exc_info=True)
    return n


def serie_valor(b: Bem) -> dict:
    """Dados do gráfico do histórico: data, valor e intervalo."""
    pontos = []
    if b.data_aquisicao and b.valor_aquisicao:
        pontos.append((b.data_aquisicao, b.valor_aquisicao, None, None))
    pontos += [(a.data, a.valor, a.minimo, a.maximo) for a in b.avaliacoes]
    return {"rotulos": [d.strftime("%d/%m/%Y") for d, *_ in pontos],
            "valor": [v / 100 for _, v, _, _ in pontos],
            "minimo": [(mi or v) / 100 for _, v, mi, _ in pontos],
            "maximo": [(ma or v) / 100 for _, v, _, ma in pontos]}


def categoria_raiz_id(db: Session, tipo: str) -> int | None:
    return db.scalar(select(Categoria.id).where(Categoria.nome == RAIZ_POR_TIPO.get(tipo), Categoria.pai_id.is_(None)))


async def ciclo_revisoes(SessionLocal, prefs) -> None:
    """Uma vez por dia (e no arranque): revisão automática do valor dos bens em atraso."""
    from .preferencias import ligada
    while True:
        try:
            if ia.disponivel() and ligada(prefs(), "avaliacao_automatica"):
                def correr():
                    with SessionLocal() as db:
                        return rever_pendentes(db, date.today())
                n = await asyncio.to_thread(correr)
                if n:
                    log.info("Revisão automática: %d bens avaliados", n)
        except Exception:
            log.exception("Falha na revisão automática do valor dos bens")
        await asyncio.sleep(24 * 3600)


def valor_total(db: Session, entidade_id: int) -> tuple[int, int]:
    """(valor dos bens não vendidos, n.º de bens) — para o painel."""
    bens = [b for b in db.scalars(select(Bem).where(Bem.entidade_id == entidade_id))
            if not b.vendido and not b.empresa]
    return sum(valor_atual(b) or 0 for b in bens), len(bens)
