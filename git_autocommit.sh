#!/bin/bash
cd /config

git add appdaemon/apps/*.py
git add appdaemon/apps/apps.yaml
git add dashboards/*.yaml
git add automations/*.yaml 2>/dev/null
git add configuration.yaml
git add README.md
git add docs/*.md
git add CLAUDE_CODE_RULES.md
git add .gitignore
git add CLAUDE_CONTEXT.md

if git diff --cached --quiet; then
    echo "$(date): No changes"
    exit 0
fi

CHANGED=$(git diff --cached --name-only | wc -l)
git commit -m "Auto-commit $(date +%Y-%m-%d): ${CHANGED} files changed"

GITHUB_TOKEN=$(grep github_token /config/secrets.yaml | awk '{print $2}')
if [ -n "$GITHUB_TOKEN" ] && [ "$GITHUB_TOKEN" != "VLO_SEM_TOKEN" ]; then
    git push https://xkamil1:${GITHUB_TOKEN}@github.com/xkamil1/home-assistant-config.git main 2>&1 | sed "s/${GITHUB_TOKEN}/***HIDDEN***/g"
    echo "$(date): Push done"
else
    echo "$(date): ERROR - github_token not set"
fi
