"""Leitura de um talão de combustível (e, opcionalmente, do conta-quilómetros) por fotografia.

A fotografia vai à Claude API (o mesmo serviço e chave das sugestões por IA, ficheiro ia.env na NAS), que devolve os
campos lidos; a página de registo preenche o formulário e quem regista confirma antes de gravar — nada é gravado
sem essa confirmação. Sem chave da API a opção não aparece.
"""
import base64
import json
from datetime import date, datetime

from . import ia

TIPOS = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_BYTES = 5 * 1024 * 1024

SISTEMA = """Lês fotografias de talões de abastecimento de combustível de postos portugueses (e, se vier, do \
conta-quilómetros do carro). Devolves só o que está escrito — não inventes. Números com ponto decimal.
Responde APENAS com um objeto JSON com as chaves: data (AAAA-MM-DD ou null), hora (HH:MM ou null), \
litros (número ou null), preco_litro (euros por litro, ou null), valor (total pago em euros, ou null), \
posto (marca e local do posto, curto, ou null), combustivel (ex.: gasóleo, gasolina 95, ou null), \
km (leitura do conta-quilómetros, inteiro, só se houver fotografia do painel; senão null), \
confianca (0 a 1)."""


def _num(v, minimo: float, maximo: float) -> float | None:
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return x if minimo < x < maximo else None


def ler(imagens: list[tuple[bytes, str]], hoje: date | None = None) -> dict:
    """imagens: [(conteúdo, media_type)] — o talão primeiro e, se houver, o painel. Devolve os campos validados."""
    if not ia.disponivel():
        raise ia.ErroIA("A leitura por fotografia precisa da chave da Claude API (ia.env na NAS).")
    if not imagens:
        raise ia.ErroIA("Falta a fotografia do talão.")
    conteudo = []
    for i, (dados, tipo) in enumerate(imagens):
        if tipo not in TIPOS:
            raise ia.ErroIA("Formato de imagem não suportado (usa JPEG ou PNG).")
        if len(dados) > MAX_BYTES:
            raise ia.ErroIA("A fotografia é demasiado grande.")
        conteudo.append({"type": "text", "text": "Talão:" if i == 0 else "Conta-quilómetros:"})
        conteudo.append({"type": "image", "source": {"type": "base64", "media_type": tipo,
                                                     "data": base64.b64encode(dados).decode()}})
    conteudo.append({"type": "text", "text": "Extrai os dados deste abastecimento."})
    texto = ia._chamar(SISTEMA, conteudo)
    ini, fim = texto.find("{"), texto.rfind("}")
    if ini < 0 or fim < ini:
        raise ia.ErroIA("Não foi possível ler o talão.")
    try:
        d = json.loads(texto[ini:fim + 1])
    except json.JSONDecodeError as e:
        raise ia.ErroIA("Não foi possível ler o talão.") from e
    hoje = hoje or date.today()
    out = {}
    try:
        dt = datetime.strptime(str(d.get("data") or ""), "%Y-%m-%d").date()
        if (hoje - dt).days <= 400 and dt <= hoje:
            out["data"] = dt.isoformat()
    except ValueError:
        pass
    hora = str(d.get("hora") or "")
    if len(hora) == 5 and hora[2] == ":" and hora[:2].isdigit() and hora[3:].isdigit():
        out["hora"] = hora
    litros, preco, valor = _num(d.get("litros"), 0, 500), _num(d.get("preco_litro"), 0.3, 5), _num(d.get("valor"), 0, 2000)
    if litros and preco and not valor:
        valor = round(litros * preco, 2)
    if valor and preco and not litros:
        litros = round(valor / preco, 2)
    if litros:
        out["litros"] = round(litros, 2)
    if valor:
        out["valor"] = round(valor, 2)
    if preco:
        out["preco_litro"] = round(preco, 3)
    km = _num(d.get("km"), 0, 5_000_000)
    if km:
        out["km"] = int(km)
    for k, n in (("posto", 120), ("combustivel", 40)):
        if d.get(k):
            out[k] = str(d[k]).strip()[:n]
    try:
        out["confianca"] = max(0.0, min(1.0, float(d.get("confianca"))))
    except (TypeError, ValueError):
        pass
    return out
