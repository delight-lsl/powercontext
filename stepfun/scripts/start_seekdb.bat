@echo off
echo ===================================================
echo [SeekDB] 正在启动 SeekDB 本地实例...
echo ===================================================
docker run -d --name seekdb-instance -p 2881:2881 -p 2886:2886 oceanbase/seekdb:latest
echo SeekDB 容器启动指令已执行! 访问端口: 2881
pause
