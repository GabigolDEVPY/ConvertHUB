"""
Parser para extrato Stone (Extrato de conta corrente - PDF).

Layout reconhecido (tabela repetida em todas as páginas, uma "célula" por lançamento):

    DATA      TIPO     DESCRIÇÃO                      VALOR        SALDO        CONTRAPARTE
              |        Caua Victor de Sousa Nunes                              NU PAGAMENTOS S.A. -
    30/09/26  Entrada                                 R$ 2,00      R$ 1.059,17
              |        Pix | Maquininha                                         INSTITUIÇÃO DE PAGAMENTO
    ------------------------------------------------------------------------------------------------
    01/09/26  Saída                                 - R$ 6.290,00  R$ 0,13      COOPERATIVA DE CRÉDITO ...
                       Transferência | Pix

Pontos do layout:
  * cada lançamento tem altura variável: a linha com a data/valor/saldo fica no
    MEIO da célula e o texto da descrição e da contraparte quebra para cima e
    para baixo dela. As células são separadas por filetes horizontais (retângulos
    finos) — é com eles que cada palavra é atribuída ao seu lançamento;
  * Descrição = nome da contraparte (1+ linhas) + última linha "Forma | Origem"
    (ex.: "Pix | Maquininha", "Transferência | Pix");
  * Contraparte = instituição financeira da outra ponta (também quebra em linhas);
  * Valor: "R$ 2,00" (entrada) ou "- R$ 2.975,00" (saída, com o '-' solto);
  * Saldo = saldo APÓS a transação (a Stone informa um por lançamento);
  * a ordem do extrato é da mais recente para a mais antiga — o parser detecta a
    direção pela cadeia de saldos e devolve as transações em ordem cronológica;
  * as colunas são lidas do cabeçalho "DATA / TIPO / DESCRIÇÃO / VALOR / SALDO /
    CONTRAPARTE" de cada página (a posição muda alguns pontos entre páginas).

API (igual aos demais parsers):
    parse(pdf_path)  -> lista de transações (ordem cronológica)
    info(pdf_path)   -> dados da conta/período para o cabeçalho do OFX
    avisos(pdf_path) -> lista de avisos de conferência

Gera saída em formato OFX 1.02 (SGML) via ofx_writer.gerar_ofx.
"""

import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path

import pdfplumber

# ── Nome e banco ────────────────────────────────────────────────────────────
NOME = "Stone (extrato conta corrente PDF)"
BANCO_NOME = "Stone Instituição de Pagamento S.A."
BANCO_ID = "197"

# ── Parâmetros de layout (pontos) ───────────────────────────────────────────
TOL_LINHA = 3.0         # variação de Y aceita para agrupar palavras na mesma linha
TOL_COLUNA = 4.0        # folga em X na borda esquerda das colunas de texto
TOL_NUMERICA = 8.0      # folga em X para Valor/Saldo (o '-' da saída fica à esquerda)
GAP_PALAVRA = 1.0       # vazio mínimo em X para inserir espaço entre palavras
MIN_LARG_FILETE = 40.0  # largura mínima de um retângulo para valer como filete
MAX_ALT_FILETE = 2.0    # altura máxima de um retângulo para valer como filete

# ── Regex ───────────────────────────────────────────────────────────────────
RE_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{2,4})$")
RE_MONEY = re.compile(r"(-)?\s*R\$\s*(-)?\s*(\d[\d.]*,\d{2})")
RE_PERIODO = re.compile(
    r"Per[íi]odo:\s*de\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE
)
RE_NAO_IMPRIMIVEL = re.compile(r"[\ue000-\uf8ff\u200b-\u200f]")


# ── Auxiliares de texto ─────────────────────────────────────────────────────

def _sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt)
        if unicodedata.category(c) != "Mn"
    )


def _limpar(txt: str) -> str:
    return re.sub(r"\s+", " ", RE_NAO_IMPRIMIVEL.sub("", txt)).strip()


def _para_float(numero: str) -> float:
    return float(numero.replace(".", "").replace(",", "."))


def _data_br(txt: str) -> date:
    d, m, a = txt.split("/")
    a = int(a)
    return date(2000 + a if a < 100 else a, int(m), int(d))


def _formatar_doc(txt: str) -> str:
    dig = re.sub(r"\D", "", txt)
    if len(dig) == 14:
        return f"{dig[:2]}.{dig[2:5]}.{dig[5:8]}/{dig[8:12]}-{dig[12:]}"
    if len(dig) == 11:
        return f"{dig[:3]}.{dig[3:6]}.{dig[6:9]}-{dig[9:]}"
    return txt


