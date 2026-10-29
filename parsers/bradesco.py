"""
Parser de extratos bancários em PDF (pdfplumber) -> lista de transações.

Foi escrito para o extrato consolidado do Bradesco Net Empresa, mas NÃO depende
de coordenadas fixas nem de um período/conta específicos:

  * Colunas (descrição, documento, crédito, débito, saldo) são detectadas pela
    linha de cabeçalho da tabela ("Data  Lançamento  Dcto.  Crédito  Débito  Saldo")
    e mantidas nas páginas seguintes. Se o cabeçalho não for achado, usa um
    layout padrão (LAYOUT_PADRAO).
  * O sinal da transação vem da COLUNA (débito => negativo), não do "-" impresso.
  * O saldo impresso em cada linha é usado para conferir (e, se possível,
    corrigir o sinal de) cada lançamento.
  * O extrato do mês termina na linha "Total" (ou em marcadores como
    "Últimos Lançamentos"); nada depois disso é lido. Cada seção tem o seu
    período ("Entre dd/mm/aaaa e dd/mm/aaaa" / "Período: ... a ...") e
    lançamentos fora dele são descartados.
  * Tolerâncias verticais (agrupamento de descrição em 2 linhas, ligação da
    data ao lançamento) são calibradas pelo espaçamento real das linhas.

API (compatível com a versão anterior):
    parse(pdf_path) -> list[dict]
    info(pdf_path)  -> dict
Extra:
    conferir(pdf_path) -> dict   (confere soma de créditos/débitos e saldos)
"""
import re
import unicodedata
from collections import defaultdict
from datetime import date

import pdfplumber

NOME = "Bradesco (extrato consolidado Net Empresa)"
BANCO_ID = "237"                      # usado quando o banco não é reconhecido
BANCO_NOME = "Banco Bradesco S.A."

# conta (como aparece no extrato) -> ACCTID customizado
MAPA_CONTA_ACCTID = {
    "0522110-2": "05221102",
}

# Reconhecimento do banco pelo cabeçalho (primeiras linhas da 1ª página)
BANCOS_CONHECIDOS = [
    (r"bradesco", "237", "Banco Bradesco S.A."),
    (r"ita[uú]", "341", "Itaú Unibanco S.A."),
    (r"santander", "033", "Banco Santander (Brasil) S.A."),
    (r"banco do brasil", "001", "Banco do Brasil S.A."),
    (r"caixa econ[oô]mica", "104", "Caixa Econômica Federal"),
    (r"sicoob", "756", "Sicoob"),
    (r"sicredi", "748", "Sicredi"),
    (r"banco inter", "077", "Banco Inter S.A."),
    (r"nubank|nu pagamentos", "260", "Nu Pagamentos S.A."),
]

# Se o PDF não trouxer o nº do documento na coluna própria, tenta usar o último
# número (>= 3 dígitos) da descrição como documento/checknum.
EXTRAIR_DOC_DA_DESCRICAO = True

# --------------------------------------------------------------------------
# Regex / constantes
# --------------------------------------------------------------------------
_D = r"\d{2}/\d{2}/\d{2,4}"
RE_DATA = re.compile(r"^\d{2}/\d{2}(?:/\d{2,4})?$")
RE_VALOR = re.compile(r"^\(?-?\d[\d.]*,\d{2}-?\)?[CD]?$")
RE_DOC = re.compile(r"^\d+$")
RE_DATA_INICIO = re.compile(r"^\d{2}/\d{2}/\d{2,4}\s+")

RE_AG = re.compile(r"\bAg(?:[eê]ncia)?\s*[:.]?\s*(\d+)", re.I)
RE_CC = re.compile(r"\b(?:CC|Conta(?:\s+Corrente)?)\s*[:.]?\s*([\d.\-]*\d)", re.I)

