import os
import time

# 因子/选币函数内部读机器时区；测试统一钉到东八区，保证金标确定性。
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()
