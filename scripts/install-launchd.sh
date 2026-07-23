#!/bin/zsh
# Install or update this user's weekly Podcast Summarizer LaunchAgent.
set -euo pipefail

script_dir=${0:A:h}
repository_dir=${script_dir:h}
label="com.ccm.podcast-summarizer"
source_plist="$repository_dir/launchd/$label.plist"
target_dir="$HOME/Library/LaunchAgents"
target_plist="$target_dir/$label.plist"
domain="gui/$(/usr/bin/id -u)"

[[ -f "$source_plist" ]] || { print -u2 "Missing launchd template: $source_plist"; exit 2; }

/bin/mkdir -p "$target_dir" "$repository_dir/logs/launchd"
if /bin/launchctl print "$domain/$label" >/dev/null 2>&1; then
  /bin/launchctl bootout "$domain/$label"
fi
/usr/bin/install -m 644 "$source_plist" "$target_plist"
/bin/launchctl bootstrap "$domain" "$target_plist"
/bin/launchctl enable "$domain/$label"
print "Installed $label. It will run Fridays at 15:00 local time."