RE_PERIODO_EXPL = [
    re.compile(rf"Entre\s+({_D})\s+e\s+({_D})", re.I),
    re.compile(rf"Per[ií]odo[^\d]{{0,25}}({_D})\s*(?:a|à|até|ate|-|–|e)\s*({_D})", re.I),
]
RE_PERIODO_FALLBACK = re.compile(rf"\b({_D})\s+(?:a|à|até|ate)\s+({_D})", re.I)
RE_SALDO_FINAL_TXT = re.compile(r"Saldo\s+em\s+\d{2}/\d{2}/\d{2,4}\s+(-?\d[\d.]*,\d{2})", re.I)

# Linhas de cabeçalho/rodapé (aplicadas ao texto normalizado, sem data inicial).
JUNK_REGEX = [re.compile(p) for p in (
    r"^folha\s+\d+\s*/\s*\d+",
    r"^pagina\s+\d+",
    r"^extrato\b",
    r"^nome do usuario",
    r"^data da operacao",
    r"^agencia\s*\|",
    r"^data\s+(lancamento|historico|descricao)",
    r"^saldo\s+anterior",
    r"^saldo\s+(do dia|parcial|final|em\s+\d)",
    r"\|\s*cnpj",
    r"^\d{3,6}\s*\|\s*[\d.\-]+",
    r"^(os )?dados acima",
)]

# Marcadores que encerram a leitura do extrato do período.
FIM_REGEX = [re.compile(p) for p in (
    r"^ultimos lancamentos",
    r"^saldos?\s+invest",
    r"^lancamentos futuros",
    r"^(os )?dados acima",
)]

# Layout padrão (pontos do PDF do Bradesco) usado se o cabeçalho não for achado.
LAYOUT_PADRAO = {
    "data_max": 60,
    "desc_max": 265,
    "doc": (265, 310),
    "esq_valores": 310,
    "cols": None,  # None => usa 'faixas' por x1
    "faixas": {"cred": (310, 430), "deb": (430, 525), "saldo": (525, 100_000)},
}

GAP_PADRAO = 7.0
TOL_DATA_PADRAO = 11.0


