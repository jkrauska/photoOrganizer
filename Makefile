# Photo cleanup into the canonical store. Override any variable on the command line, e.g.
#   make run LIMIT=200
#   make run YES=1                      # no per-batch prompts (long unattended runs)
#   make run SOURCES="/Volumes/Backup/Image Archive"
#   make queue SRC="/some/backup" KEEP=1   # copy-only: never delete originals (automatic for Time Machine)
#   make queue SRC="/big/folder" SPLIT=1  # one queue item per subfolder, then the folder itself
#   make queue-photos YEARS="2010 2011"    # Apple Photos library years, copy-only (default: all, oldest first)
# SOURCES is comma-separated so paths may contain spaces.
#
# Your own paths belong in config.mk (gitignored); copy config.mk.example to start.
-include config.mk

SOURCES ?=
DEST    ?= /Volumes/photoArk
CATALOG ?= $(HOME)/.photo-organizer/catalog.db
LOGDIR  ?= $(HOME)/.photo-organizer/logs
LIMIT   ?= 0
YEAR    ?= 2010
BATCH   ?= 5000
WORKERS ?= 4
EXIF_WORKERS ?= 8
YES     ?=
PORT    ?= 8077
FACES_PORT ?= 8078
UI_HOST ?= 0.0.0.0
DAYS_PORT ?= 8079
SHUFFLE_PORT ?= 8080

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c
export PYTHONUNBUFFERED := 1

STAMP := $(shell date +%Y%m%d-%H%M%S)
ORGANIZE = IFS=',' read -ra src <<< "$(SOURCES)"; args=(); \
	for s in "$${src[@]}"; do args+=(--source "$$s"); done; \
	uv run photo-organizer "$${args[@]}" --dest "$(DEST)" \
	--db "$(CATALOG)" --limit $(LIMIT) --batch-size $(BATCH) --workers $(WORKERS) \
	--exif-workers $(EXIF_WORKERS) --stop-file "$(STOP_FILE)" $(if $(YES),--yes) $(if $(KEEP),--keep-sources)

STOP_FILE ?= $(HOME)/.photo-organizer/STOP

QUEUE = uv run photo-organizer-queue --db "$(CATALOG)"

.PHONY: help sync require-sources inventory run queue queue-photos set-date queue-list cameras cameras-stats locations locations-infer locations-stats worker worker-bg stop verify status enrich faces browse test \
	ark-faces ark-faces-spread ark-describe ark-days ark-seed ark-stats ark-faces-ui ark-days-ui ark-shuffle-ui ui

help:
	@echo "make inventory  dry run: catalog + date + plan every file in SOURCES (nothing moves)"
	@echo "make run        move into DEST, verify, delete sources (LIMIT=n, YES=1 to skip prompts)"
	@echo "make queue SRC=\"/path/a,/path/b\"   add folders to the live queue (any time)"
	@echo "               SPLIT=1 queues each subfolder separately (large folders); KEEP=1 copy-only"
	@echo "make queue-photos [YEARS=\"2010 2011\"]   queue Apple Photos library years, oldest first (copy-only)"
	@echo "make set-date SRC=/path DATE=YYYY-MM-DD   write that date into a folder's undated files (EXIF), then queue it"
	@echo "make queue-list show the queue"
	@echo "make cameras    record each stored file's camera + lens, read on the NAS (resumable); cameras-stats: per camera"
	@echo "make locations  GPS + place (country/state/city/region) of each stored file, read on the NAS (resumable)"
	@echo "               then infers places for files without GPS; locations-stats: coverage; regions: ~/.photo-organizer/regions.json"
	@echo "make worker     process the queue in order, then wait for more (worker-bg: in background)"
	@echo "make stop       stop a running inventory/run/worker gracefully (in-flight files finish)"
	@echo "make verify     re-read every stored file from the NAS and check its hash"
	@echo "make status     live status page at http://localhost:$(PORT)/"
	@echo "make ui         every page on one server: status, archive, days, faces, shuffle (http://$(shell hostname):$(PORT))"
	@echo "make enrich     Gemma 4 + InsightFace analysis, clustering, metadata, index (Apple Photos)"
	@echo "make faces      face-cluster labeling app"
	@echo "make browse     Datasette over the analysis index"
	@echo "make ark-faces  faces for one store year (YEAR=2010); ark-describe adds Gemma, in background"
	@echo "make ark-faces-spread  faces for the whole store, SPREAD=10 random images per year per round, in background"
	@echo "make ark-days   Gemma summaries of random day folders (whole store, or DAYS_YEAR=2010), in background"
	@echo "make ark-seed   suggest names from the Apple Photos labels; ark-stats shows counts"
	@echo "make ark-faces-ui  name face clusters in the photoArk analysis DB (http://$(shell hostname):$(FACES_PORT))"
	@echo "make ark-days-ui  browse and search the day summaries (http://$(shell hostname):$(DAYS_PORT))"
	@echo "make ark-shuffle-ui  random walk through the store, score photos 1-5 (http://$(shell hostname):$(SHUFFLE_PORT))"
	@echo "make test       run the test suite"
	@echo
	@echo "Paths come from config.mk (gitignored; copy config.mk.example) or the command line."
	@echo "SOURCES=$(SOURCES)"
	@echo "DEST=$(DEST)  CATALOG=$(CATALOG)  LIMIT=$(LIMIT)"

sync:
	@uv sync --quiet --inexact   # keeps the analysis extra the ark-* targets install

$(LOGDIR):
	@mkdir -p "$(LOGDIR)"

require-sources:
	@test -n "$(SOURCES)" || { echo 'SOURCES is empty: set it in config.mk (see config.mk.example)'; \
		echo 'or pass it in, e.g. make $(MAKECMDGOALS) SOURCES="/Volumes/Backup/Pictures"'; exit 2; }

