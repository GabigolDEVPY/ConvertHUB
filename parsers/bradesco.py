import re
from datetime import date, datetime
import pdfplumber

NOME = "Bradesco (extrato consolidado Net Empresa)"
BANCO_ID = "237"

MAPA_CONTA_ACCTID = {
    "0522110-2": "05221102",
}

RE_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_VALOR = re.compile(r"^-?\d[\d.]*,\d{2}$")
RE_DOC = re.compile(r"^\d+$")
RE_AG = re.compile(r"Ag:\s*(\d+)")
RE_CC = re.compile(r"CC:\s*([\d.\-]+\d)")
RE_ENTRE = re.compile(r"Entre\s+(\d{2}/\d{2}/\d{4})\s+e\s+(\d{2}/\d{2}/\d{4})")
RE_SALDO_FINAL = re.compile(r"Saldo\s+em\s+\d{2}/\d{2}/\d{4}\s+(-?\d[\d.]*,\d{2})")

X_DATA_MAX = 60
X_DOC = (265, 310)
X_CRED = (310, 430)
X_DEB = (430, 525)
X_SALDO = (525, 10_000)
X_DESC_MAX = 265

GAP_BLOCO = 7

JUNK = (
    " | ", "extrato consolidado", "nome do usuário", "data da operação",
    "folha ", "agência |", "extrato de:", "data lançamento", "saldo anterior",
    "últimos lançamentos", "saldo invest", "os dados acima", "saldos invest",
    "data histórico", "total ",
)

# Palavras-chave para identificar cheques e variações
CHEQUE_KEYWORDS = ["CHEQUE", "CHQ", "CHE", "COMPENSADO", "CHG", "CH "]

def _agrupar_linhas(words):
    linhas = []
    atual = []
    topo = None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo <= 2:
            atual.append(w)
        else:
            linhas.append(atual)
            atual = [w]
            topo = w["top"]
        if topo is None:
            topo = w["top"]
    if atual:
        linhas.append(atual)
    return linhas

def _classificar(linha):
    doc = valor = saldo = None
    data = None
    desc = []
    for w in linha:
        t = w["text"]
        if RE_DATA.match(t) and w["x0"] < X_DATA_MAX:
            data = date(int(t[6:]), int(t[3:5]), int(t[:2]))
            continue
        
        # Captura normal de DOC (dentro da coluna oficial)
        if RE_DOC.match(t) and X_DOC[0] <= w["x0"] and w["x1"] <= X_DOC[1]:
            doc = t
            continue
        
        if RE_VALOR.match(t):
            if X_CRED[0] < w["x1"] <= X_CRED[1]:
                valor = float(t.replace(".", "").replace(",", "."))
                continue
            if X_DEB[0] < w["x1"] <= X_DEB[1]:
                valor = float(t.replace(".", "").replace(",", "."))
                continue
            if w["x1"] > X_SALDO[0]:
                saldo = float(t.replace(".", "").replace(",", "."))
                continue
        
        # NOVO: Captura números soltos (como cheques ou Dctos empurrados) que o Bradesco jogou para fora da coluna X_DOC
        if RE_DOC.match(t) and w["x0"] < X_CRED[0]:
            desc.append(t)
            continue

        if w["x0"] < X_DESC_MAX:
            desc.append(t)
            
    return {
        "doc": doc,
        "valor": valor,
        "saldo": saldo,
        "data": data,
        "desc": " ".join(desc).strip(),
    }

def _junk(texto):
    t = texto.lower()
    return any(p in t for p in JUNK)

