#!/bin/bash
# Runs as part of Render's build step. The 163MB deploy database doesn't
# live in git (GitHub blocks single files over 100MB without Git LFS) -
# it's a GitHub Release asset instead, downloaded here at build time.
# DB_DOWNLOAD_URL is set in Render's environment once the asset exists.
set -euo pipefail

mkdir -p data/processed

if [ -z "${DB_DOWNLOAD_URL:-}" ]; then
  echo "DB_DOWNLOAD_URL is not set - cannot fetch the database. Set it in Render's environment to the GitHub Release asset URL for deploy.db." >&2
  exit 1
fi

echo "Downloading deploy.db from $DB_DOWNLOAD_URL ..."
curl -fL "$DB_DOWNLOAD_URL" -o data/processed/deploy.db
echo "Downloaded $(du -h data/processed/deploy.db | cut -f1)"
