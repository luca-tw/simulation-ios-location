
VENV := venv
PYTHON := ./$(VENV)/bin/python
PORT ?= 8000
URL = http://127.0.0.1:$(PORT)

.PHONY: all run lan open dev clean

all: clean dev

run:
	sudo SIMPLE_LOCATION_PORT=$(PORT) $(PYTHON) main.py
lan:
	sudo SIMPLE_LOCATION_HOST=0.0.0.0 SIMPLE_LOCATION_PORT=$(PORT) $(PYTHON) main.py
open:
	open $(URL)
dev:
	sudo SIMPLE_LOCATION_PORT=$(PORT) $(PYTHON) main.py & ( sleep 5; open $(URL) ) 
clean:
	@pid=$$(sudo lsof -t -nP -iTCP:$(PORT) -sTCP:LISTEN); \
	if [ -n "$$pid" ]; then \
		echo "Killing process $$pid on port $(PORT)..."; \
		sudo kill -9 $$pid; \
	else \
		echo "Port $(PORT) is already free"; \
	fi