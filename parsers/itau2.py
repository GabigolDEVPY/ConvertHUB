"""
Parser para extrato Itaú Empresas (PDF).

Layout suportado (exemplo):

    Agência 8943 Conta 0099020-3 CNPJ 53.924.729/0001-10 WAYFLEX
    Lançamentos do período: 01/08/2026 até 31/08/2026
    Data Lançamentos Razão Social CNPJ/CPF Valor(R$) Saldo(R$)
    31/07/2026 SALDO ANTERIOR -2.276,08
    03/08/2026 PIX ENVIADO PAULO HENRIQUE DE FARIA 083.210.406-08 -972,00
    ...

Características:
  * não usa nome/CNPJ fixo da empresa;
  * ignora linhas informativas de saldo (SALDO ANTERIOR, SALDO TOTAL DISPONÍVEL DIA,
    SALDO MOVIMENTAÇÃO CONTA, SALDO APLIC. AUT. etc.);
  * mantém movimentações financeiras reais: PIX, boletos, cheques, tarifas,
    IOF, juros, APL APLIC AUT MAIS, RES APLIC AUT MAIS, RENDIMENTOS etc.;
  * valida o saldo com base em SALDO MOVIMENTAÇÃO CONTA, quando existir;
  * gera dados compatíveis com ofx_writer.gerar_ofx.
"""

import copy
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pdfplumber


# ── Identificação ────────────────────────────────────────────────────────────
NOME = "Itaú (extrato empresas - PDF)"
BANCO_NOME = "Banco Itaú S.A."
BANCO_ID = "341"

# Usa SALDO MOVIMENTAÇÃO CONTA como saldo final quando existir.
# Se não houver, usa SALDO TOTAL DISPONÍVEL DIA como fallback.
USAR_SALDO_MOVIMENTACAO = True


# ── Regex ────────────────────────────────────────────────────────────────────
NEGATIVOS = ("-", "−", "–")

RE_DATA = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\b")

RE_VALOR = re.compile(
    r"(?P<sinal_pre>[-−–+])?\s*"
    r"(?:R\$\s*)?"
    r"(?P<sinal_pos>[-−–+])?\s*"
    r"(?P<num>\d{1,3}(?:\.\d{3})*,\d{2})"
)

_DATA_HORA = r"\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?"
RE_PERIODO = re.compile(
    rf"Lan[çc]amentos do per[íi]odo:\s*(?P<inicio>{_DATA_HORA})\s+at[ée]\s+(?P<fim>{_DATA_HORA})",
    re.IGNORECASE,
)
# depois: datetime.strptime(m.group('inicio')[:10], '%d/%m/%d').date()

RE_AGENCIA = re.compile(r"Ag[êe]ncia\s+([\d.\-]+)")
RE_CONTA = re.compile(r"Conta\s+([\d.\-]+)")
RE_CNPJ = re.compile(r"CNPJ\s+([\d./\-]+)")

RE_DOC = re.compile(
    r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"
    r"|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"
)


# ── Prefixos conhecidos de lançamento ───────────────────────────────────────
# Ajudam a separar "tipo" e "nome" em casos comuns.
# Não são obrigatórios para o parser funcionar, mas melhoram o campo nome.
_PREFIXOS_MULTI = (
    ("pix", "enviado"),
    ("pix", "recebido"),
    ("boleto", "pago"),
    ("boleto", "recebido"),
    ("ch", "compensado"),
    ("dev", "ch"),
    ("tar", "pix", "pgto", "transf"),
    ("tar", "plano", "adapt"),
    ("tar", "ch", "valor", "sup"),
    ("tar", "dev", "chq"),
    ("taxa", "dev", "ch"),
    ("apl", "aplic", "aut", "mais"),
    ("res", "aplic", "aut", "mais"),
    ("rendimentos", "rend", "pago", "aplic", "aut", "mais"),
    ("juros", "limite", "da", "conta"),
    ("saldo", "total", "disponivel", "dia"),
    ("saldo", "movimentacao", "conta"),
    ("saldo", "aplic", "aut"),
    ("saldo", "anterior"),
)

