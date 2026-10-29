import re
from datetime import date
import pdfplumber

NOME = "Mercado Pago (extrato de conta)"
BANCO_ID = "323"  # código COMPE do Mercado Pago

# Opcional: mapeamento conta -> ACCTID para casos em que o ID do OFX
# precisa ser diferente do número da conta. Se a conta não estiver aqui,
# usa o número da conta (somente dígitos). Funciona para qualquer conta.
MAPA_CONTA_ACCTID = {}

RE_DATA = re.compile(r"^\d{2}-\d{2}-\d{4}$")
RE_VALOR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$|^-?\d+,\d{2}$")
RE_DOC = re.compile(r"^\d{6,}$")
RE_AG = re.compile(r"Ag[eê]ncia:\s*(\d+)")
RE_CC = re.compile(r"Conta:\s*(\d+)")
RE_CNPJ = re.compile(r"CPF/CNPJ:\s*(\d+)")
RE_PERIODO = re.compile(r"De\s+(\d{2}-\d{2}-\d{4})\s+al\s+(\d{2}-\d{2}-\d{4})")
_NUM = r"-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+,\d{2}"
RE_SALDO_FINAL = re.compile(r"Saldo\s+final:\s*R\$\s*(" + _NUM + ")")
RE_SALDO_INICIAL = re.compile(r"Saldo\s+inicial:\s*R\$\s*(" + _NUM + ")")

# Fallbacks caso o cabeçalho da tabela não seja encontrado na página
X_DESC_FALLBACK = 85
X_ID_FALLBACK = 190
# Distância vertical máxima (pt) entre uma linha de descrição e a linha da data
MAX_DIST_DESC = 14


def _d(txt):
    """'01-08-2026' -> date"""
    return date(int(txt[6:]), int(txt[3:5]), int(txt[:2]))


def _f(txt):
    return float(txt.replace(".", "").replace(",", "."))


def _colunas(words):
    """Descobre X da coluna Descrição / ID e topo do cabeçalho na página."""
    x_desc, x_id, top_header = X_DESC_FALLBACK, X_ID_FALLBACK, None
    for w in words:
        if w["text"] == "Descrição":
            x_desc, top_header = w["x0"], w["top"]
        elif w["text"] == "ID" and top_header is not None \
                and abs(w["top"] - top_header) < 3:
            x_id = w["x0"]
    return x_desc, x_id, top_header


def parse(pdf_path) -> list:
    transacoes = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            words = pagina.extract_words()
            x_desc, x_id, top_header = _colunas(words)
            if top_header is None:
                continue  # página sem tabela de movimentos

            words = [w for w in words if w["top"] > top_header + 3]

            # Cada linha da tabela começa com uma data na primeira coluna
            # (exige um ID de operação na mesma linha, para ignorar datas
            # soltas como "Data de geração" no rodapé)
            ancoras = [w for w in words
                       if RE_DATA.match(w["text"]) and w["x0"] < x_desc - 5
                       and any(RE_DOC.match(o["text"]) and o["x0"] >= x_id - 2
                               and abs(o["top"] - w["top"]) < 3
                               for o in words)]
            if not ancoras:
                continue

            linhas = [{"data": a["text"], "top": a["top"],
                       "desc": [], "valores": [], "doc": ""}
                      for a in ancoras]

            for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
                if w in ancoras:
                    continue
                # linha da tabela mais próxima verticalmente
                alvo = min(linhas, key=lambda l: abs(l["top"] - w["top"]))
                if abs(alvo["top"] - w["top"]) > MAX_DIST_DESC:
                    continue  # rodapé / texto fora da tabela

                t = w["text"]
                if w["x0"] >= x_id - 2:
                    # colunas ID / Valor / Saldo
                    if RE_DOC.match(t) and w["x0"] < x_id + 60 and not alvo["doc"]:
                        alvo["doc"] = t
                    elif RE_VALOR.match(t):
                        alvo["valores"].append((w["x0"], _f(t)))
                elif w["x0"] >= x_desc - 3:
                    alvo["desc"].append((w["top"], w["x0"], t))

            for l in linhas:
                desc = " ".join(
                    t for _, _, t in sorted(l["desc"], key=lambda x: (round(x[0]), x[1]))
                ).strip()
                desc = re.sub(r"\s+", " ", desc)
                vals = [v for _, v in sorted(l["valores"])]

                item = {
                    "data": _d(l["data"]),
                    "valor": vals[0] if vals else None,
                    "saldo": vals[1] if len(vals) > 1 else None,
                    "descricao": desc.upper(),
                    "memo": desc.upper(),
                    "name": "",
                    "doc": l["doc"],
                }
                if not vals:
                    item["incompleto"] = True
                    item["motivo"] = "Valor não identificado"
                transacoes.append(item)

    if not transacoes:
        raise ValueError("Nenhuma transação encontrada.")
    return transacoes


def info(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)

    ag = RE_AG.search(texto)
    cc = RE_CC.search(texto)
    cnpj = RE_CNPJ.search(texto)
    per = RE_PERIODO.search(texto)
    sf = RE_SALDO_FINAL.search(texto)
    si = RE_SALDO_INICIAL.search(texto)

    conta = cc.group(1) if cc else ""
    acctid = MAPA_CONTA_ACCTID.get(conta) or re.sub(r"\D", "", conta)

    return {
        "banco_nome": "Mercado Pago Instituição de Pagamento Ltda.",
        "banco_id": BANCO_ID,
        "agencia": ag.group(1) if ag else "",
        "conta": conta,
        "acctid": acctid,
        "documento": cnpj.group(1) if cnpj else "",
        "periodo_inicio": _d(per.group(1)) if per else None,
        "periodo_fim": _d(per.group(2)) if per else None,
        "saldo_inicial": _f(si.group(1)) if si else None,
        "saldo_final": _f(sf.group(1)) if sf else None,
    }