# --------------------------------------------------------------------------
# Utilitários
# --------------------------------------------------------------------------
def _norm(s):
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def _valor(t):
    """'1.234,56' / '-1.234,56' / '1.234,56-' / '(1.234,56)' / '1.234,56D' -> float."""
    s = t.strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("D"):
        neg, s = True, s[:-1]
    elif s.endswith("C"):
        s = s[:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    v = float(s.replace(".", "").replace(",", "."))
    return -v if neg else v


def _data(txt, periodo=None):
    """dd/mm/aaaa, dd/mm/aa ou dd/mm (ano deduzido do período) -> date | None."""
    p = txt.split("/")
    try:
        d, m = int(p[0]), int(p[1])
        if len(p) == 3:
            a = int(p[2])
            return date(a + 2000 if a < 100 else a, m, d)
        if not periodo:
            return None
        ini, fim = periodo
        for ano in (fim.year, ini.year):
            try:
                cand = date(ano, m, d)
            except ValueError:
                continue
            if ini <= cand <= fim:
                return cand
        return date(fim.year, m, d)
    except (ValueError, IndexError):
        return None


def _periodo_linha(texto):
    for rx in RE_PERIODO_EXPL:
        m = rx.search(texto)
        if m:
            d1, d2 = _data(m.group(1)), _data(m.group(2))
            if d1 and d2 and d1 <= d2:
                return (d1, d2)
    return None


def _sem_data_inicial(texto):
    return RE_DATA_INICIO.sub("", texto)


def _eh_junk(texto):
    t = _norm(_sem_data_inicial(texto)).strip()
    return any(rx.search(t) for rx in JUNK_REGEX)


def _eh_fim(texto):
    t = _norm(_sem_data_inicial(texto)).strip()
    return any(rx.search(t) for rx in FIM_REGEX)


def _eh_total(words):
    """Linha 'Total  <créditos>  <débitos>  <saldo>' (1ª palavra = Total + 3 valores)."""
    if not words or _norm(words[0]["text"]) != "total":
        return False
    return sum(1 for w in words[1:] if RE_VALOR.match(w["text"])) >= 3


def _totais(words, L):
    """Linha 'Total': devolve (créditos, débitos(negativo), saldo) pelas COLUNAS do layout."""
    vals = [w for w in words[1:] if RE_VALOR.match(w["text"])]
    por_col = {}
    for w in vals:
        col = _coluna_valor(w, L)
        if col and col not in por_col:
            por_col[col] = _valor(w["text"])
    if "saldo" in por_col and "cred" in por_col and "deb" in por_col:
        return (por_col["cred"], -abs(por_col["deb"]), por_col["saldo"])
    # sem colunas reconhecidas: assume a ordem crédito, débito, saldo
    nums = [_valor(w["text"]) for w in vals][:3]
    return tuple(nums)


def _agrupar_linhas(words):
    grupos, atual, topo = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or w["top"] - topo > 2:
            if atual:
                grupos.append(sorted(atual, key=lambda x: x["x0"]))
            atual, topo = [w], w["top"]
        else:
            atual.append(w)
    if atual:
        grupos.append(sorted(atual, key=lambda x: x["x0"]))
    return grupos


# --------------------------------------------------------------------------
# Detecção de colunas pelo cabeçalho da tabela
# --------------------------------------------------------------------------
def _detectar_layout(words):
    if any(RE_VALOR.match(w["text"]) for w in words):
        return None  # cabeçalho não tem valores monetários
    toks = [(_norm(w["text"]), w) for w in words]

    def achar(*prefixos):
        for n, w in toks:
            if any(n.startswith(p) for p in prefixos):
                return w
        return None

    cred, deb = achar("credito"), achar("debito")
    saldo, valor = achar("saldo"), achar("valor")
    if not saldo or not ((cred and deb) or valor):
        return None

    def cx(w):
        return (w["x0"] + w["x1"]) / 2

    lanc = achar("lancamento", "historico", "descricao")
    dcto = achar("dcto", "docto", "documento")
    data_h = achar("data")

    cols = {"saldo": cx(saldo)}
    if cred and deb:
        cols["cred"], cols["deb"] = cx(cred), cx(deb)
    else:
        cols["valor"] = cx(valor)
    primeira = min(c for k, c in cols.items() if k != "saldo")

    if dcto:
        desc_max = dcto["x0"] - 8
        doc = (desc_max, (cx(dcto) + primeira) / 2)
        esq_valores = doc[1]
    else:
        desc_max = primeira - 40
        doc = None
        esq_valores = desc_max

    if lanc:
        data_max = lanc["x0"] - 2
    elif data_h:
        data_max = data_h["x0"] + 60
    else:
        data_max = LAYOUT_PADRAO["data_max"]

    return {
        "data_max": data_max,
        "desc_max": desc_max,
        "doc": doc,
        "esq_valores": esq_valores,
        "cols": cols,
        "faixas": None,
    }


def _coluna_valor(w, L):
    if L["cols"] is None:
        for nome, (a, b) in L["faixas"].items():
            if a < w["x1"] <= b:
                return nome
        return None
    if w["x0"] < L["esq_valores"] - 5:
        return None
    c = (w["x0"] + w["x1"]) / 2
    return min(L["cols"].items(), key=lambda kv: abs(c - kv[1]))[0]


def _classificar(linha, L):
    doc = valor = saldo = col_valor = data_txt = None
    ultimo_valor = None
    desc = []
    for w in linha:
        t = w["text"]
        if RE_DATA.match(t) and w["x0"] < L["data_max"]:
            data_txt = t
            continue
        if (RE_DOC.match(t) and L["doc"]
                and L["doc"][0] <= w["x0"] and w["x1"] <= L["doc"][1]):
            doc = t
            continue
        if RE_VALOR.match(t):
            col = _coluna_valor(w, L)
            if col:
                v = _valor(t)
                ultimo_valor = v
                if col == "saldo":
                    saldo = v
                else:
                    # sinal pela coluna: débito => negativo; crédito/valor => como impresso
                    valor = -abs(v) if col == "deb" else v
                    col_valor = col
                continue
        if RE_DOC.match(t) and w["x0"] < L["esq_valores"]:
            desc.append(t)
            continue
        if w["x0"] < L["desc_max"]:
            desc.append(t)
    return {
        "doc": doc, "valor": valor, "col": col_valor, "saldo": saldo,
        "ultimo_valor": ultimo_valor, "data_txt": data_txt,
        "desc": " ".join(desc).strip(),
    }


# --------------------------------------------------------------------------
# Leitura do PDF
# --------------------------------------------------------------------------
def _nova_secao(periodo=None):
    return {"periodo": periodo, "saldo_anterior": None, "total": None,
            "saldo_final_txt": None, "n": 0, "fechada": False}


def _detectar_banco(pdf):
    try:
        cab = " ".join((pdf.pages[0].extract_text() or "").splitlines()[:12]).lower()
    except Exception:
        cab = ""
    for padrao, bid, nome in BANCOS_CONHECIDOS:
        if re.search(padrao, cab):
            return bid, nome
    return BANCO_ID, BANCO_NOME


def _ler(pdf_path):
    """Lê o PDF e devolve (linhas, secoes, texto_pagina1, (banco_id, banco_nome))."""
    linhas = []
    secoes = [_nova_secao()]
    layout = LAYOUT_PADRAO
    ativo = True
    tabela = False        # já passou pelo cabeçalho da tabela nesta seção?
    header_visto = False  # algum cabeçalho de tabela foi achado no documento?
    pendentes = []        # linhas lidas antes do cabeçalho (preâmbulo do banco)

    with pdfplumber.open(str(pdf_path)) as pdf:
        texto1 = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
        banco = _detectar_banco(pdf)

        for pg, pagina in enumerate(pdf.pages):
            for words in _agrupar_linhas(pagina.extract_words()):
                texto = " ".join(w["text"] for w in words)
                sec = secoes[-1]

                # início/continuação de uma seção com período explícito
                per = _periodo_linha(texto)
                if per:
                    if sec["periodo"] is None:
                        sec["periodo"] = per
                        ativo = True
                    elif sec["periodo"] == per:
                        if not sec["fechada"]:
                            ativo = True
                    else:
                        secoes.append(_nova_secao(per))
                        ativo, tabela = True, False
                    continue

                # depois do fim do extrato do período, ignora tudo (ex.: lançamentos
                # do mês seguinte e o 'Total' deles) até começar nova seção
                if not ativo:
                    continue

                # fim do extrato do período
                if _eh_total(words):
                    sec["total"] = _totais(words, layout)
                    sec["fechada"], ativo = True, False
                    continue
                if _eh_fim(texto):
                    sec["fechada"], ativo = True, False
                    continue

                # cabeçalho da tabela => (re)detecta colunas
                lay = _detectar_layout(words)
                if lay:
                    layout = lay
                    tabela = header_visto = True
                    pendentes.clear()
                    continue

                cel = _classificar(words, layout)

                # SALDO ANTERIOR (guarda e não vira lançamento)
                if _norm(_sem_data_inicial(texto)).startswith("saldo anterior"):
                    v = cel["saldo"] if cel["saldo"] is not None else cel["ultimo_valor"]
                    if v is not None:
                        sec["saldo_anterior"] = v
                    continue

                m = RE_SALDO_FINAL_TXT.search(texto)
                if m:
                    sec["saldo_final_txt"] = _valor(m.group(1))

                if _eh_junk(texto):
                    continue

                reg = {
                    "pg": pg,
                    "top": min(w["top"] for w in words),
                    "cel": cel,
                    "sec": len(secoes) - 1,
                }
                if tabela:
                    linhas.append(reg)
                    sec["n"] += 1
                else:
                    pendentes.append(reg)

    # PDF sem nenhum cabeçalho de tabela reconhecível: usa o layout padrão
    # e aceita tudo que foi lido
    if not header_visto:
        linhas = pendentes

    # período não explícito: tenta "dd/mm/aaaa a dd/mm/aaaa" no cabeçalho
    if secoes[0]["periodo"] is None:
        m = RE_PERIODO_FALLBACK.search(texto1)
        if m:
            d1, d2 = _data(m.group(1)), _data(m.group(2))
            if d1 and d2 and d1 <= d2:
                secoes[0]["periodo"] = (d1, d2)

    return linhas, secoes, texto1, banco


# --------------------------------------------------------------------------
# Montagem das transações
# --------------------------------------------------------------------------
def _calibrar(linhas):
    """Estima (gap_bloco, tolerância_data) pelo espaçamento real das linhas."""
    por_pg = defaultdict(list)
    for l in linhas:
        por_pg[l["pg"]].append(l["top"])
    difs = []
    for tops in por_pg.values():
        tops.sort()
        difs += [b - a for a, b in zip(tops, tops[1:]) if 0.5 < b - a <= 30]
    if len(difs) < 10:
        return GAP_PADRAO, TOL_DATA_PADRAO
    difs.sort()
    q25 = difs[len(difs) // 4]
    return max(GAP_PADRAO, 1.5 * q25), max(TOL_DATA_PADRAO, 2.4 * q25)


def _montar(linhas, secoes):
    ancoras = [i for i, l in enumerate(linhas) if l["cel"]["valor"] is not None]
    if not ancoras:
        raise ValueError("Nenhuma transação encontrada.")
    conjunto = set(ancoras)
    gap, tol_data = _calibrar(linhas)

    por_pg = defaultdict(list)
    for a in ancoras:
        por_pg[linhas[a]["pg"]].append(a)
    pgs_com = sorted(por_pg)

    def _proxima(pg):
        for p in pgs_com:
            if p > pg:
                return por_pg[p][0]
        return None

    def _anterior(pg):
        for p in reversed(pgs_com):
            if p < pg:
                return por_pg[p][-1]
        return None

    # --- data de cada lançamento: cada data impressa vai para o lançamento mais próximo
    data_ancora = {}
    for i, l in enumerate(linhas):
        txt = l["cel"]["data_txt"]
        if not txt:
            continue
        d = _data(txt, secoes[l["sec"]]["periodo"])
        if d is None:
            continue
        cand = [a for a in por_pg.get(l["pg"], [])
                if linhas[a]["sec"] == l["sec"] and abs(linhas[a]["top"] - l["top"]) <= tol_data]
        if cand:
            alvo = min(cand, key=lambda a: abs(linhas[a]["top"] - l["top"]))
        else:
            prox = [a for a in ancoras if a > i and linhas[a]["sec"] == l["sec"]]
            if not prox:
                continue
            alvo = prox[0]
        data_ancora.setdefault(alvo, d)

    # --- descrição em várias linhas (acima/abaixo da linha do valor)
    partes = {a: {"antes": [], "depois": []} for a in ancoras}
    for i, l in enumerate(linhas):
        cel = l["cel"]
        if not cel["desc"] or i in conjunto:
            continue
        pg, top = l["pg"], l["top"]
        anc = por_pg.get(pg, [])
        acima = [a for a in anc if linhas[a]["top"] < top]
        abaixo = [a for a in anc if linhas[a]["top"] > top]

        if acima and abaixo:
            alvo = min(acima + abaixo, key=lambda a: abs(linhas[a]["top"] - top))
            lado = "antes" if linhas[alvo]["top"] > top else "depois"
            partes[alvo][lado].append(cel["desc"])
        elif abaixo:
            primeiro = min(abaixo, key=lambda a: linhas[a]["top"])
            prev = _anterior(pg)
            if prev is not None and linhas[primeiro]["top"] - top > gap:
                partes[prev]["depois"].append(cel["desc"])
            else:
                partes[primeiro]["antes"].append(cel["desc"])
        elif acima:
            ultimo = max(acima, key=lambda a: linhas[a]["top"])
            prox = _proxima(pg)
            if top - linhas[ultimo]["top"] <= gap or prox is None:
                partes[ultimo]["depois"].append(cel["desc"])
            else:
                partes[prox]["antes"].append(cel["desc"])
        else:
            prox = _proxima(pg)
            if prox is not None:
                partes[prox]["antes"].append(cel["desc"])

    # --- transações
    transacoes = []
    data_atual = {}
    prev_saldo = {}
    for a in ancoras:
        l = linhas[a]
        cel = l["cel"]
        sec = l["sec"]
        if a in data_ancora:
            data_atual[sec] = data_ancora[a]
        data = data_atual.get(sec)

        blocos = (partes[a]["antes"]
                  + ([cel["desc"]] if cel["desc"] else [])
                  + partes[a]["depois"])
        full_desc = re.sub(r"\s+", " ", " ".join(b.strip() for b in blocos if b.strip())).strip()

        checknum = cel["doc"] or ""
        if not checknum and EXTRAIR_DOC_DA_DESCRICAO:
            m = re.search(r"(?:^|\s)(\d{3,})\s*$", full_desc)
            if m:
                checknum = m.group(1)
                full_desc = full_desc[:m.start()].strip()

        # conferência/correção pelo saldo impresso
        valor, s, aviso = cel["valor"], cel["saldo"], None
        prev = prev_saldo.get(sec, secoes[sec]["saldo_anterior"])
        if s is not None:
            if prev is not None and abs(prev + valor - s) > 0.011:
                if abs(prev - valor - s) <= 0.011:
                    valor, aviso = -valor, "sinal corrigido pelo saldo"
                else:
                    aviso = (f"saldo não confere (esperado {prev + valor:.2f}, "
                             f"extrato {s:.2f})")
            prev_saldo[sec] = s
        elif prev is not None:
            prev_saldo[sec] = round(prev + valor, 2)

        item = {
            "data": data,
            "valor": valor,
            "descricao": full_desc.upper(),
            "memo": full_desc.upper(),
            "name": "",
            "doc": checknum,
            "saldo_apos": s,
            "_sec": sec,
        }
        if aviso:
            item["aviso"] = aviso
        if data is None:
            item["incompleto"] = True
            item["motivo"] = "Data não identificada"
        transacoes.append(item)

    # --- linhas com data e texto mas sem valor (provável lançamento quebrado)
    usadas = {d for a in ancoras for d in partes[a]["antes"] + partes[a]["depois"]}
    for i, l in enumerate(linhas):
        cel = l["cel"]
        if cel["data_txt"] and cel["valor"] is None and cel["desc"]:
            desc_val = cel["desc"]
            if desc_val in usadas or _norm(desc_val).startswith("saldo"):
                continue
            transacoes.append({
                "data": _data(cel["data_txt"], secoes[l["sec"]]["periodo"]),
                "valor": None,
                "descricao": desc_val.upper(),
                "memo": desc_val.upper(),
                "name": "",
                "doc": cel["doc"] or "",
                "saldo_apos": None,
                "incompleto": True,
                "motivo": "Valor não identificado",
                "_sec": l["sec"],
            })
    return transacoes


def _dentro_do_periodo(t, secoes):
    per = secoes[t["_sec"]]["periodo"]
    return not per or t["data"] is None or per[0] <= t["data"] <= per[1]


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------
def parse(pdf_path) -> list:
    linhas, secoes, _, _ = _ler(pdf_path)
    saida = []
    for t in _montar(linhas, secoes):
        if _dentro_do_periodo(t, secoes):
            t.pop("_sec")
            saida.append(t)
    return saida


def info(pdf_path) -> dict:
    linhas, secoes, texto1, (banco_id, banco_nome) = _ler(pdf_path)

    ag = RE_AG.search(texto1)
    cc = RE_CC.search(texto1)
    conta = cc.group(1) if cc else ""
    sec0 = secoes[0]
    d1, d2 = sec0["periodo"] if sec0["periodo"] else (None, None)

    acctid = MAPA_CONTA_ACCTID.get(conta)
    if acctid is None:
        acctid = re.sub(r"\D", "", conta).zfill(8)

    saldo_final = None
    if sec0["total"] and len(sec0["total"]) == 3:
        saldo_final = sec0["total"][2]
    elif sec0["saldo_final_txt"] is not None:
        saldo_final = sec0["saldo_final_txt"]
    else:
        for l in reversed(linhas):
            if l["sec"] == 0 and l["cel"]["saldo"] is not None:
                saldo_final = l["cel"]["saldo"]
                break

    return {
        "banco_nome": banco_nome,
        "banco_id": banco_id,
        "agencia": ag.group(1) if ag else "",
        "conta": conta,
        "acctid": acctid,
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": saldo_final,
        "total_creditos": sec0["total"][0] if sec0["total"] else None,
        "total_debitos": sec0["total"][1] if sec0["total"] else None,
    }


def conferir(pdf_path) -> dict:
    """
    Confere o resultado do parser com o próprio extrato:
      - soma de créditos/débitos x linha 'Total'
      - saldo da última transação x saldo do 'Total'
      - lançamentos com aviso (saldo não confere / sinal corrigido) ou incompletos
    """
    linhas, secoes, _, _ = _ler(pdf_path)
    trans = [t for t in _montar(linhas, secoes) if _dentro_do_periodo(t, secoes)]
    rel = {"ok": True, "secoes": [], "avisos": []}

    for idx, sec in enumerate(secoes):
        ts = [t for t in trans if t["_sec"] == idx and t["valor"] is not None]
        cred = round(sum(t["valor"] for t in ts if t["valor"] > 0), 2)
        deb = round(sum(t["valor"] for t in ts if t["valor"] < 0), 2)
        item = {
            "periodo": sec["periodo"], "qtd": len(ts),
            "creditos": cred, "debitos": deb, "total_extrato": sec["total"],
        }
        if sec["total"] and len(sec["total"]) == 3:
            tc, td, tsaldo = sec["total"]
            if abs(cred - tc) > 0.01:
                rel["ok"] = False
                rel["avisos"].append(f"Seção {idx}: créditos {cred:,.2f} != Total {tc:,.2f}")
            if abs(deb - td) > 0.01:
                rel["ok"] = False
                rel["avisos"].append(f"Seção {idx}: débitos {deb:,.2f} != Total {td:,.2f}")
            ult = [t["saldo_apos"] for t in ts if t["saldo_apos"] is not None]
            if ult and abs(ult[-1] - tsaldo) > 0.01:
                rel["ok"] = False
                rel["avisos"].append(
                    f"Seção {idx}: saldo da última transação {ult[-1]:,.2f} != Total {tsaldo:,.2f}")
        rel["secoes"].append(item)

    for t in trans:
        if t.get("aviso") or t.get("incompleto"):
            rel["ok"] = False
            rel["avisos"].append(
                f"{t['data']} {t['valor']} {t['descricao'][:50]} -> "
                f"{t.get('aviso') or t.get('motivo')}")
    return rel


if __name__ == "__main__":
    import sys
    caminho = sys.argv[1]
    inf = info(caminho)
    trs = parse(caminho)
    print("INFO:", inf)
    print("TRANSAÇÕES:", len(trs))
    if trs:
        print("primeira:", trs[0])
        print("última  :", trs[-1])
    rel = conferir(caminho)
    print("CONFERÊNCIA OK:", rel["ok"])
    for s in rel["secoes"]:
        print(" seção:", s)
    for a in rel["avisos"][:30]:
        print(" aviso:", a)