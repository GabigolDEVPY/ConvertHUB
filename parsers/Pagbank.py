"""
Parser para extrato PagBank / PagSeguro (conta digital PJ/PF - PDF "Extrato da conta").

Layout reconhecido (tabela repetida em todas as páginas):

    Data         Descrição                                                     Valor
    03/09/2026   Rendimento da conta - Rendimento líquido sobre dinheiro...   R$ 0,13
    10/09/2026   Pagamento de conta - Contabilidade Santos Ltda            -R$ 810,50
    17/09/2026   Pix recebido - Appmax                                     R$ 1.427,26
    10/09/2026   Saldo do dia                                               R$ 577,23

Pontos do layout:
  * uma única data por linha, já com ano (dd/mm/aaaa);
  * a descrição vem como "Tipo - Detalhe" (ex.: "QR Code Pix enviado - Receita
    Federal"); o Tipo é tudo antes do primeiro " - ";
  * o valor é uma única coluna alinhada à direita, negativo com "-R$";
  * "Saldo do dia" é uma linha da própria tabela (saldo no fim do dia, só nos
    dias com movimento) e NÃO é transação;
  * não há saldo por transação nem saldo inicial: o inicial é deduzido
    (primeiro saldo do dia menos a movimentação desse dia);
  * a posição da coluna Descrição é lida do cabeçalho "Data / Descrição /
    Valor" de cada página, sem pontos fixos.

API (igual aos demais parsers):
    parse(pdf_path)  -> lista de transações
    info(pdf_path)   -> dados da conta/período para o cabeçalho do OFX
    avisos(pdf_path) -> lista de avisos de conferência

Gera saída em formato OFX 1.02 (SGML) via ofx_writer.gerar_ofx.
"""

import re
import unicodedata
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pdfplumber

# ── Nome e banco ────────────────────────────────────────────────────────────
NOME = "PagBank (extrato PDF)"
BANCO_NOME = "PagSeguro Internet S/A"
BANCO_ID = "290"

# ── Parâmetros de layout (pontos) ───────────────────────────────────────────
TOL_LINHA = 3.0             # variação de Y aceita para agrupar palavras na mesma linha
TOL_COLUNA = 4.0            # folga em X ao classificar uma palavra em Data/Descrição
GAP_PALAVRA = 1.0           # vazio mínimo em X para inserir espaço entre palavras
GAP_BLOCO = 25.0            # vazio mínimo em X para separar blocos do cabeçalho
MAX_GAP_CONTINUACAO = 30.0  # distância máxima em Y entre linha e sua continuação
FALLBACK_DESC = 0.192       # proporção da largura, se o cabeçalho não for achado

# ── Regex ───────────────────────────────────────────────────────────────────
RE_DATA = re.compile(r"^(\d{2})/(\d{2})/(\d{2,4})$")
RE_NUM = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$")
RE_VALOR_COLADO = re.compile(r"^(-?)R\$(-?)(\d[\d.]*,\d{2})$")
RE_SALDO = re.compile(
    r"^saldo\s+(do dia|anterior|inicial|final)\s*$", re.IGNORECASE
)
RE_PERIODO = re.compile(
    r"Per[íi]odo:\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE
)
RE_DOC = re.compile(
    r"(CNPJ|CPF):\s*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})",
    re.IGNORECASE,
)
RE_BANCO = re.compile(r"^(\d{3})\s*-\s*(.+)$", re.MULTILINE)
RE_AGENCIA = re.compile(r"Ag[êe]ncia\s*:?\s*([\d.\-]+)")
RE_CONTA = re.compile(r"\bConta\s*:?\s*(\d[\d.\-]*)")
RE_CODIGO_INICIAL = re.compile(r"^[\d.\-/]{5,}\s+(?=[A-Za-zÀ-ÿ])")

# Tipos cujo "Detalhe" é o favorecido/origem (os demais — Vendas, Rendimento,
# Cartão... — não têm contraparte).
TIPOS_COM_NOME = ("pix", "ted", "doc", "transfer", "pagamento de conta", "boleto")


# ── Auxiliares de texto ─────────────────────────────────────────────────────

def _sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt)
        if unicodedata.category(c) != "Mn"
    )


def _para_float(numero: str) -> float:
    return float(numero.replace(".", "").replace(",", "."))


def _data_br(txt: str) -> date:
    d, m, a = txt.split("/")
    a = int(a)
    return date(2000 + a if a < 100 else a, int(m), int(d))


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
    Lê o valor monetário no fim da linha ('R$ 0,13', '-R$ 810,50', 'R$ -5,00').

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

