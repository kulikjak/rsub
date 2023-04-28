
mypy:
	MYPYPATH=./sublime-stubs mypy --ignore-missing-imports --strict --check-untyped-defs rsub.py
