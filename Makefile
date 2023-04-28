
mypy:
	MYPYPATH=./sublime-stubs mypy --strict --check-untyped-defs rsub.py
