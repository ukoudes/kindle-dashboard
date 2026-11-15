#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
app_dir="$PWD/Kindle 看板.app"
./.venv/bin/python -c 'import platform; assert platform.system() == "Darwin" and platform.machine() == "arm64", "Build requires Apple Silicon macOS"'
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
/usr/bin/swiftc -swift-version 5 -parse-as-library -O -file-prefix-map "$PWD"=/src/kindle-dashboard -debug-prefix-map "$PWD"=/src/kindle-dashboard -target arm64-apple-macos13.0 -module-cache-path /private/tmp/kindle-swift-cache mac-app/KindleControl.swift mac-app/AccessView.swift mac-app/AppRuntime.swift -o "$app_dir/Contents/MacOS/KindleControl" -framework SwiftUI -framework AppKit
cp mac-app/Usage.txt "$app_dir/Contents/Resources/Usage.txt"
./.venv/bin/python - <<'PYINFO'
from pathlib import Path
import plistlib
p=Path('Kindle 看板.app/Contents/Info.plist')
p.write_bytes(plistlib.dumps({'CFBundleExecutable':'KindleControl','CFBundleIdentifier':'local.kindle-dashboard.control','CFBundleName':'Kindle 看板','CFBundleDisplayName':'Kindle 看板','CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.1.0','CFBundleVersion':'5','CFBundleIconFile':'AppIcon','LSMinimumSystemVersion':'13.0','NSHighResolutionCapable':True,'NSLocalNetworkUsageDescription':'读取和管理本机 Kindle 看板服务。'}))
PYINFO
icon_root=$(mktemp -d /private/tmp/kindle-icon.XXXXXX)
trap '/bin/rm -rf -- "$icon_root"' EXIT
/usr/bin/swiftc -module-cache-path /private/tmp/kindle-swift-cache mac-app/MakeIcon.swift -o "$icon_root/MakeIcon" -framework AppKit
"$icon_root/MakeIcon" "$icon_root/AppIcon.iconset"
./.venv/bin/python - "$icon_root" "$app_dir/Contents/Resources/AppIcon.icns" <<'PYICON'
from PIL import Image
from pathlib import Path
import sys
image=Image.open(Path(sys.argv[1])/'AppIcon.iconset/icon_512x512@2x.png')
image.save(sys.argv[2],format='ICNS')
image.save(Path(sys.argv[2]).parent/'AppMark.png')
PYICON
./.venv/bin/python mac-app/package-runtime.py
/usr/bin/strip -S "$app_dir/Contents/MacOS/KindleControl"
/usr/bin/codesign --force --sign - "$app_dir"
