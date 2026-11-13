#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")/.."
python_env=.venv311/bin/python

if [[ ${1:-} == --setup ]]; then
  if [[ ! -x "$python_env" ]]; then
    python311=$(command -v python3.11 || true)
    for candidate in "$python311" /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 "$HOME/.local/bin/python3.11"; do
      if [[ -x "$candidate" ]]; then
        "$candidate" -m venv .venv311
        break
      fi
    done
  fi
  if [[ ! -x "$python_env" ]]; then
    print -u2 '未找到 Python 3.11。安装 Python 3.11 后重新运行 --setup。'
    exit 1
  fi
  "$python_env" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else "工作台需要 Python 3.11")'
  "$python_env" -m pip install --disable-pip-version-check --only-binary=:all: -r requirements-workbench.txt
  "$python_env" -c 'import numpy, pandas, pyarrow, requests'
  print '研究工作台依赖已就绪。现在可双击本启动器。'
  exit 0
fi

if [[ ! -x "$python_env" ]]; then
  print -u2 '未找到项目 Python 环境。请在项目目录运行：'
  print -u2 './macos/启动中文回测中心.command --setup'
  if [[ -t 0 ]]; then
    read -r '?按回车关闭窗口'
  fi
  exit 1
fi
exec "$python_env" -m research_center.launcher "$@"
