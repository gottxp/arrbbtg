"""Ponto de entrada: python -m scanner [--config caminho]

Por enquanto so le e valida o config.yaml e imprime "config ok".
"""

import argparse
import sys

from scanner.config.carregar import ErroConfig, carregar_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner", description="Scanner de arbitragem cripto")
    parser.add_argument("--config", default="config.yaml", help="caminho do config.yaml (padrao: ./config.yaml)")
    args = parser.parse_args(argv)

    try:
        carregar_config(args.config)
    except ErroConfig as erro:
        print(f"Erro na configuracao: {erro}", file=sys.stderr)
        return 1

    print("config ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