_PREFIXOS_SIMPLES = {
    "pix",
    "boleto",
    "ch",
    "dev",
    "tar",
    "apl",
    "res",
    "rendimentos",
    "pagamentos",
    "juros",
    "iof",
    "saldo",
    "deposito",
    "saque",
    "transferencia",
    "pagamento",
    "recebimento",
    "credito",
    "debito",
}

_PREFIXOS_MULTI_ORDENADO = sorted(_PREFIXOS_MULTI, key=len, reverse=True)


# ── Auxiliares de texto ──────────────────────────────────────────────────────
def _sem_acento(txt: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFD", txt or "")
        if unicodedata.category(c) != "Mn"
    )


def _norm(txt: str) -> str:
    return _sem_acento(txt).lower().strip()


def _norm_token(txt: str) -> str:
    """Normaliza palavra para comparação: minúscula, sem acento e sem pontuação."""
    return re.sub(r"[^0-9a-zA-Z]+", "", _norm(txt))


def _limpar(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def _para_float(numero: str) -> float:
    return float(numero.replace(".", "").replace(",", "."))


def _valor_match(m: re.Match) -> float:
    sinal = m.group("sinal_pre") or m.group("sinal_pos")
    v = _para_float(m.group("num"))
    return -v if sinal in NEGATIVOS else v


def _extrair_doc(texto: str):
    m = RE_DOC.search(texto or "")
    if not m:
        return None
    return re.sub(r"\D", "", m.group(0))


def _normalizar_nome(nome: str):
    if not nome:
        return None
    return " ".join(
        p[:1].upper() + p[1:].lower()
        for p in nome.split()
        if p
    )


def _extrair_nome(descricao: str):
    """
    Tenta extrair o nome/contraparte da descrição.

    Ex.:
      'PIX ENVIADO PAULO HENRIQUE DE FARIA'
        -> 'Paulo Henrique De Faria'

    Se não conseguir identificar um prefixo conhecido, retorna None.
    """
    if not descricao:
        return None

    words = descricao.split()
    if not words:
        return None

    norm_words = [_norm_token(w) for w in words]

    n = 0
    for pref in _PREFIXOS_MULTI_ORDENADO:
        if len(norm_words) >= len(pref) and tuple(norm_words[:len(pref)]) == pref:
            n = len(pref)
            break

    if n == 0 and norm_words[0] in _PREFIXOS_SIMPLES:
        n = 1

    if n == 0:
        return None

    resto = " ".join(words[n:])
    resto = re.sub(r"^[\s\-–—,]+", "", resto).strip()
    if not resto:
        return None

    return _normalizar_nome(resto)


# ── Filtros de cabeçalho/rodapé ──────────────────────────────────────────────
def _eh_header_linha(line: str):
    """
    Retorna (eh_header, eh_header_tabela).

    eh_header = linha que deve ser ignorada.
    eh_header_tabela = além de ignorar, indica que a tabela começou.
    """
    n = _norm(line)
    if not n:
        return True, False

    # Cabeçalho da tabela
    if n.startswith("data") and "lancamentos" in n and "valor" in n:
        return True, True

    # Cabeçalho da conta
    if n.startswith("agencia") and "conta" in n and "cnpj" in n:
        return True, False

    if "lancamentos do periodo" in n:
        return True, False

    # Resumo inicial: "Saldo total Limite da conta Utilizado Disponível..."
    # Não confundir com "SALDO TOTAL DISPONÍVEL DIA" do corpo do extrato.
    if n.startswith("saldo total") and ("limite da conta" in n or "utilizado" in n):
        return True, False

    # Rodapés/avisos
    if n.startswith(
        (
            "em caso de duvidas",
            "os saldos acima",
            "atualizado em",
            "reclamacoes",
            "sac",
            "ouvidoria",
            "deficiente auditivo",
            "se nao ficar satisfeito",
            "fale conosco",
        )
    ):
        return True, False

    return False, False


def _eh_parada(line: str) -> bool:
    n = _norm(line)
    return (
        n.startswith("saldo da conta corrente")
        or n.startswith("lancamentos futuros")
    )


# ── Extração das linhas lógicas ──────────────────────────────────────────────
def _coletar_linhas_logicas(pdf):
    """
    Junta linhas físicas do PDF em linhas lógicas de lançamento.

    Uma linha lógica começa com data DD/MM/AAAA. Linhas seguintes sem data
    são consideradas continuação (quebra de razão social, CNPJ etc.).
    """
    rows = []
    inside = False
    current = None
    parar = False

    for page in pdf.pages:
        text = page.extract_text() or ""

        for raw in text.splitlines():
            if parar:
                break

            line = raw.strip()
            if not line:
                continue

            if _eh_parada(line):
                if current:
                    rows.append(current)
                    current = None
                parar = True
                break

            header, header_tabela = _eh_header_linha(line)
            if header:
                if header_tabela:
                    inside = True
                continue

            if not inside:
                # Se o cabeçalho da tabela não for reconhecido, tenta iniciar
                # automaticamente ao encontrar a primeira linha com data.
                if RE_DATA.match(line):
                    inside = True
                else:
                    continue

            if RE_DATA.match(line):
                if current:
                    rows.append(current)
                current = line
            else:
                if current is not None:
                    current += " " + line

        if parar:
            break

    if current:
        rows.append(current)

    return rows


# ── Período ──────────────────────────────────────────────────────────────────
def _parse_data_br(texto: str):
    if not texto:
        return None
    texto = texto.strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d/%m/%d %H:%M:%S", "%d/%m/%d"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            continue
    return None


def _parse_periodo(texto: str):
    m = RE_PERIODO.search(texto or "")
    if not m:
        return None

    d1 = _parse_data_br(m.group("inicio"))
    d2 = _parse_data_br(m.group("fim"))
    if d1 is None or d2 is None:
        return None
    return d1, d2


# ── Parse de uma linha lógica ────────────────────────────────────────────────
def _parse_row(row: str):
    row = _limpar(row)

    m = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.*)$", row)
    if not m:
        return None

    data = _parse_data_br(m.group(1))
    if data is None:
        return None

    corpo = m.group(2).strip()

    vals = list(RE_VALOR.finditer(corpo))
    if not vals:
        return {
            "kind": "sem_valor",
            "data": data,
            "texto": row,
        }

    # Remove valores monetários da descrição.
    corpo_sem_valor = RE_VALOR.sub(" ", corpo)
    corpo_sem_valor = re.sub(r"\s+", " ", corpo_sem_valor).strip()

    # Extrai CNPJ/CPF, se houver.
    doc = _extrair_doc(corpo_sem_valor)
    if doc:
        corpo_sem_valor = RE_DOC.sub(" ", corpo_sem_valor)
        corpo_sem_valor = re.sub(r"\s+", " ", corpo_sem_valor).strip()

    tipo_norm = _norm(corpo_sem_valor)

    # Linhas informativas de saldo não entram como transação.
    if tipo_norm.startswith("saldo"):
        amount = _valor_match(vals[-1])
        return {
            "kind": "saldo",
            "data": data,
            "amount": round(amount, 2),
            "descricao": corpo_sem_valor,
            "tipo_norm": tipo_norm,
        }

    # Se houver dois ou mais valores na linha, assume:
    # penúltimo = valor do lançamento
    # último = saldo corrido (se o layout mostrar saldo por lançamento)
    if len(vals) == 1:
        valor = _valor_match(vals[0])
        saldo_linha = None
    else:
        valor = _valor_match(vals[-2])
        saldo_linha = _valor_match(vals[-1])

    descricao = corpo_sem_valor

    tx = {
        "data": data,
        "valor": round(valor, 2),
        "descricao": descricao,
        "doc": doc,
        "nome": _extrair_nome(descricao),
        "saldo": None if saldo_linha is None else round(saldo_linha, 2),
        "detalhe": row,
    }

    return {
        "kind": "tx",
        "data": data,
        "tx": tx,
    }


