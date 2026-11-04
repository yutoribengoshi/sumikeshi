"""PyInstaller で EXE にするときの入口。"""
import sys

from sumikeshi.cli import main

sys.exit(main())