def _dinheiro(palavras):
    """Lê 'R$ 2,00' / '- R$ 2.975,00' de um conjunto de palavras; None se não houver."""
    m = RE_MONEY.search(" ".join(w["text"] for w in sorted(palavras, key=lambda w: w["x0"])))
    if not m:
        return None
    sinal = -1 if (m.group(1) or m.group(2)) else 1
    return sinal * _para_float(m.group(3))


# ── Auxiliares de linha ─────────────────────────────────────────────────────

def _agrupar_linhas(words, tol=TOL_LINHA):
    """Agrupa palavras em linhas pela coordenada Y (top)."""
    linhas, atual, topo = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if topo is None or abs(w["top"] - topo) <= tol:
            atual.append(w)
            topo = w["top"] if topo is None else topo
        else:
            linhas.append(atual)
            atual, topo = [w], w["top"]
    if atual:
        linhas.append(atual)
    return [sorted(l, key=lambda w: w["x0"]) for l in linhas]


def _texto(palavras) -> str:
    """Reconstrói o texto respeitando o espaçamento original."""
    partes, x_fim = [], None
    for w in sorted(palavras, key=lambda w: w["x0"]):
        if x_fim is not None and w["x0"] - x_fim > GAP_PALAVRA:
            partes.append(" ")
        partes.append(w["text"])
        x_fim = w["x1"]
    return _limpar("".join(partes))


def _centro(w) -> float:
    return (w["top"] + w["bottom"]) / 2


# ── Layout (colunas e células) ──────────────────────────────────────────────

def _cabecalho_tabela(words):
    """
    Localiza a linha 'DATA TIPO DESCRIÇÃO VALOR SALDO CONTRAPARTE'.
    Retorna dict com x das colunas e o 'top' do cabeçalho, ou None.
    """
    for linha in _agrupar_linhas(words):
        mapa = {_sem_acento(w["text"]).upper(): w for w in linha}
        if all(k in mapa for k in ("DATA", "TIPO", "DESCRICAO", "VALOR", "SALDO", "CONTRAPARTE")):
            return {
                "data": mapa["DATA"]["x0"],
                "tipo": mapa["TIPO"]["x0"],
                "desc": mapa["DESCRICAO"]["x0"],
                "valor": mapa["VALOR"]["x0"],
                "saldo": mapa["SALDO"]["x0"],
                "contra": mapa["CONTRAPARTE"]["x0"],
                "top": mapa["DATA"]["top"],
                "bottom": max(w["bottom"] for w in linha),
            }
    return None


def _filetes(pagina):
    """Posições Y (centro) dos filetes horizontais que separam os lançamentos."""
    ys = sorted({
        round((r["top"] + r["bottom"]) / 2, 1)
        for r in pagina.rects
        if (r["bottom"] - r["top"]) <= MAX_ALT_FILETE
        and (r["x1"] - r["x0"]) >= MIN_LARG_FILETE
    })
    # junta filetes quase iguais (um por coluna, com diferença de décimos)
    uniq = []
    for y in ys:
        if not uniq or y - uniq[-1] > 2:
            uniq.append(y)
    return uniq


def _celulas(anchors_y, filetes, y_ini, y_fim):
    """
    Para cada âncora (linha da data) devolve o intervalo (y_min, y_max) da sua célula:
    entre os filetes vizinhos; se duas âncoras caírem no mesmo intervalo (filete
    ausente), divide pelo ponto médio entre elas.
    """
    limites = []
    for y in anchors_y:
        acima = [f for f in filetes if f < y]
        abaixo = [f for f in filetes if f > y]
        lo = max([y_ini] + [f for f in acima if f > y_ini])
        hi = min([y_fim] + [f for f in abaixo if f < y_fim])
        limites.append([lo, hi])
    for i in range(len(anchors_y) - 1):
        a, b = limites[i], limites[i + 1]
        if a[1] > anchors_y[i + 1] or b[0] < anchors_y[i]:
            meio = (anchors_y[i] + anchors_y[i + 1]) / 2
            a[1] = min(a[1], meio)
            b[0] = max(b[0], meio)
    return [tuple(l) for l in limites]


# ── Cabeçalho do extrato ────────────────────────────────────────────────────

def _abaixo_do_rotulo(words, rotulo, x_max):
    """Texto na linha logo abaixo de um rótulo, na mesma coluna (até x_max)."""
    alvo = [
        w for w in words
        if 4 < w["top"] - rotulo["top"] <= 26
        and rotulo["x0"] - 3 <= w["x0"] < x_max
    ]
    if not alvo:
        return ""
    primeira = _agrupar_linhas(alvo)[0]
    return _texto(primeira)


