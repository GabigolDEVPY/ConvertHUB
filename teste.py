#!/usr/bin/env python3
"""Uso: python corrigir_converthub.py caminho/do/arquivo.py"""
import shutil
import sys
from pathlib import Path

PATCHES = [
    # 1) group_key_desc com opção de considerar números
    (
        'def group_key_desc(desc: str) -> str:\n'
        '    base = normalize_desc(desc)\n',
        'def group_key_desc(desc: str, considerar_numeros: bool = False) -> str:\n'
        '    base = normalize_desc(desc)\n'
        '    if considerar_numeros:\n'
        '        return "NUM|" + re.sub(r"\\s+", " ", base).strip().upper()\n',
    ),
    # 2) estado no Api
    (
        '        self._codigos = {}\n        self._txt_migrate()',
        '        self._codigos = {}\n        self._num = False\n        self._txt_migrate()',
    ),
    # 3) _rebuild_codigos
    (
        'gk = group_key_desc(lan["descricao"])\n            dn = normalize_desc(lan["descricao"])',
        'gk = group_key_desc(lan["descricao"], self._num)\n            dn = normalize_desc(lan["descricao"])',
    ),
    # 4) txt_set_codigo
    (
        'gk = group_key_desc(lan["descricao"])\n                self._memoria[gk] = codigo',
        'gk = group_key_desc(lan["descricao"], self._num)\n                self._memoria[gk] = codigo',
    ),
    (
        'if group_key_desc(outro["descricao"]) == gk:',
        'if group_key_desc(outro["descricao"], self._num) == gk:',
    ),
    # 5) snapshot
    (
        '            "perfil": self._perfil,\n            "perfis": self._cfg.get("profiles", []),',
        '            "perfil": self._perfil,\n            "considerar_numeros": self._num,\n            "perfis": self._cfg.get("profiles", []),',
    ),
    # 6) método da API
    (
        '    def txt_novo_perfil(self, name):',
        '    def txt_considerar_numeros(self, flag):\n'
        '        with self._lock:\n'
        '            self._num = bool(flag)\n'
        '            antigos = dict(self._codigos)\n'
        '            self._rebuild_codigos()\n'
        '            for uid, c in antigos.items():\n'
        '                self._codigos.setdefault(uid, c)\n'
        '            return {"ok": True, **self._txt_snapshot()}\n'
        '\n'
        '    def txt_novo_perfil(self, name):',
    ),
    # 7) HTML do checkbox
    (
        '<input id="txt-missing" type="checkbox" class="accent-sky-600"> só sem código</label>',
        '<input id="txt-missing" type="checkbox" class="accent-sky-600"> só sem código</label>\n'
        '          <label class="flex items-center gap-1.5 text-sm text-slate-600 cursor-pointer" '
        'title="Marcado: só agrupa descrições com letras E números iguais"><input id="txt-num" '
        'type="checkbox" class="accent-sky-600"> considerar números</label>',
    ),
    # 8) JS: sincronizar checkbox
    (
        '  T.lan = snap.lancamentos || T.lan;',
        '  T.lan = snap.lancamentos || T.lan;\n'
        "  { const cn = $('#txt-num'); if (cn) cn.checked = !!snap.considerar_numeros; }",
    ),
    # 9) JS: handler
    (
        "$('#txt-missing').onchange = txtRender;",
        "$('#txt-missing').onchange = txtRender;\n"
        "$('#txt-num').onchange = async () => {\n"
        "  const r = await py('txt_considerar_numeros', $('#txt-num').checked);\n"
        "  if (r.ok) { T.lan = r.lancamentos; T.memoria = r.memoria; txtRender(); }\n"
        "};",
    ),
]


def main():
    if len(sys.argv) != 2:
        print("Uso: python corrigir_converthub.py caminho/do/arquivo.py")
        sys.exit(1)
    src = Path(sys.argv[1])
    if not src.is_file():
        print(f"Erro: arquivo não encontrado: {src}")
        sys.exit(1)
    try:
        texto = src.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Erro ao ler o arquivo: {e}")
        sys.exit(1)

    crlf = "\r\n" in texto
    if crlf:
        texto = texto.replace("\r\n", "\n")

    for i, (old, new) in enumerate(PATCHES, 1):
        n = texto.count(old)
        if n != 1:
            print(f"Erro na correção {i}: trecho encontrado {n} vez(es) (esperado 1). "
                  "Nada foi gerado.")
            sys.exit(1)
        texto = texto.replace(old, new)

    if crlf:
        texto = texto.replace("\n", "\r\n")

    backup = src.with_name(src.stem + "_backup" + src.suffix)
    destino = src.with_name(src.stem + "_corrigido" + src.suffix)
    try:
        shutil.copy2(src, backup)
        destino.write_text(texto, encoding="utf-8", newline="")
    except Exception as e:
        print(f"Erro ao gravar arquivos: {e}")
        sys.exit(1)

    print(f"Backup: {backup}")
    print(f"Arquivo corrigido: {destino}")


if __name__ == "__main__":
    main()