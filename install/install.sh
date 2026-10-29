#!/bin/sh
# NOLGIA for DaVinci Resolve: copies the plugin into Resolve's Scripts folder for this user.
set -e
here=$(cd "$(dirname "$0")" && pwd)
case "$(uname -s)" in
  Darwin) target="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility" ;;
  *) target="$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Utility" ;;
esac
if [ ! -f "$here/Utility/NOLGIA.py" ]; then
  echo "Could not find the Utility folder next to this installer. Unzip the whole download first."
  exit 1
fi
if pgrep -x Resolve >/dev/null 2>&1 || pgrep -x resolve >/dev/null 2>&1; then
  # Resolve finds scripts when it starts, so it has to be quit and started again.
  echo "DaVinci Resolve is open. NOLGIA is installed anyway; to start using it:"
  echo "  1. Close the NOLGIA window if it is open."
  echo "  2. Quit Resolve: DaVinci Resolve > Quit DaVinci Resolve (Command-Q on a Mac)."
  echo "With NOLGIA 0.2.2 or older open, that quit leaves Resolve running with no"
  echo "windows; closing the NOLGIA window first avoids it (0.2.3 and newer finish the"
  echo "quit themselves). If Resolve is already left with no windows, force quit it"
  echo "(Option-Command-Esc on a Mac)."
  echo
fi
mkdir -p "$target"
cp "$here/Utility/NOLGIA.py" "$here/Utility/nolgia_resolve.zip" "$target/"
echo "NOLGIA is installed in:"
echo "  $target"
echo "Restart DaVinci Resolve, then choose NOLGIA in the Workspace > Scripts menu."
