#!/usr/bin/env python3
"""
Setup automatic daily updates for rs_industries history
Add to crontab or LaunchAgent for automatic execution
"""

import subprocess
import os
from pathlib import Path

def setup_launchagent_macos():
    """Setup LaunchAgent for macOS daily execution"""
    # We'll create two LaunchAgents:
    # 1) Scheduled run at 17:00 Mon-Fri (forced run)
    # 2) Polling agent that runs every 30 minutes to check upstream and append if new commits exist

    la_dir = Path.home() / "Library/LaunchAgents"
    la_dir.mkdir(parents=True, exist_ok=True)

    # Scheduled plist (5:00 PM Mon-Fri) - forces append
    scheduled_plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rs-log.append-rs-history-scheduled</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/sw/Desktop/stock/rs-log/check_remote_and_append.py</string>
        <string>--force</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>1</integer></dict>
        <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>2</integer></dict>
        <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>3</integer></dict>
        <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>4</integer></dict>
        <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>5</integer></dict>
    </array>
    <key>StandardErrorPath</key>
    <string>/Users/sw/Desktop/stock/rs-log/logs/append_rs_error.log</string>
    <key>StandardOutPath</key>
    <string>/Users/sw/Desktop/stock/rs-log/logs/append_rs_out.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>'''

    scheduled_file = la_dir / "com.rs-log.append-rs-history-scheduled.plist"
    with open(scheduled_file, 'w') as f:
        f.write(scheduled_plist)

    # Polling plist (runs every 30 minutes, checks remote and appends only if upstream has new commits)
    polling_plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rs-log.append-rs-history-poll</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/sw/Desktop/stock/rs-log/check_remote_and_append.py</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardErrorPath</key>
    <string>/Users/sw/Desktop/stock/rs-log/logs/append_rs_error.log</string>
    <key>StandardOutPath</key>
    <string>/Users/sw/Desktop/stock/rs-log/logs/append_rs_out.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>'''

    polling_file = la_dir / "com.rs-log.append-rs-history-poll.plist"
    with open(polling_file, 'w') as f:
        f.write(polling_plist)

    print(f"✅ Created LaunchAgents:\n  {scheduled_file}\n  {polling_file}")
    print(f"\nTo enable, run:")
    print(f"  launchctl load {scheduled_file}")
    print(f"  launchctl load {polling_file}")

    return (scheduled_file, polling_file)

def setup_crontab_linux():
    """Setup crontab for Linux/Unix execution"""
    # Two cron entries suggested:
    # 1) Poll every 30 minutes Mon-Fri to check remote and append only if new commits exist
    # 2) Forced run at 17:00 Mon-Fri
    cron_poll = "*/30 * * * 1-5 bash -c 'cd /Users/sw/Desktop/stock/rs-log && /usr/bin/python3 check_remote_and_append.py >> /Users/sw/Desktop/stock/rs-log/logs/append_rs.log 2>&1'"
    cron_forced = "0 17 * * 1-5 bash -c 'cd /Users/sw/Desktop/stock/rs-log && /usr/bin/python3 check_remote_and_append.py --force >> /Users/sw/Desktop/stock/rs-log/logs/append_rs.log 2>&1'"

    print(f"✅ To setup crontab, run:")
    print(f"  crontab -e")
    print(f"\nThen add these lines:")
    print(f"  {cron_poll}")
    print(f"  {cron_forced}")
    print(f"\nThis will poll every 30 minutes and force a run at 17:00 Mon-Fri")

def create_log_directory():
    """Create logs directory"""
    log_dir = Path('/Users/sw/Desktop/stock/rs-log/logs')
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"✅ Created log directory: {log_dir}")

def main():
    print("📋 RS Data Historical Updates - Automated Setup")
    print("=" * 60)
    
    # Create log directory
    create_log_directory()
    
    # Platform detection
    import platform
    if platform.system() == "Darwin":  # macOS
        print("\n🍎 macOS detected - Setting up LaunchAgents...")
        plists = setup_launchagent_macos()
        print(f"\n📌 Next step:")
        print(f"   launchctl load {plists[0]}")
        print(f"   launchctl load {plists[1]}")
    else:
        print("\n🐧 Linux/Unix detected - Using crontab...")
        setup_crontab_linux()
    
    print("\n" + "=" * 60)
    print("✨ Setup complete!")
    print("\nThe historical datasets will be automatically updated:")
    print("  • Polling every 30 minutes Mon-Fri to check upstream")
    print("  • Forced run at Monday-Friday 17:00 (5PM)")
    print("  • Industry Rotation & Stock RS data")
    print("  • Appends only new commits when detected (or forced)")
    print("  • Logs saved to: /Users/sw/Desktop/stock/rs-log/logs/")

if __name__ == "__main__":
    main()