def _coluna_descricao(words, largura):
    """X inicial da coluna Descrição, pelo cabeçalho 'Data / Descrição / Valor'."""
    datas = [w for w in words if w["text"].lower() == "data"]
    descs = [w for w in words if _sem_acento(w["text"]).lower() == "descricao"]
    for d in datas:
        for s in descs:
            if abs(d["top"] - s["top"]) <= 6 and s["x0"] > d["x1"]:
                return s["x0"]
    return largura * FALLBACK_DESC


# ── Cabeçalho do extrato ────────────────────────────────────────────────────

def _titular(words) -> str:
    """Nome do titular: bloco de texto logo acima do 'CNPJ:'/'CPF:' (mesma coluna)."""
    ancora = next((w for w in words if w["text"].upper() in ("CNPJ:", "CPF:")), None)
    if not ancora:
        return ""
    acima = [
        w for w in words
        if 2 < ancora["top"] - w["top"] <= 22
    ]
    melhor, dist = None, None
    for linha in _agrupar_linhas(acima):
        # quebra a linha em blocos pelos vazios em X
        blocos, atual, x_fim = [], [], None
        for w in linha:
            if x_fim is not None and w["x0"] - x_fim > GAP_BLOCO:
                blocos.append(atual)
                atual = []
            atual.append(w)
            x_fim = w["x1"]
        if atual:
            blocos.append(atual)
        for b in blocos:
            d = abs(b[0]["x0"] - ancora["x0"])
            if dist is None or d < dist:
                melhor, dist = b, d
    return _texto(melhor) if melhor else ""


def _cabecalho(pdf) -> dict:
    pag = pdf.pages[0]
    words = pag.extract_words()
    texto = pag.extract_text() or ""
    # só a parte acima da tabela, para "Conta"/"Agência" da descrição não confundir
    texto_cab = texto.split("Extrato da conta")[0] + "\n" + \
        "\n".join(texto.split("Extrato da conta")[1:2][:1])[:200]

    periodo = RE_PERIODO.search(texto)
    if not periodo:
        raise ValueError(
            "Período não encontrado no PDF — isto é um extrato do PagBank?"
        )
    d1, d2 = _data_br(periodo.group(1)), _data_br(periodo.group(2))

    doc = RE_DOC.search(texto_cab)
    agencia = RE_AGENCIA.search(texto_cab)
    conta = RE_CONTA.search(texto_cab)
    banco = RE_BANCO.search(texto_cab)

    return {
        "periodo": (d1, d2),
        "agencia": agencia.group(1) if agencia else "",
        "conta": conta.group(1).replace(".", "").replace("-", "") if conta else "",
        "documento": doc.group(2) if doc else "",
        "titular": _titular(words),
        "banco_id": banco.group(1) if banco else BANCO_ID,
        "banco_nome": banco.group(2).strip() if banco else BANCO_NOME,
    }


# ── Tipo / nome ─────────────────────────────────────────────────────────────

def _separar(descricao: str):
    """'QR Code Pix enviado - Receita Federal' -> ('QR Code Pix enviado', 'Receita Federal')."""
    if " - " in descricao:
        tipo, detalhe = descricao.split(" - ", 1)
        return tipo.strip(), detalhe.strip()
    return descricao.strip(), ""


def _extrair_nome(tipo: str, detalhe: str):
    if not detalhe:
        return None
    t = _sem_acento(tipo).lower()
    if not any(k in t for k in TIPOS_COM_NOME):
        return None
    nome = RE_CODIGO_INICIAL.sub("", detalhe).strip()
    return nome or None


def _montar(t: dict):
    tipo, detalhe = _separar(t["descricao"])
    t["tipo"] = tipo
    t["nome"] = _extrair_nome(tipo, detalhe)


# ── Leitura do PDF (uma única passada, com cache) ───────────────────────────

