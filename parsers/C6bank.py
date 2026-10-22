"""
Parser para extrato C6 Bank (conta PJ/PF - PDF exportado pelo app/Internet Banking).

Layout reconhecido (tabela repetida em todas as páginas):

    Data         Data
    lançamento   contábil   Tipo        Descrição                              Valor
    05/08        05/08      Entrada PIX Pix recebido de FULANO LTDA          R$ 2.522,19
    05/08        05/08      Saída PIX   Pix enviado para BELTRANO           -R$ 6.000,00
    09/08        10/08      Pagamento   SEBRACOM EMPRESARIAL                  -R$ 595,00
    Saldo do dia 10/08/26                                                    R$ 5.330,69

Pontos do layout:
  * as datas vêm só como dd/mm — o ano é deduzido do período do extrato
    ("Período • 1 de agosto de 2026 até 31 de agosto de 2026");
  * há duas datas por linha: lançamento (quando ocorreu) e contábil (quando
    entrou no saldo). O "Saldo do dia" fecha pela data CONTÁBIL;
  * o valor é uma única coluna alinhada à direita, negativo com "-R$";
  * não há saldo por transação, só o "Saldo do dia" ao fim de cada dia;
  * a posição das colunas (Tipo / Descrição) é lida do cabeçalho "Data / Tipo /
    Descrição / Valor" de cada página, então não depende de pontos fixos nem
    do tamanho da página.

API (igual aos demais parsers):
    parse(pdf_path)  -> lista de transações
    info(pdf_path)   -> dados da conta/período para o cabeçalho do OFX
    avisos(pdf_path) -> lista de avisos de conferência (saldos, totais, linhas incompletas)

Gera saída em formato OFX 1.02 (SGML) via ofx_writer.gerar_ofx.
"""

import re
import unicodedata
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pdfplumber

# ── Nome e banco ────────────────────────────────────────────────────────────
NOME = "C6 Bank (extrato PDF)"
BANCO_NOME = "Banco C6 S.A."
BANCO_ID = "336"

# ── Configuração ────────────────────────────────────────────────────────────
# Qual data vai em "data" da transação: "contabil" (bate com o Saldo do dia)
# ou "lancamento" (data em que a operação foi feita). As duas ficam sempre
# disponíveis em data_contabil / data_lancamento.
DATA_PADRAO = "contabil"

# ── Parâmetros de layout (pontos) ───────────────────────────────────────────
TOL_LINHA = 3.0          # variação de Y aceita para agrupar palavras na mesma linha
TOL_COLUNA = 4.0         # folga em X ao classificar uma palavra em Tipo/Descrição
GAP_PALAVRA = 1.0        # vazio mínimo em X para inserir espaço entre palavras
MAX_GAP_CONTINUACAO = 18.0  # distância máxima em Y entre linha e sua continuação
# Proporções usadas só se o cabeçalho da tabela não for encontrado na página
FALLBACK_TIPO = 0.259
FALLBACK_DESC = 0.393

# ── Regex ───────────────────────────────────────────────────────────────────
RE_DATA = re.compile(r"^(\d{2})/(\d{2})(?:/(\d{2,4}))?$")
RE_NUM = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$")
RE_VALOR_COLADO = re.compile(r"^(-?)R\$(-?)(\d[\d.]*,\d{2})$")
RE_SALDO_DIA = re.compile(
    r"^Saldo do dia\s+(\d{2})/(\d{2})/(\d{2,4})\b", re.IGNORECASE
)
RE_FIM_TABELA = re.compile(
    r"^(informa[cç][õo]es sujeitas|atendimento 24|ouvidoria|sac\b)", re.IGNORECASE
)
RE_AGENCIA = re.compile(r"Ag[êe]ncia:\s*([\d.\-]+)")
RE_CONTA = re.compile(r"Conta:\s*([\d.\-]+)")
RE_DOC = re.compile(
    r"(.+?)\s*[•·]\s*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})"
)
RE_PERIODO_EXTENSO = re.compile(
    r"Per[íi]odo\s*[•:\-]?\s*(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})"
    r"\s+(?:at[ée]|a)\s+(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})",
    re.IGNORECASE,
)
RE_PERIODO_NUMERICO = re.compile(
    r"\(\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})\s*\)"
)
RE_TOTAIS = re.compile(
    r"Entradas:\s*R\$\s*([\d.]+,\d{2}).{0,10}?Sa[ií]das:\s*R\$\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)
RE_PIX = re.compile(
    r"^pix\s+(?:recebido\s+de|enviado\s+para|recebido|enviado)\s+(.+)$", re.IGNORECASE
)

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}