def parse(pdf_path) -> list:
    linhas = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pg, pagina in enumerate(pdf.pages):
            for words in _agrupar_linhas(pagina.extract_words()):
                texto = " ".join(w["text"] for w in words)
                if _junk(texto):
                    continue
                linhas.append((pg, words[0]["top"], _classificar(words)))

    ancoras = [i for i, c in enumerate(linhas)
               if c[2]["doc"] is not None and c[2]["valor"] is not None]
    if not ancoras:
        # Fallback: se não achar ancora com doc e valor, tenta só com valor
        ancoras = [i for i, c in enumerate(linhas) if c[2]["valor"] is not None]
        if not ancoras:
            raise ValueError("Nenhuma transação encontrada.")

    marcas = [(i, linhas[i][2]["data"]) for i, c in enumerate(linhas)
              if c[2]["data"] is not None]
    data_por_ancora = {}
    for a in ancoras:
        pg_a, top_a, _ = linhas[a]
        melhor, dist = None, 11
        for i, d in marcas:
            pg_i, top_i, _ = linhas[i]
            if pg_i != pg_a:
                continue
            if abs(top_i - top_a) < dist:
                melhor, dist = d, abs(top_i - top_a)
        data_por_ancora[a] = melhor

    partes = {a: {"antes": [], "depois": []} for a in ancoras}
    conjunto = set(ancoras)

    def _proxima(pg):
        return min((a for a in ancoras if linhas[a][0] > pg),
                   key=lambda a: (linhas[a][0], linhas[a][1]), default=None)

    def _anterior(pg):
        return max((a for a in ancoras if linhas[a][0] < pg),
                   key=lambda a: (linhas[a][0], linhas[a][1]), default=None)

    for i, (pg, top, cel) in enumerate(linhas):
        if not cel["desc"] or i in conjunto:
            continue
        acima = [a for a in ancoras if linhas[a][0] == pg and linhas[a][1] < top]
        abaixo = [a for a in ancoras if linhas[a][0] == pg and linhas[a][1] > top]

        if acima and abaixo:
            alvo = min(acima + abaixo, key=lambda a: abs(linhas[a][1] - top))
            lado = "antes" if linhas[alvo][1] > top else "depois"
            partes[alvo][lado].append(cel["desc"])
        elif abaixo:
            primeiro = min(abaixo, key=lambda a: linhas[a][1])
            prev = _anterior(pg)
            if prev is not None and linhas[primeiro][1] - top > GAP_BLOCO:
                partes[prev]["depois"].append(cel["desc"])
            else:
                partes[primeiro]["antes"].append(cel["desc"])
        elif acima:
            ultimo = max(acima, key=lambda a: linhas[a][1])
            prox = _proxima(pg)
            if top - linhas[ultimo][1] <= GAP_BLOCO or prox is None:
                partes[ultimo]["depois"].append(cel["desc"])
            else:
                partes[prox]["antes"].append(cel["desc"])
        else:
            prox = _proxima(pg)
            if prox is not None:
                partes[prox]["antes"].append(cel["desc"])

    transacoes = []
    data_atual = None
    for a in ancoras:
        _, _, cel = linhas[a]
        if data_por_ancora[a] is not None:
            data_atual = data_por_ancora[a]
        
        blocos = partes[a]["antes"] + ([cel["desc"]] if cel["desc"] else []) + partes[a]["depois"]
        blocos = [b.strip() for b in blocos if b.strip()]
        
        full_desc = " ".join(blocos)
        
        # =====================================================================
        # EXTRAÇÃO UNIVERSAL DE CHECKNUM / DOCUMENTO
        # =====================================================================
        numeros = re.findall(r'\b\d+\b', full_desc)
        checknum = cel.get("doc", "") or ""
        
        if not checknum and numeros:
            checknum = numeros[-1]
            idx = full_desc.rfind(checknum)
            if idx != -1:
                full_desc = (full_desc[:idx] + full_desc[idx+len(checknum):]).strip()
                full_desc = re.sub(r'\s+', ' ', full_desc).strip()
                
        is_cheque = any(kw in full_desc.upper() for kw in CHEQUE_KEYWORDS)
        if is_cheque and not checknum:
            match = re.search(r'(\d+)\s*$', " ".join(blocos))
            if match:
                checknum = match.group(1)

        blocos_limpos = [full_desc] if full_desc else []
        memo = blocos_limpos[0] if blocos_limpos else ""
        name = " ".join(blocos_limpos[1:]) if len(blocos_limpos) > 1 else ""
        
        # Nota: o número do documento (checknum) já é gravado nos campos
        # CHECKNUM e REFNUM pelo ofx_writer — não precisa incluir no memo.
        # O memo deve conter apenas a descrição textual da transação.

        item = {
            "data": data_atual,
            "valor": cel["valor"],
            "descricao": memo.upper(),
            "memo": memo.upper(),
            "name": name.upper(),
            "doc": checknum,
        }
        if data_atual is None:
            item["incompleto"] = True
            item["motivo"] = "Data não identificada"
        transacoes.append(item)

    # Captura linhas avulsas que tinham data e texto mas não geraram âncora (valor faltando)
    for idx_linha, l in enumerate(linhas):
        if l[2]["data"] is not None and l[2]["valor"] is None:
            desc_val = l[2]["desc"]
            if desc_val and not _junk(desc_val) and not desc_val.upper().startswith("SALDO"):
                ja_usada = any(desc_val in partes[a]["antes"] or desc_val in partes[a]["depois"] for a in ancoras)
                if not ja_usada:
                    transacoes.append({
                        "data": l[2]["data"],
                        "valor": None,
                        "descricao": desc_val.upper(),
                        "memo": desc_val.upper(),
                        "name": "",
                        "doc": l[2]["doc"] or "",
                        "incompleto": True,
                        "motivo": "Valor não identificado",
                    })

    return transacoes

def info(pdf_path) -> dict:
    with pdfplumber.open(str(pdf_path)) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)

    ag = RE_AG.search(texto)
    cc = RE_CC.search(texto)
    entre = RE_ENTRE.search(texto)
    saldo_match = RE_SALDO_FINAL.search(texto)
    
    conta = cc.group(1) if cc else ""
    d1 = d2 = None
    if entre:
        d1, d2 = (
            date(int(p[6:]), int(p[3:5]), int(p[:2])) for p in entre.groups()
        )
    
    acctid = MAPA_CONTA_ACCTID.get(conta)
    if acctid is None:
        acctid = re.sub(r"\D", "", conta).zfill(8)
        
    saldo_final = None
    if saldo_match:
        saldo_final = float(saldo_match.group(1).replace(".", "").replace(",", "."))

    return {
        "banco_nome": "Banco Bradesco S.A.",
        "banco_id": BANCO_ID,
        "agencia": ag.group(1) if ag else "",
        "conta": conta,
        "acctid": acctid,
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": saldo_final,
    }