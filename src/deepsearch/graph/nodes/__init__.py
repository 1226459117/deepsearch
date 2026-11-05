"""主图节点集合。每个节点是 (state)->partial dict 的纯函数,返回值经 reducer
合并进 state;跨域持久(对话/记忆)一律经 memory 门面,节点自身不直连存储。
"""