inventory: require-sources sync | $(LOGDIR)
	$(ORGANIZE) 2>&1 | tee "$(LOGDIR)/inventory-$(STAMP).log"

run: require-sources sync | $(LOGDIR)
	$(ORGANIZE) --execute 2>&1 | tee "$(LOGDIR)/run-$(STAMP).log"

queue: sync
	@test -n "$(SRC)" || { echo 'usage: make queue SRC="/path/one,/path/two"'; exit 2; }
	@IFS=',' read -ra src <<< "$(SRC)"; $(QUEUE) add $(if $(KEEP),--keep-sources) $(if $(SPLIT),--split) "$${src[@]}"

queue-photos: sync
	@$(QUEUE) photos $(YEARS)

set-date: sync
	@test -n "$(SRC)" -a -n "$(DATE)" || { echo 'usage: make set-date SRC=/path DATE=YYYY-MM-DD'; exit 2; }
	@$(QUEUE) set-date "$(SRC)" "$(DATE)"

cameras: sync
	uv run photo-organizer-cameras --db "$(CATALOG)" run --dest "$(DEST)"

cameras-stats: sync
	@uv run photo-organizer-cameras --db "$(CATALOG)" stats

locations: sync
	uv run photo-organizer-locations --db "$(CATALOG)" read --dest "$(DEST)" $(if $(filter-out 0,$(LIMIT)),--limit $(LIMIT))
	uv run photo-organizer-locations --db "$(CATALOG)" infer

locations-infer: sync
	uv run photo-organizer-locations --db "$(CATALOG)" infer

locations-stats: sync
	@uv run photo-organizer-locations --db "$(CATALOG)" stats

queue-list: sync
	@$(QUEUE) list

worker: sync | $(LOGDIR)
	$(QUEUE) worker --dest "$(DEST)" --batch-size $(BATCH) --workers $(WORKERS) \
		--exif-workers $(EXIF_WORKERS) --stop-file "$(STOP_FILE)" 2>&1 | tee "$(LOGDIR)/worker-$(STAMP).log"

worker-bg: sync | $(LOGDIR)
	@nohup $(QUEUE) worker --dest "$(DEST)" --batch-size $(BATCH) --workers $(WORKERS) \
		--exif-workers $(EXIF_WORKERS) --stop-file "$(STOP_FILE)" > "$(LOGDIR)/worker-$(STAMP).log" 2>&1 & \
		echo "worker started (pid $$!), log: $(LOGDIR)/worker-$(STAMP).log"

stop:
	@touch "$(STOP_FILE)" && echo "Stop requested; the run finishes in-flight files and exits. Rerun to resume."

verify: sync | $(LOGDIR)
	uv run photo-organizer-verify --db "$(CATALOG)" 2>&1 | tee "$(LOGDIR)/verify-$(STAMP).log"

status: sync
	uv run photo-organizer-status --db "$(CATALOG)" --port $(PORT)

enrich: | $(LOGDIR)
	scripts/analysis_pipeline.sh 2>&1 | tee "$(LOGDIR)/enrich-$(STAMP).log"

faces:
	uv sync --quiet --extra analysis
	uv run photo-face-browser

browse:
	uv sync --quiet --extra analysis
	uv run datasette "$${PHOTO_ANALYSIS_DIR:-$(HOME)/.photo-organizer/analysis}/photos.db" -o

ark-faces: | $(LOGDIR)
	uv sync --quiet --extra analysis
	uv run photo-ark-analyze run --year $(YEAR) --no-vlm 2>&1 | tee "$(LOGDIR)/ark-faces-$(YEAR)-$(STAMP).log"

SPREAD ?= 10

ark-faces-spread: | $(LOGDIR)
	uv sync --quiet --extra analysis
	@nohup uv run photo-ark-analyze run --spread $(SPREAD) --no-vlm > "$(LOGDIR)/ark-faces-spread-$(STAMP).log" 2>&1 & \
		echo "Faces for the whole store running in background (pid $$!), log: $(LOGDIR)/ark-faces-spread-$(STAMP).log"

ark-describe: | $(LOGDIR)
	uv sync --quiet --extra analysis
	@nohup uv run photo-ark-analyze run --year $(YEAR) > "$(LOGDIR)/ark-describe-$(YEAR)-$(STAMP).log" 2>&1 & \
		echo "Gemma + faces running in background (pid $$!), log: $(LOGDIR)/ark-describe-$(YEAR)-$(STAMP).log"

ark-days: | $(LOGDIR)
	@nohup uv run photo-ark-analyze days $(if $(DAYS_YEAR),--year $(DAYS_YEAR)) > "$(LOGDIR)/ark-days-$(STAMP).log" 2>&1 & \
		echo "Day summaries running in background (pid $$!), log: $(LOGDIR)/ark-days-$(STAMP).log"

ark-seed:
	uv run photo-ark-analyze seed-names

ark-stats:
	uv run photo-ark-analyze stats

ark-faces-ui:
	uv sync --quiet --extra analysis
	uv run photo-face-browser --ark --host $(UI_HOST) --port $(FACES_PORT)

ark-days-ui:
	uv sync --quiet --extra analysis
	uv run photo-day-browser --host $(UI_HOST) --port $(DAYS_PORT)

ui:
	uv sync --quiet --inexact --extra analysis
	uv run photo-ui --host $(UI_HOST) --port $(PORT)

ark-shuffle-ui:
	uv sync --quiet --extra analysis
	uv run photo-shuffle --host $(UI_HOST) --port $(SHUFFLE_PORT)

test:
	uv run pytest -q