@lru_cache(maxsize=8)
def _ler(pdf_path_str: str):
    transacoes, saldos, iniciais = [], [], []

    with pdfplumber.open(pdf_path_str) as pdf:
        cab = _cabecalho(pdf)

        for pagina in pdf.pages:
            words = pagina.extract_words()
            desc_x = _coluna_descricao(words, pagina.width)
            lim_desc = desc_x - TOL_COLUNA

            ultima, ultimo_top = None, None

            for linha in _agrupar_linhas(words):
                topo = linha[0]["top"]
                m = RE_DATA.match(linha[0]["text"])

                # ── Linha com data ──────────────────────────────────────────
                if m and linha[0]["x0"] < lim_desc:
                    dia = _data_br(linha[0]["text"])
                    resto = linha[1:]
                    valor, n = _valor_na_cauda(resto)
                    corpo = resto[:len(resto) - n] if n else resto
                    desc = _texto(corpo)

                    ms = RE_SALDO.match(_sem_acento(desc))
                    if ms:
                        if valor is not None:
                            alvo = saldos if ms.group(1).lower() in ("do dia", "final") \
                                else iniciais
                            alvo.append((dia, valor))
                        ultima = None
                        continue

                    t = {
                        "data": dia,
                        "valor": valor,
                        "descricao": desc,
                        "doc": None,
                        "nome": None,
                        "saldo": None,
                    }
                    if valor is None:
                        t["incompleto"] = True
                        t["motivo"] = "Valor não identificado"
                    _montar(t)
                    transacoes.append(t)
                    ultima, ultimo_top = t, topo
                    continue

                # ── Continuação (descrição quebrada em 2+ linhas) ───────────
                if (
                    ultima is not None
                    and topo - ultimo_top <= MAX_GAP_CONTINUACAO
                    and linha[0]["x0"] >= lim_desc
                ):
                    valor, n = _valor_na_cauda(linha)
                    corpo = linha[:len(linha) - n] if n else linha
                    extra = _texto(corpo)
                    if extra:
                        ultima["descricao"] = f'{ultima["descricao"]} {extra}'.strip()
                    if valor is not None and ultima["valor"] is None:
                        ultima["valor"] = valor
                        ultima.pop("incompleto", None)
                        ultima.pop("motivo", None)
                    _montar(ultima)
                    ultimo_top = topo
                else:
                    ultima = None  # linha de cabeçalho/rodapé: encerra o encadeamento

    return transacoes, saldos, iniciais, cab


# ── API pública ─────────────────────────────────────────────────────────────

def parse(pdf_path) -> list:
    """
    Extrai as transações do extrato PagBank (as linhas 'Saldo do dia' ficam de fora).

    Retorna lista de dicionários:
        data, valor, descricao, doc, nome, saldo
    (+ tipo e, se faltar valor, incompleto/motivo)
    """
    transacoes, _, _, _ = _ler(str(pdf_path))
    return [dict(t) for t in transacoes]


def _movimento_por_dia(transacoes) -> dict:
    por_dia = {}
    for t in transacoes:
        if t["valor"] is not None:
            por_dia[t["data"]] = round(por_dia.get(t["data"], 0.0) + t["valor"], 2)
    return por_dia


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    transacoes, saldos, iniciais, cab = _ler(str(pdf_path))
    d1, d2 = cab["periodo"]

    no_periodo = [(d, v) for d, v in saldos if d1 <= d <= d2]
    saldo_final = max(no_periodo, key=lambda x: x[0])[1] if no_periodo else None

    # Saldo inicial: explícito, se o PDF trouxer; senão, deduzido do 1º dia.
    saldo_inicial = None
    if iniciais:
        saldo_inicial = min(iniciais, key=lambda x: x[0])[1]
    elif no_periodo:
        d, v = min(no_periodo, key=lambda x: x[0])
        saldo_inicial = round(v - _movimento_por_dia(transacoes).get(d, 0.0), 2)

    return {
        "banco_nome": cab["banco_nome"],
        "banco_id": cab["banco_id"],
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
      - linhas sem valor;
      - fechamento de cada dia: saldo(dia) - saldo(dia anterior com saldo) = soma do dia;
      - dias com movimento sem linha 'Saldo do dia';
      - transações fora do período do extrato.
    """
    transacoes, saldos, iniciais, cab = _ler(str(pdf_path))
    d1, d2 = cab["periodo"]
    msgs = []

    for t in transacoes:
        if t.get("incompleto"):
            msgs.append(f'{t["data"]:%d/%m/%Y}: {t["motivo"]} — {t["descricao"]}')
        if not (d1 <= t["data"] <= d2):
            msgs.append(
                f'{t["data"]:%d/%m/%Y}: fora do período {d1:%d/%m/%Y}–{d2:%d/%m/%Y} '
                f'— {t["descricao"]}'
            )

    por_dia = _movimento_por_dia(transacoes)
    saldo_ant = min(iniciais, key=lambda x: x[0])[1] if iniciais else None

    for dia, saldo in sorted(saldos):
        movimento = por_dia.get(dia, 0.0)
        if saldo_ant is not None:
            esperado = round(saldo_ant + movimento, 2)
            if abs(esperado - saldo) > 0.01:
                msgs.append(
                    f"{dia:%d/%m/%Y}: saldo do dia R$ {saldo:,.2f} não bate com o "
                    f"calculado R$ {esperado:,.2f} (diferença R$ {saldo - esperado:,.2f})"
                )
        saldo_ant = saldo

    dias_saldo = {d for d, _ in saldos}
    for dia in sorted(por_dia):
        if dia not in dias_saldo:
            msgs.append(f"{dia:%d/%m/%Y}: há movimento mas o PDF não traz 'Saldo do dia'")

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