def _cabecalho(pdf) -> dict:
    pag = pdf.pages[0]
    words = pag.extract_words()
    texto = pag.extract_text() or ""

    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Período não encontrado no PDF — isto é um extrato de conta corrente da Stone?"
        )
    d1, d2 = _data_br(periodo.group(1)), _data_br(periodo.group(2))

    def achar(txt, **kw):
        ref = kw.get("mesma_linha_de")
        for w in words:
            if w["text"] == txt and (ref is None or abs(w["top"] - ref["top"]) <= 3):
                return w
        return None

    nome_r = achar("Nome")
    doc_r = achar("Documento", mesma_linha_de=nome_r) if nome_r else None
    inst_r = achar("Instituição")
    ag_r = achar("Agência", mesma_linha_de=inst_r) if inst_r else None
    cc_r = achar("Conta", mesma_linha_de=ag_r) if ag_r else None

    titular = doc = banco = agencia = conta = ""
    if nome_r:
        titular = _abaixo_do_rotulo(words, nome_r, doc_r["x0"] - 3 if doc_r else 9999)
    if doc_r:
        doc = _formatar_doc(_abaixo_do_rotulo(words, doc_r, 9999))
    if inst_r:
        banco = _abaixo_do_rotulo(words, inst_r, ag_r["x0"] - 3 if ag_r else 9999)
    if ag_r:
        agencia = _abaixo_do_rotulo(words, ag_r, cc_r["x0"] - 3 if cc_r else 9999)
    if cc_r:
        conta = _abaixo_do_rotulo(words, cc_r, 9999)

    return {
        "periodo": (d1, d2),
        "titular": titular,
        "documento": doc,
        "banco_nome": banco or BANCO_NOME,
        "agencia": agencia,
        "conta": re.sub(r"[.\-\s]", "", conta),
    }


# ── Leitura do PDF (uma única passada, com cache) ───────────────────────────

@lru_cache(maxsize=8)
def _ler(pdf_path_str: str):
    brutas = []      # na ordem em que aparecem no PDF

    with pdfplumber.open(pdf_path_str) as pdf:
        cab = _cabecalho(pdf)

        for pagina in pdf.pages:
            words = pagina.extract_words()
            col = _cabecalho_tabela(words)
            if not col:
                continue  # página sem tabela (ex.: comprovante/autenticação)

            lim = {
                "tipo": col["tipo"] - TOL_COLUNA,
                "desc": col["desc"] - TOL_COLUNA,
                "valor": col["valor"] - TOL_NUMERICA,
                "saldo": col["saldo"] - TOL_NUMERICA,
                "contra": col["contra"] - TOL_COLUNA,
            }

            corpo = [w for w in words if w["top"] > col["bottom"] - 1]

            # âncoras: palavra-data na coluna Data
            ancoras = sorted(
                (w for w in corpo
                 if RE_DATA.match(w["text"]) and w["x0"] < lim["tipo"]),
                key=lambda w: w["top"],
            )
            if not ancoras:
                continue

            ys = [_centro(a) for a in ancoras]
            celulas = _celulas(ys, _filetes(pagina), col["bottom"], pagina.height)

            for anc, (lo, hi) in zip(ancoras, celulas):
                palavras = [w for w in corpo if lo <= _centro(w) < hi]

                def coluna(a, b):
                    return [w for w in palavras if a <= w["x0"] < b]

                tipo_w = coluna(lim["tipo"], lim["desc"])
                desc_w = coluna(lim["desc"], lim["valor"])
                valor_w = coluna(lim["valor"], lim["saldo"])
                saldo_w = coluna(lim["saldo"], lim["contra"])
                contra_w = coluna(lim["contra"], 1e9)

                linhas_desc = [_texto(l) for l in _agrupar_linhas(desc_w)]
                linhas_desc = [l for l in linhas_desc if l]
                forma = next((l for l in reversed(linhas_desc) if "|" in l), "")
                nome_linhas = [l for l in linhas_desc if l != forma]

                contraparte = " ".join(
                    t for t in (_texto(l) for l in _agrupar_linhas(contra_w)) if t
                )

                brutas.append({
                    "data": _data_br(anc["text"]),
                    "tipo": _texto(tipo_w),
                    "valor": _dinheiro(valor_w),
                    "saldo": _dinheiro(saldo_w),
                    "forma": forma,
                    "nome": " ".join(nome_linhas) or None,
                    "contraparte": contraparte or None,
                })

    # ── Ordem cronológica, detectada pela cadeia de saldos ──────────────────
    def pares_ok(seq, mais_antigo_primeiro):
        ok = 0
        for a, b in zip(seq, seq[1:]):
            ant, atual = (a, b) if mais_antigo_primeiro else (b, a)
            if None in (ant["saldo"], atual["saldo"], atual["valor"]):
                continue
            if abs(round(ant["saldo"] + atual["valor"], 2) - atual["saldo"]) <= 0.01:
                ok += 1
        return ok

    if pares_ok(brutas, True) >= pares_ok(brutas, False):
        cronologica = brutas
    else:
        cronologica = list(reversed(brutas))

    transacoes = []
    for b in cronologica:
        if b["forma"] and b["nome"]:
            descricao = f'{b["forma"]} - {b["nome"]}'
        else:
            descricao = b["forma"] or b["nome"] or b["tipo"]
        t = {
            "data": b["data"],
            "valor": b["valor"],
            "descricao": descricao,
            "doc": None,
            "nome": b["nome"],
            "saldo": b["saldo"],
            "tipo": b["tipo"],
            "forma": b["forma"],
            "contraparte": b["contraparte"],
        }
        if b["valor"] is None:
            t["incompleto"] = True
            t["motivo"] = "Valor não identificado"
        transacoes.append(t)

    return transacoes, cab


