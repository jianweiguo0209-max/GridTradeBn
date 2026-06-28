# GridTradeGP 运行时镜像（scheduler + monitor 两个进程共用一个镜像）。
# Python 3.9（硬约束：pandas==1.3.5 / numpy==1.22.4 / TA-Lib 0.6.8 bundled wheel）。
FROM python:3.9-slim

WORKDIR /app

# 依赖：pandas/numpy/ccxt/pyarrow/SQLAlchemy/psycopg2-binary 均为 manylinux wheel；
# TA-Lib 0.6.8 也是 bundled wheel（含 C 库），故无需 apt 装系统 ta-lib（与 CI 一致）。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir TA-Lib==0.6.8

# 业务代码（gridtrade 为纯包目录，WORKDIR=/app 即可 import）
COPY gridtrade ./gridtrade

# 全程 UTC 存储；offset 公式由 UTC_OFFSET 配置驱动（默认 8，见 config.py）
ENV PYTHONUNBUFFERED=1 \
    TZ=UTC

# 默认跑常驻 monitor；scheduler 由 fly process group / scheduled machine 覆盖命令。
CMD ["python", "-m", "gridtrade.runtime.monitor"]
