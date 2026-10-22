import re
from datetime import date, datetime
import pdfplumber

NOME = "Caixa Econômica Federal (Gerenciador CAIXA)"
BANCO_ID = "104"

# Mapeamento opcional conta -> ACCTID, igual ao parser do Bradesco.
# Preencher conforme necessário (chave = número da conta como aparece no extrato).
MAPA_CONTA_ACCTID = {
    # "000577562847-7": "000577562847",
}

RE_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_HORA_CURTA = re.compile(r"^\d{2}/\d{2}$")  # ex.: "02/04" (data efetiva, sem ano)
RE_HORA = re.compile(r"^\d{2}:\d{2}$")
RE_VALOR = re.compile(r"^\d[\d.]*,\d{2}$")
RE_DOC = re.compile(r"^\d+$")

RE_AGENCIA_CONTA = re.compile(r"Agência:\s*(\d+)\s*Conta:\s*([\d.\-]+\d)")
RE_PERIODO = re.compile(
    r"Extrato\s+no\s+per[ií]odo\s+de\s+(\d{2}/\d{2}/\d{4})\s+[àa]\s+(\d{2}/\d{2}/\d{4})"
)
RE_SALDO_ANTERIOR = re.compile(
    r"Saldo\s+anterior\s+ao\s+per[ií]odo\s+solicitado\s+R\$\s*(-?\d[\d.]*,\d{2})\s*([CD]?)"
)
RE_SALDO_DIA = re.compile(r"R\$\s*(-?\d[\d.]*,\d{2})\s*([CD]?)\s*$")

X_DATA_MAX = 90       # coluna "Data" / "Data Efetiva" (dd/mm/aaaa ou dd/mm)
X_DOC = (120, 190)    # coluna "Documento"
X_VALOR = (560, 680)  # coluna "Valor" (inclui sinal "-" e "R$")
X_SALDO = (760, 10_000)  # coluna "Saldo" (inclui "R$" e "D"/"C")
X_DESC_MAX = 560      # tudo à esquerda da coluna Valor que não é data/doc vira descrição

GAP_BLOCO = 7

JUNK = (
    "gerenciador", "cnpj:", "agência:",
    "saldo anterior ao período", "extrato no período",
    "sac caixa", "ouvidoria", "pessoas com",
    "deficiência auditiva", "alô caixa", "0800 ",
    "data documento", "documento histórico", "data efetiva",
)

# Linha de cabeçalho com o nome do titular (primeira linha da página),
# filtrada por posição (topo da página) e não pelo texto, para não
# colidir com o mesmo nome aparecendo dentro de uma descrição de PIX.
Y_CABECALHO_NOME_MAX = 60

HISTORICO_IGNORAR = ("SALDO DIA",)

# Linhas de cabeçalho de coluna que podem sobrar isoladas (ex.: "Data" sozinho,
# por cima de "Documento Histórico", devido à quebra em duas linhas do rótulo
# "Data" / "Data Efetiva" da primeira coluna)
TEXTO_CABECALHO_COLUNA = {"DATA", "DATA EFETIVA", "DOCUMENTO HISTÓRICO", "VALOR", "SALDO"}


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


def _parse_valor(txt):
    return float(txt.replace(".", "").replace(",", "."))


def _classificar(linha):
    doc = valor = saldo = None
    data = None
    data_efetiva_curta = None
    hora = None
    desc = []

    sinal_valor = 1

    for w in linha:
        t = w["text"]

        # Data completa (dd/mm/aaaa) na coluna da esquerda -> "Data"
        if RE_DATA.match(t) and w["x0"] < X_DATA_MAX:
            data = date(int(t[6:]), int(t[3:5]), int(t[:2]))
            continue

        # Data efetiva curta (dd/mm), linha de baixo da célula "Data"
        if RE_HORA_CURTA.match(t) and w["x0"] < X_DATA_MAX:
            data_efetiva_curta = t
            continue

        # Horário (hh:mm) que acompanha a data efetiva
        if RE_HORA.match(t) and w["x0"] < X_DATA_MAX:
            hora = t
            continue

        # Documento
        if RE_DOC.match(t) and X_DOC[0] <= w["x0"] and w["x1"] <= X_DOC[1]:
            doc = t
            continue

        # Sinal negativo solto antes do valor (débito)
        if t == "-" and X_VALOR[0] <= w["x0"] < X_VALOR[1]:
            sinal_valor = -1
            continue

        # "R$" é apenas marcador de moeda, ignora
        if t == "R$":
            continue

        # Letra D/C solta junto ao saldo
        if t in ("D", "C") and w["x0"] >= X_SALDO[0]:
            # será tratado junto com o valor do saldo mais abaixo, mas
            # como pode vir em token separado, guarda aqui também
            continue

        if RE_VALOR.match(t):
            if X_VALOR[0] <= w["x0"] < X_VALOR[1]:
                valor = _parse_valor(t) * sinal_valor
                continue
            if w["x0"] >= X_SALDO[0]:
                saldo = _parse_valor(t)
                continue

        if w["x0"] < X_DESC_MAX:
            desc.append(t)

    # Procura o indicador D/C do saldo (vem como token separado "D" ou "C")
    saldo_sinal = None
    for w in linha:
        if w["text"] in ("D", "C") and w["x0"] >= X_SALDO[0]:
            saldo_sinal = w["text"]
    if saldo is not None and saldo_sinal == "D":
        saldo = -abs(saldo)

    return {
        "doc": doc,
        "valor": valor,
        "saldo": saldo,
        "data": data,
        "data_efetiva": data_efetiva_curta,
        "hora": hora,
        "desc": " ".join(desc).strip(),
    }