# ── Extração principal ───────────────────────────────────────────────────────
def _extrair_dados(parsed_rows):
    transacoes = []
    saldo_anterior = None
    saldos_mov = []     # SALDO MOVIMENTAÇÃO CONTA
    saldos_total = []   # SALDO TOTAL DISPONÍVEL DIA
    saldos_aplic = []   # SALDO APLIC. AUT.
    avisos = []

    for item in parsed_rows:
        if not item:
            continue

        kind = item.get("kind")

        if kind == "sem_valor":
            texto_norm = _norm(item.get("texto", ""))
            if "saldo" not in texto_norm:
                avisos.append(
                    "Linha sem valor identificado: "
                    + item.get("texto", "")[:120]
                )
            continue

        if kind == "saldo":
            data = item["data"]
            amount = item["amount"]
            tipo = item["tipo_norm"]

            if tipo.startswith("saldo anterior"):
                saldo_anterior = amount

            elif tipo.startswith("saldo total disponivel"):
                saldos_total.append((data, amount, len(transacoes)))

            elif tipo.startswith("saldo movimentacao"):
                saldos_mov.append((data, amount, len(transacoes)))

            elif tipo.startswith("saldo aplic"):
                saldos_aplic.append((data, amount, len(transacoes)))

            # Outros saldos informativos são ignorados.
            continue

        if kind == "tx":
            transacoes.append(item["tx"])

    return (
        transacoes,
        saldo_anterior,
        saldos_mov,
        saldos_total,
        saldos_aplic,
        avisos,
    )