# ── API pública ─────────────────────────────────────────────────────────────

def parse(pdf_path) -> list:
    """
    Extrai as transações do extrato Stone, em ordem cronológica.

    Retorna lista de dicionários:
        data, valor, descricao, doc, nome, saldo
    (+ tipo, forma, contraparte e, se faltar valor, incompleto/motivo)
    """
    transacoes, _ = _ler(str(pdf_path))
    return [dict(t) for t in transacoes]


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    transacoes, cab = _ler(str(pdf_path))
    d1, d2 = cab["periodo"]

    saldo_inicial = saldo_final = None
    if transacoes:
        primeira, ultima = transacoes[0], transacoes[-1]
        if primeira["saldo"] is not None and primeira["valor"] is not None:
            saldo_inicial = round(primeira["saldo"] - primeira["valor"], 2)
        saldo_final = ultima["saldo"]

    return {
        "banco_nome": cab["banco_nome"],
        "banco_id": BANCO_ID,
        "agencia": cab["agencia"],
        "conta": cab["conta"],
        "cnpj": cab["documento"],
        "titular": cab["titular"],
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_inicial": saldo_inicial,
        "saldo_final": saldo_final,
    }


def avisos(pdf_path) -> list:
    """
    Conferências que ajudam a pegar erro de leitura:
      - lançamentos sem valor ou sem saldo;
      - cadeia de saldos: saldo anterior + valor = saldo do lançamento;
      - transações fora do período do extrato;
      - lançamentos sem descrição/contraparte.
    """
    transacoes, cab = _ler(str(pdf_path))
    d1, d2 = cab["periodo"]
    msgs = []

    anterior = None
    for i, t in enumerate(transacoes, 1):
        rot = f'{t["data"]:%d/%m/%Y} (#{i})'
        if t.get("incompleto"):
            msgs.append(f'{rot}: {t["motivo"]} — {t["descricao"]}')
        if t["saldo"] is None:
            msgs.append(f"{rot}: saldo não identificado")
        if not (d1 <= t["data"] <= d2):
            msgs.append(f"{rot}: fora do período {d1:%d/%m/%Y}–{d2:%d/%m/%Y}")
        if not t["nome"]:
            msgs.append(f"{rot}: sem nome/descrição da contraparte")

        if anterior is not None and None not in (anterior, t["saldo"], t["valor"]):
            esperado = round(anterior + t["valor"], 2)
            if abs(esperado - t["saldo"]) > 0.01:
                msgs.append(
                    f'{rot}: saldo R$ {t["saldo"]:,.2f} não bate com o calculado '
                    f'R$ {esperado:,.2f} (diferença R$ {t["saldo"] - esperado:,.2f})'
                )
        anterior = t["saldo"] if t["saldo"] is not None else anterior

    return msgs


if __name__ == '__main__':
    import sys
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 2:
        print('Uso: python ' + sys.argv[0] + ' extrato.pdf [saida.ofx]')
        sys.exit(1)
    pdf_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else Path(pdf_path).with_suffix('.ofx')
    txs = parse(pdf_path)
    inf = info(pdf_path)
    for a in avisos(pdf_path):
        print('AVISO:', a)
    gerar_ofx(txs, inf, out_path)
    print('OFX gerado:', out_path)