# Photo cleanup into the canonical store. Override any variable on the command line, e.g.
#   make run LIMIT=200
#   make run YES=1                      # no per-batch prompts (long unattended runs)
#   make inventory SOURCES="/Volumes/2t/dump"

SOURCES ?= /Volumes/fileshares/photos /Volumes/fileshares/photosCleanup2026 /Volumes/Photowork
DEST    ?= /Volumes/photoArk
CATALOG ?= $(HOME)/.photo-organizer/catalog.db
LOGDIR  ?= $(HOME)/.photo-organizer/logs
LIMIT   ?= 0
BATCH   ?= 5000
WORKERS ?= 4
EXIF_WORKERS ?= 8
YES     ?=
PORT    ?= 8077

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c
export PYTHONUNBUFFERED := 1

STAMP := $(shell date +%Y%m%d-%H%M%S)
ORGANIZE = uv run photo-organizer $(foreach s,$(SOURCES),--source "$(s)") --dest "$(DEST)" \
	--db "$(CATALOG)" --limit $(LIMIT) --batch-size $(BATCH) --workers $(WORKERS) \
	--exif-workers $(EXIF_WORKERS) $(if $(YES),--yes)

.PHONY: help sync inventory run status enrich faces browse test

help:
	@echo "make inventory  dry run: catalog + date + plan every file in SOURCES (nothing moves)"
	@echo "make run        move into DEST, verify, delete sources (LIMIT=n, YES=1 to skip prompts)"
	@echo "make status     live status page at http://localhost:$(PORT)/"
	@echo "make enrich     Gemma 4 + InsightFace analysis, clustering, metadata, index (Apple Photos)"
	@echo "make faces      face-cluster labeling app"
	@echo "make browse     Datasette over the analysis index"
	@echo "make test       run the test suite"
	@echo
	@echo "SOURCES=$(SOURCES)"
	@echo "DEST=$(DEST)  CATALOG=$(CATALOG)  LIMIT=$(LIMIT)"

sync:
	@uv sync --quiet

$(LOGDIR):
	@mkdir -p "$(LOGDIR)"

inventory: sync | $(LOGDIR)
	$(ORGANIZE) 2>&1 | tee "$(LOGDIR)/inventory-$(STAMP).log"

run: sync | $(LOGDIR)
	$(ORGANIZE) --execute 2>&1 | tee "$(LOGDIR)/run-$(STAMP).log"

status: sync
	uv run photo-organizer-status --db "$(CATALOG)" --port $(PORT)

enrich: | $(LOGDIR)
	scripts/analysis_pipeline.sh 2>&1 | tee "$(LOGDIR)/enrich-$(STAMP).log"

faces:
	uv sync --quiet --extra analysis
	uv run photo-face-browser

browse:
	uv sync --quiet --extra analysis
	uv run datasette "$${PHOTO_ANALYSIS_DIR:-$(HOME)/gemma}/photos.db" -o

test:
	uv run pytest -q