def _consolidar(transacoes, saldo_anterior, saldos_mov, saldos_total, avisos):
    final = copy.deepcopy(transacoes)

    if USAR_SALDO_MOVIMENTACAO and saldos_mov:
        saldos_uso = saldos_mov
    else:
        saldos_uso = saldos_total or saldos_mov

    # Validação usando SALDO MOVIMENTAÇÃO CONTA, se existir.
    if saldo_anterior is not None and saldos_mov:
        base = saldo_anterior
        pos = 0

        for data, saldo, pos_balance in saldos_mov:
            soma = round(
                sum(t.get("valor") or 0.0 for t in transacoes[pos:pos_balance]),
                2,
            )
            esperado = round(base + soma, 2)

            if abs(esperado - saldo) > 0.05:
                avisos.append(
                    f"{data:%d/%m/%Y}: saldo de movimentação R$ {saldo:.2f} "
                    f"≠ calculado R$ {esperado:.2f}"
                )

            base = saldo
            pos = pos_balance

    # Atribui saldo ao último lançamento do dia, quando possível.
    for data, saldo, pos_balance in saldos_uso:
        if pos_balance > 0:
            idx = pos_balance - 1
            if idx < len(final) and final[idx]["data"] == data:
                final[idx]["saldo"] = saldo

    # Saldo final.
    if saldos_uso:
        saldo_final = saldos_uso[-1][1]
    elif saldo_anterior is not None:
        saldo_final = round(
            saldo_anterior + sum(t.get("valor") or 0.0 for t in final),
            2,
        )
    else:
        saldo_final = None

    return final, saldo_final, avisos


def _totais(final):
    entradas = round(
        sum(t.get("valor") or 0.0 for t in final if (t.get("valor") or 0.0) > 0),
        2,
    )
    saidas = round(
        sum(-t.get("valor") or 0.0 for t in final if (t.get("valor") or 0.0) < 0),
        2,
    )
    rendimento = round(
        sum(
            t.get("valor") or 0.0
            for t in final
            if _norm(t.get("descricao", "")).startswith("rendimentos")
        ),
        2,
    )
    return entradas, saidas, rendimento