_PARTICULAS = {"de", "da", "do", "das", "dos", "e"}
_SIGLAS = {"me", "epp", "mei", "sa", "s/a", "cpf", "cnpj", "pj", "pf"}


# ── Auxiliares de texto ─────────────────────────────────────────────────────

def _sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt)
        if unicodedata.category(c) != "Mn"
    )


def _para_float(numero: str) -> float:
    return float(numero.replace(".", "").replace(",", "."))


def _titulo(texto: str) -> str:
    """'MAREE INSTITUICAO DE PAGAMENTO LTDA' -> 'Maree Instituicao de Pagamento Ltda'."""
    partes = []
    for i, p in enumerate(texto.split()):
        baixa = p.lower()
        if baixa in _SIGLAS:
            partes.append(p.upper())
        elif i > 0 and baixa in _PARTICULAS:
            partes.append(baixa)
        else:
            partes.append(p[:1].upper() + p[1:].lower())
    return " ".join(partes)


def _mes(nome: str) -> int:
    return MESES[_sem_acento(nome).lower()]


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
    return "".join(partes).strip()


def _valor_na_cauda(palavras):
    """
    Lê o valor monetário no fim da linha ('R$ 921,85', '-R$ 5.000,00', 'R$ -5,00').

    Retorna (valor, quantidade_de_palavras_consumidas) ou (None, 0).
    """
    if not palavras:
        return None, 0

    ultima = palavras[-1]["text"]
    m = RE_VALOR_COLADO.match(ultima)
    if m:
        neg = m.group(1) == "-" or m.group(2) == "-"
        return (-1 if neg else 1) * _para_float(m.group(3)), 1

    if not RE_NUM.match(ultima):
        return None, 0

    i, neg, viu_rs = len(palavras) - 2, False, False
    while i >= 0:
        t = palavras[i]["text"]
        if t in ("R$", "-R$", "R$-", "-"):
            neg = neg or "-" in t
            viu_rs = viu_rs or "R$" in t
            i -= 1
        else:
            break
    if not viu_rs:  # número solto na descrição não é valor
        return None, 0
    consumidas = len(palavras) - 1 - i
    return (-1 if neg else 1) * _para_float(ultima), consumidas


# ── Layout (colunas) ────────────────────────────────────────────────────────

def _colunas(words, largura):
    """
    Descobre o X inicial das colunas Tipo e Descrição pelo cabeçalho da tabela.
    Retorna (tipo_x, desc_x, achou_cabecalho).
    """
    tipos = [w for w in words if w["text"].lower() == "tipo"]
    descs = [w for w in words if _sem_acento(w["text"]).lower() == "descricao"]
    for t in tipos:
        for d in descs:
            if abs(t["top"] - d["top"]) <= 8 and d["x0"] > t["x0"]:
                return t["x0"], d["x0"], True
    return largura * FALLBACK_TIPO, largura * FALLBACK_DESC, False


# ── Cabeçalho do extrato ────────────────────────────────────────────────────

def _ler_periodo(texto: str):
    m = RE_PERIODO_EXTENSO.search(texto)
    if m:
        d1 = date(int(m.group(3)), _mes(m.group(2)), int(m.group(1)))
        d2 = date(int(m.group(6)), _mes(m.group(5)), int(m.group(4)))
        return d1, d2
    m = RE_PERIODO_NUMERICO.search(texto)
    if m:
        return _data_br(m.group(1)), _data_br(m.group(2))
    return None


def _data_br(txt: str) -> date:
    d, m, a = txt.split("/")
    return date(int(a), int(m), int(d))


def _resolver_data(dia: int, mes: int, ano, inicio: date, fim: date) -> date:
    """Completa dd/mm com o ano que cai dentro do período (cobre virada de ano)."""
    if ano:
        ano = int(ano)
        return date(2000 + ano if ano < 100 else ano, mes, dia)
    for a in sorted({inicio.year, fim.year}):
        try:
            d = date(a, mes, dia)
        except ValueError:
            continue
        if inicio - timedelta(days=31) <= d <= fim + timedelta(days=31) and \
                (a == fim.year or d >= inicio):
            return d
    return date(inicio.year, mes, dia)


def _cabecalho(pdf) -> dict:
    texto = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])

    periodo = _ler_periodo(texto)
    if not periodo:
        raise ValueError(
            "Período não encontrado no PDF — isto é um extrato do C6 Bank?"
        )
    d1, d2 = periodo

    agencia = RE_AGENCIA.search(texto)
    conta = RE_CONTA.search(texto)
    titular, documento = "", ""
    for linha in texto.splitlines()[:12]:
        m = RE_DOC.search(linha)
        if m:
            titular, documento = m.group(1).strip(), m.group(2)
            break

    totais = RE_TOTAIS.findall(texto)
    return {
        "periodo": (d1, d2),
        "agencia": agencia.group(1) if agencia else "",
        "conta": conta.group(1).replace(".", "").replace("-", "") if conta else "",
        "titular": titular,
        "documento": documento,
    }


