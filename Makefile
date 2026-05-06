# ============================================================================ #
#  cart_system — Makefile                                                       #
#                                                                               #
#  All commands run against the Docker Compose stack by default.               #
#  Use `make help` to list every target with its description.                  #
#                                                                               #
#  Prerequisites: Docker Desktop (or Docker Engine) with the Compose v2        #
#  plugin (`docker compose`, not the legacy `docker-compose`).                 #
# ============================================================================ #

# ---- Configuration --------------------------------------------------------- #

DC          := docker compose
WEB         := $(DC) exec web
PYTHON      := $(WEB) python manage.py
SETTINGS    := DJANGO_SETTINGS_MODULE=cart_system.settings.test

# Colour helpers (ANSI escape codes; silently skipped in non-TTY environments)
BOLD  := \033[1m
GREEN := \033[0;32m
CYAN  := \033[0;36m
RESET := \033[0m

.DEFAULT_GOAL := help

# Mark every target that does not produce a file as .PHONY so Make never
# confuses them with files of the same name.
.PHONY: help up down logs restart \
        migrate makemigrations \
        seed \
        test lint \
        shell shell-db \
        swagger \
        reset \
        build


# ============================================================================ #
#  HELP                                                                         #
# ============================================================================ #

help:
	@echo ""
	@echo "$(BOLD)cart_system — available make targets$(RESET)"
	@echo ""
	@echo "$(CYAN)Stack management$(RESET)"
	@echo "  $(BOLD)make up$(RESET)                 Start all services (web, worker, db, redis)"
	@echo "  $(BOLD)make down$(RESET)               Stop all services"
	@echo "  $(BOLD)make restart$(RESET)            Stop then start all services"
	@echo "  $(BOLD)make logs$(RESET)               Tail logs for all services"
	@echo "  $(BOLD)make logs s=web$(RESET)         Tail logs for a specific service (s=web|worker|db|redis)"
	@echo "  $(BOLD)make build$(RESET)              (Re)build images without starting"
	@echo "  $(BOLD)make reset$(RESET)              Destroy everything (volumes included) and rebuild cleanly"
	@echo ""
	@echo "$(CYAN)Database$(RESET)"
	@echo "  $(BOLD)make migrate$(RESET)            Apply all pending migrations"
	@echo "  $(BOLD)make makemigrations$(RESET)     Generate new migration files"
	@echo "  $(BOLD)make makemigrations app=<app>$(RESET)  Generate migrations for a specific app"
	@echo ""
	@echo "$(CYAN)Demo data$(RESET)"
	@echo "  $(BOLD)make seed$(RESET)               Seed demo tenant, products, coupons, cart (idempotent)"
	@echo "  $(BOLD)make seed args=--no-cart$(RESET) Seed without creating a demo cart"
	@echo ""
	@echo "$(CYAN)Quality$(RESET)"
	@echo "  $(BOLD)make test$(RESET)               Run the full test suite inside the container"
	@echo "  $(BOLD)make test args='-k checkout'$(RESET)  Run a specific test subset"
	@echo "  $(BOLD)make lint$(RESET)               Run ruff linter (reports only, no auto-fix)"
	@echo ""
	@echo "$(CYAN)Developer tools$(RESET)"
	@echo "  $(BOLD)make shell$(RESET)              Open a Django shell (python manage.py shell)"
	@echo "  $(BOLD)make shell-db$(RESET)           Open a psql session in the db container"
	@echo "  $(BOLD)make swagger$(RESET)            Open Swagger UI in the default browser"
	@echo ""


# ============================================================================ #
#  STACK MANAGEMENT                                                             #
# ============================================================================ #

up:
	@echo "$(GREEN)→ Starting all services...$(RESET)"
	$(DC) up -d --build
	@echo ""
	@echo "$(GREEN)✓ Stack is up.$(RESET)"
	@echo "  API:     http://localhost:8000"
	@echo "  Swagger: http://localhost:8000/api/docs/"
	@echo "  Admin:   http://localhost:8000/admin/"
	@echo ""

down:
	@echo "$(GREEN)→ Stopping all services...$(RESET)"
	$(DC) down
	@echo "$(GREEN)✓ Stack stopped.$(RESET)"

restart: down up

logs:
	@# Tail logs for a specific service (make logs s=web) or all services.
ifdef s
	$(DC) logs -f $(s)
else
	$(DC) logs -f
endif

build:
	@echo "$(GREEN)→ Building images...$(RESET)"
	$(DC) build
	@echo "$(GREEN)✓ Images built.$(RESET)"

reset:
	@echo "$(GREEN)→ Tearing down stack and wiping all volumes...$(RESET)"
	$(DC) down -v --remove-orphans
	@echo "$(GREEN)→ Rebuilding images from scratch (no cache)...$(RESET)"
	$(DC) build --no-cache
	@echo "$(GREEN)→ Starting fresh stack...$(RESET)"
	$(DC) up -d
	@echo ""
	@echo "$(GREEN)✓ Clean environment ready.$(RESET)"
	@echo "  Run 'make migrate' then 'make seed' to load demo data."
	@echo ""


# ============================================================================ #
#  DATABASE                                                                     #
# ============================================================================ #

migrate:
	@echo "$(GREEN)→ Applying migrations...$(RESET)"
	$(PYTHON) migrate --noinput
	@echo "$(GREEN)✓ Migrations applied.$(RESET)"

makemigrations:
	@echo "$(GREEN)→ Generating migrations...$(RESET)"
ifdef app
	$(PYTHON) makemigrations $(app)
else
	$(PYTHON) makemigrations
endif
	@echo "$(GREEN)✓ Done.$(RESET)"


# ============================================================================ #
#  DEMO DATA                                                                    #
# ============================================================================ #

seed:
	@echo "$(GREEN)→ Seeding demo data (idempotent)...$(RESET)"
ifdef args
	$(PYTHON) seed_demo_data $(args)
else
	$(PYTHON) seed_demo_data
endif

# ============================================================================ #
#  QUALITY                                                                      #
# ============================================================================ #

test:
	@echo "$(GREEN)→ Running test suite...$(RESET)"
ifdef args
	$(WEB) sh -c "$(SETTINGS) pytest $(args) -v"
else
	$(WEB) sh -c "$(SETTINGS) pytest -v"
endif

lint:
	@echo "$(GREEN)→ Running ruff linter...$(RESET)"
	@$(WEB) sh -c "ruff check . 2>/dev/null || \
	  (echo '$(CYAN)  ruff not installed — run: pip install ruff$(RESET)' && exit 0)"


# ============================================================================ #
#  DEVELOPER TOOLS                                                              #
# ============================================================================ #

shell:
	@echo "$(GREEN)→ Opening Django shell...$(RESET)"
	$(PYTHON) shell

shell-db:
	@echo "$(GREEN)→ Opening psql session in db container...$(RESET)"
	$(DC) exec db psql -U cart_user -d cart_system

swagger:
	@echo "$(GREEN)→ Opening Swagger UI...$(RESET)"
	@open http://localhost:8000/api/docs/ 2>/dev/null || \
	  xdg-open http://localhost:8000/api/docs/ 2>/dev/null || \
	  echo "  Visit: http://localhost:8000/api/docs/"
