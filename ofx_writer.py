"""Geração de arquivo OFX (SGML versão 102, CP1252) a partir das transações.

Recebe a lista de dicionários devolvida por um parser de layout e o
dicionário de informações da conta (parsers.<layout>.info), e escreve o
OFX pronto para importar em erp/contador/Internet Banking.
"""

from datetime import datetime
from pathlib import Path

FUSO = "[-3:BRT]"


def _dt(data, hora="120000"):
    return f"{data:%Y%m%d}{hora}{FUSO}"


def gerar_ofx(transacoes, info, caminho_saida):
    """Escreve o OFX em ``caminho_saida`` e devolve o texto gerado."""
    agora = datetime.now()
    datas = [t["data"] for t in transacoes if t.get("data")]
    d_inicio = info.get("periodo_inicio") or (min(datas) if datas else agora.date())
    d_fim = info.get("periodo_fim") or (max(datas) if datas else agora.date())

    linhas = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:NONE",
        "ENCODING:USASCII",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "<OFX>",
        "<SIGNONMSGSRSV1>",
        "<SONRS>",
        "<STATUS>",
        "<CODE>0</CODE>",
        "<SEVERITY>INFO</SEVERITY>",
        "</STATUS>",
        f"<DTSERVER>{agora:%Y%m%d}120000{FUSO}</DTSERVER>",
        "<LANGUAGE>POR</LANGUAGE>",
        "<FI>",
        f"<ORG>{info.get('banco_nome') or info.get('banco_org') or 'Banco'}</ORG>",
        f"<FID>{info.get('banco_id', '')}</FID>",
        "</FI>",
        "</SONRS>",
        "</SIGNONMSGSRSV1>",
        "<BANKMSGSRSV1>",
        "<STMTTRNRS>",
        "<TRNUID>1</TRNUID>",
        "<STATUS>",
        "<CODE>0</CODE>",
        "<SEVERITY>INFO</SEVERITY>",
        "</STATUS>",
        "<STMTRS>",
        f"<CURDEF>{info.get('moeda', 'BRL')}</CURDEF>",
        "<BANKACCTFROM>",
        f"<BANKID>{info.get('banco_id', '')}</BANKID>",
        f"<BRANCHID>{info.get('agencia', '')}</BRANCHID>",
        f"<ACCTID>{info.get('acctid') or info.get('conta', '')}</ACCTID>",
        "<ACCTTYPE>CHECKING</ACCTTYPE>",
        "</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{_dt(d_inicio)}</DTSTART>",
        f"<DTEND>{_dt(d_fim)}</DTEND>",
    ]

    seq = {}
    for t in transacoes:
        valor = float(t.get("valor", 0.0) or 0.0)
        t_data = t.get("data") or d_fim
        centavos = abs(int(round(valor * 100)))
        chave = (t_data, centavos)
        seq[chave] = seq.get(chave, 0) + 1
        fitid = f"{t_data:%Y%m%d}{centavos}{seq[chave]}"

        doc = str(t.get("doc") or "").strip()
        desc = str(t.get("descricao") or t.get("memo") or "").strip()

        linhas += [
            "<STMTTRN>",
            f"<TRNTYPE>{'CREDIT' if valor >= 0 else 'DEBIT'}</TRNTYPE>",
            f"<DTPOSTED>{_dt(t_data)}</DTPOSTED>",
            f"<TRNAMT>{valor:.2f}</TRNAMT>",
            f"<FITID>{fitid}</FITID>",
            f"<CHECKNUM>{doc}</CHECKNUM>",
            f"<REFNUM>{doc}</REFNUM>",
            f"<MEMO>{desc}</MEMO>",
        ]
        nome = t.get("nome") or t.get("name")
        if nome:
            linhas.append(f"<NAME>{str(nome)[:32]}</NAME>")
        linhas.append("</STMTTRN>")

    saldo = info.get("saldo_final")
    if saldo is not None:
        linhas += [
            "</BANKTRANLIST>",
            "<LEDGERBAL>",
            f"<BALAMT>{saldo:.2f}</BALAMT>",
            f"<DTASOF>{_dt(d_fim)}</DTASOF>",
            "</LEDGERBAL>",
        ]
    else:
        linhas.append("</BANKTRANLIST>")

    linhas += [
        "</STMTRS>",
        "</STMTTRNRS>",
        "</BANKMSGSRSV1>",
        "</OFX>",
    ]

    texto = "\n".join(linhas)
    Path(caminho_saida).write_bytes(texto.encode("cp1252", errors="replace"))
    return texto
