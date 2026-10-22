"""Parser do layout Credibom / Sicoob (extrato SISBR, conta corrente) — v2.4

Correções em relação à v2.3:
  * Valores terminados em "*" (ex.: DEP.CHEQUE BLOQ.1D 550,00*) agora são
    lidos normalmente: o "*" é descartado e o valor é tratado como crédito
    (positivo), em vez de ficar em branco.
"""

import re
import warnings
from datetime import date

import pdfplumber

NOME = "Sicoob (extrato SISBR) v2.4"

BANCO_NOME = "Banco Cooperativo do Brasil"
BANCO_ORG = "BANCO COOPERATIVO SICOOB S.A. - BANCO SICOOB"
BANCO_ID = "756"

X_DATA_MAX = 135
X_VALOR_MIN = 400
TOL_Y = 3

RE_DATA = re.compile(r"^\d{2}/\d{2}(?:/\d{4})?$")
RE_NUM = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$|^-?\d+,\d{2}$")
# sufixo: D, C ou * (valor bloqueado / sem natureza D-C)
RE_NUM_DC = re.compile(
    r"^(-?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2})([DC*])$", re.IGNORECASE
)

RE_COOP = re.compile(r"COOP\.:\s*([\d.\-]+\d)")
RE_CONTA = re.compile(r"CONTA:\s*([\d.\-]+\d)")
RE_PERIODO = re.compile(
    r"PER[ÍI]ODO:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})"
)

JUNK = (
    "sicoob", "sistema de cooperativas", "plataforma de serviços",
    "extrato conta corrente", "coop.:", "conta:", "período:", "periodo:",
    "histórico de movimenta", "data histórico", "resumo", "(+)", "(-)", "(=)",
    "saldo bloqueado", "encargos vencidos", "encargos a vencer",
    "juros vencidos", "tarifas vencidas", "outras informações",
    "vencimento cheque", "taxa cheque", "custo efetivo", "extratos emitidos",
    "sac:", "ouvidoria", "página", "pagina",
)

FIM_TABELA = "RESUMO"
SUFIXOS = ("D", "C", "*")

avisos: list = []


def _para_float(numero: str) -> float:
    return float(numero.lstrip("-").replace(".", "").replace(",", "."))


def _aplicar_dc(valor, dc):
    if valor is None or not dc:
        return valor
    if str(dc).upper() == "D":
        return -abs(valor)
    # "C" ou "*" => positivo
    return abs(valor)


def _parse_money(txt: str):
    """Devolve (valor, tem_dc, sufixo)."""
    m = RE_NUM_DC.match(txt)
    if m:
        numero, dc = m.group(1), m.group(2).upper()
        val = _para_float(numero)
        if dc == "D" or numero.startswith("-"):
            val = -val
        return val, True, dc
    if RE_NUM.match(txt):
        neg = txt.startswith("-")
        val = _para_float(txt)
        if neg:
            return -val, True, "D"
        return val, False, None
    if txt.upper() in SUFIXOS:
        return None, False, txt.upper()
    return None, False, None


def _agrupar_linhas(words):
    linhas, atual, topo = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo <= TOL_Y:
            atual.append(w)
            topo = w["top"] if topo is None else topo
        else:
            linhas.append(atual)
            atual = [w]
            topo = w["top"]
    if atual:
        linhas.append(atual)
    return linhas


def _processar_linha(linha):
    ws = sorted(linha, key=lambda w: w["x0"])
    if not ws:
        return None

    data = None
    start_idx = 0
    if ws[0]["x0"] < X_DATA_MAX and RE_DATA.match(ws[0]["text"].strip()):
        data = ws[0]["text"].strip()
        start_idx = 1

    hist, detalhe = [], []
    valor = None
    tem_dc = False
    dc = None

    for w in ws[start_idx:]:
        txt = w["text"].strip()
        if not txt:
            continue
        val, has_dc, maybe_dc = _parse_money(txt)
        na_coluna_valor = w["x0"] >= X_VALOR_MIN

        if maybe_dc and val is None and (na_coluna_valor or valor is not None):
            dc = maybe_dc
            if valor is not None:
                valor = _aplicar_dc(valor, dc)
                tem_dc = True
            continue

        if val is not None and (na_coluna_valor or valor is None):
            if valor is None:
                valor = val
                tem_dc = has_dc
                if dc:
                    valor = _aplicar_dc(valor, dc)
                    tem_dc = True
                if maybe_dc:
                    dc = maybe_dc
            continue

        if valor is not None:
            detalhe.append(txt)
        else:
            hist.append(txt)

    return {
        "data": data,
        "hist": " ".join(hist).strip(),
        "valor": valor,
        "tem_dc": tem_dc,
        "dc": dc,
        "detalhe": " ".join(detalhe).strip(),
        "x_min": ws[0]["x0"],
    }


