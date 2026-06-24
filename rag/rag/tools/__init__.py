"""``rag`` 的离线脚本工具集 — 不参与运行时.

放这里的脚本约定:

- 入口 ``python -m bapee.rag.tools.<name>`` 跑得通.
- 不依赖 ``bapee.settings`` / ``bapee.chatbot.*``, 保持 rag 单向依赖.

目前只有一个:

- :mod:`.build_kb_ast` — markdown → ``*.chunks.jsonl`` 离线构建.
  ``corpus.load_chunks`` 默认读 ``docs/ast/*.chunks.jsonl``, 跑这个脚本就是给它备料.
"""
