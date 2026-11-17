"""Deploy updated repository files to Hugging Face Space: balajidhanraj/shoonya-orb."""

import os
from pathlib import Path
from huggingface_hub import HfApi

REPO_ID = "balajidhanraj/shoonya-orb"
REPO_TYPE = "space"
ROOT_DIR = Path(__file__).resolve().parent.parent

api = HfApi()

ignore_patterns = [
    ".venv/**",
    ".git/**",
    ".env",
    ".env.*",
    "**/__pycache__/**",
    ".pytest_cache/**",
    "data/daily_token.json",
    "data/token.json",
    "data/historical/**",
    "data/backtest/**",
    "logs/**",
    "scratch/**",
    "*.log",
    "*.tmp",
]

print(f"Deploying {ROOT_DIR} to Hugging Face Space: {REPO_ID} ...")

commit_info = api.upload_folder(
    folder_path=str(ROOT_DIR),
    repo_id=REPO_ID,
    repo_type=REPO_TYPE,
    ignore_patterns=ignore_patterns,
    commit_message="feat: deploy Master Alpha 15% strategy, product types, backtest route, and UI enhancements",
)

print(f"Deployment successful!")
print(f"Commit URL: {commit_info}")