def _processar_detalhe(transacoes, detalhe):
    if not transacoes or not detalhe or _junk(detalhe):
        return
    if detalhe.upper() in SUFIXOS:
        return
    if RE_NUM.match(detalhe) or RE_NUM_DC.match(detalhe):
        return

    doc_match = re.search(r"DOC\.:\s*(.+?)(?:\s*$)", detalhe, re.IGNORECASE)
    if doc_match:
        nome_part = detalhe[:doc_match.start()].strip()
        doc_part = doc_match.group(1).strip()

        if nome_part:
            if transacoes[-1]["nome"] is None:
                transacoes[-1]["nome"] = nome_part
            else:
                transacoes[-1]["nome"] += " " + nome_part

        transacoes[-1]["doc"] = doc_part
    else:
        if transacoes[-1]["nome"] is None:
            transacoes[-1]["nome"] = detalhe
        else:
            transacoes[-1]["nome"] += " " + detalhe


def _junk(texto: str) -> bool:
    t = texto.lower().strip()
    return any(t.startswith(j) for j in JUNK)


def _eh_dc_isolado(cel) -> bool:
    return (
        not cel["data"]
        and cel["valor"] is None
        and not cel["hist"]
        and not cel["detalhe"]
        and cel["dc"] in SUFIXOS
    )


def _eh_valor_orfao(cel) -> bool:
    return (
        not cel["data"]
        and cel["valor"] is not None
        and not cel["hist"]
        and not cel["detalhe"]
    )


def _memo(t) -> str:
    partes = [t.get("hist") or t.get("descricao") or ""]
    if t.get("nome"):
        partes.append(t["nome"].strip())
    return " ".join(p for p in partes if p).strip()


def parse(pdf_path) -> list:
    """Extrai as transações. Retorna lista de dicts:
    data, valor, descricao, doc, nome."""
    avisos.clear()
    cab = _ler_cabecalho(pdf_path, com_saldo=False)
    ano_base = cab["periodo_inicio"].year
    mes_base = cab["periodo_inicio"].month

    transacoes = []
    detalhe_ok = False
    valor_pendente = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for n_pag, pagina in enumerate(pdf.pages, 1):
            for linha in _agrupar_linhas(pagina.extract_words()):
                cel = _processar_linha(linha)
                if not cel:
                    continue

                texto_linha = " ".join(
                    p for p in (cel["hist"], cel["detalhe"]) if p
                ).strip()

                if not cel["data"] and texto_linha.upper() == FIM_TABELA:
                    break

                if _eh_valor_orfao(cel):
                    valor_pendente = (cel["valor"], cel["tem_dc"], cel["dc"])
                    continue

                if _eh_dc_isolado(cel):
                    if transacoes and not transacoes[-1].get("_tem_dc"):
                        transacoes[-1]["valor"] = _aplicar_dc(
                            transacoes[-1]["valor"], cel["dc"]
                        )
                        transacoes[-1]["_tem_dc"] = True
                    continue

                if cel["data"] and cel["hist"].upper().startswith("SALDO"):
                    valor_pendente = None
                    detalhe_ok = False
                    continue

                if cel["data"]:
                    valor = cel["valor"]
                    tem_dc = cel["tem_dc"]
                    dc = cel["dc"]

                    if valor is None and valor_pendente:
                        valor, tem_dc_p, dc_p = valor_pendente
                        tem_dc = tem_dc or tem_dc_p
                        dc = dc or dc_p
                        valor_pendente = None

                    if valor is not None and not tem_dc and dc:
                        valor = _aplicar_dc(valor, dc)
                        tem_dc = True

                    if valor is None:
                        if not _junk(cel["hist"]):
                            msg_aviso = (
                                f"pág {n_pag}: linha com data e sem valor "
                                f"reconhecido: {cel['data']} {cel['hist']!r}"
                            )
                            avisos.append(msg_aviso)
                            warnings.warn(msg_aviso)
                            data_obj = None
                            try:
                                data_obj = _data_com_ano(cel["data"], ano_base, mes_base)
                            except Exception:
                                pass
                            transacoes.append({
                                "data": data_obj,
                                "valor": None,
                                "hist": cel["hist"],
                                "descricao": cel["hist"],
                                "doc": None,
                                "nome": None,
                                "incompleto": True,
                                "motivo": "Valor não identificado",
                            })
                            detalhe_ok = True
                            valor_pendente = None
                            if cel["detalhe"]:
                                _processar_detalhe(transacoes, cel["detalhe"])
                            continue
                        detalhe_ok = False
                        continue

                    transacoes.append({
                        "data": _data_com_ano(cel["data"], ano_base, mes_base),
                        "valor": valor,
                        "hist": cel["hist"],
                        "descricao": cel["hist"],
                        "doc": None,
                        "nome": None,
                        "_tem_dc": tem_dc,
                    })
                    detalhe_ok = True
                    valor_pendente = None

                    if cel["detalhe"]:
                        _processar_detalhe(transacoes, cel["detalhe"])
                    continue

                if detalhe_ok and transacoes and texto_linha:
                    _processar_detalhe(transacoes, texto_linha)

    if valor_pendente and valor_pendente[0] is not None:
        transacoes.append({
            "data": None,
            "valor": valor_pendente[0],
            "hist": "Valor sem data/histórico",
            "descricao": "Valor sem data/histórico",
            "doc": None,
            "nome": None,
            "incompleto": True,
            "motivo": "Data e histórico não identificados",
        })

    for t in transacoes:
        t.pop("_tem_dc", None)
        if not t.get("incompleto"):
            t["descricao"] = _memo(t)
        elif not t.get("descricao"):
            t["descricao"] = _memo(t)

    return transacoes