# ── Nome do favorecido/origem ───────────────────────────────────────────────

def _extrair_nome(tipo: str, descricao: str):
    m = RE_PIX.match(descricao.strip())
    if m:
        return _titulo(m.group(1))
    if _sem_acento(tipo).lower().startswith("pagamento") and descricao.strip():
        return _titulo(descricao.strip())
    return None


def _montar_descricao(tipo: str, desc: str) -> str:
    if not tipo:
        return desc
    if not desc:
        return tipo
    if desc.lower().startswith("pix"):
        return desc
    return f"{tipo} - {desc}"


# ── Leitura do PDF (uma única passada, com cache) ───────────────────────────

@lru_cache(maxsize=8)
def _ler(pdf_path_str: str):
    transacoes, saldos, totais_pdf = [], [], []

    with pdfplumber.open(pdf_path_str) as pdf:
        cab = _cabecalho(pdf)
        inicio, fim = cab["periodo"]

        texto_total = "\n".join((p.extract_text() or "") for p in pdf.pages)
        totais_pdf = [
            (_para_float(e), _para_float(s)) for e, s in RE_TOTAIS.findall(texto_total)
        ]

        for pagina in pdf.pages:
            words = pagina.extract_words()
            tipo_x, desc_x, _ = _colunas(words, pagina.width)
            lim_tipo = tipo_x - TOL_COLUNA
            lim_desc = desc_x - TOL_COLUNA

            ultima = None      # última transação da página (alvo de continuação)
            ultimo_top = None
            fim_tabela = False

            for linha in _agrupar_linhas(words):
                if fim_tabela:
                    break
                topo = linha[0]["top"]
                texto = _texto(linha)
                if not texto:
                    continue

                if RE_FIM_TABELA.match(_sem_acento(texto)):
                    fim_tabela = True
                    continue

                # ── Saldo do dia ────────────────────────────────────────────
                m = RE_SALDO_DIA.match(texto)
                if m:
                    valor, _ = _valor_na_cauda(linha)
                    if valor is not None:
                        saldos.append((
                            _resolver_data(int(m.group(1)), int(m.group(2)),
                                           m.group(3), inicio, fim),
                            valor,
                        ))
                    ultima = None
                    continue

                # ── Linha de lançamento: começa com dd/mm [dd/mm] ───────────
                m1 = RE_DATA.match(linha[0]["text"])
                if m1 and linha[0]["x0"] < lim_tipo:
                    idx = 1
                    m2 = None
                    if len(linha) > 1 and linha[1]["x0"] < lim_tipo:
                        m2 = RE_DATA.match(linha[1]["text"])
                        if m2:
                            idx = 2

                    d_lanc = _resolver_data(int(m1.group(1)), int(m1.group(2)),
                                            m1.group(3), inicio, fim)
                    d_cont = d_lanc if not m2 else _resolver_data(
                        int(m2.group(1)), int(m2.group(2)), m2.group(3), inicio, fim
                    )
                    # lançamento em dez/ano anterior com contábil em jan: ajusta
                    if d_cont < d_lanc - timedelta(days=200):
                        d_cont = d_cont.replace(year=d_cont.year + 1)

                    resto = linha[idx:]
                    valor, n = _valor_na_cauda(resto)
                    corpo = resto[:len(resto) - n] if n else resto

                    tipo = _texto([w for w in corpo if w["x0"] < lim_desc])
                    desc = _texto([w for w in corpo if w["x0"] >= lim_desc])

                    t = {
                        "data": d_cont if DATA_PADRAO == "contabil" else d_lanc,
                        "data_lancamento": d_lanc,
                        "data_contabil": d_cont,
                        "valor": valor,
                        "tipo": tipo,
                        "descricao": _montar_descricao(tipo, desc),
                        "doc": None,
                        "nome": _extrair_nome(tipo, desc),
                        "saldo": None,
                        "_desc": desc,
                    }
                    if valor is None:
                        t["incompleto"] = True
                        t["motivo"] = "Valor não identificado"
                    transacoes.append(t)
                    ultima, ultimo_top = t, topo
                    continue

                # ── Continuação (descrição quebrada em 2+ linhas) ───────────
                if (
                    ultima is not None
                    and topo - ultimo_top <= MAX_GAP_CONTINUACAO
                    and linha[0]["x0"] >= lim_tipo
                ):
                    valor, n = _valor_na_cauda(linha)
                    corpo = linha[:len(linha) - n] if n else linha
                    extra_tipo = _texto([w for w in corpo if w["x0"] < lim_desc])
                    extra_desc = _texto([w for w in corpo if w["x0"] >= lim_desc])
                    if extra_tipo:
                        ultima["tipo"] = f'{ultima["tipo"]} {extra_tipo}'.strip()
                    if extra_desc:
                        ultima["_desc"] = f'{ultima["_desc"]} {extra_desc}'.strip()
                    if valor is not None and ultima["valor"] is None:
                        ultima["valor"] = valor
                        ultima.pop("incompleto", None)
                        ultima.pop("motivo", None)
                    ultima["descricao"] = _montar_descricao(
                        ultima["tipo"], ultima["_desc"]
                    )
                    ultima["nome"] = _extrair_nome(ultima["tipo"], ultima["_desc"])
                    ultimo_top = topo

    for t in transacoes:
        t.pop("_desc", None)

    return transacoes, saldos, totais_pdf, cab


