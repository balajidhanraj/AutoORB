"""Deploy updated repository files to Hugging Face Space: balajidhanraj/shoonya-orb."""

import os
import sys
from pathlib import Path
from huggingface_hub import CommitOperationDelete, HfApi

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

# Check if user requested a full clean wipe first
wipe_first = "--wipe" in sys.argv or "-w" in sys.argv
if wipe_first:
    print(f"Listing and removing all existing files in {REPO_ID} ...")
    remote_files = api.list_repo_files(repo_id=REPO_ID, repo_type=REPO_TYPE)
    delete_ops = [CommitOperationDelete(path_in_repo=f) for f in remote_files if f != ".gitattributes"]
    if delete_ops:
        api.create_commit(
            repo_id=REPO_ID,
            repo_type=REPO_TYPE,
            operations=delete_ops,
            commit_message="chore: remove previous project files for clean fresh deployment",
        )
        print(f"Removed {len(delete_ops)} old files.")

commit_msg = (
    [arg for arg in sys.argv[1:] if not arg.startswith("-")][0]
    if any(not arg.startswith("-") for arg in sys.argv[1:])
    else "feat: complete fresh project deployment for Shoonya Automated ORB Terminal"
)

print(f"Deploying entire folder {ROOT_DIR} to Hugging Face Space: {REPO_ID} ...")

commit_info = api.upload_folder(
    folder_path=str(ROOT_DIR),
    repo_id=REPO_ID,
    repo_type=REPO_TYPE,
    ignore_patterns=ignore_patterns,
    delete_patterns="*",
    commit_message=commit_msg,
)

print("Deployment successful!")
print(f"Commit URL: {commit_info}")
