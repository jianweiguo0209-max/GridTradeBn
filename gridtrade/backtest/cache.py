"""
按天分片的 Parquet 缓存（对应回测设计文档 支柱五 5.1）。

设计要点：
- 复用的最小单元是 (namespace, symbol, day)，跨窗口、跨 offset、跨信号源都能复用同一天的数据。
- 空哨兵：「这天没数据」也落一个 schema-only 的空 parquet，exists() 仍返回 True，
  区分「没取过」与「取过=空」，避免对无数据的天反复重取。
- 原子写：先写临时文件再 os.replace 原子改名，杜绝中断留下的 0 字节脏文件。
- exists() 是廉价 stat（不读 parquet 内容），让预热可以「存在即跳过」做到幂等。

注意：依赖 pyarrow（见 requirements.txt）。运行环境需与实盘一致（pandas 1.3.5 + pyarrow）。
"""
import os
import tempfile

import pandas as pd


class ParquetCache:
    def __init__(self, root):
        self.root = root

    def _dir(self, namespace, symbol):
        return os.path.join(self.root, namespace, symbol)

    def _path(self, namespace, symbol, day):
        # day: 'YYYY-MM-DD' 字符串
        return os.path.join(self._dir(namespace, symbol), '%s.parquet' % day)

    def exists(self, namespace, symbol, day):
        """廉价 stat：文件存在且非 0 字节即视为已缓存（含空哨兵）。"""
        p = self._path(namespace, symbol, day)
        return os.path.exists(p) and os.path.getsize(p) > 0

    def read(self, namespace, symbol, day):
        p = self._path(namespace, symbol, day)
        if not (os.path.exists(p) and os.path.getsize(p) > 0):
            return None
        return pd.read_parquet(p)

    def write(self, namespace, symbol, day, df):
        """原子写：临时文件 + os.replace。"""
        d = self._dir(namespace, symbol)
        os.makedirs(d, exist_ok=True)
        p = self._path(namespace, symbol, day)
        fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
        os.close(fd)
        try:
            df.to_parquet(tmp, index=False)
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def write_empty(self, namespace, symbol, day, columns):
        """落空哨兵：这天确认没数据，写一个 schema-only 空 parquet。"""
        self.write(namespace, symbol, day, pd.DataFrame(columns=columns))

    def list_symbols(self, namespace):
        """列举某 namespace 下已缓存的所有 canonical symbol（如 'BTC/USDC:USDC'）。
        落盘结构 root/ns/<base>/<quote:settle>/ 两级 → 重建带 '/' 的 canonical。"""
        base = os.path.join(self.root, namespace)
        if not os.path.isdir(base):
            return []
        out = []
        for a in sorted(os.listdir(base)):
            ad = os.path.join(base, a)
            if not os.path.isdir(ad):
                continue
            for b in sorted(os.listdir(ad)):
                if os.path.isdir(os.path.join(ad, b)):
                    out.append('%s/%s' % (a, b))
        return out

    def list_days(self, namespace, symbol):
        """廉价列举某 symbol 在该 namespace 下已缓存的天（不读 parquet 内容）。
        返回排序后的 'YYYY-MM-DD' 列表；目录不存在则空列表。"""
        d = self._dir(namespace, symbol)
        if not os.path.isdir(d):
            return []
        return sorted(fn[:-len('.parquet')] for fn in os.listdir(d) if fn.endswith('.parquet'))

    def read_all_days(self, namespace, symbol):
        """读取某 symbol 在该 namespace 下所有已缓存天的数据，合并返回（按天排序）。"""
        frames = []
        for day in self.list_days(namespace, symbol):
            p = self._path(namespace, symbol, day)
            if os.path.getsize(p) == 0:
                continue
            try:
                frames.append(pd.read_parquet(p))
            except BaseException:
                continue
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)