def _junk(texto):
    t = texto.lower()
    return any(p in t for p in JUNK)


def _is_saldo_dia(texto):
    t = texto.upper()
    return any(h in t for h in HISTORICO_IGNORAR)


def _is_cabecalho_coluna(texto):
    return texto.strip().upper() in TEXTO_CABECALHO_COLUNA


def parse(pdf_path) -> list:
    linhas = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pg, pagina in enumerate(pdf.pages):
            for words in _agrupar_linhas(pagina.extract_words()):
                texto = " ".join(w["text"] for w in words)
                if _junk(texto):
                    continue
                if _is_cabecalho_coluna(texto):
                    continue
                # Ignora a linha do nome do titular no topo da página
                # (identificada pela posição, não pelo conteúdo textual)
                if words[0]["top"] < Y_CABECALHO_NOME_MAX and not RE_DATA.match(words[0]["text"]):
                    continue
                linhas.append((pg, words[0]["top"], _classificar(words)))

    # Âncoras: linhas com Documento OU Valor preenchidos, que não sejam "SALDO DIA"
    ancoras = [
        i for i, c in enumerate(linhas)
        if (c[2]["doc"] is not None or c[2]["valor"] is not None)
        and not _is_saldo_dia(c[2]["desc"])
    ]
    if not ancoras:
        ancoras = [i for i, c in enumerate(linhas) if c[2]["valor"] is not None]
        if not ancoras:
            raise ValueError("Nenhuma transação encontrada.")

    # Datas "oficiais" (coluna Data, dd/mm/aaaa) para preencher âncoras sem data própria
    marcas = [(i, linhas[i][2]["data"]) for i, c in enumerate(linhas)
              if c[2]["data"] is not None]

    data_por_ancora = {}
    for a in ancoras:
        pg_a, top_a, cel_a = linhas[a]
        if cel_a["data"] is not None:
            data_por_ancora[a] = cel_a["data"]
            continue
        melhor, dist = None, 11
        for i, d in marcas:
            pg_i, top_i, _ = linhas[i]
            if pg_i != pg_a:
                continue
            if abs(top_i - top_a) < dist:
                melhor, dist = d, abs(top_i - top_a)
        data_por_ancora[a] = melhor

    # Agrega texto de descrição que ficou em linhas auxiliares (ex.: linha de
    # complemento do PIX, que vem em linha própria entre o histórico e a âncora)
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
        if _is_saldo_dia(cel["desc"]):
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
        full_desc = re.sub(r"\s+", " ", full_desc).strip()

        checknum = cel.get("doc", "") or ""
        # "000000" é usado pela Caixa como documento genérico (tarifas/juros/IOF);
        # mantém o valor mas ele não serve como identificador único de transação.

        memo = full_desc
        name = ""

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
        if cel["valor"] is None:
            item["incompleto"] = True
            item["motivo"] = (item.get("motivo", "") + " Valor não identificado").strip()
        transacoes.append(item)

    # Linhas avulsas com data mas sem valor (ex.: "SALDO DIA" já é ignorado acima;
    # isto cobre qualquer outra linha de data sem transação associada)
    for idx_linha, l in enumerate(linhas):
        if l[2]["data"] is not None and l[2]["valor"] is None and l[2]["doc"] is None:
            desc_val = l[2]["desc"]
            if desc_val and not _junk(desc_val) and not _is_saldo_dia(desc_val):
                ja_usada = any(
                    desc_val in partes[a]["antes"] or desc_val in partes[a]["depois"]
                    for a in ancoras
                )
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

    ag_cc = RE_AGENCIA_CONTA.search(texto)
    periodo = RE_PERIODO.search(texto)
    saldo_ant = RE_SALDO_ANTERIOR.search(texto)

    agencia = ag_cc.group(1) if ag_cc else ""
    conta = ag_cc.group(2) if ag_cc else ""

    d1 = d2 = None
    if periodo:
        d1, d2 = (
            date(int(p[6:]), int(p[3:5]), int(p[:2])) for p in periodo.groups()
        )

    acctid = MAPA_CONTA_ACCTID.get(conta)
    if acctid is None:
        acctid = re.sub(r"\D", "", conta).zfill(8)

    saldo_inicial = None
    if saldo_ant:
        valor = _parse_valor(saldo_ant.group(1))
        sinal = saldo_ant.group(2)
        saldo_inicial = -abs(valor) if sinal == "D" else valor

    # Saldo final = saldo da última linha "SALDO DIA" (ou última linha com saldo) do extrato
    saldo_final = None
    for linha_txt in reversed(texto.splitlines()):
        m = RE_SALDO_DIA.search(linha_txt)
        if m and ("SALDO DIA" in linha_txt.upper() or RE_DATA.match(linha_txt.strip()[:10] or "")):
            valor = _parse_valor(m.group(1))
            sinal = m.group(2)
            saldo_final = -abs(valor) if sinal == "D" else valor
            break

    return {
        "banco_nome": "Caixa Econômica Federal",
        "banco_id": BANCO_ID,
        "agencia": agencia,
        "conta": conta,
        "acctid": acctid,
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_inicial": saldo_inicial,
        "saldo_final": saldo_final,
    }