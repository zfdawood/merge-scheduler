.PHONY: test sync install

# dev-loop carries copies of the shared modules so each skill can be dropped in on its own.
sync:
	cp skills/merge-gate/tools/github.py skills/merge-gate/tools/config.py skills/dev-loop/tools/

test: sync
	cd skills/merge-gate/tools && python3 test_gate.py

# Copy both skills into ~/.claude/skills (or SKILLS_DIR=...).
SKILLS_DIR ?= $(HOME)/.claude/skills
install: sync
	mkdir -p $(SKILLS_DIR)
	cp -r skills/merge-gate skills/dev-loop $(SKILLS_DIR)/
	@echo "Installed to $(SKILLS_DIR). Now: cp merge-scheduler.example.json ~/.config/merge-scheduler/config.json and edit it."
