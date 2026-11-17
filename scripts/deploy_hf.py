"""Deploy updated repository files to Hugging Face Space: balajidhanraj/shoonya-orb."""

import os
import sys
from pathlib import Path
from huggingface_hub import HfApi

REPO_ID = "balajidhanraj/shoonya-orb"
REPO_TYPE = "space"
ROOT_DIR = Path(__file__).resolve().parent.parent

api = HfApi()

ignore_patterns = [
    ".venv/**",
    ".git/**",
    ".sf/**",
    ".env",
    ".env.*",
    "**/__pycache__/**",
    ".pytest_cache/**",
    "data/daily_token.json",
    "data/token.json",
    "logs/**",
    "scratch/**",
    "*.log",
    "*.tmp",
]

commit_msg = sys.argv[1] if len(sys.argv) > 1 else "feat: sync latest Master Alpha 15% backtest data, dashboard, and live log engine"

print(f"Deploying {ROOT_DIR} to Hugging Face Space: {REPO_ID} ...")

commit_info = api.upload_folder(
    folder_path=str(ROOT_DIR),
    repo_id=REPO_ID,
    repo_type=REPO_TYPE,
    ignore_patterns=ignore_patterns,
    commit_message=commit_msg,
)

print(f"Deployment successful!")
print(f"Commit URL: {commit_info}")
