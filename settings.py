"""Allowlisted global settings persisted atomically in a writable .env directory."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading


class SettingsError(ValueError):
    pass


# key: (label, default, constraint, group). Strings with tuple constraints are enums.
SCHEMA = {
    'LIVE_TOTAL_MBPS': ('直播独立总带宽（Mbps）', 100, (10, 10000), '直播带宽'),
    'LIVE_UTILIZATION': ('直播可用带宽比例（%）', 80, (1, 100), '直播带宽'),
    'LIVE_CLIENT_MBPS': ('直播单 IP 上限（Mbps，0 为均分）', 16, (0, 10000), '直播带宽'),
    'CACHE_MAX_BYTES': ('媒体缓存上限（GB）', 9000000000, (65536, 10000000000000), '缓存'),
    'REFRESH_SECONDS': ('统计刷新间隔（秒）', 3, (1, 60), '页面'),
    'FOOTER_ICP': ('ICP备案号', '', 100, '页面'),
    'FOOTER_NOTICE': ('个人使用说明', '本服务仅供授权用户使用，请输入管理密钥。\n如果您没有访问权限，请关闭本页面。', 2000, '页面'),
    'DEBUG_ENABLED': ('启用调试日志', False, None, '页面'),
    'BANDWIDTH_TOTAL_MBPS': ('服务器总带宽（Mbps）', 200, (10, 10000), '带宽'),
    'BANDWIDTH_UTILIZATION': ('可用带宽比例（%）', 80, (1, 100), '带宽'),
    'BANDWIDTH_CLIENT_MBPS': ('单 IP 上限（Mbps，0 为均分）', 32, (0, 10000), '带宽'),
    'DEFAULT_HEIGHT': ('默认分辨率上限', 1080, (720, 1080, 2160), '播放默认值'),
    'DEFAULT_VIDEO_BITRATE': ('默认视频码率（bps）', 8000000, (500000, 20000000), '播放默认值'),
    'DEFAULT_AUDIO_BITRATE': ('默认 AAC 码率（bps）', 192000, (64000, 512000), '播放默认值'),
    'DEFAULT_TRANSCODE_VIDEO': ('默认重新编码视频', True, None, '播放默认值'),
    'DEFAULT_VIDEO_CODEC': ('默认视频编码', 'h264', ('h264', 'hevc'), '播放默认值'),
    'DEFAULT_TRANSCODE_AUDIO': ('默认转为 AAC', True, None, '播放默认值'),
    'DEFAULT_PRELOAD_PERCENT': ('默认开头预载比例（%）', 5, (1, 100), '播放默认值'),
    'HLS_TIMEOUT_SECONDS': ('NAS 分片请求超时（秒）', 90, (10, 300), 'NAS 请求'),
    'HLS_IDLE_SECONDS': ('NAS 转码空闲回收（秒）', 120, (30, 3600), 'NAS 请求'),
    'NAS_ORIGIN': ('Jellyfin HTTPS 地址（不含路径）', '', 2048, 'NAS 硬件转码'),
    'NAS_API_KEY': ('Jellyfin 管理 API 密钥（留空保留）', '', 1024, 'NAS 硬件转码'),
    'NAS_ACCELERATION': ('硬件加速后端', 'nvenc', ('none', 'nvenc', 'qsv', 'vaapi', 'amf'), 'NAS 硬件转码'),
    'NAS_HARDWARE_ENCODING': ('启用硬件编码', True, None, 'NAS 硬件转码'),
    'NAS_VAAPI_DEVICE': ('VA-API 设备（Linux）', '/dev/dri/renderD128', 200, 'NAS 硬件转码'),
    'NAS_QSV_DEVICE': ('QSV 设备（留空自动）', '', 200, 'NAS 硬件转码'),
    'NAS_DECODING_CODECS': ('硬解编码（逗号分隔）', 'h264,hevc', 200, 'NAS 硬件转码'),
    'NAS_HEVC_10BIT': ('启用 HEVC 10-bit 硬解', True, None, 'NAS 硬件转码'),
    'NAS_TONEMAPPING': ('启用色调映射', False, None, 'NAS 硬件转码'),
    'NAS_VPP_TONEMAPPING': ('启用 Intel VPP 色调映射', False, None, 'NAS 硬件转码'),
    'NAS_LOW_POWER_H264': ('启用 Intel H.264 低功耗编码', False, None, 'NAS 硬件转码'),
    'NAS_ENCODING_THREADS': ('软件编码线程（-1 为自动）', -1, (-1, 128), 'NAS 硬件转码'),
}
BW_KEYS = dict(total_mbps='BANDWIDTH_TOTAL_MBPS', utilization='BANDWIDTH_UTILIZATION', client_mbps='BANDWIDTH_CLIENT_MBPS')


def read_env(text):
    result = {}
    for line in text.splitlines():
        match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)', line)
        if not match:
            continue
        key, value = match.groups()
        if value.startswith("'") and value.endswith("'"):
            value = re.sub(r"\\([\\'n])", lambda m: {'n': '\n', "'": "'", '\\': '\\'}[m[1]], value[1:-1])
        elif value.startswith('"') and value.endswith('"'):
            value = json.loads(value)
        else:
            value = value.split(' #', 1)[0].strip()
        result[key] = value
    return result


def encoded(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n') + "'"


class Settings:
    def __init__(self, path, legacy_bandwidth=None, cache_limit=9000000000):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.values = {k: v[1] for k, v in SCHEMA.items()}
        self.values['CACHE_MAX_BYTES'] = cache_limit
        self.raw = self.path.read_text(encoding='utf-8') if self.path.exists() else ''
        source = {**{k: os.environ[k] for k in SCHEMA if k in os.environ}, **read_env(self.raw)}
        if legacy_bandwidth:
            for key, env in BW_KEYS.items():
                if env not in source:
                    self.values[env] = legacy_bandwidth[key]
        for key in SCHEMA:
            if key not in source:
                continue
            value = source[key]
            default = SCHEMA[key][1]
            if type(default) is bool:
                if value.lower() not in ('true', 'false'):
                    raise SettingsError(f'{key} 必须为 true/false')
                value = value.lower() == 'true'
            elif type(default) is int:
                value = float(value) if key in BW_KEYS.values() else int(value)
            self.values[key] = value
        self.validate(self.values)

    @staticmethod
    def validate(values):
        if set(values) != set(SCHEMA):
            raise SettingsError('配置项不完整或包含未知配置')
        for key, value in values.items():
            label, default, constraint, _ = SCHEMA[key]
            if type(default) is bool:
                valid = type(value) is bool
            elif type(default) is int:
                valid = type(value) in (int, float) and math.isfinite(value)
                valid = valid and (key in BW_KEYS.values() or int(value) == value)
                valid = valid and (value in constraint if key == 'DEFAULT_HEIGHT' else constraint[0] <= value <= constraint[1])
            else:
                valid = isinstance(value, str) and not any(ord(c) < 32 and c != '\n' for c in value)
                valid = valid and (value in constraint if isinstance(constraint, tuple) else len(value) <= constraint)
                valid = valid and (key == 'FOOTER_NOTICE' or '\n' not in value)
            if not valid:
                raise SettingsError(label + '的值无效')
        if values['BANDWIDTH_TOTAL_MBPS'] * values['BANDWIDTH_UTILIZATION'] / 100 < 10 or 0 < values['BANDWIDTH_CLIENT_MBPS'] < 10:
            raise SettingsError('带宽不能低于每 IP 10 Mbps；单 IP 上限可设为 0 表示均分')
        if values['LIVE_TOTAL_MBPS'] * values['LIVE_UTILIZATION'] / 100 < 10 or 0 < values['LIVE_CLIENT_MBPS'] < 10:
            raise SettingsError('直播带宽不能低于每 IP 10 Mbps；单 IP 上限可设为 0')
        if any(v not in ('h264', 'hevc', 'mpeg2video', 'mpeg4', 'vc1', 'vp8', 'vp9', 'av1') for v in values['NAS_DECODING_CODECS'].split(',') if v):
            raise SettingsError('硬解编码列表无效，请使用逗号分隔的编码名称')

    def revision(self):
        return hashlib.sha256(self.raw.encode()).hexdigest()

    def snapshot(self):
        with self.lock:
            return {'values': {k: v for k, v in self.values.items() if k != 'NAS_API_KEY'},
                    'nas_key_configured': bool(self.values['NAS_API_KEY']), 'revision': self.revision(),
                    'schema': {k: {'label': v[0], 'default': v[1], 'constraint': v[2], 'group': v[3]} for k, v in SCHEMA.items()}}

    def prepare(self, patch):
        if not isinstance(patch, dict) or set(patch) - set(SCHEMA):
            raise SettingsError('包含不可修改的配置项')
        patch = dict(patch)
        if patch.get('NAS_API_KEY') == '':
            patch.pop('NAS_API_KEY')
        values = {**self.values, **patch}
        self.validate(values)
        return values

    def save(self, values, revision=None):
        self.validate(values)
        with self.lock:
            raw = self.path.read_text(encoding='utf-8') if self.path.exists() else ''
            if raw != self.raw or revision is not None and revision != self.revision():
                raise SettingsError('配置已被其他页面或文件修改，请重新加载设置（手动改 .env 后需重启服务）')
            lines = []
            for line in raw.splitlines():
                match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=', line)
                if not match or match[1] not in SCHEMA:
                    lines.append(line)
            text = '\n'.join(lines).rstrip() + '\n' + '\n'.join(k + '=' + encoded(v) for k, v in values.items()) + '\n'
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=self.path.parent, delete=False) as file:
                    name = file.name
                    os.chmod(name, 0o600)
                    file.write(text)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.path)
            finally:
                if name and os.path.exists(name):
                    os.unlink(name)
            self.raw, self.values = text, dict(values)
