"""允许 `python -m lvs ...` 运行（无需 pip install）。"""

from lvs.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
