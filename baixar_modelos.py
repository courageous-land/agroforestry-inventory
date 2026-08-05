#!/usr/bin/env python3
"""Baixa os pesos dos modelos e confere a integridade de cada arquivo.

Os pesos não ficam no repositório: são 121 MB cada, e git não é lugar para
binário grande. Eles são publicados como anexos de release e baixados por aqui.

Cada arquivo é conferido pelo sha256 declarado em `modelos.json`. Download
interrompido no meio produz arquivo truncado que carrega e infere errado sem
reclamar — por isso a verificação não é opcional, e o arquivo só é considerado
válido depois de conferido.

Uso:
    python baixar_modelos.py                  # todos
    python baixar_modelos.py --especie cafe   # só um
    python baixar_modelos.py --de /caminho/local/com/os/pesos   # sem internet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from console import utf8

utf8()

RAIZ = Path(__file__).resolve().parent
DESTINO = RAIZ / "modelos"
BLOCO = 1 << 20  # 1 MB


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for pedaco in iter(lambda: f.read(BLOCO), b""):
            h.update(pedaco)
    return h.hexdigest()


def confere(caminho: Path, esperado: str) -> bool:
    return caminho.is_file() and sha256(caminho) == esperado


def barra(feito: int, total: int, largura: int = 34) -> str:
    if not total:
        return f"{feito / 1e6:.0f} MB"
    cheio = int(largura * feito / total)
    return f"[{'=' * cheio}{' ' * (largura - cheio)}] {100 * feito / total:5.1f}%"


def baixar(url: str, destino: Path) -> None:
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    try:
        with urllib.request.urlopen(url) as resposta, parcial.open("wb") as saida:
            total = int(resposta.headers.get("Content-Length") or 0)
            feito = 0
            while pedaco := resposta.read(BLOCO):
                saida.write(pedaco)
                feito += len(pedaco)
                print(f"\r    {barra(feito, total)}", end="", flush=True)
        print()
    except urllib.error.URLError as e:
        parcial.unlink(missing_ok=True)
        raise SystemExit(
            f"\nFalha ao baixar {url}\n  {e}\n"
            "Se você está sem internet, use:  python baixar_modelos.py --de <pasta com os .pth>"
        ) from e
    parcial.replace(destino)


def main() -> None:
    p = argparse.ArgumentParser(description="Baixa e verifica os pesos dos modelos.")
    p.add_argument("--especie", default=None, help="baixa só esta espécie")
    p.add_argument("--de", type=Path, default=None,
                   help="copia de uma pasta local em vez de baixar (uso offline)")
    p.add_argument("--forcar", action="store_true", help="rebaixa mesmo se o arquivo já confere")
    a = p.parse_args()

    manifesto = json.loads((RAIZ / "modelos.json").read_text(encoding="utf-8"))
    base_url = manifesto.get("base_url")
    modelos = manifesto["modelos"]
    if a.especie:
        modelos = [m for m in modelos if m["especie"] == a.especie]
        if not modelos:
            raise SystemExit(f"Espécie '{a.especie}' não existe no manifesto.")

    DESTINO.mkdir(parents=True, exist_ok=True)
    ok, faltaram = 0, []

    for m in modelos:
        alvo = DESTINO / m["arquivo"]
        print(f"\n{m['titulo']}  ({m['bytes'] / 1e6:.0f} MB)")

        if not a.forcar and confere(alvo, m["sha256"]):
            print("    já está aqui e confere")
            ok += 1
            continue

        if a.de:
            origem = a.de / m["arquivo"]
            if not origem.is_file():
                # aceita também o nome original do checkpoint
                alternativa = a.de / "checkpoint_best_regular.pth"
                origem = alternativa if alternativa.is_file() else origem
            if not origem.is_file():
                print(f"    não encontrado em {a.de}")
                faltaram.append(m["especie"])
                continue
            print(f"    copiando de {origem}")
            shutil.copy2(origem, alvo)
        elif base_url:
            baixar(base_url.rstrip("/") + "/" + m["arquivo"], alvo)
        else:
            print("    [pendente] o manifesto ainda não tem 'base_url' — os pesos não foram")
            print("               publicados. Use --de <pasta> enquanto isso.")
            faltaram.append(m["especie"])
            continue

        if confere(alvo, m["sha256"]):
            print("    verificado")
            ok += 1
        else:
            alvo.unlink(missing_ok=True)
            print("    ARQUIVO CORROMPIDO (sha256 não confere) — removido")
            faltaram.append(m["especie"])

    print(f"\n{ok} de {len(modelos)} modelo(s) prontos em {DESTINO}")
    if faltaram:
        print(f"Faltaram: {', '.join(faltaram)}")
        raise SystemExit(1)
    print("\nPróximo passo:  python inventario.py exemplo/ortofoto.tif --especie cafe")


if __name__ == "__main__":
    main()