# ── API pública ─────────────────────────────────────────────────────────────

def parse(pdf_path) -> list:
    """
    Extrai as transações do extrato C6 Bank.

    Retorna lista de dicionários:
        data, valor, descricao, doc, nome, saldo
    (+ tipo, data_lancamento, data_contabil e, se faltar valor, incompleto/motivo)
    """
    transacoes, _, _, _ = _ler(str(pdf_path))
    return [dict(t) for t in transacoes]


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    _, saldos, _, cab = _ler(str(pdf_path))
    d1, d2 = cab["periodo"]

    saldo_final = None
    no_periodo = [(d, v) for d, v in saldos if d1 <= d <= d2]
    if no_periodo:
        saldo_final = max(no_periodo, key=lambda x: x[0])[1]

    return {
        "banco_nome": BANCO_NOME,
        "banco_id": BANCO_ID,
        "agencia": cab["agencia"],
        "conta": cab["conta"],
        "cnpj": cab["documento"],
        "titular": cab["titular"],
        "moeda": "BRL",
        "periodo_inicio": d1,
        "periodo_fim": d2,
        "saldo_final": saldo_final,
    }


def avisos(pdf_path) -> list:
    """
    Conferências que ajudam a pegar erro de leitura:
      - linhas sem valor;
      - fechamento de cada dia: saldo(dia) - saldo(dia anterior) = soma do dia;
      - totais de Entradas/Saídas impressos no PDF x soma das transações.
    """
    transacoes, saldos, totais_pdf, _ = _ler(str(pdf_path))
    msgs = []

    for t in transacoes:
        if t.get("incompleto"):
            msgs.append(
                f'{t["data_lancamento"]:%d/%m/%Y}: {t["motivo"]} — {t["descricao"]}'
            )

    # Fechamento diário (agrupado pela data contábil, que é a do "Saldo do dia")
    por_dia = {}
    for t in transacoes:
        if t["valor"] is not None:
            por_dia[t["data_contabil"]] = por_dia.get(t["data_contabil"], 0.0) + t["valor"]

    anterior = None
    for dia, saldo in sorted(saldos):
        movimento = por_dia.get(dia, 0.0)
        if anterior is not None:
            esperado = round(anterior + movimento, 2)
            if abs(esperado - saldo) > 0.01:
                msgs.append(
                    f"{dia:%d/%m/%Y}: saldo do dia R$ {saldo:,.2f} não bate com o "
                    f"calculado R$ {esperado:,.2f} (diferença R$ {saldo - esperado:,.2f})"
                )
        anterior = saldo

    # Dias com movimento que não aparecem em nenhum "Saldo do dia"
    dias_saldo = {d for d, _ in saldos}
    for dia in sorted(por_dia):
        if dia not in dias_saldo:
            msgs.append(f"{dia:%d/%m/%Y}: há movimento mas o PDF não traz 'Saldo do dia'")

    # Totais do resumo mensal
    if totais_pdf:
        ent_pdf = round(sum(e for e, _ in totais_pdf), 2)
        sai_pdf = round(sum(s for _, s in totais_pdf), 2)
        ent = round(sum(t["valor"] for t in transacoes if (t["valor"] or 0) > 0), 2)
        sai = round(-sum(t["valor"] for t in transacoes if (t["valor"] or 0) < 0), 2)
        if abs(ent - ent_pdf) > 0.01:
            msgs.append(f"Entradas: lidas R$ {ent:,.2f} x PDF R$ {ent_pdf:,.2f}")
        if abs(sai - sai_pdf) > 0.01:
            msgs.append(f"Saídas: lidas R$ {sai:,.2f} x PDF R$ {sai_pdf:,.2f}")

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