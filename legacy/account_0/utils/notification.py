import json
import traceback
from datetime import datetime

import requests

from config import PROXIES, wechat_webhook_url


# 企业微信通知
def send_msg_q_wechat(content):
    try:
        data = {
            "msgtype": "text",
            "text": {
                "content": content + '\n' + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        }
        r = requests.post(wechat_webhook_url, data=json.dumps(data), timeout=10, proxies=PROXIES)
        print(f'调用企业微信接口返回： {r.text}')
        print('成功发送企业微信')
    except Exception as e:
        print(f"发送企业微信失败:{e}")
        print(traceback.format_exc())