def info(pdf_path) -> dict:
    return _ler_cabecalho(pdf_path)


def _ler_cabecalho(pdf_path, com_saldo=True) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])

    coop = RE_COOP.search(texto)
    conta = RE_CONTA.search(texto)
    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Período não encontrado no PDF — isto é um extrato Credibom/Sicoob (v2)?"
        )
    d1, d2 = (
        date(int(p[6:]), int(p[3:5]), int(p[:2])) for p in periodo.groups()
    )
    conta_txt = conta.group(1) if conta else ""
    return {
        "banco_nome": BANCO_ORG,
        "banco_id": BANCO_ID,
        "agencia": coop.group(1) if coop else "",
        "conta": conta_txt,
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": _saldo_final(pdf_path, d2) if com_saldo else None,
    }


def _saldo_final(pdf_path, dia_final):
    saldos = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            for linha in _agrupar_linhas(pagina.extract_words()):
                cel = _processar_linha(linha)
                if (
                    cel
                    and cel["valor"] is not None
                    and cel["hist"].upper() == "SALDO DO DIA"
                ):
                    saldos.append((cel["data"][:5], cel["valor"]))
    for dia, valor in saldos:
        if dia == dia_final.strftime("%d/%m"):
            return valor
    return saldos[0][1] if saldos else None


def _data_com_ano(texto: str, ano_base: int, mes_base: int) -> date:
    dia, mes = int(texto[:2]), int(texto[3:5])
    if len(texto) >= 10:
        return date(int(texto[6:10]), mes, dia)
    ano = ano_base + 1 if mes < mes_base else ano_base
    return date(ano, mes, dia)


if __name__ == "__main__":
    import sys
    from ofx_writer import gerar_ofx

    caminho = sys.argv[1]
    dados = info(caminho)
    print("=== Cabeçalho ===")
    for k, v in dados.items():
        print(f"{k}: {v}")
    print("\n=== Transações ===")
    txs = parse(caminho)
    for t in txs:
        print(t)
    print(f"\nTotal: {len(txs)} transações")
    if avisos:
        print(f"\n{len(avisos)} AVISO(S):")
        for a in avisos:
            print(" -", a)
    if len(sys.argv) >= 3:
        gerar_ofx(txs, dados, sys.argv[2])
        print(f"OFX: {sys.argv[2]}")