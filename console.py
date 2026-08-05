"""Saída de texto em UTF-8, também no Windows.

O console do Windows costuma vir em cp1252, e aí qualquer acento ou caractere de
caixa quebra a impressão. A solução usual é reencapsular `sys.stdout` num
`TextIOWrapper` UTF-8 — mas se dois módulos fizerem isso, o segundo embrulha o
buffer do primeiro, e quando o primeiro é coletado ele fecha o buffer por baixo
do segundo. O sintoma é um `ValueError: I/O operation on closed file` no meio da
execução, longe de onde está a causa.

Por isso a configuração vive aqui e é idempotente: chamar de novo não faz nada.
"""

from __future__ import annotations

import io
import sys

_feito = False


def utf8() -> None:
    """Garante stdout e stderr em UTF-8. Seguro para chamar quantas vezes quiser."""
    global _feito
    if _feito or sys.platform != "win32":
        _feito = True
        return
    for nome in ("stdout", "stderr"):
        fluxo = getattr(sys, nome, None)
        if fluxo is None or not hasattr(fluxo, "buffer"):
            continue
        if (getattr(fluxo, "encoding", "") or "").lower().replace("-", "") == "utf8":
            continue
        setattr(sys, nome, io.TextIOWrapper(
            fluxo.buffer, encoding="utf-8", errors="replace", line_buffering=True,
        ))
    _feito = True
