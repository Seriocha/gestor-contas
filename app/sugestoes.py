"""Movimentos sem categoria agrupados por padrão de descrição, com sugestão de
categoria (do histórico ou da IA) e criação de regra num clique.

O padrão de cada movimento é a sequência mais longa de palavras só com letras
da descrição normalizada, sem os prefixos do banco (COMPRA, DD, TRF, P/ …):
  'ROYAL VERSAILLES LISBOA'  ←  'COMPRA 6344 ROYAL VERSAILLES LISBOA'
  'SEGURO VIAGEM'            ←  'TRF P/ SEGURO VIAGEM ON/OFF AV'
É sempre um pedaço contíguo da própria descrição, por isso serve diretamente
como padrão («contém») de uma regra.
"""
import re
from functools import lru_cache
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .models import Movimento
from .qualificacao import compilar

PREFIXOS = {"COMPRA", "PAG", "PAG.", "PAGAMENTO", "DD", "DD-", "TRF", "TRF.", "TRANSF", "TRANSFERENCIA",
            "TRANSFERÊNCIA", "IMEDIATA", "SEPA", "MB", "WAY", "P/", "P/O", "DE", "DO", "DA", "A", "EM", "LEV",
            "LEVANTAMENTO", "CARTAO", "CARTÃO", "REF", "REF.", "N", "NO", "Nº"}
PALAVRA = re.compile(r"[A-ZÀ-Ý][A-ZÀ-Ý'&.\-]*[A-ZÀ-Ý.]")
# Transferências e pagamentos a pessoas: o texto tem nomes — nunca vão para a IA.
COMISSAO = re.compile(r"COMISS|IMPOSTO DO SELO|\bIMP\.? SELO")
PESSOAL = re.compile(r"MB ?WAY|\bTRF\b|TRANSFER|\bP/\s*[A-Z]+ [A-Z]+ [A-Z]+$")


@lru_cache(maxsize=65536)     # chamado para cada movimento em várias análises; as descrições repetem-se muito
def padrao_de(descricao: str) -> str:
    texto = re.sub(r"\s+", " ", (descricao or "").upper()).strip()
    melhor, atual = [], []
    for tok in texto.split(" "):
        if PALAVRA.fullmatch(tok) and not (not atual and tok in PREFIXOS):
            atual.append(tok)
            if sum(map(len, atual)) > sum(map(len, melhor)):
                melhor = list(atual)
        else:
            atual = []
    while melhor and melhor[-1] in PREFIXOS:
        melhor.pop()
    if melhor and sum(map(len, melhor)) >= 3:
        return " ".join(melhor[:4])
    letras = re.findall(r"[A-ZÀ-Ý]{3,}", texto)
    return max(letras, key=len) if letras else ""


@dataclass
class Grupo:
    padrao: str
    sentido: str                      # SAIDA | ENTRADA (a maioria)
    movimentos: list = field(default_factory=list)
    pessoal: bool = False
    sugestao_id: int | None = None    # categoria sugerida
    origem: str | None = None         # "histórico" | "IA"
    comerciante: str | None = None
    atividade: str | None = None
    confianca: float | None = None

    @property
    def chave(self) -> str:
        return f"{self.padrao}|{self.sentido}"

    @property
    def n(self) -> int:
        return len(self.movimentos)

    @property
    def total(self) -> int:
        return sum(m.montante for m in self.movimentos)

    @property
    def ultima(self) -> date:
        return max(m.data_mov for m in self.movimentos)

    @property
    def exemplos(self) -> list[str]:
        vistos = []
        for m in sorted(self.movimentos, key=lambda m: m.data_mov, reverse=True):
            if m.descricao_original not in vistos:
                vistos.append(m.descricao_original)
            if len(vistos) == 3:
                break
        return vistos

    @property
    def contas(self) -> list[str]:
        return sorted({m.conta.nome for m in self.movimentos})


def agrupar(sem_categoria: list[Movimento], categorizados: list[Movimento]) -> list[Grupo]:
    """Grupos por (padrão, sentido), maiores primeiro, com a categoria mais usada no histórico
    para descrições com o mesmo padrão."""
    por_chave: dict[tuple, Grupo] = {}
    for m in sem_categoria:
        p = padrao_de(m.descricao_norm or m.descricao_original)
        if not p:
            continue
        sentido = "ENTRADA" if m.montante > 0 else "SAIDA"
        g = por_chave.get((p, sentido)) or por_chave.setdefault((p, sentido), Grupo(p, sentido))
        g.movimentos.append(m)
        if PESSOAL.search((m.descricao_norm or m.descricao_original or "").upper()):
            g.pessoal = True
    grupos = sorted(por_chave.values(), key=lambda g: (-g.n, -abs(g.total)))
    historico = defaultdict(list)
    for m in categorizados:
        historico["ENTRADA" if m.montante > 0 else "SAIDA"].append(m)
    # As comissões repetem o nome do comerciante («Comissão Revolut (…): McDonald's») e não dizem nada
    # sobre a compra: ficam fora do histórico usado para sugerir.
    padroes_hist = {id(m): padrao_de(m.descricao_norm or m.descricao_original) for ms in historico.values() for m in ms
                    if not COMISSAO.search((m.descricao_norm or m.descricao_original or "").upper())}
    # por sentido: (padrão, texto, categoria) de cada movimento do histórico, e o mesmo padrão → categorias
    linhas_hist = {s: [(padroes_hist[id(m)], (m.descricao_norm or m.descricao_original or "").upper(), m.categoria_id)
                       for m in ms if id(m) in padroes_hist] for s, ms in historico.items()}
    por_padrao = {s: defaultdict(list) for s in linhas_hist}
    for s, ls in linhas_hist.items():
        for p, _, cat in ls:
            por_padrao[s][p].append(cat)
    for g in grupos:
        # Padrão curto (ex.: «COOP»): só o mesmo padrão exato — «contém» apanharia descrições de outras coisas.
        rx = compilar(g.padrao, "contem") if len(g.padrao) >= 8 else None
        if rx:
            usadas = Counter(cat for p, t, cat in linhas_hist.get(g.sentido, ()) if p == g.padrao or rx.search(t))
        else:
            usadas = Counter(por_padrao.get(g.sentido, {}).get(g.padrao, ()))
        if usadas:
            g.sugestao_id, g.origem = usadas.most_common(1)[0][0], "histórico"
    return grupos
