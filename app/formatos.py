"""Conversão de montantes e datas no formato português."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_MILHARES_PONTO = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def parse_montante(texto: str | None) -> int:
    """'1.234,56' / '1234.56' / '-12,5 €' -> cêntimos (int). Vazio -> 0."""
    t = (texto or "").strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not t:
        return 0
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    elif _MILHARES_PONTO.match(t):
        t = t.replace(".", "")
    try:
        valor = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"Montante inválido: {texto!r}")
    return int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_montante(cent: int | None, moeda: str = "EUR") -> str:
    """Cêntimos -> '1.234,56 €'."""
    if cent is None:
        return ""
    sinal = "-" if cent < 0 else ""
    inteiro, dec = divmod(abs(cent), 100)
    inteiro_txt = f"{inteiro:,}".replace(",", ".")
    simbolo = {"EUR": "€", "USD": "$", "GBP": "£"}.get(moeda, moeda)
    return f"{sinal}{inteiro_txt},{dec:02d} {simbolo}"


def montante_para_input(cent: int | None) -> str:
    if not cent:
        return ""
    sinal = "-" if cent < 0 else ""
    inteiro, dec = divmod(abs(cent), 100)
    return f"{sinal}{inteiro},{dec:02d}"


def parse_data(texto: str | None) -> date | None:
    """Aceita 'aaaa-mm-dd' (input date do browser) e 'dd-mm-aaaa' / 'dd/mm/aaaa'."""
    t = (texto or "").strip()
    if not t:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})$", t)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    raise ValueError(f"Data inválida: {texto!r}")


def fmt_data(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def normalizar_iban(texto: str | None) -> str | None:
    """'pt50 0018000312345678901 23' -> 'PT50 0018 0003 1234 5678 9012 3' (grupos de 4)."""
    t = re.sub(r"\s+", "", texto or "").upper()
    if not t:
        return None
    if "•" in t:
        return (texto or "").strip()  # já vem truncado (registos antigos)
    if not (re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", t) or (t.isdigit() and len(t) >= 19)):
        return t[:40]                 # n.º de conta / contrato curto: como foi escrito
    return " ".join(t[i:i + 4] for i in range(0, len(t), 4))[:40]


def mascarar_iban(texto: str | None) -> str | None:
    """Guarda só o país e os últimos 4 dígitos: 'PT50 •••• 1234'."""
    t = re.sub(r"\s+", "", texto or "").upper()
    if not t:
        return None
    if "•" in t or len(t) <= 8:
        return (texto or "").strip()
    return f"{t[:4]} •••• {t[-4:]}"


def opcoes_categorias(categorias) -> "Markup":
    """As <option> da árvore de categorias (para um <select>), montadas uma vez por página."""
    from markupsafe import Markup, escape
    partes = ['<option value="">— sem categoria —</option>']
    for raiz in categorias:
        partes.append(f'<option value="{raiz.id}">{escape(raiz.nome)}</option>')
        partes += [f'<option value="{c.id}">&nbsp;&nbsp;{escape(raiz.nome)} › {escape(c.nome)}</option>'
                   for c in raiz.filhas]
    return Markup("".join(partes))


def opcao_selecionada(opcoes, valor) -> "Markup":
    """As mesmas opções com a do valor marcada (selected)."""
    from markupsafe import Markup
    if valor is None:
        return opcoes
    return Markup(str(opcoes).replace(f'<option value="{valor}">', f'<option value="{valor}" selected>', 1))
