#!/bin/zsh
# launchd starts with a sparse environment. Keep the scheduled invocation
# deterministic and retain normal command output in the launchd log files.
set -euo pipefail

script_dir=${0:A:h}
repository_dir=${script_dir:h}

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$repository_dir"
exec uv run --locked python poddigest.py --no-live
