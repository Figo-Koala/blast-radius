IMAGE ?= blast-radius-entry
PLATFORM ?= linux/amd64
KIT ?= $(CURDIR)/.sample-kit
DATA := $(abspath $(KIT)/sample)
OUTPUT ?= $(CURDIR)/output.jsonl

RUN_FLAGS = --rm --platform $(PLATFORM) --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume "$(DATA)/commits:/data/commits:ro" \
	--volume "$(DATA)/tests.json:/data/tests.json:ro"

.PHONY: build sample fetch-kit score

build:
	docker build --platform $(PLATFORM) --tag $(IMAGE) .

fetch-kit:
	mkdir -p "$(KIT)"
	curl -fsSL 'https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz' | tar -xz -C "$(KIT)"

sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > "$(OUTPUT)"
	$(MAKE) score

score:
	python3 "$(KIT)/score.py" "$(DATA)" "$(DATA)/answers.json" "$(OUTPUT)"
