# 故障接续

读取精确 run 的 run.json 和对应角色 attempt 目录中的 outcome.json、stdout.txt、stderr.txt。结果未知先核对仍在执行的进程与实际 Git/远端结果，复用同一 run 接续，不复制旧角色手册或重新创建整轮评审。
