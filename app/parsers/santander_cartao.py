"""Santander — cartão de crédito: Consulta Movimentos Extracto (.xls).

Formato observado (2026-10): folha "Consulta Movimentos Extracto", ~6 linhas
de cabeçalho (data, titular), depois
  Movimento | Data | Descrição | Estado | Montante
mas cada linha tem uma coluna a mais, sem título, com C/D (crédito/débito)
antes do valor, que vem sempre positivo:
  1 | 06-07-2026 | PAG.TRANS.BANCARIA | EXTRACTADO | C | 209.36
Não traz saldo.

Sinal: D (compra) → negativo (aumenta a dívida); C (pagamento, devolução)
→ positivo. Só se importam movimentos EXTRACTADO: os que ainda estão em
curso podem mudar de data/descrição quando forem extratados e criariam
duplicados.
"""
from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["movimento", "data", "descri", "estado", "montante"]
ESTADO_FINAL = "EXTRACTADO"


def _sinal_e_valor(linha, inicio: int):
    """Procura, a partir da coluna Estado, o indicador C/D e o 1.º valor numérico."""
    sinal, valor = None, None
    for v in linha[inicio:]:
        txt = str(v).strip().upper() if v is not None else ""
        if txt in ("C", "D") and sinal is None:
            sinal = txt
        elif txt and valor is None:
            try:
                valor = para_cent(v)
            except ValueError:
                continue
    return sinal, valor


class Parser:
    chave = "santander_cartao"
    banco = "Santander (cartão de crédito)"
    FICHA = {
        "extrato": "Cartão de crédito",
        "ficheiro": "XLS (Excel 97)",
        "onde": "NetBanco › Cartões › Consulta Movimentos Extracto › Exportar",
        "colunas": "Movimento, Data, Descrição, Estado, (C/D sem título), Montante",
        "ordem": "Indiferente (a app ordena por data)",
        "saldo": False,
        "destino": "Cartão de crédito",
        "notas": "Valores sempre positivos: o sinal vem da coluna C/D. Só se importam movimentos EXTRACTADO. O pagamento («PAG…») liga-se ao débito na conta pagadora.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        if encontrar_cabecalho(linhas, COLUNAS) is None:
            return False
        return any("extracto" in normalizar_cabecalho(l[0]) for l in linhas[:8] if l)

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        movs, em_curso = [], 0
        for linha in linhas[i + 1:]:
            if not linha or not str(linha[col["data"]]).strip():
                continue
            estado = str(linha[col["estado"]] or "").strip().upper()
            sinal, valor = _sinal_e_valor(linha, col["estado"] + 1)
            if valor is None:
                continue
            if estado and estado != ESTADO_FINAL:
                em_curso += 1
                continue
            # Sem indicador C/D, um valor negativo já vem com o sinal certo; positivo = compra.
            if sinal == "C":
                montante = abs(valor)
            elif sinal == "D" or valor > 0:
                montante = -abs(valor)
            else:
                montante = valor
            movs.append(MovimentoBruto(
                data_mov=para_data(linha[col["data"]]), data_valor=None,
                descricao=str(linha[col["descri"]]).strip(), montante=montante))
        extrato = Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, cartao=True)
        if em_curso:
            extrato.avisos.append(f"{em_curso} movimento(s) ainda em curso (não extratados) foram ignorados; "
                                  "serão importados quando constarem de um extrato.")
        return extrato