def _ler(pdf_path):
    pdf_path = Path(pdf_path)

    with pdfplumber.open(str(pdf_path)) as pdf:
        first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
        rows_text = _coletar_linhas_logicas(pdf)

    periodo = _parse_periodo(first_text)

    parsed_rows = [_parse_row(r) for r in rows_text]

    (
        transacoes,
        saldo_anterior,
        saldos_mov,
        saldos_total,
        saldos_aplic,
        avisos,
    ) = _extrair_dados(parsed_rows)

    final, saldo_final, avisos = _consolidar(
        transacoes,
        saldo_anterior,
        saldos_mov,
        saldos_total,
        avisos,
    )

    entradas, saidas, rendimento = _totais(final)

    if not periodo:
        datas = [t["data"] for t in final]
        if not datas:
            datas = [x[0] for x in (saldos_mov + saldos_total)]

        if datas:
            periodo = (min(datas), max(datas))
        else:
            periodo = (None, None)

    return {
        "transacoes": final,
        "avisos": avisos,
        "periodo": periodo,
        "saldo_inicial": saldo_anterior,
        "saldo_final": saldo_final,
        "saldos_mov": saldos_mov,
        "saldos_total": saldos_total,
        "saldos_aplic": saldos_aplic,
        "total_entradas": entradas,
        "total_saidas": saidas,
        "rendimento": rendimento,
        "first_text": first_text,
    }


# ── Titular ──────────────────────────────────────────────────────────────────
def _titular(texto: str) -> str:
    linhas = [l.strip() for l in (texto or "").splitlines() if l.strip()]

    for i, l in enumerate(linhas):
        m = re.search(r"CNPJ\s+[\d./\-]+\s+(.*)", l, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()

        if RE_CNPJ.search(l):
            # Tenta próxima linha.
            for j in range(i + 1, min(i + 3, len(linhas))):
                cand = linhas[j]
                if (
                    cand
                    and not RE_AGENCIA.search(cand)
                    and not RE_CONTA.search(cand)
                    and not _norm(cand).startswith("saldo")
                ):
                    return cand

            # Tenta linha anterior.
            for j in range(i - 1, -1, -1):
                cand = linhas[j]
                if (
                    cand
                    and not RE_AGENCIA.search(cand)
                    and not RE_CONTA.search(cand)
                    and not _norm(cand).startswith("saldo")
                ):
                    return cand

    return ""


# ── API pública ──────────────────────────────────────────────────────────────
def parse(pdf_path) -> list:
    """
    Extrai as movimentações do extrato Itaú Empresas.

    Retorna lista de dicionários:
        data, valor, descricao, doc, nome, saldo, detalhe
    """
    return copy.deepcopy(_ler(pdf_path)["transacoes"])


def avisos(pdf_path) -> list:
    """Divergências encontradas ao validar saldos/linhas."""
    return list(_ler(pdf_path)["avisos"])


def info(pdf_path) -> dict:
    """Dados da conta e do período, usados no cabeçalho do OFX."""
    dados = _ler(pdf_path)
    texto = dados.get("first_text", "")

    agencia = RE_AGENCIA.search(texto)
    conta = RE_CONTA.search(texto)
    cnpj = RE_CNPJ.search(texto)

    periodo = dados.get("periodo") or (None, None)

    return {
        "banco_nome": BANCO_NOME,
        "banco_id": BANCO_ID,
        "agencia": agencia.group(1) if agencia else "",
        "conta": (
            conta.group(1).replace(".", "").replace("-", "")
            if conta
            else ""
        ),
        "cnpj": cnpj.group(1) if cnpj else "",
        "titular": _titular(texto),
        "moeda": "BRL",
        "periodo_inicio": periodo[0],
        "periodo_fim": periodo[1],
        "saldo_inicial": dados.get("saldo_inicial"),
        "saldo_final": dados.get("saldo_final"),
        "total_entradas": dados.get("total_entradas"),
        "total_saidas": dados.get("total_saidas"),
        "rendimento": dados.get("rendimento"),
    }


if __name__ == "__main__":
    import sys
    from ofx_writer import gerar_ofx

    if len(sys.argv) < 2:
        print("Uso: python itau_parser.py extrato.pdf [saida.ofx]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else Path(pdf_path).with_suffix(".ofx")

    txs = parse(pdf_path)
    inf = info(pdf_path)

    for a in avisos(pdf_path):
        print("AVISO:", a)

    gerar_ofx(txs, inf, out_path)
    print("OFX gerado:", out_path)